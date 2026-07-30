"""模型级协议路由与能力目录。

运行时只读取 Server Home 中的 ``model_metadata.toml``。随程序发布的文件
仅用于首次初始化；未知模型统一退回 Chat Completions，不启用 reasoning。
"""
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import re
import shutil
from typing import Any, Dict, List, Optional
from typing_extensions import TypedDict

from backend.core.config.types import ModelProtocol, ModelRoute, ReasoningProfile
from backend.core.persistence.home import resolve_chattree_home

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore[no-redef]


class ModelRouteError(ValueError):
    """模型元数据包含不可用的协议路由。"""


class ReasoningEffortSpec(TypedDict, total=False):
    levels: List[str]
    default: Optional[str]


class ThinkingSpec(TypedDict, total=False):
    toggleable: bool
    default_enabled: bool


class ModelMetadata(TypedDict, total=False):
    model_id: str
    route_id: str
    protocol: str
    endpoint: str
    context_length: Optional[int]
    supports_vision: bool
    supports_tools: bool
    reasoning_effort: Optional[ReasoningEffortSpec]
    thinking: Optional[ThinkingSpec]
    reasoning_profile: ReasoningProfile


_FALLBACK: ModelMetadata = {
    "context_length": None,
    "supports_vision": False,
    "supports_tools": True,
    "reasoning_effort": None,
    "thinking": None,
}

_DEFAULT_ENDPOINTS = {
    ModelProtocol.OPENAI_CHAT_COMPLETIONS.value: "/chat/completions",
    ModelProtocol.OPENAI_RESPONSES.value: "/responses",
    ModelProtocol.ANTHROPIC_MESSAGES.value: "/v1/messages",
    ModelProtocol.GEMINI_GENERATE_CONTENT.value: "/models/{model}:generateContent",
}
_BUILTIN_METADATA_FILE = Path(__file__).with_name("model_metadata.toml")


def initialize_model_metadata(home: str | Path | None = None) -> Path:
    """首次启动时创建可独立更新的 Server Home 模型元数据。"""
    metadata_file = resolve_chattree_home(home) / _BUILTIN_METADATA_FILE.name
    if not metadata_file.is_file():
        metadata_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(_BUILTIN_METADATA_FILE, metadata_file)
    return metadata_file


def _as_string_list(value: Any) -> List[str]:
    return [str(item) for item in value] if isinstance(value, list) else []


def _matches(model_name: str, name_patterns: List[str]) -> bool:
    candidates = {model_name, model_name.rsplit("/", 1)[-1]}
    return bool(name_patterns) and any(
        re.search(pattern, candidate, flags=re.IGNORECASE)
        for candidate in candidates
        for pattern in name_patterns
    )


@lru_cache(maxsize=8)
def _load_catalog(content: bytes) -> Dict[str, Any]:
    data = tomllib.loads(content.decode("utf-8"))
    profiles = data.get("reasoning_profiles")
    rules = data.get("rules")
    return {
        "reasoning_profiles": profiles if isinstance(profiles, dict) else {},
        "rules": rules if isinstance(rules, list) else [],
    }


def _catalog() -> Dict[str, Any]:
    return _load_catalog(initialize_model_metadata().read_bytes())


def _reasoning_profile(
    value: Any,
    profiles: Dict[str, Any],
) -> ReasoningProfile:
    if isinstance(value, str):
        raw = profiles.get(value) or {}
        return {
            "name": value,
            "carrier": str(raw.get("carrier") or "none"),
            "history_policy": str(raw.get("history_policy") or "drop"),
            "strict": bool(raw.get("strict", False)),
            "controls": dict(raw.get("controls") or {}),
        }
    if isinstance(value, dict):
        return {
            "name": str(value.get("name") or "custom"),
            "carrier": str(value.get("carrier") or "none"),
            "history_policy": str(value.get("history_policy") or "drop"),
            "strict": bool(value.get("strict", False)),
            "controls": dict(value.get("controls") or {}),
        }
    return {
        "name": "none",
        "carrier": "none",
        "history_policy": "drop",
        "strict": False,
        "controls": {},
    }


