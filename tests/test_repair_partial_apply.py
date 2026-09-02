"""Tests for partial success when applying exact patches.

The applier used to raise on the first unusable patch, which threw away every
other patch in the same response. A chapter with three correct fixes and one
mislabelled `old` produced nothing, and the retry had to regenerate all four —
paying again for the three that were already right.

Now each patch is judged on its own: applicable ones land, unusable ones are
reported in `rejected`. That splits into two guarantees worth pinning separately:
the good work survives, and the bad work is still visible so nothing is quietly
accepted as finished. The second matters more, because silently banking a
partial fix would let a chapter be committed with an audit finding unaddressed.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _harness import LOCATOR_BLOCK, PATCH_BLOCK, build_class

# LOCATOR_BLOCK carries the verifier, PATCH_BLOCK the applier; the partial-success
# contract spans both, since a partial application must also fail verification.
Agent = build_class("Agent", [LOCATOR_BLOCK, PATCH_BLOCK])

ORIGINAL = "\n\n".join(
    f"这是第{i}段正文，其中包含唯一的标记{i:02d}，用于定位测试。" for i in range(1, 6)
)


def good(unit, i):
    return {"unit": unit, "old": f"标记{i:02d}", "new": f"记号{i:02d}", "reason": "改标记"}


def apply(patches, cls="TEXT_ONLY", unit_count=1):
    return Agent._repair_apply_exact_patches(
        ORIGINAL, patches, cls, unit_count=unit_count,
    )


class TestGoodPatchesSurvive:
    def test_one_bad_patch_no_longer_voids_the_others(self):
        """The headline change: three correct fixes are not discarded because a
        fourth was malformed."""
        patches = [good(1, 1), good(2, 2), good(3, 3),
                   {"unit": 4, "old": "这段原文里并不存在", "new": "x"}]
        result, meta = apply(patches, unit_count=4)
        assert meta["patch_count"] == 3
        for i in (1, 2, 3):
            assert f"记号{i:02d}" in result

    def test_rejected_patch_leaves_text_untouched(self):
        patches = [good(1, 1), {"unit": 2, "old": "不存在的片段", "new": "y"}]
        result, _ = apply(patches, unit_count=2)
        assert "y" not in result

    def test_units_covered_reflects_only_applied_patches(self):
        patches = [good(1, 1), {"unit": 2, "old": "不存在的片段", "new": "y"}]
        _, meta = apply(patches, unit_count=2)
        assert meta["units_covered"] == [1]
        assert meta["units_uncovered"] == [2]


class TestRejectionsAreVisible:
    def test_partial_flag_is_set(self):
        """Without this flag a partial result is indistinguishable from a
        complete one, and the chapter could be committed half-fixed."""
        patches = [good(1, 1), {"unit": 2, "old": "不存在的片段", "new": "y"}]
        _, meta = apply(patches, unit_count=2)
        assert meta["partial"] is True

    def test_complete_application_is_not_flagged_partial(self):
        _, meta = apply([good(1, 1)])
        assert meta["partial"] is False
        assert meta["rejected"] == []

    def test_each_rejection_names_its_index_and_reason(self):
        """The retry prompt quotes these, so a vague rejection wastes the retry."""
        patches = [good(1, 1), {"unit": 2, "old": "不存在的片段", "new": "y"}]
        _, meta = apply(patches, unit_count=2)
        r = meta["rejected"][0]
        assert r["index"] == 2
        assert r["reason"]
        assert r["old_preview"]

    def test_verifier_refuses_to_pass_a_partial_result(self):
        """Program-level acceptance must treat 'some patches applied' as
        unfinished, otherwise partial success becomes silent data loss."""
        patches = [good(1, 1), {"unit": 1, "old": "不存在的片段", "new": "y"}]
        candidate, meta = apply(patches)
        verdict = Agent._repair_verify_patches(
            ORIGINAL, candidate, meta, "TEXT_ONLY",
            anchors=[{"start": 0, "end": len(ORIGINAL)}],
        )
        assert verdict["verdict"] == "escalate"
        assert any("未能应用" in r for r in verdict["reasons"])


class TestRejectionReasons:
    @pytest.mark.parametrize("bad, expect", [
        ({"unit": 1, "old": "", "new": "x"}, "old 为空"),
        ({"unit": 1, "old": "标记01", "new": "标记01"}, "没有实际变化"),
        ({"unit": 1, "old": "根本没有这句话", "new": "x"}, "找不到"),
        ("not-a-dict", "格式"),
    ])
    def test_each_failure_mode_is_named(self, bad, expect):
        _, meta = apply([good(1, 1), bad], unit_count=2)
        assert expect in meta["rejected"][0]["reason"]

    def test_ambiguous_old_is_rejected_not_applied(self):
        """Applying an ambiguous match would edit an arbitrary occurrence."""
        doubled = ORIGINAL + "\n\n又一次出现标记01，用于制造歧义。"
        result, meta = Agent._repair_apply_exact_patches(
            doubled, [good(1, 2), good(1, 1)], "TEXT_ONLY", unit_count=1,
        )
        assert meta["rejected_count"] == 1
        assert "出现多次" in meta["rejected"][0]["reason"]
        assert result.count("标记01") == 2


class TestAllPatchesUnusable:
    def test_everything_rejected_still_raises(self):
        """With nothing applicable there is no candidate to review, so this stays
        an error rather than silently returning the original text as 'fixed'."""
        with pytest.raises(ValueError):
            apply([{"unit": 1, "old": "不存在A", "new": "x"},
                   {"unit": 1, "old": "不存在B", "new": "y"}])

    def test_the_error_lists_every_reason(self):
        with pytest.raises(ValueError) as e:
            apply([{"unit": 1, "old": "不存在A", "new": "x"},
                   {"unit": 1, "old": "不存在B", "new": "y"}])
        assert "第 1 个补丁" in str(e.value)
        assert "第 2 个补丁" in str(e.value)

    def test_empty_patch_list_still_raises(self):
        with pytest.raises(ValueError):
            apply([])


class TestLimitsStillApply:
    def test_size_caps_are_enforced_on_the_applied_subset(self):
        """Partial success must not become a way to smuggle an oversized edit in
        alongside a rejected one."""
        huge = {"unit": 1, "old": "标记01", "new": "字" * 4000, "reason": "扩写"}
        with pytest.raises(ValueError):
            apply([huge, {"unit": 1, "old": "不存在", "new": "x"}])

    def test_patch_count_cap_still_applies(self):
        many = [good(1, (i % 5) + 1) for i in range(40)]
        with pytest.raises(ValueError, match="数量"):
            apply(many)
