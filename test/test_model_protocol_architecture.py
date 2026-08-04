import asyncio
import json
from copy import deepcopy
from unittest.mock import MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.dependencies import get_model_manager
from backend.api.routes import openai_proxy
from backend.core.chat.canonical_reader import model_state_items_by_node
from backend.core.chat.chat_manager import ChatManager
from backend.core.config.config import DEFAULT_MODEL_TRANSPORT, cfg
from backend.core.config.types import (
    ModelProtocol,
    ModelRoute,
    StreamChunk,
    StreamStatus,
)
from backend.core.model.model_manager import ModelManager
from backend.core.model.model_metadata import initialize_model_metadata
from backend.core.model.providers import model_fetch
from backend.core.model.providers.anthropic_provider import AnthropicProvider
from backend.core.model.providers.gemini_provider import GeminiProvider
from backend.core.model.providers.openai_compatible import OpenAICompatibleProvider
from backend.core.persistence.database import SQLitePersistence
from backend.core.persistence.repository import ChatRepository
from backend.core.storage.chat_storage import ChatStorage
from backend.core.storage.prompt_storage import PromptStorage


def route(
    protocol: str,
    *,
    route_id: str = "gateway:model:route",
    profile: dict | None = None,
) -> ModelRoute:
    endpoints = {
        ModelProtocol.OPENAI_CHAT_COMPLETIONS.value: "/chat/completions",
        ModelProtocol.OPENAI_RESPONSES.value: "/responses",
        ModelProtocol.ANTHROPIC_MESSAGES.value: "/v1/messages",
        ModelProtocol.GEMINI_GENERATE_CONTENT.value: "/models/{model}:generateContent",
    }
    return ModelRoute(
        route_id=route_id,
        provider_id="gateway",
        model_id="model",
        protocol=protocol,
        endpoint=endpoints[protocol],
        capabilities={},
        reasoning_profile=profile or {
            "name": "test",
            "carrier": "none",
            "history_policy": "drop",
            "strict": False,
            "controls": {},
        },
    )


