import asyncio
import json
import sys
from types import SimpleNamespace

sys.path.insert(0, ".")

from backend.core.tools.agent_tools import (
    AGENT_TOOL_NAMES,
    SpawnAgentTool,
    StartSubagentTool,
    StartWorkflowTool,
    WaitAgentTool,
    register_agent_management_tools,
)
from backend.core.chat.chat_manager import ChatManager
from backend.core.chat.node import NodeManager
from backend.core.chat.conversation import Conversation
from backend.core.config.types import Message, Role
from backend.core.model.providers.openai_compatible import OpenAICompatibleProvider
from backend.core.tools import mcp_server as mcp_server_module
from backend.core.tools.exposure import ToolExposureContext
from backend.core.tools.mcp_server import McpServerManager
from backend.core.tools.tool_manager import ToolManager
from backend.core.tools.tool_filter import ToolFilter
from backend.core.tools.security.capabilities import capabilities_for_tool
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

    assert "web" in names
    assert "web_search" not in names
    assert "fetch_url" not in names
    assert "list_available_tools" not in names


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

    assert "glob" in names
    assert "read" in names
    assert "grep" in names
    assert "edit" in names
    assert "shell" in names
    assert "write" not in names
    assert "patch" not in names


def test_tool_manager_full_exposure_keeps_raw_write_internal():
    manager = ToolManager({
        "tools": {
            "enabled": True,
            "builtin": {
                "exposure": "full",
                "code": {
                    "enabled": True,
                    "workspace_roots": ["D:\\Workspace\\ChatTree\\tmp"],
                }
            },
        }
    })

    names = [tool["function"]["name"] for tool in manager.get_openai_tools()]

    assert "edit" in names
    assert "write" not in names


def test_tool_manager_exposure_context_supports_explicit_allow_and_deny():
    manager = ToolManager({
        "tools": {
            "enabled": True,
            "builtin": {
                "web_search": {"enabled": False},
                "code": {"enabled": True},
            },
        }
    })

    assert manager.get_openai_tools(exposure_context=ToolExposureContext(allowed_tools=())) == []
    tools_names = {
        tool["function"]["name"]
        for tool in manager.get_openai_tools(
            exposure_context=ToolExposureContext(allowed_tools=("tools",))
        )
    }
    assert tools_names == {"tools"}
    denied_names = {
        tool["function"]["name"]
        for tool in manager.get_openai_tools(
            exposure_context=ToolExposureContext(disallowed_tools=("shell",))
        )
    }
    assert "shell" not in denied_names
    assert {"glob", "grep", "read", "edit"} <= denied_names


def test_tool_manager_explicit_context_uses_configured_default_profile():
    manager = ToolManager({
        "tools": {
            "enabled": True,
            "builtin": {
                "exposure": "minimal",
                "web_search": {"enabled": False},
                "code": {"enabled": True},
            },
        }
    })

    names = {
        tool["function"]["name"]
        for tool in manager.get_openai_tools(exposure_context=ToolExposureContext())
    }

    assert names == {"glob", "grep", "read"}


def test_full_builtin_exposure_does_not_make_mcp_visible_by_default():
    manager = ToolManager({
        "tools": {
            "enabled": True,
            "builtin": {"exposure": "full", "enabled": False},
            "mcp": {"enabled": True, "servers": {}},
        }
    })
    manager._connection_manager = SimpleNamespace(
        list_all_tools=lambda: [
            {
                "server": "demo",
                "tool": {"name": "lookup"},
                "callable_name": "demo__lookup",
                "openai_schema": {"type": "function", "function": {"name": "demo__lookup", "parameters": {}}},
            }
        ]
    )

    assert "demo__lookup" not in manager.list_tools()
    assert "demo__lookup" not in [
        tool["function"]["name"] for tool in manager.get_openai_tools()
    ]
    assert "demo__lookup" in manager.list_tools(exposure_context=ToolExposureContext(include_mcp=True))


def test_legacy_agent_management_tools_are_internal_when_registered():
    manager = ToolManager({
        "tools": {
            "enabled": True,
            "builtin": {"enabled": False},
        }
    })
    manager.register(StartSubagentTool(subagent_executor=object()))
    manager.register(StartWorkflowTool(workflow_manager=object()))

    names = [tool["function"]["name"] for tool in manager.get_openai_tools()]

    assert "start_subagent" not in names
    assert "start_workflow" not in names


