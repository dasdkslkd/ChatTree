from pathlib import Path

import pytest

from backend.core.plans.artifact import PlanArtifactStore, PlanPatchError


def test_plan_artifact_path_is_controlled(tmp_path: Path):
    store = PlanArtifactStore(tmp_path)
    path = store.path_for(conversation_id="conv/../bad", plan_id="plan:1")

    assert path == tmp_path / "conversations" / "conv___bad" / "plans" / "plan_1.md"


def test_replace_creates_utf8_plan_file(tmp_path: Path):
    store = PlanArtifactStore(tmp_path)
    result = store.update(
        conversation_id="conv1",
        plan_id="plan1",
        mode="replace",
        content="# Plan\n\n1. First step\n",
    )

    assert result.content == "# Plan\n\n1. First step\n"
    assert result.revision == 1
    assert result.path.read_text(encoding="utf-8") == result.content


def test_apply_patch_updates_existing_plan(tmp_path: Path):
    store = PlanArtifactStore(tmp_path)
    store.update(conversation_id="conv1", plan_id="plan1", mode="replace", content="# Plan\n\n- A\n")

    result = store.update(
        conversation_id="conv1",
        plan_id="plan1",
        mode="apply_patch",
        patch="*** Begin Patch\n*** Update File: plan.md\n@@\n # Plan\n \n-- A\n+- A\n+- B\n*** End Patch\n",
    )

    assert result.content == "# Plan\n\n- A\n- B\n"
    assert result.revision == 2


def test_apply_patch_rejects_missing_context(tmp_path: Path):
    store = PlanArtifactStore(tmp_path)
    store.update(conversation_id="conv1", plan_id="plan1", mode="replace", content="# Plan\n")

    with pytest.raises(PlanPatchError):
        store.update(
            conversation_id="conv1",
            plan_id="plan1",
            mode="apply_patch",
            patch="*** Begin Patch\n*** Update File: plan.md\n@@\n missing\n-old\n+new\n*** End Patch\n",
        )
