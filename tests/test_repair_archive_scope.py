"""Tests for what a commit archives, and what a rollback puts back.

Committing a repaired chapter only ever wrote chapters/NNNN.md, so the archive
only held that one file. Everything derived from the chapter - its summary, its
review, its plan, its end-of-chapter state snapshot - stayed on disk describing
the pre-repair text. For a wording tweak that costs nothing. For a chapter-level
rewrite it means rolling back restores prose that no longer matches its own
summary, and there is no copy of the old summary anywhere to recover from. The
next audit then re-reports the mismatch, which is exactly the loop this work is
meant to close.

So the archive is widened to everything a chapter owns, and rollback restores the
derived files alongside the prose. Two directions are pinned down here:

  * Completeness. Every variant a chapter can own is archived and comes back
    byte-identical, including the multi-file review and plan forms.
  * Containment. Archiving chapter 7 must not touch chapter 70's files, and a
    rollback must not resurrect a file the batch never archived.

Project-wide files (state.json, current_state.json, the memory DB) are archived
but deliberately not auto-restored: they describe the whole project, so a chapter
written after this commit has already moved them on.
"""
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _harness import ARCHIVE_BLOCK, build_class

HEADER = [
    "def __init__(self, root):\n"
    "    self.root = Path(root)\n"
    "    self.logs = []\n"
    "    self.db = None\n",
    "def log(self, msg):\n    self.logs.append(str(msg))",
]

Agent = build_class(
    "Agent", [ARCHIVE_BLOCK], header_lines=HEADER,
    extra_globals={"shutil": shutil},
)


class FakeDB:
    """Stands in for MemoryDB; records where a backup was asked to go."""

    def __init__(self, fail=False):
        self.fail = fail
        self.backups = []

    def backup_to(self, dest):
        if self.fail:
            raise RuntimeError("database is locked")
        Path(dest).write_bytes(b"sqlite-bytes")
        self.backups.append(Path(dest))


# The files chapter 7 owns, in every naming form the pipeline produces.
CH7_FILES = {
    "plans/0007.md": "第七章大纲",
    "plans/0007.task.md": "第七章任务卡",
    "reviews/0007.json": '{"verdict": "PASS"}',
    "reviews/0007.initial.json": '{"round": 0}',
    "reviews/0007.deep.json": '{"deep": true}',
    "reviews/0007.round2.json": '{"round": 2}',
    "summaries/0007.md": "第七章摘要：他终于把话说了出来。",
    "runtime/state_snapshots/0007.json": '{"chapter": 7}',
}

# Files belonging to other chapters, including the digit-prefix near-miss.
OTHER_FILES = {
    "plans/0070.md": "第七十章大纲",
    "summaries/0070.md": "第七十章摘要",
    "summaries/0008.md": "第八章摘要",
    "runtime/state_snapshots/0006.json": '{"chapter": 6}',
}


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "novel"
    for rel, text in {**CH7_FILES, **OTHER_FILES}.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    (root / "chapters").mkdir(parents=True, exist_ok=True)
    (root / "chapters" / "0007.md").write_text("第七章正文", encoding="utf-8")
    (root / "state.json").write_text('{"next_chapter": 8}', encoding="utf-8")
    (root / "current_state.json").write_text('{"as_of": 7}', encoding="utf-8")
    agent = Agent(root)
    agent.db = FakeDB()
    return agent


def archive_dir(agent, name="archive/audit_fix_b1"):
    d = agent.root / name
    d.mkdir(parents=True, exist_ok=True)
    return d


class TestSidecarMatching:
    """The name test decides what belongs to a chapter; it must be exact."""

    @pytest.mark.parametrize("name", [
        "0007.md", "0007.task.md", "0007.json", "0007.initial.json",
        "0007.deep.json", "0007.round2.json",
    ])
    def test_every_variant_of_the_chapter_matches(self, name):
        assert Agent._repair_sidecar_belongs_to(name, 7)

    @pytest.mark.parametrize("name", ["0070.md", "0071.json", "00007.md"])
    def test_longer_number_does_not_match(self, name):
        """0070 must not be read as chapter 7 with a suffix."""
        assert not Agent._repair_sidecar_belongs_to(name, 7)

    @pytest.mark.parametrize("name", ["0006.md", "0008.md", "0107.md"])
    def test_other_chapters_do_not_match(self, name):
        assert not Agent._repair_sidecar_belongs_to(name, 7)

    @pytest.mark.parametrize("name", ["", None, "notes.md", "chapter7.md", "7.md"])
    def test_unnumbered_names_do_not_match(self, name):
        assert not Agent._repair_sidecar_belongs_to(name, 7)

    def test_chapter_number_may_arrive_as_string(self):
        assert Agent._repair_sidecar_belongs_to("0007.md", "7")

    def test_unparsable_chapter_number_does_not_match(self):
        assert not Agent._repair_sidecar_belongs_to("0007.md", "第七章")