def test_agent_runtime_tools_expose_single_canonical_agent_tool():
    class FakeAgentRuntime:
        pass

    manager = ToolManager({
        "tools": {
            "enabled": True,
            "builtin": {"enabled": False},
        }
    })
    register_agent_management_tools(manager, agent_runtime=FakeAgentRuntime())

    names = {tool["function"]["name"] for tool in manager.get_openai_tools()}

    assert names == {"agent"}
    assert "start_subagent" not in names
    assert "start_workflow" not in names
    assert not (AGENT_TOOL_NAMES - {"agent"}).intersection(names)


def test_spawn_agent_schema_names_delivery_and_forbids_simulation():
    tool = SpawnAgentTool(agent_runtime=object())
    schema = tool.to_openai_tool()["function"]
    properties = schema["parameters"]["properties"]

    assert "Do not simulate a subagent" in schema["description"]
    assert properties["context_mode"]["enum"] == ["fresh", "fork"]
    assert properties["delivery"]["enum"] == ["auto", "notify", "silent"]
    for role in ["explorer", "planner", "implementer", "reviewer", "verifier", "workflow-worker"]:
        assert role in properties["agent_name"]["description"]


def test_wait_agent_schema_uses_run_ids():
    tool = WaitAgentTool(agent_runtime=object())
    function = tool.to_openai_tool()["function"]
    schema = function["parameters"]

    assert "run_ids" in schema["required"]
    assert schema["properties"]["run_ids"]["type"] == "array"
    assert "does not mean the run failed" in function["description"]
    assert "wait duration" in schema["properties"]["timeout_seconds"]["description"]


def test_start_subagent_schema_names_common_agent_roles_and_forbids_simulation():
    tool = StartSubagentTool(subagent_executor=object())
    schema = tool.to_openai_tool()["function"]
    agent_description = schema["parameters"]["properties"]["agent_name"]["description"]

    assert "Do not simulate a subagent" in schema["description"]
    for role in ["explorer", "planner", "implementer", "reviewer", "verifier"]:
        assert role in agent_description


def test_start_workflow_schema_forbids_simulating_workflow():
    tool = StartWorkflowTool(workflow_manager=object())
    schema = tool.to_openai_tool()["function"]

    assert "When the user explicitly asks for a workflow" in schema["description"]
    assert "instead of simulating" in schema["description"]
    assert "export default async function workflow(ctx)" in schema["description"]


def test_tool_manager_preserves_start_workflow_script_argument():
    class FakeWorkflowManager:
        def __init__(self):
            self.kwargs = None

        async def start(self, **kwargs):
            self.kwargs = kwargs
            return {"run_id": "workflow-1", "kind": "workflow", "status": "running"}

    fake = FakeWorkflowManager()
    manager = ToolManager({
        "tools": {
            "enabled": True,
            "builtin": {"enabled": False},
        }
    })
    manager.register(StartWorkflowTool(workflow_manager=fake))

    result = asyncio.run(manager.execute_tool(
        "start_workflow",
        {"script": "export default async function workflow(ctx) { await ctx.log('hello'); }"},
        runtime_context={
            "conversation_id": "conversation-1",
            "node_id": "node-1",
            "run_id": "run-parent",
        },
    ))

    payload = json.loads(result)
    assert payload["run_id"] == "workflow-1"
    assert fake.kwargs["script"] == "export default async function workflow(ctx) { await ctx.log('hello'); }"
    assert fake.kwargs["created_by_run_id"] == "run-parent"
    assert fake.kwargs["cancellation_parent_run_id"] is None


def test_start_subagent_tool_requires_runtime_context():
    tool = StartSubagentTool(subagent_executor=object())

    result = asyncio.run(tool.execute(task="inspect environment"))

    payload = json.loads(result)
    assert payload["error"]["type"] == "missing_runtime_context"


