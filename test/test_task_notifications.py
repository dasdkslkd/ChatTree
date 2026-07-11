import asyncio

import pytest

from backend.core.chat.conversation import Conversation
from backend.core.chat.node import NodeManager
from backend.core.config.types import Message, Role
from backend.core.notifications.task_notifications import TaskNotificationService
from backend.core.runs import RunKind, RunManager, RunStatus
from backend.core.config.types import StreamChunk, StreamStatus


class MemoryNotificationRepository:
    def __init__(self):
        self.items = {}

    def upsert_for_run(self, *, conversation_id, source_run_id, source_run_kind, summary="", content="", payload=None):
        item = self.items.get(source_run_id) or {
            "id": f"notification-{source_run_id}",
            "conversation_id": conversation_id,
            "source_run_id": source_run_id,
            "source_run_kind": source_run_kind,
            "status": "unbound",
            "delivery_node_id": None,
        }
        item.update({
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

    def get(self, notification_id):
        for item in self.items.values():
            if item["id"] == notification_id:
                return dict(item)
        return None

    def get_by_source_run(self, source_run_id):
        item = self.items.get(source_run_id)
        return dict(item) if item else None

    def bind(self, notification_id, delivery_node_id, *, bound_by):
        for item in self.items.values():
            if item["id"] == notification_id:
                if item.get("status") == "delivering":
                    raise ValueError(f"notification {notification_id} is delivering")
                item["status"] = "bound"
                item["delivery_node_id"] = delivery_node_id
                item["bound_by"] = bound_by
                return dict(item)
        raise KeyError(notification_id)

    def mark_delivering(self, notification_id, delivered_run_id):
        for item in self.items.values():
            if item["id"] == notification_id:
                item["status"] = "delivering"
                item["delivered_run_id"] = delivered_run_id
                return dict(item)
        raise KeyError(notification_id)

    def mark_delivered(self, notification_id, *, delivered_run_id, delivered_node_id):
        for item in self.items.values():
            if item["id"] == notification_id:
                item["status"] = "delivered"
                item["delivered_run_id"] = delivered_run_id
                item["delivered_node_id"] = delivered_node_id
                return dict(item)
        raise KeyError(notification_id)

    def mark_delivery_failed(self, notification_id, *, delivered_run_id, delivered_node_id, error):
        for item in self.items.values():
            if item["id"] == notification_id:
                item["status"] = "delivery_failed"
                item["delivered_run_id"] = delivered_run_id
                item["delivered_node_id"] = delivered_node_id
                item["delivery_error"] = error
                return dict(item)
        raise KeyError(notification_id)

    def mark_delivery_cancelled(self, notification_id, *, delivered_run_id, delivered_node_id):
        for item in self.items.values():
            if item["id"] == notification_id:
                item["status"] = "delivery_cancelled"
                item["delivered_run_id"] = delivered_run_id
                item["delivered_node_id"] = delivered_node_id
                return dict(item)
        raise KeyError(notification_id)

    def list_bound_for_node(self, conversation_id, node_id):
        return [
            dict(item)
            for item in self.items.values()
            if item.get("conversation_id") == conversation_id
            and item.get("delivery_node_id") == node_id
            and item.get("status") == "delivering"
        ]

    def list_pending_publications(self):
        return [
            dict(item)
            for item in self.items.values()
            if item.get("status") in {"unbound", "bound", "delivering"}
        ]


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


def test_bound_notification_focuses_only_when_delivery_node_is_current():
    class FakeChatManager:
        def __init__(self, conversation):
            self.conversation = conversation
            self.calls = []
            self._active_controllers = {}

        def get_conversation(self, conversation_id):
            return self.conversation if conversation_id == self.conversation.metadata["id"] else None

        async def send_message_stream(self, **kwargs):
            self.calls.append(kwargs)
            yield StreamChunk(
                status=StreamStatus.START,
                content="",
                node_id="notify-node",
                conversation_id=kwargs["conversation_id"],
                run_id=kwargs["run_id"],
            )
            yield StreamChunk(
                status=StreamStatus.COMPLETE,
                content="done",
                node_id="notify-node",
                conversation_id=kwargs["conversation_id"],
                run_id=kwargs["run_id"],
            )

    async def scenario():
        conversation = Conversation(conversation_id="conv-1")
        conversation.initialize_with_system_message()
        root_id = conversation.current_node_id
        first = NodeManager.create_node(
            Message({"role": Role.USER, "content": "first"}),
            parent_id=root_id,
            model_id="m",
        )
        conversation.add_node(first, parent_id=root_id)
        second = NodeManager.create_node(
            Message({"role": Role.USER, "content": "second"}),
            parent_id=first["id"],
            model_id="m",
        )
        conversation.add_node(second, parent_id=first["id"])

        repository = MemoryNotificationRepository()
        run_manager = RunManager()
        chat_manager = FakeChatManager(conversation)
        service = TaskNotificationService(
            repository=repository,
            run_manager=run_manager,
            chat_manager=chat_manager,
        )
        source = await run_manager.create_run(
            conversation_id=conversation.metadata["id"],
            kind=RunKind.COMMAND,
            anchor_node_id=first["id"],
            summary="command",
        )
        await run_manager.finish_run(source.run_id, RunStatus.COMPLETED)
        notification = await service.publish_run_notification(
            run_id=source.run_id,
            source_status="completed",
            summary="Command completed",
            content="done",
        )

        await service.bind(
            notification_id=notification["id"],
            delivery_node_id=first["id"],
        )
        await asyncio.sleep(0)
        assert chat_manager.calls[-1]["focus_new_node"] is False

        source2 = await run_manager.create_run(
            conversation_id=conversation.metadata["id"],
            kind=RunKind.COMMAND,
            anchor_node_id=second["id"],
            summary="command",
        )
        await run_manager.finish_run(source2.run_id, RunStatus.COMPLETED)
        notification2 = await service.publish_run_notification(
            run_id=source2.run_id,
            source_status="completed",
            summary="Command completed",
            content="done",
        )

        await service.bind(
            notification_id=notification2["id"],
            delivery_node_id=second["id"],
        )
        await asyncio.sleep(0)
        assert chat_manager.calls[-1]["focus_new_node"] is True

    asyncio.run(scenario())


def test_cancelled_source_run_still_delivers_bound_notification():
    class FakeChatManager:
        def __init__(self):
            self._active_controllers = {}
            self.calls = []

        def get_conversation(self, conversation_id):
            class FakeConversation:
                current_node_id = "node-other"
                nodes = {"node-1": {}, "node-other": {}}
            return FakeConversation()

        async def send_message_stream(self, **kwargs):
            self.calls.append(kwargs)
            yield StreamChunk(
                status=StreamStatus.START,
                content="",
                node_id="notify-node",
                conversation_id=kwargs["conversation_id"],
                run_id=kwargs["run_id"],
            )
            yield StreamChunk(
                status=StreamStatus.COMPLETE,
                content="cancelled source noted",
                node_id="notify-node",
                conversation_id=kwargs["conversation_id"],
                run_id=kwargs["run_id"],
            )

    async def scenario():
        repository = MemoryNotificationRepository()
        run_manager = RunManager()
        chat_manager = FakeChatManager()
        service = TaskNotificationService(
            repository=repository,
            run_manager=run_manager,
            chat_manager=chat_manager,
        )
        run_manager.add_finish_listener(service.handle_run_finished)
        source = await run_manager.create_run(
            conversation_id="conv-1",
            kind=RunKind.COMMAND,
            anchor_node_id="node-1",
            summary="command",
        )
        notification = await service.register_run_notification(
            run_id=source.run_id,
            summary="Command running",
        )
        await service.bind(
            notification_id=notification["id"],
            delivery_node_id="node-1",
            trigger=False,
        )
        await run_manager.finish_run(source.run_id, RunStatus.CANCELLED, "user stop")
        await asyncio.sleep(0)
        assert chat_manager.calls == []

        await service.publish_run_notification(
            run_id=source.run_id,
            source_status="cancelled",
            summary="Command cancelled",
            content="user stop",
        )
        await asyncio.sleep(0)

        assert repository.items[source.run_id]["status"] == "delivered"

    asyncio.run(scenario())


def test_cancelled_notification_delivery_is_not_marked_delivered():
    class FakeChatManager:
        def __init__(self):
            self._active_controllers = {}

        def get_conversation(self, conversation_id):
            class FakeConversation:
                current_node_id = "node-1"
                nodes = {"node-1": {}}
            return FakeConversation()

        async def send_message_stream(self, **kwargs):
            yield StreamChunk(
                status=StreamStatus.START,
                content="",
                node_id="notify-node",
                conversation_id=kwargs["conversation_id"],
                run_id=kwargs["run_id"],
            )
            yield StreamChunk(
                status=StreamStatus.STOPPED,
                content="",
                node_id="notify-node",
                conversation_id=kwargs["conversation_id"],
                run_id=kwargs["run_id"],
            )

    async def scenario():
        repository = MemoryNotificationRepository()
        run_manager = RunManager()
        service = TaskNotificationService(
            repository=repository,
            run_manager=run_manager,
            chat_manager=FakeChatManager(),
        )
        source = await run_manager.create_run(
            conversation_id="conv-1",
            kind=RunKind.COMMAND,
            anchor_node_id="node-1",
            summary="command",
        )
        await run_manager.finish_run(source.run_id, RunStatus.COMPLETED)
        notification = await service.publish_run_notification(
            run_id=source.run_id,
            source_status="completed",
            summary="Command completed",
            content="done",
        )
        await service.bind(
            notification_id=notification["id"],
            delivery_node_id="node-1",
        )
        await asyncio.sleep(0)

        assert repository.items[source.run_id]["status"] == "delivery_cancelled"

    asyncio.run(scenario())


def test_startup_reconciliation_recovers_terminal_notification_content():
    async def scenario():
        repository = MemoryNotificationRepository()
        run_manager = RunManager()
        service = TaskNotificationService(repository=repository, run_manager=run_manager)
        source = await run_manager.create_run(
            conversation_id="conv-1",
            kind=RunKind.COMMAND,
            anchor_node_id="node-1",
            summary="command",
            metadata={
                "command": "echo ok",
                "cwd": ".",
                "task_outcome": {
                    "kind": "run_finished",
                    "task_status": "active",
                    "step": 2,
                    "step_status": "released",
                    "run_status": "interrupted",
                },
            },
        )
        await service.register_run_notification(
            run_id=source.run_id,
            summary="Command running",
        )
        await run_manager.finish_run(source.run_id, RunStatus.INTERRUPTED, "restart")

        await service.reconcile_terminal_publications()

        recovered = repository.items[source.run_id]
        assert recovered["payload"]["source_status"] == "interrupted"
        assert recovered["payload"]["task_outcome"] == {
            "kind": "run_finished",
            "task_status": "active",
            "step": 2,
            "step_status": "released",
            "run_status": "interrupted",
        }
        assert recovered["summary"] == "Command interrupted"
        assert '"status": "interrupted"' in recovered["content"]

    asyncio.run(scenario())


def test_startup_reconciliation_releases_interrupted_delivery():
    async def scenario():
        repository = MemoryNotificationRepository()
        run_manager = RunManager()
        service = TaskNotificationService(repository=repository, run_manager=run_manager)
        source = await run_manager.create_run(
            conversation_id="conv-1",
            kind=RunKind.COMMAND,
            anchor_node_id="node-1",
        )
        await run_manager.finish_run(source.run_id, RunStatus.COMPLETED)
        notification = await service.publish_run_notification(
            run_id=source.run_id,
            source_status="completed",
            summary="Command completed",
            content="done",
        )
        delivery = await run_manager.create_run(
            conversation_id="conv-1",
            kind=RunKind.CHAT,
            anchor_node_id="node-1",
        )
        repository.mark_delivering(notification["id"], delivery.run_id)
        await run_manager.finish_run(delivery.run_id, RunStatus.INTERRUPTED)

        await service.reconcile_terminal_publications()

        assert repository.get(notification["id"])["status"] == "delivery_cancelled"

    asyncio.run(scenario())


def test_delivering_notification_cannot_be_rebound():
    async def scenario():
        repository = MemoryNotificationRepository()
        run_manager = RunManager()
        service = TaskNotificationService(repository=repository, run_manager=run_manager)
        source = await run_manager.create_run(
            conversation_id="conv-1",
            kind=RunKind.COMMAND,
            anchor_node_id="node-1",
        )
        await run_manager.finish_run(source.run_id, RunStatus.COMPLETED)
        notification = await service.publish_run_notification(
            run_id=source.run_id,
            source_status="completed",
            summary="done",
            content="done",
        )
        delivery = await run_manager.create_run(
            conversation_id="conv-1",
            kind=RunKind.CHAT,
            anchor_node_id="node-1",
        )
        repository.mark_delivering(notification["id"], delivery.run_id)

        with pytest.raises(ValueError, match="delivering"):
            await service.bind(
                notification_id=notification["id"],
                delivery_node_id="node-2",
            )

    asyncio.run(scenario())
