from __future__ import annotations

import asyncio
import json

from backend.api.routes import messages as messages_route
from backend.api.routes import runs as runs_route
from backend.core.persistence.database import SQLitePersistence
from backend.core.persistence.repository import ChatRepository
from backend.core.persistence.run_repository import SQLiteRunRepository
from backend.core.config.types import StreamStatus
from backend.core.runs import RunKind, RunManager, RunStatus
from backend.core.slash import SlashCommandDispatcher
from backend.core.transcript import TranscriptAssembler


TRANSCRIPT_ITEM_TYPES = {
    "user_message",
    "assistant_process",
    "assistant_answer",
    "plan_question",
    "plan_approval",
    "tool_approval",
    "task_notification",
    "compact",
    "run_status",
}


def _repo(tmp_path):
    persistence = SQLitePersistence(tmp_path)
    persistence.initialize()
    repository = ChatRepository(persistence)
    return persistence, repository


def _conversation(repository: ChatRepository):
    conversation_id = repository.create_conversation(title="transcript")
    root_id = repository.create_node(conversation_id, parent_id=None, child_order=0)
    return conversation_id, root_id


def _snapshot(persistence: SQLitePersistence, conversation_id: str, node_id: str):
    return TranscriptAssembler(persistence).snapshot(conversation_id, node_id)


def test_snapshot_assembles_user_message_and_assistant_answer_from_canonical_rows(tmp_path):
    persistence, repository = _repo(tmp_path)
    conversation_id, node_id = _conversation(repository)
    user_id = repository.add_message(conversation_id, node_id, "user", "你好")
    assistant_id = repository.add_message(conversation_id, node_id, "assistant", "可以。")

    payload = _snapshot(persistence, conversation_id, node_id)

    assert [(item["type"], item["id"], item["content"]) for item in payload["items"]] == [
        ("user_message", f"message:{user_id}", "你好"),
        ("assistant_answer", f"message:{assistant_id}", "可以。"),
    ]


def test_snapshot_assembles_assistant_process_tool_blocks(tmp_path):
    persistence, repository = _repo(tmp_path)
    conversation_id, node_id = _conversation(repository)
    run_id = SQLiteRunRepository(persistence).create_run(
        conversation_id,
        kind="chat",
        target_node_id=node_id,
        summary="tools",
    )
    repository.add_tool_call(
        conversation_id,
        node_id,
        tool_call_id="call-shell",
        name="shell_command",
        arguments={"command": "pwd"},
        run_id=run_id,
    )
    repository.add_tool_result(
        conversation_id,
        node_id,
        tool_result_id="result-shell",
        tool_call_id="call-shell",
        output="D:\\Workspace\\ChatTree",
        run_id=run_id,
    )

    process = next(item for item in _snapshot(persistence, conversation_id, node_id)["items"] if item["type"] == "assistant_process")

    assert process["run_id"] == run_id
    assert process["id"] == f"process:{node_id}:0"
    assert process["blocks"] == [
        {
            "type": "tool_call",
            "id": "tool-call:call-shell",
            "tool_call_id": "call-shell",
            "tool_name": "shell_command",
            "args_preview": "{\"command\": \"pwd\"}",
            "result_preview": "D:\\Workspace\\ChatTree",
            "status": "complete",
        }
    ]


def test_snapshot_restores_process_reasoning_and_content_from_canonical_messages(tmp_path):
    persistence, repository = _repo(tmp_path)
    conversation_id, node_id = _conversation(repository)
    repository.add_message(
        conversation_id,
        node_id,
        "assistant",
        "先想清楚。",
        subtype="assistant_process_reasoning",
        hidden=True,
        transcript_only=True,
        message_id="reasoning-row",
    )
    repository.add_message(
        conversation_id,
        node_id,
        "assistant",
        "我先检查文件。",
        subtype="assistant_process_content",
        hidden=True,
        transcript_only=True,
        message_id="content-row",
    )
    repository.add_tool_call(
        conversation_id,
        node_id,
        tool_call_id="call-files",
        name="list_files",
        arguments={"path": "."},
    )
    repository.add_message(conversation_id, node_id, "assistant", "检查完成。")

    items = TranscriptAssembler(persistence).snapshot(conversation_id, node_id)["items"]

    assert [item["type"] for item in items] == ["assistant_process", "assistant_answer"]
    assert items[0]["blocks"][:2] == [
        {
            "type": "reasoning",
            "id": "reasoning:reasoning-row",
            "content": "先想清楚。",
            "streaming": False,
        },
        {
            "type": "content",
            "id": "content:content-row",
            "content": "我先检查文件。",
            "streaming": False,
        },
    ]
    assert items[0]["blocks"][2]["tool_call_id"] == "call-files"
    assert items[1]["content"] == "检查完成。"


def test_snapshot_outputs_persisted_approved_tool_as_tool_approval_item(tmp_path):
    persistence, repository = _repo(tmp_path)
    conversation_id, node_id = _conversation(repository)
    repository.add_tool_call(
        conversation_id,
        node_id,
        tool_call_id="call-shell",
        name="shell_command",
        arguments={"command": "pwd"},
        status="approved",
    )
    repository.add_tool_result(
        conversation_id,
        node_id,
        tool_result_id="result-shell",
        tool_call_id="call-shell",
        output="D:\\Workspace\\ChatTree",
    )

    item = _snapshot(persistence, conversation_id, node_id)["items"][0]

    assert item["type"] == "tool_approval"
    assert item["id"] == "tool-approval:call-shell"
    assert item["status"] == "approved"
    assert item["result_preview"] == "D:\\Workspace\\ChatTree"


def test_snapshot_assembles_plan_question_and_answer_with_same_id(tmp_path):
    persistence, repository = _repo(tmp_path)
    conversation_id, node_id = _conversation(repository)
    repository.add_tool_call(
        conversation_id,
        node_id,
        tool_call_id="call-question",
        name="ask_user_question",
        arguments={"question": "默认显示什么？", "options": [{"label": "默认显示"}]},
    )
    repository.add_tool_result(
        conversation_id,
        node_id,
        tool_result_id="result-question",
        tool_call_id="call-question",
        output=json.dumps({"answer": "默认显示"}, ensure_ascii=False),
    )

    question = _snapshot(persistence, conversation_id, node_id)["items"][0]

    assert question["type"] == "plan_question"
    assert question["id"] == "plan-question:call-question"
    assert question["status"] == "answered"
    assert question["question"] == "默认显示什么？"
    assert question["answer"] == "默认显示"


