import asyncio
import json

from backend.core.chat.chat_manager import ChatManager
from backend.core.chat.node import NodeManager
from backend.core.chat.conversation import Conversation
from backend.core.config.types import Message, Role
from backend.core.model.providers.openai_compatible import OpenAICompatibleProvider
from backend.core.tools import mcp_server as mcp_server_module
from backend.core.tools.mcp_server import McpServerManager
from backend.core.tools.tool_manager import ToolManager
from backend.core.tools.tool_filter import ToolFilter
from backend.core.tools.web_search import WebSearchTool
from backend.core.tools.web_search import FetchUrlTool


def test_tool_filter_allows_aliases_and_denies_disabled():
    tool_filter = ToolFilter(enabled=["server.tool"], disabled=["blocked_tool"])

    assert tool_filter.is_allowed("server__tool", aliases=["server.tool"])
    assert not tool_filter.is_allowed("other_tool")
    assert not tool_filter.is_allowed("blocked_tool")


def test_tool_manager_keeps_builtin_inventory_when_mcp_servers_configured():
    manager = ToolManager({
        "tools": {
            "enabled": True,
            "mcp": {
                "enabled": True,
                "servers": {
                    "demo": {
                        "enabled": True,
                        "transport": "stdio",
                        "command": "missing-mcp-server",
                    }
                },
            },
        }
    })

    names = [tool["function"]["name"] for tool in manager.get_openai_tools()]

    assert "web_search" in names
    assert "fetch_url" in names
    assert "list_available_tools" in names


def test_tool_manager_registers_builtin_code_tools():
    manager = ToolManager({
        "tools": {
            "enabled": True,
            "builtin": {
                "code": {
                    "enabled": True,
                    "workspace_roots": ["D:\\Workspace\\ChatTree\\tmp"],
                }
            },
        }
    })

    names = [tool["function"]["name"] for tool in manager.get_openai_tools()]

    assert "list_files" in names
    assert "read_file" in names
    assert "run_command" in names
    assert "write_file" in names
    assert "apply_patch" in names


def test_stdio_command_splits_line_arguments():
    server = McpServerManager("demo", {
        "transport": "stdio",
        "command": "npx",
        "args": ["-y mcp-searxng"],
    })

    command = server._build_stdio_command()

    assert command[-2:] == ["-y", "mcp-searxng"]


def test_stdio_process_prefers_popen_on_windows(monkeypatch):
    class DummyProcess:
        stdin = None
        stdout = None
        stderr = None

        def poll(self):
            return None

        def terminate(self):
            return None

        def kill(self):
            return None

        def wait(self):
            return 0

    async def fail_if_asyncio_subprocess_is_used(*args, **kwargs):
        raise AssertionError("asyncio subprocess should not be used")

    popen_calls = []

    def fake_popen(command, **kwargs):
        popen_calls.append((command, kwargs))
        return DummyProcess()

    monkeypatch.setattr(mcp_server_module.os, "name", "nt")
    monkeypatch.setattr(
        mcp_server_module.asyncio,
        "create_subprocess_exec",
        fail_if_asyncio_subprocess_is_used,
    )
    monkeypatch.setattr(mcp_server_module.subprocess, "Popen", fake_popen)

    server = McpServerManager("demo", {
        "transport": "stdio",
        "command": "demo-mcp",
    })
    process = asyncio.run(server._start_stdio_process(["demo-mcp"], None, {"A": "B"}))

    assert isinstance(process, mcp_server_module._PopenProcess)
    assert popen_calls == [(["demo-mcp"], {
        "stdin": mcp_server_module.subprocess.PIPE,
        "stdout": mcp_server_module.subprocess.PIPE,
        "stderr": mcp_server_module.subprocess.PIPE,
        "cwd": None,
        "env": {"A": "B"},
    })]


