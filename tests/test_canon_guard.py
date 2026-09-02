import hashlib
import json
from pathlib import Path

import pytest

from canon_guard import (
    build_canon_ledger,
    deterministic_canon_findings,
    format_canon_ledger,
    normalize_light_quality_checks,
    recent_chapter_fulltexts,
    verify_canon_publish,
)
from continuity import normalize_handoff


ROOT = Path(__file__).resolve().parents[1]


def _codes(text, ledger, plan=""):
    return {row["code"] for row in deterministic_canon_findings(text, ledger, plan=plan)}


def _empty_ledger():
    return {
        "item_states": [], "numeric_facts": [], "knowledge_states": [],
        "active_decisions": [], "evidence_claims": [], "recent_scenes": [],
    }


def _write_handoff(root, chapter, **fields):
    directory = root / "handoffs"
    directory.mkdir(parents=True, exist_ok=True)
    data = {
        "schema_version": 2, "structured_complete": True,
        "chapter_no": chapter, "status": "complete",
        "item_states": [], "numeric_facts": [], "knowledge_states": [],
        "active_decisions": [], "evidence_claims": [], "scene_signatures": [],
        **fields,
    }
    (directory / f"{chapter:04d}.json").write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8"
    )


def test_v1_handoff_is_readable_but_not_accepted_for_new_canon():
    raw = {"chapter_no": 1, "end_location": "走廊"}
    legacy = normalize_handoff(raw, 1, "原文末尾")
    assert legacy["schema_version"] == 2
    assert legacy["structured_complete"] is False
    with pytest.raises(ValueError, match="structured_complete"):
        normalize_handoff(raw, 1, "原文末尾", require_structured=True)


def test_structured_handoff_requires_and_preserves_a_scene_after_trimming():
    raw = {
        "chapter_no": 1, "structured_complete": True,
        "present_characters": ["角色" + str(i) + "x" * 400 for i in range(24)],
        "item_states": [{
            "item_id": f"item_{i}", "name": "物品" + "x" * 100,
            "holder": "角色甲", "location": "房间" + "x" * 150,
            "status": "active", "evidence": "证据" + "x" * 300,
        } for i in range(16)],
        "scene_signatures": [{
            "scene_id": "hall", "location": "走廊", "characters": ["角色甲"],
            "entry_trigger": "出门", "purpose": "继续任务", "props": [],
            "beats": ["前行"], "outcome": "抵达门口", "closed": False,
        }],
    }
    handoff = normalize_handoff(
        raw, 1, "原文末尾", max_chars=4000, require_structured=True
    )
    assert handoff["scene_signatures"]

    no_scene = dict(raw, scene_signatures=[])
    with pytest.raises(ValueError, match="scene_signatures"):
        normalize_handoff(no_scene, 1, "原文末尾", require_structured=True)


def test_ledger_keeps_terminal_items_and_removes_inactive_bindings(tmp_path):
    _write_handoff(
        tmp_path, 1,
        item_states=[{"item_id": "key", "name": "钥匙", "holder": "角色甲", "status": "active"}],
        numeric_facts=[{"fact_id": "price", "subject": "成交价", "value": "500", "unit": "元", "status": "active"}],
        active_decisions=[{"decision_id": "recording", "character": "角色甲", "decision": "不改记录方式", "status": "active"}],
        knowledge_states=[{"knowledge_id": "character_a_secret", "character": "角色甲", "fact": "密室位置", "knows": True, "status": "active"}],
    )
    _write_handoff(
        tmp_path, 2,
        item_states=[{"item_id": "key", "name": "钥匙", "holder": "角色甲", "condition": "已销毁", "status": "destroyed"}],
        numeric_facts=[{"fact_id": "price", "subject": "成交价", "value": "500", "unit": "元", "status": "superseded"}],
        active_decisions=[{"decision_id": "recording", "character": "角色甲", "decision": "不改记录方式", "status": "resolved"}],
    )
    ledger = build_canon_ledger(tmp_path, 3)
    assert ledger["item_states"][0]["status"] == "destroyed"
    assert ledger["numeric_facts"] == []
    assert ledger["active_decisions"] == []
    assert ledger["knowledge_states"][0]["knows"] is True


def test_large_ledger_prompt_remains_valid_json():
    ledger = _empty_ledger()
    ledger["before_chapter"] = 90
    ledger["source_chapters"] = list(range(1, 90))
    ledger["item_states"] = [{
        "item_id": f"item_{i}", "name": "物品" + str(i),
        "holder": "角色甲", "location": "仓库" + "x" * 120,
        "condition": "完好" + "x" * 120, "status": "active",
    } for i in range(40)]
    text = format_canon_ledger(ledger, max_chars=3000)
    parsed = json.loads(text)
    assert len(text) <= 3000
    assert parsed["before_chapter"] == 90
    assert parsed.get("_truncated_counts")


