from __future__ import annotations

from pathlib import Path

import pytest

from client_launcher.models import LauncherError
from client_launcher.ssh_config import SshConfigStore, parse_ssh_config_hosts


def test_missing_config_returns_empty_snapshot(tmp_path: Path):
    store = SshConfigStore(tmp_path / ".ssh" / "config")

    snapshot = store.read()

    assert snapshot.text == ""
    assert snapshot.hosts == ()
    assert snapshot.warnings == ()


def test_parse_concrete_hosts_preserves_order_and_skips_patterns():
    text = """
# comments are ignored
Host gpu-box *.internal !blocked
  HostName 10.0.0.8
Host laptop gpu-box
Host ?
Host
"""

    hosts, warnings = parse_ssh_config_hosts(text)

    assert hosts == ("gpu-box", "laptop")
    assert warnings == ("Line 7: Host has no alias",)


def test_write_preserves_text_and_reloads_hosts(tmp_path: Path):
    path = tmp_path / ".ssh" / "config"
    store = SshConfigStore(path)

    snapshot = store.write("Host gpu-box\n  HostName example.test\n")

    assert path.read_text(encoding="utf-8") == snapshot.text
    assert snapshot.hosts == ("gpu-box",)
    assert snapshot.path == str(path.resolve())


def test_write_failure_preserves_existing_file(tmp_path: Path, monkeypatch):
    path = tmp_path / ".ssh" / "config"
    path.parent.mkdir(parents=True)
    path.write_text("Host old\n", encoding="utf-8")
    store = SshConfigStore(path)

    def fail_replace(_src, _dst):
        raise OSError("disk full")

    monkeypatch.setattr("client_launcher.ssh_config.os.replace", fail_replace)

    with pytest.raises(LauncherError) as exc_info:
        store.write("Host new\n")

    assert exc_info.value.code == "ssh_config_write_failed"
    assert path.read_text(encoding="utf-8") == "Host old\n"


def test_parser_warning_does_not_block_text_editing(tmp_path: Path):
    def parser(_text: str):
        raise ValueError("unsupported Include cycle")

    path = tmp_path / ".ssh" / "config"
    store = SshConfigStore(path, parser=parser)

    snapshot = store.write("Include loop\n")

    assert snapshot.text == "Include loop\n"
    assert snapshot.hosts == ()
    assert snapshot.warnings == ("unsupported Include cycle",)