def test_prepare_messages_reconstructs_tool_interaction_order():
    conversation = Conversation(title="tools")
    conversation.initialize_with_system_message("system prompt")

    user_msg = Message({
        "id": "user-1",
        "role": Role.USER,
        "content": "查一下天气",
        "timestamp": 1,
    })
    node = NodeManager.create_node(user_msg, parent_id=conversation.current_node_id, model_id="model")

    assistant_tool = {
        "role": "assistant",
        "content": "",
        "tool_calls": [{
            "id": "call-1",
            "type": "function",
            "function": {"name": "web_search", "arguments": "{\"query\":\"上海天气\"}"},
        }],
    }
    tool_msg = Message({
        "id": "tool-1",
        "role": Role.TOOL,
        "content": "{\"result\":\"晴\"}",
        "name": "web_search",
        "tool_call_id": "call-1",
        "timestamp": 2,
    })
    assistant_final = Message({
        "id": "assistant-1",
        "role": Role.ASSISTANT,
        "content": "上海天气晴。",
        "timestamp": 3,
        "tool_calls": assistant_tool["tool_calls"],
        "tool_results": [tool_msg],
        "tool_interactions": [{"assistant": assistant_tool, "tools": [tool_msg]}],
    })
    node["assistant_message"] = assistant_final
    node["tool_messages"] = [tool_msg]
    conversation.add_node(node, parent_id=conversation.current_node_id)

    manager = ChatManager.__new__(ChatManager)
    messages = manager._prepare_messages_for_api_with_conversation(conversation)

    assert [msg["role"] for msg in messages] == ["system", "user", "assistant", "tool", "assistant"]
    assert messages[2]["tool_calls"][0]["id"] == "call-1"
    assert messages[3]["tool_call_id"] == "call-1"
    assert messages[4]["content"] == "上海天气晴。"
    assert "tool_calls" not in messages[4]


def test_execute_tool_calls_returns_tool_messages():
    class FakeToolManager:
        def __init__(self):
            self.calls = []

        async def execute_tool(self, name, arguments):
            self.calls.append((name, arguments))
            return json.dumps({"name": name, "arguments": arguments}, ensure_ascii=False)

    manager = ChatManager.__new__(ChatManager)
    tool_manager = FakeToolManager()
    manager.tool_manager = tool_manager

    tool_calls = [{
        "id": "call-1",
        "type": "function",
        "function": {"name": "web_search", "arguments": "{\"query\":\"ChatTree\"}"},
    }]
    results = asyncio.run(manager._execute_tool_calls(tool_calls, "node-1"))

    assert len(results) == 1
    assert results[0]["role"] == Role.TOOL
    assert results[0]["name"] == "web_search"
    assert results[0]["tool_call_id"] == "call-1"
    assert tool_manager.calls == [("web_search", {"query": "ChatTree"})]
    assert json.loads(results[0]["content"])["arguments"] == {"query": "ChatTree"}


def test_openai_tool_call_delta_aggregation():
    provider = OpenAICompatibleProvider({"api_key": "test"})
    accumulator = {}

    provider._merge_openai_tool_call_delta(
        accumulator,
        {"index": 0, "id": "call-1", "type": "function", "function": {"name": "web_search"}},
    )
    provider._merge_openai_tool_call_delta(
        accumulator,
        {"index": 0, "function": {"arguments": "{\"query\""}},
    )
    provider._merge_openai_tool_call_delta(
        accumulator,
        {"index": 0, "function": {"arguments": ":\"ChatTree\"}"}},
    )

    calls = provider._finalize_openai_tool_calls(accumulator)
    assert calls == [{
        "id": "call-1",
        "type": "function",
        "function": {"name": "web_search", "arguments": "{\"query\":\"ChatTree\"}"},
    }]


