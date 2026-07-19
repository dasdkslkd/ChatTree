from __future__ import annotations

import os
import subprocess
from typing import Any


def subprocess_window_kwargs(*, new_process_group: bool = False) -> dict[str, Any]:
    if os.name == "nt":
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        if new_process_group:
            flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        kwargs: dict[str, Any] = {"creationflags": flags}
        startupinfo_factory = getattr(subprocess, "STARTUPINFO", None)
        if startupinfo_factory is not None:
            startupinfo = startupinfo_factory()
            startupinfo.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 1)
            startupinfo.wShowWindow = getattr(subprocess, "SW_HIDE", 0)
            kwargs["startupinfo"] = startupinfo
        return kwargs
    if new_process_group:
        return {"start_new_session": True}
    return {}