def test_snapshot_reads_plan_question_answer_from_tool_result_blob(tmp_path):
    persistence, repository = _repo(tmp_path)
    conversation_id, node_id = _conversation(repository)
    repository.add_tool_call(
        conversation_id,
        node_id,
        tool_call_id="call-question",
        name="ask_user_question",
        arguments={"question": "请详细确认？"},
    )
    long_answer = "A" * 5000
    repository.add_tool_result(
        conversation_id,
        node_id,
        tool_result_id="result-question",
        tool_call_id="call-question",
        output=json.dumps({"answer": long_answer, "plan_id": "plan-long"}, ensure_ascii=False),
    )

    question = _snapshot(persistence, conversation_id, node_id)["items"][0]

    assert question["type"] == "plan_question"
    assert question["status"] == "answered"
    assert question["plan_id"] == "plan-long"
    assert question["answer"] == long_answer


def test_repository_replaces_tool_result_by_tool_call_id(tmp_path):
    persistence, repository = _repo(tmp_path)
    conversation_id, node_id = _conversation(repository)
    repository.add_tool_call(
        conversation_id,
        node_id,
        tool_call_id="call-exit",
        name="exit_plan_mode",
        arguments={"plan": "执行计划"},
    )

    first_id = repository.add_tool_result(
        conversation_id,
        node_id,
        tool_result_id="result-old",
        tool_call_id="call-exit",
        output=json.dumps({"status": "approved"}, ensure_ascii=False),
    )
    second_id = repository.add_tool_result(
        conversation_id,
        node_id,
        tool_result_id="result-new",
        tool_call_id="call-exit",
        output=json.dumps({"status": "rejected", "feedback": "不要执行"}, ensure_ascii=False),
    )

    with persistence.connect() as conn:
        rows = conn.execute(
            """
            SELECT id, output_preview
            FROM tool_results
            WHERE conversation_id = ? AND tool_call_id = ?
            """,
            (conversation_id, "call-exit"),
        ).fetchall()

    approval = _snapshot(persistence, conversation_id, node_id)["items"][0]
    assert first_id == second_id == "result-old"
    assert len(rows) == 1
    assert json.loads(rows[0]["output_preview"])["status"] == "rejected"
    assert approval["status"] == "rejected"
    assert approval["feedback"] == "不要执行"


def test_snapshot_keeps_plan_cards_at_tool_call_position(tmp_path):
    persistence, repository = _repo(tmp_path)
    conversation_id, node_id = _conversation(repository)
    repository.add_tool_call(
        conversation_id,
        node_id,
        tool_call_id="call-before",
        name="shell_command",
        arguments={"command": "pwd"},
        call_index=0,
    )
    repository.add_tool_call(
        conversation_id,
        node_id,
        tool_call_id="call-question",
        name="ask_user_question",
        arguments={"question": "继续吗？"},
        call_index=1,
    )
    repository.add_tool_result(
        conversation_id,
        node_id,
        tool_result_id="result-question",
        tool_call_id="call-question",
        output=json.dumps({"plan_id": "plan-1", "status": "awaiting_question", "message": "ok"}, ensure_ascii=False),
    )
    repository.add_tool_call(
        conversation_id,
        node_id,
        tool_call_id="call-after",
        name="shell_command",
        arguments={"command": "whoami"},
        call_index=2,
    )

    items = _snapshot(persistence, conversation_id, node_id)["items"]

    assert [item["type"] for item in items] == [
        "assistant_process",
        "plan_question",
        "assistant_process",
    ]
    assert items[0]["blocks"][0]["tool_call_id"] == "call-before"
    assert items[1]["id"] == "plan-question:call-question"
    assert items[2]["blocks"][0]["tool_call_id"] == "call-after"


def test_snapshot_renders_failed_plan_tools_as_process_tools(tmp_path):
    persistence, repository = _repo(tmp_path)
    conversation_id, node_id = _conversation(repository)
    repository.add_tool_call(
        conversation_id,
        node_id,
        tool_call_id="call-question-error",
        name="ask_user_question",
        arguments={"question": "继续吗？"},
        call_index=0,
    )
    repository.add_tool_result(
        conversation_id,
        node_id,
        tool_result_id="result-question-error",
        tool_call_id="call-question-error",
        output=json.dumps({"error": {"type": "invalid_arguments", "message": "active plan session is required"}}, ensure_ascii=False),
    )
    repository.add_tool_call(
        conversation_id,
        node_id,
        tool_call_id="call-exit-error",
        name="exit_plan_mode",
        arguments={"plan": "计划"},
        call_index=1,
    )
    repository.add_tool_result(
        conversation_id,
        node_id,
        tool_result_id="result-exit-error",
        tool_call_id="call-exit-error",
        output=json.dumps({"error": {"type": "invalid_arguments", "message": "active plan session is required"}}, ensure_ascii=False),
    )

    items = _snapshot(persistence, conversation_id, node_id)["items"]

    assert [item["type"] for item in items] == ["assistant_process"]
    assert [block["tool_call_id"] for block in items[0]["blocks"]] == ["call-question-error", "call-exit-error"]


def test_snapshot_uses_canonical_timeline_for_process_segments_around_plan_card(tmp_path):
    persistence, repository = _repo(tmp_path)
    conversation_id, node_id = _conversation(repository)
    run_repository = SQLiteRunRepository(persistence)
    first_run = run_repository.create_run(
        conversation_id,
        kind="chat",
        target_node_id=node_id,
        summary="before",
    )
    second_run = run_repository.create_run(
        conversation_id,
        kind="chat",
        target_node_id=node_id,
        summary="after",
    )
    repository.add_message(
        conversation_id,
        node_id,
        "assistant",
        "先确认计划。",
        subtype="assistant_process_content",
        hidden=True,
        transcript_only=True,
        metadata={"run_id": first_run},
        message_id="process-before",
    )
    repository.add_tool_call(
        conversation_id,
        node_id,
        tool_call_id="call-exit",
        name="exit_plan_mode",
        arguments={"plan": "执行计划"},
        run_id=first_run,
    )
    repository.add_tool_result(
        conversation_id,
        node_id,
        tool_result_id="result-exit",
        tool_call_id="call-exit",
        output=json.dumps({"plan_id": "plan-1", "status": "awaiting_approval", "message": "ok"}, ensure_ascii=False),
    )
    repository.add_message(
        conversation_id,
        node_id,
        "assistant",
        "批准后继续。",
        subtype="assistant_process_content",
        hidden=True,
        transcript_only=True,
        metadata={"run_id": second_run},
        message_id="process-after",
    )
    with persistence.connect() as conn:
        conn.execute("UPDATE runs SET created_at = 10 WHERE id = ?", (first_run,))
        conn.execute("UPDATE runs SET created_at = 20 WHERE id = ?", (second_run,))
        conn.execute("UPDATE messages SET created_at = 100 WHERE id = 'process-before'")
        conn.execute("UPDATE tool_calls SET created_at = 100, call_index = 0 WHERE id = 'call-exit'")
        conn.execute("UPDATE messages SET created_at = 100 WHERE id = 'process-after'")

    items = _snapshot(persistence, conversation_id, node_id)["items"]

    assert [item["type"] for item in items] == [
        "assistant_process",
        "plan_approval",
        "assistant_process",
    ]
    assert items[0]["run_id"] == first_run
    assert items[0]["blocks"][0]["content"] == "先确认计划。"
    assert items[1]["id"] == "plan-approval:call-exit"
    assert items[2]["run_id"] == second_run
    assert items[2]["blocks"][0]["content"] == "批准后继续。"


