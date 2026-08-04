"""模拟网络中断，验证三层流恢复机制是否按预期触发。

运行方式（仓库根目录）：
    python scripts/simulate_stream_recovery.py

三层机制：
    Layer 1a   Chat Completions：流输出到一半断网后简单重发请求
    Layer 1b   Responses API：断网后用 previous_response_id 让服务端从断点回放
    Layer 2    所有重试耗尽的 stream 失败后，_produce_chat_run 自动注入
              "Continue from where you left off." 隐藏消息续写

本脚本起一个本地 mock SSE 服务器，在首个请求输出部分内容后模拟断网
（挂起连接触发超时），观察真实 HTTP/SSE 栈是否触发重试/续写。
"""

import asyncio
import http.server
import json
import sys
import threading
import time
from pathlib import Path

# 让脚本能 import backend.*（仓库根目录）
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("recovery-demo")

from backend.core.config.config import DEFAULT_MODEL_TRANSPORT
from backend.core.config.types import Message, ModelRoute, Role, StreamStatus
from backend.core.model.providers.openai_compatible import OpenAICompatibleProvider
from backend.api.routes.messages import SendMessageRequest, _produce_chat_run
from backend.core.runs import RunKind, RunManager
from backend.core.runs.repository import MemoryRunRepository


# ────────────────────────────────────────────────────────────────────
# 本地 mock SSE 服务器：首请求中断，重试返回完整内容
# ────────────────────────────────────────────────────────────────────

class _MockHandler(http.server.BaseHTTPRequestHandler):
    """行为：第 1 个请求输出部分内容后挂起连接（模拟断网超时），
    后续请求返回完整 SSE（模拟服务端断点续输出）。"""

    protocol_version = "HTTP/1.1"
    requests = []  # list[(path, body_bytes)]

    def log_message(self, *args):
        pass  # 关闭默认访问日志

    @classmethod
    def reset(cls):
        cls.requests = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        _MockHandler.requests.append((self.path, body))
        attempt = len(_MockHandler.requests) - 1

        partial, complete = self._frames(self.path)
        try:
            if attempt == 0:
                # 模拟网络中断：已输出部分内容后挂起，客户端将触发 idle 超时
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.wfile.write(partial)
                self.wfile.flush()
                time.sleep(60)
                return
            # 重试：返回完整内容
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(complete)))
            self.end_headers()
            self.wfile.write(complete)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _frames(self, path):
        if path.endswith("/chat/completions"):
            partial = (
                b'data: {"choices":[{"delta":{"content":"Hello "}}]}\n\n'
            )
            complete = (
                b'data: {"choices":[{"delta":{"content":"world"},'
                b'"finish_reason":"stop"}]}\n\n'
                b"data: [DONE]\n\n"
            )
        else:  # responses
            resp_id = "resp_demo_123"
            partial = (
                b'data: {"type":"response.created","response":{"id":"'
                + resp_id.encode()
                + b'"}}\n\n'
                b'data: {"type":"response.output_text.delta","delta":"Hello "}\n\n'
            )
            complete = (
                b'data: {"type":"response.output_text.delta","delta":"world"}\n\n'
                b'data: {"type":"response.completed","response":'
                b'{"output":[],"usage":{"total_tokens":10}}}\n\n'
            )
        return partial, complete


def _start_server():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _MockHandler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, port


def _provider(port, protocol):
    transport = dict(DEFAULT_MODEL_TRANSPORT)
    transport.update({
        "stream_idle_timeout_seconds": 1.5,   # 加快演示：1.5s 无事件即判定断流
        "connect_timeout_seconds": 2.0,
        "first_event_timeout_seconds": 5.0,
        "retry_base_delay_seconds": 0.1,
        "retry_max_delay_seconds": 0.5,
        "max_stream_retries": 3,
    })
    config = {
        "api_key": "test-key",
        "base_url": f"http://127.0.0.1:{port}/v1",
        "model_transport": transport,
    }
    if protocol == "openai_responses":
        config["base_url"] = f"http://127.0.0.1:{port}/v1"
    route = ModelRoute(
        route_id=f"demo:{protocol}",
        provider_id="demo",
        model_id="demo-model",
        protocol=protocol,
        endpoint=(
            "/chat/completions" if protocol == "openai_chat_completions"
            else "/responses"
        ),
        reasoning_profile={
            "name": "demo", "carrier": "none",
            "history_policy": "drop", "strict": False,
        },
    )
    return OpenAICompatibleProvider(config, route)


def _messages():
    return [Message({
        "id": "msg-1", "role": Role.USER, "content": "hello", "timestamp": 1,
    })]


PASSED = 0


def _ok(name):
    global PASSED
    PASSED += 1
    print(f"  [PASS] {name}")


def _fail(name, detail):
    print(f"  [FAIL] {name}: {detail}")
    sys.exit(1)


# ────────────────────────────────────────────────────────────────────
# 场景 1：Layer 1a  Chat Completions 简单重试
# ────────────────────────────────────────────────────────────────────