class TestArchivingIsComplete:
    def test_every_owned_file_is_archived(self, project):
        d = archive_dir(project)
        rows = project._repair_archive_sidecars(7, d)
        assert {r["source"] for r in rows} == set(CH7_FILES)

    def test_archived_content_is_byte_identical(self, project):
        d = archive_dir(project)
        rows = project._repair_archive_sidecars(7, d)
        for r in rows:
            src = project.root / r["source"]
            arc = project.root / r["archive_file"]
            assert arc.read_bytes() == src.read_bytes()

    def test_archive_paths_are_project_relative_and_posix(self, project):
        d = archive_dir(project)
        rows = project._repair_archive_sidecars(7, d)
        for r in rows:
            assert "\\" not in r["archive_file"]
            assert (project.root / r["archive_file"]).exists()

    def test_state_snapshot_is_included(self, project):
        """The snapshot is what later chapters were written against."""
        d = archive_dir(project)
        rows = project._repair_archive_sidecars(7, d)
        assert "runtime/state_snapshots/0007.json" in {r["source"] for r in rows}

    def test_missing_folders_are_tolerated(self, tmp_path):
        agent = Agent(tmp_path / "empty")
        agent.db = FakeDB()
        d = archive_dir(agent)
        assert agent._repair_archive_sidecars(7, d) == []


class TestArchivingIsContained:
    def test_other_chapters_are_not_archived(self, project):
        d = archive_dir(project)
        rows = project._repair_archive_sidecars(7, d)
        sources = {r["source"] for r in rows}
        for rel in OTHER_FILES:
            assert rel not in sources

    def test_digit_prefix_near_miss_is_not_archived(self, project):
        """The negative guarantee: chapter 70 must survive a chapter 7 commit."""
        d = archive_dir(project)
        project._repair_archive_sidecars(7, d)
        assert not (d / "plans" / "0070.md").exists()

    def test_originals_are_left_in_place(self, project):
        d = archive_dir(project)
        project._repair_archive_sidecars(7, d)
        for rel in CH7_FILES:
            assert (project.root / rel).exists()


class TestProjectFilesAreArchived:
    def test_state_files_are_archived(self, project):
        d = archive_dir(project)
        rows = project._repair_archive_project_files(d)
        assert {"state.json", "current_state.json"} <= {r["source"] for r in rows}

    def test_memory_db_snapshot_is_taken(self, project):
        d = archive_dir(project)
        project._repair_archive_project_files(d)
        assert project.db.backups
        assert (d / "novel_memory_before.sqlite3").exists()

    def test_memory_db_is_flagged_as_not_auto_restored(self, project):
        """It describes the whole project, so rollback must not replay it blindly."""
        d = archive_dir(project)
        rows = project._repair_archive_project_files(d)
        row = [r for r in rows if r["source"] == "novel_memory.sqlite3"][0]
        assert row["auto_restore"] is False

    def test_failed_db_snapshot_does_not_abort_the_commit(self, project):
        """Losing the memory snapshot must not cost us the chapter archive."""
        project.db = FakeDB(fail=True)
        d = archive_dir(project)
        rows = project._repair_archive_project_files(d)
        assert {"state.json", "current_state.json"} <= {r["source"] for r in rows}
        assert any("记忆库快照失败" in m for m in project.logs)


class TestRestoreRoundTrip:
    def _commit_then_damage(self, project):
        d = archive_dir(project)
        rows = project._repair_archive_sidecars(7, d)
        # Simulate the post-commit world: derived files were regenerated (or, for
        # a rewrite, left stale) and no longer match what was archived.
        for rel in CH7_FILES:
            (project.root / rel).write_text("提交后的新内容", encoding="utf-8")
        return {"chapter_no": 7, "sidecars": rows}

    def test_all_sidecars_come_back(self, project):
        row = self._commit_then_damage(project)
        done = project._repair_restore_sidecars(row)
        assert set(done) == set(CH7_FILES)

    def test_restored_content_matches_the_original(self, project):
        row = self._commit_then_damage(project)
        project._repair_restore_sidecars(row)
        for rel, text in CH7_FILES.items():
            assert (project.root / rel).read_text(encoding="utf-8") == text

    def test_other_chapters_are_untouched_by_restore(self, project):
        row = self._commit_then_damage(project)
        project._repair_restore_sidecars(row)
        for rel, text in OTHER_FILES.items():
            assert (project.root / rel).read_text(encoding="utf-8") == text

    def test_deleted_target_directory_is_recreated(self, project):
        row = self._commit_then_damage(project)
        shutil.rmtree(project.root / "summaries")
        done = project._repair_restore_sidecars(row)
        assert "summaries/0007.md" in done
        assert (project.root / "summaries" / "0007.md").read_text(
            encoding="utf-8") == CH7_FILES["summaries/0007.md"]

    def test_legacy_manifest_without_sidecars_is_a_no_op(self, project):
        """Batches committed before this change must still roll back."""
        assert project._repair_restore_sidecars({"chapter_no": 7}) == []

    def test_missing_archive_file_is_skipped_not_raised(self, project):
        row = {"chapter_no": 7, "sidecars": [
            {"source": "summaries/0007.md",
             "archive_file": "archive/audit_fix_b1/summaries/gone.md"},
        ]}
        assert project._repair_restore_sidecars(row) == []

    def test_row_without_source_is_skipped(self, project):
        d = archive_dir(project)
        rows = project._repair_archive_sidecars(7, d)
        broken = dict(rows[0]); broken["source"] = ""
        assert project._repair_restore_sidecars(
            {"chapter_no": 7, "sidecars": [broken]}) == []
