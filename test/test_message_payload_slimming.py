import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from backend.api.routes.messages import slim_tool_result_for_ui


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


if __name__ == "__main__":
    test_slim_tool_result_for_ui_removes_heavy_fields_but_keeps_envelope()
    print("message payload slimming tests passed")
