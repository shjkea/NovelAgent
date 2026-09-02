"""Tests for narrowing MANUAL_ONLY down to one genuine meaning.

MANUAL_ONLY used to absorb two unrelated situations and skip both:

  * the audit found a real problem but the extraction gave no usable anchor
  * the problem is real and clear but too big for a patch

Skipping the first threw away recoverable work, since the locator can often
anchor the quote without any model call. Skipping the second is what made the
pipeline look like it converged: the finding disappeared from the plan, so the
next audit round reported it again.

After this change MANUAL_ONLY means only "the chapter file is not there", and
everything else lands in a channel that will process it. The tests below pin
that down from both sides: recoverable rows must be promoted, and unrecoverable
ones must reach the rewrite queue rather than vanishing.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _harness import LOCATOR_BLOCK, NORMALIZE_BLOCK, TASK_BLOCK, build_class

HEADER = [
    'AUDIT_FIX_CLASSES = {"TEXT_ONLY", "CONTINUITY_MINOR", "REWRITE_SPAN", '
    '"REWRITE_CHAPTER", "DEFER_FUTURE"}',
    "def __init__(self, root=None):\n    self.root = Path(root) if root else None",
]

# TASK_BLOCK is pulled in for _repair_render_units, which the normalizer uses to
# render a chapter's units as a numbered list.
Agent = build_class(
    "Agent",
    [LOCATOR_BLOCK, TASK_BLOCK, NORMALIZE_BLOCK],
    header_lines=HEADER,
)

BODY = (
    "第七章 夜行\n\n"
    "父亲在三月十日的清晨从后门离开，没有惊动任何人。\n\n"
    "他走的时候带走了那盏铜灯，屋里只剩下冷掉的灶火。\n\n"
    "母亲第二天才发现，铜灯的位置空了一块灰。\n"
)


@pytest.fixture()
def workspace(tmp_path):
    (tmp_path / "chapters").mkdir()
    (tmp_path / "chapters" / "0007.md").write_text(BODY, encoding="utf-8")
    return tmp_path


def row(**kw):
    base = {
        "chapter_no": 7,
        "repair_class": "MANUAL_ONLY",
        "instruction": "把日期改为三月十五日",
        "reason": "与前文时间线冲突",
    }
    base.update(kw)
    return base


class TestResolveWeakClass:
    """The resolver decides which channel an under-specified row belongs to."""

    def test_locatable_quote_is_promoted_to_auto_fix(self, workspace):
        cls, info = Agent(workspace)._repair_resolve_weak_class(
            7, "父亲在三月十日的清晨从后门离开", BODY,
        )
        assert cls == "CONTINUITY_MINOR"
        assert info["resolution"] == "evidence_recovered"
        assert info["locate"]["ok"] is True

    def test_promotion_carries_the_verbatim_anchor(self, workspace):
        """The anchor must come from the chapter, not from the quote, so later
        exact-patch matching cannot fail on punctuation drift."""
        _, info = Agent(workspace)._repair_resolve_weak_class(
            7, "父亲在三月十日的清晨从后门离开。", BODY,
        )
        assert info["anchor_text"] in BODY

    def test_missing_quote_goes_to_rewrite_not_skip(self, workspace):
        cls, info = Agent(workspace)._repair_resolve_weak_class(7, "", BODY)
        assert cls == "REWRITE_SPAN"
        assert info["resolution"] == "no_evidence_to_rewrite"

    def test_unlocatable_quote_goes_to_rewrite_not_skip(self, workspace):
        """A quote that does not exist in the chapter is still a real finding;
        it just cannot be patched surgically."""
        cls, info = Agent(workspace)._repair_resolve_weak_class(
            7, "他在南门外买下了一整座宅院，摆了三天酒席。", BODY,
        )
        assert cls == "REWRITE_SPAN"
        assert info["resolution"] == "unlocatable_to_rewrite"
        assert info["locate"]["ok"] is False

    def test_resolver_never_returns_manual_only(self, workspace):
        """Whatever the input, a readable chapter always yields a live channel."""
        agent = Agent(workspace)
        for quote in ("", "不存在的引文内容", "父亲在三月十日的清晨从后门离开"):
            cls, _ = agent._repair_resolve_weak_class(7, quote, BODY)
            assert cls != "MANUAL_ONLY"


class TestNormalizeRouting:
    def test_manual_only_with_good_quote_becomes_auto_candidate(self, workspace):
        items = Agent(workspace)._normalize_audit_repair_items([
            row(evidence_quote="父亲在三月十日的清晨从后门离开"),
        ])
        assert len(items) == 1
        assert items[0]["repair_class"] == "CONTINUITY_MINOR"
        assert items[0]["auto_candidate"] is True
        assert items[0]["resolution"] == "evidence_recovered"

    def test_recovered_offsets_reach_the_anchor(self, workspace):
        items = Agent(workspace)._normalize_audit_repair_items([
            row(evidence_quote="他走的时候带走了那盏铜灯"),
        ])
        anchor = items[0]["anchors"][0]
        assert anchor["start"] >= 0
        assert anchor["end"] > anchor["start"]
        assert BODY[anchor["start"]:anchor["end"]] == anchor["anchor_text"]
        assert anchor["method"]

    def test_manual_only_without_quote_is_kept_as_rewrite(self, workspace):
        items = Agent(workspace)._normalize_audit_repair_items([row()])
        assert len(items) == 1
        assert items[0]["repair_class"] == "REWRITE_SPAN"
        assert items[0]["auto_candidate"] is False

    def test_unknown_class_is_resolved_not_discarded(self, workspace):
        items = Agent(workspace)._normalize_audit_repair_items([
            row(repair_class="LOOKS_FINE_TO_ME",
                evidence_quote="母亲第二天才发现"),
        ])
        assert items[0]["repair_class"] == "CONTINUITY_MINOR"

    def test_needs_evidence_is_resolved(self, workspace):
        items = Agent(workspace)._normalize_audit_repair_items([
            row(repair_class="NEEDS_EVIDENCE",
                evidence_quote="母亲第二天才发现"),
        ])
        assert items[0]["repair_class"] == "CONTINUITY_MINOR"

    def test_explicit_rewrite_class_is_left_alone(self, workspace):
        """An explicit scope verdict is information; do not re-derive it."""
        items = Agent(workspace)._normalize_audit_repair_items([
            row(repair_class="REWRITE_CHAPTER",
                evidence_quote="父亲在三月十日的清晨从后门离开"),
        ])
        assert items[0]["repair_class"] == "REWRITE_CHAPTER"
        assert items[0]["auto_candidate"] is False

    def test_text_only_is_not_downgraded_by_a_bad_quote(self, workspace):
        """Rows the extractor already classified confidently must not be
        re-routed just because their quote does not match."""
        items = Agent(workspace)._normalize_audit_repair_items([
            row(repair_class="TEXT_ONLY", evidence_quote="完全不匹配的内容"),
        ])
        assert items[0]["repair_class"] == "TEXT_ONLY"
        assert items[0]["auto_candidate"] is True

    def test_missing_chapter_is_the_only_manual_only(self, workspace):
        items = Agent(workspace)._normalize_audit_repair_items([
            row(chapter_no=999, evidence_quote="父亲在三月十日的清晨从后门离开"),
        ])
        assert items[0]["repair_class"] == "MANUAL_ONLY"
        assert items[0]["resolution"] == "chapter_missing"
        assert items[0]["auto_candidate"] is False

    def test_chapter_zero_is_manual_only(self, workspace):
        items = Agent(workspace)._normalize_audit_repair_items([row(chapter_no=0)])
        assert items[0]["repair_class"] == "MANUAL_ONLY"

    def test_defer_future_needs_no_chapter(self, workspace):
        """DEFER_FUTURE is about text not yet written, so chapter_no=0 is valid
        and must not be turned into a missing-chapter skip."""
        items = Agent(workspace)._normalize_audit_repair_items([
            row(chapter_no=0, repair_class="DEFER_FUTURE"),
        ])
        assert items[0]["repair_class"] == "DEFER_FUTURE"
        assert items[0]["auto_candidate"] is False

    def test_no_finding_is_dropped(self, workspace):
        """The headline guarantee: every input row still appears somewhere."""
        rows = [
            row(instruction="A", evidence_quote="父亲在三月十日的清晨从后门离开"),
            row(instruction="B", repair_class="REWRITE_SPAN"),
            row(instruction="C", chapter_no=999),
            row(instruction="D", repair_class="DEFER_FUTURE", chapter_no=0),
            row(instruction="E", repair_class="NEEDS_EVIDENCE"),
        ]
        items = Agent(workspace)._normalize_audit_repair_items(rows)
        seen = {
            a["instruction"]
            for item in items
            for a in item["anchors"]
        }
        assert seen == {"A", "B", "C", "D", "E"}

    def test_promoted_rows_share_a_chapter_lock_with_normal_rows(self, workspace):
        """A promoted row is a normal small fix, so it must join the existing
        chapter item instead of racing it for the same file."""
        items = Agent(workspace)._normalize_audit_repair_items([
            row(repair_class="TEXT_ONLY", instruction="改日期"),
            row(instruction="补铜灯交代",
                evidence_quote="他走的时候带走了那盏铜灯"),
        ])
        assert len(items) == 1
        assert len(items[0]["anchors"]) == 2
