from __future__ import annotations

import ast
import hashlib
import json
import re
import threading
import time
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE_PATH = ROOT / "agent_core.py"
SOURCE = SOURCE_PATH.read_text(encoding="utf-8")


class ProviderCancelledError(Exception):
    pass


def atomic_write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_name(path.name + ".pending")
    pending.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    pending.replace(path)


def audit_windows(start, end, size=4, overlap=1):
    out = []
    current = int(start)
    while current <= int(end):
        window_end = min(int(end), current + int(size) - 1)
        out.append((current, window_end))
        if window_end >= int(end):
            break
        current = window_end - int(overlap) + 1
    return out


def adjacent_seams_covered(ranges, start, end):
    covered = set()
    for left, right in ranges:
        covered.update(range(int(left), int(right)))
    return all(n in covered for n in range(int(start), int(end)))


def _runner_class():
    tree = ast.parse(SOURCE, filename=str(SOURCE_PATH))
    source_class = next(
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "NovelAgent"
    )
    wanted = {
        "_audit_json_digest", "_audit_billing_snapshot",
        "_audit_assertion_inventory", "_audit_segment_input_hash",
        "_audit_find_resumable_run", "_audit_load_checkpoint",
        "_audit_save_checkpoint", "_audit_assert_source_snapshot",
        "_audit_check_cancel", "_audit_set_stage",
        "_audit_collect_findings", "_audit_collect_review_findings",
        "_audit_unresolved_global_candidate", "_audit_finalize_global_result",
        "_run_story_audit",
    }
    methods = [
        node for node in source_class.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name in wanted
    ]
    assert {node.name for node in methods} == wanted
    class_node = ast.ClassDef(
        name="AuditRunner", bases=[], keywords=[], body=methods,
        decorator_list=[],
    )
    module = ast.fix_missing_locations(ast.Module(body=[class_node], type_ignores=[]))
    namespace = {
        "json": json, "hashlib": hashlib, "re": re, "Path": Path,
        "datetime": datetime, "time": time,
        "atomic_write_json": atomic_write_json,
        "audit_windows": audit_windows,
        "adjacent_seams_covered": adjacent_seams_covered,
        "ProviderCancelledError": ProviderCancelledError,
    }
    exec(compile(module, str(SOURCE_PATH), "exec"), namespace)
    runner = namespace["AuditRunner"]
    runner.AUDIT_SCHEMA_VERSION = 3
    runner.AUDIT_CHECKPOINT_VERSION = 1
    runner.AUDIT_PIPELINE_REVISION = "hard-continuity-v3-assertion-ledger-2"
    runner.AUDIT_BILLING_FIELDS = (
        "prompt_tokens", "cache_hit_tokens", "completion_tokens",
        "reasoning_tokens", "cost_cny", "afp", "request_count",
    )
    return runner


AuditRunner = _runner_class()


def _fake_runner(root: Path):
    agent = AuditRunner()
    agent.root = root
    agent.audit_lock = threading.RLock()
    agent.audit_stop_event = threading.Event()
    agent.audit_status = {}
    agent.logs = []
    agent.log = agent.logs.append
    def write(name, text):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text((text or "").strip() + "\n", encoding="utf-8")
    agent.write = write
    calls = {"segment": [], "verify": [], "global": []}
    fail_second_verify = {"value": True}

    snapshot = {
        "chapter_hashes": {str(n): f"chapter-{n}" for n in range(1, 8)},
        "window_input_hashes": {
            "0001_0004": "segment-input-1",
            "0004_0007": "segment-input-2",
        },
        "fingerprint": "stable-source",
    }
    agent._audit_source_snapshot = lambda *_: dict(snapshot)
    agent._audit_verify_input_hash = (
        lambda start, end, segment: agent._audit_json_digest(
            {"stage": "verify", "start": start, "end": end, "segment": segment}
        )
    )
    agent._audit_global_input_hash = (
        lambda start, end, segments: agent._audit_json_digest(
            {"stage": "global", "start": start, "end": end, "segments": segments}
        )
    )

    def billed_call():
        with agent.audit_lock:
            agent.audit_status["request_count"] = int(
                agent.audit_status.get("request_count", 0) or 0
            ) + 1
            agent.audit_status["prompt_tokens"] = int(
                agent.audit_status.get("prompt_tokens", 0) or 0
            ) + 10

    def segment(start, end):
        calls["segment"].append((start, end))
        billed_call()
        return {"start": start, "end": end, "status": "GREEN", "state_ledger": []}

    def verify(item):
        key = (item["start"], item["end"])
        calls["verify"].append(key)
        if key == (4, 7) and fail_second_verify["value"]:
            raise RuntimeError("simulated Pro timeout")
        billed_call()
        return {
            "start": item["start"], "end": item["end"], "status": "GREEN",
            "findings": [], "review_findings": [], "state_ledger": [],
        }

    def global_pass(start, end, segments):
        calls["global"].append((start, end))
        billed_call()
        return {
            "status": "GREEN", "candidate_findings": [],
            "recommended_action": "继续", "overall_summary": "未发现硬错误",
        }

    agent._audit_segment = segment
    agent._audit_verify_segment = verify
    agent._audit_global = global_pass
    agent._audit_render_markdown = lambda run: "audit complete\n"
    return agent, calls, fail_second_verify, snapshot


