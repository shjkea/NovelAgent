"""Tests for patch-level granularity within a shared chapter lock.

A chapter file can only be written once per batch, so same-chapter fixes must
share one item. The bug being fixed here is that they also used to share one
`instruction` string, which had three consequences:

  * The patch model saw one compound demand instead of N independent ones, so
    partial completion looked like success.
  * Budgets sized for a single typo were applied to a chapter holding several
    requirements, rejecting correct work for being too large.
  * A retry re-sent every requirement in the chapter, so one unfixable item
    burned the retry budget of all the others.

The invariants protected below: units stay individually addressable, each keeps
its own class, budgets scale with unit count, and a missed unit is detected
rather than silently passing.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _harness import (
    LOCATOR_BLOCK,
    NORMALIZE_BLOCK,
    PATCH_BLOCK,
    TASK_BLOCK,
    build_class,
)

HEADER = [
    'AUDIT_FIX_CLASSES = {"TEXT_ONLY", "CONTINUITY_MINOR", "REWRITE_SPAN", '
    '"REWRITE_CHAPTER", "DEFER_FUTURE"}',
    "def __init__(self, root=None):\n    self.root = Path(root) if root else None",
]

Agent = build_class(
    "Agent",
    [LOCATOR_BLOCK, TASK_BLOCK, NORMALIZE_BLOCK, PATCH_BLOCK],
    header_lines=HEADER,
)


@pytest.fixture()
def workspace(tmp_path):
    (tmp_path / "chapters").mkdir()
    for n in (7, 8):
        (tmp_path / "chapters" / f"{n:04d}.md").write_text(
            f"第{n}章正文", encoding="utf-8"
        )
    return tmp_path


def row(**kw):
    base = {
        "chapter_no": 7,
        "repair_class": "TEXT_ONLY",
        "instruction": "把日期改为三月十五日",
        "must_preserve": ["父亲从后门离开"],
        "reason": "与前文时间线冲突",
    }
    base.update(kw)
    return base


class TestSharedChapterLock:
    """Same chapter, one write, but still separate repair units."""

    def test_two_rows_share_one_item(self, workspace):
        items = Agent(workspace)._normalize_audit_repair_items([
            row(instruction="把日期改为三月十五日"),
            row(instruction="把称谓改为师兄"),
        ])
        assert len(items) == 1
        assert items[0]["chapter_no"] == 7

    def test_two_rows_stay_two_units(self, workspace):
        items = Agent(workspace)._normalize_audit_repair_items([
            row(instruction="把日期改为三月十五日"),
            row(instruction="把称谓改为师兄"),
        ])
        anchors = items[0]["anchors"]
        assert len(anchors) == 2
        assert len(items[0]["task_ids"]) == 2
        assert [a["instruction"] for a in anchors] == [
            "把日期改为三月十五日", "把称谓改为师兄",
        ]

    def test_instruction_is_numbered_not_concatenated(self, workspace):
        """The old format was a '\\n- ' bullet run, which read as one demand."""
        items = Agent(workspace)._normalize_audit_repair_items([
            row(instruction="把日期改为三月十五日"),
            row(instruction="把称谓改为师兄"),
        ])
        text = items[0]["instruction"]
        assert text.startswith("1.")
        assert "2." in text
        assert "\n- " not in text

    def test_each_unit_keeps_its_own_class(self, workspace):
        items = Agent(workspace)._normalize_audit_repair_items([
            row(instruction="改日期", repair_class="TEXT_ONLY"),
            row(instruction="补一句交代", repair_class="CONTINUITY_MINOR"),
        ])
        assert [a["repair_class"] for a in items[0]["anchors"]] == [
            "TEXT_ONLY", "CONTINUITY_MINOR",
        ]

    def test_chapter_class_is_the_loosest_unit_budget(self, workspace):
        items = Agent(workspace)._normalize_audit_repair_items([
            row(instruction="改日期", repair_class="TEXT_ONLY"),
            row(instruction="补一句交代", repair_class="CONTINUITY_MINOR"),
        ])
        assert items[0]["repair_class"] == "CONTINUITY_MINOR"

    def test_duplicate_instruction_is_deduplicated(self, workspace):
        """Report chunks overlap, so the same row arrives more than once."""
        items = Agent(workspace)._normalize_audit_repair_items([
            row(instruction="把日期改为三月十五日"),
            row(instruction="把日期改为三月十五日"),
        ])
        assert len(items[0]["anchors"]) == 1

    def test_different_chapters_never_share_a_lock(self, workspace):
        items = Agent(workspace)._normalize_audit_repair_items([
            row(chapter_no=7), row(chapter_no=8),
        ])
        assert sorted(i["chapter_no"] for i in items) == [7, 8]
        assert all(len(i["anchors"]) == 1 for i in items)

    def test_non_patch_class_is_not_folded_into_the_lock(self, workspace):
        """A rewrite-scale row is not auto-patchable, so it must not ride along
        on an auto item and get committed by association."""
        items = Agent(workspace)._normalize_audit_repair_items([
            row(instruction="改日期"),
            row(instruction="重写整场战斗", repair_class="REWRITE_CHAPTER"),
        ])
        assert len(items) == 2
        auto = [i for i in items if i["auto_candidate"]]
        assert len(auto) == 1
        assert len(auto[0]["anchors"]) == 1

    def test_must_preserve_is_unioned_across_units(self, workspace):
        items = Agent(workspace)._normalize_audit_repair_items([
            row(must_preserve=["甲"]), row(instruction="另一条", must_preserve=["乙"]),
        ])
        assert sorted(items[0]["must_preserve"]) == ["乙", "甲"]


class TestRenderUnits:
    def test_single_unit_is_not_numbered(self):
        text = Agent._repair_render_units([
            {"instruction": "把日期改为三月十五日", "repair_class": "TEXT_ONLY"},
        ])
        assert text == "把日期改为三月十五日"

    def test_multiple_units_carry_index_and_class(self):
        text = Agent._repair_render_units([
            {"instruction": "改日期", "repair_class": "TEXT_ONLY"},
            {"instruction": "补交代", "repair_class": "CONTINUITY_MINOR"},
        ])
        assert text == "1. [TEXT_ONLY]改日期\n2. [CONTINUITY_MINOR]补交代"

    def test_blank_instructions_are_dropped(self):
        text = Agent._repair_render_units([
            {"instruction": "  ", "repair_class": "TEXT_ONLY"},
            {"instruction": "改日期", "repair_class": "TEXT_ONLY"},
        ])
        assert text == "改日期"

    @pytest.mark.parametrize("anchors", [None, [], [{"instruction": ""}]])
    def test_degenerate_input_yields_empty_string(self, anchors):
        assert Agent._repair_render_units(anchors) == ""


ORIGINAL = "\n\n".join(f"这是第{i}段，标记{i:02d}，用于定位。" for i in range(1, 13))


def patch(unit, i, new):
    return {"unit": unit, "old": f"标记{i:02d}", "new": new, "reason": "测试"}


class TestPatchUnitAttribution:
    def test_covered_units_are_reported(self):
        _, meta = Agent._repair_apply_exact_patches(
            ORIGINAL,
            [patch(1, 1, "标记99"), patch(2, 2, "标记98")],
            "TEXT_ONLY",
            unit_count=2,
        )
        assert meta["units_covered"] == [1, 2]
        assert meta["units_uncovered"] == []

    def test_missed_unit_is_reported(self):
        """Silent partial completion is the failure this whole change targets."""
        _, meta = Agent._repair_apply_exact_patches(
            ORIGINAL, [patch(1, 1, "标记99")], "TEXT_ONLY", unit_count=2,
        )
        assert meta["units_uncovered"] == [2]

    def test_mislabelled_unit_does_not_discard_the_patch(self):
        """Attribution is advisory; a correct edit must still land."""
        result, meta = Agent._repair_apply_exact_patches(
            ORIGINAL, [patch(9, 1, "标记99")], "TEXT_ONLY", unit_count=2,
        )
        assert "标记99" in result
        assert meta["unattributed_patches"] == 1
        assert meta["patches"][0]["unit"] == 0

    def test_missing_unit_field_is_tolerated(self):
        result, meta = Agent._repair_apply_exact_patches(
            ORIGINAL,
            [{"old": "标记01", "new": "标记99", "reason": "无 unit 字段"}],
            "TEXT_ONLY",
        )
        assert "标记99" in result
        assert meta["unit_count"] == 1


class TestBudgetsScaleWithUnits:
    def _many(self, count, unit=1):
        return [patch(unit, i, f"改{i:02d}") for i in range(1, count + 1)]

    def test_single_unit_keeps_the_tight_patch_cap(self):
        with pytest.raises(ValueError, match="超过 8"):
            Agent._repair_apply_exact_patches(
                ORIGINAL, self._many(9), "TEXT_ONLY", unit_count=1,
            )

    def test_two_units_get_double_the_cap(self):
        """Folding two requirements into one chapter must not make correct work
        fail for exceeding a single-requirement budget."""
        result, meta = Agent._repair_apply_exact_patches(
            ORIGINAL, self._many(9), "TEXT_ONLY", unit_count=2,
        )
        assert meta["patch_count"] == 9
        assert "标记01" not in result

    def test_growth_cap_also_scales(self):
        big = [{"unit": 1, "old": "标记01", "new": "标" * 1200, "reason": "扩写"}]
        with pytest.raises(ValueError, match="净新增"):
            Agent._repair_apply_exact_patches(
                ORIGINAL, big, "TEXT_ONLY", unit_count=1,
            )
        _, meta = Agent._repair_apply_exact_patches(
            ORIGINAL, big, "TEXT_ONLY", unit_count=2,
        )
        assert meta["patch_growth"] > 900

    def test_scaling_never_lets_patches_overlap(self):
        """A bigger budget must not weaken the anti-rewrite guarantees.

        Both snippets below match uniquely on their own, so this reaches the
        overlap check rather than failing earlier on ambiguity. The overlapping
        patch is dropped rather than voiding the attempt, but the span itself is
        still only edited once — applying both would corrupt the text.
        """
        result, meta = Agent._repair_apply_exact_patches(
            ORIGINAL,
            [
                {"unit": 1, "old": "第1段，标记01", "new": "甲", "reason": "a"},
                {"unit": 2, "old": "标记01，用于", "new": "乙", "reason": "b"},
            ],
            "TEXT_ONLY",
            unit_count=4,
        )
        assert meta["patch_count"] == 1
        assert meta["rejected_count"] == 1
        assert "重叠" in meta["rejected"][0]["reason"]
        assert "甲" in result
        assert "乙" not in result

    def test_scaling_never_accepts_an_ambiguous_old(self):
        doubled = ORIGINAL + "\n\n重复出现的标记01，用于定位。"
        with pytest.raises(ValueError, match="出现多次"):
            Agent._repair_apply_exact_patches(
                doubled, [patch(1, 1, "标记99")], "TEXT_ONLY", unit_count=4,
            )
