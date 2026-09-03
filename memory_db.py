import math
import sqlite3
import threading
from array import array
from datetime import datetime
from pathlib import Path
import requests

STATE_KINDS = {
    "character_state", "relationship", "item_state",
    "location_state", "knowledge_state"
}


class _ClosingConnection(sqlite3.Connection):
    """sqlite3 context manager that also releases the Windows file handle."""
    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def _pack(vec):
    a = array("f", [float(x) for x in vec])
    return a.tobytes()


def _unpack(blob):
    a = array("f")
    a.frombytes(blob)
    return a


def _cos(a, b):
    if len(a) != len(b) or not a:
        return -1.0
    dot = na = nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0 or nb <= 0:
        return -1.0
    return dot / math.sqrt(na * nb)


def _clean_key(s):
    s = str(s or "").strip().lower()
    s = "_".join(s.split())
    return s[:180]


def normalize_memory_record(rec):
    """Normalize future state records without breaking legacy records."""
    rec = dict(rec or {})
    kind = str(rec.get("kind", "fact")).strip() or "fact"
    entity = str(rec.get("entity", "")).strip()
    key = str(rec.get("key", rec.get("key_name", ""))).strip()

    if kind == "relationship":
        other = str(rec.get("related_entity", "")).strip()
        dim = _clean_key(rec.get("dimension", "relationship")) or "relationship"
        if other:
            key = f"related:{other}|dimension:{dim}"
    elif kind == "knowledge_state":
        fact_id = _clean_key(rec.get("fact_id", ""))
        if fact_id:
            key = f"fact:{fact_id}"
    elif kind == "character_state":
        attr = _clean_key(rec.get("attribute", ""))
        if attr:
            key = f"attr:{attr}"
    elif kind in {"item_state", "location_state"}:
        state_key = _clean_key(rec.get("state_key", ""))
        if state_key:
            key = f"state:{state_key}"
    elif kind == "hook":
        hook_id = _clean_key(rec.get("hook_id", ""))
        if hook_id:
            key = f"hook:{hook_id}"

    rec["kind"] = kind
    rec["entity"] = entity
    rec["key"] = key
    return rec


class EmbeddingClient:
    def __init__(self, cfg):
        self.cfg = cfg

    def health(self):
        if not self.cfg.get("enabled", True):
            return False, "disabled"
        try:
            r = requests.get(self.cfg["health_url"], timeout=3)
            return r.status_code in (200, 503), f"HTTP {r.status_code}"
        except Exception as e:
            return False, str(e)

    def embed(self, text, is_query=False):
        if not self.cfg.get("enabled", True):
            return None
        if is_query:
            instr = self.cfg.get("query_instruction", "").strip()
            if instr:
                text = f"Instruct: {instr}\nQuery: {text}"
        payload = {"model": self.cfg["model"], "input": text, "encoding_format": "float"}
        r = requests.post(self.cfg["url"], json=payload, timeout=120)
        r.raise_for_status()
        return r.json()["data"][0]["embedding"]


