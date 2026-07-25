from __future__ import annotations

import pytest

from backend.core.server import SERVER_VERSION
from client_launcher.server_binary import (
    REQUIRED_SERVER_VERSION,
    ServerBinaryVersionError,
    ensure_supported_server_version,
    parse_chattree_server_version,
)


def test_required_server_version_tracks_backend_contract():
    assert REQUIRED_SERVER_VERSION == SERVER_VERSION


def test_parse_chattree_server_version_accepts_cli_output():
    assert parse_chattree_server_version("chattree-server 0.1.0\n") == "0.1.0"


def test_parse_chattree_server_version_scans_surrounding_output():
    assert (
        parse_chattree_server_version("debug\nchattree-server 0.1.0\n")
        == "0.1.0"
    )


@pytest.mark.parametrize("output", ["", "0.1.0", "chattree 0.1.0"])
def test_parse_chattree_server_version_rejects_invalid_output(output: str):
    with pytest.raises(ServerBinaryVersionError) as exc_info:
        parse_chattree_server_version(output)

    assert exc_info.value.code == "server_version_invalid"


def test_ensure_supported_server_version_accepts_exact_required_version():
    ensure_supported_server_version(REQUIRED_SERVER_VERSION)


def test_ensure_supported_server_version_rejects_mismatch():
    with pytest.raises(ServerBinaryVersionError) as exc_info:
        ensure_supported_server_version("9.9.9")

    assert exc_info.value.code == "server_version_incompatible"
    assert exc_info.value.observed_version == "9.9.9"
