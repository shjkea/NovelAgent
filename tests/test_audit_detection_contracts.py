"""Target contracts for the audit-to-repair detection boundary.

These tests deliberately exercise the gaps that make a locally correct audit
miss long-range continuity errors.  They are contract tests, not snapshots of
the current implementation: a failing assertion names the production behavior
that must change.

The cross-chapter evidence contract used here is::

    "evidence_quotes": [
        {"chapter_no": 3, "quote": "verbatim source fact"},
        {"chapter_no": 9, "quote": "verbatim conflicting claim"},
    ]

``evidence_quote`` remains the exact anchor in the chapter to edit.  A
cross-chapter finding is eligible for automatic repair only when confidence is
high and ``evidence_quotes`` contains verbatim evidence for both the target and
at least one related chapter.  Unsafe findings may remain in the report for
manual review, but they must not become runnable candidates.
"""

from __future__ import annotations

import ast
import hashlib
import inspect
import json
import re
import sys
from contextlib import nullcontext
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _harness import LOCATOR_BLOCK, TASK_BLOCK, build_class


ROOT = Path(__file__).resolve().parents[1]
SOURCE_PATH = ROOT / "agent_core.py"
SOURCE = SOURCE_PATH.read_text(encoding="utf-8")
PROVIDER_SOURCE = (ROOT / "provider_router.py").read_text(encoding="utf-8")


def _novel_agent_node():
    tree = ast.parse(SOURCE, filename=str(SOURCE_PATH))
    return next(
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "NovelAgent"
    )


def _method_node(name):
    return next(
        node for node in _novel_agent_node().body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == name
    )


def _method_harness(name, method_names, extra_globals=None):
    """Compile selected live NovelAgent methods into a dependency-free class."""
    selected = []
    wanted = set(method_names)
    for node in _novel_agent_node().body:
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name in wanted
        ):
            selected.append(node)

    missing = wanted - {node.name for node in selected}
    assert not missing, f"agent_core.py 缺少被测方法: {sorted(missing)}"

    class_node = ast.ClassDef(
        name=name,
        bases=[],
        keywords=[],
        body=selected,
        decorator_list=[],
    )
    module = ast.fix_missing_locations(ast.Module(body=[class_node], type_ignores=[]))
    namespace = {
        "json": json,
        "Path": Path,
        "re": __import__("re"),
    }
    namespace.update(extra_globals or {})
    exec(compile(module, str(SOURCE_PATH), "exec"), namespace)
    return namespace[name]


Collector = _method_harness(
    "Collector",
    [
        "_audit_quote_is_verbatim",
        "_audit_normalize_findings",
        "_audit_collect_findings",
        "_audit_collect_review_findings",
    ],
)
Collector.AUDIT_FIX_CLASSES = {
    "TEXT_ONLY", "CONTINUITY_MINOR", "REWRITE_SPAN",
    "REWRITE_CHAPTER", "DEFER_FUTURE",
}
Collector.AUDIT_HARD_CATEGORIES = {
    "TIME_ROLLBACK", "SCENE_REPLAY", "MEMORY_RESET", "STATE_REGRESSION",
    "RELATIONSHIP_HISTORY_REWRITE", "ITEM_STATE_RESET",
    "ABILITY_RULE_CONTRADICTION", "NUMERIC_ROLLBACK", "KNOWLEDGE_RESET",
    "LOCATION_RESET", "OTHER_HARD_CONTINUITY",
}


Verifier = _method_harness(
    "Verifier",
    [
        "_audit_json_digest",
        "_audit_assertion_inventory",
        "_audit_status_level",
        "_audit_chapter_list",
        "_audit_prepare_local_candidates",
        "_audit_quote_is_verbatim",
        "_audit_normalize_state_ledger",
        "_audit_normalize_findings",
        "_audit_partition_findings",
        "_audit_unresolved_local_candidate",
        "_audit_normalize_boundary_checks",
        "_audit_verify_segment",
        "_audit_global",
        "_audit_normalize_global_candidates",
        "_audit_global_context_chapters",
        "_audit_global_candidate_batches",
        "_audit_unresolved_global_candidate",
        "_audit_verify_global_candidates",
        "_audit_global_verify_input_hash",
    ],
    extra_globals={"hashlib": hashlib},
)
Verifier.AUDIT_FIX_CLASSES = Collector.AUDIT_FIX_CLASSES
Verifier.AUDIT_HARD_CATEGORIES = Collector.AUDIT_HARD_CATEGORIES
Verifier.AUDIT_PIPELINE_REVISION = "hard-continuity-v3-assertion-ledger-2"


Planner = build_class(
    "Planner",
    [LOCATOR_BLOCK, TASK_BLOCK],
    header_lines=[
        'AUDIT_FIX_CLASSES = {"TEXT_ONLY", "CONTINUITY_MINOR", '
        '"REWRITE_SPAN", "REWRITE_CHAPTER", "DEFER_FUTURE"}',
        "def __init__(self, root):\n    self.root = Path(root)",
    ],
)
Planner.AUDIT_HARD_CATEGORIES = Collector.AUDIT_HARD_CATEGORIES


SnapshotGuard = _method_harness(
    "SnapshotGuard",
    [
        "_validate_audit_source_snapshot",
        "_validate_repair_batch_audit_snapshot",
        "_repair_batch_dir",
    ],
    extra_globals={"hashlib": hashlib},
)