def test_live_patch_keeps_canonical_tool_card_when_stream_entries_exist(tmp_path):
    persistence, repository = _repo(tmp_path)
    conversation_id, node_id = _conversation(repository)
    run_id = SQLiteRunRepository(persistence).create_run(
        conversation_id,
        kind="chat",
        target_node_id=node_id,
        summary="live",
    )
    repository.add_tool_call(
        conversation_id,
        node_id,
        tool_call_id="call-exit",
        name="exit_plan_mode",
        arguments={"plan": "plan"},
        run_id=run_id,
    )
    repository.add_tool_result(
        conversation_id,
        node_id,
        tool_result_id="result-exit",
        tool_call_id="call-exit",
        output=json.dumps({"plan_id": "plan-1", "status": "awaiting_approval", "message": "ok"}, ensure_ascii=False),
    )

    patch = TranscriptAssembler(persistence).patch_session(run_id).feed({
        "type": "message_delta",
        "conversation_id": conversation_id,
        "node_id": node_id,
        "assistant_message_id": "assistant-live",
        "content": "running text",
    })

    items = [operation["item"] for operation in patch["operations"] if operation["op"] == "upsert"]
    assert [item["type"] for item in items] == ["plan_approval", "assistant_process"]
    assert items[0]["id"] == "plan-approval:call-exit"
    assert items[1]["blocks"][-1]["type"] == "content"
    assert items[1]["blocks"][-1]["content"] == "running text"


def test_snapshot_splits_same_node_process_items_by_run_id(tmp_path):
    persistence, repository = _repo(tmp_path)
    conversation_id, node_id = _conversation(repository)
    run_repository = SQLiteRunRepository(persistence)
    first_run = run_repository.create_run(
        conversation_id,
        kind="chat",
        target_node_id=node_id,
        summary="first",
    )
    second_run = run_repository.create_run(
        conversation_id,
        kind="chat",
        target_node_id=node_id,
        summary="second",
    )
    run_repository.finish_run(first_run, "completed", None)
    run_repository.finish_run(second_run, "failed", "boom")
    repository.add_tool_call(
        conversation_id,
        node_id,
        tool_call_id="call-first",
        name="shell_command",
        arguments={"command": "one"},
        run_id=first_run,
        call_index=0,
    )
    repository.add_tool_call(
        conversation_id,
        node_id,
        tool_call_id="call-second",
        name="shell_command",
        arguments={"command": "two"},
        run_id=second_run,
        call_index=1,
    )
    with persistence.connect() as conn:
        conn.execute("UPDATE tool_calls SET created_at = 10 WHERE id = 'call-first'")
        conn.execute("UPDATE tool_calls SET created_at = 20 WHERE id = 'call-second'")

    process_items = [
        item for item in _snapshot(persistence, conversation_id, node_id)["items"]
        if item["type"] == "assistant_process"
    ]

    assert [item["run_id"] for item in process_items] == [first_run, second_run]
    assert [item["status"] for item in process_items] == ["complete", "error"]


def test_live_stream_aggregates_previous_run_blocks_into_single_process_item(tmp_path):
    persistence, repository = _repo(tmp_path)
    conversation_id, node_id = _conversation(repository)
    run_repository = SQLiteRunRepository(persistence)
    first_run = run_repository.create_run(
        conversation_id,
        kind="chat",
        target_node_id=node_id,
        summary="round-1",
    )
    second_run = run_repository.create_run(
        conversation_id,
        kind="chat",
        target_node_id=node_id,
        summary="round-2",
    )
    run_repository.finish_run(first_run, "completed", None)
    repository.add_message(
        conversation_id,
        node_id,
        "assistant",
        "先思考。",
        subtype="assistant_process_reasoning",
        hidden=True,
        transcript_only=True,
        metadata={"run_id": first_run},
        message_id="reasoning-1",
    )
    repository.add_message(
        conversation_id,
        node_id,
        "assistant",
        "读文件。",
        subtype="assistant_process_content",
        hidden=True,
        transcript_only=True,
        metadata={"run_id": first_run},
        message_id="content-1",
    )
    repository.add_tool_call(
        conversation_id,
        node_id,
        tool_call_id="call-read",
        name="read",
        arguments={"path": "a"},
        run_id=first_run,
        call_index=0,
    )
    repository.add_tool_result(
        conversation_id,
        node_id,
        tool_result_id="result-read",
        tool_call_id="call-read",
        output="content",
        run_id=first_run,
    )
    with persistence.connect() as conn:
        conn.execute("UPDATE runs SET created_at = 10 WHERE id = ?", (first_run,))
        conn.execute("UPDATE runs SET created_at = 20 WHERE id = ?", (second_run,))
        conn.execute("UPDATE messages SET created_at = 100 WHERE id = 'reasoning-1'")
        conn.execute("UPDATE messages SET created_at = 100 WHERE id = 'content-1'")
        conn.execute("UPDATE tool_calls SET created_at = 100 WHERE id = 'call-read'")

    session = TranscriptAssembler(persistence).patch_session(second_run)
    first_patch = session.feed({
        "status": "start",
        "conversation_id": conversation_id,
        "node_id": node_id,
        "assistant_message_id": "assistant-2",
    })
    second_patch = session.feed({
        "event_type": "tool_call",
        "conversation_id": conversation_id,
        "node_id": node_id,
        "tool_call": {"id": "call-write", "name": "write", "arguments": "{}"},
    })

    first_process = next(
        operation["item"]
        for operation in first_patch["operations"]
        if operation["op"] == "upsert" and operation["item"]["type"] == "assistant_process"
    )
    assert first_process["run_id"] == first_run
    assert [block["type"] for block in first_process["blocks"]] == ["reasoning", "content", "tool_call"]
    assert first_process["status"] == "complete"

    second_process = next(
        operation["item"]
        for operation in second_patch["operations"]
        if operation["op"] == "upsert" and operation["item"]["type"] == "assistant_process"
    )
    assert second_process["run_id"] == second_run
    assert [block["type"] for block in second_process["blocks"]] == ["tool_call"]
    assert second_process["blocks"][0]["tool_call_id"] == "call-write"
    assert second_process["status"] == "running"


