from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
FRONTEND_SRC = ROOT / "frontend" / "src"
CLIENT = FRONTEND_SRC / "api" / "client.ts"


def test_frontend_api_literals_are_centralized():
    offenders = []
    direct_transport_offenders = []
    for path in FRONTEND_SRC.rglob("*"):
        if path.suffix not in {".ts", ".tsx"}:
            continue
        text = path.read_text(encoding="utf-8")
        if re.search(r"""[\"'`]/api/""", text) and path != CLIENT:
            offenders.append(path.relative_to(ROOT).as_posix())
        if re.search(r"""\b(?:fetch|sendBeacon)\(\s*[\"'`]/""", text):
            direct_transport_offenders.append(path.relative_to(ROOT).as_posix())

    assert offenders == []
    assert direct_transport_offenders == []

    client_text = CLIENT.read_text(encoding="utf-8")
    assert "'/api/v1'" in client_text
    assert "baseURL: frontendBootstrap.apiBase" in client_text
    assert re.search(r"""baseURL:\s*[\"']/api[\"']""", client_text) is None
    assert "import.meta.env" not in client_text


def test_vite_forwards_api_v1_without_root_rewrite():
    text = (ROOT / "frontend" / "vite.config.ts").read_text(encoding="utf-8")

    assert "'/api': {" in text
    assert "rewrite:" not in text