def test_numeric_canon_conflict_is_deterministic():
    ledger = _empty_ledger()
    ledger["numeric_facts"] = [{
        "fact_id": "clock_final_price", "subject": "古董钟最终成交价",
        "value": "500", "unit": "元", "aliases": ["古董钟", "成交价"],
    }]
    text = "角色甲核对付款记录：这座古董钟的最终成交价是六百元。"
    assert "CANON_NUMERIC_CONFLICT" in _codes(text, ledger)


def test_legitimate_return_and_marked_arrival_are_not_blocked():
    ledger = _empty_ledger()
    ledger["recent_scenes"] = [{
        "chapter_no": 8, "location": "角色乙家客厅",
        "characters": ["角色甲", "角色乙"], "props": ["水杯", "药瓶"],
        "entry_trigger": "探望", "beats": ["询问近况", "练功"], "closed": True,
    }]
    changed_scene = "角色甲独自回到角色乙家客厅寻找遗失的钥匙，找到后立即离开。"
    assert "RECENT_SCENE_TEMPLATE_REPLAY" not in _codes(changed_scene, ledger)

    marked = "角色甲坐公交车回去。到家以后，他拉开书桌抽屉放好钥匙。"
    assert "UNMARKED_LOCATION_JUMP" not in _codes(marked, ledger)


def test_light_quality_notices_are_advisory_but_clear_failures_block():
    normalized, blockers, advisories = normalize_light_quality_checks({
        "scene_sufficiency": {
            "status": "THIN", "reason": "互动略短但事件已经推进",
        },
        "cross_chapter_repetition": {
            "status": "NOTICEABLE", "evidence": ["有相似课堂节拍"],
        },
        "constraint_leakage": {"status": "CLEAR"},
        "chapter_logic": {
            "numbers": {"status": "ISSUE", "evidence": ""},
        },
    })
    assert blockers == []
    assert len(advisories) == 2
    assert normalized["chapter_logic"]["numbers"]["status"] == "UNKNOWN"

    _normalized, blockers, _advisories = normalize_light_quality_checks({
        "scene_sufficiency": {
            "status": "EMPTY", "evidence": ["整章只有观察，没有行动或结果变化"],
        },
        "chapter_logic": {
            "evidence_conclusion": {
                "status": "ISSUE", "evidence": "一条记录被直接写成确定因果",
            },
        },
    })
    assert {row["key"] for row in blockers} == {
        "scene_sufficiency", "chapter_logic_evidence_conclusion",
    }


def test_recent_fulltext_window_uses_prose_not_summaries(tmp_path):
    chapters = tmp_path / "chapters"
    chapters.mkdir()
    for chapter in range(1, 6):
        (chapters / f"{chapter:04d}.md").write_text(
            f"第{chapter}章唯一正文标记。", encoding="utf-8"
        )
    text = recent_chapter_fulltexts(tmp_path, 6, count=3, max_chars=6000)
    assert "第3章唯一正文标记" in text
    assert "第4章唯一正文标记" in text
    assert "第5章唯一正文标记" in text
    assert "第2章唯一正文标记" not in text


def test_canon_publish_verifier_checks_files_db_handoff_and_state(tmp_path):
    for directory in ("chapters", "summaries", "handoffs", "reviews"):
        (tmp_path / directory).mkdir()
    final = "正文"
    summary = "摘要"
    handoff = {
        "chapter_no": 1, "status": "complete", "structured_complete": True,
    }
    review = {"severity": "PASS", "needs_revision": False}
    (tmp_path / "chapters" / "0001.md").write_text(final + "\n", encoding="utf-8")
    (tmp_path / "summaries" / "0001.md").write_text(summary + "\n", encoding="utf-8")
    (tmp_path / "handoffs" / "0001.json").write_text(
        json.dumps(handoff, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    (tmp_path / "reviews" / "0001.json").write_text(
        json.dumps(review, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    (tmp_path / "state.json").write_text(json.dumps({
        "last_canon_chapter": 1,
        "next_chapter": 2,
        "last_canon_hash": hashlib.sha256(final.encode("utf-8")).hexdigest(),
    }), encoding="utf-8")
    (tmp_path / "current_state.json").write_text(
        json.dumps({"as_of_chapter": 1}), encoding="utf-8"
    )
    row = {
        "final": final, "summary": summary,
        "handoff": json.dumps(handoff, ensure_ascii=False),
        "review": json.dumps(review, ensure_ascii=False),
    }
    assert verify_canon_publish(tmp_path, 1, row, 1, last_db_row=row) == []

    (tmp_path / "handoffs" / "0001.json").write_text("{}", encoding="utf-8")
    errors = verify_canon_publish(tmp_path, 1, row, 1, last_db_row=row)
    assert any("handoff" in error.lower() for error in errors)
