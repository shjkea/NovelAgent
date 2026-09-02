"""Post-commit summary rebuild: who gets re-summarized, and who must not.

A committed repair changes the prose but leaves `summaries/NNNN.md` describing
the pre-repair text. The next audit reads those summaries, so a stale one makes
the audit re-report the inconsistency this batch just fixed. These tests pin the
decision layer that picks which chapters need a rebuild.

The negative guarantees that matter here:
  * a TEXT_ONLY repair must never trigger a rebuild - it only moves wording, and
    paying a Flash call per cosmetic fix is exactly the cost this work removes;
  * long-term memories must never be re-extracted, because extraction appends
    and would duplicate every fact the chapter already contributed;
  * a rebuild failure must never be able to undo a good commit.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _harness import RESUMMARIZE_BLOCK, build_class

# Header lines are written unindented; build_class adds the class-body indent.
HEADER = [
    "def __init__(self):\n"
    "    self.logs = []\n"
    "    self.written = {}\n"
    "    self.summarize_calls = []\n"
    "    self.summary_result = '重建后的摘要'\n"
    "    self.summary_error = None\n",
    "def log(self, msg):\n    self.logs.append(str(msg))",
    "def write(self, rel, text):\n    self.written[rel] = text",
    "def summarize(self, n, final):\n"
    "    self.summarize_calls.append(n)\n"
    "    if self.summary_error is not None:\n"
    "        raise self.summary_error\n"
    "    return self.summary_result\n",
    # Both memory entry points are booby-trapped: re-extracting memories after a
    # repair would append a second copy of every fact the chapter established.
    "def extract_memories(self, n, final, summary):\n"
    "    raise AssertionError('重建摘要绝不允许重跑长期记忆提取')",
    "def summarize_and_extract_memories(self, n, final):\n"
    "    raise AssertionError('重建摘要绝不允许重跑长期记忆提取')",
]

Stub = build_class("ResummarizeStub", [RESUMMARIZE_BLOCK], header_lines=HEADER)


@pytest.fixture
def cls():
    return Stub


def row(n=7, repair_class="CONTINUITY_MINOR", old="aaa", new="bbb"):
    return {"chapter_no": n, "repair_class": repair_class,
            "old_sha256": old, "new_sha256": new}


# ---------------------------------------------------------------- single chapter

def test_continuity_minor_is_rebuilt(cls):
    needed, reason = cls._repair_resummarize_reason(row(), True, True)
    assert needed is True
    assert reason == ""


def test_span_rewrite_is_rebuilt(cls):
    needed, _ = cls._repair_resummarize_reason(
        row(repair_class="REWRITE_SPAN"), True, True)
    assert needed is True


def test_chapter_rewrite_is_rebuilt(cls):
    needed, _ = cls._repair_resummarize_reason(
        row(repair_class="REWRITE_CHAPTER"), True, True)
    assert needed is True


def test_text_only_is_not_rebuilt(cls):
    """Negative guarantee: cosmetic edits must not cost a summary call.

    TEXT_ONLY is verified against a 6% change cap with a verbatim tail match, so
    wording moved but no fact did.
    """
    needed, reason = cls._repair_resummarize_reason(
        row(repair_class="TEXT_ONLY"), True, True)
    assert needed is False
    assert "TEXT_ONLY" in reason


def test_repair_class_is_matched_case_insensitively(cls):
    needed, _ = cls._repair_resummarize_reason(
        row(repair_class="continuity_minor"), True, True)
    assert needed is True


def test_repair_class_is_matched_after_stripping(cls):
    needed, _ = cls._repair_resummarize_reason(
        row(repair_class="  REWRITE_SPAN  "), True, True)
    assert needed is True


def test_unknown_class_defaults_to_rebuilding(cls):
    """An unrecognised class is assumed to move facts.

    One Flash call is cheaper than a stale summary dragging a clean chapter
    through another audit-and-repair round.
    """
    needed, reason = cls._repair_resummarize_reason(
        row(repair_class="SOMETHING_NEW"), True, True)
    assert needed is True
    assert reason == ""


def test_missing_repair_class_defaults_to_rebuilding(cls):
    r = row()
    r.pop("repair_class")
    needed, _ = cls._repair_resummarize_reason(r, True, True)
    assert needed is True


def test_none_repair_class_defaults_to_rebuilding(cls):
    needed, _ = cls._repair_resummarize_reason(
        row(repair_class=None), True, True)
    assert needed is True


def test_missing_chapter_file_is_skipped(cls):
    needed, reason = cls._repair_resummarize_reason(row(), False, True)
    assert needed is False
    assert "章节文件缺失" in reason


def test_missing_summary_is_not_invented(cls):
    """No existing summary means nothing stale is on disk to correct."""
    needed, reason = cls._repair_resummarize_reason(row(), True, False)
    assert needed is False
    assert "无既有摘要" in reason


def test_unchanged_text_is_skipped(cls):
    needed, reason = cls._repair_resummarize_reason(
        row(old="same", new="same"), True, True)
    assert needed is False
    assert "正文未发生变化" in reason


def test_identical_hashes_beat_a_rebuildable_class(cls):
    """Hash equality wins: identical bytes cannot have moved a fact."""
    needed, _ = cls._repair_resummarize_reason(
        row(repair_class="REWRITE_CHAPTER", old="x", new="x"), True, True)
    assert needed is False


def test_missing_hashes_do_not_count_as_unchanged(cls):
    r = row()
    r.pop("old_sha256")
    r.pop("new_sha256")
    needed, _ = cls._repair_resummarize_reason(r, True, True)
    assert needed is True


def test_missing_file_check_precedes_class_check(cls):
    needed, reason = cls._repair_resummarize_reason(
        row(repair_class="TEXT_ONLY"), False, True)
    assert needed is False
    assert "章节文件缺失" in reason


# ------------------------------------------------------------------- batch plan

def test_plan_splits_rebuild_and_skip(cls):
    rows = [row(1, "CONTINUITY_MINOR"), row(2, "TEXT_ONLY"),
            row(3, "REWRITE_CHAPTER")]
    plan = cls._repair_resummarize_plan(rows, lambda n, r: (True, True))
    assert plan["rebuild"] == [1, 3]
    assert [s["chapter_no"] for s in plan["skipped"]] == [2]


def test_plan_covers_every_row_exactly_once(cls):
    """Overall guarantee: no manifest row is dropped or double-counted."""
    rows = [row(1, "TEXT_ONLY"), row(2, "CONTINUITY_MINOR"),
            row(3, "REWRITE_SPAN"), row(4, "TEXT_ONLY"),
            row(5, "WHAT_IS_THIS")]
    plan = cls._repair_resummarize_plan(rows, lambda n, r: (True, True))
    seen = list(plan["rebuild"]) + [s["chapter_no"] for s in plan["skipped"]]
    assert sorted(seen) == [1, 2, 3, 4, 5]
    assert len(seen) == len(set(seen))


def test_plan_reports_unusable_chapter_number(cls):
    plan = cls._repair_resummarize_plan(
        [{"chapter_no": "第七章", "repair_class": "CONTINUITY_MINOR"}],
        lambda n, r: (True, True))
    assert plan["rebuild"] == []
    assert "章号" in plan["skipped"][0]["reason"]


def test_plan_accepts_string_chapter_numbers(cls):
    plan = cls._repair_resummarize_plan(
        [row("11", "CONTINUITY_MINOR")], lambda n, r: (True, True))
    assert plan["rebuild"] == [11]


def test_plan_handles_empty_manifest(cls):
    plan = cls._repair_resummarize_plan([], lambda n, r: (True, True))
    assert plan == {"rebuild": [], "skipped": []}


def test_plan_handles_missing_manifest(cls):
    plan = cls._repair_resummarize_plan(None, lambda n, r: (True, True))
    assert plan == {"rebuild": [], "skipped": []}


def test_plan_uses_the_probe_per_chapter(cls):
    rows = [row(1), row(2)]
    plan = cls._repair_resummarize_plan(
        rows, lambda n, r: (True, n == 1))
    assert plan["rebuild"] == [1]
    assert plan["skipped"][0]["chapter_no"] == 2


# --------------------------------------------------------------------- rebuild

def test_rebuild_writes_the_new_summary(cls, tmp_path):
    agent = cls()
    agent.root = tmp_path
    (tmp_path / "chapters").mkdir()
    (tmp_path / "summaries").mkdir()
    (tmp_path / "chapters" / "0007.md").write_text("正文", encoding="utf-8")
    (tmp_path / "summaries" / "0007.md").write_text("旧摘要", encoding="utf-8")

    report = agent._repair_rebuild_summaries([row(7)])
    assert report["rebuilt"] == [7]
    assert report["failed"] == []
    assert agent.written["summaries/0007.md"] == "重建后的摘要"


def test_rebuild_never_touches_memories(cls, tmp_path):
    """Negative guarantee: the memory store must not gain duplicate rows.

    The stub raises if either memory entry point is called.
    """
    agent = cls()
    agent.root = tmp_path
    (tmp_path / "chapters").mkdir()
    (tmp_path / "summaries").mkdir()
    (tmp_path / "chapters" / "0007.md").write_text("正文", encoding="utf-8")
    (tmp_path / "summaries" / "0007.md").write_text("旧摘要", encoding="utf-8")

    agent._repair_rebuild_summaries([row(7)])
    assert agent.summarize_calls == [7]


def test_rebuild_skips_text_only_without_calling_the_model(cls, tmp_path):
    agent = cls()
    agent.root = tmp_path
    (tmp_path / "chapters").mkdir()
    (tmp_path / "summaries").mkdir()
    (tmp_path / "chapters" / "0007.md").write_text("正文", encoding="utf-8")
    (tmp_path / "summaries" / "0007.md").write_text("旧摘要", encoding="utf-8")

    report = agent._repair_rebuild_summaries([row(7, "TEXT_ONLY")])
    assert agent.summarize_calls == []
    assert report["rebuilt"] == []
    assert report["skipped"][0]["chapter_no"] == 7


def test_rebuild_failure_is_reported_not_raised(cls, tmp_path):
    """Negative guarantee: a summary failure cannot abort a good commit."""
    agent = cls()
    agent.root = tmp_path
    agent.summary_error = RuntimeError("provider 超时")
    (tmp_path / "chapters").mkdir()
    (tmp_path / "summaries").mkdir()
    (tmp_path / "chapters" / "0007.md").write_text("正文", encoding="utf-8")
    (tmp_path / "summaries" / "0007.md").write_text("旧摘要", encoding="utf-8")

    report = agent._repair_rebuild_summaries([row(7)])
    assert report["rebuilt"] == []
    assert report["failed"][0]["chapter_no"] == 7
    assert "provider 超时" in report["failed"][0]["error"]
    # The stale summary is left in place rather than replaced by garbage.
    assert "summaries/0007.md" not in agent.written


def test_empty_summary_counts_as_a_failure(cls, tmp_path):
    agent = cls()
    agent.root = tmp_path
    agent.summary_result = "   "
    (tmp_path / "chapters").mkdir()
    (tmp_path / "summaries").mkdir()
    (tmp_path / "chapters" / "0007.md").write_text("正文", encoding="utf-8")
    (tmp_path / "summaries" / "0007.md").write_text("旧摘要", encoding="utf-8")

    report = agent._repair_rebuild_summaries([row(7)])
    assert report["failed"][0]["chapter_no"] == 7
    assert "summaries/0007.md" not in agent.written


def test_one_failure_does_not_stop_the_other_chapters(cls, tmp_path):
    agent = cls()
    agent.root = tmp_path
    (tmp_path / "chapters").mkdir()
    (tmp_path / "summaries").mkdir()
    for n in (7, 8):
        (tmp_path / "chapters" / f"{n:04d}.md").write_text("正文", encoding="utf-8")
        (tmp_path / "summaries" / f"{n:04d}.md").write_text("旧摘要", encoding="utf-8")

    calls = {"n": 0}
    real = agent.summarize

    def flaky(n, final):
        calls["n"] += 1
        if n == 7:
            raise RuntimeError("第一章失败")
        return real(n, final)

    agent.summarize = flaky
    report = agent._repair_rebuild_summaries([row(7), row(8)])
    assert report["rebuilt"] == [8]
    assert [f["chapter_no"] for f in report["failed"]] == [7]


def test_rebuild_reads_the_committed_text(cls, tmp_path):
    agent = cls()
    agent.root = tmp_path
    (tmp_path / "chapters").mkdir()
    (tmp_path / "summaries").mkdir()
    (tmp_path / "chapters" / "0007.md").write_text("提交后的新正文", encoding="utf-8")
    (tmp_path / "summaries" / "0007.md").write_text("旧摘要", encoding="utf-8")

    seen = {}

    def capture(n, final):
        seen[n] = final
        return "重建后的摘要"

    agent.summarize = capture
    agent._repair_rebuild_summaries([row(7)])
    assert seen[7] == "提交后的新正文"


def test_rebuild_trims_the_returned_summary(cls, tmp_path):
    agent = cls()
    agent.root = tmp_path
    agent.summary_result = "\n\n重建后的摘要\n\n"
    (tmp_path / "chapters").mkdir()
    (tmp_path / "summaries").mkdir()
    (tmp_path / "chapters" / "0007.md").write_text("正文", encoding="utf-8")
    (tmp_path / "summaries" / "0007.md").write_text("旧摘要", encoding="utf-8")

    agent._repair_rebuild_summaries([row(7)])
    assert agent.written["summaries/0007.md"] == "重建后的摘要"


def test_rebuild_reports_a_missing_summary_as_skipped(cls, tmp_path):
    agent = cls()
    agent.root = tmp_path
    (tmp_path / "chapters").mkdir()
    (tmp_path / "chapters" / "0007.md").write_text("正文", encoding="utf-8")

    report = agent._repair_rebuild_summaries([row(7)])
    assert agent.summarize_calls == []
    assert report["skipped"][0]["chapter_no"] == 7


def test_rebuild_logs_the_unrecovered_chapters(cls, tmp_path):
    agent = cls()
    agent.root = tmp_path
    agent.summary_error = RuntimeError("boom")
    (tmp_path / "chapters").mkdir()
    (tmp_path / "summaries").mkdir()
    (tmp_path / "chapters" / "0007.md").write_text("正文", encoding="utf-8")
    (tmp_path / "summaries" / "0007.md").write_text("旧摘要", encoding="utf-8")

    agent._repair_rebuild_summaries([row(7)])
    assert any("旧摘要" in m or "未能重建" in m for m in agent.logs)


def test_rebuild_handles_an_empty_manifest(cls, tmp_path):
    agent = cls()
    agent.root = tmp_path
    report = agent._repair_rebuild_summaries([])
    assert report == {"rebuilt": [], "failed": [], "skipped": []}
    assert agent.summarize_calls == []
