import asyncio
from pathlib import Path
from types import SimpleNamespace

import sys

import pytest

sys.path.insert(0, ".")

from backend.api.routes import config as config_route
from backend.core.capabilities.bootstrap import build_runtime_config_with_plugin_mcp
from backend.core.capabilities.registry import CapabilityRegistry
from backend.core.capabilities.types import LoadedPlugin
from backend.core.tools.tool_manager import ToolManager


def plugin_registry(*plugins: LoadedPlugin) -> CapabilityRegistry:
    registry = CapabilityRegistry()
    registry.add_plugins(plugins)
    return registry


def test_build_runtime_config_with_plugin_mcp_adds_prefixed_servers_without_mutating_sources(tmp_path):
    plugin_server = {
        "command": "plugin-server",
        "args": ["--stdio"],
        "env": {"PLUGIN_HOME": str(tmp_path)},
    }
    plugin = LoadedPlugin(
        plugin_id="demo@local",
        name="demo",
        root=tmp_path / "plugins" / "demo",
        mcp_servers={"search": plugin_server},
    )
    config_data = {
        "tools": {
            "enabled": True,
            "builtin": {"enabled": False},
            "mcp": {"enabled": False, "servers": {}},
        }
    }

    runtime_config = build_runtime_config_with_plugin_mcp(
        config_data,
        plugin_registry(plugin),
    )

    assert runtime_config is not config_data
    assert runtime_config["tools"] is not config_data["tools"]
    assert runtime_config["tools"]["mcp"]["enabled"] is True
    assert runtime_config["tools"]["builtin"] == {"enabled": False}
    assert runtime_config["tools"]["mcp"]["servers"]["demo.search"] == {
        "command": "plugin-server",
        "args": ["--stdio"],
        "env": {"PLUGIN_HOME": str(tmp_path)},
        "source": "plugin",
        "plugin_id": "demo@local",
        "plugin_name": "demo",
    }
    assert "source" not in plugin_server
    assert config_data["tools"]["mcp"]["enabled"] is False
    assert config_data["tools"]["mcp"]["servers"] == {}


def test_build_runtime_config_with_plugin_mcp_keeps_user_server_on_name_conflict(tmp_path):
    plugin = LoadedPlugin(
        plugin_id="demo@local",
        name="demo",
        root=tmp_path / "plugins" / "demo",
        mcp_servers={
            "search": {"command": "plugin-server"},
            "other": {"command": "other-server"},
        },
    )
    config_data = {
        "tools": {
            "builtin": {"enabled": True},
            "mcp": {
                "enabled": True,
                "servers": {
                    "demo.search": {
                        "command": "user-server",
                        "source": "user",
                    }
                },
            },
        }
    }

    runtime_config = build_runtime_config_with_plugin_mcp(
        config_data,
        plugin_registry(plugin),
    )

    servers = runtime_config["tools"]["mcp"]["servers"]
    assert servers["demo.search"] == {"command": "user-server", "source": "user"}
    assert servers["demo.other"]["command"] == "other-server"
    assert servers["demo.other"]["source"] == "plugin"


def test_build_runtime_config_with_plugin_mcp_without_plugin_servers_is_side_effect_free(tmp_path):
    config_data = {
        "tools": {
            "builtin": {"enabled": True},
            "mcp": {"enabled": False, "servers": {}},
        }
    }
    plugin = LoadedPlugin(
        plugin_id="empty@local",
        name="empty",
        root=tmp_path / "plugins" / "empty",
        mcp_servers={},
    )

    runtime_config = build_runtime_config_with_plugin_mcp(
        config_data,
        plugin_registry(plugin),
    )
    runtime_config["tools"]["builtin"]["enabled"] = False

    assert config_data["tools"]["builtin"]["enabled"] is True
    assert config_data["tools"]["mcp"]["enabled"] is False


def test_tool_manager_inventory_attributes_user_and_plugin_mcp_servers():
    manager = ToolManager(
        {
            "tools": {
                "enabled": True,
                "builtin": {"enabled": False},
                "mcp": {
                    "enabled": True,
                    "servers": {
                        "user-search": {"command": "user-server"},
                        "demo.search": {
                            "command": "plugin-server",
                            "source": "plugin",
                            "plugin_id": "demo@local",
                            "plugin_name": "demo",
                        },
                    },
                },
            }
        }
    )

    servers = {
        server["name"]: server
        for server in manager.describe_inventory()["mcp_servers"]
    }

    assert servers["user-search"]["source"] == "user"
    assert "plugin_id" not in servers["user-search"]
    assert "plugin_name" not in servers["user-search"]
    assert servers["demo.search"]["source"] == "plugin"
    assert servers["demo.search"]["plugin_id"] == "demo@local"
    assert servers["demo.search"]["plugin_name"] == "demo"


