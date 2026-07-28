# auth/subscription.py - 订阅式 provider 登录、模型发现、额度查询
#
# 设计迁移自 cc-switch（src-tauri/src/proxy/providers/codex_oauth_auth.rs、
# copilot_auth.rs）与 opencode（plugin/openai/codex.ts、plugin/github-copilot/）。
#
# 设计取舍：
# - 凭据直接存于 config.json 的 provider[id].auth 字段，不引入独立 auth.json
# - 单账号即可（ChatTree 不是切换器），多账号场景让用户复制 provider
# - 各订阅通过 subscription 字符串分支，不引入 plugin 体系
# - Device Code Flow 优先（无需本地回调服务器），Codex/Copilot 均用此方式
import asyncio
import base64
import json
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Optional, Tuple

from ..utils.logger import setup_logger

logger = setup_logger('Subscription')


class SubscriptionError(RuntimeError):
    def __init__(self, message: str, *, status: int = 0) -> None:
        super().__init__(message)
        self.status = status


# ─── Codex (ChatGPT Plus/Pro) ───────────────────────────────────────────────
CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
CODEX_ISSUER = "https://auth.openai.com"
CODEX_USERCODE_URL = f"{CODEX_ISSUER}/api/accounts/deviceauth/usercode"
CODEX_TOKEN_POLL_URL = f"{CODEX_ISSUER}/api/accounts/deviceauth/token"
CODEX_OAUTH_TOKEN_URL = f"{CODEX_ISSUER}/oauth/token"
CODEX_DEVICE_REDIRECT_URI = f"{CODEX_ISSUER}/deviceauth/callback"
CODEX_VERIFICATION_URI = f"{CODEX_ISSUER}/codex/device"
CODEX_API_BASE = "https://chatgpt.com/backend-api/codex"
CODEX_ORIGINATOR = "codex_cli_rs"
CODEX_CLIENT_VERSION = "0.144.1"

# ─── GitHub Copilot ─────────────────────────────────────────────────────────
GITHUB_CLIENT_ID_GITHUB_COM = "Iv1.b507a08c87ecfe98"
GITHUB_CLIENT_ID_GHES = "Ov23li8tweQw6odWQebz"
COPILOT_API_VERSION = "2025-10-01"
COPILOT_API_BASE_GITHUB_COM = "https://api.githubcopilot.com"

# ─── Gemini (CLI 凭据复用) ─────────────────────────────────────────────────
GEMINI_CLIENT_ID = "681255809395-oo8ft2oprdrnp9e3aqf6av3hmdib135j.apps.googleusercontent.com"
GEMINI_CLIENT_SECRET = "REMOVED"
GEMINI_TOKEN_URL = "https://oauth2.googleapis.com/token"

_EAGER_REFRESH_MS = 5 * 60 * 1000  # 提前 5 分钟主动刷新
_HTTP_TIMEOUT = 15


# ═══════════════════════════════════════════════════════════════════════════
# 公共 API
# ═══════════════════════════════════════════════════════════════════════════
async def start_login(subscription: str, **kwargs) -> Dict[str, Any]:
    """启动 OAuth 登录流程，返回前端需要的 handle 信息。"""
    if subscription == "codex":
        return await _codex_start_login()
    if subscription == "copilot":
        return await _copilot_start_login(kwargs.get("enterprise_domain"))
    raise ValueError(f"Unknown subscription: {subscription}")


async def poll_login(subscription: str, handle: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """轮询登录结果；成功返回 AuthInfo，进行中返回 None，失败抛异常。"""
    if subscription == "codex":
        return await _codex_poll_login(handle)
    if subscription == "copilot":
        return await _copilot_poll_login(handle)
    raise ValueError(f"Unknown subscription: {subscription}")


async def get_valid_token(auth: Dict[str, Any]) -> Tuple[str, Dict[str, str]]:
    """返回 (access_token, extra_headers)；必要时自动刷新。

    extra_headers 包含订阅专用头（如 Codex 的 originator/client_version/account_id，
    Copilot 的 X-GitHub-Api-Version 等）。
    """
    sub = auth.get("subscription")
    if sub == "codex":
        return await _codex_get_valid_token(auth)
    if sub == "copilot":
        return await _copilot_get_valid_token(auth)
    # claude/gemini 走 CLI 凭据复用，token 由原 CLI 负责刷新
    token = auth.get("access", "")
    return token, {}


def get_valid_token_sync(auth: Dict[str, Any]) -> Tuple[str, Dict[str, str]]:
    """同步版 get_valid_token，供同步代码路径（如 _headers）使用。

    在已有 event loop 中调用时，会在独立线程中运行协程。
    """
    if not auth.get("subscription"):
        return "", {}
    try:
        asyncio.get_running_loop()
        # 在已有 event loop 中 — 用独立线程运行协程
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, get_valid_token(auth)).result()
    except RuntimeError:
        # 无 event loop — 直接 asyncio.run
        return asyncio.run(get_valid_token(auth))