AuditSourceParser = _method_harness(
    "AuditSourceParser",
    ["_audit_source_findings"],
)

StructuredPlanCreator = _method_harness(
    "StructuredPlanCreator",
    ["_create_audit_repair_plan_from_findings"],
    extra_globals={"datetime": datetime},
)


def _finding(finding_id, target, related, target_quote, *, confidence="high",
             evidence_quotes=None):
    return {
        "finding_id": finding_id,
        "chapter_no": target,
        "related_chapters": list(related),
        "category": "STATE_REGRESSION",
        "issue": "后文对既有事实发生无依据回退",
        "required_fix": "只修正目标章的错误回忆，保持既有事实不变",
        "evidence_quote": target_quote,
        "evidence_quotes": list(evidence_quotes or []),
        "suggested_class": "CONTINUITY_MINOR",
        "must_preserve": ["源章节已经确立的事实"],
        "confidence": confidence,
    }


def test_global_findings_are_merged_into_repair_findings():
    """A finding discovered only by the global pass must reach report.json."""
    local = _finding(
        "V0002_001", 2, [1], "第二章目标证据",
        evidence_quotes=[
            {"chapter_no": 1, "quote": "第一章源事实"},
            {"chapter_no": 2, "quote": "第二章目标证据"},
        ],
    )
    global_only = _finding(
        "G0010_001", 10, [2], "第十章远程冲突",
        evidence_quotes=[
            {"chapter_no": 2, "quote": "第二章早期事实"},
            {"chapter_no": 10, "quote": "第十章远程冲突"},
        ],
    )
    segments = [{"verification": {"findings": [local]}}]
    global_result = {"findings": [global_only]}

    signature = inspect.signature(Collector._audit_collect_findings)
    assert "global_result" in signature.parameters, (
        "_audit_collect_findings 必须接收 global_result；否则全局 Pro 发现的"
        "跨窗口问题无法进入 repair findings"
    )
    rows = Collector()._audit_collect_findings(
        segments, global_result=global_result,
    )
    assert {row["finding_id"] for row in rows} == {
        "V0002_001", "G0010_001",
    }


def test_same_temporary_id_cannot_swallow_a_different_finding():
    """Overlapping windows may reuse a temporary id; content must still survive."""
    first = _finding("V0004_001", 4, [3], "第一个目标锚点")
    second = _finding("V0004_001", 4, [3], "第二个目标锚点")
    rows = Collector()._audit_collect_findings([
        {"verification": {"findings": [first]}},
        {"verification": {"findings": [second]}},
    ])
    assert len(rows) == 2
    assert {row["evidence_quote"] for row in rows} == {
        "第一个目标锚点", "第二个目标锚点",
    }
    assert len({row["finding_id"] for row in rows}) == 2


def test_same_anchor_with_different_repair_obligations_is_not_deduplicated():
    first = _finding("V0004_001", 4, [3], "同一个目标锚点")
    second = _finding("V0004_001", 4, [2], "同一个目标锚点")
    second["issue"] = "同一句还改写了另一段旧历史"
    second["required_fix"] = "恢复第二段既有历史，不改动第一段修正"
    rows = Collector()._audit_collect_findings([
        {"verification": {"findings": [first, second]}},
    ])
    assert len(rows) == 2
    assert len({row["finding_id"] for row in rows}) == 2


def test_review_only_temporary_ids_are_made_unique():
    first = _finding("V0004_001", 4, [3], "第一个人工锚点")
    second = _finding("V0004_001", 4, [2], "第二个人工锚点")
    rows = Collector._audit_collect_review_findings([
        {"verification": {"review_findings": [first, second]}},
    ])
    assert len(rows) == 2
    assert len({row["finding_id"] for row in rows}) == 2


def test_story_audit_passes_global_result_to_finding_collector():
    """The report-building call site must not discard the global result."""
    method = _method_node("_run_story_audit")
    calls = [
        node for node in ast.walk(method)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "_audit_collect_findings"
    ]
    assert len(calls) == 1, "_run_story_audit 应且只应汇总一次 repair findings"
    call = calls[0]
    passes_global = (
        len(call.args) >= 2
        or any(keyword.arg == "global_result" for keyword in call.keywords)
    )
    assert passes_global, (
        "_run_story_audit 当前只汇总分窗 verification；必须把 global_result "
        "同时传给 _audit_collect_findings"
    )


def test_flash_green_cannot_skip_pro_independent_window_scan():
    """The Pro call may depend on source_check, never on Flash's verdict."""
    method = _method_node("_run_story_audit")
    source = ast.get_source_segment(SOURCE, method) or ""
    assert "if bool(source_check):" in source
    assert "audit.get(\"status\") in {\"ORANGE\", \"RED\"}" not in source
    assert "verification = self._audit_verify_segment(audit)" in source
    assert "self._audit_verify_global_candidates(" in source


