"""Joint review is priced per cluster, so the clustering must not under-group.

Joint review exists to catch contradictions that appear only when several
candidates land together.  Previously every chapter in a batch went into one Pro
call, which meant twenty unrelated typo fixes paid for a twenty-chapter
cross-consistency analysis with nothing to find.

`_repair_review_clusters` splits the batch into groups that genuinely interact:
chapters from the same audit cluster, chapters from the same plan group, and
chapters close enough together to share scene continuity.  Anything left alone is
independent.

The dangerous direction here is under-grouping, because two chapters that should
have been compared and were not will each be approved on its own and commit a
contradiction.  So the tests lean on the grouping side: shared cause groups even
across a wide chapter gap, adjacency groups without any shared id, and the only
candidates allowed to skip the call entirely are isolated TEXT_ONLY fixes.
CONTINUITY_MINOR never skips, however lonely it looks, because it can move Canon
state on its own.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _harness import LOCATOR_BLOCK, build_class

Agent = build_class("Agent", [LOCATOR_BLOCK])


def packet(n, cls="TEXT_ONLY", cluster="", group=""):
    return {
        "chapter_no": n,
        "repair_class": cls,
        "cluster_id": cluster,
        "group_id": group,
    }


class TestSharedCauseGroups:
    """Same audit cluster or same plan group means one joint review."""

    def test_shared_cluster_id_groups_distant_chapters(self):
        """A foreshadowing cluster can span hundreds of chapters."""
        packets = [
            packet(5, cluster="C001"),
            packet(400, cluster="C001"),
        ]
        clusters, direct = Agent._repair_review_clusters(packets)
        assert clusters == [[5, 400]]
        assert direct == []

    def test_shared_group_id_groups_chapters(self):
        packets = [packet(10, group="G1"), packet(90, group="G1")]
        clusters, direct = Agent._repair_review_clusters(packets)
        assert clusters == [[10, 90]]

    def test_different_clusters_stay_apart(self):
        packets = [
            packet(10, cls="CONTINUITY_MINOR", cluster="C001"),
            packet(90, cls="CONTINUITY_MINOR", cluster="C002"),
        ]
        clusters, direct = Agent._repair_review_clusters(packets)
        assert clusters == [[10], [90]]

    def test_transitive_grouping(self):
        """A shares a cluster with B, B is adjacent to C: all three interact."""
        packets = [
            packet(10, cluster="C001"),
            packet(50, cluster="C001"),
            packet(51),
        ]
        clusters, direct = Agent._repair_review_clusters(packets)
        assert clusters == [[10, 50, 51]]


class TestAdjacencyGroups:
    """Nearby chapters share scene continuity even with no shared id."""

    def test_consecutive_chapters_group(self):
        clusters, direct = Agent._repair_review_clusters(
            [packet(10), packet(11)]
        )
        assert clusters == [[10, 11]]

    def test_gap_within_window_groups(self):
        clusters, direct = Agent._repair_review_clusters(
            [packet(10), packet(12)]
        )
        assert clusters == [[10, 12]]

    def test_gap_beyond_window_does_not_group(self):
        clusters, direct = Agent._repair_review_clusters(
            [packet(10), packet(20)]
        )
        assert clusters == []
        assert direct == [10, 20]

    def test_adjacency_window_is_configurable(self):
        packets = [packet(10), packet(20)]
        clusters, _ = Agent._repair_review_clusters(packets, adjacency=10)
        assert clusters == [[10, 20]]

    def test_chain_of_adjacent_chapters_is_one_cluster(self):
        packets = [packet(n) for n in (10, 11, 12, 13)]
        clusters, direct = Agent._repair_review_clusters(packets)
        assert clusters == [[10, 11, 12, 13]]
        assert direct == []


class TestDirectPassIsNarrow:
    """Skipping the joint call is allowed only where nothing can interact."""

    def test_isolated_text_only_skips_review(self):
        packets = [packet(10), packet(50), packet(200)]
        clusters, direct = Agent._repair_review_clusters(packets)
        assert clusters == []
        assert direct == [10, 50, 200]

    def test_isolated_continuity_minor_still_gets_reviewed(self):
        """A lone CONTINUITY_MINOR can move Canon state by itself.

        This is the guard against over-eager skipping: being far from every other
        candidate says nothing about whether this edit shifted the chapter's end
        state, and that is exactly what joint review checks.
        """
        packets = [packet(10, cls="CONTINUITY_MINOR"), packet(200)]
        clusters, direct = Agent._repair_review_clusters(packets)
        assert clusters == [[10]]
        assert direct == [200]

    def test_unknown_class_is_not_allowed_to_skip(self):
        packets = [packet(10, cls="REWRITE_SPAN"), packet(200)]
        clusters, direct = Agent._repair_review_clusters(packets)
        assert [10] in clusters
        assert 10 not in direct

    def test_blank_class_is_not_allowed_to_skip(self):
        packets = [packet(10, cls=""), packet(200)]
        clusters, direct = Agent._repair_review_clusters(packets)
        assert [10] in clusters

    def test_text_only_in_a_cluster_does_not_skip(self):
        """Cheap class, but it is next to another candidate."""
        packets = [packet(10), packet(11)]
        clusters, direct = Agent._repair_review_clusters(packets)
        assert direct == []


class TestNoChapterIsLost:
    """Every input chapter must land in exactly one bucket.

    A chapter that falls out of both lists is never approved and never blocked,
    which in the caller means it is silently dropped from the commit.
    """

    @pytest.mark.parametrize("packets", [
        [packet(1), packet(2), packet(80), packet(81), packet(200)],
        [packet(5, cluster="C1"), packet(300, cluster="C1"), packet(7)],
        [packet(n, cls="CONTINUITY_MINOR") for n in (3, 40, 41, 900)],
        [packet(1), packet(1), packet(2)],
    ])
    def test_partition_is_complete_and_disjoint(self, packets):
        clusters, direct = Agent._repair_review_clusters(packets)
        seen = [n for c in clusters for n in c] + list(direct)
        assert len(seen) == len(set(seen)), "a chapter appeared twice"
        assert set(seen) == {p["chapter_no"] for p in packets}

    def test_duplicate_chapters_collapse_to_one(self):
        packets = [packet(10), packet(10), packet(10)]
        clusters, direct = Agent._repair_review_clusters(packets)
        assert (clusters, direct) == ([], [10])

    def test_duplicate_keeps_the_stronger_class(self):
        """One chapter with a TEXT_ONLY row and a CONTINUITY_MINOR row.

        The stronger class must win, otherwise the chapter could be filed as an
        isolated TEXT_ONLY and skip the review its other row required.
        """
        packets = [
            packet(10, cls="TEXT_ONLY"),
            packet(10, cls="CONTINUITY_MINOR"),
        ]
        clusters, direct = Agent._repair_review_clusters(packets)
        assert clusters == [[10]]
        assert direct == []


class TestMalformedInput:
    """Garbage in the packet list must not take the batch down."""

    def test_empty_input(self):
        assert Agent._repair_review_clusters([]) == ([], [])
        assert Agent._repair_review_clusters(None) == ([], [])

    def test_non_dict_rows_are_ignored(self):
        clusters, direct = Agent._repair_review_clusters(
            ["nope", None, 42, packet(10)]
        )
        assert (clusters, direct) == ([], [10])

    def test_unusable_chapter_numbers_are_ignored(self):
        packets = [
            {"chapter_no": 0, "repair_class": "TEXT_ONLY"},
            {"chapter_no": -3, "repair_class": "TEXT_ONLY"},
            {"chapter_no": "abc", "repair_class": "TEXT_ONLY"},
            {"repair_class": "TEXT_ONLY"},
            packet(10),
        ]
        clusters, direct = Agent._repair_review_clusters(packets)
        assert (clusters, direct) == ([], [10])

    def test_string_chapter_numbers_are_accepted(self):
        packets = [
            {"chapter_no": "10", "repair_class": "TEXT_ONLY"},
            {"chapter_no": "11", "repair_class": "TEXT_ONLY"},
        ]
        clusters, direct = Agent._repair_review_clusters(packets)
        assert clusters == [[10, 11]]

    def test_class_matching_ignores_case_and_padding(self):
        packets = [packet(10, cls="  text_only  "), packet(200)]
        clusters, direct = Agent._repair_review_clusters(packets)
        assert direct == [10, 200]


class TestCallVolume:
    """The point of the change: cost tracks entanglement, not batch size."""

    def test_unrelated_typo_batch_needs_no_joint_call(self):
        packets = [packet(n) for n in range(10, 300, 30)]
        clusters, direct = Agent._repair_review_clusters(packets)
        assert clusters == []
        assert len(direct) == len(packets)

    def test_entangled_batch_still_gets_one_call_per_group(self):
        packets = [
            packet(10, cls="CONTINUITY_MINOR", cluster="C1"),
            packet(11, cls="CONTINUITY_MINOR", cluster="C1"),
            packet(90, cls="CONTINUITY_MINOR", cluster="C2"),
            packet(91, cls="CONTINUITY_MINOR", cluster="C2"),
        ]
        clusters, direct = Agent._repair_review_clusters(packets)
        assert clusters == [[10, 11], [90, 91]]
        assert direct == []

    def test_clusters_are_ordered_by_first_chapter(self):
        packets = [
            packet(90, cluster="C2"), packet(91, cluster="C2"),
            packet(10, cluster="C1"), packet(11, cluster="C1"),
        ]
        clusters, _ = Agent._repair_review_clusters(packets)
        assert clusters == [[10, 11], [90, 91]]