async def fetch_models(auth: Dict[str, Any]) -> list:
    """动态发现订阅可用模型列表，返回 [{"id": ...}]。"""
    sub = auth.get("subscription")
    if sub == "codex":
        return await _codex_fetch_models(auth)
    if sub == "copilot":
        return await _copilot_fetch_models(auth)
    raise ValueError(f"Unknown subscription: {sub}")


def fetch_models_sync(auth: Dict[str, Any]) -> list:
    """同步版 fetch_models，供同步代码路径使用。"""
    if not auth.get("subscription"):
        return []
    try:
        asyncio.get_running_loop()
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, fetch_models(auth)).result()
    except RuntimeError:
        return asyncio.run(fetch_models(auth))


async def query_quota(auth: Dict[str, Any]) -> Dict[str, Any]:
    """查询订阅额度。"""
    sub = auth.get("subscription")
    if sub == "codex":
        return await _codex_query_quota(auth)
    if sub == "copilot":
        return await _copilot_query_quota(auth)
    if sub == "claude":
        return await _claude_query_quota(auth)
    if sub == "gemini":
        return await _gemini_query_quota(auth)
    raise ValueError(f"Unknown subscription: {sub}")


async def read_cli_credentials(subscription: str) -> Optional[Dict[str, Any]]:
    """从 CLI 工具的本地凭据文件读取，免登录复用。"""
    if subscription == "claude":
        return _read_claude_cli_credentials()
    if subscription == "codex":
        return _read_codex_cli_credentials()
    if subscription == "gemini":
        return await _read_gemini_cli_credentials()
    return None


# ═══════════════════════════════════════════════════════════════════════════
# Codex (ChatGPT Plus/Pro)
# ═══════════════════════════════════════════════════════════════════════════
async def _codex_start_login() -> Dict[str, Any]:
    """申请 device code，返回 handle。"""
    resp = await _http_post_json(
        CODEX_USERCODE_URL,
        body={"client_id": CODEX_CLIENT_ID},
    )
    return {
        "subscription": "codex",
        "device_auth_id": resp["device_auth_id"],
        "user_code": resp["user_code"],
        "verification_uri": CODEX_VERIFICATION_URI,
        "interval": resp.get("interval", 5),
        "expires_at": int(time.time() * 1000) + resp.get("expires_in", 900) * 1000,
    }


