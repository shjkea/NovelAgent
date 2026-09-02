"""Focused contracts for incremental audit-repair submissions."""

import ast
import hashlib
import json
import textwrap
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "agent_core.py").read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)
AGENT = next(
    node for node in TREE.body
    if isinstance(node, ast.ClassDef) and node.name == "NovelAgent"
)


def method_source(name):
    node = next(
        item for item in AGENT.body
        if isinstance(item, ast.FunctionDef) and item.name == name
    )
    text = ast.get_source_segment(SOURCE, node)
    assert text
    return text


def stub_with(*names):
    body = "\n\n".join(method_source(name) for name in names)
    ns = {"hashlib": hashlib, "json": json}
    exec(compile(
        "class Stub:\n" + textwrap.indent(body, "    "),
        "<incremental-manifest>", "exec",
    ), ns)
    cls = ns["Stub"]
    for name in {"_repair_manifest_active_rows", "_repair_commit_gate_reasons"}:
        if name in names:
            setattr(cls, name, staticmethod(getattr(cls, name)))
    return cls


def test_active_rows_keep_new_submission_after_an_older_full_rollback():
    cls = stub_with("_repair_manifest_active_rows")
    manifest = {
        "rolled_back_at": "2026-09-02T10:00:00",
        "rollback_partial": False,
        "rolled_back_chapters": [6],
        "chapters": [
            {"chapter_no": 6, "new_sha256": "old"},
            {
                "chapter_no": 6,
                "submission_id": "second",
                "new_sha256": "new",
            },
            {
                "chapter_no": 12,
                "submission_id": "first",
                "rolled_back_at": "2026-09-02T10:00:00",
                "rollback_status": "restored",
            },
        ],
    }
    active = cls._repair_manifest_active_rows(manifest)
    assert [(row["chapter_no"], row["new_sha256"]) for row in active] == [(6, "new")]


def test_commit_gate_boolean_polarity_matches_review_contract():
    cls = stub_with("_repair_commit_gate_reasons")
    reasons = cls._repair_commit_gate_reasons(
        {},
        {
            "safe": True,
            "review": {
                "requested_fix_applied": False,
                "safe_to_batch_commit": False,
                "unrelated_changes": True,
                "canon_end_state_changed": False,
                "downstream_conflict": True,
            },
        },
        7,
        approved={7},
        blocked=set(),
    )
    assert "未确认审计要求已应用" in reasons
    assert "未获得批量提交安全许可" in reasons
    assert "检测到无关改动" in reasons
    assert "检测到下游连续性冲突" in reasons
    assert "Canon 章末状态发生变化" not in reasons


def test_snapshot_accepts_only_hashes_recorded_by_this_batch(tmp_path):
    cls = stub_with("_validate_audit_source_snapshot")
    agent = cls()
    agent.root = tmp_path
    chapter_dir = tmp_path / "chapters"
    chapter_dir.mkdir()
    original = "原文\n"
    committed = "本批次候选\n"
    path = chapter_dir / "0001.md"
    path.write_bytes(committed.encode("utf-8"))
    report = json.dumps({
        "schema_version": 3,
        "start": 1,
        "end": 1,
        "chapter_hashes": {"1": hashlib.sha256(original.encode("utf-8")).hexdigest()},
        "findings": [],
    }, ensure_ascii=False)

    own_hash = hashlib.sha256(committed.encode("utf-8")).hexdigest()
    agent._validate_audit_source_snapshot(report, accepted_hashes={1: own_hash})

    path.write_bytes("外部又改过\n".encode("utf-8"))
    with pytest.raises(RuntimeError, match="审计报告已过期"):
        agent._validate_audit_source_snapshot(report, accepted_hashes={1: own_hash})


def test_successful_submission_clears_stale_top_level_rollback_summary():
    source = method_source("commit_audit_repair")
    for field in (
        "rolled_back_at",
        "rolled_back_chapters",
        "rollback_partial",
        "rollback_already_original",
    ):
        assert field in source
    assert "manifest.pop(key, None)" in source
