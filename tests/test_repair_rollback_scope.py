"""Tests for per-chapter rollback scoping.

Rollback used to be all-or-nothing. The preflight walked the commit manifest and
raised on the first chapter whose on-disk hash no longer matched what the batch
had written, which aborted the rollback for every other chapter too. That made
the safety net fail in the one situation it exists for: the operator commits ten
chapters, hand-edits one, then wants the other nine back. The abort left them
with no route except copying files out of the archive manually.

Drift is a property of a single chapter, so it is now judged per chapter. The
tests below fix the two directions that matter:

  * A drifted chapter is never written. Skipping is the whole point of the check,
    so each skip reason is exercised on its own, and the batch stays marked as
    committed while any chapter still holds this batch's text.
  * A clean chapter is always restored, even when it sits next to a drifted one.

The decision layer takes a `probe` callback instead of reading files, so these
tests cover the real branching rather than a reimplementation of it.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _harness import ROLLBACK_BLOCK, build_class

Agent = build_class("Agent", [ROLLBACK_BLOCK])

COMMITTED = "hash_after_commit"
ORIGINAL = "hash_before_commit"


def row(n, new=COMMITTED, old=ORIGINAL, archive="archive/x/0001.md"):
    return {
        "chapter_no": n,
        "old_sha256": old,
        "new_sha256": new,
        "archive_file": archive,
        "repair_class": "TEXT_ONLY",
    }


def probe_all(current_hash, final=True, backup=True):
    """A probe that reports the same on-disk condition for every chapter."""
    return lambda n, r: (final, backup, current_hash)


def plan(rows, probe):
    return Agent._repair_rollback_plan(rows, probe)


class TestCleanChaptersAreRestored:
    def test_chapter_still_holding_committed_text_is_restored(self):
        res = plan([row(1)], probe_all(COMMITTED))
        assert res["restore"] == [1]
        assert res["skipped"] == []

    def test_every_clean_chapter_is_restored(self):
        rows = [row(n) for n in range(1, 6)]
        res = plan(rows, probe_all(COMMITTED))
        assert res["restore"] == [1, 2, 3, 4, 5]

    def test_chapter_already_back_at_original_is_not_drift(self):
        """A repeated rollback must not be reported as a post-commit edit."""
        res = plan([row(1)], probe_all(ORIGINAL))
        assert res["restore"] == []
        assert res["skipped"] == []
        assert [x["chapter_no"] for x in res["already"]] == [1]

    def test_missing_new_hash_falls_through_to_skip_not_restore(self):
        """Without a committed hash there is nothing to prove, so don't write."""
        res = plan([row(1, new="")], probe_all("some_other_hash"))
        assert res["restore"] == []
        assert [x["chapter_no"] for x in res["skipped"]] == [1]


class TestDriftedChaptersAreSkipped:
    def test_edited_after_commit_is_skipped(self):
        res = plan([row(1)], probe_all("hash_of_manual_edit"))
        assert res["restore"] == []
        assert [x["chapter_no"] for x in res["skipped"]] == [1]

    def test_skip_reason_explains_why(self):
        res = plan([row(1)], probe_all("hash_of_manual_edit"))
        assert "提交后又被修改" in res["skipped"][0]["reason"]

    def test_missing_chapter_file_is_skipped(self):
        res = plan([row(1)], probe_all("", final=False))
        assert res["restore"] == []
        assert "章节文件缺失" in res["skipped"][0]["reason"]

    def test_missing_archive_backup_is_skipped(self):
        """Without the backup there is no original to restore from."""
        res = plan([row(1)], probe_all(COMMITTED, backup=False))
        assert res["restore"] == []
        assert "归档备份缺失" in res["skipped"][0]["reason"]

    def test_unparsable_chapter_number_is_skipped_not_crashed(self):
        res = plan([{"chapter_no": "第一章"}], probe_all(COMMITTED))
        assert res["restore"] == []
        assert len(res["skipped"]) == 1

    def test_missing_backup_beats_hash_match(self):
        """Both conditions hold at once; the one that blocks writing must win."""
        res = plan([row(1)], probe_all(COMMITTED, backup=False))
        assert res["restore"] == []


class TestOneDriftedChapterDoesNotBlockTheRest:
    def _mixed(self):
        # Chapter 3 was hand-edited after the commit; the rest are untouched.
        rows = [row(n) for n in range(1, 6)]
        current = {n: COMMITTED for n in range(1, 6)}
        current[3] = "hash_of_manual_edit"
        return plan(rows, lambda n, r: (True, True, current[n]))

    def test_clean_chapters_are_still_restored(self):
        assert self._mixed()["restore"] == [1, 2, 4, 5]

    def test_only_the_drifted_chapter_is_skipped(self):
        assert [x["chapter_no"] for x in self._mixed()["skipped"]] == [3]

    def test_drifted_chapter_is_never_in_the_restore_set(self):
        """The negative guarantee: partial rollback must not overwrite new work."""
        assert 3 not in self._mixed()["restore"]

    def test_all_drifted_is_an_empty_restore_set(self):
        rows = [row(n) for n in range(1, 4)]
        res = plan(rows, probe_all("hash_of_manual_edit"))
        assert res["restore"] == []
        assert len(res["skipped"]) == 3


class TestPartitionIsTotal:
    """Every manifest row lands in exactly one bucket, whatever its condition."""

    def test_no_row_is_dropped_or_double_counted(self):
        rows = [row(n) for n in range(1, 8)] + [{"chapter_no": None}]
        current = {
            1: COMMITTED, 2: "edited", 3: ORIGINAL, 4: COMMITTED,
            5: "edited", 6: ORIGINAL, 7: COMMITTED,
        }
        res = plan(rows, lambda n, r: (True, True, current.get(n, "")))
        landed = (
            list(res["restore"])
            + [x["chapter_no"] for x in res["already"]]
            + [x["chapter_no"] for x in res["skipped"]]
        )
        assert len(landed) == len(rows)
        assert len(set(map(str, landed))) == len(rows)

    def test_empty_manifest_yields_empty_buckets(self):
        res = plan([], probe_all(COMMITTED))
        assert res == {"restore": [], "already": [], "skipped": []}

    def test_none_manifest_is_tolerated(self):
        assert plan(None, probe_all(COMMITTED))["restore"] == []


class TestClassifyContract:
    """The classifier is the single place that decides; check it directly."""

    def test_restore_action_for_untouched_chapter(self):
        action, reason = Agent._repair_rollback_classify(
            row(1), True, True, COMMITTED)
        assert action == Agent.ROLLBACK_RESTORE
        assert reason == ""

    def test_skip_action_carries_a_nonempty_reason(self):
        action, reason = Agent._repair_rollback_classify(
            row(1), True, True, "edited")
        assert action == Agent.ROLLBACK_SKIP
        assert reason.strip()

    def test_noop_action_for_already_original(self):
        action, _ = Agent._repair_rollback_classify(
            row(1), True, True, ORIGINAL)
        assert action == Agent.ROLLBACK_NOOP

    def test_actions_are_three_distinct_values(self):
        assert len({Agent.ROLLBACK_RESTORE, Agent.ROLLBACK_SKIP,
                    Agent.ROLLBACK_NOOP}) == 3
