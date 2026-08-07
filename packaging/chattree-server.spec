# -*- mode: python ; coding: utf-8 -*-

from __future__ import annotations

import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules


REPO_ROOT = Path(os.environ.get("CHATTREE_REPO_ROOT", SPECPATH)).resolve()
ONE_DIR = os.environ.get("CHATTREE_PYINSTALLER_ONE_DIR") == "1"
ENTRYPOINT = REPO_ROOT / "packaging" / "chattree_server_entry.py"
RIPGREP_BINARY = Path(os.environ["CHATTREE_BUNDLED_RIPGREP"]).resolve()
SEARXNG_BINARY = os.environ.get("CHATTREE_BUNDLED_SEARXNG")

datas = []
datas += collect_data_files("backend.core.model")
datas += collect_data_files("backend.core.prompts")
datas += collect_data_files("backend.workers")
datas.append(
    (str(REPO_ROOT / "packaging" / "third_party" / "SEARXNG_NOTICE.txt"), "tools/searxng")
)

unicodedata_binary = Path(sys.base_prefix) / "DLLs" / "unicodedata.pyd"

hiddenimports = []
hiddenimports += collect_submodules(
    "backend",
    filter=lambda name: not name.startswith("backend.tests"),
)
hiddenimports += collect_submodules("chattree_protocol")
hiddenimports += [
    "backend.server_cli",
    "backend.core.model.providers.anthropic_provider",
    "backend.core.model.providers.gemini_provider",
    "backend.core.model.providers.multimodal",
    "backend.core.model.providers.openai_compatible",
    "backend.core.model.providers.retry",
    "backend.core.model.providers.sse",
    "chattree_protocol.http_errors",
    "main",
    "unicodedata",
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
]

binaries = [(str(RIPGREP_BINARY), "tools/ripgrep")]
if SEARXNG_BINARY:
    binaries.append((str(Path(SEARXNG_BINARY).resolve()), "tools/searxng"))
block_cipher = None

a = Analysis(
    [str(ENTRYPOINT)],
    pathex=[str(REPO_ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
if unicodedata_binary.exists():
    a.binaries.append(("unicodedata.pyd", str(unicodedata_binary), "EXTENSION"))
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

if ONE_DIR:
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name="chattree-server",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=True,
        console=True,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
    )
    coll = COLLECT(
        exe,
        a.binaries,
        a.zipfiles,
        a.datas,
        strip=False,
        upx=True,
        upx_exclude=[],
        name="chattree-server",
    )
else:
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.zipfiles,
        a.datas,
        [],
        name="chattree-server",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=True,
        upx_exclude=[],
        runtime_tmpdir=None,
        console=True,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
    )
