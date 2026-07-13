import asyncio
import json
from pathlib import Path

from backend.core.tools.orchestrator import ToolOrchestrator
from backend.core.tools.security.approval import ApprovalManager
from backend.core.tools.security.logical_sandbox import LogicalSandbox
from backend.core.tools.security.permissions import PermissionEngine, PermissionRule
from backend.core.tools.tool_manager import ToolManager
from backend.core.workspace import normalize_workspace


def run(coro):
    return asyncio.run(coro)


def tool_call(name: str, args: dict):
    return {
        "id": "call-1",
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps(args, ensure_ascii=False),
        },
    }


def make_manager(global_root: Path) -> ToolManager:
    return ToolManager({
        "tools": {
            "builtin": {
                "enabled": True,
                "code": {
                    "enabled": True,
                    "workspace_roots": [str(global_root)],
                    "protected_paths": [".git"],
                    "command_timeout_seconds": 3,
                },
                "web_search": {"enabled": False},
            }
        }
    })


def make_orchestrator(manager: ToolManager, global_root: Path) -> ToolOrchestrator:
    return ToolOrchestrator(
        tool_manager=manager,
        permission_engine=PermissionEngine([
            PermissionRule(
                id="allow-code-tools",
                behavior="allow",
                target_type="tool",
                pattern="*",
            )
        ]),
        approval_manager=ApprovalManager(),
        logical_sandbox=LogicalSandbox([global_root], [".git"]),
    )


def test_shell_uses_workspace_cwd_instead_of_global_tool_root(tmp_path):
    global_root = tmp_path / "global"
    project = tmp_path / "project"
    global_root.mkdir()
    project.mkdir()
    (project / "marker.txt").write_text("from project", encoding="utf-8")
    manager = make_manager(global_root)
    workspace = normalize_workspace({"cwd": str(project), "protected_paths": [".git"]})

    result = run(manager.execute_tool(
        "shell",
        {"command": "python -c \"import pathlib; print(pathlib.Path('marker.txt').read_text())\""},
        workspace=workspace,
    ))

    payload = json.loads(result)
    assert payload["exit_code"] == 0
    assert payload["stdout"].strip() == "from project"
    assert payload["cwd"] == "."


def test_workspace_contexts_do_not_share_code_tool_cwd_state(tmp_path):
    global_root = tmp_path / "global"
    first = tmp_path / "first"
    second = tmp_path / "second"
    global_root.mkdir()
    first.mkdir()
    second.mkdir()
    (first / "name.txt").write_text("first", encoding="utf-8")
    (second / "name.txt").write_text("second", encoding="utf-8")
    manager = make_manager(global_root)

    first_payload = json.loads(run(manager.execute_tool(
        "read",
        {"path": "name.txt"},
        workspace=normalize_workspace({"cwd": str(first)}),
    )))
    second_payload = json.loads(run(manager.execute_tool(
        "read",
        {"path": "name.txt"},
        workspace=normalize_workspace({"cwd": str(second)}),
    )))

    assert first_payload["content"] == "1\tfirst"
    assert second_payload["content"] == "1\tsecond"


def test_orchestrator_enforces_workspace_protected_path_per_call(tmp_path):
    global_root = tmp_path / "global"
    project = tmp_path / "project"
    global_root.mkdir()
    (project / ".git").mkdir(parents=True)
    manager = make_manager(global_root)
    orchestrator = make_orchestrator(manager, global_root)

    message = run(orchestrator.execute_tool_call(
        tool_call("write", {"path": ".git/config", "content": "unsafe"}),
        conversation_id="conv-1",
        node_id="node-1",
        workspace=normalize_workspace({"cwd": str(project), "protected_paths": [".git"]}),
    ))

    error = json.loads(message["content"])["error"]
    assert error["type"] == "permission_denied"
    assert "protected path" in error["reason"]
    assert not (project / ".git" / "config").exists()
