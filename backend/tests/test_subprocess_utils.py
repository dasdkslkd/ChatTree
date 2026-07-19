import subprocess

from backend.core import subprocess_utils


def test_windows_subprocess_window_kwargs_hide_console(monkeypatch):
    monkeypatch.setattr(subprocess_utils.os, "name", "nt", raising=False)
    monkeypatch.setattr(subprocess_utils.subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    monkeypatch.setattr(subprocess_utils.subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200, raising=False)

    kwargs = subprocess_utils.subprocess_window_kwargs()

    assert kwargs["creationflags"] == 0x08000000
    if hasattr(subprocess, "STARTUPINFO"):
        assert kwargs["startupinfo"].dwFlags & subprocess.STARTF_USESHOWWINDOW
        assert kwargs["startupinfo"].wShowWindow == getattr(subprocess, "SW_HIDE", 0)


def test_windows_subprocess_window_kwargs_keep_process_group(monkeypatch):
    monkeypatch.setattr(subprocess_utils.os, "name", "nt", raising=False)
    monkeypatch.setattr(subprocess_utils.subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    monkeypatch.setattr(subprocess_utils.subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200, raising=False)

    kwargs = subprocess_utils.subprocess_window_kwargs(new_process_group=True)

    assert kwargs["creationflags"] == 0x08000200
    if hasattr(subprocess, "STARTUPINFO"):
        assert kwargs["startupinfo"].dwFlags & subprocess.STARTF_USESHOWWINDOW
        assert kwargs["startupinfo"].wShowWindow == getattr(subprocess, "SW_HIDE", 0)


def test_posix_subprocess_window_kwargs_keep_session_boundary(monkeypatch):
    monkeypatch.setattr(subprocess_utils.os, "name", "posix", raising=False)

    assert subprocess_utils.subprocess_window_kwargs() == {}
    assert subprocess_utils.subprocess_window_kwargs(new_process_group=True) == {
        "start_new_session": True,
    }
