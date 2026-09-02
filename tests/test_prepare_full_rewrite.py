import json
import sqlite3

from prepare_full_rewrite import execute_rewrite_reset, inspect_rewrite


def _seed_database(path):
    with sqlite3.connect(str(path)) as con:
        con.executescript("""
        CREATE TABLE memories (
            id INTEGER PRIMARY KEY, chapter_no INTEGER, kind TEXT,
            entity TEXT, key_name TEXT, content TEXT, importance INTEGER,
            status TEXT, active INTEGER, embedding BLOB,
            created_at TEXT, updated_at TEXT
        );
        CREATE TABLE chapters (
            chapter_no INTEGER PRIMARY KEY, final TEXT
        );
        CREATE TABLE llm_usage (
            id INTEGER PRIMARY KEY, chapter_no INTEGER
        );
        INSERT INTO chapters(chapter_no, final) VALUES (1, 'one'), (2, 'two');
        INSERT INTO memories(
            chapter_no,kind,entity,key_name,content,importance,status,active,created_at,updated_at
        ) VALUES (1,'fact','','','old fact',3,'active',1,'now','now');
        INSERT INTO llm_usage(chapter_no) VALUES (1), (2);
        """)


def test_rewrite_reset_archives_then_clears_only_after_confirmation(tmp_path):
    for folder in ("chapters", "plans", "reviews", "summaries", "handoffs"):
        directory = tmp_path / folder
        directory.mkdir(parents=True, exist_ok=True)
        suffix = ".json" if folder in {"reviews", "handoffs"} else ".md"
        for chapter in (1, 2):
            (directory / f"{chapter:04d}{suffix}").write_text(
                f"{folder}-{chapter}", encoding="utf-8"
            )
    snapshots = tmp_path / "runtime" / "state_snapshots"
    snapshots.mkdir(parents=True)
    (snapshots / "0001.json").write_text("{}", encoding="utf-8")
    cache = tmp_path / "runtime" / "plan_stage_contracts"
    cache.mkdir(parents=True)
    (cache / "old.json").write_text("{}", encoding="utf-8")
    (tmp_path / "state.json").write_text(
        '{"next_chapter":3,"last_canon_chapter":2,"last_canon_hash":"old"}',
        encoding="utf-8",
    )
    (tmp_path / "current_state.json").write_text('{"as_of_chapter":2}', encoding="utf-8")
    _seed_database(tmp_path / "novel_memory.sqlite3")

    report = inspect_rewrite(tmp_path, 2)
    assert report["ready"] is True
    assert report["confirmation"] == "REWRITE-0001-0002"
    assert (tmp_path / "chapters" / "0001.md").exists()

    result = execute_rewrite_reset(tmp_path, 2, "REWRITE-0001-0002")
    archive = result["archive"]
    manifest = json.loads((tmp_path / archive).joinpath("manifest.json").read_text(encoding="utf-8"))
    assert manifest["archive_complete"] is True
    assert (tmp_path / archive / "chapters" / "0001.md").read_text(encoding="utf-8") == "chapters-1"
    assert (tmp_path / archive / "novel_memory.sqlite3").exists()
    assert not (tmp_path / "chapters" / "0001.md").exists()
    assert not (cache / "old.json").exists()

    with sqlite3.connect(str(tmp_path / "novel_memory.sqlite3")) as con:
        assert con.execute("SELECT COUNT(*) FROM chapters").fetchone()[0] == 0
        assert con.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0
        assert con.execute("SELECT COUNT(*) FROM llm_usage").fetchone()[0] == 0
    state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert state["next_chapter"] == 1
    assert state["last_canon_chapter"] == 0
    assert "last_canon_hash" not in state
