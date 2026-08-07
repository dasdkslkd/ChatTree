"""Tests for the managed SearXNG runtime (backend/core/tools/searxng_runtime.py)."""
import asyncio
from pathlib import Path

from backend.core.tools.searxng_runtime import (
    SearxngRuntime,
    SearxngUnavailableError,
    _searxng_executable,
)


class FakeProcess:
    def __init__(self):
        self.terminated = False

    def poll(self):
        return None

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.terminated = True

    def wait(self, timeout=None):
        return 0


def test_no_binary_raises_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr("backend.core.tools.searxng_runtime._searxng_executable", lambda: None)
    runtime = SearxngRuntime({"searxng_url": "http://127.0.0.1:8888"}, home=tmp_path)
    try:
        asyncio.run(runtime.ensure_url())
        raise AssertionError("expected SearxngUnavailableError")
    except SearxngUnavailableError:
        pass


def test_failure_latches_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr("backend.core.tools.searxng_runtime._searxng_executable", lambda: None)
    runtime = SearxngRuntime({"searxng_url": "http://127.0.0.1:8888"}, home=tmp_path)

    async def probe(base_url):
        return False

    escapes = []
    monkeys = []
    monkeypatch.setattr(runtime, "_probe", probe)
    async def start_managed():
        # simulate spawn racing: override the real one so we can count calls
        raise SearxngUnavailableError("boom")

    original_start = runtime._start_managed
    calls = {"n": 0}

    async def counting_start():
        calls["n"] += 1
        return await original_start()

    runtime._start_managed = counting_start
    try:
        asyncio.run(runtime.ensure_url())
        raise AssertionError("expected SearxngUnavailableError")
    except SearxngUnavailableError:
        pass

    # Second call must short-circuit via _unavailable and not probe/start again.
    monkeypatch.setattr(runtime, "_start_managed", lambda: (_ for _ in ()).throw(AssertionError("should not start")))
    try:
        asyncio.run(runtime.ensure_url())
        raise AssertionError("expected SearxngUnavailableError")
    except SearxngUnavailableError:
        pass
    assert calls["n"] == 1


def test_external_url_reachable_is_used(tmp_path, monkeypatch):
    runtime = SearxngRuntime({"searxng_url": "http://192.168.1.10:8888"}, home=tmp_path)

    async def fake_probe(base_url):
        return base_url == "http://192.168.1.10:8888"

    monkeypatch.setattr(runtime, "_probe", fake_probe)
    assert asyncio.run(runtime.ensure_url()) == "http://192.168.1.10:8888"
    asyncio.run(runtime.close())


def test_external_url_not_reachable_spawns_managed(tmp_path, monkeypatch):
    runtime = SearxngRuntime({"searxng_url": "https://remote.example.com"}, home=tmp_path)
    spawned = {"url": None}

    def fake_spawn(binary, settings_path):
        runtime._process = FakeProcess()

    async def fake_wait_ready(base_url, proc):
        spawned["url"] = base_url
        return None

    async def probe_false(base_url):
        return False

    monkeypatch.setattr("backend.core.tools.searxng_runtime._searxng_executable", lambda: Path("fake-searxng-server"))
    monkeypatch.setattr(runtime, "_probe", probe_false)
    monkeypatch.setattr(runtime, "_spawn", fake_spawn)
    monkeypatch.setattr(runtime, "_wait_ready", fake_wait_ready)

    got = asyncio.run(runtime.ensure_url())
    assert got == spawned["url"]
    assert got.startswith("http://127.0.0.1:")
    asyncio.run(runtime.close())


def test_spawns_owned_process_and_closes(tmp_path, monkeypatch):
    runtime = SearxngRuntime({"searxng_url": "http://127.0.0.1:8888"}, home=tmp_path)
    process = FakeProcess()
    spawned = {"url": None}

    async def probe_false(base_url):
        return False

    def fake_spawn(binary, settings_path):
        runtime._process = process

    async def fake_wait_ready(base_url, proc):
        spawned["url"] = base_url
        return None

    monkeypatch.setattr("backend.core.tools.searxng_runtime._searxng_executable", lambda: Path("fake-searxng-server"))
    monkeypatch.setattr(runtime, "_probe", probe_false)
    monkeypatch.setattr(runtime, "_spawn", fake_spawn)
    monkeypatch.setattr(runtime, "_wait_ready", fake_wait_ready)

    assert asyncio.run(runtime.ensure_url()) == spawned["url"]
    assert spawned["url"].startswith("http://127.0.0.1:")
    asyncio.run(runtime.close())
    assert process.terminated


def test_settings_file_contains_secret_port_and_json(tmp_path):
    runtime = SearxngRuntime(home=tmp_path)
    settings = runtime._write_settings(8888).read_text(encoding="utf-8")
    assert "secret_key:" in settings
    assert "port: 8888" in settings
    assert "- json" in settings
    assert "request_timeout:" in settings


def test_settings_file_writes_proxy_when_configured(tmp_path):
    runtime = SearxngRuntime(
        {"outgoing_proxies": "http://127.0.0.1:7890"},
        home=tmp_path,
    )
    settings = runtime._write_settings(8888).read_text(encoding="utf-8")
    assert "outgoing:" in settings
    assert "proxies:" in settings
    assert "all://:" in settings
    assert "- http://127.0.0.1:7890" in settings


def test_proxy_url_without_scheme_is_normalized(tmp_path):
    runtime = SearxngRuntime({"outgoing_proxies": "127.0.0.1:7890"}, home=tmp_path)
    settings = runtime._write_settings(8888).read_text(encoding="utf-8")
    assert "- http://127.0.0.1:7890" in settings


def test_settings_file_omits_proxy_when_blank(tmp_path):
    runtime = SearxngRuntime({"outgoing_proxies": "  "}, home=tmp_path)
    settings = runtime._write_settings(8888).read_text(encoding="utf-8")
    assert "request_timeout:" in settings
    assert "proxies:" not in settings


def test_settings_file_omits_proxy_when_disabled(tmp_path):
    runtime = SearxngRuntime(
        {"outgoing_proxies": "http://127.0.0.1:7890", "use_proxies": False},
        home=tmp_path,
    )
    settings = runtime._write_settings(8888).read_text(encoding="utf-8")
    assert "request_timeout:" in settings
    assert "proxies:" not in settings


def test_restart_spawns_new_process(tmp_path, monkeypatch):
    runtime = SearxngRuntime({"searxng_url": "http://127.0.0.1:8888"}, home=tmp_path)
    process = FakeProcess()
    spawn_count = {"n": 0}
    spawned = {"url": None}

    def fake_spawn(binary, settings_path):
        spawn_count["n"] += 1
        runtime._process = process

    async def fake_wait_ready(base_url, proc):
        spawned["url"] = base_url
        return None

    async def probe_false(base_url):
        return False

    monkeypatch.setattr("backend.core.tools.searxng_runtime._searxng_executable", lambda: Path("fake-searxng-server"))
    monkeypatch.setattr(runtime, "_probe", probe_false)
    monkeypatch.setattr(runtime, "_spawn", fake_spawn)
    monkeypatch.setattr(runtime, "_wait_ready", fake_wait_ready)

    assert asyncio.run(runtime.ensure_url()) == spawned["url"]
    asyncio.run(runtime.restart())
    assert spawn_count["n"] == 2
    asyncio.run(runtime.close())


def test_external_flag_derivation():
    assert SearxngRuntime({"searxng_url": "http://localhost:9000"}, home=Path("."))._external is False
    assert SearxngRuntime({"searxng_url": "https://remote.example.com"}, home=Path("."))._external is True
