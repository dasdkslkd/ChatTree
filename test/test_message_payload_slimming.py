import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from backend.api.routes.messages import slim_message_for_ui, slim_tool_result_for_ui


def test_slim_tool_result_for_ui_removes_heavy_fields_but_keeps_envelope():
    tool_message = {
        "id": "tool-1",
        "role": "tool",
        "name": "shell",
        "tool_call_id": "call-1",
        "content": '{"tool_result_id":"result-1","total_chars":42000,"truncated":true,"preview":"short"}',
        "raw_content": "x" * 42000,
        "model_visible_content": "model-only",
        "tool_result_id": "result-1",
    }

    slimmed = slim_tool_result_for_ui(tool_message)

    assert slimmed == {
        "id": "tool-1",
        "role": "tool",
        "name": "shell",
        "tool_call_id": "call-1",
        "content": '{"tool_result_id":"result-1","total_chars":42000,"truncated":true,"preview":"short"}',
        "tool_result_id": "result-1",
    }


def test_slim_message_for_ui_slims_nested_tool_payloads():
    assistant = {
        "id": "assistant-1",
        "role": "assistant",
        "content": "done",
        "tool_results": [
            {
                "id": "tool-1",
                "role": "tool",
                "content": "preview",
                "raw_content": "large raw",
                "model_visible_content": "large model",
                "tool_result_id": "result-1",
            }
        ],
        "tool_interactions": [
            {
                "assistant": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{"id": "call-1", "function": {"name": "shell", "arguments": "{}"}}],
                },
                "tools": [
                    {
                        "role": "tool",
                        "tool_call_id": "call-1",
                        "content": "preview",
                        "raw_content": "large raw",
                        "model_visible_content": "large model",
                        "tool_result_id": "result-1",
                    }
                ],
            }
        ],
    }

    slimmed = slim_message_for_ui(assistant)

    assert "raw_content" not in slimmed["tool_results"][0]
    assert "model_visible_content" not in slimmed["tool_results"][0]
    assert "raw_content" not in slimmed["tool_interactions"][0]["tools"][0]
    assert "model_visible_content" not in slimmed["tool_interactions"][0]["tools"][0]
    assert slimmed["tool_interactions"][0]["tools"][0]["tool_result_id"] == "result-1"


if __name__ == "__main__":
    test_slim_tool_result_for_ui_removes_heavy_fields_but_keeps_envelope()
    test_slim_message_for_ui_slims_nested_tool_payloads()
    print("message payload slimming tests passed")
