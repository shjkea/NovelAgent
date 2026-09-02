"""Retry budgets are split by failure kind, so the classifier must be right.

Two very different things can make an attempt fail:

  mechanical - the patch could not be applied as written.  The `old` snippet was
               not copied verbatim, or matched twice, or the response was not
               valid JSON.  None of that means the model misunderstood the fix.

  semantic   - the patch applied but the result is wrong: the requested fix is
               absent, unrelated text moved, the chapter tail changed.

Only the semantic kind justifies spending the escalation budget and eventually
the expensive model.  Mechanical slips get a cheap re-ask that restates the
constraint.  The risk this file guards is the classifier being too eager to call
something mechanical: that would grant unlimited cheap retries to a chapter the
model genuinely cannot fix, and it would never reach Pro or manual review.  So
the default for anything unrecognised is `semantic`, and that default is asserted
directly rather than left implicit.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _harness import LOCATOR_BLOCK, build_class

Agent = build_class("Agent", [LOCATOR_BLOCK])


class TestMechanicalFailures:
    """Failures that say nothing about whether the model understood the fix."""

    @pytest.mark.parametrize("error", [
        "补丁 1 的 old 在原文中找不到",
        "补丁 2 的 old 在原文中出现多次",
        "补丁 1 的 old 为空",
        "补丁 3 格式不是对象",
        "补丁 2 没有实际变化",
        "补丁 2 与已应用的补丁重叠",
        "所有补丁都未能应用",
        "审计修复 patch JSON 解析失败，原始输出已保存：logs/x.txt",
        "模型没有返回可应用的补丁",
    ])
    def test_apply_level_errors_are_mechanical(self, error):
        assert Agent._repair_failure_kind(error) == "mechanical"

    def test_reason_lists_are_classified_too(self):
        """The applier reports rejections as reasons, not as one error string."""
        kind = Agent._repair_failure_kind("", ["old 在原文中找不到"])
        assert kind == "mechanical"

    def test_error_and_reasons_are_considered_together(self):
        kind = Agent._repair_failure_kind("补丁 1 的 old 出现多次", [])
        assert kind == "mechanical"


class TestSemanticFailures:
    """Failures that mean the model was wrong about the edit itself."""

    @pytest.mark.parametrize("reason", [
        "requested_fix_applied=False",
        "改动了无关段落",
        "章末 Canon 状态被改动",
        "与下游章节冲突",
        "改动比例过大",
        "第 2 条要求没有任何补丁覆盖",
    ])
    def test_review_level_failures_are_semantic(self, reason):
        assert Agent._repair_failure_kind("", [reason]) == "semantic"

    def test_unknown_failure_defaults_to_semantic(self):
        """An unrecognised failure must consume the escalation budget.

        Treating the unknown case as mechanical would hand a stuck chapter an
        unlimited supply of cheap retries and stop it ever being escalated or
        surfaced for review.  The safe default is the expensive one.
        """
        assert Agent._repair_failure_kind("某种全新的错误") == "semantic"

    def test_empty_failure_defaults_to_semantic(self):
        assert Agent._repair_failure_kind("") == "semantic"
        assert Agent._repair_failure_kind("", []) == "semantic"
        assert Agent._repair_failure_kind("   ", ["  "]) == "semantic"


class TestEscalationLadder:
    """Only semantic attempts advance toward the expensive model."""

    def test_first_two_semantic_attempts_stay_cheap(self):
        for attempt in (1, 2):
            model = Agent._repair_generation_model("TEXT_ONLY", attempt)
            assert model == "deepseek-v4-flash"

    def test_third_semantic_attempt_escalates(self):
        model = Agent._repair_generation_model("TEXT_ONLY", 3)
        assert model == "deepseek-v4-pro"

    def test_mechanical_retries_never_escalate_by_themselves(self):
        """A mechanical slip leaves the semantic counter alone.

        This is the whole point of the split: the model that keeps mis-copying a
        snippet is asked again on Flash, not promoted to Pro, because promoting it
        spends real money on a problem Pro is no better at.
        """
        semantic = 0
        for _ in range(5):
            kind = Agent._repair_failure_kind("old 在原文中找不到")
            if kind != "mechanical":
                semantic += 1
            assert Agent._repair_generation_model(
                "CONTINUITY_MINOR", semantic + 1,
            ) == "deepseek-v4-flash"

    def test_continuity_minor_is_not_special_cased(self):
        """Both auto-fix classes share one ladder."""
        for cls in ("TEXT_ONLY", "CONTINUITY_MINOR"):
            assert Agent._repair_generation_model(cls, 1) == "deepseek-v4-flash"
            assert Agent._repair_generation_model(cls, 3) == "deepseek-v4-pro"