def test_verifier_loads_target_and_related_chapter_full_text(tmp_path):
    """A Pro verifier cannot confirm a contradiction without both source texts."""
    chapters = tmp_path / "chapters"
    chapters.mkdir()
    source_marker = "SOURCE-CHAPTER-10: 他从未去过那家旅馆。"
    target_marker = "TARGET-CHAPTER-12: 他记得上次就在那家旅馆。"
    (chapters / "0010.md").write_text(source_marker, encoding="utf-8")
    (chapters / "0012.md").write_text(target_marker, encoding="utf-8")

    captured = {}
    verifier = Verifier()
    verifier.root = tmp_path
    verifier._audit_outline_for_range = lambda *_: "outline"

    def fake_chat(stage, system, user, **kwargs):
        captured["user"] = user
        return {
            "status": "GREEN",
            "suspect_chapters": [],
            "findings": [],
            "recommended_action": "继续",
        }

    verifier._audit_chat = fake_chat
    verifier._audit_verify_segment({
        "start": 10,
        "end": 12,
        "evidence_findings": [{
            "chapter_no": 12,
            "related_chapters": [10],
            "evidence_quote": target_marker,
        }],
        "deterministic_findings": [],
    })

    prompt = captured["user"]
    assert source_marker in prompt, (
        "正文复核提示缺少 related_chapters 的源章节全文；单看目标章无法"
        "确认跨章记忆冲突"
    )
    assert target_marker in prompt


def test_window_verifier_accepts_confirmed_findings_compatibility_field(tmp_path):
    """A valid Pro result must survive when returned only in the legacy field."""
    chapters = tmp_path / "chapters"
    chapters.mkdir()
    source_quote = "SOURCE-FACT-A."
    target_quote = "TARGET-CONFLICT-B."
    (chapters / "0001.md").write_text(source_quote, encoding="utf-8")
    (chapters / "0002.md").write_text(target_quote, encoding="utf-8")

    verifier = Verifier()
    verifier.root = tmp_path
    verifier._audit_outline_for_range = lambda *_: "outline"
    verifier._audit_chat = lambda *_args, **_kwargs: {
        "status": "ORANGE",
        "findings": [],
        "confirmed_findings": [{
            "chapter_no": 2,
            "related_chapters": [1],
            "category": "STATE_REGRESSION",
            "issue": "后文把已确立状态改写",
            "required_fix": "只修正目标句并恢复旧状态",
            "evidence_quote": target_quote,
            "evidence_quotes": [
                {"chapter_no": 1, "quote": source_quote},
                {"chapter_no": 2, "quote": target_quote},
            ],
            "suggested_class": "CONTINUITY_MINOR",
            "confidence": "high",
        }],
        "boundary_checks": [{
            "from_chapter": 1,
            "to_chapter": 2,
            "relation": "CONTINUOUS",
            "result": "CONSISTENT",
            "reason": "接缝本身连续",
        }],
        "assertion_checks": [],
        "state_ledger": [],
        "suspect_chapters": [1, 2],
        "recommended_action": "人工检查局部",
    }

    result = verifier._audit_verify_segment({
        "start": 1,
        "end": 2,
        "evidence_findings": [],
        "deterministic_findings": [],
    })

    assert len(result["findings"]) == 1
    assert result["findings"][0]["evidence_quote"] == target_quote
    assert result["findings"][0]["repair_ready"] is True


def test_window_verifier_accounts_for_every_flash_and_rule_candidate(tmp_path):
    """Ready wins; omitted and false-positive local candidates remain reviewable."""
    chapters = tmp_path / "chapters"
    chapters.mkdir()
    source_quote = "SOURCE-FACT-A."
    ready_quote = "READY-TARGET-B."
    omitted_quote = "OMITTED-TARGET-C."
    false_quote = "FALSE-TARGET-D."
    for n, text in {
        1: source_quote,
        2: ready_quote,
        3: omitted_quote,
        4: false_quote,
    }.items():
        (chapters / f"{n:04d}.md").write_text(text, encoding="utf-8")

    verifier = Verifier()
    verifier.root = tmp_path
    verifier._audit_outline_for_range = lambda *_: "outline"
    verifier._audit_chat = lambda *_args, **_kwargs: {
        "status": "ORANGE",
        "findings": [],
        "confirmed_findings": [{
            "candidate_id": "LF001",
            "chapter_no": 2,
            "related_chapters": [1],
            "category": "STATE_REGRESSION",
            "issue": "后文把已确立状态改写",
            "required_fix": "只修正目标句并恢复旧状态",
            "evidence_quote": ready_quote,
            "evidence_quotes": [
                {"chapter_no": 1, "quote": source_quote},
                {"chapter_no": 2, "quote": ready_quote},
            ],
            "suggested_class": "CONTINUITY_MINOR",
            "confidence": "high",
        }],
        "candidate_dispositions": [{
            "candidate_id": "LF001",
            "disposition": "CONFIRMED",
            "reason": "双边逐字证据成立",
        }],
        "false_positives": [{
            "candidate_id": "LD001",
            "reason": "确定性规则没有识别到合理时间省略",
        }],
        "boundary_checks": [
            {
                "from_chapter": n,
                "to_chapter": n + 1,
                "relation": "CONTINUOUS",
                "result": "CONSISTENT",
                "reason": "接缝本身连续",
            }
            for n in range(1, 4)
        ],
        "assertion_checks": [],
        "state_ledger": [],
        "suspect_chapters": [1, 2, 3, 4],
        "recommended_action": "人工检查局部",
    }
    segment = {
        "start": 1,
        "end": 4,
        "evidence_findings": [
            {
                "candidate_id": "LF001",
                "chapter_no": 2,
                "related_chapters": [1],
                "category": "STATE_REGRESSION",
                "issue": "后文把已确立状态改写",
                "evidence_quote": ready_quote,
                "evidence_quotes": [
                    {"chapter_no": 1, "quote": source_quote},
                    {"chapter_no": 2, "quote": ready_quote},
                ],
            },
            {
                "candidate_id": "LF002",
                "chapter_no": 3,
                "related_chapters": [1],
                "category": "MEMORY_RESET",
                "issue": "Flash 候选被 Pro 完全漏回",
                "evidence_quote": omitted_quote,
            },
        ],
        "deterministic_findings": [{
            "candidate_id": "LD001",
            "chapter_no": 4,
            "related_chapters": [3],
            "code": "TIME_REGRESSION",
            "message": "确定性规则提出时间回退候选",
            "evidence": false_quote,
        }],
    }

    result = verifier._audit_verify_segment(segment)
    ready_ids = {
        row.get("candidate_id") for row in result.get("findings") or []
    }
    review_by_id = {
        row.get("candidate_id"): row
        for row in result.get("review_findings") or []
    }

    assert ready_ids == {"LF001"}
    assert set(review_by_id) == {"LF002", "LD001"}
    assert ready_ids.isdisjoint(review_by_id)
    assert review_by_id["LF002"]["candidate_disposition"] == "omitted"
    assert review_by_id["LD001"]["candidate_disposition"] == "false_positive"
    assert "合理时间省略" in json.dumps(
        review_by_id["LD001"], ensure_ascii=False,
    )


