from __future__ import annotations

from typing import Any, Iterable, Optional

from backend.core.capabilities.prompting import (
    build_available_capabilities_prompt,
    build_skill_injections,
    format_skill_injections,
)
from backend.core.capabilities.registry import CapabilityRegistry

from .catalog import load_prompt_template
from .types import PromptBuildRequest, PromptSection, RuntimePromptContext


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
        runtime_context: Optional[RuntimePromptContext] = None,
        capability_char_budget: Optional[int] = None,
        include_available_capabilities: bool = True,
        custom_system_prompt: Optional[str] = None,
        custom_system_prompt_mode: str = "override",
    ) -> list[dict[str, Any]]:
        request = PromptBuildRequest(
            base_messages=base_messages,
            active_skill_names=active_skill_names,
            extra_sections=extra_sections,
            runtime_context=runtime_context,
            capability_char_budget=capability_char_budget,
            include_available_capabilities=include_available_capabilities,
            include_core_prompt=True,
            custom_system_prompt=custom_system_prompt,
            custom_system_prompt_mode=custom_system_prompt_mode,
            runtime_mode="main",
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
        custom_prompt = (request.custom_system_prompt or "").strip()
        custom_mode = self._normalize_custom_system_prompt_mode(
            request.custom_system_prompt_mode
        )
        if custom_prompt and custom_mode == "override":
            sections.append(
                PromptSection(
                    name="custom_system_prompt",
                    role="system",
                    content=custom_prompt,
                    priority=10,
                )
            )
        elif request.include_core_prompt:
            sections.append(
                PromptSection(
                    name="core_prompt",
                    role="system",
                    content=load_prompt_template("core"),
                    priority=10,
                    metadata={"runtime_mode": request.runtime_mode},
                )
            )
        if request.runtime_context is not None:
            sections.append(request.runtime_context.as_section(priority=15))
        if self.registry is None:
            if custom_prompt and custom_mode == "append":
                sections.append(
                    PromptSection(
                        name="custom_system_prompt",
                        role="system",
                        content=custom_prompt,
                        priority=100,
                    )
                )
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
        if custom_prompt and custom_mode == "append":
            sections.append(
                PromptSection(
                    name="custom_system_prompt",
                    role="system",
                    content=custom_prompt,
                    priority=100,
                )
            )
        return sections

    @staticmethod
    def _insertion_index(messages: list[dict[str, Any]]) -> int:
        index = 0
        while index < len(messages) and messages[index].get("role") == "system":
            index += 1
        return index

    @staticmethod
    def _normalize_custom_system_prompt_mode(mode: str) -> str:
        return mode if mode in {"override", "append"} else "override"
