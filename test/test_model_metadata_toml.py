import sys
from pathlib import Path

sys.path.insert(0, ".")

from backend.core.model.model_metadata import _matches, resolve_metadata


def test_model_metadata_is_loaded_from_toml_file():
    path = Path("backend/core/model/model_metadata.toml")
    assert path.exists()
    assert "gpt-5.4-mini" in path.read_text(encoding="utf-8")


def test_openai_frontier_models_resolve_from_metadata_file():
    meta = resolve_metadata("gpt-5.4-mini", "responses")
    assert meta["context_length"] == 400_000
    assert meta["supports_vision"] is True
    assert meta["reasoning_effort"]["levels"] == ["low", "medium", "high", "xhigh"]


def test_model_metadata_rule_matching_is_case_insensitive():
    assert _matches("gpt-5.4-mini", "responses", ["^GPT-5\\.4-MINI$"], [])
    assert _matches("anything", "RESPONSES", [], ["responses"])


def test_siliconflow_namespaced_models_resolve_from_short_model_name():
    deepseek = resolve_metadata("deepseek-ai/DeepSeek-V3", "chat_completions")
    assert deepseek["supports_vision"] is False
    assert deepseek["thinking"] == {"toggleable": True, "default_enabled": True}

    qwen_coder = resolve_metadata("Qwen/Qwen3-Coder-480B-A35B-Instruct", "chat_completions")
    assert qwen_coder["supports_vision"] is False
    assert qwen_coder["thinking"] == {"toggleable": True, "default_enabled": True}

    glm = resolve_metadata("THUDM/GLM-4.5-Air", "chat_completions")
    assert glm["supports_vision"] is False
    assert glm["thinking"] == {"toggleable": True, "default_enabled": True}


def test_claude_models_resolve_from_metadata_file():
    meta = resolve_metadata("claude-opus-4-8", "anthropic")
    assert meta["context_length"] == 1_000_000
    assert meta["supports_vision"] is True
    assert meta["reasoning_effort"]["levels"] == ["low", "medium", "high", "xhigh", "max"]
    assert meta["thinking"] == {"toggleable": True, "default_enabled": False}


def test_qwen_deepseek_glm_and_gemini_models_resolve_from_metadata_file():
    qwen = resolve_metadata("qwen3-coder-plus", "chat_completions")
    assert qwen["context_length"] == 1_000_000
    assert qwen["supports_vision"] is False
    assert qwen["thinking"] == {"toggleable": True, "default_enabled": True}

    qwen_vl = resolve_metadata("qwen3-vl-plus", "chat_completions")
    assert qwen_vl["context_length"] == 262_144
    assert qwen_vl["supports_vision"] is True

    deepseek = resolve_metadata("deepseek-v4-pro", "chat_completions")
    assert deepseek["context_length"] == 1_000_000
    assert deepseek["reasoning_effort"]["levels"] == ["high", "max"]
    assert deepseek["reasoning_effort"]["default"] == "high"
    assert deepseek["thinking"] == {"toggleable": True, "default_enabled": True}

    glm = resolve_metadata("glm-5.2", "chat_completions")
    assert glm["context_length"] == 1_000_000
    assert glm["supports_vision"] is False
    assert glm["thinking"] == {"toggleable": True, "default_enabled": True}

    gemini = resolve_metadata("gemini-2.5-flash", "gemini")
    assert gemini["context_length"] == 1_048_576
    assert gemini["supports_vision"] is True
    assert gemini["reasoning_effort"]["levels"] == ["dynamic", "low", "medium", "high"]
    assert gemini["thinking"] == {"toggleable": True, "default_enabled": False}
