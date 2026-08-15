import sys
from pathlib import Path

import pytest

sys.path.insert(0, ".")

from backend.core.model.model_metadata import (
    ModelRouteError,
    _matches,
    initialize_model_metadata,
    resolve_metadata,
    resolve_route,
)
from backend.core.model import model_metadata as metadata_module


@pytest.fixture(autouse=True)
def clear_catalog():
    metadata_module._load_catalog.cache_clear()
    yield
    metadata_module._load_catalog.cache_clear()


def test_model_metadata_uses_repository_builtin_file(tmp_path: Path):
    builtin = Path("backend/core/model/model_metadata.toml")

    assert initialize_model_metadata() == builtin.resolve()
    assert not (tmp_path / "model_metadata.toml").exists()


def test_metadata_override_is_explicit_and_process_local(tmp_path: Path, monkeypatch):
    metadata_file = tmp_path / "model_metadata.toml"
    metadata_file.write_text(
        "\n".join([
            "[[rules]]",
            'name_patterns = ["^custom$"]',
            'protocol = "openai_responses"',
            'endpoint = "/custom/responses"',
            "context_length = 123456",
        ]),
        encoding="utf-8",
    )
    monkeypatch.setattr(metadata_module, "_BUILTIN_METADATA_FILE", metadata_file)
    metadata_module._load_catalog.cache_clear()
    meta = resolve_metadata(resolve_route("test", "custom"))
    assert meta["protocol"] == "openai_responses"
    assert meta["endpoint"] == "/custom/responses"
    assert meta["context_length"] == 123_456


def test_packaged_metadata_resolves_from_builtin_source():
    meta = resolve_metadata(resolve_route("test", "gpt-5.4-mini"))

    assert meta["context_length"] == 400_000


def test_openai_frontier_models_resolve_from_metadata_file():
    gpt_5_6 = resolve_metadata(resolve_route("test", "gpt-5.6-sol"))
    gpt_5_4_mini = resolve_metadata(resolve_route("test", "gpt-5.4-mini"))

    assert gpt_5_6["reasoning_effort"]["levels"] == [
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
    ]
    assert gpt_5_4_mini["protocol"] == "openai_responses"
    assert gpt_5_4_mini["context_length"] == 400_000
    assert gpt_5_4_mini["supports_vision"] is True
    assert gpt_5_4_mini["reasoning_effort"]["levels"] == ["low", "medium", "high", "xhigh"]


def test_model_metadata_rule_matching_is_case_insensitive():
    assert _matches("gpt-5.4-mini", ["^GPT-5\\.4-MINI$"])
    assert not _matches("anything", [])


def test_siliconflow_namespaced_models_resolve_from_short_model_name():
    deepseek = resolve_metadata(resolve_route("test", "deepseek-ai/DeepSeek-V3"))
    assert deepseek["supports_vision"] is False
    assert deepseek["thinking"] == {"toggleable": True, "default_enabled": True}

    qwen_coder = resolve_metadata(resolve_route("test", "Qwen/Qwen3-Coder-480B-A35B-Instruct"))
    assert qwen_coder["supports_vision"] is False
    assert qwen_coder["thinking"] == {"toggleable": True, "default_enabled": True}

    glm = resolve_metadata(resolve_route("test", "THUDM/GLM-4.5-Air"))
    assert glm["supports_vision"] is False
    assert glm["thinking"] == {"toggleable": True, "default_enabled": True}


def test_claude_models_resolve_from_metadata_file():
    meta = resolve_metadata(resolve_route("test", "claude-opus-4-8"))
    assert meta["protocol"] == "anthropic_messages"
    assert meta["context_length"] == 1_000_000
    assert meta["supports_vision"] is True
    assert meta["reasoning_effort"]["levels"] == ["low", "medium", "high", "xhigh", "max"]
    assert meta["thinking"] == {"toggleable": True, "default_enabled": False}


