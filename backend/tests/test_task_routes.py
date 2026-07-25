from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.dependencies import get_task_service
from backend.api.errors import RequestBoundaryMiddleware, install_error_handlers
from backend.api.routes import tasks as tasks_route
from backend.core.tasks import ActiveTaskService


def client_for(service: ActiveTaskService) -> TestClient:
    app = FastAPI()
    install_error_handlers(app)
    app.add_middleware(RequestBoundaryMiddleware)
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
    current_task = client.get("/conversations/conv-1/task-state")
    assert current_task.json()["task"]["title"] == "检查实现"
    assert current_task.json()["flags"]["running"] is False
    unchanged = client.get("/conversations/conv-1/task-state", headers={"If-None-Match": current_task.headers["etag"]})
    assert unchanged.status_code == 304

    duplicate = client.post(
        "/conversations/conv-1/task",
        json={"title": "第二个任务", "steps": [{"title": "不应创建"}]},
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "conflict"

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
    empty = client.get("/conversations/conv-1/task-state")
    assert empty.json()["task"] is None
    assert empty.json()["flags"]["running"] is False
    assert empty.headers["etag"]
    empty_unchanged = client.get("/conversations/conv-1/task-state", headers={"If-None-Match": empty.headers["etag"]})
    assert empty_unchanged.status_code == 304


def test_task_routes_validate_order_evidence_and_cancel():
    client = client_for(ActiveTaskService())
    missing = client.patch(
        "/conversations/conv-1/task/steps/1",
        json={"status": "blocked", "evidence": "missing"},
    )
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "not_found"

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
    assert missing_version.json()["error"]["code"] == "task_version_required"
    invalid_version = client.patch(
        "/conversations/conv-1/task/steps/1",
        json={"status": "completed", "evidence": "invalid version"},
        headers={"If-Match": '"a"'},
    )
    assert invalid_version.status_code == 400
    assert invalid_version.json()["error"]["code"] == "invalid_request"
    out_of_order = client.patch(
        "/conversations/conv-1/task/steps/2",
        json={"status": "completed", "evidence": "too early"},
        headers={"If-Match": version},
    )
    assert out_of_order.status_code == 409
    assert out_of_order.json()["error"]["code"] == "conflict"

    no_evidence = client.patch(
        "/conversations/conv-1/task/steps/1",
        json={"status": "blocked", "evidence": ""},
        headers={"If-Match": version},
    )
    assert no_evidence.status_code == 400
    assert no_evidence.json()["error"]["code"] == "invalid_request"

    cancelled = client.request(
        "DELETE",
        "/conversations/conv-1/task",
        json={"reason": "用户取消"},
        headers={"If-Match": version},
    )
    assert cancelled.status_code == 200
    assert cancelled.json() == {"cancelled": True}
    assert client.get("/conversations/conv-1/task-state").json()["task"] is None


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
    assert stale.json()["error"]["code"] == "task_version_conflict"
    assert stale.json()["error"]["retryable"] is True
    assert stale.json()["error"]["details"] == {
        "current_version": replacement.headers["etag"],
    }
    current = client.get("/conversations/conv-1/task-state")
    assert current.json()["task"]["title"] == "任务 B"
    assert current.json()["task"]["steps"][0]["status"] == "pending"
    assert current.headers["etag"]
    assert current.headers["etag"] != replacement.headers["etag"]
