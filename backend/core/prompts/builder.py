from __future__ import annotations

from typing import Any, Iterable, Optional

from backend.core.capabilities.prompting import (
    build_available_capabilities_prompt,
    build_skill_injections,
    format_skill_injections,
)
from backend.core.capabilities.registry import CapabilityRegistry

from .types import PromptBuildRequest, PromptSection


class PromptBuilder:
    """Compose model-visible prompt sections in one place.

    The builder keeps the conversation history untouched, then inserts generated
    system sections after any leading system messages. This mirrors Codex's
    "base instructions first, contextual instructions next, user history after"
    shape without changing provider APIs.
    """

    def __init__(self, registry: Optional[CapabilityRegistry] = None) -> None:
        self.registry = registry

    def build_messages(
        self,
        base_messages: Iterable[dict[str, Any]],
        *,
        active_skill_names: Iterable[str] = (),
        extra_sections: Iterable[PromptSection] = (),
        capability_char_budget: Optional[int] = None,
        include_available_capabilities: bool = True,
    ) -> list[dict[str, Any]]:
        request = PromptBuildRequest(
            base_messages=base_messages,
            active_skill_names=active_skill_names,
            extra_sections=extra_sections,
            capability_char_budget=capability_char_budget,
            include_available_capabilities=include_available_capabilities,
        )
        return self.build(request)

    def build(self, request: PromptBuildRequest) -> list[dict[str, Any]]:
        messages = [dict(message) for message in request.base_messages]
        sections = self._sections_for_request(request)
        if not sections:
            return messages

        insert_at = self._insertion_index(messages)
        section_messages = [
            section.as_message()
            for section in sorted(sections, key=lambda item: item.priority)
            if section.content
        ]
        messages[insert_at:insert_at] = section_messages
        return messages

    def _sections_for_request(
        self,
        request: PromptBuildRequest,
    ) -> list[PromptSection]:
        sections = list(request.extra_sections)
        if self.registry is None:
            return sections

        char_budget = (
            request.capability_char_budget
            if request.capability_char_budget is not None
            else 8000
        )
        if request.include_available_capabilities:
            capability_prompt = build_available_capabilities_prompt(
                self.registry,
                char_budget=char_budget,
            )
            if capability_prompt:
                sections.append(
                    PromptSection(
                        name="available_capabilities",
                        role="system",
                        content=capability_prompt,
                        priority=20,
                    )
                )

        skill_injections = build_skill_injections(
            request.active_skill_names,
            self.registry,
        )
        skill_prompt = format_skill_injections(skill_injections)
        if skill_prompt:
            sections.append(
                PromptSection(
                    name="skill_injections",
                    role="system",
                    content=skill_prompt,
                    priority=30,
                )
            )
        return sections

    @staticmethod
    def _insertion_index(messages: list[dict[str, Any]]) -> int:
        index = 0
        while index < len(messages) and messages[index].get("role") == "system":
            index += 1
        return index