async def _codex_poll_login(handle: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """轮询 device auth → 拿 authorization_code → 换 tokens → 解析 account_id。"""
    now_ms = int(time.time() * 1000)
    if now_ms >= handle["expires_at"]:
        raise RuntimeError("Device code expired")

    resp = await _http_post_json(
        CODEX_TOKEN_POLL_URL,
        body={
            "device_auth_id": handle["device_auth_id"],
            "user_code": handle["user_code"],
        },
    )
    # 未完成时 OpenAI 返回 error 字段
    if resp.get("error") or not resp.get("authorization_code"):
        return None

    auth_code = resp["authorization_code"]
    code_verifier = resp.get("code_verifier", "")

    tokens = await _http_post_json(
        CODEX_OAUTH_TOKEN_URL,
        body={
            "grant_type": "authorization_code",
            "code": auth_code,
            "redirect_uri": CODEX_DEVICE_REDIRECT_URI,
            "client_id": CODEX_CLIENT_ID,
            "code_verifier": code_verifier,
        },
    )

    claims = _decode_jwt_claims(tokens.get("id_token", ""))
    auth_claim = claims.get("https://api.openai.com/auth") or {}
    expires_at = now_ms + int(tokens.get("expires_in", 3600)) * 1000
    return {
        "type": "oauth",
        "subscription": "codex",
        "access": tokens["access_token"],
        "refresh": tokens.get("refresh_token", ""),
        "expires": expires_at,
        "account_id": str(
            auth_claim.get("chatgpt_account_id")
            or claims.get("chatgpt_account_id")
            or ""
        ),
        "account_name": str(claims.get("name") or claims.get("nickname") or ""),
        "account_email": str(claims.get("email") or ""),
    }


async def _codex_get_valid_token(auth: Dict[str, Any]) -> Tuple[str, Dict[str, str]]:
    """返回 Codex access_token + 订阅专用头。

    Codex CLI token 实际有效期常超过 last_refresh + 8 天的保守估计，
    refresh_token 失效时回退到原 access_token——由 API 401 判定真正过期。
    """
    if _should_refresh(auth):
        try:
            await _codex_refresh_token(auth)
        except Exception:
            if not auth.get("access"):
                raise
    return auth["access"], {
        "ChatGPT-Account-Id": auth.get("account_id", ""),
        "originator": CODEX_ORIGINATOR,
        "client_version": CODEX_CLIENT_VERSION,
    }


async def _codex_refresh_token(auth: Dict[str, Any]) -> None:
    """用 refresh_token 换新的 access_token，直接 mutate auth dict。"""
    refresh = auth.get("refresh")
    if not refresh:
        raise RuntimeError("No refresh_token for Codex")
    resp = await _http_post_json(
        CODEX_OAUTH_TOKEN_URL,
        body={
            "grant_type": "refresh_token",
            "refresh_token": refresh,
            "client_id": CODEX_CLIENT_ID,
        },
    )
    auth["access"] = resp["access_token"]
    if resp.get("refresh_token"):
        auth["refresh"] = resp["refresh_token"]
    auth["expires"] = int(time.time() * 1000) + int(resp.get("expires_in", 3600)) * 1000


async def _codex_fetch_models(auth: Dict[str, Any]) -> list:
    """GET https://chatgpt.com/backend-api/codex/models 拉取模型 catalog。"""
    token, extra = await _codex_get_valid_token(auth)
    url = f"{CODEX_API_BASE}/models?client_version={CODEX_CLIENT_VERSION}"
    resp = await _http_get_json(url, token=token, extra_headers=extra)
    return _parse_codex_models(resp)


async def _codex_query_quota(auth: Dict[str, Any]) -> Dict[str, Any]:
    """GET https://chatgpt.com/backend-api/wham/usage 查询额度。"""
    token, _ = await _codex_get_valid_token(auth)
    # wham/usage 只需要 Authorization + User-Agent + ChatGPT-Account-Id，
    # 发 originator/client_version 会被 OpenAI 拒绝
    headers = {
        "User-Agent": "codex-cli",
        "ChatGPT-Account-Id": auth.get("account_id", ""),
    }
    resp = await _http_get_json(
        "https://chatgpt.com/backend-api/wham/usage",
        token=token,
        extra_headers=headers,
    )
    return _normalize_codex_quota(resp)


# ═══════════════════════════════════════════════════════════════════════════
# GitHub Copilot
# ═══════════════════════════════════════════════════════════════════════════
async def _copilot_start_login(enterprise_domain: Optional[str] = None) -> Dict[str, Any]:
    """申请 GitHub device code；github.com 用 Iv1.b507a08c87ecfe98，GHES 用 Ov23li8tweQw6odWQebz。"""
    domain = _normalize_github_domain(enterprise_domain)
    client_id = GITHUB_CLIENT_ID_GITHUB_COM if not enterprise_domain else GITHUB_CLIENT_ID_GHES
    resp = await _http_post_json(
        f"https://{domain}/login/device/code",
        body={"client_id": client_id, "scope": "read:user"},
    )
    return {
        "subscription": "copilot",
        "enterprise_domain": enterprise_domain or "",
        "github_domain": domain,
        "client_id": client_id,
        "device_code": resp["device_code"],
        "user_code": resp["user_code"],
        "verification_uri": resp.get("verification_uri", f"https://{domain}/login/device"),
        "interval": resp.get("interval", 5),
        "expires_at": int(time.time() * 1000) + resp.get("expires_in", 900) * 1000,
    }


async def _copilot_poll_login(handle: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """轮询 GitHub device code → 拿 GitHub access_token → 换 Copilot token。"""
    now_ms = int(time.time() * 1000)
    if now_ms >= handle["expires_at"]:
        raise RuntimeError("Device code expired")

    domain = handle["github_domain"]
    client_id = handle["client_id"]
    resp = await _http_post_json(
        f"https://{domain}/login/oauth/access_token",
        body={
            "client_id": client_id,
            "device_code": handle["device_code"],
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        },
    )
    if resp.get("error") or not resp.get("access_token"):
        return None

    github_token = resp["access_token"]
    # 用 GitHub token 换 Copilot token（短期 JWT）
    copilot_token = await _copilot_exchange_token(github_token, handle.get("enterprise_domain"))
    user_info = await _http_get_json(
        f"https://api.github.com/user",
        token=github_token,
    )
    return {
        "type": "oauth",
        "subscription": "copilot",
        "access": copilot_token["token"],
        "refresh": github_token,  # GitHub token 作为长期凭证，Copilot token 用它刷新
        "expires": copilot_token["expires_at"],
        "account_id": str(user_info.get("id", "")),
        "account_name": str(user_info.get("login") or user_info.get("name") or ""),
        "account_email": str(user_info.get("email") or ""),
        "enterprise_domain": handle.get("enterprise_domain", ""),
    }


async def _copilot_get_valid_token(auth: Dict[str, Any]) -> Tuple[str, Dict[str, str]]:
    """Copilot token 短期有效，过期前用 GitHub token 重新换。"""
    if _should_refresh(auth):
        await _copilot_refresh_token(auth)
    base = _copilot_api_base(auth.get("enterprise_domain"))
    extra = {
        "X-GitHub-Api-Version": COPILOT_API_VERSION,
        "Editor-Version": "chattree/1.0.0",
    }
    return auth["access"], extra


async def _copilot_refresh_token(auth: Dict[str, Any]) -> None:
    """用 GitHub refresh token 重新换 Copilot token。"""
    github_token = auth.get("refresh", "")
    copilot_token = await _copilot_exchange_token(github_token, auth.get("enterprise_domain"))
    auth["access"] = copilot_token["token"]
    auth["expires"] = copilot_token["expires_at"]


async def _copilot_exchange_token(github_token: str, enterprise_domain: Optional[str]) -> Dict[str, Any]:
    """GET https://api.github.com/copilot_internal/v2/token 换 Copilot token。"""
    resp = await _http_get_json(
        "https://api.github.com/copilot_internal/v2/token",
        token=github_token,
    )
    return {
        "token": resp["token"],
        "expires_at": int(resp.get("expires_at", int(time.time() + 1800)) * 1000),
    }


async def _copilot_fetch_models(auth: Dict[str, Any]) -> list:
    """GET https://api.githubcopilot.com/models 拉取模型列表。"""
    token, extra = await _copilot_get_valid_token(auth)
    base = _copilot_api_base(auth.get("enterprise_domain"))
    resp = await _http_get_json(f"{base}/models", token=token, extra_headers=extra)
    return _parse_copilot_models(resp)


async def _copilot_query_quota(auth: Dict[str, Any]) -> Dict[str, Any]:
    """GET https://api.github.com/copilot_internal/user 查询 Copilot 额度。"""
    github_token = auth.get("refresh", "")
    resp = await _http_get_json(
        "https://api.github.com/copilot_internal/user",
        token=github_token,
    )
    return _normalize_copilot_quota(resp)


# ═══════════════════════════════════════════════════════════════════════════
# Claude (CLI 凭据复用 + 额度查询)
# ═══════════════════════════════════════════════════════════════════════════
def _read_claude_cli_credentials() -> Optional[Dict[str, Any]]:
    """读取 ~/.claude/.credentials.json，支持 claudeAiOauth / claude.ai_oauth 两种 key。"""
    import os
    path = os.path.expanduser("~/.claude/.credentials.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        oauth = data.get("claudeAiOauth") or data.get("claude.ai_oauth") or {}
        if not oauth.get("accessToken"):
            return None
        claims = _decode_jwt_claims(oauth["accessToken"])
        return {
            "type": "oauth",
            "subscription": "claude",
            "access": oauth["accessToken"],
            "refresh": oauth.get("refreshToken", ""),
            "expires": int(oauth.get("expiresAt", 0)),
            "account_id": str(claims.get("sub") or claims.get("organization_uuid") or ""),
            "account_name": str(claims.get("name") or ""),
            "account_email": str(claims.get("email") or ""),
        }
    except Exception as e:
        logger.warning(f"读取 Claude CLI 凭据失败: {e}")
        return None


async def _claude_query_quota(auth: Dict[str, Any]) -> Dict[str, Any]:
    """GET https://api.anthropic.com/api/oauth/usage 查询 Claude 订阅额度。"""
    resp = await _http_get_json(
        "https://api.anthropic.com/api/oauth/usage",
        token=auth.get("access", ""),
        extra_headers={"anthropic-beta": "oauth-2025-04-20"},
    )
    return _normalize_claude_quota(resp)


# ═══════════════════════════════════════════════════════════════════════════
# Gemini (CLI 凭据复用 + 刷新 + 额度查询)
# ═══════════════════════════════════════════════════════════════════════════
def _read_codex_cli_credentials() -> Optional[Dict[str, Any]]:
    """读取 ~/.codex/auth.json，要求 auth_mode=='chatgpt'。

    Codex CLI 的 access_token 有效期约 8 天；用 last_refresh (RFC3339) 推算 expires，
    不主动刷新——只有真过期时才由 _should_refresh 触发。
    """
    import os
    from datetime import datetime, timedelta
    path = os.path.expanduser("~/.codex/auth.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if data.get("auth_mode") != "chatgpt":
            return None
        tokens = data.get("tokens") or {}
        if not tokens.get("access_token"):
            return None
        # Codex CLI token 有效期约 8 天；用 last_refresh 推算 expires
        last_refresh = data.get("last_refresh") or ""
        try:
            dt = datetime.fromisoformat(last_refresh.replace("Z", "+00:00"))
            expires = int((dt + timedelta(days=8)).timestamp() * 1000)
        except Exception:
            expires = int(time.time() * 1000) + 7 * 24 * 3600 * 1000
        claims = _decode_jwt_claims(tokens.get("id_token", ""))
        auth_claim = claims.get("https://api.openai.com/auth") or {}
        return {
            "type": "oauth",
            "subscription": "codex",
            "access": tokens["access_token"],
            "refresh": tokens.get("refresh_token", ""),
            "expires": expires,
            "account_id": tokens.get("account_id", "") or str(
                auth_claim.get("chatgpt_account_id")
                or claims.get("chatgpt_account_id")
                or ""
            ),
            "account_name": str(claims.get("name") or claims.get("nickname") or ""),
            "account_email": str(claims.get("email") or ""),
        }
    except Exception as e:
        logger.warning(f"读取 Codex CLI 凭据失败: {e}")
        return None


async def _read_gemini_cli_credentials() -> Optional[Dict[str, Any]]:
    """读取 ~/.gemini/oauth_creds.json；过期时用 Google OAuth 刷新。"""
    import os
    path = os.path.expanduser("~/.gemini/oauth_creds.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        access = data.get("access_token")
        if not access:
            return None
        auth = {
            "type": "oauth",
            "subscription": "gemini",
            "access": access,
            "refresh": data.get("refresh_token", ""),
            "expires": int(data.get("expiry_date", 0)),
            "account_id": "",
            "account_name": "",
            "account_email": "",
        }
        if _should_refresh(auth):
            await _gemini_refresh_token(auth)
        # 用 access_token 调 Google userinfo API 拿昵称和邮箱
        try:
            info = await _http_get_json(
                "https://www.googleapis.com/oauth2/v2/userinfo",
                token=auth["access"],
            )
            auth["account_id"] = str(info.get("id") or "")
            auth["account_name"] = str(info.get("name") or info.get("given_name") or "")
            auth["account_email"] = str(info.get("email") or "")
        except Exception as e:
            logger.warning(f"获取 Gemini 用户信息失败: {e}")
        return auth
    except Exception as e:
        logger.warning(f"读取 Gemini CLI 凭据失败: {e}")
        return None


async def _gemini_refresh_token(auth: Dict[str, Any]) -> None:
    """用 Google OAuth client 刷新 Gemini access_token。"""
    resp = await _http_post_json(
        GEMINI_TOKEN_URL,
        body={
            "grant_type": "refresh_token",
            "refresh_token": auth.get("refresh", ""),
            "client_id": GEMINI_CLIENT_ID,
            "client_secret": GEMINI_CLIENT_SECRET,
        },
    )
    auth["access"] = resp["access_token"]
    auth["expires"] = int(time.time() * 1000 + resp.get("expires_in", 3600) * 1000)


async def _gemini_query_quota(auth: Dict[str, Any]) -> Dict[str, Any]:
    """Gemini Cloud Code 配额：先 loadCodeAssist 再 retrieveUserQuota。"""
    token = auth.get("access", "")
    if _should_refresh(auth):
        await _gemini_refresh_token(auth)
        token = auth["access"]

    # Step 1: loadCodeAssist 拿 user cloudaicompanion id
    load_resp = await _http_post_json(
        "https://cloudcode-pa.googleapis.com/v1internal:loadCodeAssist",
        body={"cloudaicompanionProject": ""},
        token=token,
    )
    user_id = load_resp.get("cloudaicompanionUser", {}).get("id", "")

    # Step 2: retrieveUserQuota 拿配额
    quota_resp = await _http_post_json(
        "https://cloudcode-pa.googleapis.com/v1internal:retrieveUserQuota",
        body={"cloudaicompanionProject": "", "cloudaicompanionUserId": user_id},
        token=token,
    )
    return _normalize_gemini_quota(quota_resp)


# ═══════════════════════════════════════════════════════════════════════════
# 共用工具
# ═══════════════════════════════════════════════════════════════════════════
def _should_refresh(auth: Dict[str, Any]) -> bool:
    expires = auth.get("expires", 0)
    if not expires:
        return True  # 未知过期时间，每次调用都刷新（codex CLI 复用场景）
    return int(time.time() * 1000) + _EAGER_REFRESH_MS >= expires


def _http_request_with_error(req: urllib.request.Request) -> dict:
    """执行 HTTP 请求，将网络/认证/限流错误转为 SubscriptionError。"""
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        status = e.code
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            pass
        if status == 401:
            raise SubscriptionError(f"认证失败，请重新登录 Codex 账号（{status}）", status=status)
        if status == 403:
            raise SubscriptionError(f"无权限访问 Codex 服务，请检查订阅状态（{status}）", status=status)
        if status == 429:
            raise SubscriptionError(f"Codex 请求过于频繁，请稍后重试（{status}）", status=status)
        raise SubscriptionError(f"Codex 服务返回错误 {status}：{body or e.reason}", status=status)
    except urllib.error.URLError as e:
        reason = str(e.reason) if e.reason else str(e)
        if "timed out" in reason.lower():
            raise SubscriptionError("无法连接到 Codex 服务：连接超时，请检查网络或使用代理", status=0)
        if "SSL" in reason or "ssl" in reason.lower() or "certificate" in reason.lower():
            raise SubscriptionError("无法连接到 Codex 服务：SSL 握手失败，可能需要配置代理", status=0)
        if "getaddrinfo" in reason.lower() or "name" in reason.lower():
            raise SubscriptionError("无法解析 Codex 服务域名，请检查 DNS 设置", status=0)
        raise SubscriptionError(f"无法连接到 Codex 服务：{reason}", status=0)


async def _http_post_json(url: str, body: Dict[str, Any], token: Optional[str] = None, extra_headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """异步 POST JSON。"""
    loop = asyncio.get_event_loop()

    def _do() -> Dict[str, Any]:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if extra_headers:
            headers.update(extra_headers)
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        return _http_request_with_error(req)

    return await loop.run_in_executor(None, _do)


async def _http_get_json(url: str, token: Optional[str] = None, extra_headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """异步 GET JSON。"""
    loop = asyncio.get_event_loop()

    def _do() -> Dict[str, Any]:
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if extra_headers:
            headers.update(extra_headers)
        req = urllib.request.Request(url, headers=headers, method="GET")
        return _http_request_with_error(req)

    return await loop.run_in_executor(None, _do)


def _decode_jwt_claims(token: str) -> Dict[str, Any]:
    """解码 JWT payload（不验签），返回 claims dict；失败返回空 dict。"""
    if not token:
        return {}
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return {}
        payload = parts[1]
        padded = payload + "=" * (-len(payload) % 4)
        decoded = base64.urlsafe_b64decode(padded)
        return json.loads(decoded)
    except Exception as e:
        logger.warning(f"解析 JWT 失败: {e}")
        return {}


def _parse_codex_models(resp: Any) -> list:
    """兼容 4 种 schema：data[]/models[]/models{}/裸数组。"""
    return _parse_models_universal(resp)


def _parse_copilot_models(resp: Any) -> list:
    """Copilot /models 响应：data[]，每条含 id/name/model_picker_enabled。"""
    entries = resp.get("data") if isinstance(resp, dict) else resp
    models = []
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        if not entry.get("model_picker_enabled", True):
            continue
        model_id = entry.get("id") or entry.get("name")
        if model_id:
            models.append({"id": str(model_id), "owned_by": entry.get("vendor")})
    models.sort(key=lambda m: m["id"])
    return models


def _parse_models_universal(resp: Any) -> list:
    """通用模型列表解析：data[]/models[]/models{}/裸数组 + id/slug/model/name 多字段。"""
    entries: list = []
    if isinstance(resp, dict):
        entries = resp.get("data") or resp.get("models") or resp.get("items") or []
        if isinstance(entries, dict):
            entries = [{"id": k, **(v if isinstance(v, dict) else {})} for k, v in entries.items()]
    elif isinstance(resp, list):
        entries = resp

    models = []
    for entry in entries:
        if isinstance(entry, str):
            if entry.strip():
                models.append({"id": entry.strip(), "owned_by": None})
            continue
        if not isinstance(entry, dict):
            continue
        model_id = entry.get("id") or entry.get("slug") or entry.get("model") or entry.get("name")
        if not model_id:
            continue
        models.append({
            "id": str(model_id).strip(),
            "owned_by": entry.get("owned_by") or entry.get("vendor"),
        })

    seen = set()
    unique = []
    for m in models:
        if m["id"] not in seen:
            seen.add(m["id"])
            unique.append(m)
    unique.sort(key=lambda m: m["id"])
    return unique


def _normalize_github_domain(enterprise_domain: Optional[str]) -> str:
    """归一化 GitHub 域名：剥离协议、小写、拒绝 userinfo。"""
    if not enterprise_domain:
        return "github.com"
    domain = enterprise_domain.strip()
    if "://" in domain:
        domain = domain.split("://", 1)[1]
    if "@" in domain.split("/")[0]:
        raise ValueError("GitHub domain must not contain userinfo")
    return domain.lower().rstrip("/")


def _copilot_api_base(enterprise_domain: Optional[str]) -> str:
    """Copilot API base URL：github.com 用 api.githubcopilot.com，GHES 用 copilot-api.{domain}。"""
    if not enterprise_domain:
        return COPILOT_API_BASE_GITHUB_COM
    domain = _normalize_github_domain(enterprise_domain)
    return f"https://copilot-api.{domain}"


def _normalize_codex_quota(resp: Dict[str, Any]) -> Dict[str, Any]:
    """归一化 Codex wham/usage 响应。"""
    rate_limit = resp.get("rate_limit") or {}
    result: Dict[str, Any] = {"subscription": "codex", "windows": []}
    for key in ("primary_window", "secondary_window"):
        win = rate_limit.get(key)
        if not win:
            continue
        result["windows"].append({
            "tier": _window_label(win.get("limit_window_seconds", 0)),
            "used_percent": win.get("used_percent", 0),
            "reset_at": win.get("reset_at"),
        })
    return result


def _normalize_copilot_quota(resp: Dict[str, Any]) -> Dict[str, Any]:
    """归一化 Copilot copilot_internal/user 响应。"""
    return {
        "subscription": "copilot",
        "plan": resp.get("copilot_plan"),
        "quota_snapshots": resp.get("quota_snapshots") or {},
    }


def _normalize_claude_quota(resp: Dict[str, Any]) -> Dict[str, Any]:
    """归一化 Claude oauth/usage 响应。"""
    return {
        "subscription": "claude",
        "windows": resp,  # 直接返回 five_hour/seven_day/seven_day_opus/seven_day_sonnet 等
    }


def _normalize_gemini_quota(resp: Dict[str, Any]) -> Dict[str, Any]:
    """归一化 Gemini retrieveUserQuota 响应。"""
    buckets = []
    for bucket in resp.get("buckets") or []:
        buckets.append({
            "model_id": bucket.get("modelId"),
            "tier": _window_label(int(bucket.get("resetTime", 0)) - int(time.time())),
            "remaining_fraction": bucket.get("remainingFraction"),
            "reset_at": bucket.get("resetTime"),
        })
    return {"subscription": "gemini", "buckets": buckets}


def _window_label(seconds: int) -> str:
    """把 limit_window_seconds 映射为人类可读 tier 名。"""
    if seconds <= 0:
        return "unknown"
    if seconds == 18000:
        return "five_hour"
    if seconds == 604800:
        return "seven_day"
    if seconds == 2592000:
        return "30_day"
    hours = seconds // 3600
    if hours >= 24:
        return f"{hours // 24}_day"
    return f"{hours}_hour"