def test_openai_stream_emits_tool_call_start_before_final_tool_call():
    async def run_case():
        provider = OpenAICompatibleProvider({"api_key": "test"})

        async def fake_iter_sse_events(_path, _body):
            yield {
                "choices": [{
                    "delta": {"content": "准备调用工具。"},
                    "finish_reason": None,
                }],
            }
            yield {
                "choices": [{
                    "delta": {
                        "tool_calls": [{
                            "index": 0,
                            "id": "call-1",
                            "type": "function",
                            "function": {"name": "web_search"},
                        }],
                    },
                    "finish_reason": None,
                }],
            }
            yield {
                "choices": [{
                    "delta": {
                        "tool_calls": [{
                            "index": 0,
                            "function": {"arguments": "{\"query\":\"ChatTree\"}"},
                        }],
                    },
                    "finish_reason": "tool_calls",
                }],
            }

        provider._iter_sse_events = fake_iter_sse_events
        chunks = []
        async for chunk in provider.generate_response_stream(
            model="gpt-test",
            messages=[],
            tools=[{"type": "function", "function": {"name": "web_search", "parameters": {}}}],
            tool_choice="auto",
        ):
            chunks.append(dict(chunk))

        event_types = [chunk.get("event_type") for chunk in chunks if chunk.get("event_type")]
        assert event_types == ["tool_call_start", "tool_call"]
        tool_call_index = next(i for i, chunk in enumerate(chunks) if chunk.get("event_type") == "tool_call")
        start_index = next(i for i, chunk in enumerate(chunks) if chunk.get("event_type") == "tool_call_start")
        assert start_index < tool_call_index

    asyncio.run(run_case())


def test_openai_responses_stream_emits_tool_call_start_before_final_tool_call():
    async def run_case():
        provider = OpenAICompatibleProvider({"api_key": "test"})

        async def fake_iter_sse_events(_path, _body):
            yield {
                "type": "response.output_text.delta",
                "delta": "准备调用工具。",
            }
            yield {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {
                    "type": "function_call",
                    "call_id": "call-1",
                    "name": "web_search",
                },
            }
            yield {
                "type": "response.function_call_arguments.delta",
                "output_index": 0,
                "delta": "{\"query\":\"ChatTree\"}",
            }

        provider._iter_sse_events = fake_iter_sse_events
        chunks = []
        async for chunk in provider._stream_responses_api(
            model="gpt-test",
            messages=[],
            stream_controller=None,
            max_tokens=None,
            temperature=0.7,
            tools=[{"type": "function", "name": "web_search", "parameters": {}}],
            tool_choice="auto",
            reasoning_effort=None,
            extra_kwargs={},
        ):
            chunks.append(dict(chunk))

        event_types = [chunk.get("event_type") for chunk in chunks if chunk.get("event_type")]
        assert event_types == ["tool_call_start", "tool_call"]

    asyncio.run(run_case())


def test_searxng_html_result_parser():
    tool = WebSearchTool({"searxng_url": "http://localhost:8888"})
    html = """
    <article class="result result-default category-general">
      <a href="https://example.com/page" class="url_header" rel="noreferrer"></a>
      <h3><a href="https://example.com/page">Example <span class="highlight">Title</span></a></h3>
      <p class="content">A <span>short</span> snippet.</p>
      <div class="engines"><span>duckduckgo</span><span>google</span></div>
    </article>
    """

    results = tool._parse_searxng_html(html, 5)

    assert results == [{
        "title": "Example Title",
        "url": "https://example.com/page",
        "snippet": "A short snippet.",
        "engine": "duckduckgo,google",
    }]


def test_enable_thinking_only_for_known_compatible_models():
    provider = OpenAICompatibleProvider({"api_key": "test"})

    deepseek_body = provider._build_chat_request_kwargs(
        model="deepseek-v4-flash-ascend",
        messages=[],
        stream=True,
        max_tokens=None,
        temperature=None,
        top_p=None,
        extra_kwargs={},
    )
    assert "extra_body" not in deepseek_body
    assert not provider._supports_enable_thinking("deepseek-v4-flash-ascend")
    assert provider._supports_enable_thinking("qwen3.6-chat")


def test_fetch_url_extracts_html_without_crawl4ai():
    tool = FetchUrlTool({"max_content_length": 80})
    content = """
    <html>
      <head><title>Demo Page</title><style>.hidden{}</style></head>
      <body><script>bad()</script><article><h1>Hello</h1><p>Useful text.</p></article></body>
    </html>
    """

    assert tool._extract_title(content) == "Demo Page"
    assert tool._html_to_text(content) == "Hello Useful text."
    assert tool._truncate("x" * 100).startswith("x" * 80)
