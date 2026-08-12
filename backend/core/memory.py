from __future__ import annotations

import hashlib
import os
import re
import tempfile
import threading
import unicodedata
import uuid
from pathlib import Path
from typing import Any


class MemoryStore:
    MAX_VIEW_BYTES = 64 * 1024
    LIMITS = {
        "user": (24, 2000, 500),
        "machine": (20, 1600, 500),
        "project": (40, 3600, 600),
    }
    HEADINGS = {
        "user": "# User Memory",
        "machine": "# Machine Memory",
        "project": "# Project Memory",
    }

    def __init__(self, home: str | Path) -> None:
        self.root = Path(home).resolve() / "memories"
        self._write_lock = threading.Lock()

    def inspect(self, scope: str, project_id: str = "") -> dict[str, Any]:
        path = self._path(scope, project_id)
        path_is_safe = self._path_is_safe(path)
        result: dict[str, Any] = {
            "scope": scope,
            "name": path.name,
            "path": str(path),
            "exists": path.exists(),
            "valid": path_is_safe,
            "truncated": False,
            "content": "",
            "error": None if path_is_safe else "unsafe path",
            "entries": [],
        }
        if not result["exists"] or not path_is_safe:
            return result
        try:
            with path.open("rb") as handle:
                data = handle.read(self.MAX_VIEW_BYTES + 1)
        except OSError:
            result.update(valid=False, error="io error")
            return result

        result["truncated"] = len(data) > self.MAX_VIEW_BYTES
        data = data[: self.MAX_VIEW_BYTES]
        try:
            content = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            if not result["truncated"] or exc.reason != "unexpected end of data":
                result.update(valid=False, error="invalid utf-8")
                return result
            content = data[:exc.start].decode("utf-8")
        result["content"] = content
        if result["truncated"]:
            result.update(valid=False, error="too large")
            return result

        entries = self._parse(scope, content)
        if entries is None:
            result.update(valid=False, error="invalid format")
            return result
        result["entries"] = entries
        return result

    def snapshot(self, project_id: str = "") -> dict[str, list[str]]:
        snapshot: dict[str, list[str]] = {}
        for scope in ("user", "machine", "project"):
            if scope == "project" and not project_id:
                continue
            result = self.inspect(scope, project_id)
            if result["valid"] and result["entries"]:
                snapshot[scope] = result["entries"]
        return snapshot

    def update(
        self,
        action: Any,
        scope: Any,
        content: Any = "",
        old_text: Any = "",
        project_id: str = "",
    ) -> str:
        if (
            not isinstance(action, str)
            or not isinstance(scope, str)
            or not isinstance(content, str)
            or not isinstance(old_text, str)
            or action not in {"add", "replace", "remove"}
            or scope not in self.LIMITS
        ):
            return "error: invalid"
        if (
            (action == "add" and (not content or old_text))
            or (action == "replace" and (not content or not old_text))
            or (action == "remove" and (content or not old_text))
            or len(content) > 600
            or len(old_text) > 200
        ):
            return "error: invalid"
        if any(unicodedata.category(char) in {"Cc", "Cf"} for char in content + old_text):
            return "error: invalid"

        content = " ".join(content.split())
        old_text = " ".join(old_text.split())
        if (action in {"add", "replace"} and not content) or (action != "add" and not old_text):
            return "error: invalid"
        if content and self._is_sensitive(content):
            return "error: sensitive"

        try:
            path = self._path(scope, project_id)
        except ValueError:
            return "error: unavailable" if scope == "project" else "error: invalid"

        with self._write_lock:
            return self._update_file(path, action, scope, content, old_text)

    def _path(self, scope: str, project_id: str) -> Path:
        if scope == "user":
            return self.root / "USER.md"
        if scope == "machine":
            return self.root / "MACHINE.md"
        if scope != "project":
            raise ValueError("invalid memory scope")
        try:
            normalized_id = str(uuid.UUID(project_id))
        except ValueError as exc:
            raise ValueError("invalid project id") from exc
        return self.root / "projects" / f"{normalized_id}.md"

    def _parse(self, scope: str, content: str, *, enforce_limits: bool = True) -> list[str] | None:
        if not content:
            return []
        lines = [line for line in content.splitlines() if line.strip()]
        if not lines or lines[0] != self.HEADINGS[scope]:
            return None
        entries: list[str] = []
        for line in lines[1:]:
            if not line.startswith("- "):
                return None
            entry = " ".join(line[2:].split())
            if not entry or any(
                unicodedata.category(char) in {"Cc", "Cf"}
                for char in entry
            ):
                return None
            entries.append(entry)
        if enforce_limits and self._entries_error(scope, entries, check_sensitive=True):
            return None
        return entries

    def _update_file(
        self,
        path: Path,
        action: str,
        scope: str,
        content: str,
        old_text: str,
    ) -> str:
        if not self._path_is_safe(path):
            return "error: unavailable"
        try:
            existed = path.exists()
            if existed and not path.is_file():
                return "error: unavailable"
            original = path.read_bytes() if existed else b""
        except OSError:
            return "error: io"
        if len(original) > self.MAX_VIEW_BYTES:
            return "error: full"
        try:
            text = original.decode("utf-8")
        except UnicodeDecodeError:
            return "error: invalid"
        entries = self._parse(scope, text, enforce_limits=False)
        if entries is None:
            return "error: invalid"
        existing_error = self._entries_error(scope, entries, check_sensitive=True)
        if existing_error:
            return f"error: {existing_error}"

        updated = list(entries)
        if action == "add":
            if content in updated:
                return "ok"
            updated.append(content)
        else:
            needle = old_text.casefold()
            matches = [index for index, entry in enumerate(updated) if needle in entry.casefold()]
            if not matches:
                return "error: not found"
            if len(matches) > 1:
                return "error: ambiguous"
            index = matches[0]
            if action == "replace":
                if updated[index] == content:
                    return "ok"
                updated[index] = content
            else:
                updated.pop(index)

        error = self._entries_error(scope, updated, check_sensitive=True)
        if error:
            return f"error: {error}"
        rendered = self.HEADINGS[scope] + "\n"
        if updated:
            rendered += "\n" + "\n".join(f"- {entry}" for entry in updated) + "\n"
        encoded = rendered.encode("utf-8")
        fingerprint = (existed, hashlib.sha256(original).digest())
        temp_path: Path | None = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if not self._path_is_safe(path):
                return "error: unavailable"
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temp_path = Path(handle.name)
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            current_exists = path.exists()
            current = path.read_bytes() if current_exists else b""
            if (current_exists, hashlib.sha256(current).digest()) != fingerprint:
                return "error: conflict"
            if not self._path_is_safe(path):
                return "error: unavailable"
            os.replace(temp_path, path)
            temp_path = None
            return "ok"
        except OSError:
            return "error: io"
        finally:
            if temp_path is not None:
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    pass

    def _entries_error(
        self,
        scope: str,
        entries: list[str],
        *,
        check_sensitive: bool = False,
    ) -> str | None:
        max_entries, max_chars, max_entry_chars = self.LIMITS[scope]
        if (
            len(entries) > max_entries
            or sum(map(len, entries)) > max_chars
            or any(len(entry) > max_entry_chars for entry in entries)
        ):
            return "full"
        if len(entries) != len(set(entries)):
            return "invalid"
        if check_sensitive and any(self._is_sensitive(entry) for entry in entries):
            return "sensitive"
        return None

    def _path_is_safe(self, path: Path) -> bool:
        try:
            path.resolve(strict=False).relative_to(self.root.resolve(strict=False))
        except (OSError, ValueError):
            return False
        candidate = self.root
        relative_parts = path.relative_to(self.root).parts
        for part in relative_parts:
            if candidate.exists() and (
                candidate.is_symlink()
                or bool(getattr(os.path, "isjunction", lambda _: False)(candidate))
            ):
                return False
            candidate /= part
        return not candidate.exists() or not (
            candidate.is_symlink()
            or bool(getattr(os.path, "isjunction", lambda _: False)(candidate))
        )

    @staticmethod
    def _is_sensitive(content: str) -> bool:
        return bool(
            re.search(r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----", content, re.IGNORECASE)
            or re.search(
                r"\b(?:api[_-]?key|token|secret|password|private[_-]?key|cookie|auth)\s*[:=]\s*[^\s`$<{]+",
                content,
                re.IGNORECASE,
            )
            or re.search(
                r"\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}|npm_[A-Za-z0-9]{20,}|xox[baprs]-[A-Za-z0-9-]{16,})\b",
                content,
            )
            or re.search(
                r"\b(?:ignore (?:all |the )?(?:previous|system) instructions?|reveal (?:the )?(?:system prompt|hidden context)|bypass (?:tool )?permissions?)\b",
                content,
                re.IGNORECASE,
            )
        )
