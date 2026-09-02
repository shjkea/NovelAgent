import json
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

from agent_core import EventHub, NovelAgent
from memory_db import MemoryDB, normalize_memory_record
from provider_router import calculate_deepseek_cost


class DummyEmbed:
    def embed(self, text, is_query=False):
        # deterministic tiny vector; enough to exercise DB inserts
        return [1.0, float(len(text) % 7 + 1), 0.5]


def base_cfg():
    return {
        "title":"T",
        "llm":{"url":"http://127.0.0.1:9/v1/chat/completions","health_url":"http://127.0.0.1:9/health","model":"local"},
        "embedding":{"enabled":False,"url":"http://127.0.0.1:9/v1/embeddings","health_url":"http://127.0.0.1:9/health","model":"e","top_k":10,"min_score":-1},
        "generation":{"start_chapter":1,"chapters_per_run":1,"target_chapter_chars":1000,"recent_summary_count":3,"max_revision_rounds":1,
                      "temperatures":{"plan":0.1,"draft":0.5,"review":0.1,"revise":0.2,"memory":0.1},
                      "max_tokens":{"plan":500,"draft":1000,"review":500,"revise":1000,"memory":500}},
        "web":{"live_output_default":False},
        "management":{"active_performance":"x","performance_profiles":{"x":{"context":32768}}},
        "deepseek":{"base_url":"https://api.deepseek.com","api_key_store":"runtime/key.dpapi","fallback_to_local":True},
        "routing":{"mode":"all_local","stages":{},"presets":{"all_local":{x:{"provider":"local","model":"local","thinking":False} for x in ("plan","draft","review","revision","summary","memory")}},"auto_nsfw":{}},
        "context":{"outline_neighbor_chapters":1,"outline_legacy_max_chars":24000},
    }


def test_memory_rollback(tmp_path):
    tmp = tmp_path
    db = MemoryDB(tmp / "m.sqlite3", DummyEmbed())
    a = {"kind":"character_state","entity":"张三","attribute":"location","content":"在北京","importance":5,"status":"active"}
    b = {"kind":"character_state","entity":"张三","attribute":"location","content":"在上海","importance":5,"status":"active"}
    db.add_memories([a], 1)
    db.add_memories([b], 2)
    st = db.state_as_of(2)["states"]
    assert len(st) == 1 and "上海" in st[0]["content"]
    db.delete_from_chapter(2)
    st = db.current_state()["states"]
    assert len(st) == 1 and "北京" in st[0]["content"]

    db.add_memories([{"kind":"hook","entity":"钥匙","hook_id":"basement","content":"可打开地下室","status":"active"}], 3)
    db.add_memories([{"kind":"hook","entity":"钥匙","hook_id":"basement","content":"伏笔已回收","status":"resolved"}], 4)
    assert db.state_as_of(3)["hooks"], "resolved-later hook must remain visible historically"
    assert not db.state_as_of(4)["hooks"], "resolved hook must be absent after resolution"
    db.delete_from_chapter(4)
    assert db.current_state()["hooks"], "rollback must reactivate the old hook"


def test_schema():
    x = normalize_memory_record({"kind":"relationship","entity":"A","related_entity":"B","dimension":"Trust","content":"high"})
    assert x["key"] == "related:B|dimension:trust"
    x = normalize_memory_record({"kind":"knowledge_state","entity":"A","fact_id":"Secret X","content":"known"})
    assert x["key"] == "fact:secret_x"


def test_cost():
    cst = timezone(timedelta(hours=8))
    usage = {"prompt_tokens":1_000_000,"prompt_cache_hit_tokens":0,"prompt_cache_miss_tokens":1_000_000,"completion_tokens":1_000_000}
    assert calculate_deepseek_cost("deepseek-v4-flash", usage, datetime(2026,8,17,1,0,tzinfo=cst)) == 6.0
    assert calculate_deepseek_cost("deepseek-v4-flash", usage, datetime(2026,8,17,9,30,tzinfo=cst)) == 12.0
    assert calculate_deepseek_cost("deepseek-v4-pro", usage, datetime(2026,8,17,1,0,tzinfo=cst)) == 18.0


def test_outline_and_current_state(tmp_path):
    tmp = tmp_path
    cfg = base_cfg()
    story = tmp / "story"
    story.mkdir(parents=True, exist_ok=True)
    for f in ("premise.md","world.md","characters_seed.md","style.md"):
        (story/f).write_text(f, encoding="utf-8")
    outline = "# 总纲\nGLOBAL\n\n## 第1章 A\nONE\n\n## 第2章 B\nTWO\n\n## 第3章 C\nTHREE\n\n## 第20章 Z\nFAR\n"
    (story/"outline.md").write_text(outline, encoding="utf-8")
    (tmp/"state.json").write_text('{"next_chapter":2}', encoding="utf-8")
    agent = NovelAgent(tmp, lambda: cfg, EventHub())
    ctx = agent.outline_context(2)
    assert "GLOBAL" in ctx and "ONE" in ctx and "TWO" in ctx and "THREE" in ctx
    assert "FAR" not in ctx
    agent.db.add_memories([{"kind":"location_state","entity":"城门","state_key":"condition","content":"已毁","status":"active"}],1)
    assert "已毁" in agent.format_current_state(1)


def main():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        tmp = Path(td)
        test_memory_rollback(tmp)
    test_schema(); test_cost()
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        test_outline_and_current_state(Path(td))
    print("V3.0 core tests: PASS")


if __name__ == "__main__":
    main()
