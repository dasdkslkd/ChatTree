import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
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


def test_runtime_context_copies_share_observations_but_runs_do_not(tmp_path):
    global_root = tmp_path / "global"
    project = tmp_path / "project"
    global_root.mkdir()
    project.mkdir()
    (project / "file.txt").write_text("old\n", encoding="utf-8")
    manager = make_manager(global_root)
    workspace = normalize_workspace({"cwd": str(project)})
    first_run = {"file_observations": {}}
    same_run_copy = dict(first_run)
    second_run = {"file_observations": {}}

    run(manager.execute_tool("read", {"path": "file.txt"}, workspace=workspace, runtime_context=first_run))
    same_run = json.loads(run(manager.execute_tool(
        "edit",
        {"operation": "overwrite", "path": "file.txt", "content": "first\n"},
        workspace=workspace,
        runtime_context=same_run_copy,
    )))
    separate_run = json.loads(run(manager.execute_tool(
        "edit",
        {"operation": "overwrite", "path": "file.txt", "content": "second\n"},
        workspace=workspace,
        runtime_context=second_run,
    )))

    assert same_run_copy["file_observations"] is first_run["file_observations"]
    assert second_run["file_observations"] is not first_run["file_observations"]
    assert "error" not in same_run
    assert separate_run["error"]["type"] == "stale_file"


def test_concurrent_same_path_overwrites_allow_only_one_observed_run(tmp_path):
    global_root = tmp_path / "global"
    project = tmp_path / "project"
    global_root.mkdir()
    project.mkdir()
    (project / "file.txt").write_text("old\n", encoding="utf-8")
    manager = make_manager(global_root)
    workspace = normalize_workspace({"cwd": str(project)})
    contexts = [{"file_observations": {}}, {"file_observations": {}}]
    for context in contexts:
        run(manager.execute_tool("read", {"path": "file.txt"}, workspace=workspace, runtime_context=context))

    def overwrite(index):
        return json.loads(run(manager.execute_tool(
            "edit",
            {"operation": "overwrite", "path": "file.txt", "content": f"run-{index}\n"},
            workspace=workspace,
            runtime_context=contexts[index],
        )))

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(overwrite, range(2)))

    assert sum("error" not in result for result in results) == 1
    assert [result.get("error", {}).get("type") for result in results].count("stale_file") == 1


def test_independent_paths_keep_observations_independent(tmp_path):
    global_root = tmp_path / "global"
    project = tmp_path / "project"
    global_root.mkdir()
    project.mkdir()
    for name in ("a.txt", "b.txt"):
        (project / name).write_text("old\n", encoding="utf-8")
    manager = make_manager(global_root)
    workspace = normalize_workspace({"cwd": str(project)})
    contexts = [{"file_observations": {}}, {"file_observations": {}}]
    for index, name in enumerate(("a.txt", "b.txt")):
        run(manager.execute_tool("read", {"path": name}, workspace=workspace, runtime_context=contexts[index]))

    def overwrite(item):
        index, name = item
        return json.loads(run(manager.execute_tool(
            "edit",
            {"operation": "overwrite", "path": name, "content": "new\n"},
            workspace=workspace,
            runtime_context=contexts[index],
        )))

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(overwrite, enumerate(("a.txt", "b.txt"))))

    assert all("error" not in result for result in results)
    assert set(contexts[0]["file_observations"]) != set(contexts[1]["file_observations"])


def test_orchestrator_enforces_workspace_protected_path_per_call(tmp_path):
    global_root = tmp_path / "global"
    project = tmp_path / "project"
    global_root.mkdir()
    (project / ".git").mkdir(parents=True)
    manager = make_manager(global_root)
    orchestrator = make_orchestrator(manager, global_root)

    message = run(orchestrator.execute_tool_call(
        tool_call("edit", {
            "operation": "create",
            "path": ".git/config",
            "content": "unsafe",
        }),
        conversation_id="conv-1",
        node_id="node-1",
        workspace=normalize_workspace({"cwd": str(project), "protected_paths": [".git"]}),
    ))

    error = json.loads(message["content"])["error"]
    assert error["type"] == "permission_denied"
    assert "protected path" in error["reason"]
    assert not (project / ".git" / "config").exists()