def test_snapshot_restores_assistant_answer_status_from_corresponding_run(tmp_path):
    persistence, repository = _repo(tmp_path)
    conversation_id, node_id = _conversation(repository)
    run_repository = SQLiteRunRepository(persistence)
    run_id = run_repository.create_run(
        conversation_id,
        kind="chat",
        target_node_id=node_id,
        summary="answer",
    )
    run_repository.finish_run(run_id, "failed", "boom")
    repository.add_message(
        conversation_id,
        node_id,
        "assistant",
        "部分回答",
        subtype="assistant_answer",
        metadata={"run_id": run_id},
        message_id="assistant-failed",
    )

    answer = _snapshot(persistence, conversation_id, node_id)["items"][0]

    assert answer["type"] == "assistant_answer"
    assert answer["status"] == "error"


def test_snapshot_does_not_read_removed_projection_tables(tmp_path):
    persistence, repository = _repo(tmp_path)
    conversation_id, node_id = _conversation(repository)
    repository.add_message(conversation_id, node_id, "user", "canonical")
    with persistence.connect() as conn:
        removed_tables = (
            "transcript_" + "items",
            "plan_" + "proposals",
            "plan_" + "events",
        )
        for table in removed_tables:
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                (table,),
            ).fetchone()
            assert exists is None

    types = {item["type"] for item in _snapshot(persistence, conversation_id, node_id)["items"]}

    assert types <= TRANSCRIPT_ITEM_TYPES


def test_patch_streams_plain_text_as_process_content_until_complete(tmp_path):
    persistence, repository = _repo(tmp_path)
    conversation_id, node_id = _conversation(repository)
    run_id = SQLiteRunRepository(persistence).create_run(
        conversation_id,
        kind="chat",
        target_node_id=node_id,
        summary="stream",
    )
    session = TranscriptAssembler(persistence).patch_session(run_id)
    session.feed({
        "status": "start",
        "conversation_id": conversation_id,
        "node_id": node_id,
        "assistant_message_id": "assistant-1",
    })

    first = session.feed({"type": "message_delta", "conversation_id": conversation_id, "node_id": node_id, "content": "A"})
    second = session.feed({"type": "message_delta", "conversation_id": conversation_id, "node_id": node_id, "content": "B"})

    first_process = next(operation for operation in first["operations"] if operation["item"]["type"] == "assistant_process")
    second_process = next(operation for operation in second["operations"] if operation["item"]["type"] == "assistant_process")
    assert "assistant_answer" not in {operation["item"]["type"] for operation in first["operations"] if operation["op"] == "upsert"}
    assert first_process["item"]["blocks"] == [
        {
            "type": "content",
            "id": f"content:{run_id}:0",
            "content": "A",
            "streaming": True,
        }
    ]
    assert second_process["item"]["blocks"][0]["content"] == "AB"
    assert second_process["op"] == "upsert"
    repository.add_message(
        conversation_id,
        node_id,
        "assistant",
        "AB",
        subtype="assistant_answer",
        metadata={"run_id": run_id},
        message_id="assistant-1",
    )

    final = session.feed({
        "status": "complete",
        "conversation_id": conversation_id,
        "node_id": node_id,
        "assistant_message_id": "assistant-1",
    })

    final_answer = next(
        operation
        for operation in final["operations"]
        if operation["op"] == "upsert" and operation["item"]["type"] == "assistant_answer"
    )
    assert final_answer["item"]["content"] == "AB"
    assert final_answer["item"]["status"] == "complete"
    assert len([
        operation for operation in final["operations"]
        if operation["op"] == "upsert" and operation["item"]["id"] == "message:assistant-1"
    ]) == 1


def test_patch_accumulates_plan_process_content_in_one_block(tmp_path):
    persistence, repository = _repo(tmp_path)
    conversation_id, node_id = _conversation(repository)
    run_id = SQLiteRunRepository(persistence).create_run(
        conversation_id,
        kind="chat",
        target_node_id=node_id,
        summary="plan stream",
    )
    session = TranscriptAssembler(persistence).patch_session(run_id)
    session.feed({
        "status": "start",
        "conversation_id": conversation_id,
        "node_id": node_id,
        "assistant_message_id": "assistant-plan",
    })

    first = session.feed({
        "event_type": "process_content",
        "conversation_id": conversation_id,
        "node_id": node_id,
        "content": "计划",
    })
    second = session.feed({
        "event_type": "process_content",
        "conversation_id": conversation_id,
        "node_id": node_id,
        "content": "内容",
    })

    first_process = next(operation["item"] for operation in first["operations"] if operation["item"]["type"] == "assistant_process")
    second_process = next(operation["item"] for operation in second["operations"] if operation["item"]["type"] == "assistant_process")
    assert len(first_process["blocks"]) == 1
    assert first_process["blocks"][0]["content"] == "计划"
    assert len(second_process["blocks"]) == 1
    assert second_process["blocks"][0]["id"] == first_process["blocks"][0]["id"]
    assert second_process["blocks"][0]["content"] == "计划内容"


def test_complete_patch_uses_trailing_live_content_as_answer_before_sqlite_assistant_is_persisted(tmp_path):
    persistence, repository = _repo(tmp_path)
    conversation_id, node_id = _conversation(repository)
    run_id = SQLiteRunRepository(persistence).create_run(
        conversation_id,
        kind="chat",
        target_node_id=node_id,
        summary="stream",
    )
    session = TranscriptAssembler(persistence).patch_session(run_id)
    session.feed({
        "status": "start",
        "conversation_id": conversation_id,
        "node_id": node_id,
        "assistant_message_id": "assistant-late",
    })
    session.feed({
        "status": "content",
        "conversation_id": conversation_id,
        "node_id": node_id,
        "reasoning": "thinking",
    })
    session.feed({
        "status": "content",
        "conversation_id": conversation_id,
        "node_id": node_id,
        "content": "final ",
    })
    live_content = session.feed({
        "status": "content",
        "conversation_id": conversation_id,
        "node_id": node_id,
        "content": "answer",
    })

    final = session.feed({
        "status": "complete",
        "conversation_id": conversation_id,
        "node_id": node_id,
    })

    live_items = [
        operation["item"]
        for operation in live_content["operations"]
        if operation["op"] == "upsert"
    ]
    assert live_items[-1]["type"] == "assistant_process"
    assert [block["type"] for block in live_items[-1]["blocks"]] == ["reasoning", "content"]
    assert live_items[-1]["blocks"][-1]["content"] == "final answer"

    items = [
        operation["item"]
        for operation in final["operations"]
        if operation["op"] == "upsert"
    ]
    assert [item["type"] for item in items] == ["assistant_process", "assistant_answer"]
    assert items[0]["blocks"] == [
        {
            "type": "reasoning",
            "id": f"reasoning:{run_id}:0",
            "content": "thinking",
            "streaming": False,
        }
    ]
    assert items[1]["id"] == "message:assistant-late"
    assert items[1]["content"] == "final answer"


