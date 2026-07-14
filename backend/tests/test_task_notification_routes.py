from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.dependencies import get_task_notification_service, get_task_service
from backend.api.routes import notifications as notifications_route
from backend.api.routes import tasks as tasks_route


class FakeTaskNotificationService:
    def __init__(self) -> None:
        self.items = [
            {
                "id": "notification-1",
                "conversation_id": "conv-1",
                "source_run_id": "run-1",
                "source_run_kind": "agent",
                "status": "unbound",
                "summary": "完成",
                "content": "done",
                "created_at": 1,
                "updated_at": 1,
            }
        ]

    def list_for_conversation(self, conversation_id: str):
        return [
            item
            for item in self.items
            if item["conversation_id"] == conversation_id
            and item["status"] in {"unbound", "bound", "delivering", "delivery_failed", "delivery_cancelled"}
        ]

    async def bind(self, *, notification_id: str, delivery_node_id: str, trigger: bool):
        for item in self.items:
            if item["id"] != notification_id:
                continue
            item["status"] = "delivering" if trigger else "bound"
            item["delivery_node_id"] = delivery_node_id
            item["delivered_run_id"] = "run-delivery" if trigger else None
            item["updated_at"] = 2
            return item
        raise KeyError(notification_id)

    async def delete(self, notification_id: str):
        for item in self.items:
            if item["id"] != notification_id:
                continue
            item["status"] = "deleted"
            item["updated_at"] = 3
            return item
        raise KeyError(notification_id)


class FakeTaskService:
    async def get_active_task(self, conversation_id: str):
        return None


def client_for(service: FakeTaskNotificationService) -> TestClient:
    app = FastAPI()
    app.include_router(tasks_route.router)
    app.include_router(notifications_route.router)
    app.dependency_overrides[get_task_notification_service] = lambda: service
    app.dependency_overrides[get_task_service] = lambda: FakeTaskService()
    return TestClient(app)


def test_task_state_route_returns_304_for_matching_etag():
    client = client_for(FakeTaskNotificationService())

    first = client.get("/conversations/conv-1/task-state")

    assert first.status_code == 200
    assert first.json()["task"] is None
    assert first.json()["notifications"][0]["id"] == "notification-1"
    assert first.json()["flags"]["needsFollowup"] is True
    assert first.headers["etag"]

    unchanged = client.get(
        "/conversations/conv-1/task-state",
        headers={"If-None-Match": first.headers["etag"]},
    )

    assert unchanged.status_code == 304


def test_task_notification_mutations_return_task_state_snapshot():
    service = FakeTaskNotificationService()
    client = client_for(service)

    bound = client.post(
        "/task-notifications/notification-1/bind",
        json={"delivery_node_id": "node-1", "trigger": True},
    )

    assert bound.status_code == 200
    assert bound.json()["conversation_id"] == "conv-1"
    assert bound.json()["notifications"][0]["status"] == "delivering"
    assert bound.json()["flags"]["delivering"] is True
    assert bound.headers["etag"]

    deleted = client.post("/task-notifications/notification-1/delete", json={})

    assert deleted.status_code == 200
    assert deleted.json()["notifications"] == []
    assert deleted.json()["flags"]["needsFollowup"] is False
