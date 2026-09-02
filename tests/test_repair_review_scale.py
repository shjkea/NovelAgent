"""Joint review must stay small enough to reason over, and must stop over-blocking.

Two problems are covered here.

Chunking: a single cluster can legitimately span dozens of chapters, and handing
all of them to one call pushes the prompt past the size where the model stays
careful.  `_repair_review_chunks` splits a cluster, but the split itself is a
risk: a naive split hides the seam between two chunks, and the seam between two
neighbouring chapters is exactly where a contradiction is most likely.  So chunks
overlap by one chapter and the tests check that every adjacent pair is still
compared somewhere.

Verdicts: blocking every chapter the model failed to mention meant one sloppy
response discarded work on chapters it had no complaint about.
`_repair_joint_resolve` reads the response as an explicit whitelist plus an
explicit blocklist, and only falls back to blocking unnamed chapters when the
model actually reported a problem.  The guard in the other direction still holds:
a joint review can never approve a chapter its own independent review rejected.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _harness import LOCATOR_BLOCK, build_class

Agent = build_class("Agent", [LOCATOR_BLOCK])


class TestChunkSizing:
    """Each call must stay inside both the chapter cap and the char cap."""

    def test_small_cluster_is_one_chunk(self):
        chunks = Agent._repair_review_chunks([10, 11, 12])
        assert chunks == [[10, 11, 12]]

    def test_single_chapter_cluster(self):
        assert Agent._repair_review_chunks([10]) == [[10]]

    def test_empty_cluster(self):
        assert Agent._repair_review_chunks([]) == []
        assert Agent._repair_review_chunks(None) == []

    def test_chapter_cap_splits_long_cluster(self):
        members = list(range(1, 13))
        chunks = Agent._repair_review_chunks(members, max_chapters=4)
        assert all(len(c) <= 4 for c in chunks)
        assert len(chunks) > 1

    def test_char_cap_splits_heavy_cluster(self):
        members = [1, 2, 3, 4]
        sizes = {n: 30000 for n in members}
        chunks = Agent._repair_review_chunks(members, sizes, max_chars=50000)
        assert all(len(c) <= 2 for c in chunks)

    def test_char_cap_counts_the_carried_chapter(self):
        """The overlap chapter occupies budget in the next chunk too."""
        members = [1, 2, 3, 4, 5, 6]
        sizes = {n: 20000 for n in members}
        chunks = Agent._repair_review_chunks(members, sizes, max_chars=45000)
        for c in chunks:
            assert sum(sizes[n] for n in c) <= 45000 + 20000

    def test_one_oversized_chapter_still_gets_reviewed(self):
        """A chapter bigger than the whole budget must not vanish."""
        members = [1, 2]
        sizes = {1: 5, 2: 999999}
        chunks = Agent._repair_review_chunks(members, sizes, max_chars=100)
        seen = {n for c in chunks for n in c}
        assert seen == {1, 2}

    def test_chapter_cap_never_drops_below_two(self):
        """A cap of one would make overlap-only chunks and never advance."""
        chunks = Agent._repair_review_chunks([1, 2, 3, 4], max_chapters=1)
        assert all(len(c) >= 2 for c in chunks)
        assert {n for c in chunks for n in c} == {1, 2, 3, 4}


class TestChunkOverlap:
    """Splitting must not hide the seam between two chunks."""

    def test_chunks_overlap_by_one_chapter(self):
        chunks = Agent._repair_review_chunks(list(range(1, 10)), max_chapters=4)
        for a, b in zip(chunks, chunks[1:]):
            assert a[-1] == b[0], "seam chapter is missing from the next chunk"

    def test_every_adjacent_pair_is_compared_somewhere(self):
        """The core guarantee: no pair of neighbours goes unreviewed.

        If a split separated two chapters entirely, each would be judged without
        the other in view, which is the exact blind spot joint review exists to
        remove.
        """
        members = list(range(1, 20))
        chunks = Agent._repair_review_chunks(members, max_chapters=5)
        compared = set()
        for c in chunks:
            for i, a in enumerate(c):
                for b in c[i + 1:]:
                    compared.add((a, b))
        for a, b in zip(members, members[1:]):
            assert (a, b) in compared

    def test_no_chapter_is_dropped(self):
        members = list(range(1, 31))
        chunks = Agent._repair_review_chunks(members, max_chapters=4)
        assert {n for c in chunks for n in c} == set(members)

    def test_order_is_preserved(self):
        chunks = Agent._repair_review_chunks(list(range(1, 12)), max_chapters=3)
        for c in chunks:
            assert c == sorted(c)


class TestWhitelistSemantics:
    """An unnamed chapter is not automatically a blocked chapter."""

    def test_explicit_approval_is_honoured(self):
        approved, blocked, unresolved = Agent._repair_joint_resolve(
            [1, 2], [1, 2],
            {"approved_chapters": [1, 2], "blocked_chapters": []},
        )
        assert approved == [1, 2]
        assert blocked == []
        assert unresolved == []

    def test_explicit_block_is_honoured(self):
        approved, blocked, _ = Agent._repair_joint_resolve(
            [1, 2], [1, 2],
            {"approved_chapters": [1], "blocked_chapters": [2],
             "cross_chapter_findings": ["2 与 1 时间线冲突"]},
        )
        assert approved == [1]
        assert blocked == [2]

    def test_silence_with_no_findings_keeps_the_independent_verdict(self):
        """The over-blocking fix.

        The model reported nothing wrong and simply did not enumerate chapter 2.
        Chapter 2 already passed its own review, so it commits.
        """
        approved, blocked, unresolved = Agent._repair_joint_resolve(
            [1, 2], [1, 2],
            {"approved_chapters": [1], "blocked_chapters": [],
             "cross_chapter_findings": []},
        )
        assert approved == [1, 2]
        assert blocked == []
        assert unresolved == [2]

    def test_silence_with_findings_blocks(self):
        """A reported problem makes silence ambiguous, so it blocks.

        The finding may well be about the chapter the model forgot to list, and
        committing on that assumption is the expensive mistake.
        """
        approved, blocked, unresolved = Agent._repair_joint_resolve(
            [1, 2], [1, 2],
            {"approved_chapters": [1], "blocked_chapters": [],
             "cross_chapter_findings": ["本组存在时间线冲突"]},
        )
        assert approved == [1]
        assert blocked == [2]
        assert unresolved == [2]

    def test_silence_with_a_block_elsewhere_blocks(self):
        """Any block in the batch means the model did find something."""
        approved, blocked, _ = Agent._repair_joint_resolve(
            [1, 2, 3], [1, 2, 3],
            {"approved_chapters": [1], "blocked_chapters": [3],
             "cross_chapter_findings": []},
        )
        assert approved == [1]
        assert set(blocked) == {2, 3}

    def test_empty_response_with_clean_batch_keeps_all_verdicts(self):
        approved, blocked, unresolved = Agent._repair_joint_resolve(
            [1, 2], [1, 2], {},
        )
        assert approved == [1, 2]
        assert blocked == []
        assert unresolved == [1, 2]


class TestIndependentVerdictIsNeverOverridden:
    """Joint review may only confirm a pass, never manufacture one."""

    def test_unsafe_chapter_cannot_be_approved(self):
        """The model approves a chapter that failed its own review.

        This must not commit: the joint reviewer sees only a diff and neighbour
        summaries, so it is in no position to overturn the per-chapter check.
        """
        approved, blocked, _ = Agent._repair_joint_resolve(
            [1, 2], [1],
            {"approved_chapters": [1, 2], "blocked_chapters": []},
        )
        assert approved == [1]
        assert blocked == [2]

    def test_unsafe_and_unnamed_chapter_is_blocked_even_when_clean(self):
        approved, blocked, _ = Agent._repair_joint_resolve(
            [1, 2], [1],
            {"approved_chapters": [1], "blocked_chapters": [],
             "cross_chapter_findings": []},
        )
        assert approved == [1]
        assert blocked == [2]

    def test_explicit_block_beats_explicit_approval(self):
        approved, blocked, _ = Agent._repair_joint_resolve(
            [1], [1],
            {"approved_chapters": [1], "blocked_chapters": [1]},
        )
        assert approved == []
        assert blocked == [1]


class TestOutOfScopeAndMalformed:
    """A batch rules on its own chapters only."""

    def test_foreign_chapters_are_ignored(self):
        approved, blocked, _ = Agent._repair_joint_resolve(
            [1, 2], [1, 2],
            {"approved_chapters": [1, 2, 99], "blocked_chapters": [77]},
        )
        assert approved == [1, 2]
        assert 99 not in approved
        assert 77 not in blocked

    def test_non_numeric_entries_are_ignored(self):
        approved, blocked, _ = Agent._repair_joint_resolve(
            [1], [1],
            {"approved_chapters": ["abc", None, 1], "blocked_chapters": ["x"]},
        )
        assert approved == [1]
        assert blocked == []

    def test_string_chapter_numbers_are_accepted(self):
        approved, _, _ = Agent._repair_joint_resolve(
            [1, 2], [1, 2],
            {"approved_chapters": ["1", "2"], "blocked_chapters": []},
        )
        assert approved == [1, 2]

    def test_non_dict_response_is_treated_as_silence(self):
        approved, blocked, unresolved = Agent._repair_joint_resolve(
            [1], [1], None,
        )
        assert approved == [1]
        assert unresolved == [1]

    def test_blank_findings_do_not_count_as_a_problem(self):
        """Whitespace entries must not flip the batch into blocking mode."""
        approved, blocked, _ = Agent._repair_joint_resolve(
            [1, 2], [1, 2],
            {"approved_chapters": [1], "blocked_chapters": [],
             "cross_chapter_findings": ["", "   ", None]},
        )
        assert approved == [1, 2]
        assert blocked == []

    def test_every_chapter_gets_exactly_one_verdict(self):
        members = [1, 2, 3, 4, 5]
        approved, blocked, _ = Agent._repair_joint_resolve(
            members, [1, 2, 3],
            {"approved_chapters": [1], "blocked_chapters": [2],
             "cross_chapter_findings": ["something"]},
        )
        assert set(approved) | set(blocked) == set(members)
        assert not (set(approved) & set(blocked))
