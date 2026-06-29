from __future__ import annotations

import asyncio
import json
from pathlib import Path
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
        process = await asyncio.create_subprocess_exec(
            "node",
            str(self.worker_path),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert process.stdin is not None
        assert process.stdout is not None

        start_payload = {
            "script": script,
            "args": args,
            "budget": budget,
        }
        process.stdin.write((json.dumps(start_payload, ensure_ascii=False) + "\n").encode("utf-8"))
        await process.stdin.drain()

        host_calls = 0
        max_host_calls = int(budget.get("max_host_calls") or 200)
        while True:
            line = await process.stdout.readline()
            if not line:
                break
            message = json.loads(line.decode("utf-8"))
            msg_type = message.get("type")
            if msg_type == "host_call":
                host_calls += 1
                if host_calls > max_host_calls:
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
                process.stdin.write((json.dumps(result, ensure_ascii=False) + "\n").encode("utf-8"))
                await process.stdin.drain()
            elif msg_type == "done":
                await process.wait()
                return message.get("result")
            elif msg_type == "error":
                await process.wait()
                raise WorkflowScriptError(str(message.get("error") or "workflow failed"))

        stderr = b""
        if process.stderr is not None:
            stderr = await process.stderr.read()
        await process.wait()
        raise WorkflowScriptError(stderr.decode("utf-8", errors="replace") or "workflow worker exited without result")