def simulate_chat_completions(port):
    print("\n=== Layer 1a: Chat Completions 输出中断后简单重试 ===")
    _MockHandler.reset()
    provider = _provider(port, "openai_chat_completions")
    text = []

    async def run():
        async for chunk in provider.generate_response_stream("demo-model", _messages()):
            if chunk["status"] == StreamStatus.CONTENT and chunk.get("content"):
                text.append(chunk["content"])

    asyncio.run(run())

    chat_requests = [p for p, _ in _MockHandler.requests]
    if len(chat_requests) >= 2:
        _ok(f"断网后自动重试（收到 {len(chat_requests)} 次请求）")
    else:
        _fail("重试未触发", f"requests={len(chat_requests)}")

    joined = "".join(text)
    if "world" in joined:
        _ok(f"重试后拿到完整输出（含重复前缀属预期: '{joined}')")
    else:
        _fail("缺少完整输出", repr(joined))


# ────────────────────────────────────────────────────────────────────
# 场景 2：Layer 1b  Responses API previous_response_id 服务端回放
# ────────────────────────────────────────────────────────────────────

def simulate_responses_api(port):
    print("\n=== Layer 1b: Responses API 断点回放（previous_response_id）===")
    _MockHandler.reset()
    provider = _provider(port, "openai_responses")
    text = []

    async def run():
        async for chunk in provider.generate_response_stream(
            "demo-model", _messages(), temperature=None,
        ):
            if chunk["status"] == StreamStatus.CONTENT and chunk.get("content"):
                text.append(chunk["content"])

    asyncio.run(run())

    if len(_MockHandler.requests) < 2:
        _fail("重试未触发", f"requests={len(_MockHandler.requests)}")

    retry_body = json.loads(_MockHandler.requests[1][1].decode())
    if retry_body.get("previous_response_id") == "resp_demo_123":
        _ok("重试请求携带 previous_response_id=resp_demo_123")
    else:
        _fail("缺少 previous_response_id", str(retry_body.keys()))

    joined = "".join(text)
    if joined == "Hello world":
        _ok(f"服务端从断点续输出，无重复前缀: '{joined}'")
    else:
        _fail("内容不完整", repr(joined))


# ────────────────────────────────────────────────────────────────────
# 场景 3：Layer 2  _produce_chat_run 自动续写
# ────────────────────────────────────────────────────────────────────

def simulate_auto_continue():
    print("\n=== Layer 2: run 失败后自动注入 'Continue from where you left off.' ===")

    class FakeChatManager:
        def __init__(self):
            self.calls = []
            self._i = 0

        def send_message_stream(self, **kwargs):
            self.calls.append(dict(kwargs))
            idx = self._i
            self._i += 1

            async def _gen():
                if idx == 0:
                    yield {
                        "status": StreamStatus.CONTENT, "content": "hi",
                        "node_id": "node-target", "conversation_id": "conv-1",
                        "error": None, "tokens_used": 0,
                    }
                    raise TimeoutError("stream idle timeout")
                yield {
                    "status": StreamStatus.CONTENT, "content": " continued",
                    "node_id": "node-target", "conversation_id": "conv-1",
                    "error": None, "tokens_used": 0,
                }
                yield {
                    "status": StreamStatus.COMPLETE, "content": None,
                    "node_id": "node-target", "conversation_id": "conv-1",
                    "error": None, "tokens_used": 0,
                }

            return _gen()

    chat_manager = FakeChatManager()
    run_manager = RunManager(repository=MemoryRunRepository())

    async def scenario():
        run = await run_manager.create_run(
            conversation_id="conv-1", kind=RunKind.CHAT,
        )
        request = SendMessageRequest(
            content="hello", parent_node_id="node-parent",
            model_id="demo-model", provider_id="demo",
        )
        await _produce_chat_run(
            run=run, conversation_id="conv-1", request=request,
            chat_manager=chat_manager, run_manager=run_manager,
        )
        return run

    run = asyncio.run(scenario())

    if len(chat_manager.calls) != 2:
        _fail("auto-continue 未触发", f"calls={len(chat_manager.calls)}")

    second = chat_manager.calls[1]
    if second["content"] == "Continue from where you left off.":
        _ok("自动续写消息已注入")
    else:
        _fail("续写消息不正确", repr(second["content"]))
    if second["append_to_existing_node"] and second["hidden_user_message"]:
        _ok("续写追加到原节点且对用户隐藏")
    else:
        _fail("续写参数不正确",
              f"append={second.get('append_to_existing_node')} "
              f"hidden={second.get('hidden_user_message')}")
    if second["parent_node_id"] == "node-target" and not second["focus_new_node"]:
        _ok("续写锚定在失败节点的目标 node")
    else:
        _fail("续写节点不正确", repr(second["parent_node_id"]))
    print(f"  [INFO] 原始 run={run.run_id} 已标记 FAILED，节点 node-target")


# ────────────────────────────────────────────────────────────────────

def main():
    global PASSED
    server, port = _start_server()
    try:
        simulate_chat_completions(port)
        simulate_responses_api(port)
        simulate_auto_continue()
    finally:
        server.shutdown()

    print(f"\n=== 全部通过 {PASSED} 项 ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())