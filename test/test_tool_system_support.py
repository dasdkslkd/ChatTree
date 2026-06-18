import asyncio
import json

from backend.core.chat.chat_manager import ChatManager
from backend.core.chat.node import NodeManager
from backend.core.chat.conversation import Conversation
from backend.core.config.types import Message, Role
from backend.core.model.providers.openai_compatible import OpenAICompatibleProvider
from backend.core.tools.tool_filter import ToolFilter
from backend.core.tools.web_search import WebSearchTool
from backend.core.tools.web_search import FetchUrlTool


def test_tool_filter_allows_aliases_and_denies_disabled():
    tool_filter = ToolFilter(enabled=["server.tool"], disabled=["blocked_tool"])

    assert tool_filter.is_allowed("server__tool", aliases=["server.tool"])
    assert not tool_filter.is_allowed("other_tool")
    assert not tool_filter.is_allowed("blocked_tool")


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
        async def execute_tool(self, name, arguments):
            return json.dumps({"name": name, "arguments": arguments}, ensure_ascii=False)

    manager = ChatManager.__new__(ChatManager)
    manager.tool_manager = FakeToolManager()

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
