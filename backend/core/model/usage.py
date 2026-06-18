from typing import Any, Dict, Optional

from ..config.types import UsageInfo


def _to_plain(value: Any) -> Any:
    """Convert SDK response models to JSON-serializable Python values."""
    if value is None:
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _to_plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_plain(v) for v in value]
    if hasattr(value, "model_dump"):
        return _to_plain(value.model_dump())
    if hasattr(value, "to_dict"):
        return _to_plain(value.to_dict())
    if hasattr(value, "__dict__"):
        return {
            str(k): _to_plain(v)
            for k, v in vars(value).items()
            if not k.startswith("_")
        }
    return str(value)


def _int_value(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _get(data: Dict[str, Any], *path: str) -> Any:
    cur: Any = data
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def usage_from_openai(raw_usage: Any, source: str = "api") -> Optional[UsageInfo]:
    raw = _to_plain(raw_usage)
    if not isinstance(raw, dict):
        return None

    input_tokens = _int_value(raw.get("input_tokens", raw.get("prompt_tokens")))
    output_tokens = _int_value(raw.get("output_tokens", raw.get("completion_tokens")))
    total_tokens = _int_value(raw.get("total_tokens"), input_tokens + output_tokens)
    cached_tokens = _int_value(
        _get(raw, "input_tokens_details", "cached_tokens"),
        _int_value(_get(raw, "prompt_tokens_details", "cached_tokens")),
    )
    reasoning_tokens = _int_value(
        _get(raw, "output_tokens_details", "reasoning_tokens"),
        _int_value(_get(raw, "completion_tokens_details", "reasoning_tokens")),
    )

    return UsageInfo(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        cached_tokens=cached_tokens,
        reasoning_tokens=reasoning_tokens,
        source=source,
        raw=raw,
    )


def usage_from_anthropic(raw_usage: Any, source: str = "api") -> Optional[UsageInfo]:
    raw = _to_plain(raw_usage)
    if not isinstance(raw, dict):
        return None

    input_tokens = _int_value(raw.get("input_tokens"))
    output_tokens = _int_value(raw.get("output_tokens"))
    cache_creation = _int_value(raw.get("cache_creation_input_tokens"))
    cache_read = _int_value(raw.get("cache_read_input_tokens"))
    total_tokens = input_tokens + output_tokens + cache_creation + cache_read

    return UsageInfo(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        cache_creation_input_tokens=cache_creation,
        cache_read_input_tokens=cache_read,
        source=source,
        raw=raw,
    )


def usage_from_gemini(raw_usage: Any, source: str = "api") -> Optional[UsageInfo]:
    raw = _to_plain(raw_usage)
    if not isinstance(raw, dict):
        return None

    input_tokens = _int_value(raw.get("prompt_token_count"))
    output_tokens = _int_value(raw.get("candidates_token_count"))
    total_tokens = _int_value(raw.get("total_token_count"), input_tokens + output_tokens)

    return UsageInfo(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        source=source,
        raw=raw,
    )


def estimated_usage(total_tokens: int, output_tokens: Optional[int] = None) -> UsageInfo:
    output = _int_value(output_tokens, total_tokens)
    total = _int_value(total_tokens, output)
    return UsageInfo(
        input_tokens=max(total - output, 0),
        output_tokens=output,
        total_tokens=total,
        source="estimate",
        raw={},
    )


def usage_total(usage_info: Optional[UsageInfo], fallback: int = 0) -> int:
    if not usage_info:
        return _int_value(fallback)
    return _int_value(usage_info.get("total_tokens"), fallback)


def add_usage(left: Optional[UsageInfo], right: Optional[UsageInfo]) -> UsageInfo:
    result = UsageInfo(
        input_tokens=0,
        output_tokens=0,
        total_tokens=0,
        cached_tokens=0,
        reasoning_tokens=0,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
        source="aggregate",
        raw={},
    )
    for usage in (left, right):
        if not usage:
            continue
        for key in (
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "cached_tokens",
            "reasoning_tokens",
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
        ):
            result[key] = _int_value(result.get(key)) + _int_value(usage.get(key))
    return result
