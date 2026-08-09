from __future__ import annotations

import json
from collections import defaultdict
from typing import Any

from .persistence.blob_store import BlobStore
from .persistence.database import SQLitePersistence
from .persistence.repository import tool_result_preview


PLAN_QUESTION_TOOLS = {"ask_user_question"}
PLAN_APPROVAL_TOOLS = {"exit_plan_mode"}
PROCESS_MESSAGE_BLOCKS = {
    "assistant_process_reasoning": "reasoning",
    "assistant_round": "content",
}


class TranscriptAssembler:
    def __init__(self, persistence: SQLitePersistence) -> None:
        self.persistence = persistence
        self.blobs = BlobStore(persistence)
        self._revision_by_node: dict[tuple[str, str], int] = {}
        # 每个节点"已推送给前端"的项基线（id -> 序列化值）。按节点共享，
        # 使同一节点的跨 run 会话（如失败 run 的续写子 run）能以它为基础做差量
        # 校正，从而 diff 掉陈旧项（例如失败 run 残留的 run_status 错误项）。
        self._emitted_by_node: dict[tuple[str, str], dict[str, str]] = {}

    def next_revision(self, conversation_id: str, node_id: str | None, floor: int = 0) -> int:
        key = (conversation_id, str(node_id or ""))
        current = max(self._revision_by_node.get(key, 0), int(floor or 0))
        current += 1
        self._revision_by_node[key] = current
        return current

    def snapshot(
        self,
        conversation_id: str,
        node_id: str | None = None,
        active_streams: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        with self.persistence.connect() as conn:
            conversation = conn.execute(
                """
                SELECT id, current_node_id, root_node_id
                FROM conversations
                WHERE id = ?
                """,
                (conversation_id,),
            ).fetchone()
            if conversation is None:
                raise KeyError(conversation_id)
            tip_node_id = node_id or conversation["current_node_id"] or conversation["root_node_id"]
            if not tip_node_id:
                return {
                    "conversation_id": conversation_id,
                    "node_id": None,
                    "revision": self._revision_by_node.get((conversation_id, ""), 0),
                    "items": [],
                }
            branch = self._load_branch(conn, conversation_id, str(tip_node_id))
            if not branch:
                raise KeyError(str(tip_node_id))
            node_ids = [str(row["id"]) for row in branch]
            messages = self._load_messages(conn, conversation_id, node_ids)
            tool_calls = self._load_tool_calls(conn, conversation_id, node_ids)
            tool_results = self._load_tool_results(conn, conversation_id, node_ids)
            plans = self._load_plans(conn, conversation_id)
            runs = self._load_runs(conn, conversation_id, node_ids)
            task_notifications = self._load_task_notifications(conn, conversation_id, node_ids)

        items: list[dict[str, Any]] = []
        result_by_call_id = {
            str(result.get("tool_call_id") or ""): result
            for result in tool_results
            if result.get("tool_call_id")
        }
        plan_by_question = {
            str(plan.get("question_tool_call_id")): plan
            for plan in plans
            if plan.get("question_tool_call_id")
        }
        plan_by_exit = {
            str(plan.get("exit_tool_call_id")): plan
            for plan in plans
            if plan.get("exit_tool_call_id")
        }
        runs_by_node = defaultdict(list)
        for run in runs:
            target = run.get("target_node_id") or run.get("anchor_node_id")
            if target:
                runs_by_node[str(target)].append(run)
        notifications_by_node = defaultdict(list)
        for notification in task_notifications:
            target = notification.get("delivery_node_id") or notification.get("anchor_node_id") or notification.get("target_node_id")
            if target:
                notifications_by_node[str(target)].append(notification)

        active_stream_by_node = {
            str(stream.get("target_node_id") or stream.get("node_id") or ""): stream
            for stream in active_streams or []
            if stream.get("target_node_id") or stream.get("node_id")
        }
        for node in branch:
            node_id_value = str(node["id"])
            node_runs = runs_by_node.get(node_id_value, [])
            items.extend(self._node_items(
                conversation_id=conversation_id,
                node=node,
                messages=messages.get(node_id_value, []),
                tool_calls=tool_calls.get(node_id_value, []),
                result_by_call_id=result_by_call_id,
                plans_by_question=plan_by_question,
                plans_by_exit=plan_by_exit,
                runs=node_runs,
                task_notifications=notifications_by_node.get(node_id_value, []),
                stream=active_stream_by_node.get(node_id_value),
            ))

        return {
            "conversation_id": conversation_id,
            "node_id": str(tip_node_id),
            # 返回当前 patch revision 前沿：快照重载后前端仍能接续 SSE patch 链，
            # 否则 revision 归零会让流式期间的所有 patch 被拒并陷入无限校准
            "revision": self._revision_by_node.get((conversation_id, str(tip_node_id)), 0),
            "items": items,
        }

    def patch_for_run_event(self, run_id: str, event: dict[str, Any]) -> dict[str, Any] | None:
        return TranscriptPatchSession(self, run_id).feed(event)

    def patch_for_run_buffer(self, run_id: str, events: list[dict[str, Any]]) -> dict[str, Any] | None:
        session = TranscriptPatchSession(self, run_id)
        patch = None
        for event in events:
            patch = session.feed(event)
        return patch

    def stream_from_run_events(self, run_id: str, events: list[dict[str, Any]]) -> dict[str, Any]:
        session = TranscriptPatchSession(self, run_id)
        for event in events:
            session.feed(event, emit=False)
        return session._stream_state()

    def patch_session(self, run_id: str, revision_floor: int | None = None) -> "TranscriptPatchSession":
        return TranscriptPatchSession(self, run_id, revision_floor)

    def user_items_for_node(self, conversation_id: str, node_id: str) -> list[dict[str, Any]]:
        with self.persistence.connect() as conn:
            node = conn.execute(
                """
                SELECT id, parent_id, depth, child_order, tool_permission_mode, task_context_mode, created_at, updated_at
                FROM nodes
                WHERE conversation_id = ? AND id = ?
                """,
                (conversation_id, node_id),
            ).fetchone()
            if node is None:
                return []
            messages = self._load_messages(conn, conversation_id, [node_id]).get(node_id, [])
        return [
            self._user_item(conversation_id, node, message)
            for message in messages
            if message.get("role") == "user" and not message.get("hidden")
        ]

    def live_node_items(
        self,
        conversation_id: str,
        node_id: str,
        stream: dict[str, Any],
    ) -> list[dict[str, Any]]:
        with self.persistence.connect() as conn:
            node = conn.execute(
                """
                SELECT id, parent_id, depth, child_order, tool_permission_mode, task_context_mode, created_at, updated_at
                FROM nodes
                WHERE conversation_id = ? AND id = ?
                """,
                (conversation_id, node_id),
            ).fetchone()
            if node is None:
                node = {
                    "id": node_id,
                    "parent_id": None,
                    "depth": 0,
                    "child_order": 0,
                    "tool_permission_mode": None,
                    "task_context_mode": "attached",
                    "created_at": None,
                    "updated_at": None,
                }
            messages = self._load_messages(conn, conversation_id, [node_id]).get(node_id, [])
            tool_calls = self._load_tool_calls(conn, conversation_id, [node_id]).get(node_id, [])
            tool_results = self._load_tool_results(conn, conversation_id, [node_id])
            plans = self._load_plans(conn, conversation_id)
            runs = self._load_runs(conn, conversation_id, [node_id])
            task_notifications = self._load_task_notifications(conn, conversation_id, [node_id])

        result_by_call_id = {
            str(result.get("tool_call_id") or ""): result
            for result in tool_results
            if result.get("tool_call_id")
        }
        stream_status = str(stream.get("status") or "running")
        use_live_stream = stream_status in {"running", "stopping", "finalizing", "stopped", "error"}
        if use_live_stream:
            for call_id, result in stream.get("result_overrides_by_call_id", {}).items():
                result_by_call_id[str(call_id)] = result
        calls = (
            self._merge_live_tool_calls(tool_calls, stream.get("tool_calls", []))
            if use_live_stream
            else tool_calls
        )
        return self._node_items(
            conversation_id=conversation_id,
            node=node,
            messages=messages,
            tool_calls=calls,
            result_by_call_id=result_by_call_id,
            plans_by_question={
                str(plan.get("question_tool_call_id")): plan
                for plan in plans
                if plan.get("question_tool_call_id")
            },
            plans_by_exit={
                str(plan.get("exit_tool_call_id")): plan
                for plan in plans
                if plan.get("exit_tool_call_id")
            },
            runs=runs,
            task_notifications=task_notifications,
            stream=stream,
        )

    def _node_items(
        self,
        *,
        conversation_id: str,
        node: Any,
        messages: list[dict[str, Any]],
        tool_calls: list[dict[str, Any]],
        result_by_call_id: dict[str, dict[str, Any]],
        plans_by_question: dict[str, dict[str, Any]],
        plans_by_exit: dict[str, dict[str, Any]],
        runs: list[dict[str, Any]],
        task_notifications: list[dict[str, Any]],
        stream: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        node_id = str(node["id"] if isinstance(node, dict) else node["id"])
        compact = self._compact_item(conversation_id, node, messages)
        if compact:
            return [compact]
        items: list[dict[str, Any]] = [
            self._user_item(conversation_id, node, message)
            for message in messages
            if message.get("role") == "user" and not message.get("hidden")
        ]
        run_by_id = {
            str(run.get("id") or run.get("run_id") or ""): run
            for run in runs
            if run.get("id") or run.get("run_id")
        }
        run_time_by_id = {
            str(run.get("id") or run.get("run_id") or ""): self._order_number(run.get("created_at"), 0.0)
            for run in runs
            if run.get("id") or run.get("run_id")
        }
        process_blocks: list[dict[str, Any]] = []
        process_sequence = 0
        raw_status = str((stream or {}).get("status") or self._run_status(runs))
        use_live_stream = stream is not None and raw_status in {"running", "stopping", "finalizing", "stopped", "error"}
        stream_run_id = str((stream or {}).get("run_id") or "") or None
        latest_run = runs[-1] if runs else {}
        latest_run_metadata = latest_run.get("metadata") if isinstance(latest_run.get("metadata"), dict) else {}
        error_message = str(
            (stream or {}).get("error_message")
            or latest_run_metadata.get("error")
            or ""
        ).strip() or None
        process_run_id: str | None = None

        def append_process_item() -> None:
            nonlocal process_blocks, process_sequence, process_run_id
            if not process_blocks:
                return
            status = self._process_status_for(process_run_id, run_by_id, raw_status, stream_run_id)
            items.append({
                "type": "assistant_process",
                "id": f"process:{node_id}:{process_sequence}",
                "conversation_id": conversation_id,
                "node_id": node_id,
                "run_id": process_run_id,
                "status": status,
                "duration_ms": None if use_live_stream else self._run_duration_ms(
                    [run_by_id[process_run_id]] if process_run_id in run_by_id else runs
                ),
                "blocks": process_blocks,
            })
            process_blocks = []
            process_sequence += 1
            process_run_id = None

        def append_process_block(block: dict[str, Any], run_id: str | None) -> None:
            nonlocal process_run_id
            if run_id and process_run_id and process_run_id != run_id and process_blocks:
                append_process_item()
            if run_id and process_run_id is None:
                process_run_id = run_id
            process_blocks.append({
                key: value
                for key, value in block.items()
                if not key.startswith("_")
            })

        def append_tool_call(call: dict[str, Any]) -> None:
            result = result_by_call_id.get(str(call.get("id") or ""))
            kind = self._participation_kind(call, result)
            if kind == "plan_question":
                append_process_item()
                items.append(self._plan_question_item(
                    conversation_id,
                    call,
                    result,
                    plans_by_question.get(str(call.get("id") or "")),
                ))
            elif kind == "plan_approval":
                append_process_item()
                items.append(self._plan_approval_item(
                    conversation_id,
                    call,
                    result,
                    plans_by_exit.get(str(call.get("id") or "")),
                ))
            elif kind == "tool_approval":
                append_process_item()
                items.append(self._tool_approval_item(conversation_id, call, result))
            else:
                append_process_block(self._tool_block(call, result), str(call.get("run_id") or "") or None)

        timeline: list[tuple[tuple[float, float, float, int], str, dict[str, Any]]] = []
        ordered_process_run_ids = {
            str((message.get("metadata") or {}).get("run_id") or "")
            for message in messages
            if isinstance(message.get("metadata"), dict)
            and (message.get("metadata") or {}).get("order") is not None
        }
        for message in messages:
            if (
                message.get("role") == "assistant"
                and str(message.get("subtype") or "") in PROCESS_MESSAGE_BLOCKS
                and str(message.get("content") or "").strip()
            ):
                metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
                if use_live_stream and str(metadata.get("run_id") or "") == stream_run_id:
                    continue
                timeline.append((self._message_sort_key(message, run_time_by_id), "process_message", message))
        live_entry_tool_call_ids = {
            str(entry.get("call", {}).get("id") or "")
            for entry in (stream or {}).get("entries", [])
            if use_live_stream
            and isinstance(entry, dict)
            and entry.get("type") == "tool_call"
            and isinstance(entry.get("call"), dict)
        }
        for call in tool_calls:
            if str(call.get("id") or "") in live_entry_tool_call_ids:
                continue
            timeline.append((
                self._tool_call_sort_key(call, run_time_by_id, ordered_process_run_ids),
                "tool_call",
                call,
            ))
        if use_live_stream and stream and isinstance(stream.get("entries"), list):
            for index, entry in enumerate(stream["entries"]):
                if not isinstance(entry, dict):
                    continue
                if entry.get("type") == "block" and isinstance(entry.get("block"), dict):
                    timeline.append(((float("inf"), float(index), 0.0, 0), "stream_block", dict(entry["block"])))
                elif entry.get("type") == "tool_call" and isinstance(entry.get("call"), dict):
                    timeline.append(((float("inf"), float(index), 1.0, 0), "tool_call", dict(entry["call"])))
        timeline.sort(key=lambda entry: entry[0])
        for _, kind, payload in timeline:
            if kind == "process_message":
                block_type = PROCESS_MESSAGE_BLOCKS[str(payload.get("subtype") or "")]
                metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
                append_process_block({
                    "type": block_type,
                    "id": f"{block_type}:{payload['id']}",
                    "content": payload.get("content") or "",
                    "streaming": False,
                }, str(metadata.get("run_id") or "") or None)
            elif kind == "stream_block":
                append_process_block(payload, stream_run_id)
            else:
                call = payload
                append_tool_call(call)
        append_process_item()
        stream_usage = (stream or {}).get("usage_info")
        if (
            stream and raw_status in {"stopping", "stopped", "error"}
        ) or (
            raw_status == "error" and error_message
        ) or (
            stream and stream_usage and raw_status == "running"
        ):
            items.append(self._run_status_item(
                conversation_id,
                node_id,
                stream_run_id or self._latest_run_id(runs),
                raw_status,
                error_message,
                stream_usage,
            ))
        for notification in task_notifications:
            items.append(self._task_notification_item(conversation_id, notification))
        live_assistant_message_id = str((stream or {}).get("assistant_message_id") or "")
        has_canonical_answer = any(
            self._is_visible_assistant_answer(message)
            and (
                not live_assistant_message_id
                or str(message.get("id") or "") == live_assistant_message_id
            )
            for message in messages
        )
        if (
            stream
            and raw_status in {"running", "finalizing", "complete", "stopped", "error"}
            and live_assistant_message_id
            and str(stream.get("answer") or "").strip()
            and not has_canonical_answer
        ):
            items.append({
                "type": "assistant_answer",
                "id": f"message:{live_assistant_message_id}",
                "conversation_id": conversation_id,
                "node_id": node_id,
                "message_id": live_assistant_message_id,
                "content": stream.get("answer") or "",
                "status": self._assistant_answer_status(raw_status),
                "created_at": None,
            })
        visible_answers = [
            message
            for message in messages
            if self._is_visible_assistant_answer(message)
        ]
        continued_content = "".join(
            str(message.get("content") or "")
            for message in messages
            if message.get("role") == "assistant"
            and message.get("subtype") == "assistant_continuation"
        )
        for message in visible_answers:
            item = self._assistant_answer_item(conversation_id, message, run_by_id)
            if message is visible_answers[-1] and continued_content:
                item["content"] = continued_content + str(item.get("content") or "")
            items.append(item)
        return items

    def _merge_live_tool_calls(
        self,
        stored_calls: list[dict[str, Any]],
        live_calls: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        by_id = {str(call.get("id") or ""): call for call in stored_calls if call.get("id")}
        merged = list(stored_calls)
        for call in live_calls:
            call_id = str(call.get("id") or "")
            if not call_id:
                continue
            if call_id in by_id:
                index = next((idx for idx, item in enumerate(merged) if str(item.get("id") or "") == call_id), -1)
                if index >= 0:
                    merged[index] = {**call, **merged[index]}
                continue
            merged.append(call)
        return merged

    def _participation_kind(self, call: dict[str, Any], result: dict[str, Any] | None) -> str:
        name = str(call.get("name") or "")
        if name in PLAN_QUESTION_TOOLS:
            result_payload = self._tool_result_payload(result)
            if not result_payload or result_payload.get("error"):
                return "process_tool"
            return "plan_question"
        if name in PLAN_APPROVAL_TOOLS:
            result_payload = self._tool_result_payload(result)
            if not result_payload or result_payload.get("error"):
                return "process_tool"
            return "plan_approval"
        if self._is_tool_approval_call(call, result):
            return "tool_approval"
        return "process_tool"

    def _load_branch(self, conn: Any, conversation_id: str, tip_node_id: str) -> list[Any]:
        return conn.execute(
            """
            WITH RECURSIVE branch(id, parent_id, depth, child_order, tool_permission_mode, task_context_mode, created_at, updated_at) AS (
              SELECT id, parent_id, depth, child_order, tool_permission_mode, task_context_mode, created_at, updated_at
              FROM nodes
              WHERE conversation_id = ? AND id = ?

              UNION ALL

              SELECT parent.id, parent.parent_id, parent.depth, parent.child_order, parent.tool_permission_mode, parent.task_context_mode, parent.created_at, parent.updated_at
              FROM nodes AS parent
              JOIN branch ON branch.parent_id = parent.id
              WHERE parent.conversation_id = ?
            )
            SELECT *
            FROM branch
            ORDER BY depth, child_order, created_at, id
            """,
            (conversation_id, tip_node_id, conversation_id),
        ).fetchall()

    def _load_messages(self, conn: Any, conversation_id: str, node_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
        if not node_ids:
            return {}
        rows = conn.execute(
            f"""
            SELECT *, rowid AS _rowid
            FROM messages
            WHERE conversation_id = ?
              AND node_id IN ({self._placeholders(node_ids)})
            ORDER BY created_at, rowid
            """,
            (conversation_id, *node_ids),
        ).fetchall()
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            message = dict(row)
            message["content"] = self._message_content(message)
            message["metadata"] = self._load_json(message.get("metadata_json")) or {}
            grouped[str(message.get("node_id"))].append(message)
        return grouped

    def _load_tool_calls(self, conn: Any, conversation_id: str, node_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
        if not node_ids:
            return {}
        rows = conn.execute(
            f"""
            SELECT *, rowid AS _rowid
            FROM tool_calls
            WHERE conversation_id = ?
              AND node_id IN ({self._placeholders(node_ids)})
            ORDER BY node_id, call_index, created_at, id
            """,
            (conversation_id, *node_ids),
        ).fetchall()
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            call = dict(row)
            if not call.get("args_preview") and call.get("args_inline"):
                call["args_preview"] = str(call["args_inline"])[:4096]
            grouped[str(call.get("node_id"))].append(call)
        return grouped

    def _load_tool_results(self, conn: Any, conversation_id: str, node_ids: list[str]) -> list[dict[str, Any]]:
        if not node_ids:
            return []
        rows = conn.execute(
            f"""
            SELECT *
            FROM tool_results
            WHERE conversation_id = ?
              AND node_id IN ({self._placeholders(node_ids)})
            ORDER BY created_at, id
            """,
            (conversation_id, *node_ids),
        ).fetchall()
        results = []
        for row in rows:
            item = dict(row)
            preview = str(item.get("output_preview") or "")
            if (
                item.get("output_blob_id")
                and preview.lstrip().startswith(("{", "["))
                and self._load_json(preview) is None
            ):
                try:
                    output = self.blobs.get_text(str(item["output_blob_id"]))
                except KeyError:
                    pass
                else:
                    item["output_preview"] = tool_result_preview(output)
            item["metadata"] = self._load_json(item.get("metadata_json")) or {}
            results.append(item)
        return results

    def _load_plans(self, conn: Any, conversation_id: str) -> list[dict[str, Any]]:
        rows = conn.execute(
            """
            SELECT *
            FROM plans
            WHERE conversation_id = ?
            ORDER BY updated_at, id
            """,
            (conversation_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def _load_runs(self, conn: Any, conversation_id: str, node_ids: list[str]) -> list[dict[str, Any]]:
        if not node_ids:
            return []
        rows = conn.execute(
            f"""
            SELECT *
            FROM runs
            WHERE conversation_id = ?
              AND (
                    target_node_id IN ({self._placeholders(node_ids)})
                 OR anchor_node_id IN ({self._placeholders(node_ids)})
              )
            ORDER BY created_at, id
            """,
            (conversation_id, *node_ids, *node_ids),
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["metadata"] = self._load_json(item.get("metadata_json")) or {}
            result.append(item)
        return result

    def _load_task_notifications(self, conn: Any, conversation_id: str, node_ids: list[str]) -> list[dict[str, Any]]:
        if not node_ids:
            return []
        rows = conn.execute(
            f"""
            SELECT
              task_notifications.*,
              runs.anchor_node_id,
              runs.target_node_id,
              COALESCE(NULLIF(task_notifications.source_run_kind, ''), runs.kind, '') AS resolved_source_run_kind
            FROM task_notifications
            LEFT JOIN runs
              ON runs.conversation_id = task_notifications.conversation_id
             AND runs.id = task_notifications.source_run_id
            WHERE task_notifications.conversation_id = ?
              AND (
                    task_notifications.delivery_node_id IN ({self._placeholders(node_ids)})
                 OR runs.anchor_node_id IN ({self._placeholders(node_ids)})
                 OR runs.target_node_id IN ({self._placeholders(node_ids)})
              )
            ORDER BY task_notifications.created_at, task_notifications.id
            """,
            (conversation_id, *node_ids, *node_ids, *node_ids),
        ).fetchall()
        return [dict(row) for row in rows]

    def _message_content(self, message: dict[str, Any]) -> str:
        if message.get("content_inline") is not None:
            return str(message.get("content_inline") or "")
        blob_id = message.get("content_blob_id")
        return self.blobs.get_text(str(blob_id)) if blob_id else ""

    def _user_item(self, conversation_id: str, node: Any, message: dict[str, Any]) -> dict[str, Any]:
        metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
        parent_node_id = node["parent_id"]
        if parent_node_id == "None":
            parent_node_id = None
        return {
            "type": "user_message",
            "id": f"message:{message['id']}",
            "conversation_id": conversation_id,
            "node_id": message.get("node_id"),
            "parent_node_id": parent_node_id,
            "message_id": message.get("id"),
            "content": message.get("content") or "",
            "import_files": metadata.get("import_files") or [],
            "image_refs": metadata.get("image_refs") or [],
            "tool_permission_mode": node["tool_permission_mode"],
            "task_context_mode": node["task_context_mode"] or "attached",
            "created_at": message.get("created_at"),
        }

    def _assistant_answer_item(
        self,
        conversation_id: str,
        message: dict[str, Any],
        run_by_id: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
        generation = metadata.get("generation_info") if isinstance(metadata.get("generation_info"), dict) else {}
        return {
            "type": "assistant_answer",
            "id": f"message:{message['id']}",
            "conversation_id": conversation_id,
            "node_id": message.get("node_id"),
            "message_id": message.get("id"),
            "content": message.get("content") or "",
            "status": self._message_answer_status(message, run_by_id),
            "finish_reason": generation.get("finish_reason"),
            "created_at": message.get("created_at"),
        }

    def _tool_block(self, call: dict[str, Any], result: dict[str, Any] | None) -> dict[str, Any]:
        status = "running"
        if result is not None:
            payload = self._load_json(result.get("output_preview") or "")
            status = (
                "error"
                if result.get("status") == "error" or (isinstance(payload, dict) and payload.get("error"))
                else "complete"
            )
        elif call.get("status") in {"complete", "done"}:
            status = "complete"
        block: dict[str, Any] = {
            "type": "tool_call",
            "id": f"tool-call:{call['id']}",
            "tool_call_id": call.get("id"),
            "tool_name": call.get("name") or "",
            "args_preview": call.get("args_preview") or "",
            "result_preview": result.get("output_preview") if result else None,
            "status": status,
        }
        if result and (result.get("metadata") or {}).get("diff_before"):
            block["tool_result_id"] = result["id"]
        return block

    def _is_tool_approval_call(self, call: dict[str, Any], result: dict[str, Any] | None) -> bool:
        if str(call.get("status") or "") in {"waiting_approval", "approved", "rejected"}:
            return True
        output = result.get("output_preview") if result else None
        payload = self._load_json(output)
        if not isinstance(payload, dict):
            return False
        return str(payload.get("approval_status") or "") in {"awaiting_approval", "approved", "rejected"}

    def _tool_approval_item(
        self,
        conversation_id: str,
        call: dict[str, Any],
        result: dict[str, Any] | None,
    ) -> dict[str, Any]:
        result_payload = self._tool_result_payload(result)
        status = str(result_payload.get("approval_status") or call.get("status") or result_payload.get("status") or "")
        if status == "waiting_approval":
            status = "awaiting_approval"
        if status in {"approve", "approved"}:
            status = "approved"
        elif status in {"reject", "rejected", "denied"}:
            status = "rejected"
        if status not in {"awaiting_approval", "approved", "rejected"}:
            status = "rejected" if result is not None else "awaiting_approval"
        return {
            "type": "tool_approval",
            "id": f"tool-approval:{call['id']}",
            "conversation_id": conversation_id,
            "node_id": call.get("node_id"),
            "run_id": call.get("run_id"),
            "tool_call_id": call.get("id"),
            "tool_name": call.get("name") or "",
            "args_preview": call.get("args_preview") or "",
            "result_preview": result.get("output_preview") if result else None,
            "status": status,
            "created_at": call.get("created_at"),
        }

    def _run_status_item(
        self,
        conversation_id: str,
        node_id: str,
        run_id: str | None,
        status: str,
        message: str | None,
        usage: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        item = {
            "type": "run_status",
            "id": f"run-status:{run_id or node_id}",
            "conversation_id": conversation_id,
            "node_id": node_id,
            "run_id": run_id,
            "status": status,
        }
        if message:
            item["message"] = message
        if usage:
            item["usage"] = usage
        return item

    def _plan_question_item(
        self,
        conversation_id: str,
        call: dict[str, Any],
        result: dict[str, Any] | None,
        plan: dict[str, Any] | None,
    ) -> dict[str, Any]:
        args = self._tool_args(call)
        result_payload = self._tool_result_payload(result)
        answer = result_payload.get("answer") if isinstance(result_payload, dict) else None
        if answer is None and isinstance(result_payload, dict) and isinstance(result_payload.get("question"), dict):
            answer = result_payload["question"].get("answer")
        question = args.get("question")
        if isinstance(question, dict):
            question_text = str(question.get("question") or "")
            options = question.get("options") if isinstance(question.get("options"), list) else []
        else:
            question_text = str(question or args.get("prompt") or "")
            options = args.get("options") if isinstance(args.get("options"), list) else []
        return {
            "type": "plan_question",
            "id": f"plan-question:{call['id']}",
            "conversation_id": conversation_id,
            "node_id": call.get("node_id"),
            "run_id": call.get("run_id"),
            "plan_id": (plan or {}).get("id") or args.get("plan_id") or result_payload.get("plan_id") or "",
            "tool_call_id": call.get("id"),
            "status": "answered" if answer is not None else "awaiting_answer",
            "question": question_text,
            "options": options,
            "answer": str(answer) if answer is not None else None,
            "created_at": call.get("created_at"),
        }

    def _plan_approval_item(
        self,
        conversation_id: str,
        call: dict[str, Any],
        result: dict[str, Any] | None,
        plan: dict[str, Any] | None,
    ) -> dict[str, Any]:
        args = self._tool_args(call)
        result_payload = self._tool_result_payload(result)
        status = str(result_payload.get("status") or "")
        if status in {"approve", "approved"}:
            status = "approved"
        elif status in {"reject", "rejected", "denied"}:
            status = "rejected"
        elif status != "awaiting_approval":
            status = "awaiting_approval" if result is None else "approved"
        return {
            "type": "plan_approval",
            "id": f"plan-approval:{call['id']}",
            "conversation_id": conversation_id,
            "node_id": call.get("node_id"),
            "run_id": call.get("run_id"),
            "plan_id": (plan or {}).get("id") or args.get("plan_id") or result_payload.get("plan_id") or "",
            "tool_call_id": call.get("id"),
            "status": status,
            "plan": str(args.get("plan") or ""),
            "feedback": (
                str(result_payload.get("feedback"))
                if result_payload.get("feedback") is not None
                else None
            ),
            "created_at": call.get("created_at"),
        }

    def _task_notification_item(
        self,
        conversation_id: str,
        notification: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "type": "task_notification",
            "id": f"task-notification:{notification['id']}",
            "conversation_id": conversation_id,
            "node_id": notification.get("delivery_node_id") or notification.get("anchor_node_id") or notification.get("target_node_id"),
            "notification_id": notification.get("id"),
            "source_run_id": notification.get("source_run_id"),
            "source_run_kind": notification.get("resolved_source_run_kind") or notification.get("source_run_kind") or "",
            "status": notification.get("status") or "unbound",
            "summary": notification.get("summary") or "",
            "content": notification.get("content") or "",
            "delivery_run_id": notification.get("delivery_run_id"),
            "created_at": notification.get("created_at"),
        }

    def _compact_item(self, conversation_id: str, node: Any, messages: list[dict[str, Any]]) -> dict[str, Any] | None:
        boundary = next(
            (message for message in messages if message.get("role") == "system" and message.get("subtype") == "compact_boundary"),
            None,
        )
        if boundary is None:
            return None
        summary = next(
            (message for message in messages if message.get("role") == "assistant" and message.get("subtype") == "compact_summary"),
            None,
        )
        metadata = boundary.get("metadata") if isinstance(boundary.get("metadata"), dict) else {}
        return {
            "type": "compact",
            "id": f"compact:{node['id']}",
            "conversation_id": conversation_id,
            "node_id": node["id"],
            "summary_message_id": summary.get("id") if summary else None,
            "content": (summary or boundary).get("content") or "",
            "trigger": metadata.get("trigger") or "manual",
            "pre_tokens": metadata.get("pre_tokens"),
            "messages_to_keep": metadata.get("messages_to_keep"),
            "created_at": node["created_at"],
        }

    def _is_visible_assistant_answer(self, message: dict[str, Any]) -> bool:
        if message.get("role") != "assistant" or message.get("hidden"):
            return False
        if not str(message.get("content") or "").strip():
            return False
        subtype = message.get("subtype")
        return subtype in {None, "", "assistant_answer"}

    def _tool_args(self, call: dict[str, Any]) -> dict[str, Any]:
        raw = call.get("args_inline")
        if raw is None and call.get("args_blob_id"):
            raw = self.blobs.get_text(str(call["args_blob_id"]))
        payload = self._load_json(raw)
        return payload if isinstance(payload, dict) else {}

    def _tool_result_payload(self, result: dict[str, Any] | None) -> dict[str, Any]:
        if not result:
            return {}
        output = result.get("output_preview")
        if result.get("output_blob_id"):
            output = self.blobs.get_text(str(result["output_blob_id"]))
        payload = self._load_json(output)
        return payload if isinstance(payload, dict) else {"content": output or ""}

    def _latest_run_id(self, runs: list[dict[str, Any]]) -> str | None:
        if not runs:
            return None
        return str(runs[-1].get("id") or runs[-1].get("run_id") or "") or None

    def _run_status(self, runs: list[dict[str, Any]]) -> str:
        if not runs:
            return "complete"
        status = str(runs[-1].get("status") or "complete")
        if status == "failed":
            return "error"
        if status in {"completed", "complete"}:
            return "complete"
        if status in {"cancelled", "interrupted"}:
            return "stopped"
        return status

    def _process_status_for(
        self,
        run_id: str | None,
        run_by_id: dict[str, dict[str, Any]],
        stream_status: str,
        stream_run_id: str | None,
    ) -> str:
        if run_id and run_id == stream_run_id:
            return self._assistant_process_status(stream_status)
        if run_id and run_id in run_by_id:
            return self._assistant_process_status(self._run_status([run_by_id[run_id]]))
        return "complete"

    def _assistant_process_status(self, status: str) -> str:
        if status in {"running", "complete", "stopped", "error"}:
            return status
        if status in {"completed"}:
            return "complete"
        if status in {"failed"}:
            return "error"
        if status in {'stopping', "cancelled", "interrupted"}:
            return "stopped"
        return "complete"

    def _assistant_answer_status(self, status: str) -> str:
        status = self._assistant_process_status(status)
        return status if status in {"complete", "stopped", "error"} else "complete"

    def _message_answer_status(
        self,
        message: dict[str, Any],
        run_by_id: dict[str, dict[str, Any]],
    ) -> str:
        metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
        run_id = str(metadata.get("run_id") or "") or None
        if run_id and run_id in run_by_id:
            return self._assistant_answer_status(self._run_status([run_by_id[run_id]]))
        generation = metadata.get("generation_info") if isinstance(metadata.get("generation_info"), dict) else {}
        return self._assistant_answer_status(str(generation.get("status") or "complete"))

    def _run_duration_ms(self, runs: list[dict[str, Any]]) -> int | None:
        if not runs:
            return None
        run = runs[-1]
        start = run.get("created_at")
        end = run.get("finished_at")
        if isinstance(start, (int, float)) and isinstance(end, (int, float)):
            return max(0, int((end - start) * 1000))
        return None

    def _message_sort_key(self, message: dict[str, Any], run_time_by_id: dict[str, float]) -> tuple[float, float, float, int]:
        metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
        run_id = str(metadata.get("run_id") or "")
        if metadata.get("order") is not None:
            return (
                run_time_by_id.get(run_id, 0.0) if run_id else 0.0,
                self._order_number(metadata.get("order"), 0.0),
                0.0,
                int(self._order_number(message.get("_rowid"), 0.0)),
            )
        return (
            self._order_number(message.get("created_at"), 0.0),
            run_time_by_id.get(run_id, 1_000_000.0),
            0.0,
            int(self._order_number(message.get("_rowid"), 0.0)),
        )

    def _tool_call_sort_key(
        self,
        call: dict[str, Any],
        run_time_by_id: dict[str, float],
        ordered_process_run_ids: set[str],
    ) -> tuple[float, float, float, int]:
        run_id = str(call.get("run_id") or "")
        if run_id in ordered_process_run_ids:
            return (
                run_time_by_id.get(run_id, 0.0) if run_id else 0.0,
                self._order_number(call.get("call_index"), 0.0),
                1.0,
                0,
            )
        return (
            self._order_number(call.get("created_at"), 0.0),
            run_time_by_id.get(run_id, 1_000_000.0),
            1.0,
            int(self._order_number(call.get("call_index"), 0.0)),
        )

    @staticmethod
    def _placeholders(values: list[str]) -> str:
        return ", ".join("?" for _ in values)

    @staticmethod
    def _load_json(value: Any) -> Any:
        if value is None or value == "":
            return None
        if isinstance(value, (dict, list)):
            return value
        try:
            return json.loads(str(value))
        except (TypeError, json.JSONDecodeError):
            return None

    @staticmethod
    def _order_number(value: Any, default: float) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default


class TranscriptPatchSession:
    def __init__(
        self,
        assembler: TranscriptAssembler,
        run_id: str,
        revision_floor: int | None = None,
    ) -> None:
        self.assembler = assembler
        self.run_id = run_id
        self.revision_floor = int(revision_floor or 0)
        self.conversation_id: str | None = None
        self.node_id: str | None = None
        self.events: list[dict[str, Any]] = []

    def feed(self, event: dict[str, Any], *, emit: bool = True) -> dict[str, Any] | None:
        payload = dict(event.get("payload") if isinstance(event.get("payload"), dict) else event)
        self.events.append(payload)
        self.conversation_id = str(payload.get("conversation_id") or self.conversation_id or "")
        self.node_id = str(payload.get("target_node_id") or payload.get("node_id") or self.node_id or "")
        if not emit or not self.conversation_id or not self.node_id:
            return None
        stream = self._stream_state()
        stream["node_id"] = self.node_id
        stream["target_node_id"] = self.node_id
        items = self.assembler.live_node_items(self.conversation_id, self.node_id, stream)
        snapshot_items = self.assembler.snapshot(
            self.conversation_id,
            self.node_id,
            active_streams=[stream],
        )["items"]
        index_by_id = {
            str(item.get("id") or ""): index
            for index, item in enumerate(snapshot_items)
            if item.get("id")
        }
        # snapshot 中缺失项的插入位置：放在同节点已知项之前，避免用户消息追加到流式内容末尾
        fallback_index = next(
            (
                index
                for index, candidate in enumerate(snapshot_items)
                if str(candidate.get("node_id") or "") == str(self.node_id)
            ),
            len(snapshot_items),
        )
        current_ids = {str(item.get("id") or "") for item in items if item.get("id")}
        emitted = self.assembler._emitted_by_node.setdefault((self.conversation_id, self.node_id), {})
        remove_ids = set(emitted) - current_ids
        operations = [{"op": "remove", "id": item_id} for item_id in sorted(remove_ids)]
        fallback_offset = 0
        for item in items:
            item_id = str(item.get("id") or "")
            if not item_id:
                continue
            serialized = json.dumps(item, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
            if emitted.get(item_id) != serialized:
                index = index_by_id.get(item_id)
                if index is None:
                    index = fallback_index + fallback_offset
                    fallback_offset += 1
                operations.append({
                    "op": "upsert",
                    "item": item,
                    "index": index,
                })
        emitted.clear()
        emitted.update({
            str(item.get("id") or ""): json.dumps(item, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
            for item in items
            if item.get("id")
        })
        is_terminal_event = (
            payload.get("type") == "run_finished"
            or str(payload.get("status") or "") in {"complete", "completed", "error", "failed", "stopped", "cancelled", "interrupted"}
        )
        if not operations and not is_terminal_event:
            return None
        revision = self.assembler.next_revision(
            self.conversation_id,
            self.node_id,
            self.revision_floor,
        )
        return {
            "type": "transcript_patch",
            "conversation_id": self.conversation_id,
            "node_id": self.node_id,
            "revision": revision,
            "operations": operations,
        }

    def _stream_state(self) -> dict[str, Any]:
        status = "running"
        assistant_message_id: str | None = None
        pending_content = ""
        answer = ""
        entries: list[dict[str, Any]] = []
        reasoning = ""
        error_message = ""
        tool_calls: list[dict[str, Any]] = []
        result_overrides_by_call_id: dict[str, dict[str, Any]] = {}
        usage_info: dict[str, Any] | None = None

        def upsert_tool_call(call: dict[str, Any]) -> None:
            for existing in tool_calls:
                if existing.get("id") == call.get("id"):
                    existing.update({key: value for key, value in call.items() if value not in (None, "")})
                    return
            call["call_index"] = len(entries)
            tool_calls.append(call)
            entries.append({"type": "tool_call", "call": call})

        def flush_reasoning(streaming: bool) -> None:
            nonlocal reasoning
            if not reasoning:
                return
            block = {
                "type": "reasoning",
                "id": f"reasoning:{self.run_id}:{len(entries)}",
                "content": reasoning,
                "streaming": streaming,
            }
            entries.append({"type": "block", "block": block})
            reasoning = ""

        def flush_process_content(streaming: bool) -> None:
            nonlocal pending_content
            if not pending_content:
                return
            entries.append({"type": "block", "block": {
                "type": "content",
                "id": f"content:{self.run_id}:{len(entries)}",
                "content": pending_content,
                "streaming": streaming,
            }})
            pending_content = ""

        for event in self.events:
            event_type = str(event.get("event_type") or event.get("type") or "")
            event_status = str(event.get("status") or "")
            if event.get("assistant_message_id"):
                assistant_message_id = str(event.get("assistant_message_id"))
            if event.get("type") == "run_finished" and event_status in {"complete", "completed"}:
                status = "complete"
            elif event_status in {"complete", "completed"}:
                status = "finalizing"
            elif event_status in {"error", "failed"}:
                status = "error"
            elif event_status in {"stopped", "cancelled", "interrupted"}:
                status = "stopped"
            elif event_status == 'stopping':
                status = 'stopping'
            if event.get("error"):
                error_message = str(event["error"])
            if isinstance(event.get("usage_info"), dict):
                usage_info = event["usage_info"]

            if isinstance(event.get("reasoning"), str):
                reasoning += str(event["reasoning"])
            if event_type == "reasoning" and isinstance(event.get("content"), str):
                reasoning += str(event["content"])
            content = event.get("content")
            if isinstance(content, str) and content and event_type not in {"reasoning", "process_content"}:
                pending_content += content
            if event_type == "process_content" and isinstance(content, str) and content:
                flush_reasoning(status == "running")
                pending_content += content
            if event_type in {"tool_calls_committed", "tool_call_start", "tool_call"}:
                flush_reasoning(status == "running")
                flush_process_content(status == "running")
                for call in self._event_tool_calls(event):
                    upsert_tool_call(call)
            if event_type in {"tool_approval_request", "tool_approval_result"}:
                approval = event.get("approval") if isinstance(event.get("approval"), dict) else {}
                tool_call_id = str(approval.get("tool_call_id") or "")
                if tool_call_id:
                    flush_reasoning(status == "running")
                    flush_process_content(status == "running")
                    approval_status = str(approval.get("status") or "")
                    call_status = "waiting_approval"
                    if event_type == "tool_approval_result":
                        call_status = "approved" if approval_status == "approved" else "rejected"
                        result_overrides_by_call_id[tool_call_id] = {
                            "id": f"approval-result:{tool_call_id}",
                            "tool_call_id": tool_call_id,
                            "output_preview": json.dumps(
                                {"approval_status": call_status},
                                ensure_ascii=False,
                            ),
                            "status": "complete",
                            "metadata": {},
                        }
                    upsert_tool_call({
                        "id": tool_call_id,
                        "conversation_id": self.conversation_id,
                        "node_id": self.node_id,
                        "run_id": self.run_id,
                        "name": str(approval.get("tool_name") or ""),
                        "args_inline": "",
                        "args_preview": str(approval.get("arguments_preview") or ""),
                        "call_index": len(tool_calls),
                        "status": call_status,
                        "created_at": approval.get("created_at"),
                    })
            if event_type == "tool_call_error":
                result = self._event_tool_error(event)
                if result:
                    result_overrides_by_call_id[str(result["tool_call_id"])] = result

        flush_reasoning(status == "running")
        flush_process_content(status == "running")
        if status != "running" and entries:
            last = entries[-1]
            if isinstance(last, dict) and last.get("type") == "block":
                block = last.get("block")
                if isinstance(block, dict) and block.get("type") == "content":
                    entries.pop()
                    answer = str(block.get("content") or "")
        if status != "running":
            for entry in entries:
                block = entry.get("block") if isinstance(entry, dict) else None
                if isinstance(block, dict):
                    block["streaming"] = False
        return {
            "run_id": self.run_id,
            "status": status,
            "assistant_message_id": assistant_message_id,
            "entries": entries,
            "tool_calls": tool_calls,
            "result_overrides_by_call_id": result_overrides_by_call_id,
            "answer": answer,
            "error_message": error_message or None,
            "usage_info": usage_info,
        }

    def _event_tool_calls(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        raw_calls = event.get("tool_calls")
        if not isinstance(raw_calls, list):
            raw_call = event.get("tool_call")
            raw_calls = [raw_call] if isinstance(raw_call, dict) else []
        calls: list[dict[str, Any]] = []
        for index, call in enumerate(raw_calls):
            if not isinstance(call, dict):
                continue
            function = call.get("function") if isinstance(call.get("function"), dict) else {}
            call_id = str(call.get("id") or call.get("tool_call_id") or "")
            if not call_id:
                continue
            calls.append({
                "id": call_id,
                "conversation_id": self.conversation_id,
                "node_id": self.node_id,
                "run_id": self.run_id,
                "name": str(call.get("name") or function.get("name") or ""),
                "args_inline": function.get("arguments") or call.get("arguments") or "",
                "args_preview": str(function.get("arguments") or call.get("arguments") or "")[:4096],
                "call_index": call.get("call_index") if call.get("call_index") is not None else index,
                "status": "running",
                "created_at": None,
            })
        return calls

    def _event_tool_error(self, event: dict[str, Any]) -> dict[str, Any] | None:
        call = event.get("tool_call")
        if not isinstance(call, dict):
            return None
        call_id = str(call.get("tool_call_id") or call.get("id") or "")
        if not call_id:
            return None
        return {
            "id": f"error:{call_id}",
            "tool_call_id": call_id,
            "output_preview": str(call.get("error") or "")[:4096],
            "status": "error",
            "metadata": {},
        }