def test_window_pro_prompt_requires_boundary_and_assertion_checklists(tmp_path):
    """Independent Pro review must explicitly account for seams and hard claims."""
    chapters = tmp_path / "chapters"
    chapters.mkdir()
    (chapters / "0010.md").write_text(
        "第十章结束时是周一。他从未改过呼吸节奏。", encoding="utf-8",
    )
    (chapters / "0011.md").write_text(
        "第十一章开头仍是周一。", encoding="utf-8",
    )
    (chapters / "0012.md").write_text(
        "第十二章称这是第一次调整呼吸节奏。", encoding="utf-8",
    )

    captured = {}
    verifier = Verifier()
    verifier.root = tmp_path
    verifier._audit_outline_for_range = lambda *_: "outline"

    def fake_chat(stage, system, user, **kwargs):
        captured.update(system=system, user=user)
        return {
            "status": "GREEN",
            "suspect_chapters": [],
            "findings": [],
            "state_ledger": [],
            "recommended_action": "继续",
        }

    verifier._audit_chat = fake_chat
    verifier._audit_verify_segment({
        "start": 10,
        "end": 12,
        "evidence_findings": [],
        "deterministic_findings": [],
    })

    prompt = f"{captured['system']}\n{captured['user']}"
    for field in (
        '"boundary_checks"', '"from_chapter"', '"to_chapter"',
        '"assertion_checks"', '"assertion_id"',
    ):
        assert field in prompt, f"窗口 Pro 输出合同缺少 {field}"
    assert re.search(r"(?:每个|全部).{0,16}相邻.{0,16}(?:接缝|边界)", prompt), (
        "窗口 Pro 必须逐项覆盖全部相邻章节接缝，不能只自由描述疑点"
    )
    assert "硬断言" in prompt and "每个 assertion_id" in prompt, (
        "窗口 Pro 必须逐项核对第一次/从未/一直/最新/总共等硬断言"
    )


def test_global_prompt_carries_window_hard_assertion_checklist():
    """The global pass needs the window verifier's assertion-level evidence."""
    assertion_marker = "ASSERTION-MARKER: 他从未改过呼吸节奏。"
    captured = {}
    verifier = Verifier()
    verifier._audit_outline_index = lambda *_: "outline-index"

    def fake_chat(stage, system, user, **kwargs):
        captured.update(system=system, user=user)
        return {
            "status": "GREEN",
            "candidate_findings": [],
            "recommended_action": "继续",
            "overall_summary": "未发现硬错误",
        }

    verifier._audit_chat = fake_chat
    verifier._audit_global(10, 12, [{
        "segment": "第10-12章",
        "audit": {"status": "GREEN", "state_ledger": []},
        "verification": {
            "status": "YELLOW",
            "state_ledger": [],
            "findings": [],
            "review_findings": [],
            "assertion_checks": [{
                "chapter_no": 10,
                "assertion_quote": assertion_marker,
                "assertion_type": "NEVER",
                "verdict": "SUSPECT",
                "related_chapters": [12],
            }],
        },
    }])

    prompt = captured["user"]
    assert assertion_marker in prompt, (
        "全局 Pro 输入丢弃了窗口 Pro 已抽取的硬断言，跨窗口无法继续比对"
    )
    assert '"assertion_checks"' in prompt or "硬断言清单" in prompt


