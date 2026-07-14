import json

from backend.core.tools.perf_attrs import summarize_tool_arguments, summarize_tool_result


def test_summarize_tool_arguments_records_safe_grep_shape():
    pattern = "def.*execute_tool|async def.*execute_tool"
    attrs = summarize_tool_arguments(
        "grep",
        {
            "pattern": pattern,
            "path": "backend/core/chat/chat_manager.py",
            "output": "content",
            "context": 5,
            "limit": 50,
        },
    )

    assert attrs["tool_name"] == "grep"
    assert attrs["arg_path"] == "backend/core/chat/chat_manager.py"
    assert attrs["arg_output"] == "content"
    assert attrs["arg_context"] == 5
    assert attrs["pattern_preview"] == pattern
    assert attrs["pattern_chars"] == len(pattern)
    assert len(attrs["pattern_sha256"]) == 16


def test_summarize_tool_result_records_grep_result_shape():
    content = json.dumps(
        {
            "engine": "rg",
            "output": "content",
            "matches": [
                {"path": "a.py", "line": 1, "text": "before", "type": "context"},
                {"path": "a.py", "line": 2, "text": "needle", "type": "match"},
            ],
            "count": 2,
            "searched_files": 1,
            "skipped_non_utf8": [],
            "truncated": False,
        },
        ensure_ascii=False,
    )

    attrs = summarize_tool_result(content)

    assert attrs["result_chars"] == len(content)
    assert attrs["result_engine"] == "rg"
    assert attrs["result_output"] == "content"
    assert attrs["result_entry_count"] == 2
    assert attrs["result_match_count"] == 1
    assert attrs["result_count"] == 2
    assert attrs["result_searched_files"] == 1
    assert attrs["result_truncated"] is False


def test_summarize_tool_result_records_glob_pagination_shape():
    content = json.dumps(
        {
            "engine": "rg",
            "sort": "discovery",
            "files": ["a.py", "b.py"],
            "count": 2,
            "total": None,
            "total_known": False,
            "observed_count": 3,
            "scanned_entries": 3,
            "truncated": True,
            "next_offset": 2,
        },
        ensure_ascii=False,
    )

    attrs = summarize_tool_result(content)

    assert attrs["result_engine"] == "rg"
    assert attrs["result_sort"] == "discovery"
    assert attrs["result_total"] is None
    assert attrs["result_total_known"] is False
    assert attrs["result_observed_count"] == 3
    assert attrs["result_scanned_entries"] == 3
    assert attrs["result_file_count"] == 2
    assert attrs["result_next_offset"] == 2
