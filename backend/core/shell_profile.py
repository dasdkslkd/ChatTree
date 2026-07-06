from __future__ import annotations

import os
import platform as platform_module
import shutil
from dataclasses import asdict, dataclass, field
from typing import Any, Literal, Optional


ShellId = Literal["powershell", "pwsh", "cmd", "bash", "zsh", "sh"]
PlatformId = Literal["windows", "linux", "darwin"]


@dataclass(frozen=True)
class CommandExample:
    label: str
    command: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class ShellProfile:
    id: ShellId
    platform: PlatformId
    display_name: str
    executable: str
    args_template: list[str]
    path_separator: str
    line_ending: str
    highlighter_language: Literal["powershell", "bash", "batch"]
    syntax_notes: list[str] = field(default_factory=list)
    preferred_examples: list[CommandExample] = field(default_factory=list)
    forbidden_examples: list[CommandExample] = field(default_factory=list)

    def command_argv(self, command: str) -> list[str]:
        return [self.executable, *[part.replace("{command}", command) for part in self.args_template]]

    def snapshot(self) -> dict[str, Any]:
        data = asdict(self)
        data["preferred_examples"] = [example.to_dict() for example in self.preferred_examples]
        data["forbidden_examples"] = [example.to_dict() for example in self.forbidden_examples]
        return data


class ShellProfileResolver:
    def __init__(
        self,
        *,
        platform: Optional[str] = None,
        shell: Optional[str] = None,
        executable: Optional[str] = None,
        allowed_shells: Optional[list[str]] = None,
    ) -> None:
        self._platform = platform
        self._shell = shell
        self._executable = executable
        self._allowed_shells = {str(item).lower() for item in allowed_shells or []}

    def resolve(self) -> ShellProfile:
        platform_id = self._resolve_platform()
        shell_id = self._resolve_shell(platform_id)
        if self._allowed_shells and shell_id not in self._allowed_shells and "auto" not in self._allowed_shells:
            raise ValueError(f"shell '{shell_id}' is not allowed")
        return _profile_for(platform_id, shell_id, executable=self._executable)

    def _resolve_platform(self) -> PlatformId:
        raw = (self._platform or "").lower()
        if raw in {"windows", "win32", "cygwin", "msys"}:
            return "windows"
        if raw in {"darwin", "mac", "macos"}:
            return "darwin"
        if raw in {"linux", "posix"}:
            return "linux"
        system = platform_module.system().lower()
        if system == "windows" or os.name == "nt":
            return "windows"
        if system == "darwin":
            return "darwin"
        return "linux"

    def _resolve_shell(self, platform_id: PlatformId) -> ShellId:
        requested = _normalize_shell(self._shell)
        if requested:
            return requested
        if platform_id == "windows":
            if shutil.which("pwsh"):
                return "pwsh"
            if shutil.which("powershell"):
                return "powershell"
            return "cmd"
        env_shell = os.environ.get("SHELL", "")
        normalized = _normalize_shell(env_shell)
        if normalized in {"bash", "zsh", "sh"}:
            return normalized
        if shutil.which("bash"):
            return "bash"
        if shutil.which("zsh"):
            return "zsh"
        return "sh"


def _normalize_shell(value: Optional[str]) -> Optional[ShellId]:
    if not value:
        return None
    name = str(value).replace("\\", "/").rsplit("/", 1)[-1].lower()
    if name.endswith(".exe"):
        name = name[:-4]
    if name in {"powershell", "windows powershell"}:
        return "powershell"
    if name == "pwsh":
        return "pwsh"
    if name in {"cmd", "cmd32", "cmd64"}:
        return "cmd"
    if name == "bash":
        return "bash"
    if name == "zsh":
        return "zsh"
    if name == "sh":
        return "sh"
    return None


