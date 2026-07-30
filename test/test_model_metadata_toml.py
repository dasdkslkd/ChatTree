import sys
from pathlib import Path

import pytest

sys.path.insert(0, ".")

from backend.core.model.model_metadata import (
    _matches,
    initialize_model_metadata,
    resolve_metadata,
)


@pytest.fixture(autouse=True)
def metadata_home(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CHATTREE_HOME", str(tmp_path))
    return initialize_model_metadata(tmp_path)


def test_model_metadata_is_copied_to_server_home(metadata_home: Path):
    builtin = Path("backend/core/model/model_metadata.toml")

    assert metadata_home.read_bytes() == builtin.read_bytes()


def test_existing_server_home_metadata_is_not_overwritten(metadata_home: Path):
    custom = '[[rules]]\nname_patterns = ["^custom$"]\ncontext_length = 123\n'
    metadata_home.write_text(custom, encoding="utf-8")

    assert initialize_model_metadata(metadata_home.parent) == metadata_home
    assert metadata_home.read_text(encoding="utf-8") == custom


def test_server_home_metadata_overrides_builtin(metadata_home: Path):
    metadata_home.write_text(
        '\n'.join([
            '[[rules]]',
            'name_patterns = ["^gpt-5\\\\.4-mini$"]',
            'context_length = 123456',
            'supports_vision = false',
        ]),
        encoding="utf-8",
    )

    meta = resolve_metadata("gpt-5.4-mini", "responses")

    assert meta["context_length"] == 123_456
    assert meta["supports_vision"] is False


def test_server_home_metadata_updates_without_process_restart(metadata_home: Path):
    metadata_home.write_text(
        '[[rules]]\nname_patterns = ["^hot-model$"]\ncontext_length = 111\n',
        encoding="utf-8",
    )
    assert resolve_metadata("hot-model", "responses")["context_length"] == 111

    metadata_home.write_text(
        '[[rules]]\nname_patterns = ["^hot-model$"]\ncontext_length = 222\n',
        encoding="utf-8",
    )

    assert resolve_metadata("hot-model", "responses")["context_length"] == 222


def test_model_metadata_is_isolated_by_server_home(
    metadata_home: Path,
    monkeypatch,
):
    metadata_home.write_text(
        '[[rules]]\nname_patterns = ["^isolated$"]\ncontext_length = 111\n',
        encoding="utf-8",
    )
    assert resolve_metadata("isolated", "responses")["context_length"] == 111

    other_home = metadata_home.parent / "other-home"
    other_metadata = initialize_model_metadata(other_home)
    other_metadata.write_text(
        '[[rules]]\nname_patterns = ["^isolated$"]\ncontext_length = 222\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("CHATTREE_HOME", str(other_home))

    assert resolve_metadata("isolated", "responses")["context_length"] == 222


def test_packaged_metadata_is_fallback_before_home_initialization(metadata_home: Path):
    metadata_home.unlink()

    meta = resolve_metadata("gpt-5.4-mini", "responses")

    assert meta["context_length"] == 400_000


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
