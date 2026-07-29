import asyncio

import pytest

from backend.core.auth import subscription


def test_gemini_refresh_requires_oauth_client_environment(monkeypatch):
    monkeypatch.delenv("CHATTREE_GEMINI_CLIENT_ID", raising=False)
    monkeypatch.delenv("CHATTREE_GEMINI_CLIENT_SECRET", raising=False)

    with pytest.raises(subscription.SubscriptionError, match="CHATTREE_GEMINI_CLIENT_ID"):
        asyncio.run(subscription._gemini_refresh_token({"refresh": "test-refresh-token"}))


def test_gemini_refresh_uses_oauth_client_environment(monkeypatch):
    request = {}

    async def fake_post(url, *, body, token=None, headers=None):
        request.update(url=url, body=body)
        return {"access_token": "test-access-token", "expires_in": 3600}

    monkeypatch.setenv("CHATTREE_GEMINI_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("CHATTREE_GEMINI_CLIENT_SECRET", "test-client-secret")
    monkeypatch.setattr(subscription, "_http_post_json", fake_post)
    auth = {"refresh": "test-refresh-token"}

    asyncio.run(subscription._gemini_refresh_token(auth))

    assert request["url"] == subscription.GEMINI_TOKEN_URL
    assert request["body"]["client_id"] == "test-client-id"
    assert request["body"]["client_secret"] == "test-client-secret"
    assert auth["access"] == "test-access-token"
