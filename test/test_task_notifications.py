import asyncio

from backend.core.chat.conversation import Conversation
from backend.core.chat.node import NodeManager
from backend.core.config.types import Message, Role
from backend.core.notifications.task_notifications import TaskNotificationService
from backend.core.runs import RunKind, RunManager, RunStatus


class MemoryNotificationRepository:
    def __init__(self):
        self.items = {}

    def upsert_for_run(self, *, conversation_id, source_run_id, source_run_kind, task_id=None, summary="", content="", payload=None):
        item = self.items.get(source_run_id) or {
            "id": f"notification-{source_run_id}",
            "conversation_id": conversation_id,
            "source_run_id": source_run_id,
            "source_run_kind": source_run_kind,
            "status": "unbound",
            "delivery_node_id": None,
        }
        item.update({
            "task_id": task_id,
            "summary": summary,
            "content": content,
            "payload": payload or {},
        })
        self.items[source_run_id] = item
        return dict(item)

    def mark_observed_by_source(self, source_run_id):
        item = self.items.get(source_run_id)
        if item:
            item["status"] = "observed"
        return dict(item) if item else None


def test_conversation_add_node_can_avoid_focus_change():
    conversation = Conversation(conversation_id="conv-1")
    conversation.initialize_with_system_message()
    root_id = conversation.current_node_id
    first = NodeManager.create_node(Message({"role": Role.USER, "content": "first"}), parent_id=root_id, model_id="m")
    conversation.add_node(first, parent_id=root_id)
    assert conversation.current_node_id == first["id"]

    notify = NodeManager.create_node(Message({"role": Role.USER, "content": "notify"}), parent_id=root_id, model_id="m")
    conversation.add_node(notify, parent_id=root_id, focus=False)

    assert conversation.current_node_id == first["id"]
    assert notify["id"] in conversation.nodes[root_id]["children_ids"]


def test_observed_run_suppresses_pending_task_notification():
    async def scenario():
        repository = MemoryNotificationRepository()
        run_manager = RunManager()
        service = TaskNotificationService(repository=repository, run_manager=run_manager)
        run_manager.notification_service = service
        source = await run_manager.create_run(
            conversation_id="conv-1",
            kind=RunKind.SUBAGENT,
            anchor_node_id="node-1",
            summary="worker",
        )
        await run_manager.finish_run(source.run_id, RunStatus.COMPLETED)
        await service.publish_run_notification(
            run_id=source.run_id,
            source_status="completed",
            summary="Subagent completed",
            content="done",
            payload={"source_status": "completed"},
        )

        await run_manager.mark_observed(source.run_id, observer_run_id="parent-run", via="wait_agent")
        blocked = await service.publish_run_notification(
            run_id=source.run_id,
            source_status="completed",
            summary="Subagent completed",
            content="done again",
            payload={"source_status": "completed"},
        )

        observed = run_manager.get_run(source.run_id)["metadata"]
        assert blocked is None
        assert observed["result_observed_by_run_id"] == "parent-run"
        assert observed["result_observed_via"] == "wait_agent"
        assert repository.items[source.run_id]["status"] == "observed"

    asyncio.run(scenario())
