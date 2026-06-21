"""模型名 -> 元数据 的解析层。

按 (模型名, api_format) 解析出统一的应用层能力声明：上下文长度、图像支持、
推理强度档位、思考模式开关。前端据此显示/隐藏控件，provider 据此把统一的
规范参数（reasoning_effort / thinking_enabled）翻译成各自 API 的原生形状。

内置模型数据放在同目录的 model_metadata.toml。规则是有序的：第一条匹配的
规则生效，再与兜底值合并。用户可在 config.json 的顶层 model_metadata 字段
下提供覆盖（model_name -> 部分覆盖），合并在内置解析之上。
"""
from functools import lru_cache
from pathlib import Path
import re
from typing import List, Optional, Dict, Any
from typing_extensions import TypedDict

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11 fallback
    import tomli as tomllib  # type: ignore[no-redef]


class ReasoningEffortSpec(TypedDict, total=False):
    """推理强度声明。levels 既用于 UI 展示，也是后端校验的合法档位集合。"""
    levels: List[str]            # 例如 ["low", "medium", "high"]
    default: Optional[str]       # 缺省档位；None = 不主动发送


class ThinkingSpec(TypedDict, total=False):
    """思考模式开关声明。"""
    toggleable: bool             # 是否向用户暴露开关
    default_enabled: bool        # 缺省是否开启


class ModelMetadata(TypedDict, total=False):
    """单个模型的统一能力声明。

    reasoning_effort / thinking 为 None（或缺失）表示该模型不暴露对应控件。
    """
    model_id: str
    context_length: Optional[int]
    supports_vision: bool
    reasoning_effort: Optional[ReasoningEffortSpec]
    thinking: Optional[ThinkingSpec]


# 兜底元数据：未匹配任何规则的模型走这里——普通聊天，无推理控件。
_FALLBACK: ModelMetadata = {
    "context_length": None,
    "supports_vision": False,
    "reasoning_effort": None,
    "thinking": None,
}


_METADATA_FILE = Path(__file__).with_name("model_metadata.toml")


def _as_string_list(value: Any) -> List[str]:
    if isinstance(value, list):
        return [str(v) for v in value]
    return []


def _matches(model_name: str, api_format: str, name_patterns: List[str],
             api_formats: List[str]) -> bool:
    """命中条件：模型名匹配；未给模型名规则时可只按 api_format 匹配。"""
    name_match = bool(name_patterns) and any(
        re.search(p, model_name, flags=re.IGNORECASE) for p in name_patterns
    )
    lowered_format = api_format.lower()
    format_match = bool(api_formats) and lowered_format in {
        f.lower() for f in api_formats
    }
    return name_match or (not name_patterns and format_match)


@lru_cache(maxsize=1)
def _load_rules() -> List[Dict[str, Any]]:
    """读取 TOML 规则表。TOML 规范固定 UTF-8，tomllib 使用二进制读取。"""
    with _METADATA_FILE.open("rb") as f:
        data = tomllib.load(f)
    rules = data.get("rules", [])
    return rules if isinstance(rules, list) else []


def _metadata_from_rule(rule: Dict[str, Any]) -> ModelMetadata:
    meta = dict(_FALLBACK)
    for key in ("context_length", "supports_vision"):
        if key in rule:
            meta[key] = rule[key]

    reasoning = rule.get("reasoning_effort")
    if isinstance(reasoning, dict):
        spec: ReasoningEffortSpec = {
            "levels": _as_string_list(reasoning.get("levels")),
            "default": reasoning.get("default"),
        }
        meta["reasoning_effort"] = spec
    elif "reasoning_effort" in rule:
        meta["reasoning_effort"] = None

    thinking = rule.get("thinking")
    if isinstance(thinking, dict):
        meta["thinking"] = {
            "toggleable": bool(thinking.get("toggleable", False)),
            "default_enabled": bool(thinking.get("default_enabled", False)),
        }
    elif "thinking" in rule:
        meta["thinking"] = None

    return meta  # type: ignore[return-value]


def _builtin_metadata(model_name: str, api_format: str) -> ModelMetadata:
    """按规则表解析内置元数据；无命中返回兜底。"""
    for rule in _load_rules():
        name_patterns = _as_string_list(rule.get("name_patterns"))
        api_formats = _as_string_list(rule.get("api_formats"))
        if _matches(model_name, api_format, name_patterns, api_formats):
            return _metadata_from_rule(rule)
    return dict(_FALLBACK)  # type: ignore[return-value]


def _apply_override(meta: ModelMetadata, override: Dict[str, Any]) -> ModelMetadata:
    """把用户覆盖浅合并到解析结果之上（仅覆盖显式提供的键）。"""
    merged = dict(meta)
    for key in ("context_length", "supports_vision", "reasoning_effort", "thinking"):
        if key in override:
            merged[key] = override[key]
    return merged  # type: ignore[return-value]


def resolve_metadata(
    model_name: str,
    api_format: str,
    user_overrides: Optional[Dict[str, Dict[str, Any]]] = None,
) -> ModelMetadata:
    """解析单个模型的元数据。

    Args:
        model_name: 模型名（如 "claude-opus-4-8"、"gpt-5"、"gemini-2.5-pro"）。
        api_format: 提供商的 api_format（chat_completions/responses/anthropic/gemini）。
        user_overrides: config.json 的 model_metadata 字段，model_name -> 部分覆盖。

    Returns:
        合并后的 ModelMetadata，始终带上 model_id。
    """
    meta = _builtin_metadata(model_name, api_format)
    if user_overrides and model_name in user_overrides:
        meta = _apply_override(meta, user_overrides[model_name])
    meta["model_id"] = model_name
    return meta


def resolve_provider_metadata(
    models: List[str],
    api_format: str,
    user_overrides: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, ModelMetadata]:
    """批量解析一个提供商下所有模型的元数据，返回 model_name -> 元数据。"""
    return {
        m: resolve_metadata(m, api_format, user_overrides)
        for m in models
    }


def normalize_effort(
    effort: Optional[str],
    meta: ModelMetadata,
) -> Optional[str]:
    """校验请求的推理强度：不被模型支持则丢弃（返回 None）。"""
    if not effort:
        return None
    spec = meta.get("reasoning_effort")
    if not spec:
        return None
    levels = spec.get("levels") or []
    return effort if effort in levels else None


def normalize_thinking(
    thinking_enabled: Optional[bool],
    meta: ModelMetadata,
) -> Optional[bool]:
    """校验思考开关：模型不支持切换则丢弃（返回 None）。"""
    if thinking_enabled is None:
        return None
    spec = meta.get("thinking")
    if not spec or not spec.get("toggleable"):
        return None
    return thinking_enabled
