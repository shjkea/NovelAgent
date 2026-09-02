"""Tests for the rewrite channel: routing into it, and screening what it returns.

Findings that exact local patching cannot express (a passage that has to be
re-argued, a chapter whose premise the audit rejected) used to be parked and
skipped. The finding then disappeared from the plan, which is precisely what made
the pipeline look like it converged while the problem stayed in the text and the
next audit round reported it again.

Two things have to hold for the channel to be trustworthy:

  * Routing must be total. Every rewrite task reaches a runnable item, in both the
    structured and the legacy planning path, and a chapter is written by exactly
    one generator even when its units disagree about how big the fix is.
  * Screening must be strict in the one direction that matters. A rewrite is
    allowed to change prose freely, so the cheap programmatic checks exist to
    catch a candidate that collapsed, truncated, ran away, dropped something the
    audit said to keep, or moved the chapter tail that later chapters were written
    against. Passing the screen is not acceptance, it only earns a review.

The tests are therefore weighted toward rejection: each screening signal is
checked in isolation for its ability to block, because a screen that passes an
unsafe rewrite is worse than no screen at all.
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

Agent = build_class(
    "Agent",
    [LOCATOR_BLOCK, TASK_BLOCK, NORMALIZE_BLOCK],
    header_lines=HEADER,
)

# Ten distinct paragraphs, each long enough that changing one moves the overall
# change ratio by a small, predictable amount rather than by half the chapter.
PARAS = [
    f"第{i}段：夜里的风从山口灌进来，吹得檐下的铁马叮当作响，"
    f"他数着响声，把要说的话又在心里排了一遍。" * 2
    for i in range(10)
]
BODY = "\n\n".join(PARAS)


# The filler is sized to match an original paragraph closely, so that "many
# paragraphs changed" does not also mean "chapter shrank". Otherwise a scope test
# would fail on the length bound instead of the signal it means to exercise.
FILLER = "他忽然不想再说了，转身把门轻轻掩上，屋里只剩灯芯爆响。" * 3


def rewritten(indices, filler=FILLER):
    """Return BODY with the given paragraphs replaced, everything else verbatim."""
    out = list(PARAS)
    for i in indices:
        out[i] = filler + f"（第{i}段改写）"
    return "\n\n".join(out)


class TestRewriteScreenAcceptsReasonableWork:
    """The screen must not block candidates the reviewer should be paid to judge."""

    def test_span_rewrite_touching_one_paragraph_passes(self):
        cand = rewritten([4])
        res = Agent()._repair_verify_rewrite(BODY, cand, "REWRITE_SPAN")
        assert res["verdict"] == "pass", res["reasons"]

    def test_chapter_rewrite_may_change_everything(self):
        cand = rewritten(range(9))
        res = Agent()._repair_verify_rewrite(BODY, cand, "REWRITE_CHAPTER")
        assert res["verdict"] == "pass", res["reasons"]

    def test_passing_the_screen_is_not_acceptance(self):
        """The screen reports structural sanity only; it makes no semantic claim."""
        res = Agent()._repair_verify_rewrite(BODY, rewritten([4]), "REWRITE_SPAN")
        assert set(res["checks"]) <= {
            "nonempty", "changed", "length_ok", "scope_ok",
            "tail_intact", "preserved",
        }


class TestRewriteScreenBlocksBrokenCandidates:
    """Each signal is checked alone, so none can be carried by the others."""

    def test_empty_body_is_rejected(self):
        res = Agent()._repair_verify_rewrite(BODY, "   ", "REWRITE_SPAN")
        assert res["verdict"] == "reject"
        assert res["checks"]["nonempty"] is False

    def test_unchanged_body_is_rejected(self):
        """Echoing the original back is a silent failure, not a no-op success."""
        res = Agent()._repair_verify_rewrite(BODY, BODY, "REWRITE_CHAPTER")
        assert res["verdict"] == "reject"
        assert res["checks"]["changed"] is False

    def test_truncated_body_is_rejected(self):
        cand = "\n\n".join(PARAS[:3])
        res = Agent()._repair_verify_rewrite(BODY, cand, "REWRITE_SPAN")
        assert res["verdict"] == "reject"
        assert res["checks"]["length_ok"] is False

    def test_runaway_expansion_is_rejected(self):
        cand = BODY + "\n\n" + BODY
        res = Agent()._repair_verify_rewrite(BODY, cand, "REWRITE_CHAPTER")
        assert res["verdict"] == "reject"
        assert res["checks"]["length_ok"] is False

    def test_span_rewrite_that_rewrote_the_whole_chapter_is_rejected(self):
        """Scope, not quality: a span job that touches everything overstepped."""
        cand = "\n\n".join(
            f"完全不同的第{i}段内容，措辞与原文没有任何重合之处，"
            f"叙述节奏也彻底改变了。" * 2
            for i in range(10)
        )
        res = Agent()._repair_verify_rewrite(BODY, cand, "REWRITE_SPAN")
        assert res["verdict"] == "reject"
        assert res["checks"]["scope_ok"] is False

    def test_span_rewrite_moving_the_chapter_tail_is_rejected(self):
        """The tail carries the Canon end state later chapters were written on."""
        cand = rewritten([9])
        res = Agent()._repair_verify_rewrite(BODY, cand, "REWRITE_SPAN")
        assert res["verdict"] == "reject"
        assert res["checks"]["tail_intact"] is False

    def test_dropping_a_must_preserve_phrase_is_rejected(self):
        cand = rewritten([4])
        res = Agent()._repair_verify_rewrite(
            BODY, cand, "REWRITE_SPAN",
            must_preserve=["这句话原文里根本不存在"],
        )
        assert res["verdict"] == "reject"
        assert res["checks"]["preserved"] is False

    def test_must_preserve_is_checked_verbatim_not_fuzzily(self):
        """A near-miss paraphrase still counts as dropped."""
        keep = PARAS[2][:20]
        # Every occurrence has to go: leaving one behind would satisfy the check
        # for the wrong reason and the test would pass without proving anything.
        cand = rewritten([4]).replace(keep, keep.replace("段", "節"))
        res = Agent()._repair_verify_rewrite(
            BODY, cand, "REWRITE_SPAN", must_preserve=[keep],
        )
        assert res["checks"]["preserved"] is False

    def test_blank_must_preserve_entries_are_ignored(self):
        res = Agent()._repair_verify_rewrite(
            BODY, rewritten([4]), "REWRITE_SPAN", must_preserve=["", "   ", None],
        )
        assert res["checks"]["preserved"] is True

    def test_chapter_rewrite_is_not_held_to_the_tail_rule(self):
        """A chapter rewrite is allowed to move its ending; a span rewrite is not."""
        cand = rewritten([9])
        assert Agent()._repair_verify_rewrite(
            BODY, cand, "REWRITE_CHAPTER",
        )["verdict"] == "pass"
        assert "tail_intact" not in Agent()._repair_verify_rewrite(
            BODY, cand, "REWRITE_CHAPTER",
        )["checks"]

    def test_non_rewrite_class_cannot_use_this_channel(self):
        """Guards against a patch item being fed to the rewriter by mistake."""
        for cls in ("TEXT_ONLY", "CONTINUITY_MINOR", "MANUAL_ONLY", "", None):
            res = Agent()._repair_verify_rewrite(BODY, rewritten([4]), cls)
            assert res["verdict"] == "reject"


class TestChannelSelection:
    """A chapter must be written by exactly one generator."""

    @staticmethod
    def widest(*classes):
        return Agent()._repair_widest_class(*classes)

    def test_widest_class_wins_within_a_chapter(self):
        assert self.widest("TEXT_ONLY", "REWRITE_SPAN") == "REWRITE_SPAN"
        assert self.widest("REWRITE_SPAN", "REWRITE_CHAPTER") == "REWRITE_CHAPTER"
        assert self.widest("TEXT_ONLY", "CONTINUITY_MINOR") == "CONTINUITY_MINOR"

    def test_unknown_class_never_shrinks_the_budget(self):
        """A typo in a class name must not quietly narrow a chapter's budget."""
        assert self.widest("REWRITE_CHAPTER", "TYPO_ONLY") == "REWRITE_CHAPTER"
        assert self.widest("nonsense") == "CONTINUITY_MINOR"

    def test_mixed_chapter_is_promoted_to_the_rewrite_channel(self):
        """Otherwise two generators would race on the same chapter file."""
        cls = self.widest("TEXT_ONLY", "REWRITE_SPAN")
        assert cls in Agent()._REPAIR_REWRITE_CLASSES


