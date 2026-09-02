import io
import json
import tempfile
import zipfile
from pathlib import Path

from agent_core import EventHub, NovelAgent
from continuity import adjacent_seams_covered, audit_windows, extract_source_tail, normalize_handoff
from external_canon import (
    ExternalCanonError,
    external_canon_ranges,
    outline_external_ranges,
    sha256_text,
    validate_chapter_package,
)
from test_v30 import base_cfg


def zip_bytes(entries):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, value in entries:
            archive.writestr(name, value)
    return buf.getvalue()


def config_for(start, end):
    cfg = base_cfg()
    cfg["continuity"] = {
        "source_tail_chars": 900,
        "handoff_max_chars": 12000,
        "future_boundary_max_chars": 2000,
        "audit_window_chapters": 4,
        "audit_window_overlap": 1,
    }
    cfg["external_canon"] = {
        "enabled": True,
        "read_outline_markers": True,
        "ranges": [{"start": start, "end": end, "label": "External test volume"}],
    }
    return cfg


def make_agent(root, start=2, end=3):
    cfg = config_for(start, end)
    (root / "story").mkdir(parents=True, exist_ok=True)
    (root / "story" / "outline.md").write_text("# Test outline\n", encoding="utf-8")
    agent = NovelAgent(root, lambda: cfg, EventHub())
    return agent


def handoff_for(chapter, text):
    return normalize_handoff({
        "chapter_no": chapter, "structured_complete": True,
        "end_time": f"day {chapter} 18:00",
        "end_location": f"location {chapter}",
        "present_characters": ["A"],
        "last_actions": [f"finish chapter {chapter}"],
        "completed_events": [f"event {chapter} complete"],
        "ongoing_events": [],
        "new_information": [f"fact {chapter}"],
        "state_changes": [f"A location is {chapter}"],
        "scene_closed": True,
        "next_start": f"continue after chapter {chapter}",
        "do_not_repeat": [f"event {chapter}"],
        "future_boundaries": [],
        "uncertainties": [],
        "item_states": [], "numeric_facts": [], "knowledge_states": [],
        "active_decisions": [], "evidence_claims": [],
        "scene_signatures": [{
            "scene_id": f"external_{chapter}", "location": f"location {chapter}",
            "characters": ["A"], "entry_trigger": "continue external Canon",
            "purpose": f"complete event {chapter}", "props": [],
            "beats": [f"finish chapter {chapter}"], "outcome": f"event {chapter} complete",
            "closed": True,
        }],
    }, chapter, extract_source_tail(text, 900), 12000, require_structured=True)


def commit_predecessor(agent, chapter=1):
    text = f"# Chapter {chapter}\n\n18:00. A leaves location {chapter}."
    agent._commit_canon_bundle(
        chapter, plan="plan", draft=text,
        final_review={"severity": "PASS", "needs_revision": False},
        final=text, summary=f"summary {chapter}",
        memories=[{
            "kind": "character_state", "entity": "A", "attribute": "location",
            "content": f"location {chapter}", "importance": 5, "status": "active",
        }],
        handoff=handoff_for(chapter, text), generation_seconds=0,
        revision_seconds=0, honor_stop=False,
    )


def stub_external_models(agent, fail_chapter=None):
    agent._external_import_review_entry_seam = lambda spec, text: (
        "external entry seam plan",
        {"severity": "PASS", "needs_revision": False},
        [],
    )

    def summarize(chapter, text):
        if chapter == fail_chapter:
            raise RuntimeError(f"simulated metadata failure at {chapter}")
        memories = [{
            "kind": "character_state", "entity": "A", "attribute": "location",
            "content": f"location {chapter}", "importance": 5, "status": "active",
        }]
        return f"summary {chapter}", memories, handoff_for(chapter, text), ""

    agent.summarize_and_extract_memories = summarize


def wait_import(agent):
    thread = agent.external_import_thread
    assert thread is not None
    thread.join(timeout=15)
    assert not thread.is_alive(), "external Canon import thread did not finish"
    return agent.external_canon_snapshot()