def test_global_verifier_keeps_multiple_same_chapter_findings_unique(tmp_path):
    chapters = tmp_path / "chapters"
    chapters.mkdir()
    old_quote = "旧状态已经明确成立。"
    target_a = "后文错误地把状态改回甲。"
    target_b = "后文又错误地忘记了旧知识。"
    (chapters / "0001.md").write_text(old_quote, encoding="utf-8")
    (chapters / "0002.md").write_text(
        f"{target_a}\n{target_b}", encoding="utf-8",
    )

    verifier = Verifier()
    verifier.root = tmp_path
    verifier._audit_chat = lambda *_args, **_kwargs: {
        "findings": [
            {
                "candidate_id": "GC001",
                "chapter_no": 2,
                "related_chapters": [1],
                "category": "STATE_REGRESSION",
                "issue": "状态回退",
                "required_fix": "只修正目标句，保留旧状态",
                "evidence_quote": target_a,
                "evidence_quotes": [
                    {"chapter_no": 1, "quote": old_quote},
                    {"chapter_no": 2, "quote": target_a},
                ],
                "suggested_class": "CONTINUITY_MINOR",
                "confidence": "high",
            },
            {
                "candidate_id": "GC002",
                "chapter_no": 2,
                "related_chapters": [1],
                "category": "KNOWLEDGE_RESET",
                "issue": "知识回退",
                "required_fix": "只修正目标句，保留已知信息",
                "evidence_quote": target_b,
                "evidence_quotes": [
                    {"chapter_no": 1, "quote": old_quote},
                    {"chapter_no": 2, "quote": target_b},
                ],
                "suggested_class": "CONTINUITY_MINOR",
                "confidence": "high",
            },
        ],
        "false_positives": [],
    }
    result = verifier._audit_verify_global_candidates(1, 2, [
        {
            "candidate_id": "GC001", "chapter_no": 2,
            "related_chapters": [1], "category": "STATE_REGRESSION",
        },
        {
            "candidate_id": "GC002", "chapter_no": 2,
            "related_chapters": [1], "category": "KNOWLEDGE_RESET",
        },
    ])
    assert len(result["findings"]) == 2
    assert len({row["finding_id"] for row in result["findings"]}) == 2


def test_global_verifier_preserves_unresolved_candidates_for_review(tmp_path):
    """Every candidate needs a terminal bucket; ready rows must not duplicate."""
    chapters = tmp_path / "chapters"
    chapters.mkdir()
    old_quote = "第一章已经确立旧状态。"
    ready_quote = "第二章明确把状态改回去了。"
    for n, text in {
        1: old_quote,
        2: ready_quote,
        3: "第三章候选没有收到结构化终审结果。",
        4: "第四章候选被终审列为存在其他解释。",
    }.items():
        (chapters / f"{n:04d}.md").write_text(text, encoding="utf-8")

    verifier = Verifier()
    verifier.root = tmp_path
    verifier._audit_chat = lambda *_args, **_kwargs: {
        "findings": [{
            "candidate_id": "GC001",
            "chapter_no": 2,
            "related_chapters": [1],
            "category": "STATE_REGRESSION",
            "issue": "状态回退",
            "required_fix": "只修正目标句",
            "evidence_quote": ready_quote,
            "evidence_quotes": [
                {"chapter_no": 1, "quote": old_quote},
                {"chapter_no": 2, "quote": ready_quote},
            ],
            "suggested_class": "CONTINUITY_MINOR",
            "confidence": "high",
        }],
        # A contradictory model response must resolve in favor of the
        # evidence-ready row, while an explicit rejection remains reviewable.
        "false_positives": [
            {"candidate_id": "GC001", "reason": "模型同时误列为误报"},
            {"candidate_id": "GC003", "reason": "原文存在其他合理解释"},
        ],
    }
    candidates = [
        {
            "candidate_id": "GC001", "chapter_no": 2,
            "related_chapters": [1], "category": "STATE_REGRESSION",
            "issue": "状态回退",
        },
        {
            "candidate_id": "GC002", "chapter_no": 3,
            "related_chapters": [1], "category": "MEMORY_RESET",
            "issue": "终审遗漏了这个候选",
        },
        {
            "candidate_id": "GC003", "chapter_no": 4,
            "related_chapters": [1], "category": "KNOWLEDGE_RESET",
            "issue": "终审认为存在其他解释",
        },
    ]

    result = verifier._audit_verify_global_candidates(1, 4, candidates)
    ready_ids = {
        row.get("candidate_id") for row in result.get("findings") or []
    }
    review_by_id = {
        row.get("candidate_id"): row
        for row in result.get("review_findings") or []
    }

    assert ready_ids == {"GC001"}
    assert set(review_by_id) == {"GC002", "GC003"}
    assert ready_ids.isdisjoint(review_by_id), (
        "已通过证据门的候选不能再重复出现在人工复核列表"
    )
    assert all(row.get("repair_ready") is False for row in review_by_id.values())
    assert all(row.get("source_scope") == "global" for row in review_by_id.values())
    assert review_by_id["GC002"].get("gate_reasons"), (
        "模型漏回的候选必须说明为何降入人工复核"
    )
    assert "原文存在其他合理解释" in json.dumps(
        review_by_id["GC003"], ensure_ascii=False,
    )