def test_terminal_error_patch_keeps_live_answer_without_canonical_message(tmp_path):
    persistence, repository = _repo(tmp_path)
    conversation_id, node_id = _conversation(repository)
    run_id = SQLiteRunRepository(persistence).create_run(
        conversation_id,
        kind="chat",
        target_node_id=node_id,
        summary="stream",
    )
    session = TranscriptAssembler(persistence).patch_session(run_id)
    session.feed({
        "status": "content",
        "conversation_id": conversation_id,
        "node_id": node_id,
        "assistant_message_id": "assistant-error",
        "content": "部分回答",
    })

    final = session.feed({
        "type": "run_finished",
        "status": "failed",
        "conversation_id": conversation_id,
        "target_node_id": node_id,
        "assistant_message_id": "assistant-error",
    })

    operations = final["operations"]
    assert not any(operation["op"] == "remove" and operation["id"] == "message:assistant-error" for operation in operations)
    answer = next(
        operation["item"]
        for operation in operations
        if operation["op"] == "upsert" and operation["item"]["id"] == "message:assistant-error"
    )
    assert answer["type"] == "assistant_answer"
    assert answer["content"] == "部分回答"
    assert answer["status"] == "error"


def test_terminal_stopped_patch_keeps_live_answer_without_canonical_message(tmp_path):
    persistence, repository = _repo(tmp_path)
    conversation_id, node_id = _conversation(repository)
    run_id = SQLiteRunRepository(persistence).create_run(
        conversation_id,
        kind="chat",
        target_node_id=node_id,
        summary="stream",
    )
    session = TranscriptAssembler(persistence).patch_session(run_id)
    session.feed({
        "status": "content",
        "conversation_id": conversation_id,
        "node_id": node_id,
        "assistant_message_id": "assistant-stopped",
        "content": "已生成内容",
    })

    final = session.feed({
        "type": "run_finished",
        "status": "cancelled",
        "conversation_id": conversation_id,
        "target_node_id": node_id,
        "assistant_message_id": "assistant-stopped",
    })

    answer = next(
        operation["item"]
        for operation in final["operations"]
        if operation["op"] == "upsert" and operation["item"]["id"] == "message:assistant-stopped"
    )
    assert answer["content"] == "已生成内容"
    assert answer["status"] == "stopped"


def test_complete_patch_does_not_emit_temporary_answer_without_message_id(tmp_path):
    persistence, repository = _repo(tmp_path)
    conversation_id, node_id = _conversation(repository)
    run_id = SQLiteRunRepository(persistence).create_run(
        conversation_id,
        kind="chat",
        target_node_id=node_id,
        summary="stream",
    )
    session = TranscriptAssembler(persistence).patch_session(run_id)
    session.feed({
        "status": "content",
        "conversation_id": conversation_id,
        "node_id": node_id,
        "content": "最终答案",
    })

    final = session.feed({
        "status": "complete",
        "conversation_id": conversation_id,
        "node_id": node_id,
    })

    assert final is None or "assistant_answer" not in {
        operation["item"]["type"]
        for operation in final["operations"]
        if operation["op"] == "upsert"
    }


def test_final_patch_uses_canonical_process_blocks_instead_of_live_buffer(tmp_path):
    persistence, repository = _repo(tmp_path)
    conversation_id, node_id = _conversation(repository)
    run_repository = SQLiteRunRepository(persistence)
    run_id = run_repository.create_run(
        conversation_id,
        kind="chat",
        target_node_id=node_id,
        summary="final",
    )
    repository.add_message(
        conversation_id,
        node_id,
        "assistant",
        "已经持久化的过程。",
        subtype="assistant_process_reasoning",
        hidden=True,
        transcript_only=True,
        metadata={"run_id": run_id},
        message_id="reasoning-final",
    )
    run_repository.finish_run(run_id, "completed")
    assembler = TranscriptAssembler(persistence)
    session = assembler.patch_session(run_id)
    session.feed({
        "event_type": "reasoning",
        "status": "content",
        "conversation_id": conversation_id,
        "node_id": node_id,
        "reasoning": "已经持久化的过程。",
    }, emit=False)

    patch = session.feed({
        "type": "run_finished",
        "status": "completed",
        "conversation_id": conversation_id,
        "target_node_id": node_id,
    })

    process_items = [
        operation["item"]
        for operation in patch["operations"]
        if operation["op"] == "upsert" and operation["item"]["type"] == "assistant_process"
    ]
    snapshot_process = next(
        item
        for item in assembler.snapshot(conversation_id, node_id)["items"]
        if item["type"] == "assistant_process"
    )
    assert process_items == [snapshot_process]
    assert process_items[0]["blocks"] == [
        {
            "type": "reasoning",
            "id": "reasoning:reasoning-final",
            "content": "已经持久化的过程。",
            "streaming": False,
        }
    ]


def test_sse_attach_from_event_primes_full_stream_state_without_revision_floor(tmp_path):
    async def scenario():
        persistence, _repository = _repo(tmp_path)
        conversation_id, node_id = _conversation(_repository)
        manager = RunManager(repository=SQLiteRunRepository(persistence))
        run = await manager.create_run(
            conversation_id=conversation_id,
            kind=RunKind.CHAT,
            target_node_id=node_id,
            summary="resume",
        )
        await manager.append_event(run.run_id, {
            "status": "content",
            "conversation_id": conversation_id,
            "node_id": node_id,
            "assistant_message_id": "assistant-resume",
            "content": "A",
        })
        await manager.append_event(run.run_id, {
            "status": "content",
            "conversation_id": conversation_id,
            "node_id": node_id,
            "content": "B",
        })

        stream = runs_route._subscribe_sse(
            manager,
            TranscriptAssembler(persistence),
            run.run_id,
            from_event=2,
        )
        line = await asyncio.wait_for(anext(stream), timeout=1)
        await stream.aclose()

        patch = json.loads(line.removeprefix("data: "))
        process = next(
            operation["item"]
            for operation in patch["operations"]
            if operation["op"] == "upsert"
            and operation["item"]["type"] == "assistant_process"
        )
        assert patch["revision"] == 1
        assert process["blocks"][-1]["type"] == "content"
        assert process["blocks"][-1]["content"] == "AB"

    asyncio.run(scenario())