def test_outline_and_config_range_discovery():
    outline = "# 外部正史范围：第1051—1300章：外部正史电竞卷\n"
    parsed = outline_external_ranges(outline)
    assert [(row["start"], row["end"]) for row in parsed] == [(1051, 1300)]
    cfg = config_for(1051, 1300)
    combined = external_canon_ranges(cfg, outline)
    assert len(combined) == 1
    assert combined[0]["source"] == "configured+outline"


def test_zip_requires_exact_complete_contiguous_range():
    spec = {"start": 1051, "end": 1053, "label": "external", "source": "test"}
    good = zip_bytes([
        ("chapters/1051.md", "# 第1051章 Start\nbody"),
        ("chapters/1052.md", "# 第1052章 Middle\nbody"),
        ("chapters/1053.md", "# 第1053章 End\nbody"),
    ])
    package = validate_chapter_package(good, spec)
    assert sorted(package["texts"]) == [1051, 1052, 1053]

    bad_packages = [
        zip_bytes([("1051.md", "body"), ("1053.md", "body")]),
        zip_bytes([("1051.md", "body"), ("chapters/1051.md", "body"), ("1052.md", "body"), ("1053.md", "body")]),
        zip_bytes([("1051.txt", "body"), ("1052.md", "body"), ("1053.md", "body")]),
        zip_bytes([("1051.md", "body"), ("1052.md", "body"), ("1053.md", "body"), ("1054.md", "body")]),
        zip_bytes([("../1051.md", "body"), ("1052.md", "body"), ("1053.md", "body")]),
        zip_bytes([("1051.md", "# 第1052章 Wrong\nbody"), ("1052.md", "body"), ("1053.md", "body")]),
    ]
    for payload in bad_packages:
        try:
            validate_chapter_package(payload, spec)
        except ExternalCanonError:
            pass
        else:
            raise AssertionError("invalid external Canon ZIP must be rejected")

    full_spec = {"start": 1051, "end": 1300, "label": "external", "source": "test"}
    full = zip_bytes([
        (f"{chapter:04d}.md", f"# 第{chapter}章\nbody {chapter}")
        for chapter in range(1051, 1301)
    ])
    full_package = validate_chapter_package(full, full_spec)
    assert len(full_package["texts"]) == 250
    exact_windows = audit_windows(1048, 1303, 4, 1)
    assert any(a <= 1050 and b >= 1051 for a, b in exact_windows)
    assert any(a <= 1300 and b >= 1301 for a, b in exact_windows)


def test_generation_stops_at_external_range_and_cannot_jump(tmp_path):
    agent = make_agent(tmp_path, 1051, 1300)
    allowed, message, spec = agent.external_generation_gate(1051)
    assert not allowed and spec["start"] == 1051 and "不会生成" in message
    ok, start_message = agent.start(start_chapter=1051, count=1)
    assert not ok and "不会生成" in start_message
    allowed, message, spec = agent.external_generation_gate(1301)
    assert not allowed and spec["end"] == 1300 and "不能跳章" in message
    ok, start_message = agent.start(start_chapter=1301, count=1)
    assert not ok and "不能跳章" in start_message


