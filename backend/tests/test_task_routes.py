from __future__ import annotations

import asyncio

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.dependencies import get_task_ledger
from backend.api.routes import tasks as tasks_route
from backend.core.tasks import TaskLedger, TaskStatus


def run(coro):
    return asyncio.run(coro)


def client_for(ledger: TaskLedger) -> TestClient:
    app = FastAPI()
    app.include_router(tasks_route.router)
    app.dependency_overrides[get_task_ledger] = lambda: ledger
    return TestClient(app)


def test_task_routes_create_list_update_and_filter_finished():
    ledger = TaskLedger()
    client = client_for(ledger)

    created = client.post(
        "/conversations/conv-1/tasks",
        json={"title": "检查实现", "detail": "运行测试"},
    )
    assert created.status_code == 200
    task = created.json()
    assert task["task_id"].startswith("task_")
    assert task["status"] == TaskStatus.PENDING.value

    listed = client.get("/conversations/conv-1/tasks")
    assert listed.status_code == 200
    assert [item["task_id"] for item in listed.json()] == [task["task_id"]]

    updated = client.patch(
        f"/conversations/conv-1/tasks/{task['task_id']}",
        json={"status": "completed", "evidence_summary": "Focused tests passed"},
    )
    assert updated.status_code == 200
    assert updated.json()["status"] == TaskStatus.COMPLETED.value

    assert client.get("/conversations/conv-1/tasks").json() == []
    with_finished = client.get("/conversations/conv-1/tasks", params={"include_finished": True})
    assert [item["task_id"] for item in with_finished.json()] == [task["task_id"]]


def test_task_route_returns_404_for_missing_task():
    client = client_for(TaskLedger())

    response = client.patch(
        "/conversations/conv-1/tasks/task_missing",
        json={"status": "blocked", "evidence_summary": "missing"},
    )

    assert response.status_code == 404


def test_task_route_rejects_blocked_without_evidence():
    ledger = TaskLedger()
    client = client_for(ledger)
    task = client.post(
        "/conversations/conv-1/tasks",
        json={"title": "blocked needs evidence"},
    ).json()

    response = client.patch(
        f"/conversations/conv-1/tasks/{task['task_id']}",
        json={"status": "blocked"},
    )

    assert response.status_code == 400
    assert "evidence" in response.json()["detail"]
    listed = client.get("/conversations/conv-1/tasks").json()
    assert [item["task_id"] for item in listed] == [task["task_id"]]
    assert listed[0]["status"] == TaskStatus.PENDING.value