def test_chat_producer_withholds_error_chunk_until_run_finished(tmp_path):
    class ErrorAfterContentManager:
        def __init__(self):
            self.finalized = False

        async def send_message_stream(self, **kwargs):
            yield {
                "status": StreamStatus.CONTENT,
                "content": "live text",
                "node_id": "node-a",
                "conversation_id": kwargs["conversation_id"],
                "run_id": kwargs["run_id"],
                "tokens_used": 0,
            }
            yield {
                "status": StreamStatus.ERROR,
                "content": "",
                "node_id": "node-a",
                "conversation_id": kwargs["conversation_id"],
                "run_id": kwargs["run_id"],
                "error": "boom",
                "tokens_used": 0,
            }
            self.finalized = True

    async def scenario():
        manager = RunManager()
        chat_manager = ErrorAfterContentManager()
        run = await manager.create_run(
            conversation_id="conv-1",
            kind=RunKind.CHAT,
            target_node_id="node-a",
            summary="error",
        )

        await messages_route._produce_chat_run(
            run=run,
            conversation_id="conv-1",
            request=messages_route.SendMessageRequest(
                content="hello",
                parent_node_id="node-a",
            ),
            chat_manager=chat_manager,
            run_manager=manager,
        )

        events = manager.read_events(run.run_id)
        assert chat_manager.finalized is True
        assert [event.get("status") for event in events].count("error") == 0
        assert events[-1]["type"] == "run_finished"
        assert events[-1]["status"] == RunStatus.FAILED.value
        assert events[-1]["error"] == "boom"

    asyncio.run(scenario())


def test_direct_response_persists_canonical_assistant_answer(tmp_path):
    class RouteChatManager:
        def __init__(self, repository):
            self.chat_repository = repository

        def get_conversation(self, conversation_id):
            return None

    async def scenario():
        persistence, repository = _repo(tmp_path)
        conversation_id, node_id = _conversation(repository)
        manager = RunManager(repository=SQLiteRunRepository(persistence))
        run = await manager.create_run(
            conversation_id=conversation_id,
            kind=RunKind.DIRECT_RESPONSE,
            anchor_node_id=node_id,
            summary="/status",
        )
        slash_result = SlashCommandDispatcher().dispatch("/status")

        await messages_route._produce_direct_response(
            run=run,
            conversation_id=conversation_id,
            request=messages_route.SendMessageRequest(
                content="/status",
                parent_node_id=node_id,
            ),
            slash_result=slash_result,
            chat_manager=RouteChatManager(repository),
            run_manager=manager,
        )

        items = TranscriptAssembler(persistence).snapshot(conversation_id, node_id)["items"]
        answer = next(item for item in items if item["type"] == "assistant_answer")
        events = manager.read_events(run.run_id)
        assert "ChatTree" in answer["content"]
        assert answer["message_id"] == f"{run.run_id}:assistant"
        assert not any(event.get("status") == "complete" for event in events)
        assert events[-1]["type"] == "run_finished"

    asyncio.run(scenario())


def test_prune_summary_persists_canonical_assistant_answer(tmp_path):
    class RouteChatManager:
        def __init__(self, repository):
            self.chat_repository = repository

        def get_conversation(self, conversation_id):
            return None

        async def prune_summary(self, conversation_id, parent_node_id, **kwargs):
            return {
                "conversation_id": conversation_id,
                "parent_node_id": parent_node_id,
                "summary_id": "summary-1",
                "covered_node_count": 3,
                "covered_direct_child_count": 2,
                "summary_preview": "durable preview",
            }

    async def scenario():
        persistence, repository = _repo(tmp_path)
        conversation_id, node_id = _conversation(repository)
        manager = RunManager(repository=SQLiteRunRepository(persistence))
        run = await manager.create_run(
            conversation_id=conversation_id,
            kind=RunKind.DIRECT_RESPONSE,
            anchor_node_id=node_id,
            summary="/prune-summary",
        )
        slash_result = SlashCommandDispatcher().dispatch(f"/prune-summary node:{node_id}")

        await messages_route._produce_prune_summary(
            run=run,
            conversation_id=conversation_id,
            request=messages_route.SendMessageRequest(
                content=f"/prune-summary node:{node_id}",
                parent_node_id=node_id,
            ),
            slash_result=slash_result,
            chat_manager=RouteChatManager(repository),
            run_manager=manager,
        )

        items = TranscriptAssembler(persistence).snapshot(conversation_id, node_id)["items"]
        answer = next(item for item in items if item["type"] == "assistant_answer")
        events = manager.read_events(run.run_id)
        assert "剪枝摘要已生成" in answer["content"]
        assert "durable preview" in answer["content"]
        assert answer["message_id"] == f"{run.run_id}:assistant"
        assert not any(event.get("status") == "complete" for event in events)
        assert events[-1]["type"] == "run_finished"

    asyncio.run(scenario())


def test_patch_starts_new_turn_with_canonical_user_message(tmp_path):
    persistence, repository = _repo(tmp_path)
    conversation_id, node_id = _conversation(repository)
    user_id = repository.add_message(conversation_id, node_id, "user", "测试真实流")
    run_id = SQLiteRunRepository(persistence).create_run(
        conversation_id,
        kind="chat",
        target_node_id=node_id,
        summary="stream",
    )
    session = TranscriptAssembler(persistence).patch_session(run_id)

    first = session.feed({
        "type": "message_delta",
        "conversation_id": conversation_id,
        "node_id": node_id,
        "assistant_message_id": "assistant-1",
        "content": "OK",
    })
    second = session.feed({
        "type": "message_delta",
        "conversation_id": conversation_id,
        "node_id": node_id,
        "content": "。",
    })

    first_items = [operation["item"] for operation in first["operations"]]
    assert (first["operations"][0]["op"], first_items[0]["type"], first_items[0]["id"]) == (
        "upsert",
        "user_message",
        f"message:{user_id}",
    )
    assert {item["type"] for item in first_items} == {
        "user_message",
        "assistant_process",
    }
    assert "user_message" not in {operation["item"]["type"] for operation in second["operations"]}
    process = next(item for item in first_items if item["type"] == "assistant_process")
    assert process["blocks"][-1]["type"] == "content"