def test_one_connection_routes_each_model_from_server_home_metadata(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("CHATTREE_HOME", str(tmp_path))
    initialize_model_metadata(tmp_path)
    monkeypatch.setattr(cfg, "data", {
        "model_transport": DEFAULT_MODEL_TRANSPORT,
        "provider": {
            "gateway": {
                "name": "Gateway",
                "base_url": "http://127.0.0.1:9000/v1",
                "api_key": "test",
                "enabled": True,
                "models": [
                    "gpt-5.6",
                    "kimi-k3",
                    "claude-sonnet-4-6",
                    "unknown-model",
                ],
            },
        },
    })
    manager = ModelManager()

    responses = manager.get_model("gateway", "gpt-5.6", True)
    kimi = manager.get_model("gateway", "kimi-k3", True)
    claude = manager.get_model("gateway", "claude-sonnet-4-6", True)
    other_chat = manager.get_model("gateway", "unknown-model", True)

    assert isinstance(responses, OpenAICompatibleProvider)
    assert responses.route["protocol"] == "openai_responses"
    assert isinstance(kimi, OpenAICompatibleProvider)
    assert kimi.route["protocol"] == "openai_chat_completions"
    assert isinstance(claude, AnthropicProvider)
    assert claude.route["protocol"] == "anthropic_messages"
    assert kimi is not other_chat
    assert kimi.route["route_id"] != other_chat.route["route_id"]


def test_unknown_model_uses_plain_chat_fallback(monkeypatch, tmp_path):
    monkeypatch.setenv("CHATTREE_HOME", str(tmp_path))
    initialize_model_metadata(tmp_path)
    monkeypatch.setattr(cfg, "data", {
        "model_transport": DEFAULT_MODEL_TRANSPORT,
        "provider": {
            "gateway": {
                "enabled": True,
                "models": ["gpt-5.6", "unknown-model"],
            },
        },
    })
    manager = ModelManager()
    metadata = manager.get_provider_metadata("gateway")
    model = manager.get_model("gateway", "unknown-model")

    assert set(metadata) == {"gpt-5.6", "unknown-model"}
    assert isinstance(model, OpenAICompatibleProvider)
    assert model.route["protocol"] == "openai_chat_completions"
    assert model.route["reasoning_profile"]["carrier"] == "none"


def test_provider_requests_send_default_or_configured_user_agent():
    providers = [
        OpenAICompatibleProvider(
            {"api_key": "test"},
            route(ModelProtocol.OPENAI_CHAT_COMPLETIONS.value),
        ),
        AnthropicProvider(
            {"api_key": "test"},
            route(ModelProtocol.ANTHROPIC_MESSAGES.value),
        ),
        GeminiProvider(
            {"api_key": "test"},
            route(ModelProtocol.GEMINI_GENERATE_CONTENT.value),
        ),
    ]

    for provider in providers:
        assert provider._headers()["User-Agent"] == "ChatTree"
        provider.config["custom_user_agent"] = "custom-client/1.0"
        assert provider._headers()["User-Agent"] == "custom-client/1.0"


def test_model_discovery_sends_default_or_configured_user_agent(monkeypatch):
    response = MagicMock()
    response.__enter__.return_value.read.return_value = b'{"data":[{"id":"model"}]}'
    urlopen = MagicMock(return_value=response)
    monkeypatch.setattr(model_fetch.urllib.request, "urlopen", urlopen)

    assert model_fetch.fetch_models("https://example.test/v1", "test") == [
        {"id": "model", "owned_by": None},
    ]
    assert urlopen.call_args.args[0].get_header("User-agent") == "ChatTree"

    assert model_fetch.fetch_models(
        "https://example.test/v1",
        "test",
        custom_user_agent="custom-client/1.0",
    )
    assert urlopen.call_args.args[0].get_header("User-agent") == "custom-client/1.0"


def test_native_continuation_payloads_keep_private_state_and_order():
    responses_route = route(
        "openai_responses",
        route_id="gateway:gpt:responses",
        profile={
            "name": "openai_responses_native",
            "carrier": "responses_items",
            "history_policy": "provider_state",
            "strict": True,
            "controls": {},
        },
    )
    responses = OpenAICompatibleProvider({"api_key": "test"}, responses_route)
    reasoning_item = {
        "type": "reasoning",
        "id": "rs_1",
        "encrypted_content": "ciphertext",
        "summary": [],
    }
    function_item = {
        "type": "function_call",
        "call_id": "call_1",
        "name": "lookup",
        "arguments": "{\"q\":\"x\"}",
    }
    _, responses_input = responses._convert_messages_to_responses_input([
        {
            "role": "assistant",
            "content": "",
            "model_state_items": [
                {
                    "route_id": responses_route["route_id"],
                    "index": 0,
                    "native_payload": reasoning_item,
                },
            ],
            "tool_calls": [{
                "id": "call_1",
                "type": "function",
                "function": {"name": "lookup", "arguments": "{\"q\":\"x\"}"},
            }],
        },
        {
            "role": "tool",
            "content": "result",
            "tool_call_id": "call_1",
        },
    ])
    assert responses_input == [
        reasoning_item,
        function_item,
        {
            "type": "function_call_output",
            "call_id": "call_1",
            "output": "result",
        },
    ]

    anthropic_route = route(
        "anthropic_messages",
        route_id="gateway:claude:messages",
    )
    anthropic = AnthropicProvider({"api_key": "test"}, anthropic_route)
    blocks = [
        {"type": "thinking", "thinking": "private", "signature": "signed"},
        {
            "type": "tool_use",
            "id": "toolu_1",
            "name": "lookup",
            "input": {"q": "x"},
        },
    ]
    _, anthropic_messages = anthropic._convert_messages([
        {
            "role": "assistant",
            "content": "",
            "model_state_items": [{
                "route_id": anthropic_route["route_id"],
                "index": 0,
                "kind": "assistant_message",
                "native_payload": {
                    "role": "assistant",
                    "layout": [
                        {"state": blocks[0]},
                        {"tool_call_id": "toolu_1"},
                    ],
                },
            }],
            "tool_calls": [{
                "id": "toolu_1",
                "type": "function",
                "function": {"name": "lookup", "arguments": "{\"q\":\"x\"}"},
            }],
        },
        {
            "role": "tool",
            "content": "result",
            "tool_call_id": "toolu_1",
        },
    ])
    assert anthropic_messages[0]["content"] == blocks
    assert anthropic_messages[1]["content"][0]["tool_use_id"] == "toolu_1"

    gemini_route = route(
        "gemini_generate_content",
        route_id="gateway:gemini:generate",
    )
    gemini = GeminiProvider({"api_key": "test"}, gemini_route)
    signed_part = {
        "functionCall": {"name": "lookup", "args": {"q": "x"}},
        "thoughtSignature": "signed-thought",
    }
    _, gemini_messages = gemini._convert_messages([
        {
            "role": "assistant",
            "content": "",
            "model_state_items": [{
                "route_id": gemini_route["route_id"],
                "index": 0,
                "kind": "assistant_message",
                "native_payload": {
                    "role": "model",
                    "layout": [{
                        "tool_call_id": "call_1",
                        "thoughtSignature": "signed-thought",
                    }],
                },
            }],
            "tool_calls": [{
                "id": "call_1",
                "type": "function",
                "function": {"name": "lookup", "arguments": "{\"q\":\"x\"}"},
            }],
        },
        {
            "role": "tool",
            "name": "lookup",
            "content": "{\"value\":1}",
        },
    ])
    assert gemini_messages[0]["parts"][0] == signed_part
    assert gemini_messages[1]["parts"][0]["functionResponse"]["name"] == "lookup"


def test_chat_reasoning_profiles_control_history_and_native_fields():
    assistant = {
        "role": "assistant",
        "content": "answer",
        "reasoning_content": "private",
        "tool_calls": [{
            "id": "call_1",
            "type": "function",
            "function": {"name": "lookup", "arguments": "{}"},
        }],
    }

    kimi_route = route(
        "openai_chat_completions",
        route_id="gateway:kimi:chat",
        profile={
            "name": "kimi_preserved_all",
            "carrier": "chat_reasoning_content",
            "history_policy": "all_assistant_messages",
            "strict": True,
            "controls": {"effort_field": "reasoning_effort"},
        },
    )
    kimi = OpenAICompatibleProvider({"api_key": "test"}, kimi_route)
    messages = [
        {
            "role": "assistant",
            "content": "earlier",
            "reasoning": "earlier-private",
            "model_route_id": kimi_route["route_id"],
        },
        {
            "role": "assistant",
            "content": "answer",
            "reasoning": "private",
            "tool_calls": assistant["tool_calls"],
            "model_route_id": kimi_route["route_id"],
        },
    ]
    assert kimi._convert_messages(messages)[0]["reasoning_content"] == "earlier-private"
    assert kimi._convert_messages(messages)[1] == assistant

    tool_reasoning_route = route(
        "openai_chat_completions",
        route_id="gateway:deepseek:chat",
        profile={
            "name": "deepseek_tool_reasoning",
            "carrier": "chat_reasoning_content",
            "history_policy": "tool_assistant_messages",
            "strict": True,
            "controls": {},
        },
    )
    tool_reasoning = OpenAICompatibleProvider(
        {"api_key": "test"},
        tool_reasoning_route,
    )
    tool_reasoning_messages = deepcopy(messages)
    for message in tool_reasoning_messages:
        message["model_route_id"] = tool_reasoning_route["route_id"]
    converted_tool_reasoning = tool_reasoning._convert_messages(
        tool_reasoning_messages
    )
    assert "reasoning_content" not in converted_tool_reasoning[0]
    assert converted_tool_reasoning[1]["reasoning_content"] == "private"

    # 带 tool_calls 但 reasoning 缺失/路由不一致时，必须回传空 reasoning_content，
    # 否则 DeepSeek 思考模式上游返回 400。
    degraded_messages = deepcopy(messages[1:])
    degraded_messages[0]["model_route_id"] = tool_reasoning_route["route_id"]
    degraded_messages[0]["reasoning"] = None
    converted_degraded = tool_reasoning._convert_messages(degraded_messages)
    assert converted_degraded[0]["reasoning_content"] == ""
    degraded_messages[0]["model_route_id"] = "gateway:other:chat"
    degraded_messages[0]["reasoning"] = "private"
    converted_degraded = tool_reasoning._convert_messages(degraded_messages)
    assert converted_degraded[0]["reasoning_content"] == ""

    qwen_route = route(
        "openai_chat_completions",
        route_id="gateway:qwen:chat",
        profile={
            "name": "qwen_chat_display_only",
            "carrier": "chat_reasoning_content",
            "history_policy": "drop",
            "strict": False,
            "controls": {"thinking_style": "qwen"},
        },
    )
    qwen = OpenAICompatibleProvider({"api_key": "test"}, qwen_route)
    qwen_messages = deepcopy(messages[1:])
    qwen_messages[0]["model_route_id"] = qwen_route["route_id"]
    assert "reasoning_content" not in qwen._convert_messages(qwen_messages)[0]

    zai_route = route(
        "openai_chat_completions",
        route_id="gateway:glm:chat",
        profile={
            "name": "zai_preserved",
            "carrier": "chat_reasoning_content",
            "history_policy": "all_assistant_messages",
            "strict": True,
            "controls": {"thinking_style": "zai"},
        },
    )
    zai = OpenAICompatibleProvider({"api_key": "test"}, zai_route)
    enabled = zai._build_chat_request_kwargs(
        model="glm",
        messages=[],
        stream=True,
        max_tokens=None,
        temperature=None,
        top_p=None,
        extra_kwargs={},
        thinking_enabled=True,
    )
    disabled = zai._build_chat_request_kwargs(
        model="glm",
        messages=[],
        stream=True,
        max_tokens=None,
        temperature=None,
        top_p=None,
        extra_kwargs={},
        thinking_enabled=False,
    )
    assert enabled["thinking"] == {
        "type": "enabled",
        "clear_thinking": False,
    }
    assert disabled["thinking"] == {
        "type": "disabled",
        "clear_thinking": True,
    }


def test_minimal_model_state_survives_sqlite_restart(tmp_path):
    persistence = SQLitePersistence(tmp_path / "sqlite")
    persistence.initialize()
    repository = ChatRepository(persistence)
    conversation_id = repository.create_conversation("native state")
    node_id = "node-1"
    with persistence.connect() as connection:
        connection.execute(
            """
            INSERT INTO nodes (
              id, conversation_id, parent_id, child_order, depth, status,
              task_context_mode, created_at, updated_at
            )
            VALUES (?, ?, NULL, 0, 0, 'complete', 'attached',
                    strftime('%s', 'now'), strftime('%s', 'now'))
            """,
            (node_id, conversation_id),
        )
        connection.execute(
            """
            UPDATE conversations
            SET root_node_id = ?, current_node_id = ?
            WHERE id = ?
            """,
            (node_id, node_id, conversation_id),
        )
    payload = {
        "type": "reasoning",
        "encrypted_content": "opaque" * 5_000,
        "unknown_provider_field": {"nested": True},
    }
    assistant_message_id = repository.add_message(
        conversation_id,
        node_id,
        role="assistant",
        content="",
        subtype="assistant_round",
        hidden=True,
        transcript_only=True,
        model_route_id="gateway:gpt:responses",
        model_round_index=0,
    )
    repository.persist_model_state_items(
        conversation_id,
        assistant_message_id,
        output_items=[{
            "index": 0,
            "kind": "reasoning",
            "state_payload": payload,
        }],
    )

    reopened = ChatRepository(SQLitePersistence(tmp_path / "sqlite"))
    reopened.persistence.initialize()
    items = model_state_items_by_node(
        reopened,
        conversation_id,
        [node_id],
    )[node_id]
    assert items[0]["native_payload"] == payload
    assert items[0]["route_id"] == "gateway:gpt:responses"


def test_restarted_chat_rebuilds_protocol_view_from_native_items(tmp_path):
    chat_route = route(
        "openai_chat_completions",
        route_id="gateway:kimi:fixed-route",
        profile={
            "name": "kimi_preserved_all",
            "carrier": "chat_reasoning_content",
            "history_policy": "all_assistant_messages",
            "strict": True,
            "controls": {},
        },
    )

    class Provider:
        def __init__(self):
            self.route = chat_route
            self.calls = []

        async def generate_response_stream(
            self,
            model,
            messages,
            stream_controller=None,
            **_kwargs,
        ):
            self.calls.append(deepcopy(messages))
            call_index = len(self.calls)
            yield StreamChunk(
                status=StreamStatus.CONTENT,
                reasoning=f"private-{call_index}",
            )
            yield StreamChunk(
                status=StreamStatus.CONTENT,
                content=f"answer-{call_index}",
                tokens_used=1,
            )
            yield StreamChunk(
                status=StreamStatus.COMPLETE,
                content=None,
                tokens_used=1,
                usage_info={
                    "input_tokens": 1,
                    "output_tokens": 1,
                    "total_tokens": 2,
                    "source": "test",
                    "raw": {},
                },
            )

    class Manager:
        model_list = {"gateway": ["kimi"]}

        def __init__(self, provider):
            self.provider = provider

        def get_route(self, provider_id, model_id):
            return chat_route

        def get_model(self, provider_id, model_id, is_async=False):
            return self.provider

        def get_model_metadata(self, provider_id, model_id):
            return {
                **chat_route["capabilities"],
                "reasoning_profile": chat_route["reasoning_profile"],
            }

    async def drain(stream):
        async for _ in stream:
            pass

    persistence = SQLitePersistence(tmp_path / "sqlite")
    persistence.initialize()
    provider = Provider()
    storage = ChatStorage(str(tmp_path / "conversations"))
    prompts = PromptStorage(str(tmp_path / "prompts"))
    first_manager = ChatManager(
        Manager(provider),
        storage,
        prompts,
        chat_repository=ChatRepository(persistence),
    )
    conversation = first_manager.create_conversation("restart")
    asyncio.run(drain(first_manager.send_message_stream(
        conversation.metadata["id"],
        "first",
        model_id="kimi",
        provider_id="gateway",
        parent_node_id=conversation.current_node_id,
    )))

    reopened = SQLitePersistence(tmp_path / "sqlite")
    reopened.initialize()
    second_manager = ChatManager(
        Manager(provider),
        storage,
        prompts,
        chat_repository=ChatRepository(reopened),
    )
    restored = second_manager.get_conversation(conversation.metadata["id"])
    asyncio.run(drain(second_manager.send_message_stream(
        conversation.metadata["id"],
        "second",
        model_id="kimi",
        provider_id="gateway",
        parent_node_id=restored.current_node_id,
    )))

    with reopened.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM model_state_items"
        ).fetchone()[0] == 0
        assistant_rows = connection.execute(
            """
            SELECT subtype, content_inline, preview
            FROM messages
            WHERE conversation_id = ? AND role = 'assistant'
            ORDER BY created_at, rowid
            """,
            (conversation.metadata["id"],),
        ).fetchall()
    assert any(
        row["subtype"] == "assistant_process_reasoning"
        and row["content_inline"] == "private-1"
        for row in assistant_rows
    )
    assert all(row["preview"] == "" for row in assistant_rows)

    prior_assistant = next(
        message
        for message in provider.calls[1]
        if message.get("role") == "assistant"
        and message.get("model_route_id")
    )
    adapter = OpenAICompatibleProvider({"api_key": "test"}, chat_route)
    replayed = adapter._convert_messages([prior_assistant])
    assert replayed == [{
        "role": "assistant",
        "content": "answer-1",
        "reasoning_content": "private-1",
    }]


