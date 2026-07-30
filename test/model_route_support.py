from backend.core.config.types import ModelRoute


def fake_model_route(
    provider_id: str = "fake",
    model_id: str = "fake-model",
) -> ModelRoute:
    return ModelRoute(
        route_id=f"{provider_id}:{model_id}:openai_chat_completions",
        provider_id=provider_id,
        model_id=model_id,
        protocol="openai_chat_completions",
        endpoint="/chat/completions",
        capabilities={
            "context_length": 1_000_000,
            "supports_vision": False,
            "supports_tools": True,
            "reasoning_effort": None,
            "thinking": None,
        },
        reasoning_profile={
            "name": "generic_chat",
            "carrier": "none",
            "history_policy": "drop",
            "strict": False,
            "controls": {},
        },
    )