def test_patch_revisions_are_monotonic_per_conversation_node_across_sessions(tmp_path):
    persistence, repository = _repo(tmp_path)
    conversation_id, node_id = _conversation(repository)
    run_repository = SQLiteRunRepository(persistence)
    first_run = run_repository.create_run(
        conversation_id,
        kind="chat",
        target_node_id=node_id,
        summary="first",
    )
    second_run = run_repository.create_run(
        conversation_id,
        kind="chat",
        target_node_id=node_id,
        summary="second",
    )
    assembler = TranscriptAssembler(persistence)

    first = assembler.patch_session(first_run).feed({
        "type": "message_delta",
        "conversation_id": conversation_id,
        "node_id": node_id,
        "assistant_message_id": "assistant-1",
        "content": "一",
    })
    second = assembler.patch_session(second_run).feed({
        "type": "message_delta",
        "conversation_id": conversation_id,
        "node_id": node_id,
        "assistant_message_id": "assistant-2",
        "content": "二",
    })
    action_revision = assembler.next_revision(conversation_id, node_id)

    assert [first["revision"], second["revision"], action_revision] == [1, 2, 3]


def test_patch_uses_stable_process_ids_without_running_assistant_answer(tmp_path):
    persistence, repository = _repo(tmp_path)
    conversation_id, node_id = _conversation(repository)
    run_id = SQLiteRunRepository(persistence).create_run(
        conversation_id,
        kind="chat",
        target_node_id=node_id,
        summary="stream",
    )
    session = TranscriptAssembler(persistence).patch_session(run_id)

    patch = session.feed({
        "status": "content",
        "conversation_id": conversation_id,
        "node_id": node_id,
        "assistant_message_id": "assistant-1",
        "content": "done",
        "reasoning": "idea",
    })

    items = [operation["item"] for operation in patch["operations"]]
    assert {item["type"]: item["id"] for item in items} == {
        "assistant_process": f"process:{node_id}:0",
    }
    assert items[0]["blocks"] == [
        {
            "type": "reasoning",
            "id": f"reasoning:{run_id}:0",
            "content": "idea",
            "streaming": True,
        },
        {
            "type": "content",
            "id": f"content:{run_id}:1",
            "content": "done",
            "streaming": True,
        },
    ]


def test_patch_moves_text_before_tool_call_into_process_content(tmp_path):
    persistence, repository = _repo(tmp_path)
    conversation_id, node_id = _conversation(repository)
    run_id = SQLiteRunRepository(persistence).create_run(
        conversation_id,
        kind="chat",
        target_node_id=node_id,
        summary="stream",
    )
    session = TranscriptAssembler(persistence).patch_session(run_id)
    session.feed({
        "status": "content",
        "conversation_id": conversation_id,
        "node_id": node_id,
        "content": "我先检查。",
    })
    patch = session.feed({
        "status": "content",
        "conversation_id": conversation_id,
        "node_id": node_id,
        "event_type": "tool_call",
        "tool_calls": [
            {"id": "call-files", "function": {"name": "list_files", "arguments": "{\"path\":\".\"}"}},
        ],
    })

    items = [operation["item"] for operation in patch["operations"] if operation["op"] == "upsert"]

    assert [item["type"] for item in items] == ["assistant_process"]
    assert [block["type"] for block in items[0]["blocks"]] == ["content", "tool_call"]
    assert items[0]["blocks"][0]["content"] == "我先检查。"


def test_patch_keeps_tool_before_later_reasoning_after_tool_update(tmp_path):
    persistence, repository = _repo(tmp_path)
    conversation_id, node_id = _conversation(repository)
    run_id = SQLiteRunRepository(persistence).create_run(
        conversation_id,
        kind="chat",
        target_node_id=node_id,
        summary="stream",
    )
    session = TranscriptAssembler(persistence).patch_session(run_id)
    session.feed({
        "status": "content",
        "conversation_id": conversation_id,
        "node_id": node_id,
        "reasoning": "先分析。",
    })
    session.feed({
        "status": "content",
        "conversation_id": conversation_id,
        "node_id": node_id,
        "content": "我先检查。",
    })
    session.feed({
        "status": "content",
        "conversation_id": conversation_id,
        "node_id": node_id,
        "event_type": "tool_calls_committed",
        "tool_calls": [
            {"id": "call-files", "function": {"name": "list_files", "arguments": "{\"path\":\".\"}"}},
        ],
    })
    session.feed({
        "status": "content",
        "conversation_id": conversation_id,
        "node_id": node_id,
        "event_type": "tool_progress",
        "tool_call": {
            "id": "call-files",
            "tool_call_id": "call-files",
            "name": "list_files",
            "status": "running",
            "function": {"name": "list_files", "arguments": "{\"path\":\".\"}"},
        },
    })
    patch = session.feed({
        "status": "content",
        "conversation_id": conversation_id,
        "node_id": node_id,
        "reasoning": "工具后继续分析。",
    })

    process = next(
        operation["item"]
        for operation in patch["operations"]
        if operation["op"] == "upsert" and operation["item"]["type"] == "assistant_process"
    )

    assert [block["type"] for block in process["blocks"]] == [
        "reasoning",
        "content",
        "tool_call",
        "reasoning",
    ]
    assert process["blocks"][2]["tool_call_id"] == "call-files"
    assert process["blocks"][3]["content"] == "工具后继续分析。"


def test_patch_updates_live_tool_approval_request_and_result_with_stable_id(tmp_path):
    persistence, repository = _repo(tmp_path)
    conversation_id, node_id = _conversation(repository)
    run_id = SQLiteRunRepository(persistence).create_run(
        conversation_id,
        kind="chat",
        target_node_id=node_id,
        summary="approval",
    )
    session = TranscriptAssembler(persistence).patch_session(run_id)

    request_patch = session.feed({
        "event_type": "tool_approval_request",
        "conversation_id": conversation_id,
        "node_id": node_id,
        "approval": {
            "tool_call_id": "call-shell",
            "tool_name": "shell_command",
            "arguments_preview": "{\"command\":\"pwd\"}",
            "status": "pending",
        },
    })
    result_patch = session.feed({
        "event_type": "tool_approval_result",
        "conversation_id": conversation_id,
        "node_id": node_id,
        "approval": {
            "tool_call_id": "call-shell",
            "tool_name": "shell_command",
            "status": "approved",
        },
    })

    request_item = request_patch["operations"][0]["item"]
    result_item = result_patch["operations"][0]["item"]
    assert request_item["id"] == "tool-approval:call-shell"
    assert request_item["status"] == "awaiting_approval"
    assert result_item["id"] == "tool-approval:call-shell"
    assert result_item["status"] == "approved"


