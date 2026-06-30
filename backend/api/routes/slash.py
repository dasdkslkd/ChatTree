from __future__ import annotations

from typing import Any, Dict, List

from fastapi import APIRouter

from backend.core.slash import SlashCommandRegistry

router = APIRouter()


@router.get("/slash/commands", response_model=List[Dict[str, Any]])
async def list_slash_commands() -> List[Dict[str, Any]]:
    return SlashCommandRegistry.builtins().public_definitions()

