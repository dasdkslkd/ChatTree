from __future__ import annotations

import asyncio
import json
import re
import subprocess
from pathlib import Path
from contextlib import suppress
from typing import Any, Dict


FORBIDDEN_SCRIPT_TERMS = (
    "require(",
    "import(",
    "child_process",
    "node:",
    "process.",
    "globalThis.process",
    "fs.",
    "net.",
    "http.",
    "https.",
)


class WorkflowScriptError(Exception):
    pass


class WorkflowJsRunner:
    def __init__(self, worker_path: str | Path | None = None) -> None:
        self.worker_path = Path(worker_path) if worker_path else Path(__file__).resolve().parents[2] / "workers" / "workflow_runtime.mjs"

    def validate_script(self, script: str) -> None:
        if not _has_strict_workflow_entrypoint(script):
            raise WorkflowScriptError(
                "workflow script must be `export default async function workflow(ctx) { ... }`"
            )
        lowered = script.lower()
        for term in FORBIDDEN_SCRIPT_TERMS:
            if term.lower() in lowered:
                raise WorkflowScriptError(f"workflow script contains forbidden term: {term}")

    async def run(
        self,
        *,
        script: str,
        args: Dict[str, Any],
        budget: Dict[str, Any],
        bridge,
    ) -> Any:
        self.validate_script(script)
        process = subprocess.Popen(
            ["node", str(self.worker_path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert process.stdin is not None
        assert process.stdout is not None

        start_payload = {
            "script": script,
            "args": args,
            "budget": budget,
        }
        await asyncio.to_thread(self._write_worker_message, process, start_payload)

        host_calls = 0
        max_host_calls = int(budget.get("max_host_calls") or 200)
        write_lock = asyncio.Lock()
        host_tasks: set[asyncio.Task] = set()

        async def send_host_result(result: Dict[str, Any]) -> None:
            if process.stdin is None or process.stdin.closed:
                return
            async with write_lock:
                await asyncio.to_thread(self._write_worker_message, process, result)

        async def handle_host_call(message: Dict[str, Any]) -> None:
            if message.get("over_budget"):
                result = {"type": "host_result", "id": message.get("id"), "error": "workflow host call budget exhausted"}
            else:
                try:
                    value = await bridge.handle_call(
                        str(message.get("method") or ""),
                        message.get("params") or {},
                    )
                    result = {"type": "host_result", "id": message.get("id"), "result": value}
                except Exception as exc:
                    result = {"type": "host_result", "id": message.get("id"), "error": str(exc)}
            await send_host_result(result)

        try:
            while True:
                line = await asyncio.to_thread(process.stdout.readline)
                if not line:
                    break
                message = json.loads(line.decode("utf-8"))
                msg_type = message.get("type")
                if msg_type == "host_call":
                    host_calls += 1
                    message["over_budget"] = host_calls > max_host_calls
                    task = asyncio.create_task(handle_host_call(message))
                    host_tasks.add(task)
                    task.add_done_callback(host_tasks.discard)
                elif msg_type == "done":
                    if host_tasks:
                        await asyncio.gather(*host_tasks, return_exceptions=True)
                    await asyncio.to_thread(process.wait)
                    return message.get("result")
                elif msg_type == "error":
                    if host_tasks:
                        await asyncio.gather(*host_tasks, return_exceptions=True)
                    await asyncio.to_thread(process.wait)
                    raise WorkflowScriptError(str(message.get("error") or "workflow failed"))

            stderr = b""
            if process.stderr is not None:
                stderr = await asyncio.to_thread(process.stderr.read)
            await asyncio.to_thread(process.wait)
            raise WorkflowScriptError(stderr.decode("utf-8", errors="replace") or "workflow worker exited without result")
        finally:
            for task in host_tasks:
                task.cancel()
            if host_tasks:
                await asyncio.gather(*host_tasks, return_exceptions=True)
            if process.poll() is None:
                with suppress(ProcessLookupError):
                    process.kill()
                with suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(asyncio.to_thread(process.wait), timeout=1)
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None and not stream.closed:
                    stream.close()

    def _write_worker_message(self, process: subprocess.Popen, payload: Dict[str, Any]) -> None:
        if process.stdin is None or process.stdin.closed:
            return
        process.stdin.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
        process.stdin.flush()


def _has_strict_workflow_entrypoint(script: str) -> bool:
    return re.search(
        r"^\s*export\s+default\s+async\s+function\s+workflow\s*\(\s*ctx\s*\)\s*\{",
        script,
    ) is not None