def _metadata_from_entry(entry: Dict[str, Any], profiles: Dict[str, Any]) -> ModelMetadata:
    meta = dict(_FALLBACK)
    for key in ("context_length", "supports_vision", "supports_tools"):
        if key in entry:
            meta[key] = entry[key]

    reasoning = entry.get("reasoning_effort")
    if isinstance(reasoning, dict):
        meta["reasoning_effort"] = {
            "levels": _as_string_list(reasoning.get("levels")),
            "default": reasoning.get("default"),
        }
    elif "reasoning_effort" in entry:
        meta["reasoning_effort"] = None

    thinking = entry.get("thinking")
    if isinstance(thinking, dict):
        meta["thinking"] = {
            "toggleable": bool(thinking.get("toggleable", False)),
            "default_enabled": bool(thinking.get("default_enabled", False)),
        }
    elif "thinking" in entry:
        meta["thinking"] = None

    meta["reasoning_profile"] = _reasoning_profile(
        entry.get("reasoning_profile"),
        profiles,
    )
    return meta  # type: ignore[return-value]


def _metadata_entry(
    model_name: str,
    catalog: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    for rule in catalog["rules"]:
        if isinstance(rule, dict) and _matches(
            model_name,
            _as_string_list(rule.get("name_patterns")),
        ):
            return dict(rule)
    return None


def _route_from_entry(
    provider_id: str,
    model_name: str,
    entry: Dict[str, Any],
    profiles: Dict[str, Any],
) -> ModelRoute:
    protocol = str(entry.get("protocol") or "")
    if protocol not in _DEFAULT_ENDPOINTS:
        raise ModelRouteError(
            f"模型 {provider_id}/{model_name} 的协议无效: {protocol or '未声明'}"
        )
    endpoint = str(entry.get("endpoint") or _DEFAULT_ENDPOINTS[protocol])
    capabilities = entry.get("capabilities")
    metadata_entry = {
        **(capabilities if isinstance(capabilities, dict) else {}),
        **entry,
    }
    metadata = _metadata_from_entry(metadata_entry, profiles)
    capabilities = {
        "context_length": metadata.get("context_length"),
        "supports_vision": bool(metadata.get("supports_vision", False)),
        "supports_tools": bool(metadata.get("supports_tools", True)),
        "reasoning_effort": metadata.get("reasoning_effort"),
        "thinking": metadata.get("thinking"),
    }
    reasoning_profile = metadata.get("reasoning_profile") or _reasoning_profile(None, {})
    fingerprint = hashlib.sha256(json.dumps(
        {
            "protocol": protocol,
            "endpoint": endpoint,
            "capabilities": capabilities,
            "reasoning_profile": reasoning_profile,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")).hexdigest()[:12]
    return ModelRoute(
        route_id=f"{provider_id}:{model_name}:{fingerprint}",
        provider_id=provider_id,
        model_id=model_name,
        protocol=protocol,
        endpoint=endpoint,
        capabilities=capabilities,
        reasoning_profile=reasoning_profile,
    )


def resolve_route(
    provider_id: str,
    model_name: str,
) -> ModelRoute:
    """从 Server Home 元数据解析路由，未知模型退回普通 Chat 对话。"""
    catalog = _catalog()
    entry = _metadata_entry(model_name, catalog)
    if entry is None or not entry.get("protocol"):
        entry = {
            "protocol": ModelProtocol.OPENAI_CHAT_COMPLETIONS.value,
            "endpoint": _DEFAULT_ENDPOINTS[
                ModelProtocol.OPENAI_CHAT_COMPLETIONS.value
            ],
        }
    return _route_from_entry(
        provider_id,
        model_name,
        entry,
        catalog["reasoning_profiles"],
    )


def resolve_metadata(route: ModelRoute) -> ModelMetadata:
    capabilities = route.get("capabilities") or {}
    return ModelMetadata(
        model_id=route["model_id"],
        route_id=route["route_id"],
        protocol=route["protocol"],
        endpoint=route["endpoint"],
        context_length=capabilities.get("context_length"),
        supports_vision=bool(capabilities.get("supports_vision", False)),
        supports_tools=bool(capabilities.get("supports_tools", True)),
        reasoning_effort=capabilities.get("reasoning_effort"),
        thinking=capabilities.get("thinking"),
        reasoning_profile=route.get("reasoning_profile") or _reasoning_profile(None, {}),
    )


def resolve_provider_metadata(
    routes: List[ModelRoute],
) -> Dict[str, ModelMetadata]:
    return {route["model_id"]: resolve_metadata(route) for route in routes}


def normalize_effort(
    effort: Optional[str],
    meta: ModelMetadata,
) -> Optional[str]:
    if not effort:
        return None
    spec = meta.get("reasoning_effort")
    if not spec:
        return None
    return effort if effort in (spec.get("levels") or []) else None


def normalize_thinking(
    thinking_enabled: Optional[bool],
    meta: ModelMetadata,
) -> Optional[bool]:
    if thinking_enabled is None:
        return None
    spec = meta.get("thinking")
    if not spec or not spec.get("toggleable"):
        return None
    return thinking_enabled
