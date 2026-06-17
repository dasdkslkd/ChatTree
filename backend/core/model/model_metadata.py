# model/model_metadata.py - 模型元数据注册表
"""模型名 -> 元数据 的解析层。

按 (模型名, api_format) 解析出统一的应用层能力声明：上下文长度、图像支持、
推理强度档位、思考模式开关。前端据此显示/隐藏控件，provider 据此把统一的
规范参数（reasoning_effort / thinking_enabled）翻译成各自 API 的原生形状。

规则是有序的：第一条匹配的规则生效，再与该规则的基础值合并。用户可在
config.json 的顶层 model_metadata 字段下提供覆盖（model_name -> 部分覆盖），
合并在内置解析之上——用于兜底未知第三方模型。
"""
import re
from typing import List, Optional, Dict, Any
from typing_extensions import TypedDict


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


def _matches(model_name: str, api_format: str, name_patterns: List[str],
             api_formats: List[str]) -> bool:
    """命中条件：api_format 在列表中，或模型名（小写）匹配任一子串/正则。"""
    if api_format in api_formats:
        return True
    lowered = model_name.lower()
    return any(re.search(p, lowered) for p in name_patterns)


# 有序规则表。每条：(匹配条件, 该规则的元数据)。第一条命中的生效。
# 顺序很重要——更具体的规则排在更宽泛的前面。
_RULES = [
    # ── Anthropic / Claude ──
    # thinking 可切换 + effort 档位。名含 opus-4-7/4-8/fable/mythos 追加 xhigh/max。
    {
        "name_patterns": [r"claude", r"\bfable\b", r"\bmythos\b"],
        "api_formats": ["anthropic"],
        "meta": lambda name: {
            "context_length": 1_000_000,
            "supports_vision": True,
            "reasoning_effort": {
                # xhigh 仅 Opus 4.7/4.8/Fable/Mythos；4.6/4.5 有 max 无 xhigh；
                # 更早或未知 claude 给保守的 low/medium/high。
                "levels": (
                    ["low", "medium", "high", "xhigh", "max"]
                    if re.search(r"opus-4-[78]|fable|mythos", name.lower())
                    else ["low", "medium", "high", "max"]
                    if re.search(r"opus-4-[56]|sonnet-4-6", name.lower())
                    else ["low", "medium", "high"]
                ),
                "default": None,
            },
            "thinking": {"toggleable": True, "default_enabled": False},
        },
    },
    # ── OpenAI Responses / 推理系模型（gpt-5, o1, o3, o4）──
    # 仅推理强度档位（含 minimal）；思考不单独切换。
    {
        "name_patterns": [r"gpt-5", r"\bo1\b", r"\bo3\b", r"\bo4\b", r"o1-", r"o3-", r"o4-"],
        "api_formats": ["responses"],
        "meta": lambda name: {
            "context_length": None,
            "supports_vision": True,
            "reasoning_effort": {
                "levels": ["minimal", "low", "medium", "high"],
                "default": None,
            },
            "thinking": None,
        },
    },
    # ── Gemini 2.5 / 3（思考型）──
    # thinking 可切换 + 抽象档位（provider 内映射为 thinking_budget 整数）。
    {
        "name_patterns": [r"gemini-2\.5", r"gemini-3", r"gemini-2-5"],
        "api_formats": [],
        "meta": lambda name: {
            "context_length": 1_000_000,
            "supports_vision": True,
            "reasoning_effort": {
                "levels": ["dynamic", "low", "medium", "high"],
                "default": None,
            },
            "thinking": {"toggleable": True, "default_enabled": False},
        },
    },
    # ── 其他 Gemini（无显式思考控件，但支持视觉）──
    {
        "name_patterns": [r"gemini"],
        "api_formats": ["gemini"],
        "meta": lambda name: {
            "context_length": 1_000_000,
            "supports_vision": True,
            "reasoning_effort": None,
            "thinking": None,
        },
    },
    # ── DeepSeek / Qwen 等 chat_completions 推理模型 ──
    # 这些模型通过 enable_thinking 切换思考（网关惯例：DashScope 顶层 enable_thinking，
    # vLLM/SGLang 用 chat_template_kwargs.enable_thinking）。它们默认开启思考，
    # 这里给出可切换开关，默认开启；不走 OpenAI 的 reasoning_effort。
    {
        "name_patterns": [r"deepseek", r"qwen", r"qwq", r"\bglm\b", r"glm-", r"minimax", r"\bkimi\b", r"ernie", r"hunyuan", r"think", r"reason"],
        "api_formats": [],
        "meta": lambda name: {
            "context_length": None,
            "supports_vision": False,
            "reasoning_effort": None,
            "thinking": {"toggleable": True, "default_enabled": True},
        },
    },
]


def _builtin_metadata(model_name: str, api_format: str) -> ModelMetadata:
    """按规则表解析内置元数据；无命中返回兜底。"""
    for rule in _RULES:
        if _matches(model_name, api_format, rule["name_patterns"], rule["api_formats"]):
            meta = dict(_FALLBACK)
            meta.update(rule["meta"](model_name))
            return meta  # type: ignore[return-value]
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
