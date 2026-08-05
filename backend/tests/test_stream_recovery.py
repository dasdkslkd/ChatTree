"""测试流中断自动恢复机制（三层架构）"""

import asyncio

import httpx
import pytest

from backend.core.config.config import DEFAULT_MODEL_TRANSPORT
from backend.core.config.types import (
    Message,
    ModelRoute,
    Role,
    StreamChunk,
    StreamStatus,
)
from backend.core.runs import RunKind, RunManager, RunStatus
from backend.core.runs.repository import MemoryRunRepository
from backend.api.routes.messages import (
    SendMessageRequest,
    _produce_chat_run,
)


# ── helpers ────────────────────────────────────────────────────────────

def _route(protocol: str) -> ModelRoute:
    endpoints = {
        "openai_chat_completions": "/chat/completions",
        "openai_responses": "/responses",
    }
    return ModelRoute(
        route_id=f"test:model:{protocol}",
        provider_id="test",
        model_id="model",
        protocol=protocol,
        endpoint=endpoints[protocol],
        reasoning_profile={
            "name": "test",
            "carrier": "none",
            "history_policy": "drop",
            "strict": False,
        },
    )


def _message() -> Message:
    return Message(
        {
            "id": "msg-1",
            "role": Role.USER,
            "content": "hello",
            "timestamp": 1,
        }
    )


def _provider_config(extra=None):
    cfg = {
        "api_key": "test-key",
        "base_url": "http://127.0.0.1:9/v1",
        "model_transport": DEFAULT_MODEL_TRANSPORT,
    }
    if extra:
        cfg.update(extra)
    return cfg


# ── 可注入故障的 Provider ──────────────────────────────────────────────