def test_update_config_uses_plugin_mcp_runtime_overlay_and_preserves_chat_registry(tmp_path, monkeypatch):
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

    async def run():
        FakeToolManager.instances = []
        monkeypatch.setattr(config_route, "ToolManager", FakeToolManager)
        monkeypatch.setattr(config_route, "ModelManager", FakeModelManager)

        config_path = tmp_path / "config.json"
        config_manager = config_route.Config(str(config_path))
        config_manager.data = {
            "provider": {},
            "default_provider": "",
            "tools": {
                "builtin": {"enabled": False},
                "mcp": {"enabled": False, "servers": {}},
            },
        }
        config_manager.save()

        registry = plugin_registry(
            LoadedPlugin(
                plugin_id="demo@local",
                name="demo",
                root=tmp_path / "plugins" / "demo",
                mcp_servers={"search": {"command": "plugin-server"}},
            )
        )
        old_tool_manager = FakeToolManager(config_manager.data)
        chat_manager = SimpleNamespace(
            model_manager=object(),
            tool_manager=old_tool_manager,
            tool_orchestrator=None,
            capability_registry=registry,
        )
        app = SimpleNamespace(
            state=SimpleNamespace(
                config_manager=config_manager,
                tool_manager=old_tool_manager,
                capability_registry=registry,
                chat_manager=chat_manager,
            )
        )
        request = SimpleNamespace(app=app)

        response = await config_route.update_config(
            config_route.ConfigUpdateRequest(default_provider=""),
            request,
            config_manager,
        )

        assert response == {"message": "配置已更新"}
        assert config_manager.data["tools"]["mcp"]["enabled"] is False
        assert config_manager.data["tools"]["mcp"]["servers"] == {}
        runtime_config = app.state.tool_manager.config
        assert runtime_config["tools"]["mcp"]["enabled"] is True
        assert runtime_config["tools"]["mcp"]["servers"]["demo.search"]["source"] == "plugin"
        assert app.state.capability_registry is registry
        assert app.state.chat_manager.capability_registry is registry

    asyncio.run(run())


@pytest.mark.parametrize("operation", ["add_provider", "delete_provider"])
def test_provider_config_mutations_use_plugin_mcp_runtime_overlay(tmp_path, monkeypatch, operation):
    class FakeToolManager:
        def __init__(self, config):
            self.config = config
            self.closed = False
            self.initialized = False

        async def init(self):
            self.initialized = True

        async def close(self):
            self.closed = True

    class FakeModelManager:
        pass

    async def run():
        monkeypatch.setattr(config_route, "ToolManager", FakeToolManager)
        monkeypatch.setattr(config_route, "ModelManager", FakeModelManager)

        config_path = tmp_path / "config.json"
        config_manager = config_route.Config(str(config_path))
        config_manager.data = {
            "provider": {"old": {"name": "Old"}} if operation == "delete_provider" else {},
            "default_provider": "",
            "tools": {
                "builtin": {"enabled": False},
                "mcp": {"enabled": False, "servers": {}},
            },
        }
        config_manager.save()

        registry = plugin_registry(
            LoadedPlugin(
                plugin_id="demo@local",
                name="demo",
                root=tmp_path / "plugins" / "demo",
                mcp_servers={"search": {"command": "plugin-server"}},
            )
        )
        old_tool_manager = FakeToolManager(config_manager.data)
        app = SimpleNamespace(
            state=SimpleNamespace(
                config_manager=config_manager,
                tool_manager=old_tool_manager,
                capability_registry=registry,
                chat_manager=SimpleNamespace(capability_registry=registry),
            )
        )
        request = SimpleNamespace(app=app)

        if operation == "add_provider":
            response = await config_route.add_provider(
                config_route.AddProviderRequest(id="new", name="New"),
                request,
                config_manager,
            )
            assert response == {"message": "提供商 new 已添加"}
        else:
            response = await config_route.delete_provider("old", request, config_manager)
            assert response == {"message": "提供商 old 已删除"}

        assert config_manager.data["tools"]["mcp"]["enabled"] is False
        assert config_manager.data["tools"]["mcp"]["servers"] == {}
        runtime_config = app.state.tool_manager.config
        assert runtime_config["tools"]["mcp"]["enabled"] is True
        assert runtime_config["tools"]["mcp"]["servers"]["demo.search"]["plugin_id"] == "demo@local"

    asyncio.run(run())
