import asyncio
from pathlib import Path
from types import SimpleNamespace

from backend.api.routes import config as config_route
from backend.core.config.config import Config
from backend.core.tools.orchestrator import ToolOrchestrator
from backend.core.tools.security.approval import ApprovalManager
from backend.core.tools.security.logical_sandbox import LogicalSandbox
from backend.core.tools.security.permissions import PermissionEngine


class FakeToolManager:
    instances = []

    def __init__(self, config):
        self.config = config
        self.closed = False
        self.initialized = False
        FakeToolManager.instances.append(self)

    async def init(self):
        self.initialized = True

    async def close(self):
        self.closed = True


class FakeModelManager:
    pass


def test_update_config_refreshes_tool_orchestrator_runtime_references(tmp_path, monkeypatch):
    async def run():
        FakeToolManager.instances = []
        monkeypatch.setattr(config_route, "ToolManager", FakeToolManager)
        monkeypatch.setattr(config_route, "ModelManager", FakeModelManager)

        config_path = tmp_path / "config.json"
        old_workspace = tmp_path / "old-workspace"
        new_workspace = tmp_path / "new-workspace"
        old_workspace.mkdir()
        new_workspace.mkdir()

        config_manager = Config(str(config_path))
        config_manager.data = {
            "provider": {},
            "default_provider": "",
            "tools": {
                "permissions": {
                    "sandbox": {
                        "workspace_roots": [str(old_workspace)],
                        "protected_paths": [".git"],
                    }
                }
            },
        }
        config_manager.save()

        old_tool_manager = FakeToolManager(config_manager.data)
        approval_manager = ApprovalManager()
        orchestrator = ToolOrchestrator(
            tool_manager=old_tool_manager,
            permission_engine=PermissionEngine.default(),
            approval_manager=approval_manager,
            logical_sandbox=LogicalSandbox.for_config(config_manager.data, Path.cwd()),
        )
        chat_manager = SimpleNamespace(
            model_manager=object(),
            tool_manager=old_tool_manager,
            tool_orchestrator=orchestrator,
        )
        app = SimpleNamespace(
            state=SimpleNamespace(
                config_manager=config_manager,
                tool_manager=old_tool_manager,
                approval_manager=approval_manager,
                tool_orchestrator=orchestrator,
                chat_manager=chat_manager,
            )
        )
        request = SimpleNamespace(app=app)

        response = await config_route.update_config(
            config_route.ConfigUpdateRequest(
                tools={
                    "permissions": {
                        "sandbox": {
                            "workspace_roots": [str(new_workspace)],
                            "protected_paths": ["locked"],
                        }
                    }
                }
            ),
            request,
            config_manager,
        )

        new_tool_manager = app.state.tool_manager
        assert response == {"message": "配置已更新"}
        assert new_tool_manager is not old_tool_manager
        assert old_tool_manager.closed is True
        assert new_tool_manager.initialized is True
        assert app.state.tool_orchestrator is orchestrator
        assert app.state.tool_orchestrator.tool_manager is new_tool_manager
        assert app.state.tool_orchestrator.approval_manager is approval_manager
        assert app.state.chat_manager.tool_orchestrator is orchestrator
        assert app.state.chat_manager.tool_manager is new_tool_manager
        assert app.state.tool_orchestrator.logical_sandbox.workspace_roots == [
            new_workspace.resolve()
        ]
        assert app.state.tool_orchestrator.logical_sandbox.protected_paths == [
            Path("locked")
        ]

    asyncio.run(run())


def test_update_config_stores_global_default_model_and_strips_provider_default(tmp_path, monkeypatch):
    async def run():
        FakeToolManager.instances = []
        monkeypatch.setattr(config_route, "ToolManager", FakeToolManager)
        monkeypatch.setattr(config_route, "ModelManager", FakeModelManager)

        config_manager = Config(str(tmp_path / "config.json"))
        config_manager.data = {
            "provider": {
                "demo": {
                    "name": "Demo",
                    "models": ["alpha"],
                    "enabled": True,
                    "default_model": "legacy",
                }
            },
            "default_provider": "demo",
            "default_model": "",
        }
        config_manager.save()

        old_tool_manager = FakeToolManager(config_manager.data)
        app = SimpleNamespace(
            state=SimpleNamespace(
                config_manager=config_manager,
                tool_manager=old_tool_manager,
            )
        )

        await config_route.update_config(
            config_route.ConfigUpdateRequest(
                default_model="alpha",
                provider_configs={
                    "demo": {
                        "name": "Demo",
                        "models": ["alpha", "beta"],
                        "enabled": True,
                        "default_model": "should-be-dropped",
                    }
                },
            ),
            SimpleNamespace(app=app),
            config_manager,
        )

        assert config_manager.data["default_model"] == "alpha"
        assert "default_model" not in config_manager.data["provider"]["demo"]

    asyncio.run(run())