def test_global_verifier_prompt_and_hash_cover_endpoint_neighbors(tmp_path):
    """Full-text verification and its checkpoint hash must use the same context."""
    chapters = tmp_path / "chapters"
    chapters.mkdir()
    markers = {}
    for n in range(1, 9):
        markers[n] = f"[BODY-C{n:02d}] 第{n}章唯一正文。"
        (chapters / f"{n:04d}.md").write_text(markers[n], encoding="utf-8")

    captured = {}
    verifier = Verifier()
    verifier.root = tmp_path

    def fake_chat(stage, system, user, **kwargs):
        captured["user"] = user
        return {"findings": [], "false_positives": []}

    verifier._audit_chat = fake_chat
    candidates = [{
        "candidate_id": "GC001",
        "chapter_no": 6,
        "related_chapters": [3],
        "category": "STATE_REGRESSION",
        "issue": "跨窗口状态疑似回退",
    }]
    before = verifier._audit_global_verify_input_hash(1, 8, candidates)
    verifier._audit_verify_global_candidates(1, 8, candidates)

    prompt = captured["user"]
    expected_context = {2, 3, 4, 5, 6, 7}
    assert all(markers[n] in prompt for n in expected_context), (
        "全局终审必须回读目标章与每个关联章各自的前后邻章"
    )

    (chapters / "0002.md").write_text(
        markers[2] + " RELATED-NEIGHBOR-CHANGED", encoding="utf-8",
    )
    related_neighbor_changed = verifier._audit_global_verify_input_hash(
        1, 8, candidates,
    )
    assert related_neighbor_changed != before, (
        "关联端点邻章变化后不得复用旧全局终审断点"
    )

    (chapters / "0007.md").write_text(
        markers[7] + " TARGET-NEIGHBOR-CHANGED", encoding="utf-8",
    )
    target_neighbor_changed = verifier._audit_global_verify_input_hash(
        1, 8, candidates,
    )
    assert target_neighbor_changed != related_neighbor_changed, (
        "目标端点邻章变化后不得复用旧全局终审断点"
    )


def test_weak_evidence_may_preview_but_cannot_gain_commit_eligibility(tmp_path):
    """Weak findings may create reversible candidates, never committable ones."""
    chapters = tmp_path / "chapters"
    chapters.mkdir()
    for n in range(1, 7):
        (chapters / f"{n:04d}.md").write_text(
            f"第{n}章。UNIQUE-TARGET-{n}。", encoding="utf-8",
        )

    low_confidence = _finding(
        "LOW", 2, [1], "UNIQUE-TARGET-2", confidence="low",
        evidence_quotes=[
            {"chapter_no": 1, "quote": "第1章"},
            {"chapter_no": 2, "quote": "UNIQUE-TARGET-2"},
        ],
    )
    one_sided = _finding(
        "ONE_SIDE", 4, [3], "UNIQUE-TARGET-4",
        evidence_quotes=[
            {"chapter_no": 4, "quote": "UNIQUE-TARGET-4"},
        ],
    )
    safe = _finding(
        "SAFE", 6, [5], "UNIQUE-TARGET-6",
        evidence_quotes=[
            {"chapter_no": 5, "quote": "第5章"},
            {"chapter_no": 6, "quote": "UNIQUE-TARGET-6"},
        ],
    )

    planner = Planner(tmp_path)
    tasks, _ = planner._build_repair_tasks_from_findings([
        low_confidence, one_sided, safe,
    ])
    clusters = planner._repair_group_tasks(tasks)
    items, _, _ = planner._repair_plan_from_tasks(tasks, clusters)
    previewable = {
        task_id
        for item in items if item.get("auto_candidate")
        for task_id in item.get("task_ids") or []
    }
    committable = {
        task_id
        for item in items if item.get("auto_commit_allowed")
        for task_id in item.get("task_ids") or []
    }

    assert previewable == {"SAFE", "LOW", "ONE_SIDE"}
    assert committable == {"SAFE"}, (
        "只有双章逐字证据充分的 high finding 才能获得批量提交资格"
    )


def test_repeated_target_quote_can_preview_but_cannot_be_committed(tmp_path):
    chapters = tmp_path / "chapters"
    chapters.mkdir()
    (chapters / "0001.md").write_text("旧事实明确。", encoding="utf-8")
    (chapters / "0002.md").write_text(
        "重复目标。中间内容。重复目标。", encoding="utf-8",
    )
    planner = Planner(tmp_path)
    tasks, _ = planner._build_repair_tasks_from_findings([
        _finding(
            "AMBIGUOUS", 2, [1], "重复目标。",
            evidence_quotes=[
                {"chapter_no": 1, "quote": "旧事实明确。"},
                {"chapter_no": 2, "quote": "重复目标。"},
            ],
        ),
    ])
    assert tasks[0]["auto_candidate_allowed"] is True
    assert tasks[0]["auto_commit_allowed"] is False
    assert any("唯一定位" in reason for reason in tasks[0]["evidence_gate_reasons"])


def test_pro_finding_normalizer_requires_exact_two_sided_canon(tmp_path):
    chapters = tmp_path / "chapters"
    chapters.mkdir()
    source = "旧事实：他从未去过那家旅馆。"
    target = "错误回忆：上次就是在那家旅馆。"
    (chapters / "0001.md").write_text(source, encoding="utf-8")
    (chapters / "0002.md").write_text(target, encoding="utf-8")

    verifier = Verifier()
    verifier.root = tmp_path
    raw = {
        "chapter_no": 2,
        "related_chapters": [1],
        "category": "RELATIONSHIP_HISTORY_REWRITE",
        "issue": "后文改写既有地点历史",
        "required_fix": "删除错误的旅馆回忆，保持第1章事实",
        "evidence_quote": target,
        "evidence_quotes": [
            {"chapter_no": 1, "quote": source},
            {"chapter_no": 2, "quote": target},
        ],
        "suggested_class": "CONTINUITY_MINOR",
        "must_preserve": [],
        "confidence": "high",
    }
    ready = verifier._audit_normalize_findings([raw], [1, 2])[0]
    assert ready["repair_ready"] is True

    raw["confidence"] = "medium"
    review_only = verifier._audit_normalize_findings([raw], [1, 2])[0]
    assert review_only["repair_ready"] is False
    assert any("high" in reason for reason in review_only["gate_reasons"])


