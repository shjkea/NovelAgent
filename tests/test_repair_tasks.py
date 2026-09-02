"""Tests for zero-LLM repair task building and cluster grouping.

These two helpers replace the old Pro chunk-extract plus Pro global-review pair,
so their correctness decides whether the pipeline still skips work it should fix.
The invariants worth protecting:

  * `skipped` is reserved for DEFER_FUTURE only.  Anything else we cannot
    localise must fall through to the rewrite channel, never be dropped.
  * A located task's window must be materially smaller than the chapter, since
    that shrinkage is the entire AFP saving.
  * Two findings in one chapter stay two tasks, so one bad fix cannot poison
    an unrelated fix in the same file.
  * Joint review is only skipped when there is genuinely no cross-chapter risk.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _harness import LOCATOR_BLOCK, TASK_BLOCK, build_class

HEADER = [
    'AUDIT_FIX_CLASSES = {"TEXT_ONLY", "CONTINUITY_MINOR", "REWRITE_SPAN", '
    '"REWRITE_CHAPTER", "DEFER_FUTURE"}',
    "def __init__(self, root):\n    self.root = Path(root)",
]

Planner = build_class("Planner", [LOCATOR_BLOCK, TASK_BLOCK], header_lines=HEADER)


PARA = [
    "第七章 夜行",
    "沈砚把灯芯拨亮，窗外的雨敲在瓦上，声音密得像有人在数着什么。",
    "他记得三月十二日那天，父亲最后一次从后门离开，没有带走那只旧木匣。",
    "苏漪推门进来，肩上还沾着夜里的湿气，说码头那边的人已经等了两个时辰。",
    "「不能再等了。」她把伞靠在墙边，语气比平时急。",
    "沈砚合上账本，抬头看她，忽然觉得这个夜晚比任何一次都长。",
]
CHAPTER_TEXT = "\n\n".join(PARA)


@pytest.fixture()
def workspace(tmp_path):
    """A minimal project root: chapters/0007.md and chapters/0008.md exist."""
    (tmp_path / "chapters").mkdir()
    (tmp_path / "chapters" / "0007.md").write_text(CHAPTER_TEXT, encoding="utf-8")
    (tmp_path / "chapters" / "0008.md").write_text(
        "第八章 渡口\n\n码头的雾散得很慢，苏漪在栈桥尽头站了很久。",
        encoding="utf-8",
    )
    return tmp_path


def finding(**kw):
    base = {
        "finding_id": "V0007_001",
        "chapter_no": 7,
        "related_chapters": [],
        "issue": "时间线与前文冲突",
        "required_fix": "把日期改为三月十五日",
        "evidence_quote": "他记得三月十二日那天",
        "suggested_class": "TEXT_ONLY",
        "must_preserve": ["父亲最后一次从后门离开"],
        "confidence": "high",
    }
    base.update(kw)
    return base


class TestDeferFuture:
    def test_defer_future_is_the_only_skip(self, workspace):
        agent = Planner(workspace)
        tasks, stats = agent._build_repair_tasks_from_findings(
            [finding(suggested_class="DEFER_FUTURE")]
        )
        assert len(tasks) == 1
        assert tasks[0]["state"] == "skipped"
        assert tasks[0]["repair_class"] == "DEFER_FUTURE"
        assert tasks[0]["skip_reason"]
        assert stats["deferred"] == 1

    def test_defer_future_costs_no_location_work(self, workspace):
        """A deferred item must not even be located: it is free by construction."""
        agent = Planner(workspace)
        tasks, _ = agent._build_repair_tasks_from_findings(
            [finding(suggested_class="DEFER_FUTURE")]
        )
        assert tasks[0]["locate"] == {}


class TestMissingChapter:
    def test_absent_chapter_file_is_unlocated_not_skipped(self, workspace):
        agent = Planner(workspace)
        tasks, stats = agent._build_repair_tasks_from_findings(
            [finding(chapter_no=999, finding_id="V0999_001")]
        )
        assert tasks[0]["state"] == "unlocated"
        assert tasks[0]["state"] != "skipped"
        assert "不存在" in tasks[0]["locate"]["reason"]
        assert stats["missing_chapter"] == 1

    def test_invalid_chapter_no_is_reported(self, workspace):
        agent = Planner(workspace)
        tasks, stats = agent._build_repair_tasks_from_findings(
            [finding(chapter_no=0), finding(chapter_no="不知道")]
        )
        assert [t["state"] for t in tasks] == ["unlocated", "unlocated"]
        assert stats["missing_chapter"] == 2


class TestQuotelessFindings:
    def test_empty_quote_widens_to_rewrite_span(self, workspace):
        agent = Planner(workspace)
        tasks, stats = agent._build_repair_tasks_from_findings(
            [finding(evidence_quote="", suggested_class="CONTINUITY_MINOR")]
        )
        assert tasks[0]["state"] == "unlocated"
        assert tasks[0]["repair_class"] == "REWRITE_SPAN"
        assert stats["no_quote"] == 1

    def test_quoteless_rewrite_chapter_keeps_its_class(self, workspace):
        """Widening must never narrow an already chapter-level request."""
        agent = Planner(workspace)
        tasks, _ = agent._build_repair_tasks_from_findings(
            [finding(evidence_quote="   ", suggested_class="REWRITE_CHAPTER")]
        )
        assert tasks[0]["repair_class"] == "REWRITE_CHAPTER"


class TestSuccessfulLocate:
    def test_exact_quote_produces_located_task(self, workspace):
        agent = Planner(workspace)
        tasks, stats = agent._build_repair_tasks_from_findings([finding()])
        t = tasks[0]
        assert t["state"] == "located"
        assert t["locate"]["ok"] is True
        assert t["locate"]["method"] == "exact"
        assert stats["located"] == 1
        assert stats["by_method"]["exact"] == 1

    def test_anchor_text_is_verbatim_chapter_slice(self, workspace):
        agent = Planner(workspace)
        tasks, _ = agent._build_repair_tasks_from_findings([finding()])
        anchor = tasks[0]["anchor_text"]
        assert anchor
        assert anchor in CHAPTER_TEXT

    def test_window_is_smaller_than_chapter(self, workspace):
        """The whole point of locating: stop sending the entire chapter."""
        agent = Planner(workspace)
        tasks, _ = agent._build_repair_tasks_from_findings([finding()])
        w = tasks[0]["window"]
        assert w["chapter_chars"] == len(CHAPTER_TEXT)
        assert w["chars"] < w["chapter_chars"]
        assert CHAPTER_TEXT[w["start"]:w["end"]].count("三月十二日") == 1

    def test_punctuation_drift_still_locates(self, workspace):
        agent = Planner(workspace)
        tasks, stats = agent._build_repair_tasks_from_findings(
            [finding(evidence_quote="他记得三月十二日那天, 父亲最后一次从后门离开")]
        )
        assert tasks[0]["state"] == "located"
        assert tasks[0]["locate"]["method"] == "folded"
        assert stats["by_method"]["folded"] == 1

    def test_unrelated_quote_falls_through_to_rewrite(self, workspace):
        agent = Planner(workspace)
        tasks, stats = agent._build_repair_tasks_from_findings(
            [finding(evidence_quote="镇北将军在雪原上点齐了三千轻骑，号角声响彻整夜")]
        )
        assert tasks[0]["state"] == "unlocated"
        assert tasks[0]["repair_class"] == "REWRITE_SPAN"
        assert stats["unlocated"] == 1


class TestTaskGranularity:
    def test_two_findings_in_one_chapter_stay_two_tasks(self, workspace):
        agent = Planner(workspace)
        tasks, stats = agent._build_repair_tasks_from_findings([
            finding(finding_id="V0007_001"),
            finding(
                finding_id="V0007_002",
                evidence_quote="苏漪推门进来，肩上还沾着夜里的湿气",
                required_fix="补一句她如何得知消息",
            ),
        ])
        assert len(tasks) == 2
        assert {t["task_id"] for t in tasks} == {"V0007_001", "V0007_002"}
        assert all(t["state"] == "located" for t in tasks)
        # Independent anchors mean an failure in one cannot void the other.
        assert tasks[0]["anchor_text"] != tasks[1]["anchor_text"]
        assert stats["located"] == 2

    def test_ordering_is_stable_and_sequenced(self, workspace):
        agent = Planner(workspace)
        tasks, _ = agent._build_repair_tasks_from_findings([
            finding(chapter_no=8, finding_id="V0008_001",
                    evidence_quote="码头的雾散得很慢"),
            finding(finding_id="V0007_001"),
        ])
        assert [t["chapter_no"] for t in tasks] == [7, 8]
        assert [t["seq"] for t in tasks] == [1, 2]

    def test_non_dict_rows_are_ignored(self, workspace):
        agent = Planner(workspace)
        tasks, stats = agent._build_repair_tasks_from_findings(
            ["垃圾数据", None, finding()]
        )
        assert len(tasks) == 1
        assert stats["total"] == 1

    def test_empty_input_is_not_an_error(self, workspace):
        agent = Planner(workspace)
        tasks, stats = agent._build_repair_tasks_from_findings([])
        assert tasks == []
        assert stats["total"] == 0

    def test_unknown_class_defaults_to_conservative_mode(self, workspace):
        agent = Planner(workspace)
        tasks, _ = agent._build_repair_tasks_from_findings(
            [finding(suggested_class="随便改改")]
        )
        assert tasks[0]["repair_class"] == "CONTINUITY_MINOR"
        assert tasks[0]["suggested_class"] == ""


class TestGrouping:
    def test_isolated_text_only_skips_joint_review(self):
        clusters = Planner._repair_group_tasks([
            {"task_id": "A", "chapter_no": 12, "repair_class": "TEXT_ONLY",
             "state": "located", "related_chapters": []},
        ])
        assert len(clusters) == 1
        assert clusters[0]["needs_joint_review"] is False
        assert clusters[0]["chapters"] == [12]

    def test_continuity_minor_alone_still_needs_joint_review(self):
        clusters = Planner._repair_group_tasks([
            {"task_id": "A", "chapter_no": 12, "repair_class": "CONTINUITY_MINOR",
             "state": "located", "related_chapters": []},
        ])
        assert clusters[0]["needs_joint_review"] is True

    def test_adjacent_chapters_cluster_together(self):
        clusters = Planner._repair_group_tasks([
            {"task_id": "A", "chapter_no": 20, "repair_class": "TEXT_ONLY",
             "state": "located", "related_chapters": []},
            {"task_id": "B", "chapter_no": 21, "repair_class": "TEXT_ONLY",
             "state": "located", "related_chapters": []},
        ])
        assert len(clusters) == 1
        assert clusters[0]["chapters"] == [20, 21]
        assert clusters[0]["needs_joint_review"] is True

    def test_related_chapters_bridge_a_gap(self):
        clusters = Planner._repair_group_tasks([
            {"task_id": "A", "chapter_no": 10, "repair_class": "CONTINUITY_MINOR",
             "state": "located", "related_chapters": [40]},
            {"task_id": "B", "chapter_no": 40, "repair_class": "TEXT_ONLY",
             "state": "located", "related_chapters": []},
        ])
        assert len(clusters) == 1
        assert clusters[0]["chapters"] == [10, 40]

    def test_distant_unrelated_chapters_stay_separate(self):
        clusters = Planner._repair_group_tasks([
            {"task_id": "A", "chapter_no": 10, "repair_class": "TEXT_ONLY",
             "state": "located", "related_chapters": []},
            {"task_id": "B", "chapter_no": 50, "repair_class": "TEXT_ONLY",
             "state": "located", "related_chapters": []},
        ])
        assert len(clusters) == 2
        assert [c["chapters"] for c in clusters] == [[10], [50]]
        assert all(c["needs_joint_review"] is False for c in clusters)

    def test_skipped_tasks_are_excluded(self):
        clusters = Planner._repair_group_tasks([
            {"task_id": "A", "chapter_no": 10, "repair_class": "DEFER_FUTURE",
             "state": "skipped", "related_chapters": []},
        ])
        assert clusters == []

    def test_task_ids_cover_every_grouped_task(self):
        clusters = Planner._repair_group_tasks([
            {"task_id": "A", "chapter_no": 30, "repair_class": "TEXT_ONLY",
             "state": "located", "related_chapters": []},
            {"task_id": "B", "chapter_no": 30, "repair_class": "REWRITE_SPAN",
             "state": "unlocated", "related_chapters": []},
            {"task_id": "C", "chapter_no": 31, "repair_class": "TEXT_ONLY",
             "state": "located", "related_chapters": []},
        ])
        assert len(clusters) == 1
        assert sorted(clusters[0]["task_ids"]) == ["A", "B", "C"]

    def test_empty_input_returns_no_clusters(self):
        assert Planner._repair_group_tasks([]) == []
        assert Planner._repair_group_tasks(None) == []


class TestPlanRouting:
    """Every finding must reach exactly one channel; none may vanish."""

    def _plan(self, workspace, findings):
        agent = Planner(workspace)
        tasks, _ = agent._build_repair_tasks_from_findings(findings)
        clusters = agent._repair_group_tasks(tasks)
        return agent._repair_plan_from_tasks(tasks, clusters)

    def test_located_minor_fix_becomes_a_patch_item(self, workspace):
        items, rewrites, deferred = self._plan(workspace, [finding()])
        assert len(items) == 1
        assert (rewrites, deferred) == ([], [])
        assert items[0]["chapter_no"] == 7
        assert items[0]["auto_candidate"] is True
        assert items[0]["auto_commit_allowed"] is True
        assert items[0]["issue_id"] == "F001"

    def test_patch_item_keeps_per_task_anchors(self, workspace):
        """Chapter-level folding must not lose the individual located spans,
        or the patch stage is back to guessing inside the whole chapter."""
        items, _, _ = self._plan(workspace, [
            finding(finding_id="V0007_001"),
            finding(finding_id="V0007_002",
                    evidence_quote="苏漪推门进来，肩上还沾着夜里的湿气",
                    required_fix="补一句她如何得知消息"),
        ])
        assert len(items) == 1
        item = items[0]
        assert sorted(item["task_ids"]) == ["V0007_001", "V0007_002"]
        assert len(item["anchors"]) == 2
        assert all(a["anchor_text"] in CHAPTER_TEXT for a in item["anchors"])
        assert all(a["start"] >= 0 for a in item["anchors"])

    def test_stricter_class_wins_when_folding(self, workspace):
        items, _, _ = self._plan(workspace, [
            finding(finding_id="V0007_001", suggested_class="TEXT_ONLY"),
            finding(finding_id="V0007_002", suggested_class="CONTINUITY_MINOR",
                    evidence_quote="苏漪推门进来，肩上还沾着夜里的湿气"),
        ])
        assert items[0]["repair_class"] == "CONTINUITY_MINOR"

    def test_unlocatable_item_goes_to_rewrite_not_dropped(self, workspace):
        """An unlocatable finding must reach a runnable rewrite item.

        The queue entry records why the finding could not be patched; the `items`
        entry is what the candidate runner actually iterates. When the rewrite
        channel did not exist yet, only the queue was populated and `items` was
        empty, which meant the finding was reported but never acted on.
        """
        items, rewrites, deferred = self._plan(workspace, [
            finding(evidence_quote="镇北将军在雪原上点齐了三千轻骑，号角声响彻整夜"),
        ])
        assert deferred == []
        assert len(rewrites) == 1
        assert rewrites[0]["repair_class"] == "REWRITE_SPAN"
        assert rewrites[0]["reason"]
        assert len(items) == 1
        assert items[0]["channel"] == "rewrite"
        assert items[0]["repair_class"] == "REWRITE_SPAN"

    def test_defer_future_goes_to_deferred_only(self, workspace):
        items, rewrites, deferred = self._plan(workspace, [
            finding(suggested_class="DEFER_FUTURE"),
        ])
        assert (items, rewrites) == ([], [])
        assert len(deferred) == 1
        assert deferred[0]["skip_reason"]

    def test_three_channels_partition_all_findings(self, workspace):
        findings = [
            finding(finding_id="V0007_001"),
            finding(finding_id="V0007_002",
                    evidence_quote="镇北将军在雪原上点齐了三千轻骑，号角声响彻整夜"),
            finding(finding_id="V0008_001", chapter_no=8,
                    evidence_quote="码头的雾散得很慢"),
            finding(finding_id="V0009_001", chapter_no=9,
                    suggested_class="DEFER_FUTURE"),
            finding(finding_id="V0999_001", chapter_no=999),
        ]
        items, rewrites, deferred = self._plan(workspace, findings)

        routed = set()
        for item in items:
            routed.update(item["task_ids"])
        routed.update(r["task_id"] for r in rewrites)
        routed.update(d["task_id"] for d in deferred)
        assert routed == {f["finding_id"] for f in findings}

    def test_cluster_id_is_attached_to_items(self, workspace):
        items, _, _ = self._plan(workspace, [
            finding(finding_id="V0007_001"),
            finding(finding_id="V0008_001", chapter_no=8,
                    evidence_quote="码头的雾散得很慢"),
        ])
        # 7 and 8 are adjacent, so they share one cluster.
        assert len({i["cluster_id"] for i in items}) == 1
        assert all(i["cluster_id"] for i in items)

    def test_items_are_chapter_ordered(self, workspace):
        items, _, _ = self._plan(workspace, [
            finding(finding_id="V0008_001", chapter_no=8,
                    evidence_quote="码头的雾散得很慢"),
            finding(finding_id="V0007_001"),
        ])
        assert [i["chapter_no"] for i in items] == [7, 8]
        assert [i["issue_id"] for i in items] == ["F001", "F002"]

    def test_empty_task_list_yields_empty_channels(self, workspace):
        assert self._plan(workspace, []) == ([], [], [])
