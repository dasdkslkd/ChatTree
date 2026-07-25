from __future__ import annotations

from client_launcher.server_compat import (
    is_supported_server_version,
    parse_chattree_server_version,
)


def test_parse_chattree_server_version_accepts_exact_cli_line():
    assert (
        parse_chattree_server_version("noise\nchattree-server 0.1.0\n")
        == "0.1.0"
    )


def test_parse_chattree_server_version_rejects_unowned_output():
    assert parse_chattree_server_version("server 0.1.0\n") is None


def test_supported_server_version_requires_exact_launcher_contract():
    assert is_supported_server_version("0.1.0") is True
    assert is_supported_server_version("0.0.9") is False
    assert is_supported_server_version("0.2.0") is False