def test_state_ledger_drops_non_verbatim_anchors(tmp_path):
    chapters = tmp_path / "chapters"
    chapters.mkdir()
    (chapters / "0003.md").write_text("醒来时是早上六点。", encoding="utf-8")
    verifier = Verifier()
    verifier.root = tmp_path
    rows = verifier._audit_normalize_state_ledger([
        {
            "chapter_no": 3,
            "category": "TIME",
            "entity": "主角",
            "state_key": "自然醒时间",
            "state_value": "六点",
            "evidence_quote": "醒来时是早上六点。",
        },
        {
            "chapter_no": 3,
            "category": "TIME",
            "entity": "主角",
            "state_key": "错误锚",
            "state_value": "九点",
            "evidence_quote": "醒来时是上午九点。",
        },
    ], [3])
    assert len(rows) == 1
    assert rows[0]["state_value"] == "六点"


def test_requested_audit_window_is_not_silently_capped_at_four(tmp_path):
    """A caller asking for a 12-chapter window must get 12 or an explicit error."""
    chapters = tmp_path / "chapters"
    chapters.mkdir()
    for n in range(1, 13):
        (chapters / f"{n:04d}.md").write_text(f"第{n}章", encoding="utf-8")

    captured = {}

    class CaptureThread:
        def __init__(self, *, target, args, name, daemon):
            captured.update(target=target, args=args, name=name, daemon=daemon)

        def start(self):
            captured["started"] = True

        def is_alive(self):
            return False

    Starter = _method_harness(
        "Starter",
        ["start_story_audit"],
        extra_globals={"threading": SimpleNamespace(Thread=CaptureThread)},
    )
    starter = Starter()
    starter.root = tmp_path
    starter.audit_lock = nullcontext()
    starter.audit_thread = None
    starter.audit_stop_event = SimpleNamespace(clear=lambda: None)
    starter._run_story_audit = lambda *_: None
    starter.audit_snapshot = lambda: {"running": True}

    starter.start_story_audit(1, 12, segment_size=12, source_check=True)

    assert captured.get("started") is True
    assert captured["args"][2] == 12, (
        "segment_size=12 被无声改成了 4；应保留配置值，或在超出受支持范围时"
        "显式报错，不能静默缩窗后声称完成全文审计"
    )


def test_v3_report_is_rejected_after_canon_changes(tmp_path):
    chapters = tmp_path / "chapters"
    chapters.mkdir()
    chapter = chapters / "0001.md"
    chapter.write_text("原始 Canon", encoding="utf-8")
    report = json.dumps({
        "schema_version": 3,
        "chapter_hashes": {
            "1": hashlib.sha256(chapter.read_bytes()).hexdigest(),
        },
        "findings": [],
    }, ensure_ascii=False)

    guard = SnapshotGuard()
    guard.root = tmp_path
    guard._validate_audit_source_snapshot(report)

    chapter.write_text("审计后被修改", encoding="utf-8")
    try:
        guard._validate_audit_source_snapshot(report)
    except RuntimeError as exc:
        assert "已过期" in str(exc)
    else:
        raise AssertionError("Canon 改变后仍接受旧审计报告")


def test_v3_report_hashes_must_cover_the_entire_audited_range(tmp_path):
    chapters = tmp_path / "chapters"
    chapters.mkdir()
    for n in range(1, 4):
        (chapters / f"{n:04d}.md").write_text(f"第{n}章", encoding="utf-8")
    report = json.dumps({
        "schema_version": 3,
        "start": 1,
        "end": 3,
        "chapter_hashes": {
            "1": hashlib.sha256((chapters / "0001.md").read_bytes()).hexdigest(),
        },
        "findings": [],
    }, ensure_ascii=False)
    guard = SnapshotGuard()
    guard.root = tmp_path
    try:
        guard._validate_audit_source_snapshot(report)
    except RuntimeError as exc:
        assert "快照不完整" in str(exc)
    else:
        raise AssertionError("只带部分章节哈希的 v3 报告仍被接受")


def test_saved_repair_batch_rechecks_snapshot_before_resume(tmp_path):
    chapters = tmp_path / "chapters"
    chapters.mkdir()
    chapter = chapters / "0001.md"
    chapter.write_text("原始 Canon", encoding="utf-8")
    batch = tmp_path / "reports" / "audit_fixes" / "batch1"
    batch.mkdir(parents=True)
    report = {
        "schema_version": 3,
        "start": 1,
        "end": 1,
        "chapter_hashes": {
            "1": hashlib.sha256(chapter.read_bytes()).hexdigest(),
        },
        "findings": [],
    }
    (batch / "audit_source.txt").write_text(
        json.dumps(report, ensure_ascii=False), encoding="utf-8",
    )
    plan = {"schema_version": 3}
    guard = SnapshotGuard()
    guard.root = tmp_path
    guard._validate_repair_batch_audit_snapshot("batch1", plan)
    chapter.write_text("计划生成后被修改", encoding="utf-8")
    try:
        guard._validate_repair_batch_audit_snapshot("batch1", plan)
    except RuntimeError as exc:
        assert "已过期" in str(exc)
    else:
        raise AssertionError("修复计划恢复前没有重新校验审计快照")