class MemoryDB:
    def __init__(self, path, embed_client):
        self.path = str(path)
        self.embed_client = embed_client
        self.lock = threading.RLock()
        self.fts_enabled = False
        self._init()

    def conn(self):
        c = sqlite3.connect(self.path, timeout=30, factory=_ClosingConnection)
        c.row_factory = sqlite3.Row
        return c

    def _init(self):
        with self.conn() as c:
            c.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chapter_no INTEGER NOT NULL DEFAULT 0,
                kind TEXT NOT NULL,
                entity TEXT NOT NULL DEFAULT '',
                key_name TEXT NOT NULL DEFAULT '',
                content TEXT NOT NULL,
                importance INTEGER NOT NULL DEFAULT 3,
                status TEXT NOT NULL DEFAULT 'active',
                active INTEGER NOT NULL DEFAULT 1,
                embedding BLOB,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_mem_active ON memories(active);
            CREATE INDEX IF NOT EXISTS idx_mem_entity_key ON memories(entity, key_name, kind);
            CREATE INDEX IF NOT EXISTS idx_mem_chapter ON memories(chapter_no);

            CREATE TABLE IF NOT EXISTS chapters (
                chapter_no INTEGER PRIMARY KEY,
                source TEXT NOT NULL DEFAULT 'generated',
                plan TEXT,
                draft TEXT,
                review TEXT,
                final TEXT,
                summary TEXT,
                handoff TEXT,
                chars INTEGER NOT NULL DEFAULT 0,
                generation_seconds REAL NOT NULL DEFAULT 0,
                revision_seconds REAL NOT NULL DEFAULT 0,
                model_tokens INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS llm_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id TEXT NOT NULL UNIQUE,
                chapter_no INTEGER NOT NULL DEFAULT 0,
                stage TEXT NOT NULL DEFAULT '',
                provider TEXT NOT NULL DEFAULT '',
                model TEXT NOT NULL DEFAULT '',
                thinking INTEGER NOT NULL DEFAULT 0,
                prompt_tokens INTEGER NOT NULL DEFAULT 0,
                cache_hit_tokens INTEGER NOT NULL DEFAULT 0,
                cache_miss_tokens INTEGER NOT NULL DEFAULT 0,
                completion_tokens INTEGER NOT NULL DEFAULT 0,
                reasoning_tokens INTEGER NOT NULL DEFAULT 0,
                cost_cny REAL NOT NULL DEFAULT 0,
                elapsed_seconds REAL NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_usage_chapter ON llm_usage(chapter_no);
            CREATE INDEX IF NOT EXISTS idx_usage_stage ON llm_usage(stage);
            """)
            # Additive migration only: old Canon data is intentionally not read by
            # the new pipeline, but keeping the column makes restarts harmless.
            cols = {r["name"] for r in c.execute("PRAGMA table_info(chapters)").fetchall()}
            if "handoff" not in cols:
                c.execute("ALTER TABLE chapters ADD COLUMN handoff TEXT")
            if "source" not in cols:
                c.execute("ALTER TABLE chapters ADD COLUMN source TEXT NOT NULL DEFAULT 'generated'")
            try:
                c.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts
                USING fts5(content, entity, kind, content='memories', content_rowid='id')
                """)
                c.executescript("""
                CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
                    INSERT INTO memories_fts(rowid,content,entity,kind)
                    VALUES(new.id,new.content,new.entity,new.kind);
                END;
                CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
                    INSERT INTO memories_fts(memories_fts,rowid,content,entity,kind)
                    VALUES('delete',old.id,old.content,old.entity,old.kind);
                END;
                CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
                    INSERT INTO memories_fts(memories_fts,rowid,content,entity,kind)
                    VALUES('delete',old.id,old.content,old.entity,old.kind);
                    INSERT INTO memories_fts(rowid,content,entity,kind)
                    VALUES(new.id,new.content,new.entity,new.kind);
                END;
                """)
                self.fts_enabled = True
            except sqlite3.OperationalError:
                self.fts_enabled = False

    def backup_to(self, dest):
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with self.lock:
            src = self.conn()
            dst = sqlite3.connect(str(dest))
            try:
                src.backup(dst)
            finally:
                dst.close()
                src.close()

    def add_memory(self, rec):
        rec = normalize_memory_record(rec)
        now = datetime.now().isoformat(timespec="seconds")
        kind = str(rec.get("kind", "fact")).strip() or "fact"
        entity = str(rec.get("entity", "")).strip()
        key_name = str(rec.get("key", "")).strip()
        content = str(rec.get("content", "")).strip()
        if not content:
            return None
        chapter_no = int(rec.get("chapter_no", 0) or 0)
        importance = max(1, min(5, int(rec.get("importance", 3) or 3)))
        status = str(rec.get("status", "active")).strip().lower()
        active = 0 if status in {"resolved", "obsolete", "inactive"} else 1

        emb = None
        try:
            vec = self.embed_client.embed(f"[{kind}] {entity} {key_name}\n{content}", is_query=False)
            if vec:
                emb = _pack(vec)
        except Exception:
            emb = None

        with self.lock, self.conn() as c:
            if active and kind in STATE_KINDS and entity and key_name:
                c.execute("""
                    UPDATE memories SET active=0, status='superseded', updated_at=?
                    WHERE active=1 AND kind=? AND entity=? AND key_name=?
                """, (now, kind, entity, key_name))
            elif not active and kind in STATE_KINDS and entity and key_name:
                # Terminal tombstone (resolved/obsolete/inactive): deactivate the
                # previously active record so the key leaves the live ledger while
                # its history stays intact.
                c.execute("""
                    UPDATE memories SET active=0, status='superseded', updated_at=?
                    WHERE active=1 AND kind=? AND entity=? AND key_name=?
                """, (now, kind, entity, key_name))
            if status == "resolved" and kind == "hook":
                if entity or key_name:
                    c.execute("""
                        UPDATE memories SET active=0, status='superseded', updated_at=?
                        WHERE active=1 AND kind='hook'
                        AND (?='' OR entity=?) AND (?='' OR key_name=?)
                    """, (now, entity, entity, key_name, key_name))
            cur = c.execute("""
                INSERT INTO memories
                (chapter_no,kind,entity,key_name,content,importance,status,active,embedding,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """, (chapter_no,kind,entity,key_name,content,importance,status,active,emb,now,now))
            return cur.lastrowid

    def add_memories(self, records, chapter_no):
        ids = []
        for rec in records:
            if not isinstance(rec, dict):
                continue
            rec = dict(rec)
            rec["chapter_no"] = chapter_no
            mid = self.add_memory(rec)
            if mid:
                ids.append(mid)
        return ids

    def _row_dict(self, r, score=None, method=None):
        d = {
            "id": r["id"], "chapter_no": r["chapter_no"], "kind": r["kind"],
            "entity": r["entity"], "key": r["key_name"], "content": r["content"],
            "importance": r["importance"], "status": r["status"] if "status" in r.keys() else "active",
        }
        if method is not None:
            d["method"] = method
            d["score"] = score
        return d

    def search(self, query, top_k=12, min_score=0.22, max_chapter=None):
        top_k = max(1, int(top_k))
        where = "WHERE active=1"
        args = []
        if max_chapter is not None:
            where += " AND chapter_no<=?"
            args.append(int(max_chapter))
        with self.conn() as c:
            rows = c.execute(f"""
                SELECT id,chapter_no,kind,entity,key_name,content,importance,status,embedding
                FROM memories {where}
                ORDER BY importance DESC, chapter_no DESC
            """, args).fetchall()

        qvec = None
        try:
            qvec = self.embed_client.embed(query, is_query=True)
        except Exception:
            qvec = None
        if qvec:
            qa = array("f", [float(x) for x in qvec])
            scored = []
            for r in rows:
                if r["embedding"] is None:
                    continue
                try:
                    score = _cos(qa, _unpack(r["embedding"]))
                except Exception:
                    continue
                score2 = score + (int(r["importance"]) - 3) * 0.015
                if score >= float(min_score):
                    scored.append((score2, score, r))
            scored.sort(key=lambda x: x[0], reverse=True)
            return [self._row_dict(r, round(float(raw), 4), "vector") for _, raw, r in scored[:top_k]]

        if self.fts_enabled:
            terms = [x for x in query.replace("\n", " ").split(" ") if len(x.strip()) >= 2][:12]
            if terms:
                expr = " OR ".join(f'"{t.replace(chr(34), "")}"' for t in terms)
                extra = " AND m.chapter_no<=?" if max_chapter is not None else ""
                params = [expr]
                if max_chapter is not None:
                    params.append(int(max_chapter))
                params.append(top_k)
                try:
                    with self.conn() as c:
                        rs = c.execute(f"""
                            SELECT m.id,m.chapter_no,m.kind,m.entity,m.key_name,m.content,m.importance,m.status,
                                   bm25(memories_fts) AS rank
                            FROM memories_fts JOIN memories m ON m.id=memories_fts.rowid
                            WHERE memories_fts MATCH ? AND m.active=1 {extra}
                            ORDER BY rank LIMIT ?
                        """, params).fetchall()
                    return [self._row_dict(r, None, "fts") for r in rs]
                except Exception:
                    pass
        return [self._row_dict(r, None, "recent") for r in rows[:top_k]]

    def state_as_of(self, chapter_no=None):
        """Reconstruct latest state per normalized key as of a chapter.

        This is independent of today's active flags, so historical chapter expansion
        cannot accidentally see future state.
        """
        where = ""
        args = []
        if chapter_no is not None:
            where = "WHERE chapter_no<=?"
            args = [int(chapter_no)]
        with self.conn() as c:
            rows = c.execute(f"""
                SELECT id,chapter_no,kind,entity,key_name,content,importance,status
                FROM memories {where}
                ORDER BY chapter_no ASC,id ASC
            """, args).fetchall()
        states = {}
        permanent = []
        hooks = {}
        for r in rows:
            d = self._row_dict(r)
            kind = d["kind"]
            if kind in STATE_KINDS:
                k = (kind, d["entity"], d["key"])
                if d["status"] in {"resolved", "obsolete", "inactive"}:
                    states.pop(k, None)
                else:
                    states[k] = d
            elif kind == "hook":
                k = (d["entity"], d["key"])
                if d["status"] == "resolved":
                    hooks.pop(k, None)
                else:
                    hooks[k] = d
            elif kind in {"fact", "event"}:
                permanent.append(d)
        return {
            "states": list(states.values()),
            "hooks": list(hooks.values()),
            "facts_events": permanent[-200:],
            "as_of_chapter": chapter_no,
        }

    def current_state(self):
        return self.state_as_of(None)

    def project_state(self, chapter_no, records):
        """Project the state snapshot that an uncommitted Canon bundle would create."""
        base = self.state_as_of(max(0, int(chapter_no) - 1))
        states = {
            (x.get("kind"), x.get("entity"), x.get("key")): dict(x)
            for x in base.get("states", [])
        }
        hooks = {
            (x.get("entity"), x.get("key")): dict(x)
            for x in base.get("hooks", [])
        }
        permanent = [dict(x) for x in base.get("facts_events", [])]
        for raw in records or []:
            if not isinstance(raw, dict):
                continue
            rec = normalize_memory_record(raw)
            kind = rec.get("kind", "fact")
            entity = str(rec.get("entity", "") or "")
            key = str(rec.get("key", "") or "")
            status = str(rec.get("status", "active") or "active").lower()
            row = {
                "id": None, "chapter_no": int(chapter_no), "kind": kind,
                "entity": entity, "key": key,
                "content": str(rec.get("content", "") or "").strip(),
                "importance": max(1, min(5, int(rec.get("importance", 3) or 3))),
                "status": status,
            }
            if not row["content"]:
                continue
            if kind in STATE_KINDS:
                marker = (kind, entity, key)
                if status in {"resolved", "obsolete", "inactive"}:
                    states.pop(marker, None)
                else:
                    states[marker] = row
            elif kind == "hook":
                marker = (entity, key)
                if status == "resolved":
                    hooks.pop(marker, None)
                else:
                    hooks[marker] = row
            elif kind in {"fact", "event"}:
                permanent.append(row)
        return {
            "states": list(states.values()),
            "hooks": list(hooks.values()),
            "facts_events": permanent[-200:],
            "as_of_chapter": int(chapter_no),
        }

    def last_canon_chapter(self):
        with self.conn() as c:
            row = c.execute(
                "SELECT COALESCE(MAX(chapter_no),0) AS n FROM chapters WHERE final IS NOT NULL AND length(final)>0"
            ).fetchone()
        return int(row["n"] or 0)

    def project_replaced_chapter_state(self, chapter_no, records, as_of_chapter=None):
        """Project state after replacing one historical chapter's memory rows."""
        n = int(chapter_no)
        end = int(as_of_chapter if as_of_chapter is not None else max(n, self.last_canon_chapter()))
        with self.conn() as c:
            rows = c.execute("""SELECT id,chapter_no,kind,entity,key_name,content,importance,status
                                FROM memories WHERE chapter_no<=? AND chapter_no<>?
                                ORDER BY chapter_no ASC,id ASC""", (end, n)).fetchall()
        stream = [self._row_dict(r) for r in rows]
        for idx, raw in enumerate(records or []):
            if not isinstance(raw, dict):
                continue
            rec = normalize_memory_record(raw)
            content = str(rec.get("content", "") or "").strip()
            if not content:
                continue
            stream.append({
                "id": 10**12 + idx, "chapter_no": n,
                "kind": rec.get("kind", "fact"), "entity": str(rec.get("entity", "") or ""),
                "key": str(rec.get("key", "") or ""), "content": content,
                "importance": max(1, min(5, int(rec.get("importance", 3) or 3))),
                "status": str(rec.get("status", "active") or "active").lower(),
            })
        stream.sort(key=lambda x: (int(x.get("chapter_no", 0)), int(x.get("id") or 0)))
        states, hooks, permanent = {}, {}, []
        for row in stream:
            kind = row["kind"]
            if kind in STATE_KINDS:
                key = (kind, row["entity"], row["key"])
                if row["status"] in {"resolved", "obsolete", "inactive"}:
                    states.pop(key, None)
                else:
                    states[key] = row
            elif kind == "hook":
                key = (row["entity"], row["key"])
                if row["status"] == "resolved":
                    hooks.pop(key, None)
                else:
                    hooks[key] = row
            elif kind in {"fact", "event"}:
                permanent.append(row)
        return {"states": list(states.values()), "hooks": list(hooks.values()),
                "facts_events": permanent[-200:], "as_of_chapter": end}

    def _recompute_active_flags(self, c):
        now = datetime.now().isoformat(timespec="seconds")
        # State kinds: only latest non-resolved record per entity/key remains active.
        for kind in STATE_KINDS:
            c.execute("UPDATE memories SET active=0 WHERE kind=?", (kind,))
            groups = c.execute("""
                SELECT entity,key_name,MAX(chapter_no*1000000000 + id) AS marker
                FROM memories WHERE kind=? GROUP BY entity,key_name
            """, (kind,)).fetchall()
            for g in groups:
                r = c.execute("""
                    SELECT id,status FROM memories WHERE kind=? AND entity=? AND key_name=?
                    ORDER BY chapter_no DESC,id DESC LIMIT 1
                """, (kind, g["entity"], g["key_name"])).fetchone()
                if r and r["status"] not in {"resolved", "obsolete", "inactive"}:
                    c.execute("UPDATE memories SET active=1,status='active',updated_at=? WHERE id=?", (now, r["id"]))
        # Hooks: latest record decides whether the hook is still active.
        c.execute("UPDATE memories SET active=0 WHERE kind='hook'")
        groups = c.execute("SELECT DISTINCT entity,key_name FROM memories WHERE kind='hook'").fetchall()
        for g in groups:
            r = c.execute("""
                SELECT id,status FROM memories WHERE kind='hook' AND entity=? AND key_name=?
                ORDER BY chapter_no DESC,id DESC LIMIT 1
            """, (g["entity"], g["key_name"])).fetchone()
            if r and r["status"] != "resolved":
                c.execute("UPDATE memories SET active=1,status='active',updated_at=? WHERE id=?", (now, r["id"]))
        # Facts/events are historical records; retain unless explicitly inactive/resolved.
        c.execute("""
            UPDATE memories SET active=CASE WHEN status IN ('resolved','obsolete','inactive') THEN 0 ELSE 1 END
            WHERE kind IN ('fact','event')
        """)

    def delete_from_chapter(self, chapter_no):
        n = int(chapter_no)
        with self.lock, self.conn() as c:
            c.execute("DELETE FROM memories WHERE chapter_no>=?", (n,))
            c.execute("DELETE FROM chapters WHERE chapter_no>=?", (n,))
            c.execute("DELETE FROM llm_usage WHERE chapter_no>=?", (n,))
            self._recompute_active_flags(c)

    def save_chapter(self, n, **fields):
        now = datetime.now().isoformat(timespec="seconds")
        with self.lock, self.conn() as c:
            exists = c.execute("SELECT 1 FROM chapters WHERE chapter_no=?", (n,)).fetchone()
            if not exists:
                c.execute("INSERT INTO chapters(chapter_no,created_at,updated_at) VALUES(?,?,?)", (n, now, now))
            allowed = {"source", "plan", "draft", "review", "final", "summary", "handoff", "chars", "generation_seconds", "revision_seconds", "model_tokens"}
            pairs = [(k, v) for k, v in fields.items() if k in allowed]
            if pairs:
                sql = "UPDATE chapters SET " + ",".join(f"{k}=?" for k, _ in pairs) + ",updated_at=? WHERE chapter_no=?"
                c.execute(sql, [v for _, v in pairs] + [now, n])

    def commit_canon(self, n, fields, memories=None):
        """Commit one Canon metadata row and all extracted memories atomically.

        Embeddings are computed before opening the transaction. The transaction
        itself contains only deterministic SQLite writes, so a failed write cannot
        leave Summary/Memory/Handoff on different chapter versions.
        """
        n = int(n)
        fields = dict(fields or {})
        memories = [dict(x) for x in (memories or []) if isinstance(x, dict)]
        prepared = []
        now = datetime.now().isoformat(timespec="seconds")
        for rec in memories:
            rec = normalize_memory_record(rec)
            content = str(rec.get("content", "") or "").strip()
            if not content:
                continue
            kind = str(rec.get("kind", "fact") or "fact").strip()
            entity = str(rec.get("entity", "") or "").strip()
            key_name = str(rec.get("key", "") or "").strip()
            importance = max(1, min(5, int(rec.get("importance", 3) or 3)))
            status = str(rec.get("status", "active") or "active").strip().lower()
            active = 0 if status in {"resolved", "obsolete", "inactive"} else 1
            emb = None
            try:
                vec = self.embed_client.embed(f"[{kind}] {entity} {key_name}\n{content}", is_query=False)
                if vec:
                    emb = _pack(vec)
            except Exception:
                emb = None
            prepared.append((kind, entity, key_name, content, importance, status, active, emb))
        allowed = {"source", "plan", "draft", "review", "final", "summary", "handoff", "chars", "generation_seconds", "revision_seconds", "model_tokens"}
        pairs = [(k, v) for k, v in fields.items() if k in allowed]
        with self.lock, self.conn() as c:
            # Idempotent transaction recovery and same-chapter retry both replace
            # the chapter's memory set instead of duplicating it.
            c.execute("DELETE FROM memories WHERE chapter_no=?", (n,))
            self._recompute_active_flags(c)
            exists = c.execute("SELECT 1 FROM chapters WHERE chapter_no=?", (n,)).fetchone()
            if not exists:
                c.execute("INSERT INTO chapters(chapter_no,created_at,updated_at) VALUES(?,?,?)", (n, now, now))
            if pairs:
                sql = "UPDATE chapters SET " + ",".join(f"{k}=?" for k, _ in pairs) + ",updated_at=? WHERE chapter_no=?"
                c.execute(sql, [v for _, v in pairs] + [now, n])
            for kind, entity, key_name, content, importance, status, active, emb in prepared:
                if active and kind in STATE_KINDS and entity and key_name:
                    c.execute("""UPDATE memories SET active=0,status='superseded',updated_at=?
                               WHERE active=1 AND kind=? AND entity=? AND key_name=?""",
                              (now, kind, entity, key_name))
                if status == "resolved" and kind == "hook" and (entity or key_name):
                    c.execute("""UPDATE memories SET active=0,status='superseded',updated_at=?
                               WHERE active=1 AND kind='hook' AND (?='' OR entity=?) AND (?='' OR key_name=?)""",
                              (now, entity, entity, key_name, key_name))
                c.execute("""INSERT INTO memories
                    (chapter_no,kind,entity,key_name,content,importance,status,active,embedding,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                          (n, kind, entity, key_name, content, importance, status, active, emb, now, now))
            self._recompute_active_flags(c)
        return len(prepared)

    def get_chapter(self, n):
        with self.conn() as c:
            r = c.execute("SELECT * FROM chapters WHERE chapter_no=?", (int(n),)).fetchone()
        return dict(r) if r else None

    def canon_range_rows(self, start, end):
        """Return one lightweight provenance/hash input row per committed Canon chapter."""
        with self.conn() as c:
            rows = c.execute(
                """SELECT chapter_no,source,final,summary,handoff,chars,updated_at
                   FROM chapters
                   WHERE chapter_no BETWEEN ? AND ? AND final IS NOT NULL AND length(final)>0
                   ORDER BY chapter_no""",
                (int(start), int(end)),
            ).fetchall()
        return [dict(row) for row in rows]

    def record_usage(self, usage, chapter_no=0, stage=""):
        if not usage:
            return
        req = str(usage.get("_request_id") or "").strip()
        if not req:
            return
        prompt = int(usage.get("prompt_tokens", 0) or 0)
        hit = int(usage.get("prompt_cache_hit_tokens", 0) or 0)
        miss = int(usage.get("prompt_cache_miss_tokens", max(0, prompt-hit)) or 0)
        completion = int(usage.get("completion_tokens", 0) or 0)
        now = datetime.now().isoformat(timespec="seconds")
        with self.lock, self.conn() as c:
            c.execute("""
                INSERT OR IGNORE INTO llm_usage
                (request_id,chapter_no,stage,provider,model,thinking,prompt_tokens,cache_hit_tokens,
                 cache_miss_tokens,completion_tokens,reasoning_tokens,cost_cny,elapsed_seconds,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                req, int(chapter_no or 0), str(stage or ""), str(usage.get("_provider", "")),
                str(usage.get("_model", "")), 1 if usage.get("_thinking") else 0,
                prompt, hit, miss, completion, int(usage.get("_reasoning_tokens", 0) or 0),
                float(usage.get("_estimated_cost_cny", 0) or 0),
                float(usage.get("_elapsed_seconds", 0) or 0), now,
            ))

    def usage_stats(self, chapter_no=None):
        where = ""
        args = []
        if chapter_no is not None:
            where = "WHERE chapter_no=?"
            args = [int(chapter_no)]
        with self.conn() as c:
            total = c.execute(f"""
                SELECT COALESCE(SUM(prompt_tokens),0) prompt_tokens,
                       COALESCE(SUM(cache_hit_tokens),0) cache_hit_tokens,
                       COALESCE(SUM(completion_tokens),0) completion_tokens,
                       COALESCE(SUM(reasoning_tokens),0) reasoning_tokens,
                       COALESCE(SUM(cost_cny),0) cost_cny
                FROM llm_usage {where}
            """, args).fetchone()
            by_stage = c.execute(f"""
                SELECT stage,provider,model,COUNT(*) calls,
                       COALESCE(SUM(prompt_tokens),0) prompt_tokens,
                       COALESCE(SUM(completion_tokens),0) completion_tokens,
                       COALESCE(SUM(cost_cny),0) cost_cny
                FROM llm_usage {where}
                GROUP BY stage,provider,model ORDER BY stage,provider,model
            """, args).fetchall()
        return {
            "prompt_tokens": int(total["prompt_tokens"]),
            "cache_hit_tokens": int(total["cache_hit_tokens"]),
            "completion_tokens": int(total["completion_tokens"]),
            "reasoning_tokens": int(total["reasoning_tokens"]),
            "cost_cny": round(float(total["cost_cny"]), 6),
            "by_stage": [dict(x) for x in by_stage],
        }

    def stats(self):
        with self.conn() as c:
            row = c.execute("""
                SELECT COUNT(*) AS completed, COALESCE(SUM(chars),0) AS total_chars,
                       COALESCE(SUM(model_tokens),0) AS total_tokens
                FROM chapters WHERE final IS NOT NULL AND length(final)>0
            """).fetchone()
            mem = c.execute("SELECT COUNT(*) AS n FROM memories WHERE active=1").fetchone()["n"]
            all_mem = c.execute("SELECT COUNT(*) AS n FROM memories").fetchone()["n"]
        u = self.usage_stats()
        return {
            "completed_chapters": int(row["completed"]), "total_chars": int(row["total_chars"]),
            "total_model_tokens": int(row["total_tokens"]), "active_memories": int(mem),
            "all_memories": int(all_mem), "api_cost_cny": u["cost_cny"],
        }

    def recent_chapters(self, limit=20):
        with self.conn() as c:
            rs = c.execute("""
                SELECT chapter_no, source, chars, generation_seconds, revision_seconds, updated_at
                FROM chapters WHERE final IS NOT NULL ORDER BY chapter_no DESC LIMIT ?
            """, (limit,)).fetchall()
        out = []
        for r in rs:
            d = dict(r)
            d["cost_cny"] = self.usage_stats(d["chapter_no"])["cost_cny"]
            out.append(d)
        return out
