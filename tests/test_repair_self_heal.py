"""Self-healing rounds must be targeted, both in what they say and what they re-run.

Two defects motivated these helpers.

Findings were broadcast: every blocked chapter received the batch-wide
`cross_chapter_findings` text, so a retry prompt for chapter 5 arrived carrying
complaints about chapters 40 and 80.  The model would either try to fix an
unrelated problem in the wrong chapter or lose the real instruction in the noise.
`_repair_findings_for_chapter` narrows this to the review chunks the chapter was
actually part of.

Re-review was total: one regenerated chapter re-ran the entire joint review, so
each rescue round cost as much as the first pass.  That is why the round limit was
pinned at one, which in turn meant conflicts needing two passes went to manual
review.  `_repair_rereview_scope` limits the re-run to the clusters containing a
changed chapter, and the round limit is now three.

The safety property throughout: narrowing must never turn a block into a pass.
An unchanged chapter carries its previous verdict forward verbatim, and anything
with no recorded verdict is treated as blocked.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _harness import LOCATOR_BLOCK, build_class

Agent = build_class("Agent", [LOCATOR_BLOCK])


def joint(clusters=None, approved=None, blocked=None, findings=None):
    out = {
        "approved_chapters": approved or [],
        "blocked_chapters": blocked or [],
        "cross_chapter_findings": findings or [],
    }
    if clusters is not None:
        out["review_clusters"] = clusters
    return out


class TestFindingsAreScopedToTheChapter:
    def test_chapter_gets_only_its_own_cluster_findings(self):
        j = joint(clusters=[
            {"chapters": [1, 2], "cross_chapter_findings": ["A 与 B 时间线冲突"]},
            {"chapters": [40, 41], "cross_chapter_findings": ["伏笔提前揭示"]},
        ])
        assert Agent._repair_findings_for_chapter(j, 1) == ["A 与 B 时间线冲突"]
        assert Agent._repair_findings_for_chapter(j, 40) == ["伏笔提前揭示"]

    def test_unrelated_findings_are_not_delivered(self):
        """The broadcast defect, stated directly.

        Chapter 1 must never see the complaint about chapter 40, or the retry will
        spend its attempt on a problem that is not in the chapter.
        """
        j = joint(clusters=[
            {"chapters": [1], "cross_chapter_findings": []},
            {"chapters": [40, 41], "cross_chapter_findings": ["伏笔提前揭示"]},
        ])
        assert Agent._repair_findings_for_chapter(j, 1) == []

    def test_chapter_in_two_overlapping_chunks_gets_both(self):
        """Overlapping chunks mean a seam chapter has two sets of findings."""
        j = joint(clusters=[
            {"chapters": [1, 2, 3], "cross_chapter_findings": ["前段冲突"]},
            {"chapters": [3, 4, 5], "cross_chapter_findings": ["后段冲突"]},
        ])
        assert Agent._repair_findings_for_chapter(j, 3) == ["前段冲突", "后段冲突"]

    def test_duplicate_findings_are_collapsed(self):
        j = joint(clusters=[
            {"chapters": [1, 2], "cross_chapter_findings": ["同一问题"]},
            {"chapters": [2, 3], "cross_chapter_findings": ["同一问题"]},
        ])
        assert Agent._repair_findings_for_chapter(j, 2) == ["同一问题"]

    def test_blank_and_null_findings_are_dropped(self):
        j = joint(clusters=[
            {"chapters": [1], "cross_chapter_findings": ["", "  ", None, "真问题"]},
        ])
        assert Agent._repair_findings_for_chapter(j, 1) == ["真问题"]

    def test_chapter_absent_from_every_cluster_gets_nothing(self):
        j = joint(clusters=[{"chapters": [1], "cross_chapter_findings": ["x"]}])
        assert Agent._repair_findings_for_chapter(j, 99) == []

    def test_legacy_report_without_clusters_falls_back_to_flat_list(self):
        """Old joint_review.json has no per-chunk record; the flat list is all there is."""
        j = joint(findings=["整批反馈"])
        assert Agent._repair_findings_for_chapter(j, 7) == ["整批反馈"]

    def test_malformed_cluster_rows_are_skipped(self):
        j = joint(clusters=[
            "not a dict",
            None,
            {"chapters": [1], "cross_chapter_findings": ["有效"]},
        ])
        assert Agent._repair_findings_for_chapter(j, 1) == ["有效"]

    def test_non_numeric_chapter_entries_are_skipped(self):
        j = joint(clusters=[
            {"chapters": ["x", 1], "cross_chapter_findings": ["有效"]},
        ])
        assert Agent._repair_findings_for_chapter(j, 1) == ["有效"]

    def test_non_dict_joint_is_empty(self):
        assert Agent._repair_findings_for_chapter(None, 1) == []

    def test_non_numeric_chapter_arg_is_empty(self):
        j = joint(clusters=[{"chapters": [1], "cross_chapter_findings": ["x"]}])
        assert Agent._repair_findings_for_chapter(j, "abc") == []


class TestRereviewScope:
    def test_only_the_affected_cluster_is_in_scope(self):
        j = joint(clusters=[
            {"cluster_chapters": [1, 2, 3], "chapters": [1, 2, 3]},
            {"cluster_chapters": [40, 41], "chapters": [40, 41]},
        ])
        assert Agent._repair_rereview_scope(j, [2]) == {1, 2, 3}

    def test_unchanged_clusters_are_excluded(self):
        """The cost fix: chapters 40 and 41 are not re-reviewed."""
        j = joint(clusters=[
            {"chapters": [1, 2]},
            {"chapters": [40, 41]},
        ])
        scope = Agent._repair_rereview_scope(j, [1])
        assert 40 not in scope and 41 not in scope

    def test_unchanged_members_of_an_affected_cluster_are_included(self):
        """Their approval was granted against a candidate that no longer exists.

        Chapter 3 did not change, but it was approved while looking at the old
        version of chapter 2.  That approval has to be re-earned.
        """
        j = joint(clusters=[{"chapters": [1, 2, 3]}])
        assert 3 in Agent._repair_rereview_scope(j, [2])

    def test_changed_chapter_is_always_in_scope(self):
        j = joint(clusters=[{"chapters": [1, 2]}])
        assert 7 in Agent._repair_rereview_scope(j, [7])

    def test_two_changed_chapters_union_their_clusters(self):
        j = joint(clusters=[
            {"chapters": [1, 2]},
            {"chapters": [40, 41]},
            {"chapters": [80]},
        ])
        scope = Agent._repair_rereview_scope(j, [1, 40])
        assert scope == {1, 2, 40, 41}

    def test_scope_is_cluster_wide_not_chunk_wide(self):
        """A change pulls in the whole cluster, not just the chunk it landed in.

        Chunks are an input-size device, not a correctness boundary: every chapter
        in a cluster was grouped precisely because it can interact with the
        others.  Narrowing to the single chunk would be cheaper but would rest on
        chunk boundaries meaning something they do not.  The savings that matter
        come from skipping unrelated clusters, which this still does.
        """
        j = joint(clusters=[
            {"cluster_chapters": [1, 2, 3, 4, 5], "chapters": [1, 2, 3]},
            {"cluster_chapters": [1, 2, 3, 4, 5], "chapters": [3, 4, 5]},
            {"cluster_chapters": [40, 41], "chapters": [40, 41]},
        ])
        scope = Agent._repair_rereview_scope(j, [4])
        assert scope == {1, 2, 3, 4, 5}
        assert 40 not in scope

    def test_chunk_membership_is_the_fallback_when_cluster_is_unrecorded(self):
        """Older records only have per-chunk chapter lists."""
        j = joint(clusters=[
            {"chapters": [1, 2, 3]},
            {"chapters": [3, 4, 5]},
        ])
        assert Agent._repair_rereview_scope(j, [4]) == {3, 4, 5}

    def test_no_changes_means_no_scope(self):
        j = joint(clusters=[{"chapters": [1, 2]}])
        assert Agent._repair_rereview_scope(j, []) == set()

    def test_legacy_report_forces_a_full_review(self):
        """Without cluster records there is no safe way to narrow.

        Returning None tells the caller to review everything rather than guess,
        because a wrong guess here would skip a chapter that needed re-checking.
        """
        assert Agent._repair_rereview_scope(joint(), [1]) is None

    def test_empty_cluster_list_forces_a_full_review(self):
        assert Agent._repair_rereview_scope(joint(clusters=[]), [1]) is None

    def test_non_dict_joint_forces_a_full_review(self):
        assert Agent._repair_rereview_scope(None, [1]) is None

    def test_malformed_rows_do_not_break_scoping(self):
        j = joint(clusters=["bad", None, {"chapters": [1, 2]}])
        assert Agent._repair_rereview_scope(j, [1]) == {1, 2}

    def test_non_numeric_changed_entries_are_ignored(self):
        j = joint(clusters=[{"chapters": [1, 2]}])
        assert Agent._repair_rereview_scope(j, ["x", None]) == set()