def test_proxy_forwards_native_protocol_without_semantic_conversion(monkeypatch):
    sent = {}

    class Adapter:
        route = {"endpoint": "/responses"}

        def _url(self, path):
            return f"https://upstream.example{path}"

        def _headers(self, stream=False):
            return {
                "authorization": "Bearer upstream",
                "content-type": "application/json",
                "accept": "text/event-stream" if stream else "application/json",
            }

    class Manager:
        model_list = {"gateway": ["gpt-edge"]}

        def get_route(self, provider_id, model_id):
            return {
                "route_id": "gateway:gpt:responses",
                "provider_id": provider_id,
                "model_id": model_id,
                "protocol": "openai_responses",
                "endpoint": "/responses",
            }

        def get_model(self, provider_id, model_id, is_async=False):
            return Adapter()

    class Response:
        status_code = 207
        headers = {
            "content-type": "text/event-stream",
            "x-request-id": "req_upstream",
        }

        async def aiter_raw(self):
            yield b"data: first\n\n"
            yield b"data: second\n\n"

        async def aclose(self):
            sent["response_closed"] = True

    class Client:
        def __init__(self, *args, **kwargs):
            sent["client_kwargs"] = kwargs

        def build_request(self, method, url, headers, content):
            sent.update({
                "method": method,
                "url": url,
                "headers": headers,
                "content": content,
            })
            return object()

        async def send(self, request, stream=False):
            sent["stream"] = stream
            return Response()

        async def aclose(self):
            sent["client_closed"] = True

    monkeypatch.setattr(openai_proxy.httpx, "AsyncClient", Client)
    app = FastAPI()
    app.include_router(openai_proxy.router)
    app.dependency_overrides[get_model_manager] = lambda: Manager()
    body = {
        "model": "gateway/gpt-edge",
        "input": [{"role": "user", "content": "hello"}],
        "include": ["reasoning.encrypted_content"],
        "store": False,
        "stream": True,
        "unknown": {"keep": True},
    }

    response = TestClient(app).post(
        "/proxy/v1/responses?compat=exact",
        json=body,
        headers={"OpenAI-Beta": "responses=v1"},
    )

    assert response.status_code == 207
    assert response.content == b"data: first\n\ndata: second\n\n"
    assert response.headers["x-request-id"] == "req_upstream"
    assert sent["url"] == "https://upstream.example/responses?compat=exact"
    assert sent["headers"]["openai-beta"] == "responses=v1"
    assert sent["headers"]["authorization"] == "Bearer upstream"
    forwarded = json.loads(sent["content"])
    assert forwarded == {**body, "model": "gpt-edge"}
    assert sent["stream"] is True
    assert sent["response_closed"] is True
    assert sent["client_closed"] is True
