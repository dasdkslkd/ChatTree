from __future__ import annotations

import argparse
import json
import threading
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.core.perf.aggregate import summarize_events, write_reports
from backend.core.persistence.home import resolve_chattree_home


DEFAULT_API_BASE_URL = "http://127.0.0.1:8001/api/v1"


def _api_url(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run ChatTree real-call performance trial.")
    parser.add_argument(
        "--base-url",
        default=DEFAULT_API_BASE_URL,
        help="ChatTree API base URL, including the version prefix.",
    )
    parser.add_argument("--duration-seconds", type=float, default=60)
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--prompt-file")
    parser.add_argument("--conversation-id")
    parser.add_argument("--parent-node-id")
    parser.add_argument("--provider-id")
    parser.add_argument("--model-id")
    parser.add_argument("--output-dir")
    parser.add_argument("--enable-server-perf", action="store_true")
    return parser


def _json_request(
    base_url: str,
    path: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    timeout: int = 30,
) -> Any:
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        _api_url(base_url, path),
        data=data,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read().decode("utf-8")
        return json.loads(body) if body else None


def _append_jsonl(path: Path, lock: threading.Lock, event: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with lock:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True))
            handle.write("\n")


def _create_conversation(base_url: str) -> dict[str, Any]:
    return _json_request(
        base_url,
        "/conversations",
        method="POST",
        payload={"title": "Performance Trial"},
    )


def _stream_once(
    *,
    base_url: str,
    conversation_id: str,
    parent_node_id: str,
    prompt: str,
    output_path: Path,
    lock: threading.Lock,
    provider_id: str | None,
    model_id: str | None,
    worker_id: int,
    request_index: int,
) -> str:
    request_id = uuid.uuid4().hex
    started = time.perf_counter()
    first_event_ms: float | None = None
    event_count = 0
    last_node_id = parent_node_id
    payload = {
        "content": prompt,
        "parent_node_id": parent_node_id,
        "focus_new_node": True,
    }
    if provider_id:
        payload["provider_id"] = provider_id
    if model_id:
        payload["model_id"] = model_id

    _append_jsonl(output_path, lock, {
        "type": "mark",
        "name": "trial.request_start",
        "ts": time.time(),
        "request_id": request_id,
        "conversation_id": conversation_id,
        "worker_id": worker_id,
        "request_index": request_index,
    })

    try:
        request = urllib.request.Request(
            _api_url(base_url, f"/conversations/{conversation_id}/messages/stream"),
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=600) as response:
            buffer = ""
            while True:
                raw = response.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace")
                if line.strip() == "":
                    part = buffer.strip()
                    buffer = ""
                    if not part.startswith("data: "):
                        continue
                    data = part[6:]
                    if data == "[DONE]":
                        break
                    event_started = time.perf_counter()
                    parsed = json.loads(data)
                    if first_event_ms is None:
                        first_event_ms = (event_started - started) * 1000
                    event_count += 1
                    last_node_id = parsed.get("target_node_id") or parsed.get("node_id") or last_node_id
                    _append_jsonl(output_path, lock, {
                        "type": "span",
                        "name": "trial.sse_event",
                        "duration_ms": (time.perf_counter() - event_started) * 1000,
                        "ts": time.time(),
                        "request_id": request_id,
                        "run_id": parsed.get("run_id"),
                        "conversation_id": conversation_id,
                        "node_id": last_node_id,
                        "attrs": {
                            "status": parsed.get("status"),
                            "event_type": parsed.get("event_type"),
                            "event_index": parsed.get("event_index"),
                        },
                    })
                    continue
                buffer += line
        status = "completed"
        error = None
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        status = "error"
        error = str(exc)

    _append_jsonl(output_path, lock, {
        "type": "span",
        "name": "trial.request",
        "duration_ms": (time.perf_counter() - started) * 1000,
        "ts": time.time(),
        "request_id": request_id,
        "conversation_id": conversation_id,
        "node_id": last_node_id,
        "attrs": {
            "status": status,
            "error": error,
            "event_count": event_count,
            "first_event_ms": first_event_ms,
            "worker_id": worker_id,
            "request_index": request_index,
        },
    })
    return last_node_id