class _FaultyStreamProvider:
    """Mixin: 替换 _iter_sse_events 为按序列注入故障的模拟版本。

    sse_sequences: list of (events, error_or_None)
        每个元素是一个 tuple: (事件列表, 在结束后抛出的异常或 None)
    """

    def __init__(self, *args, sse_sequences=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._sse_sequences = sse_sequences or []
        self._sse_call_count = 0
        self._sse_last_bodies: list[dict] = []

    async def _iter_sse_events(self, path, body, *, stream_controller=None):
        self._sse_call_count += 1
        self._sse_last_bodies.append(dict(body))
        idx = self._sse_call_count - 1
        if idx >= len(self._sse_sequences):
            return
        events, error = self._sse_sequences[idx]
        try:
            for event in events:
                yield event
            if error is not None:
                raise error
        except GeneratorExit:
            pass


# ── Chat Completions / Responses API chunk 工厂函数 ─────────────────────

def _cc_delta(content):
    """单个 Chat Completions delta 事件。"""
    return {"choices": [{"delta": {"content": content}}]}


def _cc_finish(reason="stop"):
    """Chat Completions 完成事件。"""
    return {"choices": [{"delta": {}, "finish_reason": reason}]}


def _resp_chunks(*args):
    """生成 Responses API 事件列表。

    每个参数可以是:
        str        -> response.output_text.delta
        ("created", id) -> response.created
        ("completed", total_tokens) -> response.completed
        ("reasoning", text) -> response.reasoning_summary_text.delta
    """
    events = []
    for arg in args:
        if isinstance(arg, str):
            events.append({"type": "response.output_text.delta", "delta": arg})
        elif arg[0] == "created":
            events.append(
                {"type": "response.created", "response": {"id": arg[1]}}
            )
        elif arg[0] == "completed":
            events.append(
                {
                    "type": "response.completed",
                    "response": {
                        "output": [],
                        "usage": (
                            {"total_tokens": arg[1]} if arg[1] else {}
                        ),
                    },
                }
            )
        elif arg[0] == "reasoning":
            events.append(
                {"type": "response.reasoning_summary_text.delta", "delta": arg[1]}
            )
    return events


# ═══════════════════════════════════════════════════════════════════════
# Layer 1a — Chat Completions：无续传能力，有输出后立即失败
# ═══════════════════════════════════════════════════════════════════════

def test_chat_completions_fails_fast_after_output():
    """模拟输出到一半断流：Chat Completions 无续传能力，不得重试，应立即失败交由上层续写。"""

    sequences = [
        ([_cc_delta("Hello")], TimeoutError("idle timeout")),
        ([_cc_delta(" world"), _cc_finish()], None),
    ]

    class Provider(_FaultyStreamProvider, object):
        pass

    from backend.core.model.providers.openai_compatible import (
        OpenAICompatibleProvider,
    )

    FaultyProvider = type(
        "FaultyChatProvider",
        (Provider, OpenAICompatibleProvider),
        {},
    )

    provider = FaultyProvider(
        _provider_config(), _route("openai_chat_completions"), sse_sequences=sequences
    )

    collected = []
    error_chunks = []

    async def run():
        async for chunk in provider.generate_response_stream(
            "test-model", [_message()],
        ):
            if chunk["status"] == StreamStatus.ERROR:
                error_chunks.append(chunk)
            elif chunk["status"] == StreamStatus.CONTENT and chunk.get("content"):
                collected.append(chunk["content"])

    asyncio.run(run())

    assert provider._sse_call_count == 1
    assert "".join(collected) == "Hello"
    assert len(error_chunks) == 1
    assert "idle timeout" in error_chunks[0]["error"]
    # 终结 ERROR chunk 必须携带错误类型分类，供上层自动续写判定
    assert error_chunks[0]["metadata"]["retryable"] is True


def test_chat_completions_stops_after_max_retries():
    """重试耗尽后应抛出异常。"""

    error = TimeoutError("idle timeout")
    sequences = [([], error)] * 6

    class Provider(_FaultyStreamProvider, object):
        pass

    from backend.core.model.providers.openai_compatible import (
        OpenAICompatibleProvider,
    )

    FaultyProvider = type(
        "FaultyChatProvider",
        (Provider, OpenAICompatibleProvider),
        {},
    )

    provider = FaultyProvider(
        _provider_config(extra={
            "model_transport": {
                **DEFAULT_MODEL_TRANSPORT,
                "retry_base_delay_seconds": 0,
                "retry_max_delay_seconds": 0,
                "retry_jitter_fraction": 0,
            },
        }),
        _route("openai_chat_completions"),
        sse_sequences=sequences,
    )

    error_chunks = []

    async def run():
        async for chunk in provider.generate_response_stream(
            "test-model", [_message()],
        ):
            if chunk["status"] == StreamStatus.ERROR:
                error_chunks.append(chunk)

    asyncio.run(run())

    # 初始尝试 + 5 次重试 = 6 次调用（max_stream_retries=5）
    assert provider._sse_call_count == 6
    assert len(error_chunks) == 1
    assert "idle timeout" in error_chunks[0]["error"]


# ═══════════════════════════════════════════════════════════════════════
# Layer 1b — Responses API：捕获 response_id，重试时注入 previous_response_id
# ═══════════════════════════════════════════════════════════════════════

def test_responses_api_retries_with_previous_response_id():
    """Responses API 收到 response.created 后断流，
    重试请求必须带上 previous_response_id。"""

    resp_id = "resp_test_cafe_1234"
    sequences = [
        (
            _resp_chunks(("created", resp_id), "Hello"),
            TimeoutError("idle timeout"),
        ),
        (
            _resp_chunks(" world", ("completed", 10)),
            None,
        ),
    ]

    class Provider(_FaultyStreamProvider, object):
        pass

    from backend.core.model.providers.openai_compatible import (
        OpenAICompatibleProvider,
    )

    FaultyProvider = type(
        "FaultyResponsesProvider",
        (Provider, OpenAICompatibleProvider),
        {},
    )

    provider = FaultyProvider(
        _provider_config({"base_url": ""}),
        _route("openai_responses"),
        sse_sequences=sequences,
    )

    collected = []
    error_chunks = []

    async def run():
        async for chunk in provider.generate_response_stream(
            "test-model",
            [_message()],
            temperature=None,
        ):
            if chunk["status"] == StreamStatus.ERROR:
                error_chunks.append(chunk)
            elif chunk["status"] == StreamStatus.CONTENT and chunk.get("content"):
                collected.append(chunk["content"])

    asyncio.run(run())

    assert provider._sse_call_count == 2
    assert not error_chunks, f"unexpected error: {error_chunks}"

    # 第二个请求的 body 中应包含 previous_response_id
    second_body = provider._sse_last_bodies[1]
    assert second_body.get("previous_response_id") == resp_id, (
        f"expected previous_response_id={resp_id}, "
        f"got body keys: {sorted(second_body.keys())}"
    )

    assert "".join(collected) == "Hello world"


def test_responses_api_without_created_event_still_retries():
    """如果 response.created 事件从未到达（例如连接在 SSE 握手前断开），
    仍应重试但不注入 previous_response_id。"""

    sequences = [
        ([], TimeoutError("connect timeout")),
        (_resp_chunks("ok", ("completed", 5)), None),
    ]

    class Provider(_FaultyStreamProvider, object):
        pass

    from backend.core.model.providers.openai_compatible import (
        OpenAICompatibleProvider,
    )

    FaultyProvider = type(
        "FaultyResponsesNoCreated",
        (Provider, OpenAICompatibleProvider),
        {},
    )

    provider = FaultyProvider(
        _provider_config({"base_url": ""}),
        _route("openai_responses"),
        sse_sequences=sequences,
    )

    collected = []
    error_chunks = []

    async def run():
        async for chunk in provider.generate_response_stream(
            "test-model",
            [_message()],
            temperature=None,
        ):
            if chunk["status"] == StreamStatus.ERROR:
                error_chunks.append(chunk)
            elif chunk["status"] == StreamStatus.CONTENT and chunk.get("content"):
                collected.append(chunk["content"])

    asyncio.run(run())

    assert provider._sse_call_count == 2
    assert not error_chunks

    # 第二个请求不应包含 previous_response_id
    second_body = provider._sse_last_bodies[1]
    assert "previous_response_id" not in second_body

    assert "".join(collected) == "ok"


# ═══════════════════════════════════════════════════════════════════════
# Layer 2 — auto-continue：run 失败后注入隐藏"继续"消息
# ═══════════════════════════════════════════════════════════════════════

class _FakeRunRepository(MemoryRunRepository):
    """记录 finish_run 调用的内存仓库。"""
    manages_task_bindings = False

    def __init__(self):
        super().__init__()
        self.finished: list[tuple] = []

    def finish_run(self, run_id, status, error=None):
        self.finished.append((run_id, status, error))
        return super().finish_run(run_id, status, error)


@pytest.mark.parametrize("failure", [
    TimeoutError("idle timeout"),
    httpx.ReadError("", request=httpx.Request("POST", "http://test/v1")),
])
def test_auto_continue_triggers_on_recoverable_stream_error(failure):
    """当 send_message_stream 因可恢复网络错误失败且有已绑定节点时，
    _produce_chat_run 应自动调用 send_message_stream 注入隐藏"继续"消息。"""

    CHUNK_ACTIVE = StreamChunk(
        status=StreamStatus.CONTENT,
        content="hi",
        node_id="node-target",
        conversation_id="conv-1",
        error=None,
        tokens_used=0,
    )

    class FakeChatManager:
        def __init__(self):
            self.calls = []
            self._call_index = 0

        def send_message_stream(self, **kwargs):
            self.calls.append(dict(kwargs))
            idx = self._call_index
            self._call_index += 1

            async def _gen():
                if idx == 0:
                    # 第一次调用：产出一些内容后抛出可恢复网络错误
                    yield CHUNK_ACTIVE
                    raise failure
                else:
                    # auto-continue 调用：正常完成
                    yield StreamChunk(
                        status=StreamStatus.CONTENT,
                        content=" continued",
                        node_id="node-target",
                        conversation_id="conv-1",
                        error=None,
                        tokens_used=0,
                    )
                    yield StreamChunk(
                        status=StreamStatus.COMPLETE,
                        content=None,
                        node_id="node-target",
                        conversation_id="conv-1",
                        error=None,
                        tokens_used=0,
                    )

            return _gen()

    chat_manager = FakeChatManager()
    repository = _FakeRunRepository()
    run_manager = RunManager(repository=repository)

    async def scenario():
        run = await run_manager.create_run(
            conversation_id="conv-1", kind=RunKind.CHAT
        )
        request = SendMessageRequest(
            content="hello",
            parent_node_id="node-parent",
            model_id="gpt-4",
            provider_id="openai",
        )

        await _produce_chat_run(
            run=run,
            conversation_id="conv-1",
            request=request,
            chat_manager=chat_manager,
            run_manager=run_manager,
        )
        return run

    run = asyncio.run(scenario())

    # 应调用两次 send_message_stream
    assert len(chat_manager.calls) == 2, (
        f"expected 2 calls, got {len(chat_manager.calls)}"
    )

    # 第一次调用：正常参数
    first = chat_manager.calls[0]
    assert first["content"] == "hello"
    assert not first.get("hidden_user_message")
    assert not first.get("append_to_existing_node")

    # 第二次调用（auto-continue）：隐藏消息，追加到已有节点
    second = chat_manager.calls[1]
    assert second["content"] == "Continue from where you left off."
    assert second["hidden_user_message"] is True
    assert second["suppress_user_message"] is True
    assert second["append_to_existing_node"] is True
    assert second["parent_node_id"] == "node-target"
    assert second["focus_new_node"] is False

    # 原始 run 应标记为 FAILED
    finished = [f for f in repository.finished if f[0] == run.run_id]
    assert len(finished) == 1
    assert finished[0][1] == RunStatus.FAILED.value


def test_auto_continue_skips_on_non_retryable_error():
    """非网络错误的失败不应触发自动续写。"""

    class FakeChatManager:
        def __init__(self):
            self.calls = []

        def send_message_stream(self, **kwargs):
            self.calls.append(dict(kwargs))

            async def _gen():
                raise ValueError("model returned nonsense")

            return _gen()

    chat_manager = FakeChatManager()
    repository = _FakeRunRepository()
    run_manager = RunManager(repository=repository)

    async def scenario():
        run = await run_manager.create_run(
            conversation_id="conv-1", kind=RunKind.CHAT
        )
        request = SendMessageRequest(
            content="hello",
            parent_node_id="node-parent",
        )

        await _produce_chat_run(
            run=run,
            conversation_id="conv-1",
            request=request,
            chat_manager=chat_manager,
            run_manager=run_manager,
        )

    asyncio.run(scenario())

    # 只应调用一次（无 auto-continue）
    assert len(chat_manager.calls) == 1, (
        f"expected 1 call, got {len(chat_manager.calls)}"
    )


def test_auto_continue_triggers_on_error_chunk_with_retryable_metadata():
    """provider 以 ERROR chunk 报告可恢复网络错误时（异常未抛到路由层），
    应依据 chunk metadata.retryable 触发自动续写。"""

    class FakeChatManager:
        def __init__(self):
            self.calls = []
            self._call_index = 0

        def send_message_stream(self, **kwargs):
            self.calls.append(dict(kwargs))
            idx = self._call_index
            self._call_index += 1

            async def _gen():
                if idx == 0:
                    yield StreamChunk(
                        status=StreamStatus.CONTENT,
                        content="hi",
                        node_id="node-target",
                        conversation_id="conv-1",
                        error=None,
                        tokens_used=0,
                    )
                    yield StreamChunk(
                        status=StreamStatus.ERROR,
                        content=None,
                        node_id="node-target",
                        conversation_id="conv-1",
                        error="ReadError",
                        tokens_used=0,
                        metadata={"retryable": True},
                    )
                else:
                    yield StreamChunk(
                        status=StreamStatus.COMPLETE,
                        content=None,
                        node_id="node-target",
                        conversation_id="conv-1",
                        error=None,
                        tokens_used=0,
                    )

            return _gen()

    chat_manager = FakeChatManager()
    repository = _FakeRunRepository()
    run_manager = RunManager(repository=repository)

    async def scenario():
        run = await run_manager.create_run(
            conversation_id="conv-1", kind=RunKind.CHAT
        )
        request = SendMessageRequest(
            content="hello",
            parent_node_id="node-parent",
        )
        await _produce_chat_run(
            run=run,
            conversation_id="conv-1",
            request=request,
            chat_manager=chat_manager,
            run_manager=run_manager,
        )

    asyncio.run(scenario())

    assert len(chat_manager.calls) == 2
    assert chat_manager.calls[1]["content"] == "Continue from where you left off."
    assert chat_manager.calls[1]["append_to_existing_node"] is True


def test_auto_continue_skips_when_no_bound_node():
    """如果流在绑定节点前就失败了（没有 node_id），不应触发自动续写。"""

    class FakeChatManager:
        def __init__(self):
            self.calls = []

        def send_message_stream(self, **kwargs):
            self.calls.append(dict(kwargs))

            async def _gen():
                raise TimeoutError("connect timeout")
                yield  # pragma: no cover

            return _gen()

    chat_manager = FakeChatManager()
    repository = _FakeRunRepository()
    run_manager = RunManager(repository=repository)

    async def scenario():
        run = await run_manager.create_run(
            conversation_id="conv-1", kind=RunKind.CHAT
        )
        request = SendMessageRequest(
            content="hello",
            parent_node_id="node-parent",
        )

        await _produce_chat_run(
            run=run,
            conversation_id="conv-1",
            request=request,
            chat_manager=chat_manager,
            run_manager=run_manager,
        )

    asyncio.run(scenario())

    # 只应调用一次（无 auto-continue，因为没有绑定节点）
    assert len(chat_manager.calls) == 1, (
        f"expected 1 call, got {len(chat_manager.calls)}"
    )