def test_all_repair_resume_and_commit_entrypoints_recheck_snapshot():
    for method_name in (
        "start_single_pro_retry",
        "start_audit_repair_candidates",
        "commit_audit_repair",
    ):
        source = ast.get_source_segment(SOURCE, _method_node(method_name)) or ""
        assert "_validate_repair_batch_audit_snapshot" in source, (
            f"{method_name} 恢复旧批次前没有重新校验 v3 Canon 快照"
        )


def test_all_pre_v3_structured_findings_are_preview_only(tmp_path):
    chapters = tmp_path / "chapters"
    chapters.mkdir()
    source = "旧事实明确。"
    target = "后文状态回退。"
    (chapters / "0001.md").write_text(source, encoding="utf-8")
    (chapters / "0002.md").write_text(target, encoding="utf-8")
    planner = Planner(tmp_path)
    planner.repair_lock = nullcontext()
    planner.repair_status = {}
    planner.repair_snapshot = lambda: {}
    planner._repair_check_cancel = lambda: None
    planner._repair_batch_dir = lambda batch_id: (
        tmp_path / "reports" / "audit_fixes" / str(batch_id)
    )
    planner.log = lambda *_: None
    result = StructuredPlanCreator._create_audit_repair_plan_from_findings(
        planner,
        findings=[_finding(
            "OLD", 2, [1], target,
            evidence_quotes=[
                {"chapter_no": 1, "quote": source},
                {"chapter_no": 2, "quote": target},
            ],
        )],
        schema_version=2,
        source_text=json.dumps({"schema_version": 2}),
        source_label="old.json",
        model="deepseek-v4-pro",
    )
    item = result["plan"]["items"][0]
    assert item["auto_candidate"] is True
    assert item["auto_commit_allowed"] is False
    assert any("旧版" in reason for reason in item["evidence_gate_reasons"])


def test_pasted_or_incomplete_v3_findings_are_preview_only(tmp_path):
    chapters = tmp_path / "chapters"
    chapters.mkdir(exist_ok=True)
    (chapters / "0001.md").write_text("旧事实明确。", encoding="utf-8")
    (chapters / "0002.md").write_text("后文状态回退。", encoding="utf-8")
    planner = Planner(tmp_path)
    planner.repair_lock = nullcontext()
    planner.repair_status = {}
    planner.repair_snapshot = lambda: {}
    planner._repair_check_cancel = lambda: None
    planner._repair_batch_dir = lambda batch_id: (
        tmp_path / "reports" / "audit_fixes" / str(batch_id)
    )
    planner.log = lambda *_: None
    complete = _finding(
        "PASTED", 2, [1], "后文状态回退。",
        evidence_quotes=[
            {"chapter_no": 1, "quote": "旧事实明确。"},
            {"chapter_no": 2, "quote": "后文状态回退。"},
        ],
    )
    complete["repair_ready"] = True
    pasted = StructuredPlanCreator._create_audit_repair_plan_from_findings(
        planner, [complete], 3, "{}", "pasted_report", "deepseek-v4-pro",
    )["plan"]["items"][0]
    assert pasted["auto_candidate"] is True
    assert pasted["auto_commit_allowed"] is False

    incomplete = dict(complete)
    incomplete.pop("evidence_quotes")
    incomplete.pop("repair_ready")
    missing = StructuredPlanCreator._create_audit_repair_plan_from_findings(
        planner, [incomplete], 3, "{}", "reports/audit_runs/run/report.json",
        "deepseek-v4-pro",
    )["plan"]["items"][0]
    assert missing["auto_candidate"] is True
    assert missing["auto_commit_allowed"] is False


def test_v3_review_only_findings_are_kept_as_manual_work_items():
    ready = _finding("READY", 2, [1], "目标证据")
    review = _finding("REVIEW", 3, [1], "弱证据", confidence="medium")
    version, rows = AuditSourceParser()._audit_source_findings(json.dumps({
        "schema_version": 3,
        "findings": [ready],
        "review_only_findings": [review],
    }, ensure_ascii=False))
    assert version == 3
    assert {row["finding_id"] for row in rows} == {"READY", "REVIEW"}
    review_row = next(row for row in rows if row["finding_id"] == "REVIEW")
    assert review_row["repair_ready"] is False
    assert any("人工复核" in reason for reason in review_row["gate_reasons"])


def test_v3_ready_finding_wins_over_identical_review_duplicate():
    ready = _finding("READY", 2, [1], "目标证据")
    review = dict(ready)
    review["finding_id"] = "WEAKER"
    review["confidence"] = "medium"
    version, rows = AuditSourceParser()._audit_source_findings(json.dumps({
        "schema_version": 3,
        "findings": [ready],
        "review_only_findings": [review],
    }, ensure_ascii=False))
    assert version == 3
    assert [row["finding_id"] for row in rows] == ["READY"]


def test_audit_chat_streams_and_bounds_provider_retries():
    source = ast.get_source_segment(SOURCE, _method_node("_audit_chat")) or ""
    assert "stream=True" in source
    assert "deepseek_retry_attempts_override" not in source
    assert 'str(label or "").startswith("audit_")' in PROVIDER_SOURCE
    assert "attempts = min(attempts, 2)" in PROVIDER_SOURCE


def test_legacy_prose_plans_can_preview_but_cannot_be_committed():
    source = ast.get_source_segment(
        SOURCE, _method_node("_create_audit_repair_plan_sync"),
    ) or ""
    assert "旧版/纯文本审计报告可生成候选" in source
    assert 'item["auto_candidate"] = True' in source
    assert 'item["auto_commit_allowed"] = False' in source