def test_failed_pro_window_resumes_without_repeating_completed_windows(tmp_path):
    agent, calls, fail_second_verify, _ = _fake_runner(tmp_path)

    agent._run_story_audit(1, 7, 4, True)
    run_dir = next((tmp_path / "reports" / "audit_runs").iterdir())
    failed = json.loads((run_dir / "run_state.json").read_text(encoding="utf-8"))
    assert failed["status"] == "failed"
    assert set(failed["checkpoints"]) == {
        "segment_0001_0004", "verify_0001_0004", "segment_0004_0007",
    }

    fail_second_verify["value"] = False
    agent._run_story_audit(1, 7, 4, True)

    assert calls["segment"] == [(1, 4), (4, 7)]
    assert calls["verify"] == [(1, 4), (4, 7), (4, 7)]
    assert calls["global"] == [(1, 7)]
    report = json.loads((run_dir / "report.json").read_text(encoding="utf-8"))
    assert report["resumed"] is True
    assert report["billing"]["request_count"] == 5


def test_changed_source_snapshot_does_not_resume_failed_run(tmp_path):
    agent, _, _, snapshot = _fake_runner(tmp_path)
    request = {"start": 1, "end": 7, "segment_size": 4, "source_check": True}
    ranges = [(1, 4), (4, 7)]
    run_dir = tmp_path / "reports" / "audit_runs" / "old"
    run_dir.mkdir(parents=True)
    atomic_write_json(run_dir / "run_state.json", {
        "checkpoint_version": agent.AUDIT_CHECKPOINT_VERSION,
        "pipeline_revision": agent.AUDIT_PIPELINE_REVISION,
        "audit_schema_version": agent.AUDIT_SCHEMA_VERSION,
        "status": "failed", "request": request,
        "ranges": [[1, 4], [4, 7]],
        "source_fingerprint": snapshot["fingerprint"],
        "chapter_hashes": snapshot["chapter_hashes"],
        "window_input_hashes": snapshot["window_input_hashes"],
        "checkpoints": {},
    })

    changed = dict(snapshot)
    changed["fingerprint"] = "changed-source"
    assert agent._audit_find_resumable_run(request, ranges, changed) == (
        None, None, None,
    )


def test_checkpoint_rejects_truncation_and_dependency_mismatch(tmp_path):
    agent, _, _, _ = _fake_runner(tmp_path)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    agent.audit_status.update({key: 0 for key in agent.AUDIT_BILLING_FIELDS})
    state = {"checkpoints": {}, "billing": {}}
    agent._audit_save_checkpoint(
        run_dir, state, "verify", "verify.json", {"status": "GREEN"},
        "input", dependency_hash="segment-a",
    )

    assert agent._audit_load_checkpoint(
        run_dir, state, "verify", "verify.json", "input",
        dependency_hash="segment-b",
    ) is None
    (run_dir / "verify.json").write_text("{", encoding="utf-8")
    assert agent._audit_load_checkpoint(
        run_dir, state, "verify", "verify.json", "input",
        dependency_hash="segment-a",
    ) is None


def test_segment_input_hash_uses_the_real_material_builder(tmp_path):
    agent, _, _, _ = _fake_runner(tmp_path)
    agent._audit_outline_for_range = lambda *_: "outline"
    agent._audit_segment_material = lambda *_: "full chapter material"
    agent._audit_deterministic_window = lambda *_: []
    assert len(agent._audit_segment_input_hash(1, 4)) == 64


def test_report_status_and_summary_are_rebuilt_from_terminal_findings(tmp_path):
    """Preliminary global candidates must not masquerade as final findings."""
    agent, calls, fail_second_verify, _ = _fake_runner(tmp_path)
    fail_second_verify["value"] = False
    candidate = {
        "candidate_id": "GC001",
        "chapter_no": 7,
        "related_chapters": [1],
        "category": "STATE_REGRESSION",
        "issue": "全局初筛提出的跨窗口候选",
    }
    review = {
        **candidate,
        "finding_id": "R0007_001",
        "required_fix": "需要人工结合邻章确认",
        "evidence_quote": "",
        "evidence_quotes": [],
        "suggested_class": "CONTINUITY_MINOR",
        "confidence": "medium",
        "repair_ready": False,
        "source_scope": "global",
        "gate_reasons": ["全局终审未获得双侧逐字证据"],
    }

    def preliminary_global(start, end, segments):
        calls["global"].append((start, end))
        return {
            "status": "ORANGE",
            "candidate_findings": [candidate],
            "recommended_action": "暂停并修改候选章节",
            "overall_summary": (
                "PRELIMINARY_UNVERIFIED: 已确认三个跨窗口硬错误。"
            ),
        }

    agent._audit_global = preliminary_global
    agent._audit_global_verify_input_hash = (
        lambda start, end, candidates: agent._audit_json_digest({
            "stage": "global_verify",
            "start": start,
            "end": end,
            "candidates": candidates,
        })
    )
    agent._audit_verify_global_candidates = lambda *_: {
        "status": "YELLOW",
        "findings": [],
        "review_findings": [review],
        "false_positives": [],
        "verified_chapters": [1, 7],
    }

    agent._run_story_audit(1, 7, 4, True)

    run_dir = next((tmp_path / "reports" / "audit_runs").iterdir())
    report = json.loads((run_dir / "report.json").read_text(encoding="utf-8"))
    assert report["findings_count"] == 0
    assert report["review_only_findings_count"] == 1
    assert report["global"]["status"] == "YELLOW", (
        "只有人工复核项时，最终状态必须来自终审清单而非初筛 ORANGE"
    )
    summary = str(report["global"].get("overall_summary") or "")
    assert "PRELIMINARY_UNVERIFIED" not in summary, (
        "总体判断不能继续呈现尚未通过正文终审的候选结论"
    )
    assert report["global"]["final_findings_count"] == 0
    assert report["global"]["final_review_findings_count"] == 1
    assert "0 项" in summary
    assert "1 项" in summary and "人工复核" in summary
