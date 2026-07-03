from __future__ import annotations

import re
from dataclasses import dataclass
from fnmatch import fnmatch
from typing import Literal, Optional


CommandBehavior = Literal["allow", "ask", "deny"]


@dataclass(frozen=True)
class CommandDecision:
    behavior: CommandBehavior
    reason: str
    matched_rule: Optional["CommandRule"] = None


@dataclass(frozen=True)
class CommandRule:
    id: str
    behavior: CommandBehavior
    pattern: str
    reason: str


class CommandPolicy:
    _COMPOUND_OR_REDIRECT_RE = re.compile(r"&&|\|\||[;|<>]")
    _SEGMENT_SPLIT_RE = re.compile(r"&&|\|\||[;|<>]")
    _SHELL_SUBSTITUTION_RE = re.compile(r"\$\(|`")

    def __init__(self, rules: list[CommandRule]):
        self._rules = list(rules)

    @classmethod
    def default(cls) -> "CommandPolicy":
        return cls([
            CommandRule("deny-rm-rf", "deny", "rm -rf *", "destructive recursive deletion"),
            CommandRule("deny-remove-item-recursive", "deny", "Remove-Item * -Recurse*", "destructive recursive deletion"),
            CommandRule("deny-remove-item-force-recursive", "deny", "Remove-Item * -Force* -Recurse*", "destructive recursive deletion"),
            CommandRule("allow-git-status", "allow", "git status*", "read-only git status"),
            CommandRule("allow-git-diff", "allow", "git diff*", "read-only git diff"),
            CommandRule("allow-rg", "allow", "rg*", "read-only ripgrep search"),
            CommandRule("allow-pytest", "allow", "pytest*", "test command"),
            CommandRule("ask-git-commit", "ask", "git commit*", "git commit mutates repository history"),
            CommandRule("ask-npm-install", "ask", "npm install*", "dependency installation mutates project state"),
            CommandRule("ask-pip-install", "ask", "pip install*", "dependency installation mutates environment"),
        ])

    def classify(self, command: str, *, shell_id: str = "") -> CommandDecision:
        normalized = command.strip()
        if not normalized:
            return CommandDecision("deny", "empty command")

        deny = self._first_matching_rule(normalized, "deny", include_segments=True)
        if deny:
            return CommandDecision("deny", deny.reason, deny)

        if self._SHELL_SUBSTITUTION_RE.search(normalized):
            shell = f" in {shell_id}" if shell_id else ""
            return CommandDecision("ask", f"shell command substitution{shell} requires approval")

        if self._COMPOUND_OR_REDIRECT_RE.search(normalized):
            shell = f" in {shell_id}" if shell_id else ""
            return CommandDecision("ask", f"compound, piped, or redirected command{shell} requires approval")

        rule = self._first_matching_rule(normalized)
        if rule:
            return CommandDecision(rule.behavior, rule.reason, rule)

        shell = f" for {shell_id}" if shell_id else ""
        return CommandDecision("ask", f"unclassified command{shell} requires approval")

    def _first_matching_rule(
        self,
        command: str,
        behavior: Optional[CommandBehavior] = None,
        include_segments: bool = False,
    ) -> Optional[CommandRule]:
        candidates = [command]
        if include_segments:
            candidates.extend(segment.strip() for segment in self._SEGMENT_SPLIT_RE.split(command) if segment.strip())

        for rule in self._rules:
            if behavior and rule.behavior != behavior:
                continue
            if any(fnmatch(candidate, rule.pattern) for candidate in candidates):
                return rule
        return None