def test_patch_only_upserts_changed_items(tmp_path):
    persistence, repository = _repo(tmp_path)
    conversation_id, node_id = _conversation(repository)
    run_id = SQLiteRunRepository(persistence).create_run(
        conversation_id,
        kind="chat",
        target_node_id=node_id,
        summary="approval",
    )
    session = TranscriptAssembler(persistence).patch_session(run_id)
    session.feed({
        "status": "start",
        "conversation_id": conversation_id,
        "node_id": node_id,
        "assistant_message_id": "assistant-approval",
    })
    session.feed({
        "event_type": "tool_approval_request",
        "conversation_id": conversation_id,
        "node_id": node_id,
        "approval": {
            "tool_call_id": "call-shell",
            "tool_name": "shell_command",
            "arguments_preview": '{"command":"pwd"}',
            "status": "pending",
        },
    })

    patch = session.feed({
        "status": "content",
        "conversation_id": conversation_id,
        "node_id": node_id,
        "content": "waiting explanation",
    })

    items = [
        operation["item"]
        for operation in patch["operations"]
        if operation["op"] == "upsert"
    ]
    assert [item["type"] for item in items] == ["assistant_process"]
    assert items[0]["blocks"][-1]["type"] == "content"
    assert items[0]["blocks"][-1]["content"] == "waiting explanation"


def test_patch_keeps_stopping_out_of_assistant_process_status(tmp_path):
    persistence, repository = _repo(tmp_path)
    conversation_id, node_id = _conversation(repository)
    run_id = SQLiteRunRepository(persistence).create_run(
        conversation_id,
        kind="chat",
        target_node_id=node_id,
        summary="stop",
    )

    patch = TranscriptAssembler(persistence).patch_session(run_id).feed({
        "status": 'stopping',
        "conversation_id": conversation_id,
        "node_id": node_id,
        "reasoning": "还在收尾",
    })
    items = [operation["item"] for operation in patch["operations"]]

    assert next(item for item in items if item["type"] == "assistant_process")["status"] == "stopped"
    assert next(item for item in items if item["type"] == "run_status")["status"] == 'stopping'


def test_patch_upsert_carries_global_transcript_index(tmp_path):
    persistence, repository = _repo(tmp_path)
    conversation_id, root_id = _conversation(repository)
    child_id = repository.create_node(conversation_id, parent_id=root_id, child_order=0)
    root_message_id = repository.add_message(conversation_id, root_id, "user", "first")
    child_message_id = repository.add_message(conversation_id, child_id, "user", "second")
    run_id = SQLiteRunRepository(persistence).create_run(
        conversation_id,
        kind="chat",
        target_node_id=child_id,
        summary="index",
    )

    patch = TranscriptAssembler(persistence).patch_session(run_id).feed({
        "status": "content",
        "conversation_id": conversation_id,
        "node_id": child_id,
        "assistant_message_id": "assistant-1",
        "content": "answer",
    })

    index_by_id = {
        operation["item"]["id"]: operation["index"]
        for operation in patch["operations"]
        if operation["op"] == "upsert"
    }
    assert index_by_id == {
        f"message:{child_message_id}": 1,
        f"process:{child_id}:0": 2,
    }
    assert f"message:{root_message_id}" not in index_by_id


def test_patch_emits_run_status_for_empty_terminal_failure_or_stop(tmp_path):
    for terminal_status, expected in (
        ("failed", "error"),
        ("error", "error"),
        ("stopped", "stopped"),
        ("cancelled", "stopped"),
        ("interrupted", "stopped"),
    ):
        persistence, repository = _repo(tmp_path / terminal_status)
        conversation_id, node_id = _conversation(repository)
        run_id = SQLiteRunRepository(persistence).create_run(
            conversation_id,
            kind="chat",
            target_node_id=node_id,
            summary=terminal_status,
        )

        patch = TranscriptAssembler(persistence).patch_session(run_id).feed({
            "type": "run_finished",
            "status": terminal_status,
            "conversation_id": conversation_id,
            "target_node_id": node_id,
            "run_id": run_id,
        })

        items = [
            operation["item"]
            for operation in patch["operations"]
            if operation["op"] == "upsert"
        ]
        assert items == [{
            "type": "run_status",
            "id": f"run-status:{run_id}",
            "conversation_id": conversation_id,
            "node_id": node_id,
            "run_id": run_id,
            "status": expected,
        }]
        assert patch["operations"][0]["index"] == 0


def test_snapshot_assembles_plan_approval_from_exit_plan_mode_tool(tmp_path):
    persistence, repository = _repo(tmp_path)
    conversation_id, node_id = _conversation(repository)
    repository.add_tool_call(
        conversation_id,
        node_id,
        tool_call_id="call-exit",
        name="exit_plan_mode",
        arguments={"plan": "1. 改后端\n2. 跑测试"},
    )
    repository.add_tool_result(
        conversation_id,
        node_id,
        tool_result_id="result-exit",
        tool_call_id="call-exit",
        output=json.dumps({"plan_id": "plan-1", "status": "approved"}, ensure_ascii=False),
    )

    approval = _snapshot(persistence, conversation_id, node_id)["items"][0]

    assert approval["type"] == "plan_approval"
    assert approval["id"] == "plan-approval:call-exit"
    assert approval["tool_call_id"] == "call-exit"
    assert approval["plan_id"] == "plan-1"
    assert approval["status"] == "approved"
    assert approval["plan"] == "1. 改后端\n2. 跑测试"


def test_snapshot_assembles_task_notification_from_canonical_table(tmp_path):
    persistence, repository = _repo(tmp_path)
    conversation_id, node_id = _conversation(repository)
    run_id = SQLiteRunRepository(persistence).create_run(
        conversation_id,
        kind="workflow",
        anchor_node_id=node_id,
        summary="workflow finished",
    )
    with persistence.connect() as conn:
        conn.execute(
            """
            INSERT INTO task_notifications (
              id,
              conversation_id,
              source_run_id,
              source_run_kind,
              status,
              summary,
              content,
              created_at,
              updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, strftime('%s', 'now'), strftime('%s', 'now'))
            """,
            (
                "notification-1",
                conversation_id,
                run_id,
                "workflow",
                "unbound",
                "工作流完成",
                "产物已生成",
            ),
        )

    notification = _snapshot(persistence, conversation_id, node_id)["items"][0]

    assert notification["type"] == "task_notification"
    assert notification["id"] == "task-notification:notification-1"
    assert notification["node_id"] == node_id
    assert notification["source_run_id"] == run_id
    assert notification["source_run_kind"] == "workflow"
    assert notification["summary"] == "工作流完成"
    assert notification["content"] == "产物已生成"