def _profile_for(platform_id: PlatformId, shell_id: ShellId, *, executable: Optional[str]) -> ShellProfile:
    if shell_id in {"powershell", "pwsh"}:
        exe = executable or shell_id
        display = "PowerShell" if shell_id == "powershell" else "PowerShell 7"
        return ShellProfile(
            id=shell_id,
            platform=platform_id,
            display_name=display,
            executable=exe,
            args_template=["-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", "{command}"],
            path_separator="\\",
            line_ending="\r\n",
            highlighter_language="powershell",
            syntax_notes=[
                "Use PowerShell syntax for variables, quoting, pipes, and control flow.",
                "Environment variables use $env:NAME and assignment uses $env:NAME = \"value\".",
                "PowerShell pipes objects; prefer Select-Object, Where-Object, and ForEach-Object for transformations.",
                "Use single-quoted here-strings @' ... '@ for multiline literal text.",
            ],
            preferred_examples=[
                CommandExample("run tests", "npm test"),
                CommandExample("build project", "npm run build"),
                CommandExample("set env and run", "$env:FOO = \"bar\"; npm test"),
                CommandExample("inline python", "@'\nprint('hello')\n'@ | python -"),
            ],
            forbidden_examples=[
                CommandExample("Bash control flow", "if [ -f package.json ]; then npm test; fi"),
                CommandExample("Bash env prefix", "FOO=bar npm test"),
                CommandExample("POSIX stderr null", "cmd 2>/dev/null"),
            ],
        )
    if shell_id == "cmd":
        return ShellProfile(
            id="cmd",
            platform=platform_id,
            display_name="Command Prompt",
            executable=executable or "cmd",
            args_template=["/d", "/s", "/c", "{command}"],
            path_separator="\\",
            line_ending="\r\n",
            highlighter_language="batch",
            syntax_notes=[
                "Use cmd.exe batch syntax.",
                "Environment variables use %NAME%; set variables with set NAME=value.",
                "Prefer PowerShell if available for complex scripting.",
            ],
            preferred_examples=[
                CommandExample("run tests", "npm test"),
                CommandExample("set env and run", "set FOO=bar && npm test"),
            ],
            forbidden_examples=[
                CommandExample("bash env prefix", "FOO=bar npm test"),
                CommandExample("powershell env assignment", "$env:FOO = \"bar\""),
            ],
        )
    display = {"bash": "Bash", "zsh": "Zsh", "sh": "POSIX sh"}[shell_id]
    return ShellProfile(
        id=shell_id,
        platform=platform_id,
        display_name=display,
        executable=executable or shell_id,
        args_template=["-lc", "{command}"] if shell_id in {"bash", "zsh"} else ["-c", "{command}"],
        path_separator="/",
        line_ending="\n",
        highlighter_language="bash",
        syntax_notes=[
            f"Use {display} syntax for variables, quoting, pipes, and control flow.",
            "Environment variables can be set inline as NAME=value command.",
            "Here-documents and POSIX redirection are valid in this shell.",
        ],
        preferred_examples=[
            CommandExample("run tests", "npm test"),
            CommandExample("build project", "npm run build"),
            CommandExample("set env and run", "FOO=bar npm test"),
            CommandExample("inline python", "python - <<'PY'\nprint('hello')\nPY"),
        ],
        forbidden_examples=[
            CommandExample("powershell listing", "Get-ChildItem -Force"),
            CommandExample("powershell env assignment", "$env:FOO = \"bar\"; npm test"),
        ],
    )


def render_command_tool_guidance(profile: ShellProfile) -> str:
    preferred = "\n".join(f"- {item.label}: `{item.command}`" for item in profile.preferred_examples)
    forbidden = "\n".join(f"- Do not use {item.label}" for item in profile.forbidden_examples)
    notes = "\n".join(f"- {note}" for note in profile.syntax_notes)
    return (
        f"Command environment: active shell is {profile.display_name} on {profile.platform}.\n"
        "Commands run in the active shell shown here. Do not assume POSIX shell syntax unless the active shell is bash/zsh/sh.\n"
        f"Path separator: `{profile.path_separator}`. Code block language: `{profile.highlighter_language}`.\n"
        "\nSyntax notes:\n"
        f"{notes}\n"
        "\nPreferred examples:\n"
        f"{preferred}\n"
        "\nForbidden examples:\n"
        f"{forbidden}"
    )
