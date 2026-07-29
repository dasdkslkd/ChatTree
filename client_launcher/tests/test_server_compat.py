from __future__ import annotations

from backend.core.server import SERVER_VERSION
from client_launcher.server_compat import (
    is_supported_server_version,
    parse_chattree_server_version,
)


def test_parse_chattree_server_version_accepts_exact_cli_line():
    assert (
        parse_chattree_server_version(
            f"noise\nchattree-server {SERVER_VERSION}\n"
        )
        == SERVER_VERSION
    )


def test_parse_chattree_server_version_rejects_unowned_output():
    assert parse_chattree_server_version("server 0.1.0\n") is None


def test_supported_server_version_requires_exact_launcher_contract():
    assert is_supported_server_version(SERVER_VERSION) is True
    assert is_supported_server_version("0.0.9") is False
    assert is_supported_server_version("0.2.0") is False