def test_qwen_deepseek_glm_and_gemini_models_resolve_from_metadata_file():
    qwen = resolve_metadata(resolve_route("test", "qwen3-coder-plus"))
    assert qwen["context_length"] == 1_000_000
    assert qwen["supports_vision"] is False
    assert qwen["thinking"] == {"toggleable": True, "default_enabled": True}

    qwen_vl = resolve_metadata(resolve_route("test", "qwen3-vl-plus"))
    assert qwen_vl["context_length"] == 262_144
    assert qwen_vl["supports_vision"] is True

    deepseek = resolve_metadata(resolve_route("test", "deepseek-v4-pro"))
    assert deepseek["context_length"] == 1_000_000
    assert deepseek["reasoning_effort"]["levels"] == ["high", "max"]
    assert deepseek["reasoning_effort"]["default"] == "high"
    assert deepseek["thinking"] == {"toggleable": True, "default_enabled": True}

    glm = resolve_metadata(resolve_route("test", "glm-5.2"))
    assert glm["context_length"] == 1_000_000
    assert glm["supports_vision"] is False
    assert glm["thinking"] == {"toggleable": True, "default_enabled": True}

    gemini = resolve_metadata(resolve_route("test", "gemini-2.5-flash"))
    assert gemini["context_length"] == 1_048_576
    assert gemini["supports_vision"] is True
    assert gemini["reasoning_effort"]["levels"] == ["dynamic", "low", "medium", "high"]
    assert gemini["thinking"] == {"toggleable": True, "default_enabled": False}


def test_aggregate_reasoning_aliases_preserve_chat_reasoning():
    for model in ("k3", "qwen-reasoner", "qwen3.5-thinking", "qwen3.6-reasoner"):
        meta = resolve_metadata(resolve_route("gateway", model))

        assert meta["reasoning_profile"]["carrier"] == "chat_reasoning_content"
        assert meta["reasoning_profile"]["history_policy"] == "all_assistant_messages"
        assert meta["reasoning_profile"]["strict"] is True

    k3 = resolve_metadata(resolve_route("gateway", "kimi-k3"))
    assert k3["reasoning_effort"] == {
        "levels": ["low", "high", "max"],
        "default": "high",
    }
    assert k3["thinking"] is None


def test_unknown_model_falls_back_to_plain_chat_completions():
    route = resolve_route("gateway", "unlisted-model")
    meta = resolve_metadata(route)

    assert route["protocol"] == "openai_chat_completions"
    assert route["endpoint"] == "/chat/completions"
    assert meta["reasoning_profile"]["carrier"] == "none"
    assert meta["reasoning_effort"] is None
    assert meta["thinking"] is None


def test_legacy_metadata_without_protocol_uses_plain_chat_fallback(
    tmp_path: Path,
    monkeypatch,
):
    metadata_file = tmp_path / "model_metadata.toml"
    metadata_file.write_text(
        "\n".join([
            "[[rules]]",
            'name_patterns = ["^legacy-model$"]',
            'api_formats = ["responses"]',
            "context_length = 123456",
        ]),
        encoding="utf-8",
    )
    monkeypatch.setattr(metadata_module, "_BUILTIN_METADATA_FILE", metadata_file)
    metadata_module._load_catalog.cache_clear()

    meta = resolve_metadata(resolve_route("gateway", "legacy-model"))

    assert meta["protocol"] == "openai_chat_completions"
    assert meta["context_length"] is None
    assert meta["reasoning_profile"]["carrier"] == "none"


def test_invalid_metadata_protocol_fails_before_network_request(tmp_path: Path, monkeypatch):
    metadata_file = tmp_path / "model_metadata.toml"
    metadata_file.write_text(
        "\n".join([
            "[[rules]]",
            'name_patterns = ["^broken$"]',
            'protocol = "unknown"',
        ]),
        encoding="utf-8",
    )
    monkeypatch.setattr(metadata_module, "_BUILTIN_METADATA_FILE", metadata_file)
    metadata_module._load_catalog.cache_clear()

    with pytest.raises(ModelRouteError, match="协议无效"):
        resolve_route("gateway", "broken")
