from __future__ import annotations

import re
from typing import Optional

from .types import SlashParsedInput


SLASH_PATTERN = re.compile(r"^\s*/([A-Za-z0-9_.:-]+)(?=\s|$)(.*)$", re.DOTALL)


def parse_slash_command(text: str) -> Optional[SlashParsedInput]:
    match = SLASH_PATTERN.match(text or "")
    if not match:
        return None
    name = match.group(1)
    rest = match.group(2) or ""
    return SlashParsedInput(
        raw=text,
        name=name,
        args=rest.strip(),
        command_text=f"/{name}",
    )