def _worker(
    *,
    base_url: str,
    duration_seconds: float,
    conversation_id: str,
    initial_parent_node_id: str,
    prompt: str,
    output_path: Path,
    lock: threading.Lock,
    provider_id: str | None,
    model_id: str | None,
    worker_id: int,
) -> None:
    deadline = time.monotonic() + duration_seconds
    parent_node_id = initial_parent_node_id
    request_index = 0
    while time.monotonic() < deadline:
        parent_node_id = _stream_once(
            base_url=base_url,
            conversation_id=conversation_id,
            parent_node_id=parent_node_id,
            prompt=prompt,
            output_path=output_path,
            lock=lock,
            provider_id=provider_id,
            model_id=model_id,
            worker_id=worker_id,
            request_index=request_index,
        )
        request_index += 1


def main() -> int:
    args = _build_parser().parse_args()

    perf_run_id = f"trial_{uuid.uuid4().hex[:12]}"
    output_dir = Path(args.output_dir).expanduser() if args.output_dir else resolve_chattree_home() / "perf" / "runs" / perf_run_id
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / "trial-events.jsonl"
    previous_server_perf: dict[str, Any] | None = None
    if args.enable_server_perf:
        safe_server_root = (resolve_chattree_home() / "perf").resolve()
        resolved_output_dir = output_dir.resolve(strict=False)
        try:
            resolved_output_dir.relative_to(safe_server_root)
        except ValueError as exc:
            raise SystemExit(
                f"--enable-server-perf requires --output-dir under {safe_server_root}"
            ) from exc
        previous_server_perf = _json_request(args.base_url, "/perf/config")
        _json_request(
            args.base_url,
            "/perf/config",
            method="POST",
            payload={
                "enabled": True,
                "perf_run_id": perf_run_id,
                "output_dir": str(output_dir),
            },
        )
    prompt = (
        Path(args.prompt_file).read_text(encoding="utf-8")
        if args.prompt_file
        else "用三句话总结当前项目的性能风险，并列出一个最小验证步骤。"
    )

    try:
        if args.conversation_id:
            conversation_id = args.conversation_id
            parent_node_id = args.parent_node_id
            if not parent_node_id:
                raise SystemExit("--conversation-id requires --parent-node-id for a real stream request")
        else:
            created = _create_conversation(args.base_url)
            conversation_id = created["id"]
            parent_node_id = created["current_node_id"]

        lock = threading.Lock()
        started = time.time()
        with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as executor:
            futures = [
                executor.submit(
                    _worker,
                    base_url=args.base_url,
                    duration_seconds=args.duration_seconds,
                    conversation_id=conversation_id,
                    initial_parent_node_id=parent_node_id,
                    prompt=prompt,
                    output_path=output_path,
                    lock=lock,
                    provider_id=args.provider_id,
                    model_id=args.model_id,
                    worker_id=index,
                )
                for index in range(max(1, args.concurrency))
            ]
            wait(futures)
            for future in futures:
                future.result()

        summary = summarize_events([
            output_dir / "backend-events.jsonl",
            output_dir / "frontend-events.jsonl",
            output_path,
        ])
        summary["trial"] = {
            "base_url": args.base_url,
            "conversation_id": conversation_id,
            "parent_node_id": parent_node_id,
            "duration_seconds": args.duration_seconds,
            "concurrency": max(1, args.concurrency),
            "elapsed_seconds": round(time.time() - started, 3),
        }
        write_reports(summary, output_dir)
        print(str(output_dir))
    finally:
        if previous_server_perf is not None:
            restore_payload = {
                "enabled": bool(previous_server_perf.get("enabled")),
                "perf_run_id": previous_server_perf.get("perf_run_id") or None,
                "output_dir": previous_server_perf.get("output_dir") or None,
                "sample_rate": previous_server_perf.get("sample_rate"),
            }
            try:
                _json_request(args.base_url, "/perf/config", method="POST", payload=restore_payload)
            except Exception as exc:
                print(f"warning: failed to restore server perf config: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
