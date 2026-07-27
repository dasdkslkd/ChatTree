# providers/model_fetch.py - OpenAI 兼容 /v1/models 端点动态发现
#
# 候选 URL 构造迁移自 cc-switch 的 services/model_fetch.rs：
# - models_url_override 非空 → 单候选
# - base_url 以 /v{N} 结尾（如 /v1、智谱 /api/coding/paas/v4）→ {base}/models
# - 否则 → {base}/v1/models
# - 命中 KNOWN_COMPAT_SUFFIXES 时追加剥离后缀的根 + /v1/models + /models
#
# 该模块对所有 provider（OpenAI 兼容、Anthropic 兼容子路径、智谱 Coding Plan 等）
# 提供统一的模型列表获取能力，避免在多处重复实现简单的 URL 拼接。
import json
import urllib.request
import urllib.error
from typing import List, Dict, Any, Optional

from ...utils.logger import setup_logger

logger = setup_logger('ModelFetch')

# 已知「Anthropic 协议兼容子路径」后缀；按长度降序，最长前缀优先匹配。
# baseURL 命中这些后缀时，候选列表会追加「剥离后缀再拼 /v1/models / /models」的版本。
_KNOWN_COMPAT_SUFFIXES = [
    "/api/claudecode",
    "/api/anthropic",
    "/apps/anthropic",
    "/api/coding",
    "/claudecode",
    "/anthropic",
    "/step_plan",
    "/coding",
    "/claude",
]

_FETCH_TIMEOUT = 15
_ERROR_BODY_MAX_CHARS = 512


def build_models_url_candidates(
    base_url: str,
    models_url_override: Optional[str] = None,
) -> List[str]:
    """构造模型列表端点的候选 URL 列表，按优先级排序。

    迁移自 cc-switch services/model_fetch.rs:build_models_url_candidates。
    """
    if models_url_override and models_url_override.strip():
        return [models_url_override.strip()]

    trimmed = (base_url or "").strip().rstrip("/")
    if not trimmed:
        return []

    candidates: List[str] = []

    # baseURL 已以版本段 /v{N} 结尾时（如 /v1、智谱 /api/coding/paas/v4），
    # OpenAI 惯例的模型端点是 {base}/models，不能再补 /v1。
    if _ends_with_version_segment(trimmed):
        candidates.append(f"{trimmed}/models")
        if not trimmed.endswith("/v1"):
            candidates.append(f"{trimmed}/v1/models")
    else:
        candidates.append(f"{trimmed}/v1/models")

    if stripped := _strip_compat_suffix(trimmed):
        root = stripped.rstrip("/")
        if root and "://" in root:
            candidates.append(f"{root}/v1/models")
            candidates.append(f"{root}/models")

    # 去重保序
    unique: List[str] = []
    for url in candidates:
        if url not in unique:
            unique.append(url)
    return unique


def fetch_models(
    base_url: str,
    api_key: str,
    models_url_override: Optional[str] = None,
    custom_user_agent: Optional[str] = None,
    extra_headers: Optional[Dict[str, str]] = None,
) -> List[Dict[str, Any]]:
    """按候选 URL 列表顺序尝试 GET 模型列表，返回 [{id, owned_by?}]。

    404/405 继续尝试下一个候选；其他 4xx/5xx 立即失败。
    """
    if not api_key and not extra_headers:
        return []

    candidates = build_models_url_candidates(base_url, models_url_override)
    if not candidates:
        return []

    headers: Dict[str, str] = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    if custom_user_agent:
        headers["User-Agent"] = custom_user_agent
    if extra_headers:
        headers.update(extra_headers)

    last_err: Optional[str] = None
    for url in candidates:
        try:
            req = urllib.request.Request(url, headers=headers, method="GET")
            with urllib.request.urlopen(req, timeout=_FETCH_TIMEOUT) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            models = _parse_models(data)
            if models:
                return models
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            body = _truncate(body)
            if exc.code in (404, 405):
                last_err = f"HTTP {exc.code}: {body}"
                continue
            raise RuntimeError(f"HTTP {exc.code}: {body}")
        except Exception as exc:
            raise RuntimeError(f"Request failed: {exc}")

    raise RuntimeError(f"All candidates failed: {last_err or 'no candidates'}")


def _parse_models(data: Any) -> List[Dict[str, Any]]:
    """兼容多种响应 schema：data[]/models[]/models{}/裸数组。"""
    entries: List[Any] = []
    if isinstance(data, dict):
        entries = (
            data.get("data")
            or data.get("models")
            or data.get("items")
            or []
        )
        if isinstance(entries, dict):
            # models 是 map：{model_id: {...}}
            entries = [
                {"id": k, **(v if isinstance(v, dict) else {})}
                for k, v in entries.items()
            ]
    elif isinstance(data, list):
        entries = data

    models: List[Dict[str, Any]] = []
    for entry in entries:
        if isinstance(entry, str):
            if entry.strip():
                models.append({"id": entry.strip(), "owned_by": None})
            continue
        if not isinstance(entry, dict):
            continue
        model_id = (
            entry.get("id")
            or entry.get("slug")
            or entry.get("model")
            or entry.get("name")
        )
        if not model_id or not str(model_id).strip():
            continue
        models.append({
            "id": str(model_id).strip(),
            "owned_by": (
                entry.get("owned_by")
                or entry.get("ownedBy")
                or entry.get("provider")
                or entry.get("vendor")
            ),
        })

    # 去重 + 排序
    seen: set = set()
    unique: List[Dict[str, Any]] = []
    for m in models:
        if m["id"] not in seen:
            seen.add(m["id"])
            unique.append(m)
    unique.sort(key=lambda m: m["id"])
    return unique


def _ends_with_version_segment(url: str) -> bool:
    """判断 URL 是否以 OpenAI 风格的版本段 /v{N} 结尾。"""
    last = url.rsplit("/", 1)[-1]
    if not last.startswith("v"):
        return False
    digits = last[1:]
    return bool(digits) and digits.isdigit()


def _strip_compat_suffix(base_url: str) -> Optional[str]:
    """若 base_url 以任一已知兼容子路径结尾，返回剥离后的剩余部分。"""
    for suffix in _KNOWN_COMPAT_SUFFIXES:
        if base_url.endswith(suffix):
            return base_url[: len(base_url) - len(suffix)]
    return None


def _truncate(body: str) -> str:
    if len(body) <= _ERROR_BODY_MAX_CHARS:
        return body
    return body[:_ERROR_BODY_MAX_CHARS] + "…"
