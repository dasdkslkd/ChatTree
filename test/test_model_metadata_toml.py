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


@pytest.fixture(autouse=True)
def metadata_home(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CHATTREE_HOME", str(tmp_path))
    return initialize_model_metadata(tmp_path)


def test_model_metadata_is_copied_to_server_home(metadata_home: Path):
    builtin = Path("backend/core/model/model_metadata.toml")

    assert metadata_home.read_bytes() == builtin.read_bytes()


def test_existing_server_home_metadata_is_not_overwritten(metadata_home: Path):
    custom = '[[rules]]\nname_patterns = ["^custom$"]\n'
    metadata_home.write_text(custom, encoding="utf-8")

    assert initialize_model_metadata(metadata_home.parent) == metadata_home
    assert metadata_home.read_text(encoding="utf-8") == custom


def test_server_home_metadata_is_the_runtime_route_source(metadata_home: Path):
    metadata_home.write_text(
        "\n".join([
            "[[rules]]",
            'name_patterns = ["^custom$"]',
            'protocol = "openai_responses"',
            'endpoint = "/custom/responses"',
            "context_length = 123456",
        ]),
        encoding="utf-8",
    )

    meta = resolve_metadata(resolve_route("test", "custom"))

    assert meta["protocol"] == "openai_responses"
    assert meta["endpoint"] == "/custom/responses"
    assert meta["context_length"] == 123_456


def test_server_home_metadata_updates_without_process_restart(metadata_home: Path):
    metadata_home.write_text(
        "\n".join([
            "[[rules]]",
            'name_patterns = ["^hot-model$"]',
            'protocol = "openai_chat_completions"',
            "context_length = 111",
        ]),
        encoding="utf-8",
    )
    assert resolve_metadata(resolve_route("test", "hot-model"))["context_length"] == 111

    metadata_home.write_text(
        "\n".join([
            "[[rules]]",
            'name_patterns = ["^hot-model$"]',
            'protocol = "openai_chat_completions"',
            "context_length = 222",
        ]),
        encoding="utf-8",
    )

    assert resolve_metadata(resolve_route("test", "hot-model"))["context_length"] == 222


def test_model_metadata_is_isolated_by_server_home(
    metadata_home: Path,
    monkeypatch,
):
    metadata_home.write_text(
        "\n".join([
            "[[rules]]",
            'name_patterns = ["^isolated$"]',
            'protocol = "openai_chat_completions"',
            "context_length = 111",
        ]),
        encoding="utf-8",
    )
    assert resolve_metadata(resolve_route("test", "isolated"))["context_length"] == 111

    other_home = metadata_home.parent / "other-home"
    other_metadata = initialize_model_metadata(other_home)
    other_metadata.write_text(
        "\n".join([
            "[[rules]]",
            'name_patterns = ["^isolated$"]',
            'protocol = "openai_chat_completions"',
            "context_length = 222",
        ]),
        encoding="utf-8",
    )
    monkeypatch.setenv("CHATTREE_HOME", str(other_home))

    assert resolve_metadata(resolve_route("test", "isolated"))["context_length"] == 222


def test_packaged_metadata_seeds_the_server_home_source():
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


def test_unknown_model_falls_back_to_plain_chat_completions():
    route = resolve_route("gateway", "unlisted-model")
    meta = resolve_metadata(route)

    assert route["protocol"] == "openai_chat_completions"
    assert route["endpoint"] == "/chat/completions"
    assert meta["reasoning_profile"]["carrier"] == "none"
    assert meta["reasoning_effort"] is None
    assert meta["thinking"] is None


def test_legacy_metadata_without_protocol_uses_plain_chat_fallback(
    metadata_home: Path,
):
    metadata_home.write_text(
        "\n".join([
            "[[rules]]",
            'name_patterns = ["^legacy-model$"]',
            'api_formats = ["responses"]',
            "context_length = 123456",
        ]),
        encoding="utf-8",
    )

    meta = resolve_metadata(resolve_route("gateway", "legacy-model"))

    assert meta["protocol"] == "openai_chat_completions"
    assert meta["context_length"] is None
    assert meta["reasoning_profile"]["carrier"] == "none"


def test_invalid_metadata_protocol_fails_before_network_request(metadata_home: Path):
    metadata_home.write_text(
        "\n".join([
            "[[rules]]",
            'name_patterns = ["^broken$"]',
            'protocol = "unknown"',
        ]),
        encoding="utf-8",
    )

    with pytest.raises(ModelRouteError, match="协议无效"):
        resolve_route("gateway", "broken")