@pytest.fixture()
def workspace(tmp_path):
    (tmp_path / "chapters").mkdir()
    for n in (7, 8):
        (tmp_path / "chapters" / f"{n:04d}.md").write_text(BODY, encoding="utf-8")
    return tmp_path


def task(**kw):
    base = {
        "task_id": "T001",
        "chapter_no": 7,
        "repair_class": "REWRITE_SPAN",
        "instruction": "重写他决定留下的那一段，使动机与第六章一致",
        "issue": "动机与前文冲突",
        "state": "located",
        "locate": {"start": -1, "end": -1},
    }
    base.update(kw)
    return base


class TestStructuredPathRouting:
    """`_repair_plan_from_tasks`: the queue records why, `items` carries the work."""

    def test_rewrite_task_reaches_a_runnable_item(self, workspace):
        items, queue, deferred = Agent(workspace)._repair_plan_from_tasks(
            [task()], [],
        )
        assert len(queue) == 1
        assert [i["chapter_no"] for i in items] == [7]
        assert items[0]["channel"] == "rewrite"

    def test_queue_alone_would_not_be_enough(self, workspace):
        """The runner iterates `items`; a queue-only entry would never be picked up."""
        items, queue, _ = Agent(workspace)._repair_plan_from_tasks([task()], [])
        queued = {int(x["chapter_no"]) for x in queue}
        assert queued <= {int(i["chapter_no"]) for i in items}

    def test_patch_and_rewrite_in_one_chapter_yield_one_item(self, workspace):
        items, _, _ = Agent(workspace)._repair_plan_from_tasks(
            [
                task(task_id="T001", repair_class="TEXT_ONLY"),
                task(task_id="T002", repair_class="REWRITE_SPAN"),
            ],
            [],
        )
        assert len(items) == 1
        assert items[0]["channel"] == "rewrite"
        # The patch requirement survives as an instruction to the rewriter, which
        # is why folding it into the rewrite loses nothing.
        assert len(items[0]["anchors"]) == 2

    def test_deferred_tasks_do_not_enter_the_rewrite_channel(self, workspace):
        items, queue, deferred = Agent(workspace)._repair_plan_from_tasks(
            [task(repair_class="DEFER_FUTURE")], [],
        )
        assert (queue, items) == ([], [])
        assert len(deferred) == 1

    def test_every_task_lands_in_exactly_one_destination(self, workspace):
        tasks = [
            task(task_id="T001", chapter_no=7, repair_class="TEXT_ONLY"),
            task(task_id="T002", chapter_no=8, repair_class="REWRITE_CHAPTER"),
            task(task_id="T003", chapter_no=9, repair_class="DEFER_FUTURE"),
        ]
        items, queue, deferred = Agent(workspace)._repair_plan_from_tasks(tasks, [])
        placed = (
            {t for i in items for t in i["task_ids"]}
            | {x["task_id"] for x in deferred}
        )
        assert placed == {"T001", "T002", "T003"}

    def test_items_are_ordered_by_chapter(self, workspace):
        items, _, _ = Agent(workspace)._repair_plan_from_tasks(
            [task(task_id="T002", chapter_no=8), task(task_id="T001", chapter_no=7)],
            [],
        )
        assert [i["chapter_no"] for i in items] == [7, 8]
