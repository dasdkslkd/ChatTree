from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.dependencies import get_task_notification_service
from backend.api.routes import notifications as notifications_route


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
        return [item for item in self.items if item["conversation_id"] == conversation_id]


def client_for(service: FakeTaskNotificationService) -> TestClient:
    app = FastAPI()
    app.include_router(notifications_route.router)
    app.dependency_overrides[get_task_notification_service] = lambda: service
    return TestClient(app)


def test_task_notifications_route_returns_304_for_matching_etag():
    client = client_for(FakeTaskNotificationService())

    first = client.get("/conversations/conv-1/task-notifications")

    assert first.status_code == 200
    assert first.json()[0]["id"] == "notification-1"
    assert first.headers["etag"]

    unchanged = client.get(
        "/conversations/conv-1/task-notifications",
        headers={"If-None-Match": first.headers["etag"]},
    )

    assert unchanged.status_code == 304
