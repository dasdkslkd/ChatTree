import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from backend.core.chat.chat_manager import ChatManager
from backend.core.chat.canonical_reader import messages_by_node
from backend.core.config.types import StreamChunk, StreamController, StreamStatus
from backend.core.storage.chat_storage import ChatStorage
from backend.core.storage.prompt_storage import PromptStorage
from model_route_support import fake_model_route


class RaisingProvider:
    async def generate_response_stream(
        self,
        model,
        messages,
        stream_controller: StreamController = None,
        **kwargs,
    ):
        yield StreamChunk(
            status=StreamStatus.START,
            content=None,
            node_id=stream_controller.node_id,
            conversation_id=stream_controller.conversation_id,
            error=None,
            tokens_used=0,
        )
        raise RuntimeError("upstream quota exceeded")


class FailThenSucceedProvider:
    """首轮抛错（模拟上游 400），次轮正常返回。"""

    def __init__(self):
        self.calls = 0

    async def generate_response_stream(
        self,
        model,
        messages,
        stream_controller: StreamController = None,
        **kwargs,
    ):
        self.calls += 1
        yield StreamChunk(
            status=StreamStatus.START,
            content=None,
            node_id=stream_controller.node_id,
            conversation_id=stream_controller.conversation_id,
            error=None,
            tokens_used=0,
        )
        if self.calls == 1:
            raise RuntimeError("HTTP 400: reasoning_content missing")
        yield StreamChunk(
            status=StreamStatus.CONTENT,
            content="retry answer",
            node_id=stream_controller.node_id,
            conversation_id=stream_controller.conversation_id,
            error=None,
            tokens_used=1,
        )
        yield StreamChunk(
            status=StreamStatus.COMPLETE,
            content=None,
            node_id=stream_controller.node_id,
            conversation_id=stream_controller.conversation_id,
            error=None,
            tokens_used=1,
            metadata={"finish_reason": "stop"},
        )


class FakeModelManager:
    model_list = {"fake-provider": ["fake-model"]}

    def __init__(self, provider=None):
        self._provider = provider or RaisingProvider()

    def get_route(self, provider, model):
        return fake_model_route(provider, model)

    def get_model(self, provider, model, is_async=False):
        return self._provider

    def get_model_metadata(self, provider, model):
        return self.get_route(provider, model)["capabilities"]


async def _provider_exception_streams_and_persists_real_error(tmp_path):
    chat_manager = ChatManager(
        FakeModelManager(),
        ChatStorage(storage_dir=str(tmp_path / "conversations")),
        PromptStorage(storage_dir=str(tmp_path / "prompts")),
    )
    conversation = chat_manager.create_conversation("provider error")

    chunks = [
        chunk
        async for chunk in chat_manager.send_message_stream(
            conversation.metadata["id"],
            "hello",
            model_id="fake-model",
            parent_node_id=conversation.current_node_id,
        )
    ]

    error_chunk = next(chunk for chunk in chunks if chunk.get("status") == StreamStatus.ERROR)
    assert error_chunk["error"] == "upstream quota exceeded"
    assert error_chunk["node_id"]

    messages = messages_by_node(
        chat_manager.chat_repository,
        conversation.metadata["id"],
        [error_chunk["node_id"]],
    ).get(error_chunk["node_id"], [])
    assert not [
        message for message in messages
        if message.get("role") == "assistant" and message.get("subtype") == "assistant_answer"
    ]


def test_provider_exception_streams_and_persists_real_error(tmp_path):
    asyncio.run(_provider_exception_streams_and_persists_real_error(tmp_path))


class ErrorWithUsageProvider:
    """模拟流中途异常中断：终结 ERROR 块携带上游已报告的用量。"""

    async def generate_response_stream(
        self,
        model,
        messages,
        stream_controller: StreamController = None,
        **kwargs,
    ):
        yield StreamChunk(
            status=StreamStatus.START,
            content=None,
            node_id=stream_controller.node_id,
            conversation_id=stream_controller.conversation_id,
            error=None,
            tokens_used=0,
        )
        yield StreamChunk(
            status=StreamStatus.CONTENT,
            content="partial",
            node_id=stream_controller.node_id,
            conversation_id=stream_controller.conversation_id,
            error=None,
            tokens_used=5,
        )
        yield StreamChunk(
            status=StreamStatus.ERROR,
            content=None,
            node_id=stream_controller.node_id,
            conversation_id=stream_controller.conversation_id,
            error="connection lost",
            tokens_used=120,
            usage_info={
                "input_tokens": 100,
                "output_tokens": 20,
                "total_tokens": 120,
                "cache_read_input_tokens": 40,
                "source": "api",
                "raw": {},
            },
        )


async def _abnormal_termination_persists_usage(tmp_path):
    """流式输出非正常结束时，节点用量不得丢失为 0。"""
    chat_manager = ChatManager(
        FakeModelManager(ErrorWithUsageProvider()),
        ChatStorage(storage_dir=str(tmp_path / "conversations")),
        PromptStorage(storage_dir=str(tmp_path / "prompts")),
    )
    conversation = chat_manager.create_conversation("usage on error")

    node_id = None
    async for chunk in chat_manager.send_message_stream(
        conversation.metadata["id"],
        "hello",
        model_id="fake-model",
        parent_node_id=conversation.current_node_id,
    ):
        if chunk.get("node_id"):
            node_id = chunk["node_id"]
    assert node_id

    node = chat_manager.get_conversation(conversation.metadata["id"]).nodes[node_id]
    turn_usage = node["usage"]["turn_usage"]
    assert turn_usage["total_tokens"] == 120
    assert turn_usage.get("cache_read_input_tokens") == 40
    assert node["usage"]["active_context_usage"]["total_tokens"] == 120


def test_abnormal_termination_persists_usage(tmp_path):
    asyncio.run(_abnormal_termination_persists_usage(tmp_path))


async def _continue_after_error_keeps_user_message_before_retry_answer(tmp_path):
    """出错后续发“继续”：用户消息必须位于错误节点与重试回答之间。"""
    from backend.core.transcript import TranscriptAssembler

    chat_manager = ChatManager(
        FakeModelManager(FailThenSucceedProvider()),
        ChatStorage(storage_dir=str(tmp_path / "conversations")),
        PromptStorage(storage_dir=str(tmp_path / "prompts")),
    )
    conversation = chat_manager.create_conversation("continue after error")

    errored_node = None
    async for chunk in chat_manager.send_message_stream(
        conversation.metadata["id"],
        "first question",
        model_id="fake-model",
        parent_node_id=conversation.current_node_id,
    ):
        if chunk.get("node_id"):
            errored_node = chunk["node_id"]
    assert errored_node

    async for _chunk in chat_manager.send_message_stream(
        conversation.metadata["id"],
        "继续",
        model_id="fake-model",
        parent_node_id=errored_node,
    ):
        pass

    assembler = TranscriptAssembler(chat_manager.chat_repository.persistence)
    items = assembler.snapshot(conversation.metadata["id"])["items"]
    sequence = [
        (item["type"], item.get("content") or "")
        for item in items
        if item["type"] in {"user_message", "assistant_answer"}
    ]
    assert sequence == [
        ("user_message", "first question"),
        ("user_message", "继续"),
        ("assistant_answer", "retry answer"),
    ]


def test_continue_after_error_keeps_user_message_before_retry_answer(tmp_path):
    asyncio.run(_continue_after_error_keeps_user_message_before_retry_answer(tmp_path))
