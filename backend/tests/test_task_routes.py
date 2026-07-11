from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.dependencies import get_task_service
from backend.api.routes import tasks as tasks_route
from backend.core.tasks import ActiveTaskService


def client_for(service: ActiveTaskService) -> TestClient:
    app = FastAPI()
    app.include_router(tasks_route.router)
    app.dependency_overrides[get_task_service] = lambda: service
    return TestClient(app)


def test_task_routes_manage_single_active_task_until_completion():
    client = client_for(ActiveTaskService())

    created = client.post(
        "/conversations/conv-1/task",
        json={
            "title": "检查实现",
            "detail": "运行测试",
            "steps": [{"title": "实现"}, {"title": "验证"}],
        },
    )
    assert created.status_code == 200
    version = created.headers["etag"]
    assert created.json()["status"] == "pending"
    assert "task_id" not in created.json()
    assert client.get("/conversations/conv-1/task").json()["title"] == "检查实现"

    duplicate = client.post(
        "/conversations/conv-1/task",
        json={"title": "第二个任务", "steps": [{"title": "不应创建"}]},
    )
    assert duplicate.status_code == 409

    first = client.patch(
        "/conversations/conv-1/task/steps/1",
        json={"status": "completed", "evidence": "实现完成"},
        headers={"If-Match": version},
    )
    assert first.status_code == 200
    assert first.json()["completed"] is False
    version = first.headers["etag"]

    final = client.patch(
        "/conversations/conv-1/task/steps/2",
        json={"status": "completed", "evidence": "测试通过"},
        headers={"If-Match": version},
    )
    assert final.status_code == 200
    assert final.json()["completed"] is True
    assert final.json()["task"] is None
    assert [step["status"] for step in final.json()["task_snapshot"]["steps"]] == [
        "completed",
        "completed",
    ]
    assert client.get("/conversations/conv-1/task").json() is None


def test_task_routes_validate_order_evidence_and_cancel():
    client = client_for(ActiveTaskService())
    missing = client.patch(
        "/conversations/conv-1/task/steps/1",
        json={"status": "blocked", "evidence": "missing"},
    )
    assert missing.status_code == 404

    created = client.post(
        "/conversations/conv-1/task",
        json={"title": "任务", "steps": [{"title": "一"}, {"title": "二"}]},
    )
    version = created.headers["etag"]
    missing_version = client.patch(
        "/conversations/conv-1/task/steps/1",
        json={"status": "completed", "evidence": "missing version"},
    )
    assert missing_version.status_code == 428
    invalid_version = client.patch(
        "/conversations/conv-1/task/steps/1",
        json={"status": "completed", "evidence": "invalid version"},
        headers={"If-Match": '"a"'},
    )
    assert invalid_version.status_code == 400
    out_of_order = client.patch(
        "/conversations/conv-1/task/steps/2",
        json={"status": "completed", "evidence": "too early"},
        headers={"If-Match": version},
    )
    assert out_of_order.status_code == 409

    no_evidence = client.patch(
        "/conversations/conv-1/task/steps/1",
        json={"status": "blocked", "evidence": ""},
        headers={"If-Match": version},
    )
    assert no_evidence.status_code == 400

    cancelled = client.request(
        "DELETE",
        "/conversations/conv-1/task",
        json={"reason": "用户取消"},
        headers={"If-Match": version},
    )
    assert cancelled.status_code == 200
    assert cancelled.json() == {"cancelled": True}
    assert client.get("/conversations/conv-1/task").json() is None


def test_task_routes_reject_stale_version_after_task_replacement():
    client = client_for(ActiveTaskService())
    first = client.post(
        "/conversations/conv-1/task",
        json={"title": "任务 A", "steps": [{"title": "A"}]},
    )
    stale_version = first.headers["etag"]
    cancelled = client.request(
        "DELETE",
        "/conversations/conv-1/task",
        json={"reason": "replace"},
        headers={"If-Match": stale_version},
    )
    assert cancelled.status_code == 200
    replacement = client.post(
        "/conversations/conv-1/task",
        json={"title": "任务 B", "steps": [{"title": "B"}]},
    )

    stale = client.patch(
        "/conversations/conv-1/task/steps/1",
        json={"status": "completed", "evidence": "late A result"},
        headers={"If-Match": stale_version},
    )

    assert stale.status_code == 412
    current = client.get("/conversations/conv-1/task")
    assert current.json()["title"] == "任务 B"
    assert current.json()["steps"][0]["status"] == "pending"
    assert current.headers["etag"] == replacement.headers["etag"]