def test_start_subagent_tool_starts_background_run_with_inherited_context():
    class FakeSubagentExecutor:
        def __init__(self):
            self.kwargs = None

        async def start(self, **kwargs):
            self.kwargs = kwargs
            return {"run_id": "subagent-1", "kind": "subagent", "status": "running"}

    executor = FakeSubagentExecutor()
    tool = StartSubagentTool(subagent_executor=executor)

    result = asyncio.run(tool.execute(
        task="检查本机环境",
        agent_name="explorer",
        _runtime_context={
            "conversation_id": "conversation-1",
            "node_id": "node-1",
            "permission_mode": "ask_always",
            "workspace": {"cwd": "D:\\Workspace\\ChatTree"},
        },
    ))

    payload = json.loads(result)
    assert payload["run_id"] == "subagent-1"
    assert executor.kwargs["conversation_id"] == "conversation-1"
    assert executor.kwargs["parent_node_id"] == "node-1"
    assert executor.kwargs["agent_name"] == "explorer"
    assert executor.kwargs["input_data"] == "检查本机环境"
    assert executor.kwargs["permission_mode"] == "ask_always"
    assert executor.kwargs["workspace"] == {"cwd": "D:\\Workspace\\ChatTree"}
    assert executor.kwargs["delegated_task"] == "检查本机环境"


def test_tool_manager_builtin_enabled_false_hides_builtin_runtime_tools():
    manager = ToolManager({
        "tools": {
            "enabled": True,
            "builtin": {
                "enabled": False,
            },
        }
    })

    names = [tool["function"]["name"] for tool in manager.get_openai_tools()]

    assert "web_search" not in names
    assert "read" not in names
    assert "list_available_tools" not in names


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

        def capabilities_for(self, name, workspace=None):
            return capabilities_for_tool(name)

        async def execute_tool(self, name, arguments, workspace=None, runtime_context=None):
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
        assert event_types == ["tool_call_start", "tool_call", "tool_call"]
        tool_call_chunks = [chunk for chunk in chunks if chunk.get("event_type") == "tool_call"]
        assert tool_call_chunks[0]["tool_calls"][0]["function"]["name"] == "web_search"
        assert tool_call_chunks[0]["tool_calls"][0]["function"]["arguments"] == ""
        assert tool_call_chunks[-1]["tool_calls"][0]["function"]["arguments"] == "{\"query\":\"ChatTree\"}"
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
        assert event_types == ["tool_call_start", "tool_call", "tool_call"]
        tool_call_chunks = [chunk for chunk in chunks if chunk.get("event_type") == "tool_call"]
        assert tool_call_chunks[0]["tool_calls"][0]["function"]["name"] == "web_search"
        assert tool_call_chunks[0]["tool_calls"][0]["function"]["arguments"] == ""
        assert tool_call_chunks[-1]["tool_calls"][0]["function"]["arguments"] == "{\"query\":\"ChatTree\"}"

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


def test_responses_input_places_function_output_immediately_after_call():
    provider = OpenAICompatibleProvider({"api_key": "test", "api_format": "responses"})

    _, response_input = provider._convert_messages_to_responses_input([
        Message({"role": "user", "content": "use tool"}),
        Message({
            "role": "assistant",
            "content": "I will call a tool.",
            "tool_calls": [{
                "id": "call-1",
                "type": "function",
                "function": {"name": "spawn_agent", "arguments": "{}"},
            }],
        }),
        Message({
            "role": Role.TOOL,
            "tool_call_id": "call-1",
            "name": "spawn_agent",
            "content": "{\"ok\": true}",
        }),
    ])

    types = [item["type"] for item in response_input]
    assert types == ["message", "message", "function_call", "function_call_output"]
    assert response_input[2]["call_id"] == "call-1"
    assert response_input[3]["call_id"] == "call-1"


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

    class FakeResponse:
        text = "<html><body>" + ("x" * 100) + "</body></html>"
        headers = {"content-type": "text/html"}
        status_code = 200
        url = "https://example.test/page"

        def raise_for_status(self):
            return None

    class FakeClient:
        async def get(self, url):
            return FakeResponse()

    tool._http_client = FakeClient()
    result = asyncio.run(tool._fetch_with_http("https://example.test/page"))
    assert result["content"].startswith("x" * 80)
    assert "[Content truncated" in result["content"]