def test_complete_import_becomes_normal_canon_and_unlocks_exit(tmp_path):
    agent = make_agent(tmp_path)
    commit_predecessor(agent)
    stub_external_models(agent)
    payload = zip_bytes([
        ("0002.md", "# 第2章 External one\n\n18:30. A reaches location 2."),
        ("0003.md", "# 第3章 External two\n\n19:00. A reaches location 3."),
    ])
    ok, message = agent.start_external_canon_import(payload, range_start=2)
    assert ok, message
    status = wait_import(agent)
    assert status["stage"] == "完成" and status["item_done"] == 2
    allowed, message, _ = agent.external_generation_gate(4, deep=True)
    assert allowed, message
    assert agent.db.get_chapter(2)["source"] == "external_canon"
    assert agent.db.get_chapter(3)["source"] == "external_canon"
    assert agent.db.stats()["completed_chapters"] == 3
    assert all((tmp_path / "chapters" / f"{n:04d}.md").exists() for n in (1, 2, 3))
    boundary = agent.previous_boundary_context(4)
    assert boundary["chapter_no"] == 3 and boundary["status"] == "complete"
    assert boundary["canon_exit_state"]["chapter_no"] == 3
    assert "location 3" in boundary["source_tail"]
    assert "location 3" in json.dumps(boundary["handoff"], ensure_ascii=False)
    protected = agent._boundary_prompt(boundary)
    assert "Canon 退出状态" in protected and "canon_sha256" in protected
    assert adjacent_seams_covered(audit_windows(1, 4, 4, 1), 1, 4)
    exit_state = json.loads((tmp_path / "runtime" / "external_canon" / "0002-0003" / "exit_state.json").read_text(encoding="utf-8"))
    assert exit_state["chapter_no"] == 3
    assert exit_state["metadata_source"] == "novelagent_extracted"

    (tmp_path / "chapters" / "0003.md").write_text("tampered\n", encoding="utf-8")
    allowed, message, _ = agent.external_generation_gate(4, deep=True)
    assert not allowed and "完整性复核失败" in message


def test_validated_package_metadata_avoids_reextracting(tmp_path):
    agent = make_agent(tmp_path)
    commit_predecessor(agent)
    texts = {
        2: "# 第2章 External one\nbody",
        3: "# 第3章 External two\nbody",
    }
    metadata = []
    for chapter, text in texts.items():
        metadata.append((f"metadata/{chapter:04d}.json", json.dumps({
            "chapter_no": chapter,
            "content_sha256": sha256_text(text),
            "summary": f"validated summary {chapter}",
            "memories": [{"kind": "fact", "entity": "A", "key": f"fact_{chapter}", "content": f"fact {chapter}", "importance": 3}],
            "handoff": handoff_for(chapter, text),
        }, ensure_ascii=False)))
    payload = zip_bytes([(f"{chapter:04d}.md", text) for chapter, text in texts.items()] + metadata)
    stub_external_models(agent)
    calls = []
    agent.summarize_and_extract_memories = lambda chapter, text: calls.append(chapter)
    ok, message = agent.start_external_canon_import(payload, range_start=2)
    assert ok, message
    status = wait_import(agent)
    assert status["stage"] == "完成" and calls == []
    assert agent.db.get_chapter(2)["summary"] == "validated summary 2"


def test_partial_import_never_unlocks_and_same_zip_resumes(tmp_path):
    agent = make_agent(tmp_path)
    commit_predecessor(agent)
    payload = zip_bytes([
        ("0002.md", "# 第2章 External one\nbody"),
        ("0003.md", "# 第3章 External two\nbody"),
    ])
    stub_external_models(agent, fail_chapter=3)
    ok, message = agent.start_external_canon_import(payload, range_start=2)
    assert ok, message
    failed = wait_import(agent)
    assert failed["stage"] == "失败" and failed["item_done"] == 1
    allowed, _message, _ = agent.external_generation_gate(4, deep=True)
    assert not allowed

    stub_external_models(agent)
    ok, message = agent.start_external_canon_import(payload, range_start=2)
    assert ok, message
    complete = wait_import(agent)
    assert complete["stage"] == "完成"
    assert complete["skipped"] == 1
    allowed, message, _ = agent.external_generation_gate(4, deep=True)
    assert allowed, message


def main():
    test_outline_and_config_range_discovery()
    test_zip_requires_exact_complete_contiguous_range()
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        test_generation_stops_at_external_range_and_cannot_jump(Path(td))
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        test_complete_import_becomes_normal_canon_and_unlocks_exit(Path(td))
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        test_validated_package_metadata_avoids_reextracting(Path(td))
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        test_partial_import_never_unlocks_and_same_zip_resumes(Path(td))
    print("External Canon architecture tests: PASS")


if __name__ == "__main__":
    main()
