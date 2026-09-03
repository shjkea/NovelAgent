import hashlib
import difflib
import json
import os
import re
import shutil
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from queue import Queue

from memory_db import (
    EmbeddingClient,
    MemoryDB,
    STATE_KINDS,
    normalize_memory_record,
)
from continuity import (
    adjacent_seams_covered,
    audit_windows,
    degraded_handoff,
    deterministic_boundary_findings,
    extract_source_tail,
    normalize_handoff,
)
from canon_guard import (
    build_canon_ledger,
    deterministic_canon_findings,
    format_canon_ledger,
    normalize_light_quality_checks,
    recent_chapter_fulltexts,
    verify_canon_publish,
)
from external_canon import (
    ExternalCanonError,
    atomic_write_json,
    canonical_chapter_text,
    external_canon_ranges,
    find_range,
    load_manifest,
    new_manifest,
    range_key,
    sha256_file,
    sha256_text,
    validate_chapter_package,
    verify_manifest_files,
)
from provider_router import (
    LLMRouter,
    ProviderCancelledError,
    ProviderRefusalError,
    calculate_volcengine_afp,
)
try:
    from provider_router import ProviderLengthError
except ImportError:  # compatibility with older provider_router builds
    class ProviderLengthError(RuntimeError):
        pass


class PlanContextOverflowError(RuntimeError):
    """Plan preflight stopped before the provider call and awaits user approval."""
    pass


class CanonContextLimitError(RuntimeError):
    """A Canon stage exceeded the persistent high-context hard limit."""
    pass


class FinalQualityGateError(RuntimeError):
    """A revised candidate failed its mandatory post-revision review."""
    pass


class PlanQualityGateError(RuntimeError):
    """A chapter Plan remained uncertain or inconsistent after bounded retries."""
    pass


class CanonCommitError(RuntimeError):
    """A prepared Canon bundle could not be published consistently."""
    pass


class ExternalCanonGateError(RuntimeError):
    """Normal generation reached an externally owned Canon range or locked exit."""
    pass


class EventHub:
    def __init__(self):
        self.lock = threading.Lock()
        self.subscribers = []

    def subscribe(self):
        q = Queue(maxsize=2000)
        with self.lock:
            self.subscribers.append(q)
        return q

    def unsubscribe(self, q):
        with self.lock:
            if q in self.subscribers:
                self.subscribers.remove(q)

    def publish(self, event):
        dead = []
        with self.lock:
            for q in self.subscribers:
                try:
                    q.put_nowait(event)
                except Exception:
                    dead.append(q)
            for q in dead:
                if q in self.subscribers:
                    self.subscribers.remove(q)


def _strip_json_fence(text):
    txt = (text or "").lstrip("\ufeff").strip()
    txt = re.sub(r"<think\b[^>]*>.*?</think\s*>", "", txt, flags=re.I | re.S).strip()
    txt = re.sub(r"^```(?:json)?\s*", "", txt, flags=re.I)
    txt = re.sub(r"\s*```$", "", txt)
    return txt.strip()


def _balanced_json_candidates(text):
    """Yield balanced JSON objects from prose/fences without being fooled by braces in strings."""
    txt = _strip_json_fence(text)
    for start, ch in enumerate(txt):
        if ch != "{":
            continue
        depth = 0
        in_string = False
        escaped = False
        for end in range(start, len(txt)):
            current = txt[end]
            if in_string:
                if escaped:
                    escaped = False
                elif current == "\\":
                    escaped = True
                elif current == '"':
                    in_string = False
                continue
            if current == '"':
                in_string = True
            elif current == "{":
                depth += 1
            elif current == "}":
                depth -= 1
                if depth == 0:
                    yield txt[start:end + 1]
                    break


def _json_obj(text, fallback=None):
    txt = _strip_json_fence(text)
    candidates = [txt, *_balanced_json_candidates(txt)]
    seen = set()
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        for variant in (candidate, re.sub(r",\s*([}\]])", r"\1", candidate)):
            try:
                return json.loads(variant)
            except Exception:
                pass
    return fallback


def _estimate_stream_tokens(text):
    """Cheap live token estimate for UI only; final usage overwrites it."""
    t = text or ""
    if not t:
        return 0.0
    cjk = len(re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]", t))
    other = max(0, len(t.strip()) - cjk)
    return float(cjk) + float(other) / 4.0


def _read_prompt(root, name):
    """Read an editable prompt file on demand so changes apply to the next request."""
    path = Path(root) / "prompts" / name
    try:
        return path.read_text(encoding="utf-8").strip()
    except FileNotFoundError as e:
        raise RuntimeError(f"缺少提示词文件：{path}") from e


class NovelAgent:
    def __init__(self, root, config_loader, hub):
        self.root = Path(root)
        self.config_loader = config_loader
        self.hub = hub
        self.stop_event = threading.Event()
        self.thread = None
        self.lock = threading.RLock()
        self.live_output = False
        self._run_start_override = None
        self._run_count_override = None
        self.dlc_stop_event = threading.Event()
        self.dlc_thread = None
        self.dlc_lock = threading.RLock()
        self.dlc_status = {
            "running": False, "chapter": None, "scene_id": "", "stage": "空闲",
            "started_at": None, "elapsed_seconds": 0.0, "first_chunk_at": None,
            "last_chunk_at": None, "stream_est_tokens": 0.0, "display_tps": 0.0,
            "model_tps": 0.0, "prompt_tps": 0.0, "output_chars": 0,
            "last_error": "", "output_file": "", "canon_hash": "",
            "custom_prompt": "", "max_tokens": 0, "preview_text": "",
            "draw_count": 1, "current_draw": 0, "candidates_completed": 0,
            "candidates_passed": 0, "candidates_blocked": 0,
            "review_status": "", "candidate_id": "", "last_candidate_id": "",
            "prompt_tokens": 0, "completion_tokens": 0,
            "request_count": 0, "provider": "grok", "model": "",
        }
        self.audit_stop_event = threading.Event()
        self.audit_thread = None
        self.audit_lock = threading.RLock()
        self.audit_status = {
            "running": False, "run_id": "", "start": None, "end": None,
            "segment_size": 4, "segment_index": 0, "segment_total": 0,
            "stage": "空闲", "stage_label": "", "started_at": None,
            "elapsed_seconds": 0.0, "last_error": "", "report_file": "",
            "status": "", "source_check": True, "prompt_tokens": 0,
            "cache_hit_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0,
            "cost_cny": 0.0, "afp": 0.0, "request_count": 0,
        }

        # Audit-driven multi-chapter repair is intentionally isolated from normal
        # Canon generation and from the audit itself.  It only produces candidates
        # until an explicit transactional commit.
        self.repair_stop_event = threading.Event()
        self.repair_thread = None
        self.repair_lock = threading.RLock()
        self.repair_status = {
            "running": False, "batch_id": "", "stage": "空闲", "stage_label": "",
            "started_at": None, "elapsed_seconds": 0.0, "last_error": "",
            "model": "deepseek-v4-pro", "item_index": 0, "item_total": 0,
            "candidate_ready": 0, "candidate_blocked": 0,
            "joint_safe": None, "committed": False, "rolled_back": False,
            "last_commit_mode": "", "last_commit_forced": False,
            "last_commit_manual": False, "last_commit_failed_gates": [],
            "last_commit_chapters": [], "last_commit_force_reason": "",
            "prompt_tokens": 0, "cache_hit_tokens": 0, "completion_tokens": 0,
            "reasoning_tokens": 0, "cost_cny": 0.0, "afp": 0.0,
            "request_count": 0,
        }

        # Reader reflow is a non-destructive maintenance pipeline.  The model
        # may choose paragraph boundaries only; original prose is always copied
        # locally and verified before a reader version is written.
        self.reader_stop_event = threading.Event()
        self.reader_thread = None
        self.reader_lock = threading.RLock()
        self._reader_router_local = threading.local()
        self.reader_status = {
            "running": False, "start": None, "end": None, "chapter": None,
            "stage": "空闲", "stage_label": "", "started_at": None,
            "elapsed_seconds": 0.0, "workers": 4, "overwrite": False,
            "item_total": 0, "item_done": 0, "completed": 0,
            "skipped": 0, "failed": 0, "last_error": "", "errors": [],
            "last_output_chapter": None, "output_dir": "reader_chapters",
            "prompt_tokens": 0, "cache_hit_tokens": 0,
            "completion_tokens": 0, "reasoning_tokens": 0,
            "cost_cny": 0.0, "afp": 0.0, "request_count": 0,
        }

        self.external_import_thread = None
        self.external_import_lock = threading.RLock()
        self.external_import_status = {
            "running": False, "range_key": "", "start": None, "end": None,
            "label": "", "stage": "空闲", "stage_label": "",
            "started_at": None, "elapsed_seconds": 0.0,
            "chapter": None, "item_total": 0, "item_done": 0,
            "imported": 0, "skipped": 0, "last_error": "",
            "package_sha256": "", "range_digest": "",
            "manifest_file": "", "boundary_status": "pending",
            "exit_state_status": "pending", "errors": [],
        }

        self.status = {
            "running": False, "chapter": None, "stage": "空闲", "stage_label": "",
            "stage_started_at": None, "stage_elapsed_seconds": 0.0,
            "stage_first_chunk_at": None, "stage_stream_chunks": 0,
            "stage_estimated_tps": 0.0, "stage_completion_tokens": 0,
            "stage_prompt_tokens": 0, "stage_cache_hit_tokens": 0,
            "stage_reasoning_tokens": 0, "stage_cost_cny": 0.0,
            "stage_stream_est_tokens": 0.0, "stage_reasoning_est_tokens": 0.0,
            "stage_output_est_tokens": 0.0, "stage_reasoning_tps": 0.0,
            "stage_output_tps": 0.0, "stage_last_chunk_at": None,
            "stage_first_reasoning_at": None, "stage_first_output_at": None,
            "stage_provider": "", "stage_model": "", "stage_thinking": False,
            "stage_context_limit": 0, "chapter_chars": 0, "session_chars": 0,
            "char_per_sec": 0.0, "model_tps": 0.0, "prompt_tps": 0.0,
            "completion_tokens_last": 0, "prompt_tokens_last": 0,
            "session_model_tokens": 0, "retrieved_memories": 0,
            "last_error": "", "started_at": None, "auto_nsfw_decision": None,
            "preview_text": "", "preview_label": "", "preview_chapter": None,
            "chapter_complexity_score": 0, "chapter_complexity_level": "normal",
            "chapter_complexity_label": "普通", "chapter_complexity_reasons": [],
            "stage_context_target_tokens": 0, "stage_context_estimated_tokens": 0,
            "stage_context_trimmed": False,
            "plan_arc_status": "idle", "plan_arc_relation": "unknown",
            "plan_arc_confidence": "LOW", "plan_arc_error": "",
            "plan_gate_status": "idle", "plan_gate_attempts": 0,
            "plan_gate_error": "",
            "handoff_status": "none", "handoff_chapter": None,
            "handoff_error": "", "handoff_tail_chars": 0,
            "plan_overflow": {
                "pending": False, "chapter": None, "estimated_tokens": 0,
                "reason": "",
                "target_tokens": 0, "safe_tokens": 0, "provider_safe_tokens": 0,
                "over_tokens": 0, "resume_count": 0, "created_at": None,
                "hard_blocked": False,
                "auto_window_size": 10, "auto_window_allowed": 6,
                "auto_window_used": 0, "auto_window_remaining": 6,
                "history_chapters_checked": 0,
            },
        }
        self._plan_overflow_approval = None
        next_chapter = 1
        try:
            state_path = self.root / "state.json"
            if state_path.exists():
                loaded = json.loads(state_path.read_text(encoding="utf-8"))
                next_chapter = max(1, int(loaded.get("next_chapter", 1) or 1))
        except Exception:
            next_chapter = 1
        # Whole-chapter billing history lives in SQLite, so restarts preserve
        # the rolling cost decision without a second counter in state.json.
        try:
            history = self._chapter_cost_guard_usage(next_chapter)
            self.status["plan_overflow"].update({
                "auto_window_size": history["window_size"],
                "auto_window_allowed": history["confirm_at"],
                "auto_window_used": history["over_limit"],
                "auto_window_remaining": max(0, history["confirm_at"] - history["over_limit"]),
                "history_chapters_checked": history["checked"],
                "cost_guard_mode": history["mode"],
                "cost_guard_limit": history["limit"],
            })
        except Exception:
            pass
        self._complexity_cache = {}
        self._stage_contract_cache = []
        cfg = self.config_loader()
        self.live_output = bool(cfg.get("web", {}).get("live_output_default", False))
        self.embed = EmbeddingClient(cfg["embedding"])
        self.db = MemoryDB(self.root / "novel_memory.sqlite3", self.embed)
        self._recover_canon_transactions()
        self.router = None
        self.reload_clients()

    def reload_clients(self):
        cfg = self.config_loader()
        self.embed.cfg = cfg["embedding"]
        self.router = LLMRouter(
            self.root, self.config_loader,
            on_metrics=self._on_metrics, on_chunk=self._on_chunk, logger=self.log,
            stop_event=self.stop_event,
        )
        self.dlc_router = LLMRouter(
            self.root, self.config_loader,
            on_metrics=self._on_dlc_metrics, on_chunk=self._on_dlc_chunk, logger=self.log,
            stop_event=self.dlc_stop_event,
        )
        self.audit_router = LLMRouter(
            self.root, self.config_loader,
            on_metrics=self._on_audit_metrics, on_chunk=None, logger=self.log,
            stop_event=self.audit_stop_event,
        )
        self.repair_router = LLMRouter(
            self.root, self.config_loader,
            on_metrics=self._on_repair_metrics, on_chunk=None, logger=self.log,
            stop_event=self.repair_stop_event,
        )
        # Worker-local reader routers are recreated on the next reader job so a
        # config/API-account change never leaks an old client into a new batch.
        self._reader_router_local = threading.local()

    def _emit(self, typ, **payload):
        self.hub.publish({"type": typ, **payload})

    def log(self, text):
        self._emit("log", text=str(text))

    def _record_billing_meta(self, usage):
        """Persist provider/source metadata that the legacy llm_usage table does not keep."""
        request_id = str((usage or {}).get("_request_id", "") or "").strip()
        if not request_id:
            return
        db_path = self.root / "novel_memory.sqlite3"
        with sqlite3.connect(db_path, timeout=5) as con:
            con.execute(
                """CREATE TABLE IF NOT EXISTS llm_billing_meta (
                       request_id TEXT PRIMARY KEY,
                       api_source TEXT NOT NULL DEFAULT '',
                       api_account TEXT NOT NULL DEFAULT '',
                       api_account_label TEXT NOT NULL DEFAULT '',
                       request_started_at TEXT NOT NULL DEFAULT '',
                       estimated_afp REAL,
                       estimated_cost_cny REAL NOT NULL DEFAULT 0
                   )"""
            )
            con.execute(
                """INSERT OR REPLACE INTO llm_billing_meta
                   (request_id, api_source, api_account, api_account_label, request_started_at, estimated_afp, estimated_cost_cny)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    request_id,
                    str((usage or {}).get("_api_source", "") or ""),
                    str((usage or {}).get("_api_account", "") or ""),
                    str((usage or {}).get("_api_account_label", "") or ""),
                    str((usage or {}).get("_request_started_at", "") or ""),
                    (usage or {}).get("_estimated_afp"),
                    float((usage or {}).get("_estimated_cost_cny", 0) or 0),
                ),
            )
            con.commit()

    def _on_metrics(self, timings, usage, label=""):
        with self.lock:
            if timings:
                if timings.get("predicted_per_second") is not None:
                    self.status["model_tps"] = round(float(timings["predicted_per_second"]), 2)
                if timings.get("prompt_per_second") is not None:
                    self.status["prompt_tps"] = round(float(timings["prompt_per_second"]), 2)
            if usage:
                c = int(usage.get("completion_tokens", 0) or 0)
                p = int(usage.get("prompt_tokens", 0) or 0)
                hit = int(usage.get("prompt_cache_hit_tokens", 0) or 0)
                reasoning = int(usage.get("_reasoning_tokens", 0) or 0)
                self.status["completion_tokens_last"] = c
                self.status["prompt_tokens_last"] = p
                self.status["stage_completion_tokens"] = c
                self.status["stage_prompt_tokens"] = p
                self.status["stage_cache_hit_tokens"] = hit
                self.status["stage_reasoning_tokens"] = reasoning
                self.status["stage_cost_cny"] = float(usage.get("_estimated_cost_cny", 0) or 0)
                self.status["stage_afp_estimate"] = usage.get("_estimated_afp")
                self.status["stage_api_source"] = usage.get("_api_source", "official" if usage.get("_provider") == "deepseek" else "")
                self.status["stage_provider"] = usage.get("_provider", "")
                self.status["stage_model"] = usage.get("_model", "")
                self.status["stage_thinking"] = bool(usage.get("_thinking", False))
                self.status["stage_context_limit"] = 1_000_000 if usage.get("_provider") == "deepseek" else int(
                    self.config_loader().get("management", {}).get("performance_profiles", {}).get(
                        self.config_loader().get("management", {}).get("active_performance", "balanced"), {}
                    ).get("context", 32768)
                )
                self.status["session_model_tokens"] += c
                chapter = int(self.status.get("chapter") or 0)
        if usage:
            try:
                self.db.record_usage(usage, chapter_no=chapter, stage=label)
                self._record_billing_meta(usage)
            except Exception as e:
                self.log(f"API 用量写库失败：{e}")

    def _on_audit_metrics(self, timings, usage, label=""):
        if not usage:
            return
        with self.audit_lock:
            self.audit_status["prompt_tokens"] += int(usage.get("prompt_tokens", 0) or 0)
            self.audit_status["cache_hit_tokens"] += int(usage.get("prompt_cache_hit_tokens", 0) or 0)
            self.audit_status["completion_tokens"] += int(usage.get("completion_tokens", 0) or 0)
            self.audit_status["reasoning_tokens"] += int(usage.get("_reasoning_tokens", 0) or 0)
            self.audit_status["cost_cny"] = round(
                float(self.audit_status.get("cost_cny", 0) or 0) + float(usage.get("_estimated_cost_cny", 0) or 0), 6
            )
            afp = usage.get("_estimated_afp")
            if afp is not None:
                self.audit_status["afp"] = round(float(self.audit_status.get("afp", 0) or 0) + float(afp or 0), 3)
            self.audit_status["request_count"] += 1
        try:
            # Audit usage is deliberately chapter_no=0 so it never contaminates Canon chapter billing.
            self.db.record_usage(usage, chapter_no=0, stage=label or "audit")
            self._record_billing_meta(usage)
        except Exception as e:
            self.log(f"剧情审计 API 用量写库失败：{e}")

    def audit_snapshot(self):
        with self.audit_lock:
            out = dict(self.audit_status)
        if out.get("running") and out.get("started_at"):
            out["elapsed_seconds"] = round(max(0.0, time.time() - float(out["started_at"])), 1)
        return out

    def _on_repair_metrics(self, timings, usage, label=""):
        if not usage:
            return
        with self.repair_lock:
            self.repair_status["prompt_tokens"] += int(usage.get("prompt_tokens", 0) or 0)
            self.repair_status["cache_hit_tokens"] += int(usage.get("prompt_cache_hit_tokens", 0) or 0)
            self.repair_status["completion_tokens"] += int(usage.get("completion_tokens", 0) or 0)
            self.repair_status["reasoning_tokens"] += int(usage.get("_reasoning_tokens", 0) or 0)
            self.repair_status["cost_cny"] = round(
                float(self.repair_status.get("cost_cny", 0) or 0)
                + float(usage.get("_estimated_cost_cny", 0) or 0), 6
            )
            afp = usage.get("_estimated_afp")
            if afp is not None:
                self.repair_status["afp"] = round(
                    float(self.repair_status.get("afp", 0) or 0) + float(afp or 0), 3
                )
            self.repair_status["request_count"] += 1
        try:
            # Maintenance traffic is chapter_no=0 and never mixed with chapter billing.
            self.db.record_usage(usage, chapter_no=0, stage=label or "audit_repair")
            self._record_billing_meta(usage)
        except Exception as e:
            self.log(f"审计修复 API 用量写库失败：{e}")

    def repair_snapshot(self):
        with self.repair_lock:
            out = dict(self.repair_status)
        if out.get("running") and out.get("started_at"):
            out["elapsed_seconds"] = round(max(0.0, time.time() - float(out["started_at"])), 1)
        return out

    def _on_reader_metrics(self, timings, usage, label=""):
        if not usage:
            return
        with self.reader_lock:
            self.reader_status["prompt_tokens"] += int(usage.get("prompt_tokens", 0) or 0)
            self.reader_status["cache_hit_tokens"] += int(usage.get("prompt_cache_hit_tokens", 0) or 0)
            self.reader_status["completion_tokens"] += int(usage.get("completion_tokens", 0) or 0)
            self.reader_status["reasoning_tokens"] += int(usage.get("_reasoning_tokens", 0) or 0)
            self.reader_status["cost_cny"] = round(
                float(self.reader_status.get("cost_cny", 0) or 0)
                + float(usage.get("_estimated_cost_cny", 0) or 0), 6
            )
            afp = usage.get("_estimated_afp")
            if afp is not None:
                self.reader_status["afp"] = round(
                    float(self.reader_status.get("afp", 0) or 0) + float(afp or 0), 3
                )
            self.reader_status["request_count"] += 1
        try:
            # Reader maintenance is not Canon generation and must not pollute a
            # chapter's normal generation bill.
            self.db.record_usage(usage, chapter_no=0, stage=label or "reader_reflow")
            self._record_billing_meta(usage)
        except Exception as e:
            self.log(f"读者版智能分段 API 用量写库失败：{e}")

    def reader_snapshot(self):
        with self.reader_lock:
            out = dict(self.reader_status)
            out["errors"] = list(self.reader_status.get("errors") or [])
        if out.get("running") and out.get("started_at"):
            out["elapsed_seconds"] = round(max(0.0, time.time() - float(out["started_at"])), 1)
        total = int(out.get("item_total") or 0)
        done = int(out.get("item_done") or 0)
        out["progress_pct"] = round(done * 100.0 / total, 2) if total else 0.0
        return out

    def _on_dlc_metrics(self, timings, usage, label=""):
        with self.dlc_lock:
            if timings:
                if timings.get("predicted_per_second") is not None:
                    self.dlc_status["model_tps"] = round(float(timings["predicted_per_second"]), 2)
                if timings.get("prompt_per_second") is not None:
                    self.dlc_status["prompt_tps"] = round(float(timings["prompt_per_second"]), 2)
            if usage:
                self.dlc_status["prompt_tokens"] += int(usage.get("prompt_tokens", 0) or 0)
                self.dlc_status["completion_tokens"] += int(usage.get("completion_tokens", 0) or 0)
                self.dlc_status["request_count"] += 1
                self.dlc_status["provider"] = str(usage.get("_provider", "grok") or "grok")
                self.dlc_status["model"] = str(usage.get("_model", "") or "")
        # Third-party Grok pricing is provider-specific. Keep only the live token
        # and request counters; do not estimate or write DLC billing history.

    def _on_dlc_chunk(self, text, label, elapsed, emit_text=False, kind="content"):
        if kind == "reasoning":
            return
        now = time.time()
        est = _estimate_stream_tokens(text)
        with self.dlc_lock:
            if self.dlc_status.get("first_chunk_at") is None:
                self.dlc_status["first_chunk_at"] = now
            self.dlc_status["last_chunk_at"] = now
            self.dlc_status["stream_est_tokens"] += est
            self.dlc_status["output_chars"] += len(text or "")
            self.dlc_status["preview_text"] = (self.dlc_status.get("preview_text", "") + (text or ""))[-80000:]
            elapsed2 = max(0.001, now - float(self.dlc_status["first_chunk_at"]))
            self.dlc_status["display_tps"] = round(self.dlc_status["stream_est_tokens"] / elapsed2, 2)
        if text:
            self._emit("dlc_output", text=text, label=label)

    def _on_chunk(self, text, label, elapsed, emit_text=False, kind="content"):
        now = time.time()
        est_tokens = _estimate_stream_tokens(text)
        with self.lock:
            if self.status["stage_first_chunk_at"] is None:
                self.status["stage_first_chunk_at"] = now
            self.status["stage_last_chunk_at"] = now
            self.status["stage_stream_chunks"] += 1
            self.status["stage_stream_est_tokens"] += est_tokens
            gen_elapsed = max(0.001, now - float(self.status["stage_first_chunk_at"]))
            self.status["stage_estimated_tps"] = round(
                self.status["stage_stream_est_tokens"] / gen_elapsed, 2
            )

            if kind == "reasoning":
                if self.status["stage_first_reasoning_at"] is None:
                    self.status["stage_first_reasoning_at"] = now
                self.status["stage_thinking"] = True
                self.status["stage_reasoning_est_tokens"] += est_tokens
                r_elapsed = max(0.001, now - float(self.status["stage_first_reasoning_at"]))
                self.status["stage_reasoning_tps"] = round(
                    self.status["stage_reasoning_est_tokens"] / r_elapsed, 2
                )
                # Before final usage arrives, expose an estimated live count.
                self.status["stage_reasoning_tokens"] = max(
                    int(self.status.get("stage_reasoning_tokens", 0) or 0),
                    int(round(self.status["stage_reasoning_est_tokens"])),
                )
            else:
                if self.status["stage_first_output_at"] is None:
                    self.status["stage_first_output_at"] = now
                self.status["stage_output_est_tokens"] += est_tokens
                o_elapsed = max(0.001, now - float(self.status["stage_first_output_at"]))
                self.status["stage_output_tps"] = round(
                    self.status["stage_output_est_tokens"] / o_elapsed, 2
                )
                if label in ("draft", "revision", "expand", "polish", "minor"):
                    self.status["chapter_chars"] += len(text)
                    self.status["session_chars"] += len(text)
                    if self.live_output:
                        self.status["preview_label"] = label
                        self.status["preview_text"] = (self.status.get("preview_text", "") + (text or ""))[-80000:]
                    if elapsed > 0:
                        self.status["char_per_sec"] = round(self.status["chapter_chars"] / elapsed, 2)
        if kind != "reasoning" and self.live_output and label in ("draft", "revision", "expand", "polish", "minor") and text:
            self._emit("canon_output", text=text, label=label)
        if kind != "reasoning" and emit_text and self.live_output:
            self._emit("output", text=text, label=label)

    def set_live_output(self, enabled):
        self.live_output = bool(enabled)
        self._emit("live", enabled=self.live_output)

    def snapshot(self):
        with self.lock:
            s = dict(self.status)
        high_enabled, high_target, high_max = self.high_context_policy()
        s["high_context_mode_enabled"] = high_enabled
        s["high_context_target_tokens"] = high_target
        s["high_context_max_tokens"] = high_max
        if s.get("stage_started_at"):
            s["stage_elapsed_seconds"] = round(max(0.0, time.time() - float(s["stage_started_at"])), 1)
        else:
            s["stage_elapsed_seconds"] = 0.0
        now = time.time()
        last_chunk = s.get("stage_last_chunk_at")
        idle = max(0.0, now - float(last_chunk)) if last_chunk else None
        s["stream_idle_seconds"] = round(idle, 1) if idle is not None else None
        live_tps = float(s.get("stage_output_tps", 0.0) or 0.0)
        if live_tps <= 0:
            live_tps = float(s.get("stage_reasoning_tps", 0.0) or 0.0)
        if live_tps <= 0:
            live_tps = float(s.get("stage_estimated_tps", 0.0) or 0.0)
        if last_chunk and idle is not None and idle <= 8.0:
            s["display_tps"] = round(live_tps, 2)
            s["display_tps_source"] = "live_stream_estimate"
        elif last_chunk and idle is not None and idle > 8.0:
            s["display_tps"] = 0.0
            s["display_tps_source"] = "stream_idle"
        else:
            s["display_tps"] = float(s.get("model_tps", 0.0) or 0.0)
            s["display_tps_source"] = "model_metrics"
        s["stream_stalled"] = bool(last_chunk and idle is not None and idle > 30.0 and s.get("stage") != "空闲")
        s["live_output"] = self.live_output
        s["db"] = self.db.stats()
        services = self.router.health()
        ok2, msg2 = self.embed.health()
        s["services"] = {
            "deepseek": services.get("deepseek", {}),
            "grok": services.get("grok", {}),
            "embedding": {"ok": ok2, "detail": msg2},
        }
        limit = int(s.get("stage_context_limit") or 0)
        prompt = int(s.get("stage_prompt_tokens") or 0)
        s["stage_context_pct"] = round(prompt * 100.0 / limit, 2) if limit else 0.0
        s["usage_total"] = self.db.usage_stats()
        s["usage_chapter"] = self.db.usage_stats(s.get("chapter")) if s.get("chapter") else {}
        return s

    def read(self, name):
        p = self.root / name
        return p.read_text(encoding="utf-8") if p.exists() else ""

    def read_story(self, name):
        """Read novel-specific source material from the dedicated story/ directory."""
        p = self.root / "story" / name
        return p.read_text(encoding="utf-8") if p.exists() else ""

    def write(self, name, text):
        p = self.root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text((text or "").strip() + "\n", encoding="utf-8")

    def _continuity_config(self):
        cfg = self.config_loader().get("continuity", {}) or {}
        return {
            "source_tail_chars": max(500, min(12000, int(cfg.get("source_tail_chars", 2600) or 2600))),
            "handoff_max_chars": max(6000, min(30000, int(cfg.get("handoff_max_chars", 20000) or 20000))),
            "future_boundary_max_chars": max(1000, min(10000, int(cfg.get("future_boundary_max_chars", 5000) or 5000))),
            "audit_window_chapters": max(2, min(8, int(cfg.get("audit_window_chapters", 4) or 4))),
            "audit_window_overlap": max(1, min(3, int(cfg.get("audit_window_overlap", 1) or 1))),
            "recent_scene_limit": max(4, min(20, int(cfg.get("recent_scene_limit", 10) or 10))),
            "ledger_prompt_max_chars": max(6000, min(24000, int(cfg.get("ledger_prompt_max_chars", 14000) or 14000))),
        }

    def _writing_quality_config(self):
        cfg = self.config_loader().get("writing_guardrails", {}) or {}
        return {
            "silent_constraints": bool(cfg.get("silent_constraints", True)),
            "recent_fulltext_chapters": max(0, min(5, int(cfg.get("recent_fulltext_chapters", 3) or 3))),
            "recent_fulltext_max_chars": max(6000, min(60000, int(cfg.get("recent_fulltext_max_chars", 30000) or 30000))),
            "light_scene_sufficiency": bool(cfg.get("light_scene_sufficiency", True)),
            "soft_style_repetition": bool(cfg.get("soft_style_repetition", True)),
            "canon_commit_verification": bool(cfg.get("canon_commit_verification", True)),
        }

    def canon_ledger(self, n):
        cfg = self._continuity_config()
        return build_canon_ledger(
            self.root, int(n), recent_scene_limit=cfg["recent_scene_limit"]
        )

    def canon_guard_context(self, n):
        cfg = self._continuity_config()
        return format_canon_ledger(
            self.canon_ledger(n), max_chars=cfg["ledger_prompt_max_chars"]
        )

    # ---------- externally authored Canon ranges ----------

    def _external_ranges(self):
        return external_canon_ranges(self.config_loader(), self.read_story("outline.md"))

    def _external_range_root(self, spec):
        return self.root / "runtime" / "external_canon" / range_key(spec)

    def _external_manifest_path(self, spec):
        return self._external_range_root(spec) / "manifest.json"

    def _load_external_manifest(self, spec):
        return load_manifest(self._external_manifest_path(spec))

    def _external_range_file_status(self, spec):
        try:
            manifest = self._load_external_manifest(spec)
            status = str((manifest or {}).get("status") or "waiting")
            entries = (manifest or {}).get("entries") or {}
            imported = len(entries) if isinstance(entries, dict) else 0
            error = str((manifest or {}).get("last_error") or "")
        except Exception as exc:
            manifest, status, imported, error = None, "invalid", 0, str(exc)
        total = int(spec["end"]) - int(spec["start"]) + 1
        return {
            **dict(spec), "key": range_key(spec), "status": status,
            "imported": min(total, imported), "total": total,
            "remaining": max(0, total - imported), "complete": status == "complete",
            "last_error": error,
            "manifest_file": str(self._external_manifest_path(spec).relative_to(self.root)).replace("\\", "/"),
        }

    @staticmethod
    def _external_state_sha(state):
        value = json.loads(json.dumps(state or {}, ensure_ascii=False))
        if isinstance(value, dict):
            value.pop("generated_at", None)
            for group in ("states", "hooks", "facts_events"):
                for row in value.get(group, []) or []:
                    if isinstance(row, dict):
                        row.pop("id", None)
                        row.pop("active", None)
                        row.pop("retrieval", None)
        return hashlib.sha256(
            json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    def verify_external_range(self, spec):
        """Verify manifest, Canon files, Handoffs, database provenance, and exit state."""
        manifest = self._load_external_manifest(spec)
        continuity_cfg = self._continuity_config()
        errors = verify_manifest_files(
            self.root, spec, manifest,
            source_tail_chars=continuity_cfg["source_tail_chars"],
        )
        start, end = int(spec["start"]), int(spec["end"])
        rows = {int(row["chapter_no"]): row for row in self.db.canon_range_rows(start, end)}
        expected_hashes = (manifest or {}).get("expected_chapter_hashes") or {}
        for chapter in range(start, end + 1):
            row = rows.get(chapter)
            if not row:
                errors.append(f"database is missing Canon chapter {chapter}")
                continue
            if str(row.get("source") or "") != "external_canon":
                errors.append(f"chapter {chapter} database source is not external_canon")
            if sha256_text(row.get("final") or "") != str(expected_hashes.get(str(chapter)) or ""):
                errors.append(f"chapter {chapter} database body does not match import manifest")
            if not str(row.get("summary") or "").strip():
                errors.append(f"chapter {chapter} database Summary is missing")
            try:
                handoff = json.loads(str(row.get("handoff") or "{}"))
                if handoff.get("status") != "complete" or int(handoff.get("chapter_no", 0) or 0) != chapter:
                    errors.append(f"chapter {chapter} database Handoff is not complete")
            except Exception:
                errors.append(f"chapter {chapter} database Handoff is invalid")
            if len(errors) >= 60:
                errors.append("additional database verification errors omitted")
                break
        boundary = (manifest or {}).get("boundary_validation") or {}
        if boundary.get("status") != "passed" or int(boundary.get("previous_chapter", 0) or 0) != start - 1:
            errors.append(f"external Canon entry seam {start-1}-{start} has not passed validation")
        exit_path = self._external_range_root(spec) / "exit_state.json"
        if exit_path.exists():
            try:
                exit_state = json.loads(exit_path.read_text(encoding="utf-8"))
                actual_state = self.db.state_as_of(end)
                actual_state_sha = self._external_state_sha(actual_state)
                if str(exit_state.get("current_state_sha256") or "") != actual_state_sha:
                    errors.append("chapter exit state no longer matches database state projection")
                if self.db.last_canon_chapter() == end:
                    current_state_path = self.root / "current_state.json"
                    current_state = json.loads(current_state_path.read_text(encoding="utf-8"))
                    current_state_sha = self._external_state_sha(current_state)
                    if current_state_sha != actual_state_sha:
                        errors.append("current_state.json does not match external Canon exit projection")
                    state_file = json.loads((self.root / "state.json").read_text(encoding="utf-8"))
                    if int(state_file.get("next_chapter", 0) or 0) != end + 1:
                        errors.append("state.json does not point to the first chapter after the external Canon range")
                snapshot = json.loads(
                    (self.root / "runtime" / "state_snapshots" / f"{end:04d}.json").read_text(encoding="utf-8")
                )
                if int(snapshot.get("chapter", 0) or 0) != end or snapshot.get("source") != "external_canon":
                    errors.append("final external Canon state snapshot is missing provenance")
            except Exception:
                errors.append("chapter exit state cannot be verified against database state")
        return list(dict.fromkeys(errors))

    def external_generation_gate(self, chapter_no, *, deep=True):
        """Return whether normal generation owns this chapter and all prior ranges are sealed."""
        chapter_no = max(1, int(chapter_no))
        ranges = self._external_ranges()
        owned = find_range(ranges, chapter_no)
        if owned:
            return False, (
                f"第 {owned['start']}-{owned['end']} 章是{owned['label']}，由外部写作后导入普通 Canon。"
                f"NovelAgent 不会生成该范围，也不会跳过它。"
            ), owned
        for spec in ranges:
            if chapter_no <= int(spec["end"]):
                continue
            try:
                manifest = self._load_external_manifest(spec)
            except Exception as exc:
                return False, f"外部正史卷 {spec['start']}-{spec['end']} 清单无效：{exc}", spec
            if not manifest or manifest.get("status") != "complete":
                return False, (
                    f"外部正史卷 {spec['start']}-{spec['end']} 尚未完整导入并验收；"
                    f"第 {chapter_no} 章已锁定，不能跳章继续。"
                ), spec
            if deep:
                errors = self.verify_external_range(spec)
                if errors:
                    return False, (
                        f"外部正史卷 {spec['start']}-{spec['end']} 完整性复核失败：{errors[0]}；"
                        f"第 {chapter_no} 章继续生成已阻止。"
                    ), spec
        return True, "", None

    def external_canon_snapshot(self):
        with self.external_import_lock:
            out = dict(self.external_import_status)
            out["errors"] = list(self.external_import_status.get("errors") or [])
        if out.get("running") and out.get("started_at"):
            out["elapsed_seconds"] = round(max(0.0, time.time() - float(out["started_at"])), 1)
        ranges = [self._external_range_file_status(spec) for spec in self._external_ranges()]
        if not out.get("running") and ranges:
            selected = next((row for row in ranges if row["status"] != "complete"), ranges[-1])
            out["item_total"] = int(selected["total"])
            out["item_done"] = int(selected["imported"])
            if selected["status"] == "complete":
                try:
                    spec = next(row for row in self._external_ranges() if range_key(row) == selected["key"])
                    manifest = self._load_external_manifest(spec) or {}
                    out["boundary_status"] = str((manifest.get("boundary_validation") or {}).get("status") or "pending")
                    out["exit_state_status"] = "passed" if (manifest.get("exit_state") or {}).get("sha256") else "pending"
                    if out.get("stage") == "空闲":
                        out["stage"] = "完成"
                        out["stage_label"] = "整卷完整性、入口接缝和退出状态已通过"
                except Exception:
                    pass
        next_chapter = int(self.load_state().get("next_chapter", 1) or 1)
        allowed, message, blocked = self.external_generation_gate(next_chapter, deep=False)
        out.update({
            "ranges": ranges, "next_chapter": next_chapter,
            "generation_blocked": not allowed, "gate_message": message,
            "blocked_range": range_key(blocked) if blocked else "",
        })
        total = int(out.get("item_total") or 0)
        done = int(out.get("item_done") or 0)
        out["progress_pct"] = round(done * 100.0 / total, 2) if total else 0.0
        return out

    def _external_import_predecessor_check(self, spec):
        previous = int(spec["start"]) - 1
        if previous < 1:
            return
        path = self.root / "chapters" / f"{previous:04d}.md"
        row = self.db.get_chapter(previous)
        if not path.exists() or not row or not str(row.get("final") or "").strip():
            raise ExternalCanonError(
                f"必须先正常生成并提交第 {previous} 章，才能导入 {spec['start']}-{spec['end']}"
            )
        if sha256_file(path) != sha256_text(row.get("final") or ""):
            raise ExternalCanonError(f"第 {previous} 章正文文件与数据库 Canon 不一致")
        handoff_path = self.root / "handoffs" / f"{previous:04d}.json"
        try:
            handoff = json.loads(handoff_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise ExternalCanonError(f"第 {previous} 章缺少有效 Handoff") from exc
        if handoff.get("status") != "complete":
            raise ExternalCanonError(f"第 {previous} 章 Handoff 未完成，不能建立外部正史入口接缝")
        last_db = self.db.last_canon_chapter()
        if last_db > int(spec["end"]):
            raise ExternalCanonError(
                f"数据库已存在第 {int(spec['end'])+1} 章或更后 Canon；必须先按既有回档流程移除后续，"
                "不能在其下方补插外部正史"
            )
        chapters_dir = self.root / "chapters"
        if chapters_dir.exists():
            for path in chapters_dir.glob("*.md"):
                match = re.fullmatch(r"(\d{4})\.md", path.name, re.I)
                if match and int(match.group(1)) > int(spec["end"]):
                    raise ExternalCanonError(
                        f"已存在后续正文 {path.name}；外部卷验收前不能保留第 {int(spec['end'])+1} 章以后正文"
                    )

    def start_external_canon_import(self, payload, range_start=None):
        ranges = self._external_ranges()
        if not ranges:
            return False, "outline/config 中没有外部正史范围"
        if range_start is None:
            incomplete = [spec for spec in ranges if self._external_range_file_status(spec)["status"] != "complete"]
            spec = incomplete[0] if incomplete else ranges[0]
        else:
            spec = next((row for row in ranges if int(row["start"]) == int(range_start)), None)
        if not spec:
            return False, f"未配置起始章为 {range_start} 的外部正史范围"
        cfg = self.config_loader().get("external_canon", {}) or {}
        package = validate_chapter_package(
            payload, spec,
            max_zip_bytes=int(cfg.get("max_zip_bytes", 128 * 1024 * 1024) or 128 * 1024 * 1024),
            max_chapter_bytes=int(cfg.get("max_chapter_bytes", 2 * 1024 * 1024) or 2 * 1024 * 1024),
            max_total_bytes=int(cfg.get("max_total_bytes", 256 * 1024 * 1024) or 256 * 1024 * 1024),
        )
        with self.external_import_lock, self.lock:
            if self.external_import_status.get("running"):
                return False, "外部正史导入已在运行"
            if self.status.get("running"):
                return False, "Canon 正在运行；请先停止再导入外部正史"
            if self.audit_snapshot().get("running") or self.repair_snapshot().get("running") or self.reader_snapshot().get("running"):
                return False, "审计、审计修复或读者版任务正在运行；请先停止再导入"
            if self.dlc_snapshot().get("running"):
                return False, "DLC 正在运行；请先停止再导入外部正史"
            self._external_import_predecessor_check(spec)
            manifest_path = self._external_manifest_path(spec)
            old = self._load_external_manifest(spec)
            if old and old.get("status") == "complete":
                if old.get("package_sha256") == package["package_sha256"] and not self.verify_external_range(spec):
                    return False, f"外部正史卷 {spec['start']}-{spec['end']} 已完整导入并验收"
                return False, "该外部正史范围已有完成清单；新包不能静默覆盖"
            if old and old.get("package_sha256") and old.get("package_sha256") != package["package_sha256"]:
                return False, "已有未完成导入使用了不同 ZIP；为避免混合版本，不能直接续传"

            completed = {}
            old_entries = (old or {}).get("entries") or {}
            db_rows = {int(row["chapter_no"]): row for row in self.db.canon_range_rows(spec["start"], spec["end"])}
            for chapter, expected_hash in package["chapter_hashes"].items():
                path = self.root / "chapters" / f"{chapter:04d}.md"
                row = db_rows.get(chapter)
                if not path.exists() and not row:
                    continue
                summary_path = self.root / "summaries" / f"{chapter:04d}.md"
                handoff_path = self.root / "handoffs" / f"{chapter:04d}.json"
                valid_core = (
                    path.exists() and row
                    and str(row.get("source") or "") == "external_canon"
                    and sha256_file(path) == expected_hash
                    and sha256_text(row.get("final") or "") == expected_hash
                    and summary_path.exists() and handoff_path.exists()
                )
                try:
                    resume_handoff = json.loads(handoff_path.read_text(encoding="utf-8")) if valid_core else {}
                    valid_core = valid_core and resume_handoff.get("status") == "complete"
                except Exception:
                    valid_core = False
                old_entry = old_entries.get(str(chapter))
                if valid_core and isinstance(old_entry, dict) and str(old_entry.get("canon_sha256") or "") == expected_hash:
                    completed[str(chapter)] = dict(old_entry)
                    continue
                if valid_core:
                    # A process can exit after the per-chapter Canon journal is
                    # fully published but before the range manifest is updated.
                    completed[str(chapter)] = {
                        "chapter_no": chapter, "source": "external_canon",
                        "canon_sha256": expected_hash,
                        "summary_sha256": sha256_file(summary_path),
                        "handoff_sha256": sha256_file(handoff_path),
                        "metadata_source": "recovered_after_manifest_interruption",
                        "committed_at": str(row.get("updated_at") or datetime.now().isoformat(timespec="seconds")),
                    }
                    continue
                if not valid_core:
                    return False, f"第 {chapter} 章已存在且不属于本次可恢复的同版本外部导入"

            stage_dir = self._external_range_root(spec) / "source"
            stage_dir.mkdir(parents=True, exist_ok=True)
            for chapter, text in package["texts"].items():
                target = stage_dir / f"{chapter:04d}.md"
                temp = target.with_name(target.name + ".pending")
                temp.write_text(text, encoding="utf-8", newline="\n")
                os.replace(temp, target)
            metadata_dir = stage_dir / "metadata"
            for key, value in (package.get("metadata") or {}).items():
                name = "exit_state.json" if key == "exit_state" else f"{int(key):04d}.json"
                atomic_write_json(metadata_dir / name, value)
            manifest = new_manifest(spec, package, completed_entries=completed)
            manifest["resumed_at"] = datetime.now().isoformat(timespec="seconds") if completed else None
            atomic_write_json(manifest_path, manifest)
            total = int(spec["end"]) - int(spec["start"]) + 1
            self.external_import_status = {
                "running": True, "range_key": range_key(spec),
                "start": int(spec["start"]), "end": int(spec["end"]),
                "label": spec["label"], "stage": "准备完成",
                "stage_label": "等待整理外部正文", "started_at": time.time(),
                "elapsed_seconds": 0.0, "chapter": None,
                "item_total": total, "item_done": len(completed),
                "imported": 0, "skipped": len(completed), "last_error": "",
                "package_sha256": package["package_sha256"],
                "range_digest": package["range_digest"],
                "manifest_file": str(manifest_path.relative_to(self.root)).replace("\\", "/"),
                "boundary_status": "pending", "exit_state_status": "pending", "errors": [],
            }
        self.stop_event.clear()
        self.reload_clients()
        self.external_import_thread = threading.Thread(
            target=self._run_external_canon_import, args=(dict(spec),), daemon=True
        )
        self.external_import_thread.start()
        self.log(f"外部正史导入已启动：第 {spec['start']}-{spec['end']} 章；全部文件已通过 ZIP 完整性预检。")
        return True, f"已开始导入第 {spec['start']}-{spec['end']} 章外部正史"

    def request_stop_external_canon_import(self):
        with self.external_import_lock:
            if not self.external_import_status.get("running"):
                return False, "外部正史导入当前未运行"
            self.external_import_status["stage_label"] = "正在停止；已提交章节保持原子一致"
        self.stop_event.set()
        try:
            if self.router:
                self.router.cancel_current()
        except Exception:
            pass
        self.log("收到外部正史导入停止请求；范围不会解锁，已完成章节可用同一 ZIP 恢复。")
        return True, "已发送停止请求"

    def _external_import_review_entry_seam(self, spec, text):
        chapter = int(spec["start"])
        previous = chapter - 1
        previous_text = (self.root / "chapters" / f"{previous:04d}.md").read_text(encoding="utf-8")
        previous_handoff = json.loads((self.root / "handoffs" / f"{previous:04d}.json").read_text(encoding="utf-8"))
        local = deterministic_boundary_findings(
            previous_text, text, previous_handoff,
            current_task=self.chapter_task_card(chapter),
            next_task=self.future_task_boundary(chapter),
        )
        blocking = [row for row in local if str(row.get("severity") or "").upper() == "MAJOR"]
        if blocking:
            raise ExternalCanonError(
                f"第 {previous}-{chapter} 章入口接缝本地检查失败：{blocking[0]['message']}"
            )
        plan = (
            f"外部正史第 {chapter} 章导入验收。正文已经由外部作者定稿；"
            "只核对上一章出口、当前章大纲边界和连续性，不把外部来源当作 DLC。"
        )
        review = self.review_chapter(
            chapter, plan, text, self.chapter_task_card(chapter),
            deep=False, final_gate=True,
        )
        if str(review.get("severity", "PASS")).upper() != "PASS" or review.get("needs_revision"):
            evidence = review.get("continuity") or review.get("revision_instructions") or []
            detail = str(evidence[0]) if isinstance(evidence, list) and evidence else "模型边界 Review 未通过"
            raise ExternalCanonError(f"第 {previous}-{chapter} 章入口接缝 Review 未通过：{detail}")
        return plan, review, local

    def _write_external_exit_state(self, spec, manifest):
        end = int(spec["end"])
        chapter_path = self.root / "chapters" / f"{end:04d}.md"
        handoff_path = self.root / "handoffs" / f"{end:04d}.json"
        handoff = json.loads(handoff_path.read_text(encoding="utf-8"))
        if handoff.get("status") != "complete":
            raise ExternalCanonError(f"第 {end} 章 Handoff 未完成，不能建立 Canon 退出状态")
        current_state = self.db.state_as_of(end)
        state_sha = self._external_state_sha(current_state)
        supplied_path = self._external_range_root(spec) / "source" / "metadata" / "exit_state.json"
        if supplied_path.exists():
            supplied = json.loads(supplied_path.read_text(encoding="utf-8"))
            supplied_state = supplied.get("current_state")
            if supplied_state is not None:
                supplied_sha = self._external_state_sha(supplied_state)
                if supplied_sha != state_sha:
                    raise ExternalCanonError(
                        f"外部提供的第 {end} 章 current_state 与已提交 Memory 状态投影不一致"
                    )
        exit_state = {
            "schema_version": 1, "kind": "external_canon_exit_state",
            "source": "external_canon", "range": {"start": spec["start"], "end": spec["end"]},
            "chapter_no": end, "canon_sha256": sha256_file(chapter_path),
            "source_tail": handoff.get("source_tail") or "",
            "source_tail_sha256": hashlib.sha256(str(handoff.get("source_tail") or "").encode("utf-8")).hexdigest(),
            "handoff": handoff, "handoff_sha256": sha256_file(handoff_path),
            "current_state": current_state, "current_state_sha256": state_sha,
            "verified_at": datetime.now().isoformat(timespec="seconds"),
            "metadata_source": "validated_package_exit_state" if supplied_path.exists() else "novelagent_extracted",
        }
        path = self._external_range_root(spec) / "exit_state.json"
        atomic_write_json(path, exit_state)
        manifest["exit_state"] = {
            "file": str(path.relative_to(self.root)).replace("\\", "/"),
            "sha256": sha256_file(path), "chapter_no": end,
            "canon_sha256": exit_state["canon_sha256"],
            "handoff_sha256": exit_state["handoff_sha256"],
            "current_state_sha256": state_sha,
        }

    def _run_external_canon_import(self, spec):
        manifest_path = self._external_manifest_path(spec)
        try:
            manifest = self._load_external_manifest(spec)
            entries = manifest.setdefault("entries", {})
            source_dir = self._external_range_root(spec) / "source"
            first_text = canonical_chapter_text(
                (source_dir / f"{int(spec['start']):04d}.md").read_text(encoding="utf-8")
            )
            with self.external_import_lock:
                self.external_import_status.update({
                    "chapter": int(spec["start"]), "stage": "入口接缝验收",
                    "stage_label": f"检查第 {int(spec['start'])-1}-{int(spec['start'])} 章",
                })
            entry_plan, entry_review, entry_local = self._external_import_review_entry_seam(spec, first_text)
            with self.external_import_lock:
                self.external_import_status["boundary_status"] = "passed"
            for chapter in range(int(spec["start"]), int(spec["end"]) + 1):
                if self.stop_event.is_set():
                    raise ProviderCancelledError("外部正史导入已停止")
                key = str(chapter)
                if key in entries:
                    continue
                source_path = source_dir / f"{chapter:04d}.md"
                text = canonical_chapter_text(source_path.read_text(encoding="utf-8"))
                expected_hash = str((manifest.get("expected_chapter_hashes") or {}).get(key) or "")
                if sha256_text(text) != expected_hash:
                    raise ExternalCanonError(f"暂存的第 {chapter} 章正文哈希已变化")
                with self.external_import_lock:
                    self.external_import_status.update({
                        "chapter": chapter, "stage": "整理 Canon 元数据",
                        "stage_label": f"第 {chapter} 章 Summary / Memory / Handoff",
                    })
                plan = entry_plan if chapter == int(spec["start"]) else (
                    f"第 {chapter} 章为已定稿外部正史；NovelAgent 仅接管 Canon 元数据和后续连续性状态。"
                )
                review = entry_review if chapter == int(spec["start"]) else {
                    "severity": "PASS", "needs_revision": False,
                    "source": "external_canon_import",
                    "validation": "ZIP range/name/hash preflight; final body accepted as externally authored Canon",
                }
                metadata_dir = source_dir / "metadata"
                metadata_path = metadata_dir / f"{chapter:04d}.json"
                metadata_source = "novelagent_extracted"
                if metadata_path.exists():
                    raw_meta = json.loads(metadata_path.read_text(encoding="utf-8"))
                    source_tail = extract_source_tail(text, self._continuity_config()["source_tail_chars"])
                    handoff = normalize_handoff(
                        raw_meta.get("handoff"), chapter, source_tail,
                        self._continuity_config()["handoff_max_chars"],
                        require_structured=True,
                    )
                    summary = str(raw_meta.get("summary") or "").strip()
                    memories = list(raw_meta.get("memories") or [])
                    handoff_error = ""
                    metadata_source = "validated_package_metadata"
                    self.log(f"第 {chapter} 章使用正文哈希绑定且结构校验通过的外部 Canon 元数据。")
                else:
                    summary, memories, handoff, handoff_error = self.summarize_and_extract_memories(chapter, text)
                exit_metadata_path = metadata_dir / "exit_state.json"
                if chapter == int(spec["end"]) and exit_metadata_path.exists():
                    supplied_exit = json.loads(exit_metadata_path.read_text(encoding="utf-8"))
                    if isinstance(supplied_exit.get("handoff"), dict):
                        source_tail = extract_source_tail(text, self._continuity_config()["source_tail_chars"])
                        handoff = normalize_handoff(
                            supplied_exit["handoff"], chapter, source_tail,
                            self._continuity_config()["handoff_max_chars"],
                            require_structured=True,
                        )
                        handoff_error = ""
                        metadata_source += "+validated_exit_handoff"
                        self.log(f"第 {chapter} 章使用正文哈希绑定且结构校验通过的外部 Canon 退出 Handoff。")
                if (
                    handoff_error or handoff.get("status") != "complete"
                    or handoff.get("structured_complete") is not True
                    or not handoff.get("scene_signatures")
                ):
                    raise ExternalCanonError(
                        f"第 {chapter} 章 Handoff 提取未完成：{handoff_error or handoff.get('error') or 'unknown'}"
                    )
                self._commit_canon_bundle(
                    chapter, plan=plan, draft=text, final_review=review, final=text,
                    summary=summary, memories=memories, handoff=handoff,
                    generation_seconds=0.0, revision_seconds=0.0,
                    honor_stop=True, source="external_canon",
                )
                entries[key] = {
                    "chapter_no": chapter, "source": "external_canon",
                    "canon_sha256": sha256_file(self.root / "chapters" / f"{chapter:04d}.md"),
                    "summary_sha256": sha256_file(self.root / "summaries" / f"{chapter:04d}.md"),
                    "handoff_sha256": sha256_file(self.root / "handoffs" / f"{chapter:04d}.json"),
                    "metadata_source": metadata_source,
                    "committed_at": datetime.now().isoformat(timespec="seconds"),
                }
                manifest["updated_at"] = datetime.now().isoformat(timespec="seconds")
                manifest["last_error"] = ""
                atomic_write_json(manifest_path, manifest)
                with self.external_import_lock:
                    self.external_import_status["item_done"] = len(entries)
                    self.external_import_status["imported"] += 1

            self._stop_after_stage("外部正史逐章接管")
            manifest["boundary_validation"] = {
                "status": "passed", "previous_chapter": int(spec["start"]) - 1,
                "current_chapter": int(spec["start"]),
                "review_severity": str((entry_review or {}).get("severity") or "PASS").upper(),
                "local_findings": entry_local,
                "validated_at": datetime.now().isoformat(timespec="seconds"),
            }
            self._write_external_exit_state(spec, manifest)
            manifest["status"] = "complete"
            manifest["completed_at"] = datetime.now().isoformat(timespec="seconds")
            manifest["updated_at"] = manifest["completed_at"]
            atomic_write_json(manifest_path, manifest)
            errors = self.verify_external_range(spec)
            if errors:
                manifest["status"] = "invalid"
                manifest["last_error"] = errors[0]
                atomic_write_json(manifest_path, manifest)
                raise ExternalCanonError(f"整卷验收失败：{errors[0]}")
            with self.external_import_lock:
                self.external_import_status.update({
                    "stage": "完成", "stage_label": "整卷完整性、入口接缝和退出状态已通过",
                    "chapter": int(spec["end"]), "item_done": int(spec["end"]) - int(spec["start"]) + 1,
                    "boundary_status": "passed", "exit_state_status": "passed", "last_error": "",
                })
            self.log(
                f"外部正史卷第 {spec['start']}-{spec['end']} 章已完整导入普通 Canon；"
                f"入口接缝与第 {spec['end']} 章退出状态已通过，第 {int(spec['end'])+1} 章可以继续。"
            )
            self._emit("external_canon_finished", start=spec["start"], end=spec["end"])
        except ProviderCancelledError:
            try:
                manifest = self._load_external_manifest(spec) or {}
                manifest["status"] = "stopped"
                manifest["updated_at"] = datetime.now().isoformat(timespec="seconds")
                atomic_write_json(manifest_path, manifest)
            except Exception:
                pass
            with self.external_import_lock:
                self.external_import_status.update({
                    "stage": "已停止", "stage_label": "范围仍锁定；可用同一 ZIP 恢复",
                })
            self.log("外部正史导入已停止；未完成清单不会解锁后续章节。")
        except Exception as exc:
            try:
                manifest = self._load_external_manifest(spec) or {}
                manifest["status"] = "failed"
                manifest["last_error"] = str(exc)
                manifest["updated_at"] = datetime.now().isoformat(timespec="seconds")
                atomic_write_json(manifest_path, manifest)
            except Exception:
                pass
            with self.external_import_lock:
                errors = list(self.external_import_status.get("errors") or [])
                errors.append(str(exc))
                self.external_import_status.update({
                    "stage": "失败", "stage_label": "导入未完成，后续章节保持锁定",
                    "last_error": str(exc), "errors": errors[-20:],
                })
            self.log(f"外部正史导入失败：{exc}；范围完成门未解锁。")
            self._emit("error", text=f"外部正史导入失败：{exc}")
        finally:
            self._stage(None, "空闲", "")
            with self.external_import_lock:
                self.external_import_status["running"] = False
            self.stop_event.clear()

    def future_task_boundary(self, n, detailed=True):
        """Return the next task, or a non-spoiling guard for prose-producing stages."""
        if not detailed:
            return (
                "【下一章边界保护】下一章存在独立硬任务，但具体答案对当前正文阶段隐藏。"
                "本章只能达到当前章节任务卡规定的结束状态；不得主动解释尚未解决的问题，"
                "不得确认新的因果、身份、关系或能力结论，也不得替下一章完成兑现。"
            )
        limit = self._continuity_config()["future_boundary_max_chars"]
        next_card = self.chapter_task_card(int(n) + 1)
        next_outline = self.current_chapter_outline(int(n) + 1)
        text = f"【第{int(n)+1}章硬任务卡】\n{next_card}\n\n【第{int(n)+1}章大纲】\n{next_outline}".strip()
        return text[:limit] if text else "（未找到下一章明确任务；不得自行提前扩展未来剧情。）"

    def previous_boundary_context(self, n, include_future_details=True):
        """Load protected previous-Chapter handoff and deterministic Canon suffix."""
        n = int(n)
        if n > 1 and self._writing_quality_config()["canon_commit_verification"]:
            pending = sorted((self.root / "runtime" / "canon_transactions").glob("*.json"))
            if pending:
                raise CanonCommitError(
                    "存在未完成的 Canon 事务，已阻止读取上一章并生成下一章："
                    + "，".join(path.name for path in pending[:4])
                )
            previous = n - 1
            db_last = self.db.last_canon_chapter()
            previous_row = self.db.get_chapter(previous) or {}
            last_row = self.db.get_chapter(db_last) or {}
            errors = verify_canon_publish(
                self.root, previous, previous_row, db_last, last_db_row=last_row,
            )
            if errors:
                raise CanonCommitError(
                    f"第 {previous} 章 Canon 状态未完整同步，已阻止生成第 {n} 章："
                    + "；".join(errors[:8])
                )
        if n <= 1:
            result = {
                "status": "first_chapter", "chapter_no": None, "handoff": None,
                "source_tail": "", "canon_exit_state": None,
                "error": "", "future_boundary": self.future_task_boundary(n, include_future_details),
            }
        else:
            prev = n - 1
            cfg = self._continuity_config()
            chapter_path = self.root / "chapters" / f"{prev:04d}.md"
            canon = chapter_path.read_text(encoding="utf-8") if chapter_path.exists() else ""
            tail = extract_source_tail(canon, cfg["source_tail_chars"])
            path = self.root / "handoffs" / f"{prev:04d}.json"
            handoff = None
            error = ""
            if path.exists():
                try:
                    raw = json.loads(path.read_text(encoding="utf-8"))
                    handoff = normalize_handoff(raw, prev, tail, cfg["handoff_max_chars"])
                except Exception as exc:
                    error = f"handoff 文件无效：{exc}"
            else:
                error = "缺少上一章 handoff 文件"
            if handoff is None:
                handoff = degraded_handoff(prev, tail, error)
            if not include_future_details:
                # Handoff is generated with the detailed author boundary so
                # reviewers can validate the seam.  Prose-producing stages must
                # not receive that answer again through next_start or
                # future_boundaries; the real Canon tail and ongoing state are
                # sufficient to continue naturally.
                handoff = dict(handoff)
                handoff["next_start"] = (
                    "承接上一章末尾的时间、地点、人物状态和 ongoing_events；"
                    "不得重新执行 completed_events。"
                )
                handoff["future_boundaries"] = []
            canon_exit_state = None
            exit_spec = next((spec for spec in self._external_ranges() if int(spec["end"]) == prev), None)
            if exit_spec:
                try:
                    manifest = self._load_external_manifest(exit_spec) or {}
                    exit_meta = manifest.get("exit_state") or {}
                    exit_path = self._external_range_root(exit_spec) / "exit_state.json"
                    if manifest.get("status") != "complete" or not exit_path.exists():
                        raise ValueError("external Canon exit state is not complete")
                    if sha256_file(exit_path) != str(exit_meta.get("sha256") or ""):
                        raise ValueError("external Canon exit state hash mismatch")
                    canon_exit_state = json.loads(exit_path.read_text(encoding="utf-8"))
                except Exception as exc:
                    error = (error + "；" if error else "") + f"外部正史退出状态无效：{exc}"
            result = {
                "status": handoff.get("status", "degraded"), "chapter_no": prev,
                "handoff": handoff, "source_tail": tail,
                "canon_exit_state": canon_exit_state, "error": error,
                "future_boundary": self.future_task_boundary(n, include_future_details),
            }
        with self.lock:
            self.status["handoff_status"] = result["status"]
            self.status["handoff_chapter"] = result["chapter_no"]
            self.status["handoff_error"] = result["error"]
            self.status["handoff_tail_chars"] = len(result["source_tail"])
        if result["status"] not in {"complete", "first_chapter"}:
            self.log(f"连续性交接降级：{result['error']}；仍保留 {len(result['source_tail'])} 字上一章真实正文末尾。")
        return result

    @staticmethod
    def _boundary_prompt(boundary):
        if boundary.get("status") == "first_chapter":
            previous = "（第1章，无上一章。）"
            tail = "（无）"
        else:
            handoff = dict(boundary.get("handoff") or {})
            handoff.pop("source_tail", None)
            previous = json.dumps(handoff, ensure_ascii=False, indent=2)
            tail = boundary.get("source_tail") or "（上一章正文文件缺失）"
        exit_state = dict(boundary.get("canon_exit_state") or {})
        if exit_state:
            # The full current state is already injected by _stage_static_context.
            # Keep the independently verified exit identity protected without
            # duplicating a potentially large state projection.
            exit_state.pop("current_state", None)
            exit_state.pop("handoff", None)
            exit_state.pop("source_tail", None)
            exit_text = json.dumps(exit_state, ensure_ascii=False, indent=2)
        else:
            exit_text = "（上一章不是外部正史卷出口，或无独立退出状态。）"
        return f"""
【受保护连续性交接：普通裁剪不得删除】
{previous}

【受保护的上一章最终 Canon 正文末尾：程序直接截取】
{tail}

【受保护的上一章 Canon 退出状态：外部卷出口时校验正文/Handoff/状态哈希】
{exit_text}

【时间交接解释】
- handoff.end_time 只是上一章正文出现的最后明确时间点，不是要求下一章自动进入第二天的指令。
- handoff.scene_closed=true 只表示上一处具体场景已经收束，不表示当天生活、当前事件或相关关系已经结束；下一章可以在同一天、同一地点或紧接的动作上自然承接。
- 只有大纲、事件因果或人物行程确实需要时才推进日期，不要为了满足连续性、避免重开场景或凑完整章节而使用“第二天/第三天”。

【受保护的后续任务边界：本章不得提前消费】
{boundary.get('future_boundary') or '（无明确后续任务）'}
"""

    def _log_boundary_context(self, stage, boundary):
        handoff = dict(boundary.get("handoff") or {})
        handoff.pop("source_tail", None)
        self.log(
            f"{stage} 受保护连续性上下文：handoff={boundary.get('status')} "
            f"{len(json.dumps(handoff, ensure_ascii=False)) if handoff else 0}字；"
            f"上一章原文末尾={len(boundary.get('source_tail') or '')}字；"
            f"后续任务边界={len(boundary.get('future_boundary') or '')}字；"
            f"Canon退出状态={'已验证' if boundary.get('canon_exit_state') else '无'}；"
            "这些连续性字段不参与普通裁剪。"
        )

    def _recover_canon_transactions(self):
        """Finish durable Canon publishes interrupted after their prepare point."""
        tx_root = self.root / "runtime" / "canon_transactions"
        if not tx_root.exists():
            return
        for journal in sorted(tx_root.glob("*.json")):
            try:
                data = json.loads(journal.read_text(encoding="utf-8"))
                n = int(data["chapter_no"])
                payload = data["db_payload"]
                self.db.commit_canon(n, payload["fields"], payload.get("memories") or [])
                for item in data.get("files") or []:
                    temp = self.root / item["temp"]
                    target = self.root / item["target"]
                    if temp.exists():
                        target.parent.mkdir(parents=True, exist_ok=True)
                        os.replace(temp, target)
                    elif not target.exists() or hashlib.sha256(target.read_bytes()).hexdigest() != item["sha256"]:
                        raise FileNotFoundError(f"事务缺少正确的暂存/目标文件：{temp}")
                if self._writing_quality_config()["canon_commit_verification"]:
                    db_last = self.db.last_canon_chapter()
                    db_row = self.db.get_chapter(n) or {}
                    last_row = self.db.get_chapter(db_last) or {}
                    errors = verify_canon_publish(
                        self.root, n, db_row, db_last, last_db_row=last_row,
                        expected_files=data.get("files") or [],
                        expected_fields=payload.get("fields") or {},
                    )
                    if errors:
                        raise CanonCommitError("；".join(errors[:10]))
                journal.unlink(missing_ok=True)
                self.log(f"已恢复并完成第 {n} 章中断的 Canon 原子提交。")
            except Exception as exc:
                self.log(f"Canon 事务恢复失败（{journal.name}）：{exc}；生成将保持可见错误状态。")
                with self.lock:
                    self.status["handoff_status"] = "error"
                    self.status["handoff_error"] = f"未完成 Canon 事务：{journal.name}: {exc}"

    def _commit_canon_bundle(self, n, *, plan, draft, final_review, final,
                              summary, memories, handoff, generation_seconds,
                              revision_seconds, honor_stop=True, source=None):
        """Durably publish one version across files, state snapshots, and SQLite."""
        if honor_stop:
            self._stop_after_stage("Canon 提交准备")
        n = int(n)
        final = str(final or "").strip()
        summary = str(summary or "").strip()
        existing = self.db.get_chapter(n) or {}
        canon_source = str(source or existing.get("source") or "generated").strip() or "generated"
        handoff_text = json.dumps(handoff, ensure_ascii=False, indent=2)
        review_text = json.dumps(final_review, ensure_ascii=False, indent=2)
        last_before = self.db.last_canon_chapter()
        current_chapter = max(n, last_before)
        projected = self.db.project_replaced_chapter_state(n, memories, current_chapter)
        chapter_state = self.db.project_replaced_chapter_state(n, memories, n)
        generated_at = datetime.now().isoformat(timespec="seconds")
        projected["generated_at"] = generated_at
        chapter_state["generated_at"] = generated_at
        state_file = self.load_state()
        # SQLite Canon is authoritative. A chapter-1 rewrite must be able to
        # replace a stale pre-reset next_chapter=81 instead of preserving it.
        state_file["next_chapter"] = current_chapter + 1
        state_file["last_canon_chapter"] = current_chapter
        if n >= last_before:
            state_file["last_canon_hash"] = hashlib.sha256(final.encode("utf-8")).hexdigest()
        file_payloads = {
            f"chapters/{n:04d}.md": final + "\n",
            f"summaries/{n:04d}.md": summary + "\n",
            f"handoffs/{n:04d}.json": handoff_text + "\n",
            f"handoffs/{n:04d}.tail.txt": str(handoff.get("source_tail") or "").strip() + "\n",
            f"reviews/{n:04d}.json": review_text + "\n",
            "current_state.json": json.dumps(projected, ensure_ascii=False, indent=2) + "\n",
            "state.json": json.dumps(state_file, ensure_ascii=False, indent=2) + "\n",
        }
        for snap_n in range(n, current_chapter + 1):
            snap_state = chapter_state if snap_n == n else self.db.project_replaced_chapter_state(n, memories, snap_n)
            snap_state["generated_at"] = generated_at
            snapshot = {
                "chapter": snap_n, "next_chapter": snap_n + 1,
                "generated_at": generated_at, "current_state": snap_state,
                "source_rebuilt_from_chapter": n,
                "source": canon_source if snap_n == n else "reprojected",
                "handoff_status": handoff.get("status", "unknown") if snap_n == n else "unchanged",
            }
            file_payloads[f"runtime/state_snapshots/{snap_n:04d}.json"] = json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n"
        usage = self.db.usage_stats(n)
        db_fields = {
            "source": canon_source,
            "plan": plan, "draft": draft, "review": review_text, "final": final,
            "summary": summary, "handoff": handoff_text, "chars": len(final),
            "generation_seconds": generation_seconds,
            "revision_seconds": revision_seconds,
            "model_tokens": usage.get("completion_tokens", 0),
        }
        txid = f"{n:04d}_{hashlib.sha256(final.encode('utf-8')).hexdigest()[:12]}"
        tx_root = self.root / "runtime" / "canon_transactions"
        tx_root.mkdir(parents=True, exist_ok=True)
        files = []
        for rel, content in file_payloads.items():
            target = self.root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            temp = target.with_name(target.name + f".{txid}.pending")
            temp.write_text(content, encoding="utf-8", newline="\n")
            files.append({
                "target": str(target.relative_to(self.root)).replace("\\", "/"),
                "temp": str(temp.relative_to(self.root)).replace("\\", "/"),
                "sha256": hashlib.sha256(temp.read_bytes()).hexdigest(),
            })
        journal = tx_root / f"{txid}.json"
        transaction = {
            "schema_version": 1, "chapter_no": n, "phase": "prepared",
            "created_at": generated_at, "source": canon_source, "files": files,
            "db_payload": {"fields": db_fields, "memories": memories},
        }
        journal.write_text(json.dumps(transaction, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            memory_count = self.db.commit_canon(n, db_fields, memories)
            transaction["phase"] = "db_committed"
            journal.write_text(json.dumps(transaction, ensure_ascii=False, indent=2), encoding="utf-8")
            for item in files:
                temp = self.root / item["temp"]
                target = self.root / item["target"]
                if hashlib.sha256(temp.read_bytes()).hexdigest() != item["sha256"]:
                    raise CanonCommitError(f"暂存文件校验失败：{item['temp']}")
                os.replace(temp, target)
            if self._writing_quality_config()["canon_commit_verification"]:
                db_last = self.db.last_canon_chapter()
                db_row = self.db.get_chapter(n) or {}
                last_row = self.db.get_chapter(db_last) or {}
                errors = verify_canon_publish(
                    self.root, n, db_row, db_last, last_db_row=last_row,
                    expected_files=files, expected_fields=db_fields,
                )
                if errors:
                    raise CanonCommitError(
                        f"第 {n} 章 Canon 发布后校验失败：" + "；".join(errors[:10])
                    )
            journal.unlink(missing_ok=True)
            with self.lock:
                self.status["handoff_status"] = handoff.get("status", "unknown")
                self.status["handoff_chapter"] = n
                self.status["handoff_error"] = handoff.get("error", "")
                self.status["handoff_tail_chars"] = int(handoff.get("source_tail_chars", 0) or 0)
            return memory_count
        except Exception as exc:
            # The durable journal and pending files allow exact completion on restart.
            raise CanonCommitError(f"第 {n} 章 Canon 提交未完成，已保留恢复事务：{journal}: {exc}") from exc

    def load_state(self):
        p = self.root / "state.json"
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
        cfg = self.config_loader()
        return {"next_chapter": cfg["generation"].get("start_chapter", 1)}

    def save_state(self, state):
        # Drop the obsolete request-based window if an older state file still
        # contains it. Historical decisions now come from llm_usage by chapter.
        state.pop("plan_request_history", None)
        (self.root / "state.json").write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

    def _chapter_cost_guard_policy(self):
        cfg = self.config_loader().get("cost_control", {})
        mode = str(cfg.get("chapter_cost_guard_mode", "afp") or "afp").strip().lower()
        if mode not in {"afp", "cny", "unlimited"}:
            mode = "afp"
        size = max(1, int(cfg.get("chapter_cost_guard_window_chapters", 10) or 10))
        confirm_at = max(1, min(size, int(cfg.get("chapter_cost_guard_confirm_at_chapters", 6) or 6)))
        key = "chapter_cost_guard_cny_limit" if mode == "cny" else "chapter_cost_guard_afp_limit"
        default = 5.0 if mode == "cny" else 20.0
        limit = max(0.0, float(cfg.get(key, default) or 0.0))
        return mode, size, confirm_at, limit

    def _chapter_cost_guard_usage(self, chapter):
        """Count preceding completed chapters whose actual total spend exceeded the limit."""
        mode, size, confirm_at, limit = self._chapter_cost_guard_policy()
        chapter = max(1, int(chapter or 1))
        result = {
            "mode": mode, "window_size": size, "confirm_at": confirm_at,
            "limit": limit, "over_limit": 0, "checked": 0, "chapters": [],
        }
        if mode == "unlimited":
            return result
        db_path = self.root / "novel_memory.sqlite3"
        if not db_path.exists():
            return result
        con = None
        try:
            con = sqlite3.connect(db_path, timeout=5)
            with con:
                con.row_factory = sqlite3.Row
                completed = con.execute(
                    """SELECT chapter_no FROM chapters
                       WHERE chapter_no < ? AND final IS NOT NULL AND length(final) > 0
                       ORDER BY chapter_no DESC LIMIT ?""",
                    (chapter, size),
                ).fetchall()
                chapter_nos = sorted(int(r["chapter_no"]) for r in completed)
                result["checked"] = len(chapter_nos)
                result["chapters"] = chapter_nos
                if not chapter_nos:
                    return result
                marks = ",".join("?" for _ in chapter_nos)
                has_meta = bool(con.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='llm_billing_meta'"
                ).fetchone())
                meta_join = "LEFT JOIN llm_billing_meta m ON m.request_id=u.request_id" if has_meta else ""
                meta_cols = (
                    ", m.api_source, m.estimated_afp, m.estimated_cost_cny"
                    if has_meta else
                    ", '' AS api_source, NULL AS estimated_afp, NULL AS estimated_cost_cny"
                )
                rows = con.execute(
                    f"""SELECT u.chapter_no,u.provider,u.model,u.prompt_tokens,u.cache_hit_tokens,
                               u.cache_miss_tokens,u.completion_tokens,u.cost_cny {meta_cols}
                        FROM llm_usage u {meta_join}
                        WHERE u.chapter_no IN ({marks}) ORDER BY u.id""",
                    chapter_nos,
                ).fetchall()
                totals = {n: {"afp": 0.0, "cny": 0.0} for n in chapter_nos}
                for row in rows:
                    n = int(row["chapter_no"] or 0)
                    src = str(row["api_source"] or "").strip().lower()
                    provider = str(row["provider"] or "").strip().lower()
                    stored_cny = float(row["cost_cny"] or 0.0)
                    if not src:
                        if provider in {"volcengine_agent_plan", "volcengine", "ark_agent_plan"}:
                            src = "volcengine_agent_plan"
                        elif stored_cny > 0:
                            src = "official"
                        elif provider == "deepseek":
                            src = "volcengine_agent_plan"
                    if src == "official":
                        meta_cny = row["estimated_cost_cny"]
                        totals[n]["cny"] += float(
                            meta_cny if meta_cny is not None and float(meta_cny) > 0 else stored_cny
                        )
                    elif src == "volcengine_agent_plan":
                        meta_afp = row["estimated_afp"]
                        if meta_afp is None:
                            usage = {
                                "prompt_tokens": int(row["prompt_tokens"] or 0),
                                "prompt_cache_hit_tokens": int(row["cache_hit_tokens"] or 0),
                                "prompt_cache_miss_tokens": int(row["cache_miss_tokens"] or 0),
                                "completion_tokens": int(row["completion_tokens"] or 0),
                            }
                            meta_afp = calculate_volcengine_afp(str(row["model"] or ""), usage) or 0.0
                        totals[n]["afp"] += float(meta_afp)
                result["over_limit"] = sum(1 for n in chapter_nos if totals[n][mode] > limit)
                result["totals"] = totals
        except (sqlite3.Error, OSError, TypeError, ValueError):
            return result
        finally:
            if con is not None:
                con.close()
        return result

    def recent_summaries(self, n, count):
        parts = []
        for x in range(max(1, n - int(count)), n):
            p = self.root / "summaries" / f"{x:04d}.md"
            if p.exists():
                parts.append(f"## 第{x}章摘要\n{p.read_text(encoding='utf-8')}")
        return "\n\n".join(parts) if parts else "（暂无前章摘要）"

    def recent_plan_context(self, n, count=4, max_chars=10000):
        """Return compact excerpts of already generated Plans for structure checks."""
        parts = []
        for chapter_no in range(max(1, int(n) - int(count)), int(n)):
            path = self.root / "plans" / f"{chapter_no:04d}.md"
            if not path.exists():
                continue
            try:
                text = path.read_text(encoding="utf-8").strip()
            except OSError:
                continue
            if not text:
                continue
            if len(text) > 2600:
                text = text[:1500].rstrip() + "\n[…中段省略…]\n" + text[-1000:].lstrip()
            parts.append(f"## 第{chapter_no}章已生成 Plan\n{text}")
        rendered = "\n\n".join(parts)
        if len(rendered) <= int(max_chars):
            return rendered or "（暂无已生成的前章 Plan）"
        return rendered[-int(max_chars):]

    def _outline_source_text(self, n=None):
        """Return the main outline or a configured chapter-range override.

        Range overrides let a long, domain-heavy volume keep its detailed
        outline in a dedicated file while remaining part of normal Canon
        generation. They replace outline context only; they never transfer
        chapter ownership or enable external manuscript import.
        """
        if n is None:
            return self.read_story("outline.md")

        rows = self.config_loader().get("context", {}).get("outline_range_overrides", []) or []
        matches = []
        for raw in rows:
            if not isinstance(raw, dict):
                raise ValueError("context.outline_range_overrides entries must be objects")
            try:
                start = int(raw.get("start"))
                end = int(raw.get("end"))
            except (TypeError, ValueError) as exc:
                raise ValueError("outline range override start/end must be integers") from exc
            if start < 1 or end < start:
                raise ValueError(f"invalid outline range override: {start}-{end}")
            if start <= int(n) <= end:
                matches.append((end - start, start, end, raw))

        if not matches:
            return self.read_story("outline.md")

        matches.sort(key=lambda item: (item[0], item[1], item[2]))
        raw = matches[0][3]
        relative = str(raw.get("path") or "").strip()
        if not relative:
            raise ValueError("outline range override path is empty")
        source = (self.root / relative).resolve()
        try:
            source.relative_to(self.root.resolve())
        except ValueError as exc:
            raise ValueError("outline range override path must stay inside the project") from exc
        if not source.is_file():
            raise FileNotFoundError(f"区间详细大纲不存在：{relative}")
        text = source.read_text(encoding="utf-8")
        if not text.strip():
            raise ValueError(f"区间详细大纲为空：{relative}")
        return text

    def _outline_blocks(self, n=None):
        """Parse outline headings that may describe one chapter or a chapter range.

        Supported examples: 第4章, 4章, 第1-5章, 1—5章, 第1至5章, 第1到第5章.
        Returns (raw_text, global_part, blocks), where each block has start/end/text.
        """
        text = self._outline_source_text(n)
        if not text.strip():
            return text, "", []
        # Only Markdown headings define outline blocks.  Ordinary prose often
        # starts with phrases such as “第19章事件进一步失控”; treating those as
        # headings silently cuts the real chapter task in half.  A single-chapter
        # heading must also have a visible separator after “章” (space/colon/etc.)
        # or end there.  Range headings keep the legacy “第76—200章剧情规划” form.
        pat = re.compile(
            r"(?m)^[ \t]{0,3}#{1,6}[ \t]+(?:第[ \t]*)?(\d+)[ \t]*"
            r"(?:(?:[-—–~～]|至|到)[ \t]*(?:第[ \t]*)?(\d+)[ \t]*)?章([^\n]*)"
        )
        matches = []
        for match in pat.finditer(text):
            suffix = match.group(3) or ""
            # “### 第20章结尾钩子” is a subsection inside chapter 20, not a
            # second chapter-20 task.  Existing range headings such as
            # “# 第76—200章剧情规划” remain compatible.
            if match.group(2) is None and suffix and not re.match(
                r"^[ \t:：—–\-（(【\[]", suffix
            ):
                continue
            matches.append(match)
        if not matches:
            return text, "", []
        global_part = text[:matches[0].start()].strip()
        blocks = []
        for i, m in enumerate(matches):
            start_ch = int(m.group(1))
            end_ch = int(m.group(2) or start_ch)
            if end_ch < start_ch:
                start_ch, end_ch = end_ch, start_ch
            block_end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            blocks.append({
                "start": start_ch,
                "end": end_ch,
                "text": text[m.start():block_end].strip(),
            })
        return text, global_part, blocks

    def outline_context(self, n):
        """Inject global/volume/nearby outline, supporting chapter-range headings."""
        text, global_part, blocks = self._outline_blocks(n)
        if not text.strip():
            return "（暂无大纲）"
        cfg = self.config_loader().get("context", {})
        neighbors = int(cfg.get("outline_neighbor_chapters", 3))
        if not blocks:
            limit = int(cfg.get("outline_legacy_max_chars", 24000))
            if len(text) <= limit:
                return text
            return text[: int(limit * 0.65)] + "\n\n[...中间远期大纲已省略...]\n\n" + text[-int(limit * 0.35):]

        lo = max(1, int(n) - neighbors)
        hi = int(n) + neighbors
        picked = []
        if global_part:
            picked.append("【总纲/卷级说明】\n" + global_part)

        selected = [b for b in blocks if b["end"] >= lo and b["start"] <= hi]
        if not selected:
            def distance(b):
                if int(n) < b["start"]:
                    return b["start"] - int(n)
                if int(n) > b["end"]:
                    return int(n) - b["end"]
                return 0
            selected = sorted(blocks, key=distance)[:max(3, neighbors * 2 + 1)]

        # Each range block appears once even if it covers many nearby chapters.
        for b in selected:
            picked.append(b["text"])
        return "\n\n".join(picked)

    def current_chapter_outline(self, n):
        """Return the outline block whose single chapter/range contains chapter n."""
        text, _, blocks = self._outline_blocks(n)
        if not text.strip():
            return "（暂无大纲）"
        if not blocks:
            return self.outline_context(n)
        matched = [b for b in blocks if b["start"] <= int(n) <= b["end"]]
        if matched:
            # Broad volume/stage ranges can overlap nested per-chapter ranges.
            # Prefer the narrowest match so a chapter never receives an entire
            # 100-chapter stage block as its hard task/context.
            matched.sort(key=lambda b: (b["end"] - b["start"], b["start"]))
            return matched[0]["text"]
        return self.outline_context(n)

    def plan_stage_outline_context(self, n, include_future_details=False):
        """Return a bounded multi-chapter view used only by Plan/Review.

        Draft and Revision deliberately do not receive this text.  They execute
        the current task card and the Plan's non-spoiling handoff instead of
        seeing concrete future answers.  Plan and Review may opt into full
        nearby blocks for stage sequencing and validation; Draft/Revision keep
        the default non-spoiling view.
        """
        n = int(n)
        cfg = self.config_loader().get("context", {})
        lookbehind = max(0, min(3, int(cfg.get("plan_stage_lookbehind_chapters", 3))))
        lookahead = max(1, min(8, int(cfg.get("plan_stage_lookahead_chapters", 8))))
        max_chars = max(4000, min(20000, int(cfg.get("plan_stage_outline_max_chars", 18000))))
        text, _, blocks = self._outline_blocks(n)
        if not text.strip() or not blocks:
            return self.current_chapter_outline(n)[:max_chars]

        lo = max(1, n - lookbehind)
        hi = n + lookahead
        selected = [b for b in blocks if b["end"] >= lo and b["start"] <= hi]
        if not selected:
            return self.current_chapter_outline(n)[:max_chars]

        rendered_blocks = []
        for block in selected:
            lines = block["text"].splitlines()
            is_current = block["start"] <= n <= block["end"]
            if include_future_details or is_current or block["end"] < n:
                block_text = block["text"]
            else:
                heading = lines[0].strip() if lines else f"第{block['start']}章"
                block_text = (
                    f"{heading}\n"
                    "- （后续章节方向仅供阶段排序；具体任务由该章单独规划。）"
                )
            rendered_blocks.append({**block, "text": block_text})

        rendered = "\n\n".join(b["text"] for b in rendered_blocks)
        if len(rendered) <= max_chars:
            return rendered

        # Preserve the current task and the nearest future blocks before distant
        # context when unusually verbose chapter outlines exceed the cap.
        ranked = sorted(
            rendered_blocks,
            key=lambda b: (
                0 if b["start"] <= n <= b["end"] else 1,
                min(abs(n - b["start"]), abs(n - b["end"])),
                b["start"],
            ),
        )
        kept = []
        used = 0
        for block in ranked:
            part = block["text"]
            remaining = max_chars - used
            if remaining <= 0:
                break
            if len(part) <= remaining:
                kept.append(block)
                used += len(part) + 2
            elif not kept:
                kept.append({**block, "text": part[:remaining] + "\n[...本章大纲后文因阶段视野上限省略...]"})
                break
        kept.sort(key=lambda b: (b["start"], b["end"]))
        return "\n\n".join(b["text"] for b in kept)

    @staticmethod
    def _default_stage_contract(chapter, status="fallback", error=""):
        """Return a generic contract that is safe for any story section."""
        return {
            "schema_version": 2,
            "chapter_no": int(chapter),
            "status": status,
            "relation": "uncertain",
            "confidence": "LOW",
            "arc_label": "",
            "entry_state": "依据上一章受保护交接和当前章节任务承接；不假定新的时间或地点。",
            "chapter_change": "只推进当前章节大纲明确要求的最小必要变化。",
            "cut_point": "在当前事件、关系或决定的自然停点收束，不补完整作息。",
            "carry_out": "保留尚未完成的事件、关系或问题，交给下一章自然承接。",
            "time_span": "按事件需要决定；不默认一章一天。",
            "continuation_mode": "unknown",
            "sequence_role": "continue",
            "next_entry_hint": "",
            "no_routine_shell": True,
            "must_preserve": [],
            "must_not_advance": ["不得提前消费后续章节的具体结果、秘密或兑现"],
            "allowed_expansion": ["优先扩写与当前事件或人物关系有关的互动和现实阻力"],
            "reason": "阶段合同不可用；以当前章节任务卡和上一章交接为准。",
            "error": str(error or "")[:800],
        }

    @staticmethod
    def _normalize_stage_contract(value, chapter, window_start, window_end, source_digest=""):
        """Normalize one model-produced rolling contract into bounded chapter entries."""
        if not isinstance(value, dict):
            fallback = NovelAgent._default_stage_contract(chapter, error="阶段合同未返回 JSON 对象")
            fallback.update({
                "window_start": int(window_start), "window_end": int(window_end),
                "source_digest": source_digest,
            })
            return {"schema_version": 2, "status": "fallback", "window_start": int(window_start),
                    "window_end": int(window_end), "source_digest": source_digest,
                    "chapters": [fallback]}

        relation_aliases = {
            "same_event": "same_event", "same event": "same_event", "连续事件": "same_event", "同一事件": "same_event",
            "same_arc": "same_arc", "same arc": "same_arc", "连续阶段": "same_arc", "同一阶段": "same_arc", "同一主线": "same_arc",
            "independent": "independent", "独立": "independent", "非连续": "independent",
            "uncertain": "uncertain", "不确定": "uncertain", "无法判断": "uncertain",
        }
        confidence_aliases = {"high": "HIGH", "高": "HIGH", "medium": "MEDIUM", "中": "MEDIUM", "low": "LOW", "低": "LOW"}
        raw_entries = value.get("chapters") or value.get("chapter_contracts") or []
        if isinstance(raw_entries, dict):
            raw_entries = [dict(item or {}, chapter_no=key) for key, item in raw_entries.items() if isinstance(item, dict)]
        if not isinstance(raw_entries, list):
            raw_entries = []

        def short(item, name, limit, default=""):
            text = str(item.get(name) or "").strip()
            return (text or default)[:limit]

        def items(item, name, limit=8, item_limit=260):
            raw = item.get(name)
            if not isinstance(raw, list):
                return []
            out = []
            for part in raw:
                text = str(part or "").strip()
                if text and text not in out:
                    out.append(text[:item_limit])
                if len(out) >= limit:
                    break
            return out

        def flag(item, name, default=True):
            value = item.get(name, default)
            if isinstance(value, str):
                return value.strip().lower() not in {"false", "0", "no", "否"}
            return bool(value)

        chapters = []
        for item in raw_entries:
            try:
                chapter_no = int(item.get("chapter_no", item.get("chapter")))
            except (TypeError, ValueError):
                continue
            if chapter_no < int(window_start) or chapter_no > int(window_end):
                continue
            default = NovelAgent._default_stage_contract(chapter_no, status="complete")
            relation_raw = str(item.get("relation") or "").strip().lower()
            normalized = {
                **default,
                "chapter_no": chapter_no,
                "status": "complete",
                "relation": relation_aliases.get(relation_raw, "uncertain"),
                "confidence": confidence_aliases.get(str(item.get("confidence") or "").strip().lower(), "LOW"),
                "arc_label": short(item, "arc_label", 160),
                "entry_state": short(item, "entry_state", 700, default["entry_state"]),
                "chapter_change": short(item, "chapter_change", 700, default["chapter_change"]),
                "cut_point": short(item, "cut_point", 700, default["cut_point"]),
                "carry_out": short(item, "carry_out", 700, default["carry_out"]),
                "time_span": short(item, "time_span", 260, default["time_span"]),
                "continuation_mode": short(item, "continuation_mode", 120, "unknown"),
                "sequence_role": short(item, "sequence_role", 80, "continue"),
                "next_entry_hint": short(item, "next_entry_hint", 500),
                "no_routine_shell": flag(item, "no_routine_shell", True),
                "must_preserve": items(item, "must_preserve"),
                "must_not_advance": items(item, "must_not_advance") or default["must_not_advance"],
                "allowed_expansion": items(item, "allowed_expansion") or default["allowed_expansion"],
                "reason": short(item, "reason", 700, default["reason"]),
                "error": "",
                "window_start": int(window_start), "window_end": int(window_end),
                "source_digest": source_digest,
            }
            chapters.append(normalized)

        chapters.sort(key=lambda item: item["chapter_no"])
        if not any(item["chapter_no"] == int(chapter) for item in chapters):
            chapters.append({
                **NovelAgent._default_stage_contract(chapter, status="fallback"),
                "window_start": int(window_start), "window_end": int(window_end),
                "source_digest": source_digest,
            })
            chapters.sort(key=lambda item: item["chapter_no"])
        return {
            "schema_version": 2,
            "status": "complete" if chapters and any(item["status"] == "complete" for item in chapters) else "fallback",
            "window_start": int(window_start), "window_end": int(window_end),
            "source_digest": source_digest,
            "generated_for": int(value.get("generated_for") or chapter),
            "arc_notes": str(value.get("arc_notes") or "").strip()[:1000],
            "chapters": chapters,
        }

    def _stage_contract_chapter(self, contract, chapter):
        for item in contract.get("chapters", []) if isinstance(contract, dict) else []:
            if int(item.get("chapter_no", -1) or -1) == int(chapter):
                current = dict(item)
                rows = [row for row in contract.get("chapters", []) if isinstance(row, dict)]
                rows.sort(key=lambda row: int(row.get("chapter_no", 0) or 0))
                previous = [row for row in rows if int(row.get("chapter_no", 0) or 0) < int(chapter)]
                following = [row for row in rows if int(row.get("chapter_no", 0) or 0) > int(chapter)]
                if previous:
                    row = previous[-1]
                    current["previous_contract"] = {
                        "chapter_no": row.get("chapter_no"),
                        "relation": row.get("relation", "uncertain"),
                        "carry_out": str(row.get("carry_out") or "")[:500],
                        "cut_point": str(row.get("cut_point") or "")[:500],
                    }
                if following:
                    row = following[0]
                    current["next_contract"] = {
                        "chapter_no": row.get("chapter_no"),
                        "relation": row.get("relation", "uncertain"),
                        "entry_state": str(row.get("entry_state") or "")[:500],
                        "chapter_change": str(row.get("chapter_change") or "")[:500],
                        "next_entry_hint": str(row.get("next_entry_hint") or "")[:500],
                    }
                return current
        return self._default_stage_contract(chapter)

    def _saved_stage_contract(self, chapter):
        """Load the current chapter's contract without exposing future details."""
        path = self.root / "plans" / f"{int(chapter):04d}.stage.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return self._default_stage_contract(chapter)
        if isinstance(data, dict) and isinstance(data.get("chapters"), list):
            return self._stage_contract_chapter(data, chapter)
        if isinstance(data, dict) and int(data.get("chapter_no", -1) or -1) == int(chapter):
            return dict(data)
        return self._default_stage_contract(chapter)

    @staticmethod
    def _stage_contract_tail(contract, limit=3):
        """Serialize the prior rolling window's tail for the next window."""
        if not isinstance(contract, dict):
            return ""
        rows = [row for row in contract.get("chapters", []) if isinstance(row, dict)]
        rows.sort(key=lambda row: int(row.get("chapter_no", 0) or 0))
        if not rows:
            return ""
        return json.dumps(rows[-max(1, int(limit)):], ensure_ascii=False, indent=2)[:6000]

    @staticmethod
    def _normalize_plan_contract_check(value):
        """Normalize the small post-Plan contract check without blocking Plan."""
        if not isinstance(value, dict):
            return {
                "status": "UNCERTAIN", "score": 50, "violations": [],
                "fixes": [], "summary": "检查器未返回可解析的 JSON；保留当前计划。",
                "error": "计划合同检查未返回 JSON 对象",
                "raw_score": None, "score_semantics": "missing",
            }

        aliases = {
            "pass": "PASS", "passed": "PASS", "ok": "PASS", "通过": "PASS", "合格": "PASS",
            "revise": "REVISE", "revision": "REVISE", "fail": "REVISE", "不通过": "REVISE", "需修正": "REVISE",
            "uncertain": "UNCERTAIN", "unknown": "UNCERTAIN", "不确定": "UNCERTAIN", "无法判断": "UNCERTAIN",
        }
        raw_status = str(value.get("status") or value.get("verdict") or "").strip().lower()
        status = aliases.get(raw_status, "")
        violations = value.get("violations")
        if not isinstance(violations, list):
            violations = value.get("issues")
        if not isinstance(violations, list):
            violations = []

        def clean_list(raw, limit, item_limit=500):
            if not isinstance(raw, list):
                return []
            out = []
            for item in raw:
                if isinstance(item, dict):
                    code = str(item.get("code") or item.get("type") or "").strip()
                    detail = str(item.get("detail") or item.get("evidence") or item.get("message") or "").strip()
                    text = f"{code}: {detail}" if code and detail else (code or detail)
                else:
                    text = str(item or "").strip()
                if text and text not in out:
                    out.append(text[:item_limit])
                if len(out) >= limit:
                    break
            return out

        violation_text = clean_list(violations, 8)
        if not status:
            if value.get("needs_revision") is True or value.get("passed") is False or violation_text:
                status = "REVISE"
            elif value.get("passed") is True or value.get("needs_revision") is False:
                status = "PASS"
            else:
                status = "UNCERTAIN"
        elif status == "PASS" and violation_text:
            status = "REVISE"
        has_compliance_score = "compliance_score" in value
        raw_score = value.get(
            "compliance_score",
            value.get("score", 100 if status == "PASS" else 40 if status == "REVISE" else 50),
        )
        try:
            score = int(raw_score)
        except (TypeError, ValueError):
            score = 100 if status == "PASS" else 40 if status == "REVISE" else 50
        score = max(0, min(100, score))
        score_semantics = "compliance_0_100" if has_compliance_score else "legacy_ambiguous"

        if status == "PASS" and not violation_text and not has_compliance_score:
            score = 100
            score_semantics = "legacy_status_normalized"
        elif status == "PASS" and score < 80:
            status = "UNCERTAIN"
            score_semantics = "contradictory_compliance_score"

        if status == "REVISE" and not violation_text:
            status = "UNCERTAIN"
            score_semantics = "revise_without_evidence"
        return {
            "status": status,
            "score": score,
            "violations": violation_text,
            "fixes": clean_list(value.get("fixes") or value.get("revision_instructions"), 8),
            "summary": str(value.get("summary") or value.get("reason") or "").strip()[:1000],
            "error": "",
            "raw_score": raw_score,
            "score_semantics": score_semantics,
        }

    @staticmethod
    def _plan_repeated_diary_flags(plan, stage_contract, nearby_context=""):
        """Find a repeated full-day shell before relying on the model verdict."""
        relation = str((stage_contract or {}).get("relation") or "uncertain").lower()
        if relation not in {"same_event", "same_arc", "uncertain"}:
            return []
        current = str(plan or "")
        nearby = str(nearby_context or "")
        if not current or not nearby or "暂无已生成的前章 Plan" in nearby:
            return []

        opening = r"(?:早上|上午|起床|新的一天|第[一二三四五六七八九十]+天|次日|隔天|翌日|周[一二三四五六日天]|星期[一二三四五六日天])"
        closing = r"(?:回家|回到家|回住处|吃完饭|写完作业|睡觉|睡着|入睡|关灯|明天再说|当天结束|本章结尾|收束)"
        routine = ("起床", "上学", "放学", "回家", "吃饭", "晚饭", "写作业", "洗漱", "睡觉", "睡着", "关灯", "公交", "乘车", "通勤")
        event_terms = (
            "交锋", "冲突", "争执", "发现", "调查", "选择", "决定", "拒绝", "交换",
            "获得", "失去", "暴露", "误会", "关系", "线索", "异常", "结果", "后果",
            "能力", "训练", "战斗", "任务", "联系", "收到", "消息", "询问", "前往",
            "查看", "确认", "交谈", "见面", "拿到", "阅读", "分析", "整理", "准备",
            "处理", "进入", "离开", "试探", "答应", "拒绝", "提出", "解释",
        )
        current_routine = sum(1 for word in routine if word in current)
        nearby_routine = sum(1 for word in routine if word in nearby)
        current_events = sum(1 for word in event_terms if word in current)
        current_shell = bool(re.search(opening, current) and re.search(closing, current))
        nearby_shells = len(re.findall(opening, nearby)) >= 2 and len(re.findall(closing, nearby)) >= 2
        repeated_days = len(re.findall(r"(?:第[一二三四五六七八九十]+天|次日|隔天|翌日|几天后|周[一二三四五六日天]|星期[一二三四五六日天])", current)) >= 2
        nearby_day_markers = len(re.findall(r"(?:第[一二三四五六七八九十]+天|次日|隔天|翌日|几天后|周[一二三四五六日天]|星期[一二三四五六日天])", nearby))

        flags = []
        if (
            current_shell and nearby_shells and current_routine >= 3
            and nearby_routine >= 4 and current_events <= 2
        ):
            flags.append("当前计划与前章计划连续采用完整作息开场和回家/睡觉收尾，需改为事件切片或压缩时间")
        elif repeated_days and current_routine >= 3 and current_events <= 2:
            flags.append("当前计划连续铺开多个自然日和重复作息，但未体现必要的事件切点")
        elif nearby_day_markers >= 3 and current_routine >= 4 and current_events <= 2:
            flags.append("前章已连续使用日期推进和重复作息，本章必须压缩日常并把篇幅用于新的事件或人物关系变化")
        return flags

    def _check_plan_stage_contract(self, n, plan, stage_contract, nearby_context="",
                                   canon_context=""):
        """Check whether a generated Plan follows its rolling stage contract.

        Provider failures and malformed answers become UNCERTAIN.  The caller
        retries them and blocks prose generation if no trustworthy Plan remains.
        """
        cfg = self.config_loader().get("writing_guardrails", {}) or {}
        if not bool(cfg.get("enabled", True)) or not bool(cfg.get("plan_contract_check", True)):
            return {"status": "SKIP", "score": 100, "violations": [], "fixes": [], "summary": "已关闭计划合同检查。", "error": ""}
        if self.stop_event.is_set():
            raise ProviderCancelledError("用户请求停止 Plan 合同检查")
        contract_text = json.dumps(stage_contract or {}, ensure_ascii=False, separators=(",", ":"))[:10000]
        plan_text = str(plan or "").strip()
        if not plan_text:
            return {"status": "UNCERTAIN", "score": 0, "violations": ["计划为空"], "fixes": ["重新生成完整章节计划"], "summary": "计划为空，无法检查。", "error": ""}
        system = """你是长篇网文的章节计划执行检查器。你不写正文，也不重写计划，只判断计划是否遵守当前章节合同和任务边界。必须输出严格 JSON，不要 Markdown。
只有存在会改变章节切点、提前消费后续结果、把连续事件拆成独立日记，或明显违反当前任务的实质问题时，才判定 REVISE。普通日常、水字数、措辞不够漂亮不算 REVISE。
"""
        user = f"""检查第{int(n)}章的章节计划。

【当前章节阶段承接合同】
{contract_text}

【待检查章节计划】
{plan_text[:18000]}

【最近章节摘要与上一章交接（仅用于判断是否重复使用章节骨架）】
{str(nearby_context or "（无额外摘要）")[:6000]}

【结构化Canon账本｜不得覆盖】
{str(canon_context or "（尚无结构化历史；第1章或旧格式兼容状态）")[:14000]}

检查重点：
- same_event 是否从 entry_state 进入，并只推进到 cut_point，把 carry_out 留给下一章；
- same_arc 是否承接阶段目标而没有一次性完成后续多章结果；
- 是否无依据地套用“新的一天开始—完成事项—回家/睡觉”的日记骨架，或用完整作息代替事件推进；
- 是否提前消费后续章节的秘密、结果、关系确认或主线威胁；
- 是否把Canon中已经完成的任务、研究步骤或知识揭示重新写成第一次；
- 是否无触发撤销仍有效决定，改变物品持有人/位置或最终数字；
- 是否让结论强度超过现有样本和证据，或低变化重开近期已关闭场景；
- 是否违反当前章节任务卡、上一章交接或人物知识边界。

输出：{{"status":"PASS|REVISE|UNCERTAIN","compliance_score":0,"violations":["实质问题"],"fixes":["自动重规划时应如何修正"],"summary":"一句话依据"}}
- compliance_score 是合规度，范围 0—100：100 表示完全遵守，0 表示存在严重实质违规；不是风险分。
- PASS 时 compliance_score 必须为 80—100 且 violations 必须为空。
- REVISE 时必须在 violations 中写出至少一项明确实质违规；没有明确违规证据时只能用 UNCERTAIN。
REVISE 只用于实质问题；证据不足用 UNCERTAIN。"""
        cfg = self.config_loader().get("writing_guardrails", {}) or {}
        try:
            check_retry_limit = max(0, min(2, int(cfg.get("plan_contract_check_retries", 1))))
        except (TypeError, ValueError):
            check_retry_limit = 1
        result = None
        for check_attempt in range(check_retry_limit + 1):
            try:
                raw = self._chat(
                    "plan", system, user, 0.1, 900, False, "plan_contract_check", False,
                    routing_context=f"第{int(n)}章 Plan 合同执行检查",
                    thinking_override=False, reasoning_effort_override="low",
                    response_format={"type": "json_object"},
                )
                result = self._normalize_plan_contract_check(_json_obj(raw))
            except ProviderCancelledError:
                raise
            except Exception as exc:
                result = {
                    "status": "UNCERTAIN", "score": 50, "violations": [], "fixes": [],
                    "summary": "合同检查失败；当前计划不能据此放行。",
                    "error": f"{type(exc).__name__}: {exc}"[:800],
                    "raw_score": None, "score_semantics": "checker_error",
                }
            if result.get("status") != "UNCERTAIN" or check_attempt >= check_retry_limit:
                break
            self.log(
                f"Plan 合同检查结果不确定：第 {int(n)} 章仅重试检查器 "
                f"（第 {check_attempt + 1}/{check_retry_limit} 次），不重新生成 Plan。"
            )
        if result.get("status") == "UNCERTAIN" and result.get("error"):
            self.log(
                f"Plan 合同执行检查失败：{result.get('error')}；"
                "已按不确定处理，不把检查器失败误判为 Plan 失败。"
            )
        static_flags = self._plan_repeated_diary_flags(plan, stage_contract, nearby_context)
        if static_flags and result.get("status") != "SKIP":
            result["violations"] = list(dict.fromkeys(static_flags + list(result.get("violations") or [])))[:8]
            result["fixes"] = list(dict.fromkeys([
                "保留本章核心事件和人物互动，删除重复作息流程，明确入口、推进和章末交接",
            ] + list(result.get("fixes") or [])))[:8]
            result["status"] = "REVISE"
            result["score"] = min(int(result.get("score", 50) or 50), 35)
            result["summary"] = (result.get("summary") or "")[:700]
            self.log(f"Plan 结构硬检查命中：第 {int(n)} 章检测到连续日记式骨架，自动进入纠偏。")
        return result

    @staticmethod
    def _plan_check_rank(check, index=0):
        status_rank = {"PASS": 3, "REVISE": 2, "UNCERTAIN": 1, "SKIP": 1}
        row = check if isinstance(check, dict) else {}
        return (
            status_rank.get(str(row.get("status") or "").upper(), 0),
            int(row.get("score", 0) or 0),
            -len(row.get("violations") or []),
            -int(index),
        )

    def _generate_checked_plan(self, n, system, user, generation_cfg, stage_contract,
                               nearby_context="", canon_context=""):
        """Generate Plan, then automatically repair material contract drift."""
        guard_cfg = self.config_loader().get("writing_guardrails", {}) or {}
        try:
            retry_limit = max(0, min(2, int(guard_cfg.get("plan_contract_retries", 1))))
        except (TypeError, ValueError):
            retry_limit = 1

        with self.lock:
            self.status.update({"plan_gate_status": "running", "plan_gate_attempts": 0, "plan_gate_error": ""})

        candidates = []
        current_user = user
        for attempt in range(retry_limit + 1):
            if self.stop_event.is_set():
                raise ProviderCancelledError("用户请求停止 Plan 生成")
            if attempt:
                self.log(f"Plan 合同自动纠偏：第 {n} 章正在重规划（第 {attempt}/{retry_limit} 次）。")
            try:
                plan = self._chat(
                    "plan", system, current_user, generation_cfg["temperatures"]["plan"],
                    generation_cfg["max_tokens"]["plan"], True, "plan", False,
                )
            except ProviderCancelledError:
                raise
            except Exception as exc:
                if not candidates:
                    raise
                self.log(f"Plan 合同自动纠偏重规划失败：{exc}；保留上一版计划继续。")
                with self.lock:
                    self.status["plan_gate_error"] = f"{type(exc).__name__}: {exc}"[:800]
                break
            check = self._check_plan_stage_contract(
                n, plan, stage_contract, nearby_context=nearby_context,
                canon_context=canon_context,
            )
            candidates.append((plan, check))
            with self.lock:
                self.status.update({
                    "plan_gate_status": check.get("status", "UNCERTAIN"),
                    "plan_gate_attempts": len(candidates),
                    "plan_gate_error": check.get("error", ""),
                })
            check_status = str(check.get("status") or "UNCERTAIN").upper()
            if check_status in {"PASS", "SKIP"} or attempt >= retry_limit:
                break
            if check_status != "REVISE" or not check.get("violations"):
                break
            issues = check.get("violations") or [
                "当前计划检查结果不确定，必须重新核对结构化Canon、阶段切点和任务完成状态"
            ]
            fixes = check.get("fixes") or []
            feedback = "\n".join(f"- {item}" for item in (issues + fixes)[:12])
            current_user = user + f"""

【阶段合同自动纠偏｜只修计划结构，不改变大方向】
上一版计划被执行检查判定存在实质偏离。请保留当前章节大纲要求和人物事实，只修正以下问题后重新输出完整章节计划：
{feedback}
这是结构性拒绝，不是文风建议。不要沿用上一版的日程骨架；必须从具体未完事件、人物互动或现实问题进入，以“触发→行动/互动→变化/后果”安排场景，并在行动、决定、关系余波、异常证据或现实后果处切断。普通作息只能合并成一句过渡；不得把早上、下午、晚上或回家睡觉当作场景顺序或默认章尾。不要解释重试过程，不要写正文；必须明确本章入口、实际推进、cut point、carry out。
"""

        best_index, (best_plan, best_check) = max(
            enumerate(candidates), key=lambda item: self._plan_check_rank(item[1][1], item[0])
        )
        if candidates and all(
            str(check.get("status") or "").upper() == "REVISE"
            for _, check in candidates
        ):
            best_index = len(candidates) - 1
            best_plan, best_check = candidates[best_index]
        checks = {
            "chapter_no": int(n), "selected_attempt": int(best_index),
            "attempts": len(candidates), "selected": best_check,
            "all_checks": [check for _, check in candidates],
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }
        try:
            self.write(f"plans/{int(n):04d}.contract_check.json", json.dumps(checks, ensure_ascii=False, indent=2))
        except Exception as exc:
            self.log(f"Plan 合同检查记录写入失败：{exc}；不影响本章继续。")
        if best_index:
            self.log(
                f"Plan 合同自动纠偏完成：第 {n} 章采用第 {best_index + 1} 版计划，"
                f"检查结果={best_check.get('status')}，评分={best_check.get('score')}。"
            )
        else:
            self.log(
                f"Plan 合同执行检查：第 {n} 章结果={best_check.get('status')}，"
                f"评分={best_check.get('score')}；无需重规划。"
            )
        with self.lock:
            self.status.update({
                "plan_gate_status": best_check.get("status", "UNCERTAIN"),
                "plan_gate_attempts": len(candidates),
                "plan_gate_error": best_check.get("error", ""),
            })
        if str(best_check.get("status") or "").upper() not in {"PASS", "SKIP"}:
            raise PlanQualityGateError(
                f"第 {n} 章 Plan 经 {len(candidates)} 次检查后仍为 "
                f"{best_check.get('status')}（{best_check.get('score')}/100）；"
                "已保存检查记录，未进入正文生成"
            )
        return best_plan

    def plan_stage_contract(self, n):
        """Create or reuse a generic rolling contract for the nearby outline window."""
        n = int(n)
        full_cfg = self.config_loader()
        cfg = full_cfg.get("context", {}) or {}
        lookbehind = max(0, min(3, int(cfg.get("plan_stage_lookbehind_chapters", 3) or 3)))
        lookahead = max(1, min(8, int(cfg.get("plan_stage_lookahead_chapters", 8) or 8)))
        window_start = max(1, n - lookbehind)
        window_end = n + lookahead
        source_text = self._outline_source_text(n)
        source_digest = hashlib.sha256(str(source_text).encode("utf-8")).hexdigest()

        with self.lock:
            self.status.update({
                "plan_arc_status": "running", "plan_arc_relation": "unknown",
                "plan_arc_confidence": "LOW", "plan_arc_error": "",
            })
        prior_contract = None
        for cached in reversed(getattr(self, "_stage_contract_cache", [])):
            if cached.get("source_digest") != source_digest:
                continue
            cached_start = int(cached.get("window_start", 0) or 0)
            cached_end = int(cached.get("window_end", 0) or 0)
            if cached_start <= n <= cached_end:
                if n == cached_end:
                    prior_contract = cached
                    continue
                current = self._stage_contract_chapter(cached, n)
                with self.lock:
                    self.status.update({
                        "plan_arc_status": "cached", "plan_arc_relation": current.get("relation", "uncertain"),
                        "plan_arc_confidence": current.get("confidence", "LOW"), "plan_arc_error": "",
                    })
                self.log(f"Plan 阶段合同：复用第 {cached.get('window_start')}-{cached.get('window_end')} 章滚动合同。")
                return current

        contract_dir = self.root / "runtime" / "plan_stage_contracts"
        if contract_dir.exists():
            for path in sorted(contract_dir.glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True)[:40]:
                try:
                    cached = json.loads(path.read_text(encoding="utf-8"))
                except Exception:
                    continue
                if cached.get("source_digest") != source_digest:
                    continue
                cached_start = int(cached.get("window_start", 0) or 0)
                cached_end = int(cached.get("window_end", 0) or 0)
                if cached_start <= n <= cached_end:
                    if n == cached_end:
                        prior_contract = cached
                        continue
                    self._stage_contract_cache.append(cached)
                    current = self._stage_contract_chapter(cached, n)
                    with self.lock:
                        self.status.update({
                            "plan_arc_status": "cached", "plan_arc_relation": current.get("relation", "uncertain"),
                            "plan_arc_confidence": current.get("confidence", "LOW"), "plan_arc_error": "",
                        })
                    self.log(f"Plan 阶段合同：从缓存恢复第 {cached.get('window_start')}-{cached.get('window_end')} 章滚动合同。")
                    return current

        stage_outline = self.plan_stage_outline_context(n, include_future_details=True)
        prior_tail = self._stage_contract_tail(prior_contract)
        canon_context = self.canon_guard_context(n)
        system = """你是长篇网文的阶段规划编辑。你不写正文，只把一个附近大纲窗口整理成可执行的章节承接合同。合同必须适用于日常生活、感情、调查、训练、战斗、转场等不同类型，不得把所有章节强行判为连续，也不得把所有章节写成独立日记。必须输出严格 JSON，不要 Markdown。"""
        user = f"""请为第{window_start}—{window_end}章附近的大纲生成滚动阶段合同。当前需要执行的是第{n}章；输入可能缺少某些章节，不能凭空补写剧情。

判定规则：
- same_event：同一件事直接连续，当前章应明确从上一章状态进入，并在事件或对话的自然切点停下，不必完成整件事。
- same_arc：属于同一阶段但局部事件已改变，可以完成本章小事件，同时留下阶段状态。
- independent：确实独立，不要为了连续而硬接；仍要给当前章一个自然结束状态。
- uncertain：证据不足，按当前章大纲和上一章交接保守处理。
- 每个章节都必须写 entry_state、chapter_change、cut_point、carry_out；cut_point 是章节结构切点，不是强行制造悬念。
- 对同一阶段的连续章节额外写 sequence_role（open/continue/turn/close）、next_entry_hint，并标记 no_routine_shell=true；如果前后章节共享同一事件或阶段，必须优先设计事件切片和承接状态，而不是给每章套一个完整自然日。
- 时间跨度按事件需要决定，不得默认一章一天；普通作息不构成章节推进。
- 后续章节的具体结果只能写入 must_not_advance 或 carry_out 的留白提醒，不能提前变成当前章任务。

输出结构：
{{
  "generated_for": {n},
  "arc_notes": "整个窗口的简短阶段说明",
  "chapters": [
    {{
      "chapter_no": 1,
      "relation": "same_event|same_arc|independent|uncertain",
      "confidence": "HIGH|MEDIUM|LOW",
      "arc_label": "阶段名称",
      "entry_state": "本章从什么状态开始",
      "chapter_change": "本章必须改变什么",
      "cut_point": "本章应该停在哪里",
      "carry_out": "什么状态交给下一章",
      "time_span": "自然时间跨度",
      "continuation_mode": "same_scene|same_day|跨日|跳跃|unknown",
      "sequence_role": "open|continue|turn|close",
      "next_entry_hint": "下一章从什么未完状态接入；没有则留空",
      "no_routine_shell": true,
      "must_preserve": ["必须保留的承接事实"],
      "must_not_advance": ["本章禁止提前完成的后续内容"],
      "allowed_expansion": ["可扩写的人物互动或现实阻力"],
      "reason": "判断依据"
    }}
  ]
}}

章节编号必须使用输入中实际出现的编号；至少返回当前第{n}章，其他附近章节按能判断的范围返回。

【前3章、当前章、后8章大纲】
{stage_outline}

【上一滚动合同尾部交接｜仅用于跨窗口保留状态】
{prior_tail or "（没有可继承的上一窗口合同）"}

【截至第{n-1}章的结构化Canon账本｜不得覆盖】
{canon_context}

如果当前大纲字面要求的事件、知识揭示、物品取得、决定或研究步骤已经在Canon账本中完成，
必须把当前章合同改写为继续、深化或处理后果，禁止安排人物重新第一次经历。无法无冲突执行时，
必须在当前章 confidence=LOW、reason 和 must_not_advance 中明确指出，不得假装未发生。
"""
        self.log(f"Plan 阶段合同：生成第 {n} 章前3章、后8章的通用滚动合同……")
        if self.stop_event.is_set():
            raise ProviderCancelledError("用户请求停止 Plan 阶段合同生成")
        try:
            contract_retry_limit = max(0, min(2, int(
                (full_cfg.get("writing_guardrails", {}) or {}).get("plan_stage_contract_retries", 1)
            )))
        except (TypeError, ValueError):
            contract_retry_limit = 1
        contract = None
        contract_error = None
        for attempt in range(contract_retry_limit + 1):
            try:
                if attempt:
                    self.log(
                        f"Plan 阶段合同返回不完整：第 {n} 章进行第 "
                        f"{attempt}/{contract_retry_limit} 次重试。"
                    )
                raw = self._chat(
                    "plan", system, user, 0.2, 2800, False, "plan_stage_contract", False,
                    routing_context=f"第{n}章附近阶段合同",
                    thinking_override=False, reasoning_effort_override="low",
                    response_format={"type": "json_object"},
                )
                candidate_contract = self._normalize_stage_contract(
                    _json_obj(raw), n, window_start, window_end, source_digest
                )
                current_candidate = self._stage_contract_chapter(candidate_contract, n)
                if current_candidate.get("status") != "complete":
                    raise ValueError("阶段合同未提供当前章的完整条目")
                contract = candidate_contract
                break
            except ProviderCancelledError:
                raise
            except Exception as exc:
                contract_error = exc
        if contract is None:
            exc = contract_error or RuntimeError("阶段合同生成失败")
            contract = {
                "schema_version": 2, "status": "fallback", "window_start": window_start,
                "window_end": window_end, "source_digest": source_digest,
                "generated_for": n, "arc_notes": "", "chapters": [
                    {**self._default_stage_contract(n, error=f"{type(exc).__name__}: {exc}"),
                     "window_start": window_start, "window_end": window_end,
                     "source_digest": source_digest}
                ],
            }
            self.log(
                f"Plan 阶段合同失败：{exc}；使用通用保守合同。"
                "该合同不具备放行资格，后续 Plan 必须通过严格执行检查后才能生成正文。"
            )

        if contract.get("status") == "complete":
            self._stage_contract_cache.append(contract)
            self._stage_contract_cache = self._stage_contract_cache[-20:]
            contract_dir.mkdir(parents=True, exist_ok=True)
            cache_path = contract_dir / f"{source_digest[:16]}_{window_start}_{window_end}.json"
            try:
                cache_path.write_text(json.dumps(contract, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            except OSError as exc:
                self.log(f"Plan 阶段合同缓存写入失败：{exc}；不影响本章继续。")

        current = self._stage_contract_chapter(contract, n)
        with self.lock:
            self.status.update({
                "plan_arc_status": contract.get("status", "fallback"),
                "plan_arc_relation": current.get("relation", "uncertain"),
                "plan_arc_confidence": current.get("confidence", "LOW"),
                "plan_arc_error": current.get("error", ""),
            })
        return current

    def chapter_task_card(self, n):
        """Build a zero-cost hard task card from the current chapter/range outline."""
        cfg = self.config_loader().get("writing_guardrails", {})
        if not bool(cfg.get("enabled", True)) or not bool(cfg.get("task_card", True)):
            return ""

        text, _, blocks = self._outline_blocks(n)
        candidates = [b for b in blocks if b["start"] <= int(n) <= b["end"]]
        candidates.sort(key=lambda b: (b["end"] - b["start"], b["start"]))
        matched = candidates[0] if candidates else None
        outline = matched["text"] if matched else self.current_chapter_outline(n)

        if matched and matched["end"] > matched["start"]:
            span = matched["end"] - matched["start"] + 1
            granularity = f"""【大纲粒度说明】
当前大纲块覆盖第{matched['start']}-{matched['end']}章（共 {span} 章），不是第{n}章的单章清单。
- 这段大纲描述的是整个区间应逐步完成的方向；绝对不能在第{n}章一次性把整段全部写完。
- 必须结合最近章节摘要与当前状态，先判断区间目标中哪些已经在前文完成；已完成内容不得重复推进。
- 本章只选择当前时间点最自然、最小必要的一步推进，给后续章节保留空间。
- 如果某个重大事件无法从大纲判断“明确应发生在第{n}章”，默认不要在本章发生；宁可写合理过渡，也不要抢跑。
- 区间大纲中的后半段内容可能属于第{n+1}章及以后，禁止为了完整覆盖大纲而提前消费。"""
        else:
            granularity = "【大纲粒度说明】\n当前大纲块是单章粒度，可按本章任务直接执行。"

        return f"""【第{n}章硬任务卡】
【当前章节所属大纲原文——最高剧情依据】
{outline}

{granularity}

【大纲到正文的转换原则】
- 大纲锁定的是人物、时间、因果、关键事件、结果和章末状态，不锁定大纲本身的句式、段落顺序和表现形式。
- 章节是一个事件或关系推进单元，不是一个必须从早写到晚的自然日。先确定本章要改变什么，再按因果需要决定时间跨度；可以只写数小时，也可以自然跨越一晚、数日或更长，不要求固定天数，也不为跨日而跨日。
- 没有新变化的等待、通勤、作息和重复练习，使用一句话、简短蒙太奇或直接跳过；不要每个日期都重新安排“早上—白天—晚上”，也不要为了闭环补上起床、上学、吃饭、回家、练功、写作业和睡觉。
- 允许在事件尚未完全结束时收章，把 ongoing 状态交给下一章；章末优先落在行动、决定、关系余波、异常证据或现实后果，不以睡觉、关灯或“明天再说”作为默认收束。
- 上一章结束在哪一天，不代表本章必须从下一天早晨开始；只有承接自然需要时才推进日期。日期变化本身不算剧情推进。
- 大纲中的规则、边界、职责和评价标准属于作者背景资料；除非场景中确有自然需要，不得让角色逐条宣读，不得自动扩写成协议、表格、会议纪要、墙上标语或培训问答。
- 不要为了“覆盖大纲”让人物轮流发言、轮流展示能力或轮流总结自身问题。只选择对本章因果与转折有作用的人物进入前景，其余信息可以通过自然略写保留。
- 人物不应总能立即、准确、完整地诊断自己。允许误判、回避、停顿、嘴硬、打断和事后才意识到问题。
- 能从动作、对话和结果中推断出的结论，不再由教练、记录者或叙述者复述；不得把人物表现压缩成标签式评语。
- 专业内容必须落在具体规则、局面、选择和后果上；资料不足时宁可减少术语，不得用抽象术语拼装虚假专业感。

【执行规则】
1. 当前章节所属大纲和本任务卡的优先级高于最近正文中的悬念、伏笔和模型自行联想。
2. 最近章节存在未解内容，不代表本章必须调查、解释、强化或回收。
3. 只推进当前章节大纲明确要求的事件，以及完成这些事件所必需的自然过渡。
4. 不得创建会改变后续剧情的新主线、新秘密、新组织、新长期目标或大型伏笔。
5. 除非当前章节大纲明确要求，否则不得把普通行为升级解释为阴谋、仪式、监视、跟踪或超自然现象。
6. 除非当前章节大纲明确要求，否则不得提前推进超自然主线，不得提前消费未来章节内容。
7. 除非当前章节大纲明确要求，否则不得跨越式推进感情关系、告白、确认关系或重大关系转折。
8. 严格核对人物身份、性别、关系、知识边界，以及上一章已经发生的事实。
9. 章节结束时只达到当前章节大纲要求的状态，不替后续章节完成剧情。
10. 不得把“完整度”理解为完整记录一天；如果本章核心事件在半天内完成，就保持短跨度，如果关系、调查或训练自然延续，就用合理的时间跳跃跨日推进。

【默认禁止项——只有当前章节大纲明确要求时才可解除】
- 主动调查未解悬念或异常人物
- 新匿名纸条、神秘警告、跟踪、监视、仪式、隐藏组织、神秘车辆等新增悬疑升级
- 将普通日常细节强行解释成阴谋或超自然迹象
- 提前揭露角色真实身份、秘密设定或超自然设定
- 无大纲依据的重大感情推进
- 无大纲依据的新主线、新秘密、新长期伏笔
"""

    def format_memories(self, memories):
        if not memories:
            return "（尚无可检索的长期记忆）"
        lines = []
        for m in memories:
            score = "" if m.get("score") is None else f" sim={m['score']:.3f}"
            lines.append(f"- [第{m['chapter_no']}章/{m['kind']}/{m.get('entity','')}/{m.get('key','')}{score}] {m['content']}")
        return "\n".join(lines)

    def format_current_state(self, as_of_chapter):
        st = self.db.state_as_of(max(0, int(as_of_chapter)))
        parts = []
        for group, title in (("states", "当前人物/世界状态"), ("hooks", "未回收伏笔")):
            rows = st.get(group, [])
            if rows:
                parts.append(f"【{title}】")
                for m in rows:
                    parts.append(f"- [{m['kind']}/{m['entity']}/{m['key']}] {m['content']}")
        return "\n".join(parts) if parts else "【当前状态】\n（尚无动态状态）"

    @staticmethod
    def _clean_character_heading(text):
        x = re.sub(r"[*_`#]+", "", str(text or "")).strip()
        x = re.sub(r"\s+", " ", x)
        return x

    def _character_seed_entries(self):
        """Split characters_seed.md into reusable character-sized blocks.

        This is intentionally heuristic and local-only: it does not make another
        model call, so the anti-drift lock adds no API cost. Markdown headings are
        preferred; simple 姓名:/角色: paragraphs are also supported.
        """
        text = self.read_story('characters_seed.md').strip()
        if not text:
            return []

        stop = {
            "人物设定", "角色设定", "主要人物", "主要角色", "人物", "角色",
            "主角", "配角", "NPC", "初始反派角色", "其他人物",
            "人物关系", "角色关系", "设定", "人设",
            "外貌", "性格", "背景", "家庭", "经历", "关系", "能力", "特征",
            "备注", "当前状态", "基础信息", "身份", "标签", "语言风格", "说话习惯",
            "初始人物设定", "基础说明", "初始性格", "初始性格与能力",
            "家庭背景", "初始关系", "与主角的初始关系", "信息边界与使用原则",
        }

        def names_for(heading, block, infer_heading=True):
            out = []
            probe = self._clean_character_heading(heading)
            for pat in (
                r"(?:姓名|角色名|人物名|名字)\s*[：:]\s*([\u3400-\u9fff·]{2,8})",
                r"(?:主要角色|主要人物|角色|人物)\s*[：:]\s*([\u3400-\u9fff·]{2,8})",
            ):
                for m in re.finditer(pat, block):
                    out.append(m.group(1).strip())
            if probe and infer_heading:
                # Handles headings such as "角色乙（女主）" / "角色乙 - 女主".
                head = re.split(r"[（(【\[：:\-—|/\s]", probe, maxsplit=1)[0].strip()
                if 2 <= len(head) <= 8:
                    out.append(head)
                # Handles "主要角色：角色乙".
                m = re.search(r"[：:]\s*([\u3400-\u9fff·]{2,8})", probe)
                if m:
                    out.append(m.group(1).strip())
            cleaned = []
            for name in out:
                name = name.strip(" ，,。；;：:（）()【】[]")
                if 2 <= len(name) <= 8 and name not in stop and name not in cleaned:
                    cleaned.append(name)
            return cleaned

        entries = []
        heads = list(re.finditer(r"(?m)^(#{1,6})[ \t]+(.+?)[ \t]*$", text))
        if heads:
            for i, m in enumerate(heads):
                level = len(m.group(1))
                heading = m.group(2).strip()
                # A character heading owns nested subheadings (e.g. 外貌/性格) until
                # the next heading at the same or a higher level.
                end = len(text)
                for nxt in heads[i + 1:]:
                    if len(nxt.group(1)) <= level:
                        end = nxt.start()
                        break
                block = text[m.start():end].strip()
                # Level-2 headings represent people in the current story file;
                # deeper headings are fields such as “初始性格/家庭背景”, not
                # additional character names. Explicit name fields still work.
                names = names_for(heading, block, infer_heading=(level <= 2))
                if names:
                    entries.append({"heading": heading, "names": names, "text": block})
        else:
            for para in re.split(r"\n\s*\n", text):
                block = para.strip()
                if not block:
                    continue
                names = names_for("", block)
                if names:
                    entries.append({"heading": names[0], "names": names, "text": block})
        return entries

    def character_lock(
        self, n, task_card="", plan="",
        state_snapshot=None, extra_focus=""
    ):
        """Return a compact, late-position lock for characters involved this chapter."""
        focus = "\n".join(x for x in (
            self.current_chapter_outline(n),
            task_card or "",
            plan or "",
            extra_focus or "",
        ) if x)
        if not focus.strip():
            return ""

        state = (
            state_snapshot if isinstance(state_snapshot, dict)
            else self.db.state_as_of(max(0, int(n) - 1))
        )
        state_rows = list(state.get("states", []) or [])
        state_entities = []
        for row in state_rows:
            ent = str(row.get("entity", "")).strip()
            if ent and ent not in state_entities:
                state_entities.append(ent)

        ranked = []
        for entry in self._character_seed_entries():
            matched = []
            pos = 10**9
            for name in entry.get("names", []):
                i = focus.find(name)
                if i >= 0:
                    matched.append(name); pos = min(pos, i)
            for ent in state_entities:
                if ent in entry.get("text", ""):
                    i = focus.find(ent)
                    if i >= 0:
                        matched.append(ent); pos = min(pos, i)
            if matched:
                ranked.append((pos, entry, list(dict.fromkeys(matched))))
        ranked.sort(key=lambda x: x[0])
        ranked = ranked[:6]
        if not ranked:
            return ""

        selected_names = []
        blocks = []
        total = 0
        for _, entry, matched in ranked:
            for name in matched:
                if name not in selected_names:
                    selected_names.append(name)
            block = entry.get("text", "").strip()
            if len(block) > 2200:
                block = block[:2200].rstrip() + "\n（该人物基础设定摘录已截断）"
            if total + len(block) > 7000:
                break
            blocks.append(block)
            total += len(block)

        dynamic = []
        for row in state_rows:
            ent = str(row.get("entity", "")).strip()
            if ent and ent in selected_names:
                dynamic.append(
                    f"- [{row.get('kind','state')}/{ent}/{row.get('key','')}] {row.get('content','')}"
                )
        dynamic_text = "\n".join(dynamic[:40]) if dynamic else "（无额外动态状态；以基础设定和正文已知事实为准）"

        return """【本章人物事实锁｜优先防止人设漂移】
以下摘录来自 characters_seed.md 与截至上一章的当前状态。它们不是新的剧情要求。
- 身份、性别、家庭出身、基础外貌、核心性格等硬事实不得被章节计划或自由发挥覆盖。
- 关系、地点、伤病、持有物、已知信息等可变项，以“当前动态状态”和最近已发生 Canon 为准。
- 如果章节计划与本人物锁发生事实冲突，必须修正计划中的错误，而不是改写人物设定。

【相关人物基础设定】
%s

【相关人物当前动态状态】
%s
""" % ("\n\n".join(blocks), dynamic_text)

    def static_context(self, n):
        cfg = self.config_loader()
        return f"""
【小说标题】
{cfg.get('title','未命名小说')}

【故事核心】
{self.read_story('premise.md')}

【世界观硬设定】
{self.read_story('world.md')}

【初始人物设定】
{self.read_story('characters_seed.md')}

【文风要求】
{self.read_story('style.md')}

【当前章节附近大纲】
{self.outline_context(n)}

{self.format_current_state(n-1)}
"""

    def retrieve(self, query, max_chapter=None):
        cfg = self.config_loader(); ecfg = cfg["embedding"]
        memories = self.db.search(query, top_k=ecfg.get("top_k", 14), min_score=ecfg.get("min_score", 0.22), max_chapter=max_chapter)
        with self.lock:
            self.status["retrieved_memories"] = len(memories)
        return memories

    def common_context(self, n, retrieval_query):
        cfg = self.config_loader()
        recent = self.recent_summaries(n, cfg["generation"].get("recent_summary_count", 5))
        memories = self.retrieve(retrieval_query, max_chapter=n-1)
        return self.static_context(n) + f"""

【最近章节摘要】
{recent}

【SQLite 长期记忆：按当前任务语义检索】
{self.format_memories(memories)}
"""

    @staticmethod
    def _estimate_prompt_tokens(text):
        """Conservative local token estimate for Plan preflight budgeting.

        DeepSeek does not expose a free tokenizer endpoint, so this intentionally
        over-estimates mixed Chinese/Markdown text a little.  The real usage from
        the provider remains the billing source of truth after the request.
        """
        text = str(text or "")
        if not text:
            return 0
        cjk = len(re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]", text))
        nonspace = len(re.sub(r"\s+", "", text)) - cjk
        # Chinese is close to one token per character for budgeting purposes;
        # Latin/Markdown tends to pack more densely.  1.10 provides headroom so
        # a displayed 30K target is less likely to cross Volcengine's 32K tier.
        return max(1, int((cjk + max(0, nonspace) / 3.2) * 1.10 + 64))

    @staticmethod
    def _relevance_units(text):
        """Cheap language-agnostic lexical units used only for local state ranking."""
        text = str(text or "")
        units = set(re.findall(r"[A-Za-z0-9_]{2,24}", text.lower()))
        cjk = "".join(re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff]", text))
        # Character bigrams work surprisingly well for names/places/items without
        # adding jieba or another dependency.
        units.update(cjk[i:i+2] for i in range(max(0, len(cjk)-1)))
        return units

    @staticmethod
    def _format_state_snapshot(st):
        """Render one already-read state snapshot without another SQLite scan."""
        parts = []
        for group, title in (("states", "当前人物/世界状态"), ("hooks", "未回收伏笔")):
            rows = st.get(group, []) or []
            if rows:
                parts.append(f"【{title}】")
                for m in rows:
                    parts.append(
                        f"- [{m.get('kind','state')}/{m.get('entity','')}/{m.get('key','')}] "
                        f"{m.get('content','')}"
                    )
        return "\n".join(parts) if parts else "【当前状态】\n（尚无动态状态）"

    def _prepare_relevant_current_state(
        self, as_of_chapter, focus_text, snapshot=None, cancel_event=None,
    ):
        """Read and score dynamic state once for all Plan budget probes.

        Older builds repeated this full pass for every binary-search probe. With
        several thousand state rows that could pin one CPU core for minutes before
        the provider route was even logged. The prepared result is immutable for
        the current Plan and can be rendered at many row limits cheaply.
        """
        as_of = max(0, int(as_of_chapter))
        st = snapshot if isinstance(snapshot, dict) else self.db.state_as_of(as_of)
        cancel_event = cancel_event or self.stop_event
        focus = str(focus_text or "")
        focus_units = self._relevance_units(focus)
        candidates = []
        total_rows = 0

        for group, title in (("states", "当前人物/世界状态"), ("hooks", "未回收伏笔")):
            for idx, row in enumerate(st.get(group, []) or []):
                total_rows += 1
                if total_rows == 1 or total_rows % 128 == 0:
                    if cancel_event.is_set():
                        raise ProviderCancelledError("用户请求停止 Plan 本地状态整理")

                ent = str(row.get("entity", "") or "").strip()
                key = str(row.get("key", "") or "").strip()
                content = str(row.get("content", "") or "").strip()
                exact_focus = False
                score = 0.0
                if ent and ent in focus:
                    score += 100.0
                    exact_focus = True
                if key and len(key) >= 2 and key in focus:
                    score += 55.0
                    exact_focus = True

                overlap = len(focus_units.intersection(self._relevance_units(f"{ent} {key} {content}")))
                score += min(30.0, float(overlap) * 1.5)
                importance = int(row.get("importance", 3) or 3)
                score += max(0, min(5, importance)) * 1.5
                row_ch = int(row.get("chapter_no", 0) or 0)
                age = max(0, as_of - row_ch)
                if age <= 3:
                    score += 24.0
                elif age <= 10:
                    score += 14.0
                elif age <= 30:
                    score += 6.0
                if group == "hooks" and not exact_focus:
                    score *= 0.35
                candidates.append((score, group, title, idx, row))

        relevant = [item for item in candidates if item[0] > 0]
        relevant.sort(
            key=lambda x: (
                -x[0],
                0 if x[1] == "states" else 1,
                -int(x[4].get("chapter_no", 0) or 0),
                x[3],
            )
        )
        explicit_entities = []
        for _, _, _, _, row in relevant:
            ent = str(row.get("entity", "") or "").strip()
            if ent and ent in focus and ent not in explicit_entities:
                explicit_entities.append(ent)
        return {
            "snapshot": st,
            "candidates": candidates,
            "relevant": relevant,
            "explicit_entities": explicit_entities[:12],
            "total_rows": total_rows,
            "cancel_event": cancel_event,
        }

    def _render_prepared_current_state(self, prepared, max_rows=24):
        """Render a prepared relevance ranking at one row budget."""
        max_rows = max(1, int(max_rows))
        total_rows = int(prepared.get("total_rows", 0) or 0)
        snapshot = prepared.get("snapshot") or {}
        relevant = prepared.get("relevant") or []
        candidates = prepared.get("candidates") or []
        explicit_entities = prepared.get("explicit_entities") or []
        cancel_event = prepared.get("cancel_event") or self.stop_event
        if total_rows <= max_rows or not relevant:
            return self._format_state_snapshot(snapshot)

        chosen = []
        chosen_keys = set()
        if len(explicit_entities) >= 2:
            reserve_each = max(6, min(18, max_rows // max(4, len(explicit_entities) * 3)))
            for ent in explicit_entities:
                count = 0
                for item_no, item in enumerate(relevant):
                    if item_no % 256 == 0 and cancel_event.is_set():
                        raise ProviderCancelledError("用户请求停止 Plan 本地状态裁剪")
                    row = item[4]
                    if str(row.get("entity", "") or "").strip() != ent:
                        continue
                    key = (item[1], item[3])
                    if key in chosen_keys:
                        continue
                    chosen.append(item); chosen_keys.add(key); count += 1
                    if count >= reserve_each or len(chosen) >= max_rows:
                        break

        per_entity = {}
        for item in chosen:
            ent = str(item[4].get("entity", "") or "").strip()
            per_entity[ent] = per_entity.get(ent, 0) + 1
        entity_cap = max_rows if len(explicit_entities) < 2 else max(20, int(max_rows * 0.55))
        for item_no, item in enumerate(relevant):
            if item_no % 256 == 0 and cancel_event.is_set():
                raise ProviderCancelledError("用户请求停止 Plan 本地状态裁剪")
            if len(chosen) >= max_rows:
                break
            key = (item[1], item[3])
            if key in chosen_keys:
                continue
            ent = str(item[4].get("entity", "") or "").strip()
            if ent and per_entity.get(ent, 0) >= entity_cap:
                continue
            chosen.append(item); chosen_keys.add(key)
            per_entity[ent] = per_entity.get(ent, 0) + 1

        chosen_ids = {(item[1], item[3]) for item in chosen}
        parts = []
        for group, title in (
            ("states", "当前人物/世界状态（相关性裁剪）"),
            ("hooks", "未回收伏笔（相关性裁剪）"),
        ):
            rows = [
                (idx, row) for _, g, _, idx, row in candidates
                if g == group and (g, idx) in chosen_ids
            ]
            rows.sort(key=lambda x: x[0])
            if rows:
                parts.append(f"【{title}】")
                for _, m in rows:
                    parts.append(
                        f"- [{m.get('kind','state')}/{m.get('entity','')}/{m.get('key','')}] "
                        f"{m.get('content','')}"
                    )
        if not parts:
            return self._format_state_snapshot(snapshot)
        omitted = max(0, total_rows - len(chosen))
        if omitted:
            parts.append(
                f"（已省略 {omitted} 条与本章关联较弱的动态状态；"
                "最近状态、人物事实锁、本章大纲和语义检索记忆仍保留。）"
            )
        return "\n".join(parts)

    def format_relevant_current_state(
        self, as_of_chapter, focus_text, max_rows=24, snapshot=None, cancel_event=None,
    ):
        """Compatibility wrapper for stages that only need one state subset."""
        prepared = self._prepare_relevant_current_state(
            as_of_chapter, focus_text, snapshot=snapshot, cancel_event=cancel_event,
        )
        return self._render_prepared_current_state(prepared, max_rows=max_rows)

    def _focus_character_names(self, focus_text):
        """Find named seeded characters explicitly involved in this chapter."""
        focus = str(focus_text or "")
        names = []
        for entry in self._character_seed_entries():
            for name in entry.get("names", []) or []:
                if name and name in focus and name not in names:
                    names.append(name)
        return names[:16]

    def analyze_chapter_complexity(self, n, focus_text, memories=None):
        """Local-only, explainable chapter complexity score.

        No extra model or embedding request is made.  It reuses already retrieved
        memories plus outline/task/plan/draft text and local recent summaries.
        Complexity can only stay the same or rise during a chapter.
        """
        cfg = self.config_loader()
        ccfg = cfg.get("cost_control", {})
        cconf = ccfg.get("chapter_complexity", {}) or {}
        medium_at = int(cconf.get("medium_score", 4))
        complex_at = int(cconf.get("complex_score", 8))
        old_gap = int(cconf.get("old_memory_chapters", 80))
        very_old_gap = int(cconf.get("very_old_memory_chapters", 150))
        return_gap = int(cconf.get("return_gap_chapters", 20))

        focus = str(focus_text or "")
        memories = list(memories or [])
        names = self._focus_character_names(focus)

        mem_chapters = []
        old_count = 0
        very_old_count = 0
        hook_hits = 0
        relation_hits = 0
        retrieved_entities = set()

        for m in memories:
            ch = int(m.get("chapter_no", 0) or 0)
            if ch > 0:
                mem_chapters.append(ch)
                gap = max(0, int(n) - ch)
                if gap >= old_gap:
                    old_count += 1
                if gap >= very_old_gap:
                    very_old_count += 1
            kind = str(m.get("kind", "") or "")
            if kind == "hook":
                hook_hits += 1
            if kind in {"relationship", "knowledge_state"}:
                relation_hits += 1
            ent = str(m.get("entity", "") or "").strip()
            if ent:
                retrieved_entities.add(ent)

        # A character named in the current task/plan but absent from a fairly
        # generous recent-summary window is treated as a weak "returning" signal.
        recent_window = max(1, min(return_gap, int(n) - 1)) if int(n) > 1 else 1
        recent_text = self.recent_summaries(n, recent_window) if int(n) > 1 else ""
        returning = [name for name in names if name not in recent_text]

        strong_keywords = (
            "揭露", "真相", "身份", "秘密", "重逢", "回归", "反转", "决裂",
            "背叛", "失踪", "死亡", "复仇", "汇合", "回收伏笔", "旧事",
            "多年以前", "当年", "幕后", "知情", "暴露", "摊牌",
        )
        keyword_hits = sum(1 for x in strong_keywords if x in focus)

        score = 0
        reasons = []

        char_count = len(names)
        if char_count >= 7:
            score += 3
            reasons.append(f"{char_count} 名相关人物")
        elif char_count >= 4:
            score += 1
            reasons.append(f"{char_count} 名相关人物")

        entity_count = len(set(names).union(retrieved_entities))
        if entity_count >= 9:
            score += 2
            reasons.append(f"{entity_count} 个相关实体")
        elif entity_count >= 6:
            score += 1
            reasons.append(f"{entity_count} 个相关实体")

        if old_count >= 6:
            score += 3
            reasons.append(f"{old_count} 条跨 ≥{old_gap} 章记忆")
        elif old_count >= 3:
            score += 1
            reasons.append(f"{old_count} 条跨 ≥{old_gap} 章记忆")

        if very_old_count >= 2:
            score += 2
            reasons.append(f"{very_old_count} 条跨 ≥{very_old_gap} 章旧记忆")
        elif very_old_count == 1:
            score += 1
            reasons.append(f"命中 1 条跨 ≥{very_old_gap} 章旧记忆")

        if hook_hits >= 4:
            score += 3
            reasons.append(f"{hook_hits} 条相关旧伏笔")
        elif hook_hits >= 2:
            score += 2
            reasons.append(f"{hook_hits} 条相关旧伏笔")
        elif hook_hits == 1:
            score += 1
            reasons.append("命中 1 条相关旧伏笔")

        if relation_hits >= 7:
            score += 2
            reasons.append(f"{relation_hits} 条关系/知识边界记忆")
        elif relation_hits >= 4:
            score += 1
            reasons.append(f"{relation_hits} 条关系/知识边界记忆")

        if len(returning) >= 2:
            score += 2
            reasons.append("久未在最近摘要出现：" + "、".join(returning[:4]))
        elif len(returning) == 1:
            score += 1
            reasons.append("久未在最近摘要出现：" + returning[0])

        if mem_chapters:
            span = max(mem_chapters) - min(mem_chapters)
            if span >= 150:
                score += 2
                reasons.append(f"检索记忆跨度 {span} 章")
            elif span >= 80:
                score += 1
                reasons.append(f"检索记忆跨度 {span} 章")
        else:
            span = 0

        if keyword_hits >= 5:
            score += 2
            reasons.append(f"{keyword_hits} 个复杂剧情弱信号")
        elif keyword_hits >= 2:
            score += 1
            reasons.append(f"{keyword_hits} 个复杂剧情弱信号")

        score = min(10, score)

        # Continuity-sensitive signals force at least medium even if the additive
        # score happens to land below the threshold.
        force_medium = bool(
            very_old_count >= 1
            or hook_hits >= 2
            or len(returning) >= 1
            or (old_count >= 3 and relation_hits >= 2)
        )
        force_complex = bool(
            (very_old_count >= 3 and hook_hits >= 2)
            or (char_count >= 7 and relation_hits >= 4)
            or (old_count >= 6 and hook_hits >= 4)
        )

        if force_complex or score >= complex_at:
            level, label = "complex", "复杂"
        elif force_medium or score >= medium_at:
            level, label = "medium", "中等"
        else:
            level, label = "normal", "普通"

        info = {
            "score": score,
            "level": level,
            "label": label,
            "reasons": reasons[:8] or ["未发现明显跨线连续性风险"],
            "characters": names,
            "old_memory_count": old_count,
            "very_old_memory_count": very_old_count,
            "hook_hits": hook_hits,
            "relation_hits": relation_hits,
            "returning_characters": returning,
            "memory_span": span,
        }

        # Complexity never drops within the same chapter as more evidence becomes
        # available after Plan/Draft.
        previous = self._complexity_cache.get(int(n))
        order = {"normal": 0, "medium": 1, "complex": 2}
        if previous and (
            order.get(previous.get("level"), 0) > order.get(level, 0)
            or int(previous.get("score", 0)) > score
        ):
            info = previous
        else:
            self._complexity_cache[int(n)] = info

        with self.lock:
            self.status["chapter_complexity_score"] = int(info["score"])
            self.status["chapter_complexity_level"] = info["level"]
            self.status["chapter_complexity_label"] = info["label"]
            self.status["chapter_complexity_reasons"] = list(info["reasons"])

        return info

    def high_context_policy(self):
        """Return the persistent manual high-context mode and bounded limits."""
        ccfg = self.config_loader().get("cost_control", {}) or {}
        enabled = bool(ccfg.get("high_context_mode_enabled", False))
        try:
            target = int(ccfg.get("high_context_target_tokens", 120000) or 120000)
        except (TypeError, ValueError):
            target = 120000
        try:
            hard_max = int(ccfg.get("high_context_max_tokens", 127000) or 127000)
        except (TypeError, ValueError):
            hard_max = 127000
        target = max(33000, min(127000, target))
        hard_max = max(target, min(127999, hard_max))
        return enabled, target, hard_max

    def _canon_stage_context_budget(self, stage_key, complexity_level):
        high_enabled, high_target, _high_max = self.high_context_policy()
        if high_enabled:
            return high_target
        ccfg = self.config_loader().get("cost_control", {})
        defaults = {
            "draft": {"normal": 40000, "medium": 50000, "complex": 62000},
            "review": {"normal": 50000, "medium": 65000, "complex": 80000},
            "deep_review": {"normal": 60000, "medium": 75000, "complex": 95000},
            "revision": {"normal": 45000, "medium": 58000, "complex": 72000},
        }
        configured = ccfg.get("canon_context_budgets", {}) or {}
        stage_cfg = configured.get(stage_key, {}) or {}
        value = stage_cfg.get(
            complexity_level,
            defaults.get(stage_key, {}).get(complexity_level, 50000),
        )
        return max(24000, min(150000, int(value)))

    def _stage_static_context(
        self, n, outline_text=None, current_state_text=None,
        character_seed_text=None,
    ):
        """Static story context with caller-controlled outline/state slices."""
        cfg = self.config_loader()
        if outline_text is None:
            outline_text = self.outline_context(n)
        if current_state_text is None:
            current_state_text = self.format_current_state(n - 1)
        if character_seed_text is None:
            character_seed_text = self.read_story('characters_seed.md')
        character_seed_text = str(character_seed_text or "").strip()
        if not character_seed_text:
            character_seed_text = "（本章相关人物基础设定见后置的“本章人物事实锁”。）"
        return f"""
【小说标题】
{cfg.get('title','未命名小说')}

【故事核心】
{self.read_story('premise.md')}

【世界观硬设定】
{self.read_story('world.md')}

【初始人物设定】
{character_seed_text}

【文风要求】
{self.read_story('style.md')}

【当前章节附近大纲】
{outline_text}

{current_state_text}
"""

    def _trim_canon_stage_prompt(
        self, n, stage_key, system, focus_text, memories, build_user
    ):
        """Fit Draft/Review/DeepReview/Revision by trimming only redundant context.

        build_user signature:
            build_user(state_text, outline_text, memory_rows, recent_count) -> str

        Never truncates task card, Plan, draft, review instructions, character lock,
        premise/world/characters/style, or the current outline block.
        """
        cfg = self.config_loader()
        g = cfg["generation"]
        ccfg = cfg.get("cost_control", {})
        enabled = bool(ccfg.get("canon_context_trim_enabled", True))
        complexity = self.analyze_chapter_complexity(
            n, focus_text=focus_text, memories=memories
        )
        high_enabled, high_target, high_max = self.high_context_policy()
        target = self._canon_stage_context_budget(stage_key, complexity["level"])
        if high_enabled:
            target = high_target
            safe = high_max
        else:
            safe_margin = max(
                1000, min(20000, int(ccfg.get("canon_context_safe_margin_tokens", 5000)))
            )
            safe = target + safe_margin

        state_text = self.format_current_state(n - 1)
        original_state = state_text
        # Prose-producing stages must not see the next chapter's answer through
        # the generic nearby-outline window. Plan and Review retain adjacent
        # blocks for sequencing and validation; Draft/Revision get the current
        # hard block plus the protected previous handoff only.
        outline_text = (
            self.current_chapter_outline(n)
            if stage_key in {"draft", "revision"}
            else self.outline_context(n)
        )
        current_outline = self.current_chapter_outline(n)
        memory_keep = len(memories)
        recent_count = int(g.get("recent_summary_count", 3))
        actions = []

        user = build_user(
            state_text, outline_text, memories[:memory_keep], recent_count
        )
        raw_est = self._estimate_prompt_tokens(system + "\n" + user)

        min_rows_defaults = {
            "draft": 16,
            "review": 24,
            "deep_review": 32,
            "revision": 24,
        }
        min_rows_cfg = ccfg.get("canon_context_min_state_rows", {}) or {}
        min_rows = max(
            8,
            int(min_rows_cfg.get(stage_key, min_rows_defaults.get(stage_key, 16))),
        )

        snapshot = self.db.state_as_of(max(0, int(n - 1)))
        total_state_rows = sum(
            len(snapshot.get(group, []) or []) for group in ("states", "hooks")
        )
        state_trimmed = False

        def row_count(rendered):
            return sum(
                1
                for line in str(rendered or "").splitlines()
                if line.lstrip().startswith("- [")
            )

        def rebuild():
            return build_user(
                state_text, outline_text, memories[:memory_keep], recent_count
            )

        def fit_state_to_budget(budget_tokens):
            """Largest relevant state slice that fits; otherwise smallest useful slice."""
            nonlocal state_text
            if total_state_rows <= min_rows:
                return None

            lo, hi = min_rows, max(min_rows, total_state_rows - 1)
            best = None
            smallest = None

            while lo <= hi:
                mid = (lo + hi) // 2
                candidate = self.format_relevant_current_state(
                    n - 1, focus_text, max_rows=mid
                )
                if candidate == original_state:
                    hi = mid - 1
                    continue

                previous_state = state_text
                state_text = candidate
                candidate_user = rebuild()
                est = self._estimate_prompt_tokens(system + "\n" + candidate_user)
                state_text = previous_state

                item = (candidate, est, row_count(candidate))
                if smallest is None or item[1] < smallest[1]:
                    smallest = item

                if est <= budget_tokens:
                    best = item
                    lo = mid + 1
                else:
                    hi = mid - 1

            return best or smallest

        if enabled and raw_est > target:
            # 1) The growing current_state is the primary cost source. Trim it first.
            fitted = fit_state_to_budget(max(24000, target - 500))
            if fitted:
                candidate, est, kept = fitted
                state_text = candidate
                state_trimmed = True
                actions.append(
                    f"current_state {total_state_rows}→{kept} 条"
                )
                user = rebuild()

            # 2) Preserve recent summaries by default. Only reduce if the complete
            # fixed prompt still cannot fit after current_state trimming.
            if self._estimate_prompt_tokens(system + "\n" + user) > target and recent_count > 3:
                old = recent_count
                recent_count = 3
                actions.append(f"最近摘要 {old}→3")
                user = rebuild()

            if self._estimate_prompt_tokens(system + "\n" + user) > target and recent_count > 2:
                old = recent_count
                recent_count = 2
                actions.append(f"最近摘要 {old}→2")
                user = rebuild()

            # 3) Nearby outline is redundant with task_card/current outline.
            if self._estimate_prompt_tokens(system + "\n" + user) > target:
                if current_outline and current_outline != outline_text:
                    outline_text = current_outline
                    actions.append("附近大纲→当前章节/区间")
                    user = rebuild()

            # 4) Semantic memories are already high-value; only drop the weakest tail
            # if we still exceed the safe margin.
            if (
                self._estimate_prompt_tokens(system + "\n" + user) > safe
                and memory_keep > 6
            ):
                old = memory_keep
                memory_keep = 6
                actions.append(f"长期记忆 {old}→6")
                user = rebuild()

            # 5) Refill relevant state into spare room after other reductions.
            if state_trimmed and self._estimate_prompt_tokens(system + "\n" + user) < target - 1000:
                fitted = fit_state_to_budget(max(24000, target - 300))
                if fitted:
                    candidate, est, kept = fitted
                    state_text = candidate
                    if actions and actions[0].startswith("current_state"):
                        actions[0] = f"current_state {total_state_rows}→{kept} 条"
                    user = rebuild()

        final_est = self._estimate_prompt_tokens(system + "\n" + user)
        with self.lock:
            self.status["stage_context_target_tokens"] = int(target)
            self.status["stage_context_estimated_tokens"] = int(final_est)
            self.status["stage_context_trimmed"] = bool(actions)

        reason_text = "；".join(complexity["reasons"][:4])
        if enabled:
            if actions:
                self.log(
                    f"{stage_key} 动态上下文：复杂度={complexity['label']} "
                    f"{complexity['score']}/10（{reason_text}）；"
                    f"估算 {raw_est:,}→{final_est:,} tokens，目标≈{target:,}；"
                    + "；".join(actions)
                    + "。实际 token 以 API usage 为准。"
                )
            else:
                self.log(
                    f"{stage_key} 动态上下文：复杂度={complexity['label']} "
                    f"{complexity['score']}/10（{reason_text}）；"
                    f"估算 {final_est:,} tokens，无需裁剪，目标≈{target:,}。"
                )
        else:
            self.log(
                f"{stage_key} 动态上下文裁剪已关闭；复杂度={complexity['label']} "
                f"{complexity['score']}/10；估算 {final_est:,} tokens。"
            )

        if enabled and final_est > safe and not high_enabled:
            self.log(
                f"{stage_key} 上下文仍高于安全余量（{final_est:,}>{safe:,}），"
                "说明固定高价值内容本身较大；为保护连续性不再强行截断。"
            )

        if high_enabled and final_est > high_max:
            raise CanonContextLimitError(
                f"{stage_key} 上下文裁剪后仍有 {final_est:,} tokens，"
                f"超过高上下文模式硬上限 {high_max:,}；本次请求未发送。"
            )

        return user, complexity

    def _plan_static_context(
        self, n, outline_text=None, current_state_text=None,
        character_seed_text=None,
    ):
        cfg = self.config_loader()
        if outline_text is None:
            outline_text = self.outline_context(n)
        if current_state_text is None:
            current_state_text = self.format_current_state(n-1)
        if character_seed_text is None:
            character_seed_text = self.read_story('characters_seed.md')
        character_seed_text = str(character_seed_text or "").strip()
        if not character_seed_text:
            character_seed_text = "（本章相关人物基础设定见后置的“本章人物事实锁”。）"
        return f"""
【小说标题】
{cfg.get('title','未命名小说')}

【故事核心】
{self.read_story('premise.md')}

【世界观硬设定】
{self.read_story('world.md')}

【初始人物设定】
{character_seed_text}

【文风要求】
{self.read_story('style.md')}

【当前章节附近大纲】
{outline_text}

{current_state_text}
"""

    def _chat(self, stage, system, user, temperature, max_tokens, stream, label, emit_text,
              routing_context="", provider_override=None, model_override=None,
              thinking_override=None, response_format=None, reasoning_effort_override=None):
        cfg = self.config_loader()
        canon_cfg = cfg.get("canon", {})
        cost_cfg = cfg.get("cost_control", {})
        canon_stages = {"plan", "draft", "review", "deep_review", "revision", "summary"}
        canon_deepseek_only = bool(canon_cfg.get("deepseek_only", True)) and stage in canon_stages
        stage_models = canon_cfg.get("stage_models", {})
        lightweight_plan_aux = stage == "plan" and label in {"plan_stage_contract", "plan_contract_check"}
        defaults = {
            "plan": ("deepseek-v4-pro", True, "low"),
            "draft": ("deepseek-v4-flash", False, "low"),
            "review": ("deepseek-v4-flash", True, "low"),
            "deep_review": ("deepseek-v4-pro", True, "high"),
            "revision": ("deepseek-v4-flash", False, "low"),
            "summary": ("deepseek-v4-flash", False, "low"),
            "memory": ("deepseek-v4-flash", False, "low"),
        }
        if canon_deepseek_only:
            default_model, default_thinking, default_effort = defaults[stage]
            scfg = stage_models.get(stage, {})
            provider_override = "deepseek"
            model_override = scfg.get("model", default_model)
            thinking_override = bool(scfg.get("thinking", default_thinking))
            if lightweight_plan_aux:
                model_key = (
                    "plan_stage_contract_model"
                    if label == "plan_stage_contract"
                    else "plan_contract_check_model"
                )
                model_override = str(
                    cfg.get("writing_guardrails", {}).get(model_key, "deepseek-v4-flash")
                ) or "deepseek-v4-flash"
                thinking_override = False
        elif stage == "memory":
            # Memory extraction also stays on DeepSeek.  The retired 8080 text
            # model is no longer a routing or fallback dependency.
            scfg = stage_models.get("memory", {})
            provider_override = "deepseek"
            model_override = scfg.get("model", "deepseek-v4-flash")
            thinking_override = bool(scfg.get("thinking", False))
            canon_deepseek_only = True

        scfg_eff = stage_models.get(stage, {}) if isinstance(stage_models, dict) else {}
        if reasoning_effort_override is None:
            reasoning_effort_override = str(scfg_eff.get("reasoning_effort", "low" if bool(thinking_override) else "low"))
        if reasoning_effort_override == "medium":
            reasoning_effort_override = "high"
        chat_kwargs = {
            "stream": stream,
            "label": label,
            "emit_text": emit_text,
            "routing_context": routing_context,
            "provider_override": provider_override,
            "model_override": model_override,
            "thinking_override": thinking_override,
            "response_format": response_format,
            "reasoning_effort_override": reasoning_effort_override,
            "allow_local_fallback": not canon_deepseek_only,
        }
        try:
            text, spec = self.router.chat(
                stage, system, user, temperature, max_tokens, **chat_kwargs
            )
        except ProviderRefusalError as error:
            is_filtered_review = (
                stage in {"review", "deep_review"}
                and getattr(error, "finish_reason", "") == "content_filter"
            )
            if not is_filtered_review:
                raise
            self.log(
                f"第 {self.status.get('chapter') or '-'} 章 {label or stage} 输出被内容过滤；"
                "将保持完整审查输入，禁用 Thinking，并以简短中性 JSON 自动重试 1 次。"
            )
            retry_system = system + """

【内容过滤恢复模式】
你正在进行编辑审查，不是在续写或复述正文。仍须完整检查所有材料，但最终 JSON 必须简短、中性：
- 不复述敏感、暴力或成人情节，不输出长篇原文引句；
- 证据只写段落位置和不超过20字的非敏感定位短语，必要时用中性概括；
- 保持原定 JSON 字段、PASS/MINOR/MAJOR 与 needs_revision 判定，不得省略最终答案。
"""
            retry_user = user + (
                "\n\n【恢复输出要求】上一次最终输出被服务过滤。"
                "请直接返回完整、简短的审查 JSON，不描述过滤原因，不复述可能触发过滤的原句。"
            )
            retry_kwargs = dict(chat_kwargs)
            retry_kwargs["thinking_override"] = False
            retry_kwargs["reasoning_effort_override"] = "low"
            text, spec = self.router.chat(
                stage, retry_system, retry_user, temperature, max_tokens,
                **retry_kwargs,
            )
        with self.lock:
            self.status["stage_provider"] = spec.get("provider", "")
            self.status["stage_model"] = spec.get("model", "")
            self.status["stage_thinking"] = bool(spec.get("thinking", False))
            self.status["auto_nsfw_decision"] = spec.get("auto_nsfw_decision")
        return text

    def plan_chapter(self, n, task_card=""):
        self._stage(n, "规划", f"第 {n} 章规划")
        with self.lock:
            self.status.update({
                "plan_arc_status": "idle", "plan_arc_relation": "unknown",
                "plan_arc_confidence": "LOW", "plan_arc_error": "",
            })
        if self.stop_event.is_set():
            raise ProviderCancelledError("用户请求停止 Plan 本地准备")
        cfg = self.config_loader(); g = cfg["generation"]
        ccfg = cfg.get("cost_control", {})
        # Cost history is a chapter-start gate, not a prediction of this
        # chapter. Ask before retrieval, prompt assembly, or any provider call.
        history = self._chapter_cost_guard_usage(n)
        with self.lock:
            approval = dict(self._plan_overflow_approval or {})
        already_approved = int(approval.get("chapter", -1) or -1) == int(n)
        cost_blocked_at_start = (
            history["mode"] != "unlimited"
            and history["over_limit"] >= history["confirm_at"]
            and not already_approved
        )
        if cost_blocked_at_start:
            high_enabled, _high_target, high_max = self.high_context_policy()
            recovery_target = max(32000, min(127000, int(
                ccfg.get("plan_context_recovery_target_tokens", 38000) or 38000
            )))
            recovery_max = max(recovery_target, min(127000, int(
                ccfg.get("plan_context_recovery_max_tokens", 42000) or 42000
            )))
            ceiling = high_max if high_enabled else recovery_max
            overflow = {
                "pending": True, "reason": "cost_guard", "chapter": int(n),
                "estimated_tokens": 0, "target_tokens": int(recovery_target),
                "safe_tokens": int(ceiling), "provider_safe_tokens": int(ceiling),
                "over_tokens": 0, "resume_count": 0,
                "created_at": datetime.now().isoformat(timespec="seconds"),
                "hard_blocked": False,
                "auto_window_size": history["window_size"],
                "auto_window_allowed": history["confirm_at"],
                "auto_window_used": history["over_limit"],
                "auto_window_remaining": max(0, history["confirm_at"] - history["over_limit"]),
                "history_chapters_checked": history["checked"],
                "cost_guard_mode": history["mode"], "cost_guard_limit": history["limit"],
            }
            with self.lock:
                self._plan_overflow_approval = None
                self.status["plan_overflow"] = overflow
            self._emit("plan_overflow", **overflow)
            unit = "元" if history["mode"] == "cny" else " AFP"
            raise PlanContextOverflowError(
                f"前 {history['window_size']} 个已完成章节中已有 "
                f"{history['over_limit']}/{history['confirm_at']} 章的整章实际总费用超过 "
                f"{history['limit']:g}{unit}；已在本章准备开始前停止，请在 Canon 面板确认。"
            )
        prepare_started = time.perf_counter()
        self.log(f"Plan 本地准备：读取第 {n} 章任务卡、大纲与最近摘要……")
        task_card = task_card or self.chapter_task_card(n)
        current_outline = self.current_chapter_outline(n)
        # Build/reuse one generic rolling contract for this nearby outline
        # window.  The main Plan receives only the current chapter's contract;
        # Draft/Revision never receive the future-facing outline window.
        stage_contract = self.plan_stage_contract(n)
        self.write(f"plans/{n:04d}.stage.json", json.dumps(stage_contract, ensure_ascii=False, indent=2))
        stage_contract_text = json.dumps(stage_contract, ensure_ascii=False, indent=2)
        boundary = self.previous_boundary_context(n, include_future_details=False)
        boundary_prompt = self._boundary_prompt(boundary)
        self._log_boundary_context("Plan", boundary)
        canon_context = self.canon_guard_context(n)
        seed_query = f"第{n}章写作规划。当前章节大纲：{current_outline[-9000:]}\n上一章交接：{boundary_prompt[-7000:]}\n最近剧情：{self.recent_summaries(n,g['recent_summary_count'])[-6000:]}"

        # Retrieve once.  Smart trimming only slices these already-ranked results,
        # so it never adds another embedding/API request.
        ecfg = cfg.get("embedding", {})
        self.log("Plan 本地准备：检索相关长期记忆……")
        memories = self.retrieve(seed_query, max_chapter=n-1)
        if self.stop_event.is_set():
            raise ProviderCancelledError("用户请求停止 Plan 本地准备")
        recent_count = int(g.get("recent_summary_count", 3))
        outline_text = current_outline
        recent_plan_text = self.recent_plan_context(n, count=min(4, recent_count), max_chars=9000)
        self.log("Plan 本地准备：读取当前状态快照……")
        st_snapshot = self.db.state_as_of(max(0, int(n-1)))
        state_text = self._format_state_snapshot(st_snapshot)
        memory_keep = len(memories)
        character_lock = self.character_lock(
            n,
            task_card=task_card,
            state_snapshot=st_snapshot,
            extra_focus=seed_query,
        )
        # Generic chapter outlines may contain no character names. Do not fall
        # back to the complete cast file: handoff, Canon and ranked state already
        # provide continuity, while matched character cards remain protected.
        plan_character_seed = ""
        if not character_lock.strip():
            self.log(
                "Plan 人物事实锁：当前任务、交接和最近摘要均未匹配明确人物；"
                "使用 Canon/交接/相关状态，不注入完整 characters_seed.md。"
            )

        system = """你是长篇小说章节规划师。第一职责是服从当前章节大纲与任务卡，第二职责是连续性，第三职责才是戏剧性。
严格区分作者知道的信息与角色已经知道的信息。不得提前泄露尚未揭示的秘密。
不得因为最近正文出现悬念就擅自把它升级为当前主线。
大纲是事实与结果约束，不是正文模板。规划时必须把大纲转换成有阻力、有选择、有后果的场景，禁止按人物或条目轮流验收。
最高结构约束：章节按事件/关系单元规划，不按自然日规划；不得把“早上—白天—晚上—回家/睡觉”当作默认章节骨架。"""

        instruction = f"""
现在规划第{n}章。首先输出【任务卡解读】，再输出章节计划。
你还会收到【当前章节阶段承接合同】。它由当前章前最多3章、后最多8章的大纲生成，是通用的结构约束：
- 先执行合同中的 entry_state、chapter_change、cut_point、carry_out，再把它转换成具体场景；不要重新决定本章在阶段中的切点。
- relation 为 same_event 时，把本章当作同一事件的一个切片，允许在动作、对话或局势未完处收束；relation 为 same_arc 时承接阶段目标但可以完成本章小事件；relation 为 independent 时不要为了连续而硬接。
- 合同只是结构辅助，当前章节大纲和上一章受保护交接优先；不得把合同里的后续结果、秘密或兑现提前写入本章。
- 不论关系类型，都不要用“新的一天开始—完成事项—回家/睡觉”替代事件推进；时间跨度按合同和事件需要决定，不默认一章一天。
【任务卡解读】必须包含：
- 类型重心：用百分比估计本章各类内容权重，总和100%；只能依据当前章节大纲，不得为了戏剧性擅自提高悬疑/超自然/感情权重。
- 阶段位置：依据阶段承接合同说明本章属于哪条连续事件、承接前文什么状态、只推进到哪一步，并把什么未完状态交给下一章；若并非连续事件则明确说明。
- 时间跨度判断：明确本章从哪里承接、到哪里结束、预计跨越多久，以及哪些无变化的日常会被压缩或跳过；按事件/关系需要决定跨度，不默认“一章一天”，也不要为了跨日而硬加日期。
- 本章必须：只列大纲明确要求或不可缺少的推进。
- 本章可以：只列不改变剧情方向的自然生活/场景/互动补充。
- 本章禁止：列出本章不应推进的悬念、未来剧情、感情或世界观内容。
- 本章核心看点：读者阅读本章主要享受什么。可以是日常陪伴、人物互动、小反馈、冲突试探或日常趣味，不强制每章出现大事件。
- 主要互动：明确本章最值得展开的两三个人物及其互动、分歧或默契，避免所有人物平均分配篇幅。
- 阶段性满足：本章至少兑现哪一种阅读期待；可以很小，但必须与当前大纲一致，不得凭空制造爽点或强钩子。
- 弹性扩写方向：需要充实篇幅时，优先扩写哪些人物互动、现实细节或局势试探。允许有生活感和适量日常，但不要只靠起床、吃饭、乘车、回家、写作业、睡觉等连续作息流程补足篇幅。
- 事件骨架硬要求：先用一句话写清“从哪个未完状态进入→本章发生什么选择/阻力→状态如何改变→把什么交给下一章”。场景顺序必须按“触发→行动/互动→变化/后果”组织，不能按“早上→上午→下午→晚上”组织。除非当前大纲明确把作息本身当成事件，否则起床、上学、放学、乘车、吃饭、回家、写作业、睡觉只能作为一句过渡，不能单独占一个场景。
- 章节切点硬要求：本章至少有一个落在行动、对话、决定、关系余波、异常证据或现实后果上的切点；若计划最后只是回家、关灯、睡觉或“明天再说”，必须重新设计章末落点。计划中任何与此冲突的日程安排都视为待修正项。
- 章节结束状态：明确本章结束后人物和主线应停在哪一步。
- 章末落点：明确最后停在什么动作、对话、决定、情绪或局势上，并说明哪些兑现必须留给下一章。
- 合同执行核对：明确写出本章实际采用的 cut_point 和 carry_out；若与阶段合同不同，说明当前章节大纲为何必须覆盖该差异。
- 时间边界检查：若计划选择单日，说明这是事件天然限定而不是日记式模板；若只是过渡日常，应改为压缩时间或跨日承接。不得把回家、睡觉写成默认章尾。
- 连续章节检查：结合最近章节摘要，若前几章已经连续采用“新的一天开始—完成事项—回家收尾”的骨架，本章优先改用事件切片、时间跳跃或未完局面收章；不要为了变化强行添加大事件。
- 重复结构硬约束：你会看到最近已生成的章节 Plan。若其中已有完整作息骨架，本章不得只换日期后重复；必须明确新的事件推进、人物关系变化、选择/后果或异常反馈。若当前大纲确实要求跨日，压缩无变化作息并把篇幅放在事件和人物互动上。
- 最终自检：如果删掉“早上/下午/晚上、上学/回家、吃饭/睡觉”等时间和作息词后，计划仍没有明确的选择、阻力、互动变化或结果变化，判定为不合格，必须重写计划结构。
- 如果任务卡标明当前大纲覆盖多章：必须结合最近摘要判断区间目标已经完成了多少，本章只安排剩余目标中的一小步，禁止把整个区间一次规划完。

然后输出：
1. 本章目标
2. 场景的核心问题、参与者相互冲突的即时诉求，以及局势或认知转折
3. 场景顺序；每场说明前一场如何导致下一场，禁止用并列测试或轮流汇报凑场景
4. 主要人物开始/结束状态
5. 仅在任务卡允许时推进或回收的伏笔
6. 绝对不能发生的连续性/剧情偏航错误
7. 结尾方式（不得为了制造钩子新增大纲外悬念）
不要写正文。
"""

        def build_user():
            static = self._plan_static_context(
                n,
                outline_text=outline_text,
                current_state_text=state_text,
                character_seed_text=plan_character_seed,
            )
            recent = self.recent_summaries(n, recent_count)
            mem_text = self.format_memories(memories[:memory_keep])
            ctx = static + f"""

【最近章节摘要】
{recent}

【最近已生成的章节 Plan｜只用于检查重复结构，不覆盖当前大纲】
{recent_plan_text}

【SQLite 长期记忆：按当前任务语义检索】
{mem_text}
"""
            return ctx + boundary_prompt + f"""
{task_card}

【结构化Canon账本｜受保护硬约束】
{canon_context}

账本中的物品持有人/位置、最终数字、人物已知事实、仍有效决定、证据置信度和近期场景均为已发生事实。
规划前必须逐项对账：任务若已完成则改为继续或深化；改变有效决定必须安排新触发和人物明确意识；
不得用不足样本得到确定结论；不得低变化重开近期已关闭场景。无法同时满足任务和Canon时，本计划不合格。

【当前章节阶段承接合同｜Plan 执行约束】
这是从当前章前3章、后8章附近大纲生成的通用阶段合同。它只规定本章的承接、推进和切点；当前章节任务卡仍是最高依据。
{stage_contract_text}

{character_lock}

{instruction}
"""

        user = build_user()
        trim_enabled = bool(ccfg.get("plan_context_trim_enabled", True))
        high_enabled, high_target, high_max = self.high_context_policy()
        if high_enabled:
            target = high_target
            safe = high_max
        else:
            target = max(24000, min(31000, int(ccfg.get("plan_context_target_tokens", 30000))))
            safe = max(target, min(31800, int(ccfg.get("plan_context_safe_tokens", 31000))))
        # Two distinct thresholds: target drives automatic trimming, while safe
        # is the no-confirm ceiling. Prompts between them are sent automatically.
        send_limit = target
        raw_est = self._estimate_prompt_tokens(system + "\n" + user)
        actions = []
        initial_state_text = state_text
        initial_memory_keep = memory_keep
        initial_recent_count = recent_count
        initial_outline_text = outline_text

        if trim_enabled and raw_est > send_limit:
            # Spend the available 30K trimming budget on as much relevant
            # current_state as possible instead of jumping to a fixed row cap.
            fill_target = max(24000, min(send_limit, int(ccfg.get("plan_context_fill_tokens", send_limit))))
            focus = "\n".join((current_outline, task_card, seed_query))
            total_state_rows = sum(len(st_snapshot.get(group, []) or []) for group in ("states", "hooks"))
            original_state_text = state_text
            state_trimmed = False
            state_action_index = None
            ranking_started = time.perf_counter()
            self.log(f"Plan 本地准备：一次性整理并排序 {total_state_rows:,} 条状态……")
            prepared_state = self._prepare_relevant_current_state(
                n-1, focus, snapshot=st_snapshot
            )
            self.log(
                f"Plan 本地准备：状态排序完成，耗时 "
                f"{time.perf_counter() - ranking_started:.2f} 秒；开始按{send_limit/1000:.1f}K目标裁剪。"
            )

            def state_row_count(text):
                return sum(1 for line in str(text or "").splitlines() if line.lstrip().startswith("- ["))

            def fit_state_to_budget(budget_tokens):
                """Return the largest relevance-ranked state subset that fits budget.

                No model/embedding call is made here.  We only re-render already
                stored current_state rows and estimate the complete Plan prompt.
                If fixed high-value content already exceeds the target, return
                the smallest relevant subset instead of silently restoring all
                current_state rows. A final hard guard decides whether the
                irreducible prompt is safe to send.
                """
                nonlocal state_text
                if total_state_rows <= 1:
                    return None
                lo, hi = 1, max(1, total_state_rows - 1)
                best = None
                smallest = None
                probes = 0
                while lo <= hi:
                    if self.stop_event.is_set():
                        raise ProviderCancelledError("用户请求停止 Plan 本地裁剪")
                    probes += 1
                    if probes > 18:
                        self.log("Plan 本地裁剪：已达到18次计算上限，使用当前最优结果。")
                        break
                    mid = (lo + hi) // 2
                    candidate = self._render_prepared_current_state(prepared_state, max_rows=mid)
                    if candidate == original_state_text:
                        # Either there is nothing safely rankable, or mid already
                        # represents the full state.  Search a smaller subset.
                        hi = mid - 1
                        continue
                    prev = state_text
                    state_text = candidate
                    candidate_user = build_user()
                    est = self._estimate_prompt_tokens(system + "\n" + candidate_user)
                    state_text = prev
                    item = (candidate, est, state_row_count(candidate))
                    if smallest is None or item[1] < smallest[1]:
                        smallest = item
                    if est <= budget_tokens:
                        best = item
                        lo = mid + 1
                    else:
                        hi = mid - 1
                return best or smallest

            # 1) Drop only the weakest semantic-memory tail first.
            if memory_keep > 6:
                old = memory_keep; memory_keep = 6
                actions.append(f"长期记忆 {old}→{memory_keep}")
                user = build_user()

            # 2) Dynamically fill current_state to the remaining Plan budget.
            if self._estimate_prompt_tokens(system + "\n" + user) > send_limit:
                fitted = fit_state_to_budget(fill_target)
                if fitted:
                    filtered, _, kept_rows = fitted
                    before_rows = total_state_rows
                    state_text = filtered
                    state_trimmed = True
                    state_action_index = len(actions)
                    actions.append(f"current_state {before_rows}→{kept_rows} 条（按{fill_target/1000:.1f}K预算动态填充）")
                    user = build_user()

            # 3) If even the relevance-filled state cannot meet target, reduce the
            # remaining weak memory tail.
            if self._estimate_prompt_tokens(system + "\n" + user) > send_limit and memory_keep > 4:
                old = memory_keep; memory_keep = 4
                actions.append(f"长期记忆 {old}→{memory_keep}")
                user = build_user()

            # 4) Keep at least the last two summaries whenever possible.
            if self._estimate_prompt_tokens(system + "\n" + user) > send_limit and recent_count > 2:
                old = recent_count; recent_count = 2
                actions.append(f"最近摘要 {old}→{recent_count}")
                user = build_user()

            # 5) Nearby outline is redundant with the hard task card.  As a last
            # safe step, retain only the current chapter/range block here; the task
            # card still contains the same current outline verbatim.
            if self._estimate_prompt_tokens(system + "\n" + user) > send_limit:
                compact_outline = current_outline
                if compact_outline and compact_outline != outline_text:
                    outline_text = compact_outline
                    actions.append("附近大纲→仅当前章节/区间")
                    user = build_user()

            # 6) If other safe reductions created spare room, refill relevant state
            # back toward the configured send limit. Do not leave several thousand
            # useful context unused just because a fixed row cap happened to fit.
            if state_trimmed and self._estimate_prompt_tokens(system + "\n" + user) < fill_target - 250:
                fitted = fit_state_to_budget(fill_target)
                if fitted:
                    filtered, _, kept_rows = fitted
                    state_text = filtered
                    if state_action_index is not None:
                        actions[state_action_index] = f"current_state {total_state_rows}→{kept_rows} 条（按{fill_target/1000:.1f}K预算动态填充）"
                    user = build_user()

            # Final safety squeeze only if an unusually large single state row or
            # estimation variance still leaves us beyond the send limit.
            if self._estimate_prompt_tokens(system + "\n" + user) > send_limit:
                fitted = fit_state_to_budget(send_limit)
                if fitted:
                    filtered, _, kept_rows = fitted
                    state_text = filtered
                    if state_action_index is not None:
                        actions[state_action_index] = f"current_state {total_state_rows}→{kept_rows} 条（压缩目标回退）"
                    else:
                        actions.append(f"current_state {total_state_rows}→{kept_rows} 条（压缩目标回退）")
                    user = build_user()

            # Emergency reductions preserve the hard task card, current outline
            # and relevant character lock while dropping redundant tails.
            if self._estimate_prompt_tokens(system + "\n" + user) > send_limit and memory_keep > 2:
                old = memory_keep; memory_keep = 2
                actions.append(f"长期记忆 {old}→{memory_keep}（压缩目标回退）")
                user = build_user()
            if self._estimate_prompt_tokens(system + "\n" + user) > send_limit and recent_count > 1:
                old = recent_count; recent_count = 1
                actions.append(f"最近摘要 {old}→{recent_count}（压缩目标回退）")
                user = build_user()

            # Micro-squeeze only weak semantic memory. Character hard facts are a
            # protected layer and must never be shortened to make a request fit.
            if self._estimate_prompt_tokens(system + "\n" + user) > send_limit and memory_keep > 1:
                old = memory_keep; memory_keep = 1
                actions.append(f"长期记忆 {old}→{memory_keep}（微量超线回退）")
                user = build_user()
            if self._estimate_prompt_tokens(system + "\n" + user) > send_limit:
                # Last non-destructive fallback: the same relevant facts are
                # already represented by character_lock, recent summary and the
                # retained semantic memory. Avoid sending duplicate state rows.
                state_text = "【当前状态】\n（本章相关状态已由人物事实锁、最近摘要和语义记忆覆盖。）"
                actions.append("current_state重复尾项→省略（最终安全线回退）")
                user = build_user()

            # If the inexpensive tier is impossible even after the low-price
            # attempt, do not send the aggressively stripped prompt. Restore
            # useful context and make a second, quality-first fit inside the
            # operator-configured recovery band.
            low_price_est = self._estimate_prompt_tokens(system + "\n" + user)
            if not high_enabled and low_price_est > safe:
                recovery_target = max(32000, min(127000, int(
                    ccfg.get("plan_context_recovery_target_tokens", 38000) or 38000
                )))
                recovery_max = max(recovery_target, min(127000, int(
                    ccfg.get("plan_context_recovery_max_tokens", 42000) or 42000
                )))
                state_text = initial_state_text
                memory_keep = initial_memory_keep
                recent_count = initial_recent_count
                outline_text = initial_outline_text
                # Report only the effective restored prompt, not temporary
                # low-price probes that were discarded.
                actions = [
                    f"低价线不可达（{low_price_est:,}>{safe:,}）→恢复必要上下文，"
                    f"改按{recovery_target/1000:.1f}K目标/{recovery_max/1000:.1f}K上限"
                ]
                send_limit = recovery_target
                user = build_user()
                if memory_keep > 6:
                    old = memory_keep; memory_keep = 6
                    actions.append(f"恢复阶段长期记忆 {old}→{memory_keep}")
                    user = build_user()
                if self._estimate_prompt_tokens(system + "\n" + user) > recovery_target:
                    fitted = fit_state_to_budget(recovery_target)
                    if fitted:
                        state_text, _, kept_rows = fitted
                        actions.append(f"恢复阶段current_state→{kept_rows} 条")
                        user = build_user()
                if self._estimate_prompt_tokens(system + "\n" + user) > recovery_target and recent_count > 2:
                    old = recent_count; recent_count = 2
                    actions.append(f"恢复阶段最近摘要 {old}→{recent_count}")
                    user = build_user()
                if self._estimate_prompt_tokens(system + "\n" + user) > recovery_target:
                    compact_outline = current_outline
                    if compact_outline and compact_outline != outline_text:
                        outline_text = compact_outline
                        actions.append("恢复阶段附近大纲→仅当前章节/区间")
                        user = build_user()
                if self._estimate_prompt_tokens(system + "\n" + user) > recovery_target and memory_keep > 4:
                    old = memory_keep; memory_keep = 4
                    actions.append(f"恢复阶段长期记忆 {old}→{memory_keep}")
                    user = build_user()

        final_est = self._estimate_prompt_tokens(system + "\n" + user)
        with self.lock:
            self.status["stage_context_target_tokens"] = int(send_limit)
            self.status["stage_context_estimated_tokens"] = int(final_est)
            self.status["stage_context_trimmed"] = bool(actions)
        self.log(
            f"Plan 本地准备完成：总耗时 {time.perf_counter() - prepare_started:.2f} 秒；"
            "即将执行安全线判断。"
        )
        if trim_enabled:
            if actions:
                self.log(
                    f"Plan 上下文智能裁剪：估算 {raw_est:,} → {final_est:,} tokens；"
                    + "；".join(actions)
                    + f"。自动压缩目标≤{send_limit:,}；≤{safe:,} 无需确认；实际 token 以 API usage 为准。"
                )
            else:
                self.log(f"Plan 上下文智能裁剪：估算 {raw_est:,} tokens，无需裁剪（压缩目标≤{send_limit:,}，确认线>{safe:,}）。")
        else:
            self.log(f"Plan 上下文智能裁剪：已关闭；估算 {raw_est:,} tokens。")

        # Persistent high-context mode is independent from the normal recovery
        # band and cost-history guard, but never bypasses its own hard limit.
        if high_enabled:
            history = self._chapter_cost_guard_usage(n)
            with self.lock:
                approval = dict(self._plan_overflow_approval or {})
            approved = int(approval.get("chapter", -1) or -1) == int(n)
            cost_blocked = (
                history["mode"] != "unlimited"
                and history["over_limit"] >= history["confirm_at"]
            )
            if final_est <= high_max and cost_blocked and not approved:
                overflow = {
                    "pending": True, "reason": "cost_guard", "chapter": int(n),
                    "estimated_tokens": int(final_est), "target_tokens": int(send_limit),
                    "safe_tokens": int(high_max), "provider_safe_tokens": int(high_max),
                    "over_tokens": 0, "resume_count": 0,
                    "created_at": datetime.now().isoformat(timespec="seconds"),
                    "hard_blocked": False,
                    "auto_window_size": history["window_size"],
                    "auto_window_allowed": history["confirm_at"],
                    "auto_window_used": history["over_limit"],
                    "auto_window_remaining": max(0, history["confirm_at"] - history["over_limit"]),
                    "history_chapters_checked": history["checked"],
                    "cost_guard_mode": history["mode"], "cost_guard_limit": history["limit"],
                }
                with self.lock:
                    self._plan_overflow_approval = None
                    self.status["plan_overflow"] = overflow
                self._emit("plan_overflow", **overflow)
                unit = "元" if history["mode"] == "cny" else " AFP"
                raise PlanContextOverflowError(
                    f"前 {history['window_size']} 个已完成章节中已有 "
                    f"{history['over_limit']}/{history['confirm_at']} 章的整章实际总费用超过 "
                    f"{history['limit']:g}{unit}；已在本章第一次生成请求前停止，请在 Canon 面板确认。"
                )
            if final_est <= high_max:
                with self.lock:
                    self._plan_overflow_approval = None
                    self._clear_plan_overflow_locked()
                self.log(
                    f"第 {n} 章高上下文模式已开启：Plan 估算 {final_est:,} tokens，"
                    f"裁剪目标 {send_limit:,}，硬上限 {high_max:,}；自动发送。"
                )
            else:
                overflow = {
                    "pending": True,
                    "chapter": int(n),
                    "estimated_tokens": int(final_est),
                    "target_tokens": int(send_limit),
                    "safe_tokens": int(high_max),
                    "provider_safe_tokens": int(high_max),
                    "over_tokens": int(max(0, final_est - high_max)),
                    "resume_count": 0,
                    "created_at": datetime.now().isoformat(timespec="seconds"),
                    "hard_blocked": True,
                    "auto_window_size": 0,
                    "auto_window_allowed": 0,
                    "auto_window_used": 0,
                    "auto_window_remaining": 0,
                    "history_chapters_checked": 0,
                }
                with self.lock:
                    self._plan_overflow_approval = None
                    self.status["plan_overflow"] = overflow
                self._emit("plan_overflow", **overflow)
                raise PlanContextOverflowError(
                    f"Plan 上下文裁剪后仍有 {final_est:,} tokens，"
                    f"超过高上下文模式硬上限 {high_max:,}；本次请求未发送，"
                    "且不能用单次确认绕过硬上限。"
                )

        else:
            recovery_target = max(32000, min(127000, int(
                ccfg.get("plan_context_recovery_target_tokens", 38000) or 38000
            )))
            recovery_max = max(recovery_target, min(127000, int(
                ccfg.get("plan_context_recovery_max_tokens", 42000) or 42000
            )))
            history = self._chapter_cost_guard_usage(n)
            with self.lock:
                approval = dict(self._plan_overflow_approval or {})
            approved = (
                int(approval.get("chapter", -1) or -1) == int(n)
            )
            cost_blocked = (
                history["mode"] != "unlimited"
                and history["over_limit"] >= history["confirm_at"]
            )
            context_blocked = final_est > recovery_max
            if approved:
                with self.lock:
                    self._plan_overflow_approval = None
                    self._clear_plan_overflow_locked()
                self.log(
                    f"第 {n} 章已由用户一次性确认；Plan 估算 {final_est:,} tokens，"
                    "现在发送本章规划请求。"
                )
            elif cost_blocked or context_blocked:
                reason = "cost_guard" if cost_blocked and not context_blocked else (
                    "context_limit" if context_blocked and not cost_blocked else "cost_guard_and_context_limit"
                )
                overflow = {
                    "pending": True,
                    "reason": reason,
                    "chapter": int(n),
                    "estimated_tokens": int(final_est),
                    "target_tokens": int(recovery_target),
                    "safe_tokens": int(recovery_max),
                    "provider_safe_tokens": int(recovery_max),
                    "over_tokens": int(max(0, final_est - recovery_max)),
                    "resume_count": 0,
                    "created_at": datetime.now().isoformat(timespec="seconds"),
                    "hard_blocked": False,
                    "auto_window_size": history["window_size"],
                    "auto_window_allowed": history["confirm_at"],
                    "auto_window_used": history["over_limit"],
                    "auto_window_remaining": max(0, history["confirm_at"] - history["over_limit"]),
                    "history_chapters_checked": history["checked"],
                    "cost_guard_mode": history["mode"],
                    "cost_guard_limit": history["limit"],
                }
                with self.lock:
                    self._plan_overflow_approval = None
                    self.status["plan_overflow"] = overflow
                self._emit("plan_overflow", **overflow)
                reasons = []
                if cost_blocked:
                    unit = "元" if history["mode"] == "cny" else " AFP"
                    reasons.append(
                        f"前 {history['window_size']} 个已完成章节中已有 "
                        f"{history['over_limit']}/{history['confirm_at']} 章的整章实际总费用超过 "
                        f"{history['limit']:g}{unit}"
                    )
                if context_blocked:
                    reasons.append(f"Plan 恢复后仍有 {final_est:,} tokens，超过恢复上限 {recovery_max:,}")
                raise PlanContextOverflowError(
                    "；".join(reasons) + "；已在本章第一次生成请求前停止，请在 Canon 面板确认。"
                )
            else:
                with self.lock:
                    self._plan_overflow_approval = None
                    self._clear_plan_overflow_locked()
                if history["mode"] != "unlimited":
                    unit = "元" if history["mode"] == "cny" else " AFP"
                    self.log(
                        f"本章费用历史检查通过：前 {history['window_size']} 个已完成章节中 "
                        f"{history['over_limit']}/{history['confirm_at']} 章整章实际总费用超过 "
                        f"{history['limit']:g}{unit}；自动发送。"
                    )

        gate_context = (
            f"【结构化Canon账本】\n{canon_context}\n\n"
            f"【最近章节摘要】\n{self.recent_summaries(n, min(4, recent_count))}\n\n"
            f"【最近已生成的章节 Plan】\n{recent_plan_text}\n\n"
            f"{boundary_prompt[-5000:]}"
        )
        return self._generate_checked_plan(
            n, system, user, g, stage_contract, gate_context,
            canon_context=canon_context,
        )

    def draft_chapter(self, n, plan, task_card=""):
        self._stage(n, "正文生成", f"正在写第 {n} 章")
        cfg = self.config_loader(); g = cfg["generation"]
        task_card = task_card or self.chapter_task_card(n)
        stage_contract = self._saved_stage_contract(n)
        stage_contract.pop("next_contract", None)
        stage_contract_text = json.dumps(stage_contract, ensure_ascii=False, indent=2)
        boundary = self.previous_boundary_context(n, include_future_details=False)
        boundary_prompt = self._boundary_prompt(boundary)
        self._log_boundary_context("Draft", boundary)
        canon_context = self.canon_guard_context(n)
        memories = self.retrieve(plan, max_chapter=n-1)
        character_lock = self.character_lock(n, task_card=task_card, plan=plan)
        # The late-position character lock already contains the relevant base
        # cards and dynamic state.  Re-sending the entire, ever-growing cast file
        # dilutes the current chapter focus and wastes context.
        draft_character_seed = (
            "" if character_lock.strip()
            else self.read_story('characters_seed.md')
        )

        system = """你是中文长篇小说作家。当前章节任务卡和章节计划是本章剧情方向的硬约束。
严格遵守世界观、人物当前状态、知识边界、时间线和章节计划。
只输出小说正文，不解释创作过程，不擅自改变硬设定，不擅自把辅助悬念升级成主线。
大纲只锁定事实、因果和结果，不是正文句式或逐项验收表。把约束藏在自然行动、选择和后果里，不让人物替作者宣读规则或总结主题。
硬边界只在你的内部决策中生效。禁止为了证明合规而反复写“他没有多想/没有怀疑/这很普通/与他无关/他不知道/不是……而是……”；若某项边界无需进入场景，直接不写，不用否定句向读者报备。
最高结构约束：章节是事件/关系单元，不是自然日记；除非当前任务明确要求，禁止用完整作息撑满一章。"""

        marker_prompt = _read_prompt(
            self.root, "canon_dlc_marker.md"
        ).replace("{{CHAPTER4}}", f"{n:04d}")

        def build_user(state_text, outline_text, memory_rows, recent_count):
            ctx = self._stage_static_context(
                n, outline_text=outline_text, current_state_text=state_text,
                character_seed_text=draft_character_seed,
            ) + f"""
【最近章节摘要】
{self.recent_summaries(n, recent_count)}

【与本章计划最相关的长期记忆】
{self.format_memories(memory_rows)}
"""
            return ctx + boundary_prompt + f"""
{task_card}

【结构化Canon账本｜正文不可改写的既有事实】
{canon_context}

任何物品移动、数字变化、知识新增、决定改变和研究结论都必须从账本当前状态合法演变。
同一重要地点可以重访，但不得复用近期已关闭场景的道具组合、互动节拍和相同人物初始状态，
除非计划给出了明确新触发且本章产生显著不同的结果。

【第{n}章计划】
{plan}

【第{n}章阶段承接合同｜正文执行硬约束】
{stage_contract_text}

{character_lock}

请写第{n}章正文，目标约 {g.get('target_chapter_chars',4500)} 个汉字；该数字只是篇幅参考，不得为凑字数添加无作用作息。
要求：把当前事件链写完整、保持自然对话；“场景完整”不等于“把一天写完整”。不得让角色提前知道信息；保持伤病、物品、地点、关系和时间连续。
先执行阶段合同中的 entry_state、chapter_change、cut_point、carry_out，再展开正文；不要把合同字段逐条解释给读者。正文必须按事件/关系单元推进，不按自然日填满篇幅：直接从上一章交接点进入，不要先补写一个新早晨或完整作息。严格按计划和合同确定的时间跨度写；无变化的通勤、吃饭、回家、练功、作业和睡觉要压缩或跳过。除非当前任务必须，正文可以在事件或对话仍在进行时收束，不必回家、关灯或睡觉。
正文的场景骨架只能是“触发→行动/互动→变化/后果”。不得用“早上→白天→晚上”排列场景；起床、上学、放学、乘车、吃饭、回家、写作业、睡觉不得单独成场。若计划中出现完整日程，优先将其压缩成事件之间的一句转场。章末必须落在行动、对话、决定、关系余波、异常证据或现实后果上，不能用回家、关灯、睡觉或“明天再说”收束，除非当前任务明确要求该动作本身产生剧情变化。
章末必须保留合同中的 carry_out，不能提前替下一章完成尚未发生的结果。
正文完成后在内部快速核对数字、时间地点、物品归属、动作先后和证据—结论强度；只修正文，不输出检查过程。单次观测只能形成待验证线索，人物明确误判除外，但叙述必须保留其不确定性。
场景必须围绕具体问题推进，人物诉求允许冲突、误判和延迟理解；禁止重复“犯错—追问—准确自省—导师总结—记录评语”的培训案例结构。
如果最近章节的悬念与本章任务卡无关，只保持其未解决状态，不主动调查、解释、强化或新增同类悬念。

{marker_prompt}
"""

        focus = "\n".join(
            (self.current_chapter_outline(n), task_card, plan, character_lock)
        )
        user, _ = self._trim_canon_stage_prompt(
            n, "draft", system, focus, memories, build_user
        )

        with self.lock:
            self.status["chapter_chars"] = 0; self.status["char_per_sec"] = 0
        t0 = time.perf_counter()
        routing_context = task_card + "\n" + plan
        text = self._chat(
            "draft", system, user, g["temperatures"]["draft"], g["max_tokens"]["draft"],
            True, "draft", True, routing_context=routing_context
        )
        return text, time.perf_counter() - t0

    def review_chapter(self, n, plan, draft, task_card="", deep=False,
                       final_gate=False, prior_review=None, continuity_lock=""):
        stage_key = "deep_review" if deep else "review"
        stage_name = "深度审查" if deep else "结构化审查"
        self._stage(n, stage_name, f"{'深度复核' if deep else '检查'}第 {n} 章")
        cfg = self.config_loader(); g = cfg["generation"]
        task_card = task_card or self.chapter_task_card(n)
        stage_contract = self._saved_stage_contract(n)
        plan_gate = {}
        try:
            gate_record = json.loads(
                (self.root / "plans" / f"{int(n):04d}.contract_check.json").read_text(encoding="utf-8")
            )
            plan_gate = gate_record.get("selected") if isinstance(gate_record, dict) else {}
            if not isinstance(plan_gate, dict):
                plan_gate = {}
        except (OSError, ValueError, TypeError):
            plan_gate = {}
        stage_outline = self.plan_stage_outline_context(n, include_future_details=True)
        boundary = self.previous_boundary_context(n)
        boundary_prompt = self._boundary_prompt(boundary)
        self._log_boundary_context("Final Review" if final_gate else ("Deep Review" if deep else "Review"), boundary)
        canon_ledger = self.canon_ledger(n)
        canon_context = format_canon_ledger(
            canon_ledger, max_chars=self._continuity_config()["ledger_prompt_max_chars"]
        )
        memories = self.retrieve(plan + "\n" + draft[:6000], max_chapter=n-1)
        character_lock = self.character_lock(n, task_card=task_card, plan=plan)
        review_character_seed = (
            "" if character_lock.strip()
            else self.read_story('characters_seed.md')
        )
        local_findings = deterministic_boundary_findings(
            boundary.get("source_tail", ""), draft,
            previous_handoff=boundary.get("handoff"),
            current_task=task_card,
            next_task=boundary.get("future_boundary", ""),
            month=(lambda m: int(m.group(1)) if m else None)(
                re.search(r"(?<!\d)(1[0-2]|[1-9])月", self.outline_context(n))
            ),
        ) if n > 1 else []
        local_findings.extend(deterministic_canon_findings(draft, canon_ledger, plan=plan))
        for finding in local_findings:
            finding.setdefault("signature", self._deterministic_finding_signature(finding))
        local_findings_text = (
            json.dumps(local_findings, ensure_ascii=False, indent=2)
            if local_findings else "（无）"
        )
        quality_cfg = self._writing_quality_config()
        recent_fulltext = (
            recent_chapter_fulltexts(
                self.root, n, count=quality_cfg["recent_fulltext_chapters"],
                max_chars=quality_cfg["recent_fulltext_max_chars"],
            )
            if quality_cfg["soft_style_repetition"] else "（已关闭最近正文重复检查）"
        )

        system = """你是严格的长篇小说审稿人。当前章节任务卡与大纲是剧情方向最高依据。
除了连续性，还必须检查剧情偏航、未来内容提前消费、新主线/新秘密/新伏笔、悬疑类型膨胀和感情推进过快。
只依据提供资料检查，不凭空创造错误。必须输出一个 JSON 对象，不要 Markdown。
如果这是深度复核，请重点确认初审指出的剧情级风险是否真实存在，避免误判 MAJOR。"""

        review_instructions = f"""
【审查模式】
{'这是修订后的最终质量门：必须逐项确认原问题已修复、修订未引入新冲突、相邻边界仍成立、章末状态符合大纲且未提前消费下一章；仍有严重问题必须 needs_revision=true。' if final_gate else '这是常规候选审查。'}

【修订前问题（仅最终质量门使用）】
{json.dumps(prior_review or {}, ensure_ascii=False, indent=2) if final_gate else '（无）'}

【剧情偏航检查】
1. 是否创建当前章节大纲没有要求的新主线、新秘密、新组织或长期目标。
2. 是否把辅助悬疑/未解内容写成当前主要剧情，或主动调查、解释、强化本章不该推进的伏笔。
3. 是否提前消费未来章节、超自然设定或角色真实身份。
4. 是否让人物知道当前不应该知道的信息。
5. 是否让感情发展速度明显超出当前章节大纲。
6. 是否新增会影响长期剧情的秘密、警告、跟踪、监视、仪式、神秘人物/车辆等元素。
7. 是否与上一章事实矛盾，人物身份、性别、关系是否错误。
8. 是否为了制造结尾钩子而新增大纲外悬念。
9. 相邻章节边界：时间是否无说明倒退；地点是否无过渡复位；已离场人物是否重回原场景；已完成动作是否重新开始；是否大段重演；认知、伤势、物品、关系、身份是否回退；关闭场景是否无标记重开。
10. 区分明确标记的回忆、倒叙和多视角复现，不能机械误报合理回溯。

【章内一致性与论证强度检查｜逐项执行，不能因文风顺畅而跳过】
1. 数字：同一价格、数量、时长、日期和测量值前后是否一致；必须区分报价、成交价、预算和实付，不能把其中一个悄悄替换成另一个。
2. 时间与地点：动作是否发生在人物实际所在的时间地点；乘车、离场、到家、进屋等状态变化是否有必要过渡，不能在公交车上无标记地操作家中书桌或抽屉。
3. 物品归属：所有者、当前持有人、存放地点和消耗/遗失/损毁状态是否匹配；代词“东西”“它”也必须回指到实际物品，不能让没有持有它的人“留着”。
4. 动作顺序：取得、拆封、使用、归还、收纳等动作是否按可执行顺序发生；结果不能先于必要动作，已完成动作不能无说明再次开始。
5. 证据—结论：明确列出实际新增了多少样本、观测到了什么、最多能支持什么结论；单次/单条观测只能形成待验证线索，不能排除变量、确认因果或把“可能性高”改写为“就是”。
6. 审查输出如果判 PASS，意味着以上六项均已对照正文和结构化Canon实际检查，不能只复述大纲、计划或前一次Review的结论。

【约束静默化检查】
- 约束是作者侧边界，不是正文内容。正文不应反复用“没有多想、没有怀疑、只是普通、与他无关、他不知道、不是……而是……”向读者证明没有越界。
- 偶尔一次符合人物语气的否定句不是问题；只有这类合规说明连续出现、替代了自然动作和人物反应时，才判 NOTICEABLE 或 SEVERE。

【小说化检查】
1. 是否把大纲中的边界、规则或评价标准机械扩写成协议、表格、会议纪要、墙上标语或角色轮流确认。
2. 是否反复使用“犯错—追问—准确自省—导师总结—记录评语”的同构段落。
3. 是否让不同人物使用同一种项目管理或风险评估语言，缺少符合关系与性格的口语差异。
4. 是否在动作和结果已经表达清楚后，再由叙述者或角色总结“这就是”“这说明”“不是……而是……”等结论。
5. 是否用模糊的角色、目标、资源、位置等抽象术语代替当前作品已经提供的具体专业规则。

【章节边界与日记式展开检查】
- 一章只覆盖几个小时、只覆盖一天或跨越数日都可以，时间跨度本身不是问题；只检查它是否服务于本章事件和关系推进。
- 如果正文在没有大纲依据的情况下按“早上—上课—放学—回家—练功/写作业—睡觉”完整走流程，且主要篇幅没有人物选择、关系余波、现实阻力、能力应用或结果变化，判为 MINOR 重复/结构问题。
- 如果连续章节都以完整作息开场并以睡觉、关灯或“明天再说”收束，且本章可以通过时间跳跃或事件切片压缩，必须在 repetition 和 revision_instructions 中指出具体可压缩段落；不要仅以“节奏慢”泛泛评价。
- 不得因为章节恰好发生在一天内，或因为正文合理使用了日期标记，就机械要求作者跨日。
{_read_prompt(self.root, "canon_dlc_review.md")}

输出严格 JSON：
{{
  "needs_revision": true/false,
  "severity": "PASS/MINOR/MAJOR",
  "requires_full_rewrite": true/false,
  "confidence": "HIGH/MEDIUM/LOW",
  "risk_flags": {{
    "plot_drift": true/false,
    "future_leak": true/false,
    "new_mainline": true/false
  }},
  "continuity": ["..."],
  "character": ["..."],
  "knowledge": ["..."],
  "world": ["..."],
  "plot": ["..."],
  "plot_drift": [],
  "future_leak": [],
  "new_mainline": [],
  "style": ["..."],
  "repetition": ["..."],
  "chapter_logic": {{
    "numbers": {{"status":"PASS|ISSUE|NA|UNKNOWN","evidence":"有问题时给正文短证据"}},
    "time_place": {{"status":"PASS|ISSUE|NA|UNKNOWN","evidence":"有问题时给正文短证据"}},
    "item_ownership": {{"status":"PASS|ISSUE|NA|UNKNOWN","evidence":"有问题时给正文短证据"}},
    "action_order": {{"status":"PASS|ISSUE|NA|UNKNOWN","evidence":"有问题时给正文短证据"}},
    "evidence_conclusion": {{"status":"PASS|ISSUE|NA|UNKNOWN","evidence":"有问题时给正文短证据"}}
  }},
  "scene_sufficiency": {{"status":"SUFFICIENT|THIN|EMPTY|UNKNOWN","evidence":[],"reason":""}},
  "cross_chapter_repetition": {{"status":"CLEAR|NOTICEABLE|SEVERE|UNKNOWN","evidence":[],"reason":""}},
  "constraint_leakage": {{"status":"CLEAR|NOTICEABLE|SEVERE|UNKNOWN","evidence":[],"reason":""}},
  "issue_keys": ["稳定的问题键，只给必须修复的问题；同一问题跨轮必须使用同一个键"],
  "boundary_evidence": [{{"issue":"问题", "previous_quote":"上一章逐字证据", "current_quote":"本章逐字证据"}}],
  "revision_instructions": ["只列必须修的问题"]
}}

如果存在【连续性裁决已锁定】，必须检查锁定事实，不得再次提出与裁决相反的替代方案。

风险字段规则：
- risk_flags 是供程序判断是否升级 Deep Review 的唯一剧情风险布尔信号。
- 只有确认存在对应风险时才设为 true；“提及但不构成风险”“符合大纲”“未提前消费”等情况必须为 false。
- plot_drift / future_leak / new_mainline 三个说明数组只写实际存在的风险证据；不存在风险时必须输出 []，不要写“未发现风险”之类的说明。

判定规则：
- PASS：无必须修改的问题。
- MINOR：局部连续性/表达/重复问题，可在保留主体的情况下修复。
- MAJOR：剧情类型或主线明显跑偏、提前消费未来章节、大量新增大纲外悬疑/秘密、知识边界大面积错误、关键人物身份/性别/家庭等硬设定严重冲突、世界观硬规则严重冲突，或必须删除/重写多个核心场景。
- “小说化检查”不是可忽略的个人文风偏好：局部出现同构培训问答、协议化对白或重复结论时判为 MINOR；多个核心场景都被清单式结构支配、必须重搭场景链时判为 MAJOR。
- 网文质量分层：普通场景偏薄、一般重复和少量约束痕迹可以放行或写入提示；只有场景几乎没有推进、跨章低变化重演、或约束说明严重占据正文时才要求 MINOR 修订。不要用字数本身判定质量。
- severity=MAJOR 时 requires_full_rewrite 必须为 true，needs_revision 必须为 true。
不要因为个人文风偏好要求 revision。
"""

        def build_user(state_text, outline_text, memory_rows, recent_count):
            return self._stage_static_context(
                n, outline_text=outline_text, current_state_text=state_text,
                character_seed_text=review_character_seed,
            ) + boundary_prompt + f"""
{task_card}

【结构化Canon账本｜必须逐项核对】
{canon_context}

【相邻大纲边界】
上一章：{self.current_chapter_outline(max(1, n-1))}
当前章：{self.current_chapter_outline(n)}
下一章：{self.current_chapter_outline(n+1)}

【多章阶段视野｜仅用于检查当前章在事件链中的位置】
{stage_outline}

【最近摘要】
{self.recent_summaries(n, recent_count)}
【相关长期记忆】
{self.format_memories(memory_rows)}
【本章计划】
{plan}

{character_lock}

{continuity_lock}

【待审正文】
{draft}

【本地候选检查｜仅是待验证线索，不是既定结论】
{local_findings_text}
其中 severity=REVIEW 的项目必须结合上下文语义验证：若只是同一事项的后续推进、合理回溯或词语重合，必须忽略；若确属问题，写入对应问题数组和修订指令。
severity=MINOR/MAJOR 的本地项目已有结构化账本或正文证据支持，不得被模型PASS覆盖；MINOR局部修订，MAJOR按硬冲突处理。

【最近三章正文｜只用于低成本重复与风格同质化检查】
{recent_fulltext}
只在当前正文与最近正文同时出现相同场景骨架、相同人物初始状态、相同互动节拍且没有明显新变化时判为 SEVERE；仅地点相同、普通日常相似、合理呼应或词语重合都不是问题。该项主要用于提示，不要为了消除一般相似而强行改写。

{review_instructions}
"""

        focus = "\n".join(
            (
                self.current_chapter_outline(n),
                task_card,
                plan,
                draft,
                character_lock,
                continuity_lock,
            )
        )
        user, _ = self._trim_canon_stage_prompt(
            n, stage_key, system, focus, memories, build_user
        )

        raw = self._chat(stage_key, system, user, g["temperatures"]["review"], g["max_tokens"]["review"], True, stage_key, False, response_format={"type": "json_object"})
        obj = _json_obj(raw)
        if not isinstance(obj, dict):
            obj = {
                "needs_revision": True, "severity": "MAJOR", "requires_full_rewrite": True,
                "continuity": ["Review JSON 解析失败"], "revision_instructions": [raw[:2000]],
            }
        severity = str(obj.get("severity", "PASS")).upper()
        if severity not in {"PASS", "MINOR", "MAJOR"}:
            severity = "MAJOR" if obj.get("requires_full_rewrite") else ("MINOR" if obj.get("needs_revision") else "PASS")
        obj["severity"] = severity
        confidence = str(obj.get("confidence", "MEDIUM")).upper()
        if confidence not in {"HIGH", "MEDIUM", "LOW"}:
            confidence = "MEDIUM"
        contract_status = str(stage_contract.get("status") or "fallback").lower()
        contract_confidence = str(stage_contract.get("confidence") or "LOW").upper()
        gate_status = str(plan_gate.get("status") or "UNCERTAIN").upper()
        try:
            gate_score = int(plan_gate.get("score", 0) or 0)
        except (TypeError, ValueError):
            gate_score = 0
        strict_plan_verified = gate_status == "PASS" and gate_score >= 80
        contract_untrusted = contract_status != "complete" or contract_confidence == "LOW"
        if contract_untrusted:
            ceiling = "MEDIUM" if strict_plan_verified else "LOW"
            confidence_rank = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}
            if confidence_rank[confidence] > confidence_rank[ceiling]:
                confidence = ceiling
        obj["confidence"] = confidence
        obj["quality_provenance"] = {
            "stage_contract_status": contract_status,
            "stage_contract_confidence": contract_confidence,
            "plan_gate_status": gate_status,
            "plan_gate_score": gate_score,
            "strict_plan_verified": strict_plan_verified,
            "review_confidence_capped": contract_untrusted,
        }

        # V4.1.2: risk explanation arrays are human-readable evidence, not booleans.
        # Deep Review escalation must only use explicit risk_flags.  Older/malformed
        # responses that omit risk_flags fall back to all-false; MAJOR still escalates
        # independently, so we do not guess risk truth from natural-language arrays.
        raw_flags = obj.get("risk_flags")
        if not isinstance(raw_flags, dict):
            raw_flags = {}

        def _strict_bool(value):
            if isinstance(value, bool):
                return value
            if isinstance(value, (int, float)) and value in (0, 1):
                return bool(value)
            if isinstance(value, str):
                text = value.strip().lower()
                if text in {"true", "1", "yes", "y"}:
                    return True
                if text in {"false", "0", "no", "n", ""}:
                    return False
            return False

        obj["risk_flags"] = {
            "plot_drift": _strict_bool(raw_flags.get("plot_drift")),
            "future_leak": _strict_bool(raw_flags.get("future_leak")),
            "new_mainline": _strict_bool(raw_flags.get("new_mainline")),
        }

        if severity == "MAJOR":
            obj["needs_revision"] = True
            obj["requires_full_rewrite"] = bool(
                self.config_loader().get("writing_guardrails", {}).get("major_drift_full_rewrite", True)
            )
        else:
            obj.setdefault("needs_revision", False)
            obj.setdefault("requires_full_rewrite", False)
        for k in (
            "continuity", "character", "knowledge", "world", "plot",
            "plot_drift", "future_leak", "new_mainline", "style",
            "repetition", "revision_instructions",
        ):
            if not isinstance(obj.get(k), list):
                obj[k] = [] if obj.get(k) is None else [str(obj.get(k))]
        if not isinstance(obj.get("boundary_evidence"), list):
            obj["boundary_evidence"] = []
        quality_input = dict(obj)
        if not quality_cfg["light_scene_sufficiency"]:
            quality_input.pop("scene_sufficiency", None)
        if not quality_cfg["soft_style_repetition"]:
            quality_input.pop("cross_chapter_repetition", None)
        if not quality_cfg["silent_constraints"]:
            quality_input.pop("constraint_leakage", None)
        quality_checks, quality_blockers, quality_advisories = normalize_light_quality_checks(quality_input)
        obj["light_quality_checks"] = quality_checks
        obj["quality_advisories"] = quality_advisories
        if quality_blockers:
            for finding in quality_blockers:
                bucket = finding["bucket"]
                if bucket not in obj or not isinstance(obj[bucket], list):
                    obj[bucket] = []
                message = f"[轻量质量门/{finding['key']}] {finding['message']}"
                obj[bucket].append(message)
                obj["revision_instructions"].append(finding["message"])
            if str(obj.get("severity") or "PASS").upper() == "PASS":
                obj["severity"] = "MINOR"
            obj["needs_revision"] = True
            if str(obj.get("severity") or "MINOR").upper() != "MAJOR":
                obj["requires_full_rewrite"] = False
        soft_quality_present = (
            quality_checks["scene_sufficiency"]["status"] == "THIN"
            or quality_checks["cross_chapter_repetition"]["status"] == "NOTICEABLE"
            or quality_checks["constraint_leakage"]["status"] == "NOTICEABLE"
        )
        hard_review_buckets = (
            "continuity", "character", "knowledge", "world", "plot",
            "plot_drift", "future_leak", "new_mainline",
        )
        if (
            not quality_blockers
            and soft_quality_present
            and str(obj.get("severity") or "").upper() == "MINOR"
            and not any(obj.get(key) for key in hard_review_buckets)
            and not any(obj["risk_flags"].values())
        ):
            obj["quality_advisories"] = list(dict.fromkeys(
                obj["quality_advisories"]
                + [str(x) for x in (obj.get("style") or []) if str(x).strip()]
                + [str(x) for x in (obj.get("repetition") or []) if str(x).strip()]
                + [str(x) for x in (obj.get("revision_instructions") or []) if str(x).strip()]
            ))
            obj["severity"] = "PASS"
            obj["needs_revision"] = False
            obj["requires_full_rewrite"] = False
            obj["revision_instructions"] = []
        severity = str(obj.get("severity") or "PASS").upper()
        raw_issue_keys = obj.get("issue_keys")
        issue_keys = []
        if isinstance(raw_issue_keys, list):
            for value in raw_issue_keys:
                key = re.sub(r"[^a-z0-9_]+", "_", str(value).strip().lower()).strip("_")
                if key:
                    issue_keys.append(key)
        issue_keys.extend(finding["key"] for finding in quality_blockers)
        findings_text = " ".join(
            str(obj.get(k) or "") for k in (
                "continuity", "character", "knowledge", "world", "plot",
                "plot_drift", "future_leak", "new_mainline", "style",
                "repetition", "revision_instructions",
            )
        )
        def add_issue(key, terms):
            if any(term in findings_text for term in terms):
                issue_keys.append(key)
        if severity != "PASS" or bool(obj.get("needs_revision")):
            # Prefer the model's explicit stable keys.  Keyword inference is a
            # compatibility fallback only; scanning affirmative prose such as
            # “符合大纲要求、未发现重复” used to manufacture unrelated keys.
            if not issue_keys:
                add_issue("character_identity_continuity", ("身份", "服装", "出手者", "张哥", "灰色运动", "深蓝色", "人物混淆"))
                add_issue("escape_causality", ("脱身", "放行", "环境", "空隙", "轻敌"))
                add_issue("knowledge_boundary", ("知道", "认知边界", "提前知晓"))
                add_issue("world_rule", ("世界观", "能力", "硬规则"))
                add_issue("plot_alignment", ("大纲要求", "剧情偏航", "主线", "因果"))
                add_issue("prose_structure", ("清单式", "协议化", "机械总结", "同构", "小说化"))
                add_issue("repetition", ("重复段落", "重复表达", "模板化"))
            if not issue_keys:
                issue_keys.append("unclassified_revision")
        else:
            # A PASS response sometimes repeats rubric words in positive phrases
            # such as “未发现重复” or returns stale issue_keys.  Neither may
            # trigger conflict resolution or spend a revision round.
            issue_keys = []
            obj["needs_revision"] = False
            obj["requires_full_rewrite"] = False
        obj["issue_keys"] = list(dict.fromkeys(issue_keys))
        obj["_pre_deterministic_verdict"] = {
            "severity": str(obj.get("severity") or "PASS").upper(),
            "needs_revision": bool(obj.get("needs_revision")),
            "requires_full_rewrite": bool(obj.get("requires_full_rewrite")),
            "issue_keys": list(obj.get("issue_keys") or []),
        }
        obj["_review_candidate_signature"] = hashlib.sha256(
            str(draft or "").strip().encode("utf-8")
        ).hexdigest()[:24]
        obj["deterministic_boundary_findings"] = local_findings
        major_local_findings = [
            x for x in local_findings
            if str(x.get("severity") or "").upper() == "MAJOR"
        ]
        minor_local_findings = [
            x for x in local_findings
            if str(x.get("severity") or "").upper() == "MINOR"
        ]
        advisory_local_findings = [
            x for x in local_findings
            if str(x.get("severity") or "").upper() not in {"MAJOR", "MINOR"}
        ]
        obj["deterministic_advisories"] = advisory_local_findings
        if minor_local_findings and not major_local_findings:
            obj["needs_revision"] = True
            if str(obj.get("severity") or "PASS").upper() == "PASS":
                obj["severity"] = "MINOR"
            obj["continuity"].extend(
                f"[本地确定性检查/{x['code']}] {x['message']}；证据：{x['evidence']}"
                for x in minor_local_findings
            )
            obj["revision_instructions"].extend(x["message"] for x in minor_local_findings)
            obj["issue_keys"] = list(dict.fromkeys(
                list(obj.get("issue_keys") or [])
                + [f"deterministic_{str(x.get('code') or 'canon').lower()}" for x in minor_local_findings]
            ))
        if major_local_findings:
            obj["needs_revision"] = True
            obj["severity"] = "MAJOR"
            obj["requires_full_rewrite"] = True
            obj["continuity"].extend(
                f"[本地确定性检查/{x['code']}] {x['message']}；证据：{x['evidence']}"
                for x in major_local_findings
            )
            obj["revision_instructions"].extend(x["message"] for x in major_local_findings)
            obj["issue_keys"] = list(dict.fromkeys(
                list(obj.get("issue_keys") or [])
                + [f"deterministic_{str(x.get('code') or 'boundary').lower()}" for x in major_local_findings]
            ))
        return obj

    @staticmethod
    def _deterministic_finding_issue_key(finding):
        code = str((finding or {}).get("code") or "canon").strip().lower()
        return "deterministic_" + re.sub(r"[^a-z0-9_]+", "_", code).strip("_")

    @staticmethod
    def _deterministic_finding_signature(finding):
        finding = finding if isinstance(finding, dict) else {}
        stored = str(finding.get("signature") or "").strip().lower()
        if re.fullmatch(r"[0-9a-f]{24}", stored):
            return stored
        payload = json.dumps({
            "code": str(finding.get("code") or "").strip().upper(),
            "message": re.sub(r"\s+", "", str(finding.get("message") or "")),
            "evidence": re.sub(r"\s+", "", str(finding.get("evidence") or "")),
        }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]

    @staticmethod
    def _review_issue_signature(review, issue_key):
        """Bind a model issue disposition to the evidence that was arbitrated."""
        review = review if isinstance(review, dict) else {}
        candidate_signature = str(review.get("_review_candidate_signature") or "").strip().lower()
        if re.fullmatch(r"[0-9a-f]{24}", candidate_signature):
            payload = f"{str(issue_key or '').strip()}\0{candidate_signature}"
            return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]
        evidence_fields = (
            "continuity", "character", "knowledge", "world", "plot",
            "plot_drift", "future_leak", "new_mainline", "style",
            "repetition", "revision_instructions", "boundary_evidence",
            "light_quality_checks",
        )
        payload = {
            "issue_key": str(issue_key or "").strip(),
            "evidence": {field: review.get(field) for field in evidence_fields if review.get(field)},
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:24]

    def _apply_review_resolution(self, review, resolution):
        """Apply structured arbitration without hiding unrelated new failures."""
        if not isinstance(review, dict) or not isinstance(resolution, dict):
            return review
        dismissed = {
            str(key) for key in (resolution.get("dismissed_issue_keys") or [])
            if str(key).strip()
        }
        confirmed = {
            str(key) for key in (resolution.get("confirmed_issue_keys") or [])
            if str(key).strip()
        }
        mode = str(resolution.get("repair_mode") or "LOCAL").upper()
        if mode not in {"LOCAL", "FULL_REWRITE"}:
            mode = "LOCAL"

        out = dict(review)
        findings = list(out.get("deterministic_boundary_findings") or [])
        dismissed_signatures = {
            str(value).strip().lower()
            for value in (resolution.get("dismissed_finding_signatures") or [])
            if str(value).strip()
        }
        raw_issue_signatures = resolution.get("dismissed_issue_signatures")
        issue_signatures = {
            str(key): str(value).strip().lower()
            for key, value in (
                raw_issue_signatures.items() if isinstance(raw_issue_signatures, dict) else []
            )
            if str(key).strip() and str(value).strip()
        }
        removed = [
            finding for finding in findings
            if (
                self._deterministic_finding_issue_key(finding) in dismissed
                and self._deterministic_finding_signature(finding) in dismissed_signatures
            )
        ]
        remaining = [finding for finding in findings if finding not in removed]
        blocking_remaining = [
            finding for finding in remaining
            if str(finding.get("severity") or "").upper() in {"MAJOR", "MINOR"}
        ]
        protected_keys = {
            self._deterministic_finding_issue_key(finding) for finding in blocking_remaining
        }
        exact_model_dismissals = {
            key for key in dismissed
            if issue_signatures.get(key) == self._review_issue_signature(out, key)
        }
        removed_keys = {
            self._deterministic_finding_issue_key(finding) for finding in removed
        }
        effective_dismissed = (exact_model_dismissals | removed_keys) - protected_keys
        if removed:
            removed_messages = {str(finding.get("message") or "") for finding in removed}
            remaining_messages = {str(finding.get("message") or "") for finding in remaining}
            rendered_removed = {
                f"[本地确定性检查/{finding.get('code')}] {finding.get('message')}；证据：{finding.get('evidence')}"
                for finding in removed
            }
            out["continuity"] = [
                message for message in list(out.get("continuity") or [])
                if str(message) not in rendered_removed
            ]
            out["revision_instructions"] = [
                message for message in list(out.get("revision_instructions") or [])
                if not (str(message) in removed_messages and str(message) not in remaining_messages)
            ]
            out["deterministic_boundary_findings"] = remaining
            out["resolution_dismissed_findings"] = removed

            baseline = out.get("_pre_deterministic_verdict")
            if isinstance(baseline, dict):
                base_severity = str(baseline.get("severity") or "PASS").upper()
                if base_severity not in {"PASS", "MINOR", "MAJOR"}:
                    base_severity = "MAJOR"
                out["severity"] = base_severity
                out["needs_revision"] = bool(baseline.get("needs_revision"))
                out["requires_full_rewrite"] = bool(baseline.get("requires_full_rewrite"))
                issue_keys = [
                    key for key in list(baseline.get("issue_keys") or [])
                    if str(key) not in effective_dismissed
                ]
                major_remaining = [
                    finding for finding in remaining
                    if str(finding.get("severity") or "").upper() == "MAJOR"
                ]
                minor_remaining = [
                    finding for finding in remaining
                    if str(finding.get("severity") or "").upper() == "MINOR"
                ]
                issue_keys.extend(self._deterministic_finding_issue_key(x) for x in major_remaining + minor_remaining)
                out["issue_keys"] = list(dict.fromkeys(issue_keys))
                if major_remaining:
                    out["severity"] = "MAJOR"
                    out["needs_revision"] = True
                    out["requires_full_rewrite"] = True
                elif minor_remaining:
                    out["needs_revision"] = True
                    if out["severity"] == "PASS":
                        out["severity"] = "MINOR"
                    if out["severity"] != "MAJOR":
                        out["requires_full_rewrite"] = False
            else:
                out["issue_keys"] = [
                    key for key in list(out.get("issue_keys") or []) if str(key) not in effective_dismissed
                ]

        original_keys = {str(key) for key in (out.get("issue_keys") or []) if str(key).strip()}
        out["issue_keys"] = [
            key for key in list(out.get("issue_keys") or [])
            if str(key) not in effective_dismissed
        ]
        current_keys = {str(key) for key in (out.get("issue_keys") or []) if str(key).strip()}
        if original_keys and not current_keys and original_keys.issubset(effective_dismissed):
            out["severity"] = "PASS"
            out["needs_revision"] = False
            out["requires_full_rewrite"] = False
            out["revision_instructions"] = []

        covered_by_resolution = bool(current_keys) and current_keys.issubset(confirmed)
        if covered_by_resolution and mode == "LOCAL":
            out["needs_revision"] = True
            out["requires_full_rewrite"] = False
            if str(out.get("severity") or "PASS").upper() == "MAJOR":
                out["severity"] = "MINOR"
        elif covered_by_resolution and mode == "FULL_REWRITE":
            out["needs_revision"] = True
            out["requires_full_rewrite"] = True
            out["severity"] = "MAJOR"

        out["applied_conflict_resolution"] = {
            "repair_mode": mode,
            "confirmed_issue_keys": sorted(confirmed),
            "dismissed_issue_keys": sorted(dismissed),
            "dismissed_finding_signatures": sorted(dismissed_signatures),
            "dismissed_issue_signatures": dict(sorted(issue_signatures.items())),
        }
        return out

    def resolve_review_conflicts(self, n, plan, draft, reviews, task_card="", issue_keys=None):
        """Choose one canon-consistent repair when the same issue repeats."""
        keys = list(dict.fromkeys(str(x) for x in (issue_keys or []) if str(x).strip()))
        if not keys:
            return None
        cfg = self.config_loader(); g = cfg["generation"]
        boundary = self.previous_boundary_context(n)
        self._stage(n, "连续性裁决", f"第 {n} 章重复问题裁决")
        system = """你是长篇小说的连续性冲突裁决编辑。你不写正文，只解决审查意见之间的反复冲突。
必须以上一章交接事实、当前章节大纲和任务卡为最高依据，从候选方案中选择唯一可执行方案。
不得输出“方案A或方案B都可以”。如果资料不足以唯一判断，必须标记 ambiguous=true，不得编造事实。
只能处置【重复问题键】中列出的键，不得改名、替换或新增问题键。
必须逐键标记 CONFIRMED（真实问题）或 DISMISSED（证据不支持/误报），并明确采用 LOCAL 或 FULL_REWRITE 修订模式。
输出严格 JSON，不要 Markdown。"""
        user = f"""{self._boundary_prompt(boundary)}

【当前章节任务卡】
{task_card}
【当前章节大纲】
{self.current_chapter_outline(n)}
【相邻章节大纲】
上一章：{self.current_chapter_outline(max(1, n-1))}
下一章：{self.current_chapter_outline(n+1)}
【本章计划】
{plan}
【重复问题键】
{json.dumps(keys, ensure_ascii=False)}
【最近审查结果】
{json.dumps(reviews[-4:], ensure_ascii=False, indent=2)}
【待裁决候选】
{draft}

输出：
{{"ambiguous":true/false,"issue_dispositions":[{{"issue_key":"必须原样来自重复问题键","status":"CONFIRMED|DISMISSED","reason":"证据理由"}}],"repair_mode":"LOCAL|FULL_REWRITE","decision":"唯一裁决结论","locked_facts":["修订后必须保持的事实"],"acceptance_checks":["最终审查必须确认的结果"]}}"""
        raw = self._chat(
            "deep_review", system, user, g["temperatures"]["review"],
            g["max_tokens"]["review"], True, "conflict_resolution", False,
            response_format={"type": "json_object"},
        )
        result = _json_obj(raw)
        if not isinstance(result, dict):
            return None
        requested = list(dict.fromkeys(keys))
        dispositions = {}
        raw_dispositions = result.get("issue_dispositions")
        if isinstance(raw_dispositions, list):
            for item in raw_dispositions:
                if not isinstance(item, dict):
                    continue
                key = str(item.get("issue_key") or "").strip()
                status = str(item.get("status") or "").strip().upper()
                if key in requested and status in {"CONFIRMED", "DISMISSED"}:
                    dispositions[key] = {
                        "issue_key": key,
                        "status": status,
                        "reason": str(item.get("reason") or "").strip(),
                    }
        for key in requested:
            dispositions.setdefault(key, {
                "issue_key": key,
                "status": "CONFIRMED",
                "reason": "裁决未明确标记，按真实问题保守处理",
            })
        result["issue_keys"] = requested
        result["issue_dispositions"] = [dispositions[key] for key in requested]
        result["confirmed_issue_keys"] = [
            key for key in requested if dispositions[key]["status"] == "CONFIRMED"
        ]
        result["dismissed_issue_keys"] = [
            key for key in requested if dispositions[key]["status"] == "DISMISSED"
        ]
        latest_review = reviews[-1] if reviews and isinstance(reviews[-1], dict) else {}
        result["dismissed_finding_signatures"] = sorted({
            self._deterministic_finding_signature(finding)
            for finding in (latest_review.get("deterministic_boundary_findings") or [])
            if self._deterministic_finding_issue_key(finding) in result["dismissed_issue_keys"]
        })
        result["dismissed_issue_signatures"] = {
            key: self._review_issue_signature(latest_review, key)
            for key in result["dismissed_issue_keys"]
        }
        repair_mode = str(result.get("repair_mode") or "LOCAL").strip().upper()
        result["repair_mode"] = repair_mode if repair_mode in {"LOCAL", "FULL_REWRITE"} else "LOCAL"
        result["ambiguous"] = bool(result.get("ambiguous", False))
        result["decision"] = str(result.get("decision") or "").strip()
        result["locked_facts"] = [str(x) for x in (result.get("locked_facts") or []) if str(x).strip()]
        result["acceptance_checks"] = [str(x) for x in (result.get("acceptance_checks") or []) if str(x).strip()]
        if (
            result["ambiguous"] or not result["decision"]
            or (result["confirmed_issue_keys"] and not result["locked_facts"])
        ):
            self.write(
                f"reviews/{n:04d}.conflict_resolution.json",
                json.dumps(result, ensure_ascii=False, indent=2),
            )
            return result
        lock_parts = [
            "【连续性裁决已锁定】",
            f"修订模式：{result['repair_mode']}",
            f"裁决：{result['decision']}",
        ]
        if result["locked_facts"]:
            lock_parts.append("必须保持：\n" + "\n".join(f"- {x}" for x in result["locked_facts"]))
        if result["acceptance_checks"]:
            lock_parts.append("最终检查：\n" + "\n".join(f"- {x}" for x in result["acceptance_checks"]))
        if result["dismissed_issue_keys"]:
            lock_parts.append("已裁定为误报、不得再次阻断：\n" + "\n".join(
                f"- {key}" for key in result["dismissed_issue_keys"]
            ))
        result["lock_text"] = "\n".join(lock_parts)
        self.write(
            f"reviews/{n:04d}.conflict_resolution.json",
            json.dumps(result, ensure_ascii=False, indent=2),
        )
        return result

    def revise_chapter(self, n, plan, draft, review, round_no, task_card="", continuity_lock=""):
        self._stage(n, "修订", f"正在修订第 {n} 章（第 {round_no} 轮）")
        cfg = self.config_loader(); g = cfg["generation"]
        task_card = task_card or self.chapter_task_card(n)
        stage_contract = self._saved_stage_contract(n)
        stage_contract.pop("next_contract", None)
        stage_contract_text = json.dumps(stage_contract, ensure_ascii=False, indent=2)
        boundary = self.previous_boundary_context(n, include_future_details=False)
        boundary_prompt = self._boundary_prompt(boundary)
        self._log_boundary_context("Revision", boundary)
        canon_context = self.canon_guard_context(n)
        review_text = json.dumps(review, ensure_ascii=False, indent=2)
        memories = self.retrieve(plan + "\n" + review_text, max_chapter=n-1)
        full_rewrite = bool(review.get("requires_full_rewrite"))
        regression_note = str(review.get("_regression_retry") or "").strip()
        character_lock = self.character_lock(n, task_card=task_card, plan=plan)
        revision_character_seed = (
            "" if character_lock.strip()
            else self.read_story('characters_seed.md')
        )

        if full_rewrite:
            system = """你是小说剧情纠偏重写编辑。上一版正文发生剧情级偏航。
必须以当前章节硬任务卡和章节计划为最高依据，从头重写本章。原正文只用于保留没有冲突的既有事实，不得保留导致跑偏的场景链。
删除大纲未要求的新主线、新秘密、新组织、过度悬疑、提前超自然展开、提前感情推进和未来章节内容。
重写后的章节仍按事件或关系推进单元组织，不按自然日填满：不得默认从早写到晚、补齐通勤作息和睡觉，也不得为了当天闭环提前结束；按本章事件需要决定时间跨度，并可在原有事件或关系余波处收章。
只输出重写后的完整正文，不解释。
""" + "\n" + _read_prompt(self.root, "canon_dlc_revision_full.md")
            rewrite_note = "【修订模式】MAJOR 剧情偏航：整章纠偏重写。不要只改几个词或保留错误场景骨架。"
        else:
            system = """你是小说局部修订编辑。只修复结构化审查明确指出的问题。
未被 revision_instructions 点名的场景、事件、因果、人物出场、信息揭示、动作结果、段落顺序和结尾以前的正文必须保留，不得改写成另一套章节方案。
尤其禁止为了修改一个结尾、几句对话或局部重复而删除已经通过审查的核心事件。当前章节硬任务卡和章节计划优先于模型自行发挥。
如果审查明确指出日记式作息重复，可以合并或删除无变化的通勤、作息和睡觉过渡，并把章末移到原有事件、决定或关系余波；不得借此新增剧情或强行跨日。
输出完整正文，但实际改动范围必须局部且可由 revision_instructions 逐项解释。
""" + "\n" + _read_prompt(self.root, "canon_dlc_revision_minor.md")
            rewrite_note = "【修订模式】MINOR/普通修订：只修必须问题，保留无问题内容。"

        def build_user(state_text, outline_text, memory_rows, recent_count):
            return self._stage_static_context(
                n, outline_text=outline_text, current_state_text=state_text,
                character_seed_text=revision_character_seed,
            ) + boundary_prompt + f"""
{task_card}

【结构化Canon账本｜修订不可改写的既有事实】
{canon_context}

任何物品移动、数字变化、知识新增、决定改变和研究结论都必须从账本当前状态合法演变。
局部修订不能为了消除一句冲突而制造新的跨章冲突；整章重写也不得回退既有Canon。

【最近摘要】
{self.recent_summaries(n, recent_count)}

【相关长期记忆】
{self.format_memories(memory_rows)}
【章节计划】
{plan}
【阶段承接合同｜修订后必须保持】
{stage_contract_text}
【原正文】
{draft}
【结构化审查】
{review_text}

{character_lock}

{continuity_lock}

{rewrite_note}
{('【回退恢复要求】' + regression_note) if regression_note else ''}
修订后仍必须保持合同中的 entry_state、chapter_change、cut_point、carry_out；不得把完整作息或回家睡觉恢复成默认章尾。
输出修订后的第{n}章完整正文。
"""

        focus = "\n".join(
            (
                self.current_chapter_outline(n),
                task_card,
                plan,
                review_text,
                draft,
                character_lock,
                continuity_lock,
            )
        )
        user, _ = self._trim_canon_stage_prompt(
            n, "revision", system, focus, memories, build_user
        )

        with self.lock:
            self.status["chapter_chars"] = 0; self.status["char_per_sec"] = 0
        t0 = time.perf_counter()
        routing_context = task_card + "\n" + plan + "\n" + review_text
        temperature = g["temperatures"]["revise"] if full_rewrite else min(
            0.25, float(g["temperatures"]["revise"])
        )
        out = self._chat(
            "revision", system, user, temperature, g["max_tokens"]["revise"],
            True, "revision", True, routing_context=routing_context
        )
        return out, time.perf_counter() - t0

    def _memory_canon_text(self, text):
        return re.sub(r"<DLC_SCENE\b[^>]*?/?>", "", text or "", flags=re.I).strip()

    def summarize(self, n, final):
        """Legacy single-purpose summary call, retained for compatibility/tools."""
        self._stage(n, "生成摘要", f"整理第 {n} 章摘要")
        g = self.config_loader()["generation"]
        system = "你是长篇小说记忆整理员。摘要用于后续写作，不做文学评论。"
        canon_text = self._memory_canon_text(final)
        user = f"""为第{n}章生成结构化摘要。
【正文】
{canon_text}
必须记录：关键事件、角色知道/不知道的信息、关系变化、伤病/死亡/物品变化、地点时间、新增/回收伏笔、下一章必须继承的状态。
"""
        return self._chat("summary", system, user, g["temperatures"].get("memory", 0.15), g["max_tokens"].get("summary", 1800), True, "summary", False)

    def extract_memories(self, n, final, summary):
        """Legacy single-purpose memory call, retained as a recovery path."""
        self._stage(n, "写入记忆库", f"提取第 {n} 章长期记忆")
        g = self.config_loader()["generation"]
        system = """你负责提取小说长期记忆。只输出 JSON 对象 {"memories": [...]}，不要 Markdown。
每条一个原子事实。kind 只能用 character_state, relationship, knowledge_state, item_state, location_state, event, fact, hook。
规范字段：
- character_state: entity, attribute, content
- relationship: entity, related_entity, dimension, content
- knowledge_state: entity, fact_id, content
- item_state: entity, state_key, content
- location_state: entity, state_key, content
- hook: entity, hook_id, content, status=active|resolved
- event/fact: entity, key, content
每条都含 importance 1-5；非 hook 默认 status=active。
不要创造正文不存在的信息。稳定标识应短、语义固定。"""
        canon_text = self._memory_canon_text(final)
        user = f"""【第{n}章摘要】
{summary}
【第{n}章正文】
{canon_text}
提取适合进入长期记忆数据库的记录。DLC 标记与 DLC 文件都不是 Canon 事实，不得写入长期记忆。"""
        raw = self._chat("memory", system, user, g["temperatures"]["memory"], g["max_tokens"]["memory"], True, "memory", False, response_format={"type": "json_object"})
        data = _json_obj(raw, {})
        if isinstance(data, list):
            records = data
        elif isinstance(data, dict):
            records = data.get("memories", [])
        else:
            records = []
        if not isinstance(records, list):
            self.log("长期记忆 JSON 解析失败，本章仍已保存。")
            self.write(f"logs/memory_raw_{n:04d}.txt", raw)
            return []
        return self.db.add_memories(records, n)

    @staticmethod
    def _key_semantic_label(key):
        """Strip the canonical type prefix and punctuation from a state key."""
        s = str(key or "").strip().lower()
        if ":" in s:
            s = s.split(":", 1)[1]
        s = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", " ", s)
        return re.sub(r"\s+", " ", s).strip()

    @staticmethod
    def _key_labels_similar(a, b):
        """Similarity gate for state-key retirement proposals."""
        na, nb = (
            NovelAgent._key_semantic_label(a),
            NovelAgent._key_semantic_label(b),
        )
        if not na or not nb:
            return False
        if na == nb:
            return True
        if na in nb or nb in na:
            return True
        return difflib.SequenceMatcher(None, na, nb).ratio() >= 0.6

    def _memory_state_registry(self, n, focus_text, max_chars=32000):
        """
        Collect existing stable state keys for entities appearing in the
        current Canon text. Group keys by entity and state kind so the memory
        model can reuse existing IDs instead of creating synonyms.
        """
        focus = str(focus_text or "")
        snapshot = self.db.state_as_of(max(0, int(n) - 1))
        rows = list(snapshot.get("states", []) or [])
        rows += list(snapshot.get("hooks", []) or [])
        if not rows:
            return "（无现有状态键；新建 key 时请保持规范字段与稳定标识）"
        groups = {}
        for row in rows:
            ent = str(row.get("entity", "") or "").strip()
            kind = str(row.get("kind", "") or "").strip()
            key = str(row.get("key", "") or "").strip()
            if not ent or not key:
                continue
            if ent in focus:
                groups.setdefault(ent, {}).setdefault(kind, []).append(key)
        if not groups:
            return "（当前正文未匹配到已有状态实体；新建 key 时请保持规范字段与稳定标识）"
        lines = []
        for ent in sorted(groups):
            for kind in sorted(groups[ent]):
                keys = sorted(set(groups[ent][kind]))
                lines.append(f"- {ent} / {kind}: " + ", ".join(keys))
        rendered = "\n".join(lines)
        if len(rendered) > max_chars:
            rendered = rendered[:max_chars].rstrip() + "\n…（注册表已截断）"
        return rendered

    def _validated_state_retirements(self, n, raw_groups, new_records):
        """
        Validate model-proposed alias merges. Only accept exact existing keys
        with the same entity/kind, a valid surviving key and a sufficiently
        similar key label. Return obsolete tombstone records.
        """
        if not isinstance(raw_groups, list):
            return []
        snapshot = self.db.state_as_of(max(0, int(n) - 1))
        active = {}
        for row in snapshot.get("states", []) or []:
            ent = str(row.get("entity", "") or "").strip()
            kind = str(row.get("kind", "") or "").strip()
            key = str(row.get("key", "") or "").strip()
            if not ent or not key:
                continue
            active.setdefault((ent, kind), set()).add(key)
        # A kept key may be one the current chapter creates/updates.
        created = {}
        for raw in new_records or []:
            if not isinstance(raw, dict):
                continue
            try:
                norm = normalize_memory_record(raw)
            except Exception:
                continue
            ent = str(norm.get("entity", "") or "").strip()
            kind = str(norm.get("kind", "") or "").strip()
            key = str(norm.get("key", "") or "").strip()
            if not ent or not key:
                continue
            created.setdefault((ent, kind), set()).add(key)

        tombs = []
        for group in raw_groups:
            if not isinstance(group, dict):
                continue
            kind = str(group.get("kind", "") or "").strip()
            entity = str(group.get("entity", "") or "").strip()
            keep_key = str(group.get("keep_key", "") or "").strip()
            retire_keys = group.get("retire_keys")
            if kind not in STATE_KINDS or not entity or not keep_key:
                continue
            if not isinstance(retire_keys, list):
                continue
            marker = (entity, kind)
            if (keep_key not in active.get(marker, set())
                    and keep_key not in created.get(marker, set())):
                continue
            for old_key in retire_keys:
                old_key = str(old_key or "").strip()
                if not old_key or old_key == keep_key:
                    continue
                if old_key not in active.get(marker, set()):
                    continue
                if not self._key_labels_similar(keep_key, old_key):
                    continue
                tombs.append({
                    "kind": kind,
                    "entity": entity,
                    "key": old_key,
                    "content": f"(状态键维护：{old_key} 已并入保留键 {keep_key})",
                    "importance": 1,
                    "status": "obsolete",
                })
                if len(tombs) >= 80:
                    return tombs
        return tombs

    def summarize_and_extract_memories(self, n, final):
        """Build Summary, Memory candidates, and Handoff in one Flash request."""
        self._stage(n, "摘要 + 记忆 + 交接", f"一次整理第 {n} 章摘要、长期记忆与交接")
        g = self.config_loader()["generation"]
        canon_text = self._memory_canon_text(final)
        continuity_cfg = self._continuity_config()
        source_tail = extract_source_tail(canon_text, continuity_cfg["source_tail_chars"])
        system = """你是长篇小说 Canon 记忆整理员。一次完成“章节摘要”“长期记忆提取”和“章末交接状态提取”。
只依据提供的 Canon 正文，不做文学评论，不创造正文不存在的信息。
只输出严格 JSON 对象，不要 Markdown，不要代码围栏。
输出结构必须是：
{
  "summary": "结构化章节摘要",
  "memories": [ ... ],
  "state_retirements": [
    {
      "kind": "character_state",
      "entity": "人物",
      "keep_key": "继续保留的精确key",
      "retire_keys": ["待退休的精确key"]
    }
  ],
  "handoff": {
    "chapter_no": 1,
    "structured_complete": true,
    "end_time": "准确时间；无法确认写 unknown",
    "end_location": "章末地点；无法确认写 unknown",
    "present_characters": [],
    "last_actions": [],
    "completed_events": [],
    "ongoing_events": [],
    "new_information": [],
    "state_changes": [],
    "scene_closed": true/false/"unknown",
    "next_start": "下一章必须从何处承接",
    "do_not_repeat": [],
    "future_boundaries": [],
    "uncertainties": [],
    "item_states": [{"item_id":"稳定ID","name":"物品名","aliases":[],"owner":"所有者","holder":"当前持有人","location":"当前地点","quantity":"数量","condition":"状态","status":"active|consumed|lost|destroyed","evidence":"正文逐字短证据"}],
    "numeric_facts": [{"fact_id":"稳定ID","subject":"数字所描述的事实","value":"最终数值","unit":"单位","aliases":[],"kind":"price|count|time|measurement|fact","status":"active|superseded","evidence":"正文逐字短证据"}],
    "knowledge_states": [{"knowledge_id":"人物_事实稳定ID","character":"人物","fact":"已经知道或明确不知道的事实","fact_terms":[],"knows":true,"status":"active|superseded","evidence":"正文逐字短证据"}],
    "active_decisions": [{"decision_id":"稳定ID","character":"人物","decision":"仍约束后文的决定或承诺","change_requires":"改变该决定所需的新触发或明确过程","status":"active|revoked|resolved","evidence":"正文逐字短证据"}],
    "evidence_claims": [{"claim_id":"稳定ID","subject":"研究问题","observation":"实际观测","conclusion":"当前最多能得出的结论","sample_count":1,"confidence":"CONFIRMED|HIGH|MEDIUM|LOW|UNKNOWN","limits":"证据限制和替代解释","status":"active|superseded","evidence":"正文逐字短证据"}],
    "scene_signatures": [{"scene_id":"本章场景稳定ID","location":"具体地点","characters":[],"entry_trigger":"进入原因","purpose":"即时目标","props":[],"beats":[],"outcome":"结束变化","closed":true}]
  }
}

summary 必须记录：关键事件、角色知道/不知道的信息、关系变化、伤病/死亡/物品变化、地点时间、新增/回收伏笔、下一章必须继承的状态。

memories 中每条必须是一个原子事实。kind 只能使用：character_state, relationship, knowledge_state, item_state, location_state, event, fact, hook。
规范字段：
- character_state: entity, attribute, content
- relationship: entity, related_entity, dimension, content
- knowledge_state: entity, fact_id, content
- item_state: entity, state_key, content
- location_state: entity, state_key, content
- hook: entity, hook_id, content, status=active|resolved
- event/fact: entity, key, content
每条都含 importance 1-5；非 hook 默认 status=active。
稳定标识应短、语义固定。
state_retirements 是状态键维护指令，只用于合并“同义或已被替代”的旧 key：
- 只能引用【现有状态键注册表】中出现、或本章 memories 新建的精确 key，且保持同一 kind 与 entity；keep_key 为继续保留的精确 key，retire_keys 为待退休的精确 key。
- 同一状态维度必须优先复用已有 key，禁止仅为换措辞而新建同义 key。
- 每章最多退休 80 个 key；历史记录不会删除，只会以 obsolete 状态退出活动账本。
DLC_SCENE 标记和 DLC 文件均不是 Canon 事实，不得写入 summary 或 memories。

信息密度规则：
- Summary 只记录本章中“后续章节真正需要知道或继承”的内容；普通场景铺陈、无后续影响的动作/对白/环境细节不要写入摘要。
- Memory 只提取长期有效、未来可能需要检索的事实；短暂情绪、一次性动作、普通日常细节、已经被更明确新状态覆盖的旧状态不要写入长期记忆。
- 同一事实不要换一种措辞重复记录；若正文只是再次确认已有事实，不要新增重复 memory。
- summary 建议控制在 1200 个中文字符以内；优先压缩措辞，不要为了凑长度复述正文。
- memories 最多 12 条，可以为 0 条；只保留后续真正可能检索的最高价值事实。
- 每条 memory 只表达一个原子事实，content 尽量简洁，不解释“为什么要记录”。
- 记忆数量不设下限，宁缺毋滥；不要为了“看起来完整”而凑条目。
- 对时间线、人物知识边界、关系状态、伤病/物品/地点变化以及真正持续的伏笔，仍应完整保留。"""
        system += """

handoff 是独立短期交接层，不能用 summary 代替：
- 必须精确记录章末时间、地点、在场人物、最后动作、已完成与未完成事件、新获信息、人物/伤势/物品/关系变化、场景是否关闭、下一章承接点、不得重复内容和不得提前消费的未来任务。
- end_time 只记录正文最后明确出现的时间点；scene_closed 只描述具体场景是否收束，不能把任一字段推断成“本章必须在当天结束”或“下一章必须从第二天开始”。
- 普通动作与一次性场景状态即使不适合 Summary/Memory，只要影响下一章开场，也必须进入 handoff。
- 不确定的信息必须写 unknown 并列入 uncertainties，绝对不能补写正文没有的事实。
- handoff.chapter_no 必须等于当前章节号。
- structured_complete 必须为 true。六类结构化字段必须全部返回；没有变化时使用空数组，禁止省略。
- item_states、numeric_facts、knowledge_states、active_decisions、evidence_claims 只记录本章新增或改变的Canon状态；稳定ID后续更新时必须复用。
- numeric_facts 只记录后文必须保持一致的最终数字，不记录无关环境数字。报价和成交价必须使用不同fact_id。
- evidence_claims 必须区分观测和结论；一次或单条样本不能标为CONFIRMED，也不能写成排除因果。
- scene_signatures 至少一条，记录主要场景的地点、人物、进入原因、道具、互动节拍、结果和是否关闭；不要把普通修辞当道具。
"""
        state_registry = self._memory_state_registry(n, canon_text)
        user = f"""请整理第{n}章。
【第{n}章 Canon 正文】
{canon_text}

【本章涉及实体的现有状态键注册表】
{state_registry}

【作者给出的后续任务边界】
{self.future_task_boundary(n)}

一次返回 summary、memories 与 handoff。handoff.future_boundaries 只能从上述作者边界提取，禁止自行编造。不要为了记忆数量而重复同一事实。"""
        # Combined output needs room for both products. This is only a ceiling; billing is based on actual tokens.
        max_tokens_cfg = g.get("max_tokens", {})
        configured_combined_max = int(max_tokens_cfg.get(
            "summary_memory",
            int(max_tokens_cfg.get("summary", 1800)) + int(max_tokens_cfg.get("memory", 2600)),
        ))
        # V4.3: only Summary+Memory gets a larger ceiling. Other stage budgets stay unchanged.
        combined_max = max(8000, configured_combined_max)
        retry_max = max(12000, combined_max)
        try:
            raw = self._chat(
                "summary", system, user,
                g.get("temperatures", {}).get("memory", 0.15),
                combined_max,
                True, "summary_memory", False,
                response_format={"type": "json_object"},
            )
        except ProviderLengthError as e:
            partial = str(getattr(e, "content", "") or "")
            if partial:
                self.write(f"logs/summary_memory_length_{n:04d}_{combined_max}.txt", partial)
                self.log(f"已保存本次被截断的摘要+记忆原始输出：logs/summary_memory_length_{n:04d}_{combined_max}.txt")
            if retry_max <= combined_max:
                reason = f"摘要+记忆+交接达到输出上限 {combined_max}"
                self.log(reason + "；使用保留原文末尾的降级交接并停止批量继续。")
                return f"第{n}章摘要提取失败，须人工复核。", [], degraded_handoff(n, source_tail, reason), reason
            self.log(f"摘要+记忆达到输出上限 {combined_max}，自动精简重试 max_tokens={retry_max}。")
            retry_user = user + """

【长度异常后的强制精简要求】
上一轮输出异常过长并达到长度上限。本轮不得延续上一轮的展开方式，必须重新从 Canon 正文提炼：
- summary 控制在约 1200 个中文字符以内，只保留后续章节必须继承的信息；
- memories 最多 12 条，可以为 0 条，只选长期有效且未来可能检索的最高价值事实；
- 禁止重复同一事实、禁止复述普通场景、禁止解释记录理由；
- 每条 memory 只写一个简短原子事实；
- 只输出规定 JSON 对象。
"""
            try:
                raw = self._chat(
                    "summary", system, retry_user,
                    g.get("temperatures", {}).get("memory", 0.15),
                    retry_max,
                    True, "summary_memory_retry", False,
                    response_format={"type": "json_object"},
                )
            except ProviderLengthError as e2:
                partial2 = str(getattr(e2, "content", "") or "")
                if partial2:
                    self.write(f"logs/summary_memory_length_{n:04d}_{retry_max}.txt", partial2)
                    self.log(f"已保存重试仍被截断的原始输出：logs/summary_memory_length_{n:04d}_{retry_max}.txt")
                reason = f"摘要+记忆+交接精简重试仍达到输出上限 {retry_max}"
                self.log(reason + "；使用保留原文末尾的降级交接并停止批量继续。")
                return f"第{n}章摘要提取失败，须人工复核。", [], degraded_handoff(n, source_tail, reason), reason
        data = _json_obj(raw, {})
        summary = data.get("summary", "") if isinstance(data, dict) else ""
        records = data.get("memories", []) if isinstance(data, dict) else []
        raw_retirements = data.get("state_retirements") if isinstance(data, dict) else None
        raw_handoff = data.get("handoff") if isinstance(data, dict) else None
        if not isinstance(summary, str) or not summary.strip() or not isinstance(records, list):
            reason = "摘要+记忆+交接 JSON 解析失败"
            self.log(reason + "；不写入长期记忆，保留确定性正文末尾并停止批量继续。")
            self.write(f"logs/summary_memory_raw_{n:04d}.txt", raw)
            fallback_summary = summary.strip() if isinstance(summary, str) and summary.strip() else f"第{n}章摘要提取失败，须人工复核。"
            return fallback_summary, [], degraded_handoff(n, source_tail, reason), reason
        summary = summary.strip()
        if len(records) > 12:
            def _mem_importance(item):
                try:
                    return int((item or {}).get("importance", 0) or 0)
                except Exception:
                    return 0
            # Hard safety guard: keep only the 12 highest-value memories. Stable
            # ordering preserves the model's original order for equal importance.
            indexed = list(enumerate(records))
            indexed.sort(key=lambda pair: (-_mem_importance(pair[1]), pair[0]))
            keep_idx = {i for i, _ in indexed[:12]}
            records = [item for i, item in enumerate(records) if i in keep_idx]
            self.log("摘要+记忆返回超过 12 条长期记忆，已按 importance 保留最高价值 12 条。")
        retirements = self._validated_state_retirements(
            n, raw_retirements, records
        )
        if retirements:
            records.extend(retirements)
            self.log(
                f"状态键维护：Agent 合并并退休 "
                f"{len(retirements)} 个同义或被替代 key。"
            )
        if len(summary) > 1800:
            self.log(f"章节摘要偏长（{len(summary)} 字符）；已保留完整摘要，不做机械截断。")
        handoff_error = ""
        try:
            handoff = normalize_handoff(
                raw_handoff, n, source_tail, continuity_cfg["handoff_max_chars"],
                require_structured=True,
            )
        except Exception as exc:
            handoff_error = f"handoff 结构化提取失败：{exc}"
            handoff = degraded_handoff(n, source_tail, handoff_error)
            self.write(f"logs/handoff_raw_{n:04d}.txt", json.dumps(raw_handoff, ensure_ascii=False, indent=2))
            self.log(handoff_error + "；已保留确定性正文末尾，提交后停止批量继续。")
        return summary, records, handoff, handoff_error

    # ---------- V4.2 global story audit (DeepSeek-only) ----------
    # v1 = prose-only report. v2 added structured repair findings. v3 separates
    # hard-continuity detection from outline review, requires two-sided verbatim
    # evidence, and records the Canon snapshot the report was built from.
    AUDIT_SCHEMA_VERSION = 3
    AUDIT_REPAIR_FINDINGS_MIN_VERSION = 2
    AUDIT_MAX_WINDOW_CHAPTERS = 12
    AUDIT_CHECKPOINT_VERSION = 1
    AUDIT_PIPELINE_REVISION = "hard-continuity-v3-assertion-ledger-2"
    AUDIT_HARD_CATEGORIES = {
        "TIME_ROLLBACK",
        "SCENE_REPLAY",
        "MEMORY_RESET",
        "STATE_REGRESSION",
        "RELATIONSHIP_HISTORY_REWRITE",
        "ITEM_STATE_RESET",
        "ABILITY_RULE_CONTRADICTION",
        "NUMERIC_ROLLBACK",
        "KNOWLEDGE_RESET",
        "LOCATION_RESET",
        "OTHER_HARD_CONTINUITY",
    }

    def _audit_check_cancel(self):
        if self.audit_stop_event.is_set():
            raise ProviderCancelledError("剧情一致性审计已停止")

    def _audit_set_stage(self, stage, label=""):
        with self.audit_lock:
            self.audit_status["stage"] = stage
            self.audit_status["stage_label"] = label
        self.log(f"剧情审计：{label or stage}")

    def _audit_outline_for_range(self, start, end):
        text, global_part, blocks = self._outline_blocks()
        if not text.strip():
            return "（暂无 outline.md）"
        picked = []
        if global_part:
            picked.append("【总纲/卷级说明】\n" + global_part)
        for b in blocks:
            if b["end"] >= int(start) and b["start"] <= int(end):
                picked.append(b["text"])
        if picked:
            return "\n\n".join(picked)
        return self.outline_context(start)

    def _audit_outline_index(self, start, end):
        _, _, blocks = self._outline_blocks()
        rows = []
        for b in blocks:
            if b["end"] < int(start) or b["start"] > int(end):
                continue
            first = (b.get("text") or "").splitlines()[0].strip()
            rows.append(first or f"第{b['start']}-{b['end']}章")
        return "\n".join(rows) if rows else "（无可解析章节标题）"

    def _audit_summary_for_chapter(self, n):
        p = self.root / "summaries" / f"{int(n):04d}.md"
        if p.exists():
            return p.read_text(encoding="utf-8").strip()
        # Old projects may have a DB summary even when the file was removed; do not
        # silently invent one.  Missing summaries are visible to the audit model.
        return "（缺少本章 summary 文件）"

    def _audit_review_compact(self, n):
        p = self.root / "reviews" / f"{int(n):04d}.json"
        if not p.exists():
            return {"severity": "UNKNOWN", "risk_flags": {}, "issues": []}
        try:
            obj = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {"severity": "UNKNOWN", "risk_flags": {}, "issues": ["review JSON 无法解析"]}
        issues = []
        for key in ("continuity", "character", "knowledge", "world", "plot"):
            vals = obj.get(key) if isinstance(obj.get(key), list) else []
            for item in vals[:2]:
                text = str(item or "").strip()
                if text:
                    issues.append(f"{key}: {text[:500]}")
        return {
            "severity": str(obj.get("severity", "UNKNOWN") or "UNKNOWN"),
            "confidence": str(obj.get("confidence", "") or ""),
            "risk_flags": obj.get("risk_flags", {}) if isinstance(obj.get("risk_flags"), dict) else {},
            "issues": issues[:6],
        }

    def _audit_segment_material(self, start, end):
        parts = []
        for n in range(int(start), int(end) + 1):
            chapter = self.root / "chapters" / f"{n:04d}.md"
            if not chapter.exists():
                continue
            review = self._audit_review_compact(n)
            parts.append(
                f"## 第{n}章完整 Canon 正文\n{chapter.read_text(encoding='utf-8').strip()}\n\n"
                f"【辅助 Summary，不得代替正文】\n{self._audit_summary_for_chapter(n)}\n"
                f"【辅助最终 Review 简表】\n{json.dumps(review, ensure_ascii=False)}"
            )
        return "\n\n".join(parts)

    def _audit_assertion_inventory(self, start, end, per_chapter=16):
        """Extract short verbatim history assertions before either model pass."""
        root = getattr(self, "root", None)
        if root is None:
            return []
        rules = (
            (0, "NEGATED_HISTORY", r"从未|从来没有|从来没|没有(?:改|调整|试|做|去|见|问|说|听|拿|给|交|还|写|记|练|用|碰|发生|经历|住)[^。！？!?；;\n]{0,24}过|没(?:改|调整|试|做|去|见|问|说|听|拿|给|交|还|写|记|练|用|碰|发生|经历|住)[^。！？!?；;\n]{0,24}过"),
            (1, "CURRENT_STATE", r"(?:抱着|拿着|手里抱着|手里拿着)[^。！？!?；;\n]{0,24}(?:活页本|本子)|(?:活页本|本子)[^。！？!?；;\n]{0,24}(?:书包里|手里)|(?:他|她|主角|同伴)的?书包里|(?:他的|她的|他那本|她那本)[^。！？!?；;\n]{0,16}(?:本子|活页本|笔记|书包|手机|物品)"),
            (1, "COUNT_OR_ORDINAL", r"第[0-9零〇一二两三四五六七八九十百]+次|(?:总共|一共|累计|共计)[^。！？!?；;\n]{0,16}[0-9零〇一二两三四五六七八九十百]+次|已经(?:记|写|做|运行|完成|练|试)[^。！？!?；;\n]{0,12}[0-9零〇一二两三四五六七八九十百]+次|[0-9零〇一二两三四五六七八九十百]+次(?:都)?(?:记录|运行|尝试|实验|练功|修改|改动|接触|见面|记)|最新|最近(?:一次|几次|[0-9零〇一二两三四五六七八九十百]+次)|上一次"),
            (0, "EVENT_OCCURRED", r"改过|改了|调整过|调整了|缩短|增加|减少|放进|装进|交给|递给|拿走|带走|归还"),
            (3, "EVENT_OCCURRED", r"开始|结束|完成|记录|写入"),
            (3, "TIME_ANCHOR", r"周[一二三四五六日天]|星期[一二三四五六日天]|周末|昨晚|昨天|今早|今天|今晚|次日|第二天|当晚|清晨|凌晨|中午|下午|晚上|刚才"),
            (4, "CURRENT_STATE", r"第一次|首次|一直|仍然|还是|又回到|依旧"),
        )
        rows = []
        for n in range(int(start), int(end) + 1):
            path = root / "chapters" / f"{n:04d}.md"
            if not path.exists():
                continue
            text = path.read_text(encoding="utf-8")
            candidates = []
            for sentence in re.split(r"(?<=[。！？!?；;])\s*|\n+", text):
                sentence = sentence.strip()
                if not sentence:
                    continue
                matched = []
                for priority, fact_kind, pattern in rules:
                    hit = re.search(pattern, sentence)
                    if hit:
                        matched.append((priority, fact_kind, hit.group(0), hit.start()))
                if not matched:
                    continue
                matched.sort(key=lambda item: (item[0], item[3]))
                first_pos = matched[0][3]
                if len(sentence) <= 220:
                    quote = sentence
                else:
                    left = max(0, first_pos - 70)
                    quote = sentence[left:left + 220].strip()
                candidates.append({
                    "priority": matched[0][0],
                    "fact_kind_hint": matched[0][1],
                    "signals": list(dict.fromkeys(item[2] for item in matched))[:8],
                    "quote": quote,
                })
            seen_quotes = set()
            candidates.sort(key=lambda row: row["priority"])
            kept = []
            for row in candidates:
                quote = row["quote"]
                if quote in seen_quotes:
                    continue
                seen_quotes.add(quote)
                kept.append(row)
                if len(kept) >= int(per_chapter):
                    break
            for serial, row in enumerate(kept, 1):
                rows.append({
                    "assertion_id": f"A{n:04d}_{serial:02d}",
                    "chapter_no": n,
                    "fact_kind_hint": row["fact_kind_hint"],
                    "signals": row["signals"],
                    "quote": row["quote"],
                })
        return rows

    def _audit_deterministic_window(self, start, end):
        out = []
        outline = self._audit_outline_for_range(start, end)
        month_match = re.search(r"(?<!\d)(1[0-2]|[1-9])月", outline)
        month = int(month_match.group(1)) if month_match else None
        titles = {}
        for n in range(int(start), int(end) + 1):
            p = self.root / "chapters" / f"{n:04d}.md"
            if not p.exists():
                continue
            text = p.read_text(encoding="utf-8")
            title = next((x.strip() for x in text.splitlines() if x.strip()), "")
            if title and title in titles:
                out.append({"chapter_no": n, "related_chapters": [titles[title]], "code": "DUPLICATE_TITLE", "message": f"重复章节标题：{title}", "evidence": title})
            titles[title] = n
            if n > int(start):
                prev_path = self.root / "chapters" / f"{n-1:04d}.md"
                if prev_path.exists():
                    hp = self.root / "handoffs" / f"{n-1:04d}.json"
                    try:
                        handoff = json.loads(hp.read_text(encoding="utf-8")) if hp.exists() else {}
                    except Exception:
                        handoff = {}
                    rows = deterministic_boundary_findings(
                        prev_path.read_text(encoding="utf-8"), text,
                        previous_handoff=handoff,
                        current_task=self.chapter_task_card(n),
                        next_task=self.future_task_boundary(n), month=month,
                    )
                    for row in rows:
                        out.append({
                            "chapter_no": n, "related_chapters": [n - 1],
                            "code": row["code"], "message": row["message"],
                            "evidence": row.get("evidence", ""),
                        })
        return out

    def _audit_chat(self, stage, system, user, model, thinking=True, effort="low", max_tokens=5000):
        self._audit_check_cancel()
        text, _ = self.audit_router.chat(
            stage, system, user, temperature=0.15, max_tokens=max_tokens,
            # Thinking responses can remain silent for more than five minutes
            # when requested non-streaming.  Streaming keeps the socket active
            # while still returning one complete JSON string to this caller.
            stream=True, label=stage, emit_text=False, routing_context="",
            provider_override="deepseek", model_override=model,
            thinking_override=bool(thinking), response_format={"type": "json_object"},
            reasoning_effort_override=effort, allow_local_fallback=False,
        )
        obj = _json_obj(text)
        if not isinstance(obj, dict):
            raw_name = f"logs/audit_{stage}_raw_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
            self.write(raw_name, text)
            raise RuntimeError(f"剧情审计 {stage} JSON 解析失败，原始输出已保存：{raw_name}")
        return obj

    @staticmethod
    def _audit_status_level(value):
        x = str(value or "GREEN").upper()
        return x if x in {"GREEN", "YELLOW", "ORANGE", "RED"} else "YELLOW"

    @staticmethod
    def _audit_chapter_list(value, start, end, limit=8):
        rows = value if isinstance(value, list) else []
        out = []
        for x in rows:
            try:
                n = int(x)
            except Exception:
                m = re.search(r"\d+", str(x or ""))
                if not m:
                    continue
                n = int(m.group())
            if int(start) <= n <= int(end) and n not in out:
                out.append(n)
            if len(out) >= int(limit):
                break
        return out

    def _audit_prepare_local_candidates(self, rows, start, end,
                                        prefix="LF", deterministic=False):
        """Give every Flash/rule candidate a stable identity and hard category."""
        code_categories = {
            "TIME_REGRESSION": "TIME_ROLLBACK",
            "DATE_REGRESSION": "TIME_ROLLBACK",
            "SEASON_CONFLICT": "TIME_ROLLBACK",
            "CLOSED_SCENE_REOPENED": "SCENE_REPLAY",
            "COMPLETED_EVENT_REPEATED": "SCENE_REPLAY",
            "ADJACENT_PROCESS_REPLAY": "SCENE_REPLAY",
            "DUPLICATE_TITLE": "SCENE_REPLAY",
            "KNOWLEDGE_REGRESSION": "KNOWLEDGE_RESET",
            "STATE_REGRESSION": "STATE_REGRESSION",
        }
        out = []
        for raw in rows if isinstance(rows, list) else []:
            if not isinstance(raw, dict):
                continue
            try:
                n = int(raw.get("chapter_no") or 0)
            except Exception:
                continue
            if not int(start) <= n <= int(end):
                continue
            related = []
            for value in raw.get("related_chapters") or []:
                try:
                    chapter = int(value)
                except Exception:
                    continue
                if (
                    int(start) <= chapter <= int(end)
                    and chapter != n and chapter not in related
                ):
                    related.append(chapter)
            code = str(raw.get("code") or "").strip().upper()
            category = str(raw.get("category") or "").strip().upper()
            if deterministic:
                category = code_categories.get(code, "OTHER_HARD_CONTINUITY")
            elif category not in self.AUDIT_HARD_CATEGORIES:
                category = "OTHER_HARD_CONTINUITY"
            issue = str(
                raw.get("issue") or raw.get("message")
                or "本地扫描提出的硬连续性疑点"
            ).strip()
            candidate_id = str(raw.get("candidate_id") or "").strip()
            if not candidate_id.startswith(prefix):
                candidate_id = f"{prefix}{len(out) + 1:03d}"
            row = dict(raw)
            row.update({
                "candidate_id": candidate_id,
                "chapter_no": n,
                "related_chapters": related[:12],
                "category": category,
                "issue": issue,
                "source_scope": (
                    "segment_deterministic" if deterministic else "segment_flash"
                ),
            })
            out.append(row)
        return out[:80]

    def _audit_segment(self, start, end):
        outline = self._audit_outline_for_range(start, end)
        material = self._audit_segment_material(start, end)
        deterministic = self._audit_deterministic_window(start, end)
        assertions = self._audit_assertion_inventory(start, end)
        system = """你是网文硬连续性初筛员。你收到重叠窗口内每一章的完整 Canon 正文。
正文是第一证据；Summary、Review、Outline 只用于理解背景，不能覆盖正文事实。

本轮只查普通读者能感知的硬错误：时间倒退或星期错位、同一场景无说明重演、人物忘记已发生事件、物品/伤势/能力/知识/关系状态复位、次数或最新记录回退、既有相处历史被后文替换。
不要报告节奏、文风、爽点、细节丰富度、普通大纲执行偏差，也不要因为大纲要求某次实验失败而把正文成功写成连续性错误。明确回忆、倒叙、多视角复现不算错误。

判定必须服从显式语义：只写“合上卷子/收起作业”不等于“全部做完”；未注明属于哪一周的“周二/周五”不能仅凭当前是周一就判成未来日期。时间冲突必须先串起窗口内全部星期与日期锚。重点核对“第一次、从未、一直、最新、总共、又回到、仍然”等会改写历史或次数的断言。

Flash 只负责高召回提出候选，不拥有最终裁决权。每个候选必须同时给出“问题位置原文”和“与其冲突的对照原文”；找不到两侧逐字证据就不要进入 evidence_findings。还要为全局阶段抽取可能被后文推翻的持久状态锚点。必须输出严格 JSON。"""
        user = f"""审计范围：第{start}-{end}章。

【对应 Outline】
{outline}

【本地确定性检查结果】
{json.dumps(deterministic, ensure_ascii=False, indent=2)}

【程序逐字提取的硬断言索引；必须与正文和状态账本一起核对】
{json.dumps(assertions, ensure_ascii=False, indent=2)}

【完整正文窗口 + 辅助资料】
{material}

输出严格 JSON：
{{
  "status": "GREEN/YELLOW/ORANGE/RED",
  "segment_summary": "只总结本阶段实际完成到哪里，不复写正文",
  "outline_completion_pct": 0,
  "completed_goals": [],
  "missing_or_delayed": [],
  "future_leaks": [],
  "persistent_new_mainlines": [],
  "character_drift": [],
  "timeline_issues": [],
  "growth_progress_issues": [],
  "continuity_issues": [],
  "repetition_issues": [],
  "state_regressions": [],
  "state_ledger": [
    {{
      "chapter_no": {start},
      "category": "TIME/SCENE/MEMORY/ITEM/ABILITY/PROCEDURE/KNOWLEDGE/RELATIONSHIP/COUNT/LOCATION/OTHER",
      "fact_kind": "CURRENT_STATE/EVENT_OCCURRED/NEGATED_HISTORY/COUNT_OR_ORDINAL/TIME_ANCHOR",
      "entity": "人物或对象",
      "state_key": "稳定状态字段",
      "state_value": "本章确立的事实",
      "evidence_quote": "该章正文逐字引文"
    }}
  ],
  "evidence_findings": [
    {{
      "chapter_no": {start},
      "related_chapters": [],
      "category": "TIME_ROLLBACK/SCENE_REPLAY/MEMORY_RESET/STATE_REGRESSION/RELATIONSHIP_HISTORY_REWRITE/ITEM_STATE_RESET/ABILITY_RULE_CONTRADICTION/NUMERIC_ROLLBACK/KNOWLEDGE_RESET/LOCATION_RESET/OTHER_HARD_CONTINUITY",
      "issue": "只描述硬连续性冲突",
      "evidence_quote": "问题所在章正文逐字引文",
      "evidence_quotes": [
        {{"chapter_no": {start}, "quote": "与 evidence_quote 完全相同的目标章逐字引文"}},
        {{"chapter_no": {start}, "quote": "与其冲突的正文逐字引文"}}
      ],
      "confidence": "high/medium/low"
    }}
  ],
  "suspect_chapters": [],
  "source_check_needed": false,
  "recommended_action": "继续/后续纠偏/人工检查局部/暂停批量生成"
}}

判定：
GREEN=未发现硬连续性疑点；
YELLOW=只有证据不足的弱疑点；
ORANGE=存在具有两侧正文证据、需要 Pro 复核的硬错误候选；
RED=多处硬状态被重置或核心历史被重写。
大纲完成度不影响上述状态。只要 evidence_findings 非空，suspect_chapters 必须列出问题章和对照章。
state_ledger 要逐章优先记录明确时间锚、已结束场景、物品/记录方式、能力改动、已知信息、关系与亲密历史、实验或记录次数；每条都必须有该章逐字引文。任何方法、节奏、顺序或参数的尝试和改动，即使最后失败或恢复原状，也必须记为 EVENT_OCCURRED；任何“从未、没有改过、第一次、一直”之类历史断言必须记为 NEGATED_HISTORY；第N次、总共N次、最新记录必须记为 COUNT_OR_ORDINAL，数字保持原文。"""
        obj = self._audit_chat(
            f"audit_segment_{start:04d}_{end:04d}", system, user,
            model="deepseek-v4-flash", thinking=True, effort="low", max_tokens=7000,
        )
        obj["status"] = self._audit_status_level(obj.get("status"))
        obj["start"] = int(start); obj["end"] = int(end)
        obj["suspect_chapters"] = self._audit_chapter_list(obj.get("suspect_chapters"), start, end)
        obj["state_ledger"] = self._audit_normalize_state_ledger(
            obj.get("state_ledger"), range(int(start), int(end) + 1)
        )
        obj["evidence_findings"] = self._audit_prepare_local_candidates(
            obj.get("evidence_findings"), start, end,
            prefix="LF", deterministic=False,
        )
        obj["deterministic_findings"] = self._audit_prepare_local_candidates(
            deterministic, start, end, prefix="LD", deterministic=True,
        )
        if obj["deterministic_findings"]:
            for row in obj["deterministic_findings"]:
                n = int(row.get("chapter_no") or 0)
                if start <= n <= end and n not in obj["suspect_chapters"]:
                    obj["suspect_chapters"].append(n)
            if obj["status"] == "GREEN":
                obj["status"] = "ORANGE"
            obj["source_check_needed"] = True
        return obj

    # Repair classes usable by the audit as a suggested routing hint.  The repair
    # pipeline re-validates these, so a wrong hint can only cost precision, never
    # safety.
    AUDIT_FIX_CLASSES = {"TEXT_ONLY", "CONTINUITY_MINOR", "REWRITE_SPAN", "REWRITE_CHAPTER", "DEFER_FUTURE"}

    def _audit_quote_is_verbatim(self, chapter_no, quote):
        quote = str(quote or "").strip()
        if not quote:
            return False
        p = self.root / "chapters" / f"{int(chapter_no):04d}.md"
        return p.exists() and quote in p.read_text(encoding="utf-8")

    def _audit_normalize_state_ledger(self, rows, allowed_chapters):
        """Keep only durable state anchors backed by exact shown Canon text."""
        allowed = {int(x) for x in (allowed_chapters or [])}
        valid_categories = {
            "TIME", "SCENE", "MEMORY", "ITEM", "ABILITY", "PROCEDURE", "KNOWLEDGE",
            "RELATIONSHIP", "COUNT", "LOCATION", "OTHER",
        }
        valid_fact_kinds = {
            "CURRENT_STATE", "EVENT_OCCURRED", "NEGATED_HISTORY",
            "COUNT_OR_ORDINAL", "TIME_ANCHOR",
        }
        out, seen = [], set()
        for raw in rows if isinstance(rows, list) else []:
            if not isinstance(raw, dict):
                continue
            try:
                n = int(raw.get("chapter_no") or 0)
            except Exception:
                continue
            quote = str(raw.get("evidence_quote") or "").strip()
            if n not in allowed or not self._audit_quote_is_verbatim(n, quote):
                continue
            category = str(raw.get("category") or "OTHER").strip().upper()
            if category not in valid_categories:
                category = "OTHER"
            fact_kind = str(raw.get("fact_kind") or "CURRENT_STATE").strip().upper()
            if fact_kind not in valid_fact_kinds:
                fact_kind = "CURRENT_STATE"
            entity = str(raw.get("entity") or "").strip()
            key = str(raw.get("state_key") or "").strip()
            value = str(raw.get("state_value") or "").strip()
            if not key or not value:
                continue
            fingerprint = (n, category, fact_kind, entity, key, value, quote)
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            out.append({
                "chapter_no": n,
                "category": category,
                "fact_kind": fact_kind,
                "entity": entity,
                "state_key": key,
                "state_value": value,
                "evidence_quote": quote,
                "quote_is_verbatim": True,
            })
        return out[:160]

    def _audit_normalize_findings(self, rows, allowed_chapters):
        """Normalize Pro findings and enforce the two-sided evidence contract."""
        allowed = {int(x) for x in (allowed_chapters or [])}
        out = []
        for idx, raw in enumerate(rows if isinstance(rows, list) else [], 1):
            if not isinstance(raw, dict):
                continue
            try:
                n = int(raw.get("chapter_no") or 0)
            except Exception:
                continue
            # A finding must point at a chapter the verifier was actually shown.
            if n not in allowed:
                continue

            cls = str(raw.get("suggested_class") or "").strip().upper()
            if cls not in self.AUDIT_FIX_CLASSES:
                cls = ""

            quote = str(raw.get("evidence_quote") or "").strip("\r\n").strip()

            issue = str(raw.get("issue") or "").strip()
            fix = str(raw.get("required_fix") or "").strip()
            if not issue and not fix:
                continue

            category = str(raw.get("category") or "").strip().upper()
            confidence = str(raw.get("confidence") or "").strip().lower()
            candidate_id = str(raw.get("candidate_id") or "").strip()

            related = []
            for x in raw.get("related_chapters") or []:
                try:
                    x = int(x)
                except Exception:
                    continue
                if x > 0 and x != n and x not in related:
                    related.append(x)

            evidence_quotes = []
            raw_evidence = (
                raw.get("evidence_quotes")
                or raw.get("related_evidence")
                or raw.get("comparison_evidence")
                or []
            )
            for evidence in raw_evidence:
                if not isinstance(evidence, dict):
                    continue
                try:
                    evidence_chapter = int(evidence.get("chapter_no") or 0)
                except Exception:
                    continue
                evidence_quote = str(
                    evidence.get("quote") or evidence.get("evidence_quote") or ""
                ).strip()
                if evidence_chapter not in allowed or not evidence_quote:
                    continue
                verbatim = self._audit_quote_is_verbatim(evidence_chapter, evidence_quote)
                evidence_quotes.append({
                    "chapter_no": evidence_chapter,
                    "quote": evidence_quote,
                    "quote_is_verbatim": verbatim,
                })
                if evidence_chapter != n and evidence_chapter not in related:
                    related.append(evidence_chapter)

            target_path = self.root / "chapters" / f"{n:04d}.md"
            target_text = (
                target_path.read_text(encoding="utf-8")
                if target_path.exists() else ""
            )
            target_occurrences = target_text.count(quote) if quote else 0
            target_verbatim = target_occurrences > 0
            target_is_covered = any(
                x.get("quote_is_verbatim") and int(x["chapter_no"]) == n
                and x.get("quote") == quote
                for x in evidence_quotes
            )
            if related:
                comparison_is_covered = any(
                    x.get("quote_is_verbatim") and int(x["chapter_no"]) in related
                    for x in evidence_quotes
                )
            else:
                comparison_is_covered = any(
                    x.get("quote_is_verbatim")
                    and (int(x["chapter_no"]), x.get("quote")) != (n, quote)
                    for x in evidence_quotes
                )
            gate_reasons = []
            if category not in self.AUDIT_HARD_CATEGORIES:
                gate_reasons.append("不是硬连续性类别")
            if confidence != "high":
                gate_reasons.append("置信度不是 high")
            if not target_verbatim or not target_is_covered:
                gate_reasons.append("目标章问题引文未被逐字双证据覆盖")
            if target_occurrences != 1:
                gate_reasons.append("目标章问题引文不能唯一定位")
            if not comparison_is_covered:
                gate_reasons.append("缺少关联章或同章另一处逐字对照证据")
            if not fix:
                gate_reasons.append("缺少明确的最小修正目标")
            if cls not in self.AUDIT_FIX_CLASSES or cls == "DEFER_FUTURE":
                gate_reasons.append("缺少可执行的小修分类")

            out.append({
                "finding_id": f"V{n:04d}_{idx:03d}",
                "candidate_id": candidate_id,
                "chapter_no": n,
                "related_chapters": related[:12],
                "category": category,
                "issue": issue,
                "required_fix": fix,
                "evidence_quote": quote,
                "quote_is_verbatim": target_verbatim,
                "evidence_quotes": evidence_quotes[:16],
                "suggested_class": cls,
                "must_preserve": [
                    str(x).strip()
                    for x in (raw.get("must_preserve") or [])
                    if str(x).strip() and str(x).strip() in target_text
                ][:16],
                "confidence": confidence,
                "repair_ready": not gate_reasons,
                "gate_reasons": gate_reasons,
            })
        return out

    @staticmethod
    def _audit_partition_findings(rows):
        ready, review_only = [], []
        for row in rows or []:
            (ready if row.get("repair_ready") else review_only).append(row)
        return ready, review_only

    def _audit_unresolved_local_candidate(self, candidate, reason,
                                          disposition="omitted"):
        n = int(candidate.get("chapter_no") or 0)
        candidate_id = str(candidate.get("candidate_id") or "").strip()
        quote = str(
            candidate.get("evidence_quote") or candidate.get("evidence") or ""
        ).strip()
        quote_is_verbatim = bool(
            quote and self._audit_quote_is_verbatim(n, quote)
        )
        evidence_quotes = []
        for evidence in candidate.get("evidence_quotes") or []:
            if not isinstance(evidence, dict):
                continue
            try:
                chapter = int(evidence.get("chapter_no") or 0)
            except Exception:
                continue
            evidence_quote = str(
                evidence.get("quote") or evidence.get("evidence_quote") or ""
            ).strip()
            if evidence_quote and self._audit_quote_is_verbatim(chapter, evidence_quote):
                evidence_quotes.append({
                    "chapter_no": chapter,
                    "quote": evidence_quote,
                    "quote_is_verbatim": True,
                })
        if quote_is_verbatim and not any(
            int(row.get("chapter_no") or 0) == n
            and str(row.get("quote") or "") == quote
            for row in evidence_quotes
        ):
            evidence_quotes.append({
                "chapter_no": n, "quote": quote, "quote_is_verbatim": True,
            })
        return {
            "finding_id": f"R{n:04d}_{candidate_id or 'LOCAL'}",
            "candidate_id": candidate_id,
            "chapter_no": n,
            "related_chapters": list(candidate.get("related_chapters") or [])[:12],
            "category": str(candidate.get("category") or "OTHER_HARD_CONTINUITY"),
            "issue": str(candidate.get("issue") or "本地扫描提出的硬连续性疑点"),
            "required_fix": "",
            "evidence_quote": quote if quote_is_verbatim else "",
            "quote_is_verbatim": quote_is_verbatim,
            "evidence_quotes": evidence_quotes[:16],
            "suggested_class": "",
            "must_preserve": [],
            "confidence": "low",
            "repair_ready": False,
            "gate_reasons": [str(reason or "窗口 Pro 未形成可提交的双边正文证据链")],
            "source_scope": str(candidate.get("source_scope") or "segment_candidate"),
            "candidate_disposition": str(disposition or "omitted"),
            "deterministic_code": str(candidate.get("code") or ""),
        }

    def _audit_normalize_boundary_checks(self, rows, shown):
        """Require one explicit verdict for every adjacent chapter seam."""
        chapters = sorted({int(x) for x in (shown or [])})
        expected = [
            (left, left + 1) for left in chapters
            if left + 1 in chapters
        ]
        raw_by_pair = {}
        for raw in rows if isinstance(rows, list) else []:
            if not isinstance(raw, dict):
                continue
            try:
                pair = (
                    int(raw.get("from_chapter") or raw.get("left_chapter") or 0),
                    int(raw.get("to_chapter") or raw.get("right_chapter") or 0),
                )
            except Exception:
                continue
            if pair not in expected or pair in raw_by_pair:
                continue
            raw_by_pair[pair] = raw

        out = []
        for left, right in expected:
            raw = raw_by_pair.get((left, right))
            if raw is None:
                out.append({
                    "from_chapter": left,
                    "to_chapter": right,
                    "relation": "UNKNOWN",
                    "from_quote": "",
                    "to_quote": "",
                    "link_quote": "",
                    "result": "UNCERTAIN",
                    "reason": "Pro 未返回该相邻章节接缝的逐项裁决",
                    "protocol_missing": True,
                })
                continue
            relation = str(raw.get("relation") or "UNKNOWN").strip().upper()
            if relation not in {"CONTINUOUS", "NEXT_DAY", "ELAPSED", "UNKNOWN"}:
                relation = "UNKNOWN"
            result = str(raw.get("result") or "UNCERTAIN").strip().upper()
            if result not in {"CONSISTENT", "CONFLICT", "UNCERTAIN"}:
                result = "UNCERTAIN"
            from_quote = str(raw.get("from_quote") or "").strip()
            to_quote = str(raw.get("to_quote") or "").strip()
            link_quote = str(raw.get("link_quote") or "").strip()
            out.append({
                "from_chapter": left,
                "to_chapter": right,
                "relation": relation,
                "from_quote": from_quote,
                "from_quote_is_verbatim": bool(
                    from_quote and self._audit_quote_is_verbatim(left, from_quote)
                ),
                "to_quote": to_quote,
                "to_quote_is_verbatim": bool(
                    to_quote and self._audit_quote_is_verbatim(right, to_quote)
                ),
                "link_quote": link_quote,
                "link_quote_chapter": next((
                    n for n in (right, left)
                    if link_quote and self._audit_quote_is_verbatim(n, link_quote)
                ), 0),
                "result": result,
                "reason": str(raw.get("reason") or "").strip(),
                "protocol_missing": False,
            })
        return out

    def _audit_verify_segment(self, segment):
        start, end = int(segment["start"]), int(segment["end"])
        chapters = []
        shown = []
        # Pro independently reads the complete window. Flash is a candidate
        # generator, not a gate: a Flash miss must not prevent Pro from seeing a
        # local reset such as a repeated scene or next-chapter time rollback.
        for n in range(start, end + 1):
            p = self.root / "chapters" / f"{n:04d}.md"
            if p.exists():
                chapters.append(f"## 第{n}章正文\n{p.read_text(encoding='utf-8').strip()}")
                shown.append(n)
        if not chapters:
            return None
        assertions = self._audit_assertion_inventory(start, end)
        expected_boundaries = [
            {"from_chapter": n, "to_chapter": n + 1}
            for n in shown if n + 1 in shown
        ]
        local_candidates = [
            *self._audit_prepare_local_candidates(
                segment.get("evidence_findings"), start, end,
                prefix="LF", deterministic=False,
            ),
            *self._audit_prepare_local_candidates(
                segment.get("deterministic_findings"), start, end,
                prefix="LD", deterministic=True,
            ),
        ]
        system = """你是网文硬连续性终审员。你会看到窗口内全部 Canon 正文和 Flash 初筛。
Flash 可能漏报或误报；必须独立通读整个窗口，不能只复核 Flash 列出的疑点。

只确认普通读者能感知的硬错误：时间倒退或星期错位、同一场景无说明重演、人物忘记已发生事件、物品/伤势/能力/知识/关系状态复位、次数或最新记录回退、既有相处历史被后文替换。不要报告节奏、文风、爽点、普通大纲偏差，也不要因为大纲要求某次实验失败而把正文成功写成连续性错误。

必须按显式语义推理：只写“合上卷子/收起作业”不等于“全部做完”；未注明周次的“周二/周五”不能仅因当前是周一就判成未来日期，先串起全部时间锚。重点审查“第一次、从未、一直、最新、总共、又回到、仍然”等历史断言，但也要允许合理省略和概括。

必须完成两张逐项检查表：boundary_checks 对给出的每个相邻章节接缝恰好返回一项，核对前章结尾、后章开头以及“刚才/昨晚/次日/还”等承接词；assertion_checks 对程序提取的每个 assertion_id 恰好返回一项。任何 CONFLICT 都必须同时写入 findings；暂时无法排除的具体矛盾写 UNCERTAIN，程序会保留人工复核，不能省略该项。

Flash 和本地确定性规则提出的每个 candidate_id 也必须在 candidate_dispositions 中恰好裁决一次。CONFIRMED 必须同时在 findings 或 confirmed_findings 返回带相同 candidate_id 的证据项；REVIEW/FALSE_POSITIVE 必须写明理由。你仍须独立发现候选之外的新问题，新发现可不带 candidate_id。

每个确认项必须提供两侧 Canon 逐字证据。evidence_quote 定位需要修改的目标章；evidence_quotes 必须同时包含目标章这段原文和与其冲突的对照原文。不得改写引文、不得省略、不得加省略号。证据不足时放入 false_positives 或不输出。
同时抽取后续可能被推翻的持久状态锚点，供全局 Pro 比对。必须输出严格 JSON。"""
        user = f"""范围：第{start}-{end}章。
【Outline，仅用于识别明确倒叙等作者意图；不审普通执行偏差】
{self._audit_outline_for_range(start, end)}

【Flash 初筛，仅作候选，不是裁决】
{json.dumps(segment, ensure_ascii=False, indent=2)}

【必须逐项裁决的 Flash/本地规则候选】
{json.dumps(local_candidates, ensure_ascii=False, indent=2)}

【必须逐项覆盖的相邻章节接缝】
{json.dumps(expected_boundaries, ensure_ascii=False, indent=2)}

【程序逐字提取的硬断言；每个 assertion_id 必须裁决】
{json.dumps(assertions, ensure_ascii=False, indent=2)}

【窗口内完整 Canon 正文】
{chr(10).join(chapters)}

输出严格 JSON：
{{
  "status": "GREEN/YELLOW/ORANGE/RED",
  "confirmed_findings": [],
  "false_positives": [],
  "candidate_dispositions": [
    {{
      "candidate_id": "逐字复制输入 LFxxx/LDxxx；每个候选恰好一项",
      "disposition": "CONFIRMED/REVIEW/FALSE_POSITIVE",
      "reason": "简短裁决依据"
    }}
  ],
  "suspect_chapters": [],
  "recommended_action": "继续/后续纠偏/人工检查局部/暂停批量生成",
  "boundary_checks": [
    {{
      "left_chapter": {shown[0]},
      "right_chapter": {shown[min(1, len(shown) - 1)]},
      "relation": "CONTINUOUS/NEXT_DAY/ELAPSED/UNKNOWN",
      "from_quote": "前章结尾时间或场景锚逐字引文；没有则空字符串",
      "to_quote": "后章开头时间或场景锚逐字引文；没有则空字符串",
      "link_quote": "刚才/昨晚/次日/还等承接词所在逐字引文；没有则空字符串",
      "result": "CONSISTENT/CONFLICT/UNCERTAIN",
      "reason": "简短判定依据"
    }}
  ],
  "assertion_checks": [
    {{
      "assertion_id": "逐字复制输入 assertion_id",
      "assertion_quote": "逐字复制该 assertion_id 对应的 quote",
      "result": "CONSISTENT/CONFLICT/UNCERTAIN",
      "related_assertion_ids": [],
      "reason": "冲突或不确定时说明与哪条既有事实不一致；一致时可留空"
    }}
  ],
  "state_ledger": [
    {{
      "chapter_no": {shown[0]},
      "category": "TIME/SCENE/MEMORY/ITEM/ABILITY/PROCEDURE/KNOWLEDGE/RELATIONSHIP/COUNT/LOCATION/OTHER",
      "fact_kind": "CURRENT_STATE/EVENT_OCCURRED/NEGATED_HISTORY/COUNT_OR_ORDINAL/TIME_ANCHOR",
      "entity": "人物或对象",
      "state_key": "稳定状态字段",
      "state_value": "本章确立的事实",
      "evidence_quote": "该章正文逐字引文"
    }}
  ],
  "findings": [
    {{
      "chapter_no": {shown[0]},
      "related_chapters": [],
      "category": "TIME_ROLLBACK/SCENE_REPLAY/MEMORY_RESET/STATE_REGRESSION/RELATIONSHIP_HISTORY_REWRITE/ITEM_STATE_RESET/ABILITY_RULE_CONTRADICTION/NUMERIC_ROLLBACK/KNOWLEDGE_RESET/LOCATION_RESET/OTHER_HARD_CONTINUITY",
      "issue": "问题是什么",
      "required_fix": "需要怎样改正；只描述改正目标，不写新剧情",
      "evidence_quote": "从该章正文逐字复制的原句或连续片段，必须能在该章中唯一定位到问题所在位置",
      "evidence_quotes": [
        {{"chapter_no": {shown[0]}, "quote": "与 evidence_quote 完全相同的目标章逐字引文"}},
        {{"chapter_no": {shown[0]}, "quote": "与目标位置冲突的另一段 Canon 逐字引文"}}
      ],
      "suggested_class": "TEXT_ONLY/CONTINUITY_MINOR/REWRITE_SPAN/REWRITE_CHAPTER/DEFER_FUTURE",
      "must_preserve": ["目标章中必须逐字保留的原句；没有则留空"],
      "confidence": "high/medium/low"
    }}
  ]
}}

findings 规则：
- 只为「确认成立」的问题建条目；false_positives 不要进 findings。
- chapter_no 只能是本次给你正文的章节：{shown}。
- evidence_quote 与 evidence_quotes 必须逐字来自正文；缺少目标章或对照章任一侧就不能给 high。
- related_chapters 必须覆盖 evidence_quotes 中与目标章不同的章节。
- suggested_class 判定：
  TEXT_ONLY=日期、时间词、称谓、笔误、局部措辞等文字级错误，不改变任何剧情状态；
  CONTINUITY_MINOR=需要补少量句子或短段做衔接闭环，但该章结束时的长期状态不变；
  REWRITE_SPAN=需要重写一个段落或一小节，会改变局部事件细节；
  REWRITE_CHAPTER=该章核心事件结果本身就错了，需要重写整章；
  DEFER_FUTURE=不必回改旧正文，在后续章节自然补足即可。
- must_preserve 只能填写目标章中确实存在、修改后必须逐字保留的句子，不要写抽象事实。

state_ledger 规则：逐章抽取以后可能被推翻的持久事实，优先记录明确时间锚、醒来时间、场景是否已经结束、物品当前形态和记录方式、能力是否改动、人物已经知道什么、关系与亲密历史、实验或记录次数。每条都必须有该章逐字引文；不要只写剧情概括。方法、节奏、顺序、参数只要尝试或改动过，即使失败或恢复原状，也记 EVENT_OCCURRED；“从未、没有改过、第一次、一直”等记 NEGATED_HISTORY；第N次、总共N次、最新记录记 COUNT_OR_ORDINAL，并保留原文数字。

只有两侧正文证据明确支持时才确认问题。"""
        obj = self._audit_chat(
            f"audit_verify_{start:04d}_{end:04d}", system, user,
            model="deepseek-v4-pro", thinking=True, effort="high", max_tokens=9000,
        )
        obj["status"] = self._audit_status_level(obj.get("status"))
        obj["suspect_chapters"] = self._audit_chapter_list(obj.get("suspect_chapters"), start, end)
        raw_findings = []
        for key in ("findings", "confirmed_findings"):
            for raw in obj.get(key) if isinstance(obj.get(key), list) else []:
                if isinstance(raw, dict):
                    raw_findings.append(raw)
        normalized = self._audit_normalize_findings(raw_findings, shown)
        local_candidate_map = {
            str(row.get("candidate_id") or "").strip(): row
            for row in local_candidates
            if str(row.get("candidate_id") or "").strip()
        }
        for row in normalized:
            candidate_id = str(row.get("candidate_id") or "").strip()
            candidate = local_candidate_map.get(candidate_id)
            if candidate is None:
                continue
            link_reasons = []
            target_changed = (
                int(row.get("chapter_no") or 0)
                != int(candidate.get("chapter_no") or 0)
            )
            if target_changed:
                link_reasons.append("窗口 Pro 擅自改变了本地候选的目标章节")
            if str(row.get("category") or "") != str(candidate.get("category") or ""):
                link_reasons.append("窗口 Pro 擅自改变了本地候选的问题类别")
            expected_related = {
                int(value) for value in (candidate.get("related_chapters") or [])
            }
            actual_related = {
                int(value) for value in (row.get("related_chapters") or [])
            }
            if expected_related and not expected_related.intersection(actual_related):
                link_reasons.append("窗口 Pro 未引用本地候选指定的对照章节")
            if link_reasons:
                row["repair_ready"] = False
                row["gate_reasons"] = list(dict.fromkeys(
                    (row.get("gate_reasons") or []) + link_reasons
                ))
                row["chapter_no"] = int(candidate.get("chapter_no") or 0)
                row["related_chapters"] = list(
                    candidate.get("related_chapters") or []
                )[:12]
                row["category"] = str(candidate.get("category") or "")
                row["issue"] = str(candidate.get("issue") or row.get("issue") or "")
                if target_changed:
                    row["evidence_quote"] = ""
                    row["quote_is_verbatim"] = False
            row["source_scope"] = str(
                candidate.get("source_scope") or "segment_candidate"
            )
            row["candidate_disposition"] = (
                "confirmed" if row.get("repair_ready") else "rejected"
            )
        ready, review = self._audit_partition_findings(normalized)
        ready_candidate_ids = {
            str(row.get("candidate_id") or "").strip()
            for row in ready if str(row.get("candidate_id") or "").strip()
        }
        review = [
            row for row in review
            if str(row.get("candidate_id") or "").strip() not in ready_candidate_ids
        ]
        resolved_candidate_ids = ready_candidate_ids | {
            str(row.get("candidate_id") or "").strip()
            for row in review if str(row.get("candidate_id") or "").strip()
        }
        disposition_reasons = {}
        for raw in obj.get("candidate_dispositions") if isinstance(obj.get("candidate_dispositions"), list) else []:
            if not isinstance(raw, dict):
                continue
            candidate_id = str(raw.get("candidate_id") or "").strip()
            disposition = str(raw.get("disposition") or "").strip().upper()
            if candidate_id in local_candidate_map and disposition in {
                "CONFIRMED", "REVIEW", "FALSE_POSITIVE",
            }:
                disposition_reasons[candidate_id] = (
                    disposition, str(raw.get("reason") or "").strip()
                )
        false_rows = obj.get("false_positives") if isinstance(obj.get("false_positives"), list) else []
        for raw in false_rows:
            if isinstance(raw, dict):
                candidate_id = str(raw.get("candidate_id") or "").strip()
                reason = str(raw.get("reason") or raw.get("issue") or "").strip()
            else:
                match = re.search(r"L[FD]\d+", str(raw or ""), flags=re.I)
                candidate_id = match.group(0).upper() if match else ""
                reason = str(raw or "").strip()
            if candidate_id in local_candidate_map:
                disposition_reasons[candidate_id] = ("FALSE_POSITIVE", reason)
        for candidate_id, candidate in local_candidate_map.items():
            if candidate_id in resolved_candidate_ids:
                continue
            disposition, reason = disposition_reasons.get(
                candidate_id, ("OMITTED", "")
            )
            if disposition == "FALSE_POSITIVE":
                message = reason or "窗口 Pro 判为误报，保留排除结论供人工复核"
                disposition_value = "false_positive"
            elif disposition == "REVIEW":
                message = reason or "窗口 Pro 要求人工复核，未形成可提交的双边正文证据链"
                disposition_value = "review"
            elif disposition == "CONFIRMED":
                message = reason or "窗口 Pro 称候选成立，但没有返回可通过证据门的 finding"
                disposition_value = "rejected"
            else:
                message = "窗口 Pro 未返回该本地候选的结构化裁决，已保留供人工复核"
                disposition_value = "omitted"
            review.append(self._audit_unresolved_local_candidate(
                candidate, message, disposition=disposition_value
            ))
        boundary_checks = self._audit_normalize_boundary_checks(
            obj.get("boundary_checks"), shown
        )
        for check in boundary_checks:
            if check.get("result") == "CONSISTENT":
                continue
            left = int(check["from_chapter"])
            right = int(check["to_chapter"])
            already_retained = any(
                int(row.get("chapter_no") or 0) == right
                and left in (row.get("related_chapters") or [])
                for row in normalized
            )
            if already_retained:
                continue
            target_quote = (
                check.get("to_quote")
                if check.get("to_quote_is_verbatim") else ""
            )
            evidence_quotes = []
            if check.get("from_quote_is_verbatim"):
                evidence_quotes.append({
                    "chapter_no": left, "quote": check.get("from_quote"),
                    "quote_is_verbatim": True,
                })
            if target_quote:
                evidence_quotes.append({
                    "chapter_no": right, "quote": target_quote,
                    "quote_is_verbatim": True,
                })
            review.append({
                "finding_id": f"RB{right:04d}_{left:04d}",
                "candidate_id": f"BC{left:04d}_{right:04d}",
                "chapter_no": right,
                "related_chapters": [left],
                "category": "TIME_ROLLBACK",
                "issue": check.get("reason") or f"第{left}章至第{right}章接缝未得到明确一致结论",
                "required_fix": "",
                "evidence_quote": target_quote,
                "quote_is_verbatim": bool(target_quote),
                "evidence_quotes": evidence_quotes,
                "suggested_class": "",
                "must_preserve": [],
                "confidence": "medium" if check.get("result") == "CONFLICT" else "low",
                "repair_ready": False,
                "gate_reasons": [
                    "相邻章节接缝存在疑点，但尚未形成可自动修复的双边证据链"
                ],
                "source_scope": "segment_boundary",
                "candidate_disposition": "review",
            })

        assertion_map = {
            str(row.get("assertion_id") or ""): row for row in assertions
        }
        assertion_checks, checked_ids = [], set()
        for raw in obj.get("assertion_checks") if isinstance(obj.get("assertion_checks"), list) else []:
            if not isinstance(raw, dict):
                continue
            assertion_id = str(raw.get("assertion_id") or "").strip()
            if assertion_id not in assertion_map or assertion_id in checked_ids:
                continue
            result = str(raw.get("result") or "UNCERTAIN").strip().upper()
            if result not in {"CONSISTENT", "CONFLICT", "UNCERTAIN"}:
                result = "UNCERTAIN"
            related_ids = [
                str(x).strip() for x in (raw.get("related_assertion_ids") or [])
                if str(x).strip() in assertion_map and str(x).strip() != assertion_id
            ]
            assertion_checks.append({
                "assertion_id": assertion_id,
                "chapter_no": int(assertion_map[assertion_id].get("chapter_no") or 0),
                "assertion_quote": str(assertion_map[assertion_id].get("quote") or ""),
                "fact_kind_hint": str(
                    assertion_map[assertion_id].get("fact_kind_hint") or ""
                ),
                "result": result,
                "related_assertion_ids": list(dict.fromkeys(related_ids))[:12],
                "reason": str(raw.get("reason") or "").strip(),
            })
            checked_ids.add(assertion_id)
        missing_assertions = [
            assertion_id for assertion_id in assertion_map
            if assertion_id not in checked_ids
        ]
        retained_assertion_ids = {
            str(row.get("candidate_id") or "").removeprefix("AC")
            for row in [*ready, *review]
            if str(row.get("candidate_id") or "").startswith("AC")
        }
        finding_evidence = {
            (int(evidence.get("chapter_no") or 0), str(evidence.get("quote") or ""))
            for row in normalized
            for evidence in (row.get("evidence_quotes") or [])
            if isinstance(evidence, dict)
        }
        for check in assertion_checks:
            if check.get("result") == "CONSISTENT":
                continue
            assertion_id = str(check.get("assertion_id") or "")
            if assertion_id in retained_assertion_ids:
                continue
            involved_ids = [assertion_id, *(check.get("related_assertion_ids") or [])]
            involved = [
                assertion_map[value] for value in involved_ids
                if value in assertion_map
            ]
            if not involved:
                continue
            if any(
                (int(item.get("chapter_no") or 0), str(item.get("quote") or ""))
                in finding_evidence
                for item in involved
            ):
                continue
            target = max(involved, key=lambda item: int(item.get("chapter_no") or 0))
            target_chapter = int(target.get("chapter_no") or 0)
            target_quote = str(target.get("quote") or "")
            related = sorted({
                int(item.get("chapter_no") or 0) for item in involved
                if int(item.get("chapter_no") or 0) != target_chapter
            })
            review.append({
                "finding_id": f"RA{target_chapter:04d}_{assertion_id}",
                "candidate_id": f"AC{assertion_id}",
                "chapter_no": target_chapter,
                "related_chapters": related,
                "category": "OTHER_HARD_CONTINUITY",
                "issue": check.get("reason") or "硬断言逐项核对未得到明确一致结论",
                "required_fix": "",
                "evidence_quote": target_quote,
                "quote_is_verbatim": bool(
                    target_quote
                    and self._audit_quote_is_verbatim(target_chapter, target_quote)
                ),
                "evidence_quotes": [
                    {
                        "chapter_no": int(item.get("chapter_no") or 0),
                        "quote": str(item.get("quote") or ""),
                        "quote_is_verbatim": True,
                    }
                    for item in involved
                ][:16],
                "suggested_class": "",
                "must_preserve": [],
                "confidence": "medium" if check.get("result") == "CONFLICT" else "low",
                "repair_ready": False,
                "gate_reasons": [
                    "硬断言存在冲突或不确定性，但尚未形成可自动修复的完整证据链"
                ],
                "source_scope": "segment_assertion",
                "candidate_disposition": "review",
            })
        if missing_assertions:
            review.append({
                "finding_id": f"RP{end:04d}_{start:04d}",
                "candidate_id": f"AP{start:04d}_{end:04d}",
                "chapter_no": int(end),
                "related_chapters": [n for n in shown if n != int(end)][:12],
                "category": "OTHER_HARD_CONTINUITY",
                "issue": f"窗口 Pro 漏回 {len(missing_assertions)} 条硬断言的逐项裁决，复核协议不完整",
                "required_fix": "",
                "evidence_quote": "",
                "quote_is_verbatim": False,
                "evidence_quotes": [],
                "suggested_class": "",
                "must_preserve": [],
                "confidence": "low",
                "repair_ready": False,
                "gate_reasons": ["窗口硬断言检查未完整执行，不能据此自动判定无问题"],
                "source_scope": "segment_protocol",
                "candidate_disposition": "omitted",
                "missing_assertion_ids": missing_assertions[:32],
            })
        obj["findings"] = ready
        obj["review_findings"] = review
        obj["boundary_checks"] = boundary_checks
        obj["assertion_checks"] = assertion_checks
        obj["assertion_check_missing_ids"] = missing_assertions
        obj["protocol_complete"] = not any(
            row.get("protocol_missing") for row in boundary_checks
        ) and not missing_assertions
        if not obj["protocol_complete"] and obj["status"] == "GREEN":
            obj["status"] = "YELLOW"
        obj["state_ledger"] = self._audit_normalize_state_ledger(obj.get("state_ledger"), shown)
        obj["verified_chapters"] = shown
        obj["start"] = start; obj["end"] = end
        return obj

    def _audit_global(self, start, end, segments):
        compact = []
        for item in segments:
            audit = item.get("audit") or {}
            verification = item.get("verification") or {}
            # Both passes already had their quotes checked against Canon. Merge
            # them for recall; the global pass can only propose candidates and a
            # later Pro pass still reloads both full chapters before repair.
            state_ledger, seen_ledger = [], set()
            ledger_sources = [verification.get("state_ledger") or []]
            if item.get("verification") is None:
                ledger_sources = []
            ledger_sources.append(audit.get("state_ledger") or [])
            for source_rows in ledger_sources:
                for state in source_rows:
                    if not isinstance(state, dict):
                        continue
                    key = (
                        int(state.get("chapter_no") or 0),
                        str(state.get("category") or ""),
                        str(state.get("fact_kind") or ""),
                        str(state.get("entity") or ""),
                        str(state.get("state_key") or ""),
                        str(state.get("state_value") or ""),
                        str(state.get("evidence_quote") or ""),
                    )
                    if key in seen_ledger:
                        continue
                    seen_ledger.add(key)
                    state_ledger.append(state)
            row = {
                "segment": item.get("segment"),
                "status": verification.get("status") or audit.get("status"),
                "state_ledger": state_ledger,
                "confirmed_finding_signatures": [
                    {
                        "chapter_no": finding.get("chapter_no"),
                        "related_chapters": finding.get("related_chapters") or [],
                        "category": finding.get("category"),
                        "issue": finding.get("issue"),
                    }
                    for finding in (verification.get("findings") or [])
                    if isinstance(finding, dict)
                ],
                "assertion_checks": verification.get("assertion_checks") or [],
            }
            compact.append(row)
        assertions = self._audit_assertion_inventory(start, end)
        system = """你是网文硬连续性的全局比对员。你收到各窗口由正文抽取并校验的状态账本，以及程序从正文逐字提取的高风险历史断言。
你的任务只是在不同窗口之间寻找硬状态冲突：时间/星期倒退、场景或记忆复位、物品/能力/知识/关系历史被重写、次数或“最新记录”回退。不要审节奏、文风、爽点或普通大纲完成度。

逐项比较硬断言和账本，不得只读各段结论：
- EVENT_OCCURRED 与后文 NEGATED_HISTORY 冲突时必须列候选，即使此前改动失败或后来恢复原状；
- 第N次、总共N次、最新/最近一次必须按章节顺序核对，不能只比较两个数字；中间新增记录或运行也计入历史；
- 物品放入、交给、带走、归还等所有权/持有人变化必须形成事件链，后文“他的/她的”不得跳过转移过程；
- 相邻或跨窗的时间锚必须结合“刚才、昨晚、次日、还”等承接词串成证据链。

你此时没有全部正文，因此只能提出 candidate_findings，不能直接生成可修复 findings。候选必须指出需要修改的较后章节和至少一个确立旧事实的关联章节；后续程序会回读目标章、关联章及其前后相邻章的完整 Canon 再由 Pro 终审。候选阶段以召回为主：只要逐字断言或账本形成具体冲突迹象就列出，最终双边正文证据门负责阻止误修。必须输出严格 JSON。"""
        user = f"""全局范围：第{start}-{end}章。
【Outline 区间索引】
{self._audit_outline_index(start, end)}

【各阶段审计与证据复核】
{json.dumps(compact, ensure_ascii=False, indent=2)}

【程序从 Canon 逐字提取的硬断言清单】
{json.dumps(assertions, ensure_ascii=False, indent=2)}

输出严格 JSON：
{{
  "status": "GREEN/YELLOW/ORANGE/RED",
  "overall_summary": "全局执行状态概括",
  "outline_completion_pct": 0,
  "major_findings": [],
  "character_long_term_drift": [],
  "timeline_long_term_issues": [],
  "growth_progress_issues": [],
  "persistent_new_mainlines": [],
  "missing_or_early_goals": [],
  "recommended_action": "继续批量生成/后续章节纠偏/检查指定区间/暂停并回滚局部",
  "focus_ranges": [],
  "candidate_findings": [
    {{
      "candidate_id": "GC001",
      "chapter_no": {end},
      "related_chapters": [{start}],
      "category": "TIME_ROLLBACK/SCENE_REPLAY/MEMORY_RESET/STATE_REGRESSION/RELATIONSHIP_HISTORY_REWRITE/ITEM_STATE_RESET/ABILITY_RULE_CONTRADICTION/NUMERIC_ROLLBACK/KNOWLEDGE_RESET/LOCATION_RESET/OTHER_HARD_CONTINUITY",
      "issue": "两个窗口状态账本之间疑似存在的硬冲突",
      "ledger_evidence": []
    }}
  ]
}}
candidate_findings 只放跨窗口冲突；局部窗口已经确认的问题不要重复。候选无需满足自动修复证据门，但必须指向具体的逐字断言或状态账本冲突，不能写泛泛猜测。不要建议从第1章重写，优先给出最小必要处理范围。"""
        obj = self._audit_chat(
            f"audit_global_{start:04d}_{end:04d}", system, user,
            model="deepseek-v4-pro", thinking=True, effort="high", max_tokens=7500,
        )
        obj["status"] = self._audit_status_level(obj.get("status"))
        obj["candidate_findings"] = self._audit_normalize_global_candidates(
            obj.get("candidate_findings"), start, end
        )
        return obj

    def _audit_normalize_global_candidates(self, rows, start, end):
        out, seen = [], set()
        for raw in rows if isinstance(rows, list) else []:
            if not isinstance(raw, dict):
                continue
            try:
                n = int(raw.get("chapter_no") or 0)
            except Exception:
                continue
            if not int(start) <= n <= int(end):
                continue
            related = []
            for value in raw.get("related_chapters") or []:
                try:
                    chapter = int(value)
                except Exception:
                    continue
                if int(start) <= chapter <= int(end) and chapter != n and chapter not in related:
                    related.append(chapter)
            category = str(raw.get("category") or "").strip().upper()
            issue = str(raw.get("issue") or "").strip()
            if not related or category not in self.AUDIT_HARD_CATEGORIES or not issue:
                continue
            key = (n, tuple(related), category, issue)
            if key in seen:
                continue
            seen.add(key)
            out.append({
                "candidate_id": f"GC{len(out) + 1:03d}",
                "chapter_no": n,
                "related_chapters": related[:12],
                "category": category,
                "issue": issue,
                "ledger_evidence": raw.get("ledger_evidence") or [],
            })
        return out[:80]

    @staticmethod
    def _audit_global_context_chapters(start, end, candidates):
        """Expand every target/source endpoint by one chapter within the run."""
        endpoints = set()
        for row in candidates or []:
            if not isinstance(row, dict):
                continue
            for value in [row.get("chapter_no") or 0, *(row.get("related_chapters") or [])]:
                try:
                    n = int(value)
                except Exception:
                    continue
                if int(start) <= n <= int(end):
                    endpoints.add(n)
        return sorted({
            neighbor
            for n in endpoints
            for neighbor in (n - 1, n, n + 1)
            if int(start) <= neighbor <= int(end)
        })

    def _audit_global_candidate_batches(self, candidates, max_chapters=12,
                                        max_chars=70000, start=None, end=None):
        """Batch cross-window suspects while keeping every evidence chapter whole."""
        batches, current, current_chapters, current_chars = [], [], set(), 0
        sizes = {}
        endpoints = [
            int(value)
            for row in (candidates or []) if isinstance(row, dict)
            for value in [row.get("chapter_no") or 0, *(row.get("related_chapters") or [])]
            if str(value or "").isdigit() and int(value) > 0
        ]
        lower = int(start) if start is not None else max(1, min(endpoints, default=1) - 1)
        upper = int(end) if end is not None else max(endpoints, default=1) + 1
        for raw in candidates or []:
            chapters = set(self._audit_global_context_chapters(lower, upper, [raw]))
            added = chapters - current_chapters
            added_chars = 0
            for n in added:
                if n not in sizes:
                    p = self.root / "chapters" / f"{n:04d}.md"
                    sizes[n] = len(p.read_text(encoding="utf-8")) if p.exists() else 0
                added_chars += sizes[n]
            if current and (
                len(current_chapters | chapters) > int(max_chapters)
                or current_chars + added_chars > int(max_chars)
            ):
                batches.append(current)
                current, current_chapters, current_chars = [], set(), 0
                added = chapters
                added_chars = sum(sizes.get(n, 0) for n in added)
            current.append(raw)
            current_chapters |= chapters
            current_chars += added_chars
        if current:
            batches.append(current)
        return batches

    @staticmethod
    def _audit_unresolved_global_candidate(candidate, reason, disposition="omitted"):
        """Keep a global suspect visible when it cannot pass the repair gate."""
        n = int(candidate.get("chapter_no") or 0)
        candidate_id = str(candidate.get("candidate_id") or "").strip()
        return {
            "finding_id": f"R{n:04d}_{candidate_id or 'GLOBAL'}",
            "candidate_id": candidate_id,
            "chapter_no": n,
            "related_chapters": list(candidate.get("related_chapters") or [])[:12],
            "category": str(candidate.get("category") or "OTHER_HARD_CONTINUITY"),
            "issue": str(candidate.get("issue") or "全局状态比对提出的硬连续性疑点"),
            "required_fix": "",
            "evidence_quote": "",
            "quote_is_verbatim": False,
            "evidence_quotes": [],
            "ledger_evidence": candidate.get("ledger_evidence") or [],
            "suggested_class": "",
            "must_preserve": [],
            "confidence": "low",
            "repair_ready": False,
            "gate_reasons": [str(reason or "全局终审未形成可提交的双边正文证据链")],
            "source_scope": "global",
            "candidate_disposition": str(disposition or "omitted"),
        }

    def _audit_verify_global_candidates(self, start, end, candidates):
        """Reload endpoints and neighbors; account for every global candidate."""
        all_ready, all_review, all_false, all_shown = [], [], [], []
        batches = self._audit_global_candidate_batches(
            candidates, start=start, end=end
        )
        for batch_index, batch in enumerate(batches, 1):
            shown = self._audit_global_context_chapters(start, end, batch)
            chapters = []
            for n in shown:
                p = self.root / "chapters" / f"{n:04d}.md"
                if p.exists():
                    chapters.append(f"## 第{n}章完整 Canon 正文\n{p.read_text(encoding='utf-8').strip()}")
            if not chapters:
                for candidate in batch:
                    all_review.append(self._audit_unresolved_global_candidate(
                        candidate,
                        "候选涉及章节正文缺失，无法完成全局双边证据复核",
                        disposition="unavailable",
                    ))
                continue
            system = """你是跨章节硬连续性证据终审员。全局状态账本提出了候选，现在给你每个候选的目标章、关联章及这些端点前后各一章的完整 Canon 正文。
只判断候选是否由正文明确支持，不扩展成节奏、文风或大纲执行意见。必须逐字引用两侧正文：evidence_quote 是需要修改的目标章锚点；evidence_quotes 同时覆盖目标章锚点和至少一个关联章旧事实。没有双侧证据、存在合理时间解释、属于回忆/倒叙，或只是措辞含混时，不得给 high，也不得进入确认 findings。“合上/收起”不自动等于“全部完成”，未注明周次的星期词也不能自行绑定到当前周。必须输出严格 JSON。"""
            user = f"""审计范围：第{start}-{end}章；当前批次 {batch_index}/{len(batches)}。

【全局候选】
{json.dumps(batch, ensure_ascii=False, indent=2)}

【候选端点及前后相邻章的完整 Canon 正文；相邻章只用于补全事件链，不得擅自改变候选目标章】
{chr(10).join(chapters)}

输出严格 JSON：
{{
  "dispositions": [
    {{
      "candidate_id": "每个输入候选都必须恰好出现一次",
      "disposition": "CONFIRMED/REVIEW/FALSE_POSITIVE",
      "reason": "简短说明裁决依据"
    }}
  ],
  "false_positives": [
    {{"candidate_id": "GCxxx", "reason": "排除理由"}}
  ],
  "findings": [
    {{
      "candidate_id": "必须逐字复制对应全局候选的 candidate_id",
      "chapter_no": {shown[-1]},
      "related_chapters": [{shown[0]}],
      "category": "TIME_ROLLBACK/SCENE_REPLAY/MEMORY_RESET/STATE_REGRESSION/RELATIONSHIP_HISTORY_REWRITE/ITEM_STATE_RESET/ABILITY_RULE_CONTRADICTION/NUMERIC_ROLLBACK/KNOWLEDGE_RESET/LOCATION_RESET/OTHER_HARD_CONTINUITY",
      "issue": "正文明确支持的硬连续性冲突",
      "required_fix": "只修正较后目标章，恢复既有 Canon 事实；不新编历史",
      "evidence_quote": "目标章需要修改位置的逐字引文",
      "evidence_quotes": [
        {{"chapter_no": {shown[-1]}, "quote": "与 evidence_quote 完全相同的目标章逐字引文"}},
        {{"chapter_no": {shown[0]}, "quote": "关联章确立旧事实的逐字引文"}}
      ],
      "suggested_class": "TEXT_ONLY/CONTINUITY_MINOR/REWRITE_SPAN/REWRITE_CHAPTER",
      "must_preserve": [],
      "confidence": "high/medium/low"
    }}
  ]
}}
只确认给出的候选。每个输入 candidate_id 必须在 dispositions 中恰好裁决一次，不能漏项。CONFIRMED 必须同时给出 finding；REVIEW 和 FALSE_POSITIVE 必须说明 reason。每条 finding 必须保留对应 candidate_id，且 chapter_no、category 和关联旧事实章节必须与该候选一致。must_preserve 只能放目标章中必须逐字保留的原句。"""
            obj = self._audit_chat(
                f"audit_global_verify_{batch_index:03d}", system, user,
                model="deepseek-v4-pro", thinking=True, effort="high", max_tokens=9000,
            )
            candidate_map = {
                str(row.get("candidate_id") or "").strip(): row
                for row in batch
                if str(row.get("candidate_id") or "").strip()
            }
            raw_findings = []
            for key in ("findings", "confirmed_findings"):
                for raw in obj.get(key) if isinstance(obj.get(key), list) else []:
                    if isinstance(raw, dict):
                        raw_findings.append(raw)
            normalized = self._audit_normalize_findings(raw_findings, shown)
            resolved = {}
            for row in normalized:
                candidate_id = str(row.get("candidate_id") or "").strip()
                candidate = candidate_map.get(candidate_id)
                link_reasons = []
                if not candidate:
                    continue
                if int(row.get("chapter_no") or 0) != int(candidate.get("chapter_no") or 0):
                    link_reasons.append("全局终审擅自改变了目标章节")
                if str(row.get("category") or "") != str(candidate.get("category") or ""):
                    link_reasons.append("全局终审擅自改变了问题类别")
                expected_related = {
                    int(x) for x in (candidate.get("related_chapters") or [])
                }
                actual_related = {
                    int(x) for x in (row.get("related_chapters") or [])
                }
                if not expected_related.intersection(actual_related):
                    link_reasons.append("全局终审未引用候选指定的旧事实章节")
                if link_reasons:
                    row["repair_ready"] = False
                    row["gate_reasons"] = list(dict.fromkeys(
                        (row.get("gate_reasons") or []) + link_reasons
                    ))
                    target_changed = (
                        int(row.get("chapter_no") or 0)
                        != int(candidate.get("chapter_no") or 0)
                    )
                    row["chapter_no"] = int(candidate.get("chapter_no") or 0)
                    row["related_chapters"] = list(
                        candidate.get("related_chapters") or []
                    )[:12]
                    row["category"] = str(candidate.get("category") or "")
                    row["issue"] = str(candidate.get("issue") or row.get("issue") or "")
                    if target_changed:
                        row["evidence_quote"] = ""
                        row["quote_is_verbatim"] = False
                row["source_scope"] = "global"
                row["candidate_disposition"] = (
                    "confirmed" if row.get("repair_ready") else "rejected"
                )
                existing = resolved.get(candidate_id)
                if existing is None or (
                    row.get("repair_ready") and not existing.get("repair_ready")
                ):
                    resolved[candidate_id] = row

            disposition_reasons = {}
            for raw in obj.get("dispositions") if isinstance(obj.get("dispositions"), list) else []:
                if not isinstance(raw, dict):
                    continue
                candidate_id = str(raw.get("candidate_id") or "").strip()
                disposition = str(raw.get("disposition") or "").strip().upper()
                if candidate_id in candidate_map and disposition in {
                    "CONFIRMED", "REVIEW", "FALSE_POSITIVE",
                }:
                    disposition_reasons[candidate_id] = (
                        disposition, str(raw.get("reason") or "").strip()
                    )
            false_rows = obj.get("false_positives") if isinstance(obj.get("false_positives"), list) else []
            for raw in false_rows:
                if isinstance(raw, dict):
                    candidate_id = str(raw.get("candidate_id") or "").strip()
                    reason = str(raw.get("reason") or raw.get("issue") or "").strip()
                else:
                    match = re.search(r"GC\d+", str(raw or ""), flags=re.I)
                    candidate_id = match.group(0).upper() if match else ""
                    reason = str(raw or "").strip()
                if candidate_id in candidate_map:
                    disposition_reasons[candidate_id] = ("FALSE_POSITIVE", reason)

            for candidate_id, candidate in candidate_map.items():
                row = resolved.get(candidate_id)
                if row is not None and row.get("repair_ready"):
                    row["finding_id"] = (
                        f"G{int(row['chapter_no']):04d}_{len(all_ready) + 1:03d}"
                    )
                    all_ready.append(row)
                    continue
                if row is not None:
                    row["finding_id"] = (
                        f"GR{int(candidate.get('chapter_no') or 0):04d}_{len(all_review) + 1:03d}"
                    )
                    all_review.append(row)
                    continue
                disposition, reason = disposition_reasons.get(
                    candidate_id, ("OMITTED", "")
                )
                if disposition == "FALSE_POSITIVE":
                    message = reason or "全局终审判为误报，但未形成可核验的双边排除证据"
                    disposition_value = "false_positive"
                elif disposition == "REVIEW":
                    message = reason or "全局终审要求人工复核，未形成可提交的双边正文证据链"
                    disposition_value = "review"
                elif disposition == "CONFIRMED":
                    message = reason or "全局终审称候选成立，但没有返回可通过证据门的 finding"
                    disposition_value = "rejected"
                else:
                    message = "全局终审未返回该候选的结构化裁决，已保留供人工复核"
                    disposition_value = "omitted"
                all_review.append(self._audit_unresolved_global_candidate(
                    candidate, message, disposition=disposition_value
                ))
            all_false.extend(false_rows)
            all_shown.extend(n for n in shown if n not in all_shown)
        candidate_dispositions = [
            {
                "candidate_id": str(row.get("candidate_id") or ""),
                "disposition": (
                    "confirmed" if row.get("repair_ready")
                    else str(row.get("candidate_disposition") or "review")
                ),
            }
            for row in [*all_ready, *all_review]
            if str(row.get("candidate_id") or "")
        ]
        return {
            "status": "ORANGE" if all_ready else ("YELLOW" if all_review else "GREEN"),
            "findings": all_ready,
            "review_findings": all_review,
            "false_positives": all_false,
            "candidate_dispositions": candidate_dispositions,
            "verified_chapters": all_shown,
        }

    @staticmethod
    def _audit_md_list(value):
        rows = value if isinstance(value, list) else []
        return "\n".join(f"- {str(x)}" for x in rows if str(x).strip()) or "- 无"

    @staticmethod
    def _audit_collect_findings(segments, global_result=None):
        """Merge local and globally verified findings without losing attribution.

        This list is the machine-readable contract consumed by the repair
        planner, which lets the planner skip LLM extraction entirely.
        """
        candidates = []
        for item in segments or []:
            v = item.get("verification") or {}
            for row in v.get("findings") or []:
                if isinstance(row, dict):
                    candidates.append(dict(row))
        for row in (global_result or {}).get("findings") or []:
            if isinstance(row, dict):
                candidates.append(dict(row))

        out, by_content = [], {}
        for row in candidates:
            content_key = (
                int(row.get("chapter_no") or 0),
                str(row.get("category") or "").strip(),
                str(row.get("evidence_quote") or "").strip(),
                str(row.get("issue") or "").strip(),
                str(row.get("required_fix") or "").strip(),
            )
            existing = by_content.get(content_key)
            if existing is not None:
                existing["related_chapters"] = list(dict.fromkeys(
                    (existing.get("related_chapters") or [])
                    + (row.get("related_chapters") or [])
                ))[:12]
                existing["evidence_quotes"] = list({
                    (int(x.get("chapter_no") or 0), str(x.get("quote") or "")): x
                    for x in (
                        (existing.get("evidence_quotes") or [])
                        + (row.get("evidence_quotes") or [])
                    )
                    if isinstance(x, dict)
                }.values())[:16]
                continue
            out.append(row)
            by_content[content_key] = row
        out.sort(key=lambda x: (int(x.get("chapter_no") or 0), str(x.get("finding_id") or "")))
        used_ids = set()
        for row_index, row in enumerate(out, 1):
            finding_id = str(row.get("finding_id") or "").strip()
            if not finding_id or finding_id in used_ids:
                prefix = "G" if row.get("source_scope") == "global" else "V"
                serial = row_index
                finding_id = f"{prefix}{int(row.get('chapter_no') or 0):04d}_{serial:03d}"
                while finding_id in used_ids:
                    serial += 1
                    finding_id = f"{prefix}{int(row.get('chapter_no') or 0):04d}_{serial:03d}"
                row["finding_id"] = finding_id
            used_ids.add(finding_id)
        return out

    @staticmethod
    def _audit_collect_review_findings(segments, global_result=None,
                                       ready_findings=None):
        candidates = []
        for item in segments or []:
            for row in (item.get("verification") or {}).get("review_findings") or []:
                if isinstance(row, dict):
                    candidates.append(dict(row))
        for row in (global_result or {}).get("review_findings") or []:
            if isinstance(row, dict):
                candidates.append(dict(row))
        ready_candidate_ids = {
            str(row.get("candidate_id") or "").strip()
            for row in (ready_findings or []) if isinstance(row, dict)
            if str(row.get("candidate_id") or "").strip()
        }
        ready_content = {
            (
                int(row.get("chapter_no") or 0),
                str(row.get("category") or "").strip(),
                str(row.get("evidence_quote") or "").strip(),
                str(row.get("issue") or "").strip(),
                str(row.get("required_fix") or "").strip(),
            )
            for row in (ready_findings or []) if isinstance(row, dict)
        }
        out, seen_content = [], set()
        for row in candidates:
            content_key = (
                int(row.get("chapter_no") or 0),
                str(row.get("category") or "").strip(),
                str(row.get("evidence_quote") or "").strip(),
                str(row.get("issue") or "").strip(),
                str(row.get("required_fix") or "").strip(),
            )
            candidate_id = str(row.get("candidate_id") or "").strip()
            if (
                (candidate_id and candidate_id in ready_candidate_ids)
                or content_key in ready_content
            ):
                continue
            dedupe_key = (candidate_id, *content_key) if candidate_id else content_key
            if dedupe_key in seen_content:
                continue
            seen_content.add(dedupe_key)
            out.append(row)
        out.sort(key=lambda x: (int(x.get("chapter_no") or 0), str(x.get("finding_id") or "")))
        used_ids = set()
        for row_index, row in enumerate(out, 1):
            finding_id = str(row.get("finding_id") or "").strip()
            if not finding_id or finding_id in used_ids:
                finding_id = f"R{int(row.get('chapter_no') or 0):04d}_{row_index:03d}"
                serial = row_index
                while finding_id in used_ids:
                    serial += 1
                    finding_id = f"R{int(row.get('chapter_no') or 0):04d}_{serial:03d}"
                row["finding_id"] = finding_id
            used_ids.add(finding_id)
        return out

    @staticmethod
    def _audit_finalize_global_result(global_result, findings, review_findings):
        """Replace preliminary model prose with the final evidence-gated verdict."""
        result = dict(global_result or {})
        ready = [row for row in (findings or []) if isinstance(row, dict)]
        review = [row for row in (review_findings or []) if isinstance(row, dict)]
        if ready:
            status = "ORANGE"
            action = "按可定位修复项执行最小修改；人工复核项只生成预览，不得自动提交"
        elif review:
            status = "YELLOW"
            action = "先人工复核保留疑点；可生成预览候选，但不得自动提交"
        else:
            status = "GREEN"
            action = "未发现通过证据门或需要保留复核的硬连续性问题，可继续批量生成"

        chapters = sorted({
            int(value)
            for row in [*ready, *review]
            for value in [row.get("chapter_no") or 0, *(row.get("related_chapters") or [])]
            if str(value or "").isdigit() and int(value) > 0
        })
        focus_ranges = []
        if chapters:
            range_start = range_end = chapters[0]
            for n in chapters[1:]:
                if n == range_end + 1:
                    range_end = n
                    continue
                focus_ranges.append(
                    f"第{range_start}章" if range_start == range_end
                    else f"第{range_start}-{range_end}章"
                )
                range_start = range_end = n
            focus_ranges.append(
                f"第{range_start}章" if range_start == range_end
                else f"第{range_start}-{range_end}章"
            )

        result.update({
            "status": status,
            "overall_summary": (
                f"终审结果：自动修复准入 {len(ready)} 项；"
                f"人工复核 {len(review)} 项。所有未通过双边正文证据门的疑点均未获自动提交资格。"
            ),
            "major_findings": [
                f"第{int(row.get('chapter_no') or 0)}章：{str(row.get('issue') or '').strip()}"
                for row in ready
            ],
            "focus_ranges": focus_ranges,
            "recommended_action": action,
            "final_findings_count": len(ready),
            "final_review_findings_count": len(review),
        })
        return result

    def _audit_render_findings_md(self, findings):
        if not findings:
            return "- 无可自动定位的修复项"
        lines = []
        for row in findings:
            n = int(row.get("chapter_no") or 0)
            cls = str(row.get("suggested_class") or "未分类")
            lines.append(f"### 第{n}章 · {cls} · {row.get('finding_id', '')}")
            lines.append("")
            lines.append(f"- 问题：{row.get('issue', '')}")
            lines.append(f"- 需要的修正：{row.get('required_fix', '')}")
            related = row.get("related_chapters") or []
            if related:
                lines.append(f"- 关联章节：{related}")
            preserve = row.get("must_preserve") or []
            if preserve:
                lines.append(f"- 必须保持：{'；'.join(str(x) for x in preserve)}")
            conf = str(row.get("confidence") or "").strip()
            if conf:
                lines.append(f"- 置信度：{conf}")
            gate_reasons = row.get("gate_reasons") or []
            if gate_reasons:
                lines.append(f"- 未进入自动修复：{'；'.join(str(x) for x in gate_reasons)}")
            quote = str(row.get("evidence_quote") or "").strip()
            if quote:
                lines.append("- 正文证据：")
                lines.append("")
                lines.append("```")
                lines.append(quote)
                lines.append("```")
            else:
                lines.append("- 正文证据：（复核未能定位到具体语句）")
            comparisons = [
                x for x in (row.get("evidence_quotes") or [])
                if isinstance(x, dict)
                and not (
                    int(x.get("chapter_no") or 0) == n
                    and str(x.get("quote") or "").strip() == quote
                )
            ]
            if comparisons:
                lines.append("- 冲突对照证据：")
                for evidence in comparisons:
                    lines.append(
                        f"  - 第{int(evidence.get('chapter_no') or 0)}章："
                        f"{str(evidence.get('quote') or '').strip()}"
                    )
            lines.append("")
        return "\n".join(lines).strip()

    def _audit_render_markdown(self, run):
        g = run.get("global", {})
        lines = [
            f"# 剧情一致性审计：第{run['start']}-{run['end']}章",
            "",
            f"- 总体状态：**{g.get('status', 'UNKNOWN')}**",
            f"- Outline 完成度：**{g.get('outline_completion_pct', '—')}%**",
            f"- 全文重叠窗口：{run.get('segment_size', 4)}章（重叠1章）",
            f"- Pro 独立整窗扫描：{'开启' if run.get('source_check') else '关闭（仅观察，不生成可修复项）'}",
            f"- 自动修复准入项：{len(run.get('findings') or [])}；仅供人工复核项：{len(run.get('review_only_findings') or [])}",
            f"- DeepSeek 请求数：{run.get('billing', {}).get('request_count', 0)}",
            f"- DeepSeek 官方估算：¥{float(run.get('billing', {}).get('cost_cny', 0) or 0):.4f}",
            f"- 火山 Agent Plan 估算：{float(run.get('billing', {}).get('afp', 0) or 0):.3f} AFP",
            "",
            "## 总体判断", "", str(g.get("overall_summary", "")), "",
            "## 主要发现", "", self._audit_md_list(g.get("major_findings")), "",
            "## 长期人物漂移", "", self._audit_md_list(g.get("character_long_term_drift")), "",
            "## 时间线", "", self._audit_md_list(g.get("timeline_long_term_issues")), "",
            "## 成长进度", "", self._audit_md_list(g.get("growth_progress_issues")), "",
            "## 持续性新主线", "", self._audit_md_list(g.get("persistent_new_mainlines")), "",
            "## 遗漏或提前目标", "", self._audit_md_list(g.get("missing_or_early_goals")), "",
            "## 建议", "", str(g.get("recommended_action", "")), "",
            "## 建议关注区间", "", self._audit_md_list(g.get("focus_ranges")), "",
            "## 可定位修复项", "",
            f"（共 {len(run.get('findings') or [])} 项；修复流水线优先读取 report.json 中的结构化 findings）", "",
            self._audit_render_findings_md(run.get("findings")), "",
            "## 仅供人工复核项", "",
            f"（共 {len(run.get('review_only_findings') or [])} 项；可生成预览候选，但不会获得自动提交资格）", "",
            self._audit_render_findings_md(run.get("review_only_findings")), "",
            "## 分段结果", "",
        ]
        for item in run.get("segments", []):
            a = item.get("audit", {})
            v = item.get("verification")
            lines += [
                f"### {item.get('segment', '')} · {a.get('status', 'UNKNOWN')}", "",
                str(a.get("segment_summary", "")), "",
                f"- Outline 完成度：{a.get('outline_completion_pct', '—')}%",
                f"- 建议：{a.get('recommended_action', '')}",
            ]
            if v:
                lines += [f"- 正文复核：{v.get('status', 'UNKNOWN')} · {v.get('recommended_action', '')}"]
            lines.append("")
        return "\n".join(lines).strip() + "\n"

    AUDIT_BILLING_FIELDS = (
        "prompt_tokens", "cache_hit_tokens", "completion_tokens",
        "reasoning_tokens", "cost_cny", "afp", "request_count",
    )

    @staticmethod
    def _audit_json_digest(value):
        payload = json.dumps(
            value, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), default=str,
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def _audit_billing_snapshot(self):
        with self.audit_lock:
            return {
                key: self.audit_status.get(key, 0)
                for key in self.AUDIT_BILLING_FIELDS
            }

    def _audit_chapter_hashes(self, start, end):
        hashes = {}
        for n in range(int(start), int(end) + 1):
            path = self.root / "chapters" / f"{n:04d}.md"
            if path.exists():
                hashes[str(n)] = hashlib.sha256(path.read_bytes()).hexdigest()
        return hashes

    def _audit_segment_input_hash(self, start, end):
        """Hash every local input used by the Flash window pass."""
        payload = {
            "pipeline_revision": self.AUDIT_PIPELINE_REVISION,
            "stage": "segment",
            "start": int(start),
            "end": int(end),
            "model": "deepseek-v4-flash",
            "thinking": True,
            "reasoning_effort": "low",
            "outline": self._audit_outline_for_range(start, end),
            "material": self._audit_segment_material(start, end),
            "deterministic": self._audit_deterministic_window(start, end),
            "assertion_inventory": self._audit_assertion_inventory(start, end),
        }
        return self._audit_json_digest(payload)

    def _audit_verify_input_hash(self, start, end, segment):
        chapters = {}
        for n in range(int(start), int(end) + 1):
            path = self.root / "chapters" / f"{n:04d}.md"
            if path.exists():
                chapters[str(n)] = path.read_text(encoding="utf-8")
        return self._audit_json_digest({
            "pipeline_revision": self.AUDIT_PIPELINE_REVISION,
            "stage": "verify_segment",
            "start": int(start),
            "end": int(end),
            "model": "deepseek-v4-pro",
            "thinking": True,
            "reasoning_effort": "high",
            "outline": self._audit_outline_for_range(start, end),
            "segment": segment,
            "assertion_inventory": self._audit_assertion_inventory(start, end),
            "chapters": chapters,
        })

    def _audit_global_input_hash(self, start, end, segments):
        return self._audit_json_digest({
            "pipeline_revision": self.AUDIT_PIPELINE_REVISION,
            "stage": "global",
            "start": int(start),
            "end": int(end),
            "model": "deepseek-v4-pro",
            "thinking": True,
            "reasoning_effort": "high",
            "outline_index": self._audit_outline_index(start, end),
            "assertion_inventory": self._audit_assertion_inventory(start, end),
            "segments": segments,
        })

    def _audit_global_verify_input_hash(self, start, end, candidates):
        involved = self._audit_global_context_chapters(start, end, candidates)
        chapter_hashes = {}
        for n in involved:
            path = self.root / "chapters" / f"{n:04d}.md"
            if path.exists():
                chapter_hashes[str(n)] = hashlib.sha256(path.read_bytes()).hexdigest()
        return self._audit_json_digest({
            "pipeline_revision": self.AUDIT_PIPELINE_REVISION,
            "stage": "global_verify",
            "start": int(start),
            "end": int(end),
            "model": "deepseek-v4-pro",
            "thinking": True,
            "reasoning_effort": "high",
            "candidates": candidates,
            "chapter_hashes": chapter_hashes,
        })

    def _audit_source_snapshot(self, start, end, ranges):
        chapter_hashes = self._audit_chapter_hashes(start, end)
        window_input_hashes = {
            f"{int(a):04d}_{int(b):04d}": self._audit_segment_input_hash(a, b)
            for a, b in ranges
        }
        payload = {
            "pipeline_revision": self.AUDIT_PIPELINE_REVISION,
            "chapter_hashes": chapter_hashes,
            "window_input_hashes": window_input_hashes,
            "global_outline_index": self._audit_outline_index(start, end),
        }
        return {
            "chapter_hashes": chapter_hashes,
            "window_input_hashes": window_input_hashes,
            "fingerprint": self._audit_json_digest(payload),
        }

    def _audit_find_resumable_run(self, request, ranges, source_snapshot):
        run_root = self.root / "reports" / "audit_runs"
        if not run_root.exists():
            return None, None, None
        candidates = []
        for state_path in run_root.glob("*/run_state.json"):
            try:
                candidates.append((state_path.stat().st_mtime, state_path))
            except OSError:
                continue
        expected_ranges = [[int(a), int(b)] for a, b in ranges]
        for _, state_path in sorted(candidates, reverse=True):
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(state, dict):
                continue
            if (state_path.parent / "report.json").exists():
                continue
            if int(state.get("checkpoint_version") or 0) != self.AUDIT_CHECKPOINT_VERSION:
                continue
            if str(state.get("pipeline_revision") or "") != self.AUDIT_PIPELINE_REVISION:
                continue
            if int(state.get("audit_schema_version") or 0) != self.AUDIT_SCHEMA_VERSION:
                continue
            if state.get("status") not in {"running", "failed", "stopped"}:
                continue
            if state.get("request") != request or state.get("ranges") != expected_ranges:
                continue
            if state.get("source_fingerprint") != source_snapshot["fingerprint"]:
                continue
            if state.get("chapter_hashes") != source_snapshot["chapter_hashes"]:
                continue
            if state.get("window_input_hashes") != source_snapshot["window_input_hashes"]:
                continue
            if not isinstance(state.get("checkpoints"), dict):
                continue
            return state_path.parent.name, state_path.parent, state
        return None, None, None

    def _audit_load_checkpoint(self, run_dir, state, key, filename,
                               input_hash, dependency_hash=""):
        record = (state.get("checkpoints") or {}).get(str(key))
        if not isinstance(record, dict) or record.get("complete") is not True:
            return None
        if str(record.get("input_hash") or "") != str(input_hash):
            return None
        if str(record.get("dependency_hash") or "") != str(dependency_hash or ""):
            return None
        path = Path(run_dir) / str(filename)
        try:
            output = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None
        if not isinstance(output, dict):
            return None
        if self._audit_json_digest(output) != str(record.get("output_hash") or ""):
            return None
        return output

    def _audit_save_checkpoint(self, run_dir, state, key, filename, output,
                               input_hash, dependency_hash=""):
        if not isinstance(output, dict):
            raise RuntimeError(f"审计阶段 {key} 没有生成可保存的结构化结果")
        path = Path(run_dir) / str(filename)
        atomic_write_json(path, output)
        output_hash = self._audit_json_digest(output)
        state.setdefault("checkpoints", {})[str(key)] = {
            "complete": True,
            "file": str(filename),
            "input_hash": str(input_hash),
            "dependency_hash": str(dependency_hash or ""),
            "output_hash": output_hash,
            "completed_at": datetime.now().isoformat(timespec="seconds"),
        }
        state["billing"] = self._audit_billing_snapshot()
        state["status"] = "running"
        state["last_error"] = ""
        state["updated_at"] = datetime.now().isoformat(timespec="seconds")
        atomic_write_json(Path(run_dir) / "run_state.json", state)
        return output_hash

    def _audit_assert_source_snapshot(self, state, start, end, ranges):
        current = self._audit_source_snapshot(start, end, ranges)
        if current["fingerprint"] != state.get("source_fingerprint"):
            raise RuntimeError(
                "审计期间 Canon 或辅助审计资料已变化；已停止混用旧断点，请重新启动审计"
            )
        return current

    def _run_story_audit(self, start, end, segment_size, source_check):
        ranges = audit_windows(start, end, size=segment_size, overlap=1)
        if not adjacent_seams_covered(ranges, start, end):
            raise RuntimeError("全文审计窗口未覆盖所有相邻章节接缝")
        request = {
            "start": int(start), "end": int(end),
            "segment_size": int(segment_size),
            "source_check": bool(source_check),
        }
        source_snapshot = self._audit_source_snapshot(start, end, ranges)
        run_id, run_dir, run_state = self._audit_find_resumable_run(
            request, ranges, source_snapshot
        )
        resumed = bool(run_state)
        now = datetime.now().isoformat(timespec="seconds")
        if not resumed:
            run_id = datetime.now().strftime("%Y%m%d_%H%M%S") + f"_{start:04d}_{end:04d}"
            run_dir = self.root / "reports" / "audit_runs" / run_id
            suffix = 2
            while run_dir.exists():
                run_id = (
                    datetime.now().strftime("%Y%m%d_%H%M%S")
                    + f"_{start:04d}_{end:04d}_{suffix:02d}"
                )
                run_dir = self.root / "reports" / "audit_runs" / run_id
                suffix += 1
            run_dir.mkdir(parents=True, exist_ok=False)
            run_state = {
                "checkpoint_version": self.AUDIT_CHECKPOINT_VERSION,
                "pipeline_revision": self.AUDIT_PIPELINE_REVISION,
                "audit_schema_version": self.AUDIT_SCHEMA_VERSION,
                "run_id": run_id,
                "status": "running",
                "request": request,
                "ranges": [[int(a), int(b)] for a, b in ranges],
                "source_fingerprint": source_snapshot["fingerprint"],
                "chapter_hashes": source_snapshot["chapter_hashes"],
                "window_input_hashes": source_snapshot["window_input_hashes"],
                "checkpoints": {},
                "billing": {key: 0 for key in self.AUDIT_BILLING_FIELDS},
                "created_at": now,
                "updated_at": now,
                "last_error": "",
            }
        else:
            run_state["status"] = "running"
            run_state["resumed_at"] = now
            run_state["updated_at"] = now
            run_state["last_error"] = ""
        # The run manifest is committed before the first model call.  A crash or
        # timeout can therefore resume only checkpoints tied to these exact inputs.
        atomic_write_json(run_dir / "run_state.json", run_state)
        saved_billing = dict(run_state.get("billing") or {})
        with self.audit_lock:
            self.audit_status.update({
                "running": True, "run_id": run_id, "start": int(start), "end": int(end),
                "segment_size": int(segment_size), "segment_index": 0, "segment_total": len(ranges),
                "stage": "准备", "stage_label": "恢复审计断点" if resumed else "准备审计资料",
                "started_at": time.time(), "resumed": resumed,
                "last_error": "", "report_file": "", "status": "", "source_check": bool(source_check),
                "prompt_tokens": int(saved_billing.get("prompt_tokens", 0) or 0),
                "cache_hit_tokens": int(saved_billing.get("cache_hit_tokens", 0) or 0),
                "completion_tokens": int(saved_billing.get("completion_tokens", 0) or 0),
                "reasoning_tokens": int(saved_billing.get("reasoning_tokens", 0) or 0),
                "cost_cny": float(saved_billing.get("cost_cny", 0.0) or 0.0),
                "afp": float(saved_billing.get("afp", 0.0) or 0.0),
                "request_count": int(saved_billing.get("request_count", 0) or 0),
            })
        if resumed:
            self.log(f"剧情审计续跑：复用运行 {run_id} 中已校验的阶段断点。")
        try:
            segments = []
            for idx, (a, b) in enumerate(ranges, 1):
                self._audit_check_cancel()
                with self.audit_lock:
                    self.audit_status["segment_index"] = idx
                self._audit_set_stage("分段审计", f"{idx}/{len(ranges)} · 第{a}-{b}章 · Flash Thinking")
                range_key = f"{a:04d}_{b:04d}"
                segment_key = f"segment_{range_key}"
                segment_file = f"segment_{range_key}.json"
                segment_input_hash = run_state["window_input_hashes"][range_key]
                audit = self._audit_load_checkpoint(
                    run_dir, run_state, segment_key, segment_file,
                    segment_input_hash,
                )
                if audit is None:
                    audit = self._audit_segment(a, b)
                    segment_output_hash = self._audit_save_checkpoint(
                        run_dir, run_state, segment_key, segment_file, audit,
                        segment_input_hash,
                    )
                else:
                    segment_output_hash = self._audit_json_digest(audit)
                    self.log(f"剧情审计断点复用：第{a}-{b}章 Flash 已完成。")
                verification = None
                if bool(source_check):
                    self._audit_set_stage("正文复核", f"第{a}-{b}章独立全文扫描 · Pro Thinking high")
                    verify_key = f"verify_{range_key}"
                    verify_file = f"verify_{range_key}.json"
                    verify_input_hash = self._audit_verify_input_hash(a, b, audit)
                    verification = self._audit_load_checkpoint(
                        run_dir, run_state, verify_key, verify_file,
                        verify_input_hash, dependency_hash=segment_output_hash,
                    )
                    if verification is None:
                        verification = self._audit_verify_segment(audit)
                        if verification:
                            self._audit_save_checkpoint(
                                run_dir, run_state, verify_key, verify_file,
                                verification, verify_input_hash,
                                dependency_hash=segment_output_hash,
                            )
                    else:
                        self.log(f"剧情审计断点复用：第{a}-{b}章 Pro 已完成。")
                segments.append({"segment": f"第{a}-{b}章", "audit": audit, "verification": verification})
            self._audit_check_cancel()
            source_snapshot = self._audit_assert_source_snapshot(
                run_state, start, end, ranges
            )
            self._audit_set_stage("全局综合", f"第{start}-{end}章 · Pro Thinking")
            global_input_hash = self._audit_global_input_hash(start, end, segments)
            global_result = self._audit_load_checkpoint(
                run_dir, run_state, "global", "global.json", global_input_hash,
            )
            if global_result is None:
                global_result = self._audit_global(start, end, segments)
                self._audit_save_checkpoint(
                    run_dir, run_state, "global", "global.json",
                    global_result, global_input_hash,
                )
            else:
                self.log("剧情审计断点复用：全局综合已完成。")
            global_verification = None
            candidates = global_result.get("candidate_findings") or []
            if bool(source_check) and candidates:
                self._audit_set_stage(
                    "全局证据复核",
                    "回读跨窗疑点端点及相邻章全文 · Pro Thinking high",
                )
                global_verify_hash = self._audit_global_verify_input_hash(
                    start, end, candidates
                )
                global_dependency_hash = self._audit_json_digest(global_result)
                global_verification = self._audit_load_checkpoint(
                    run_dir, run_state, "global_verification",
                    "global_verification.json", global_verify_hash,
                    dependency_hash=global_dependency_hash,
                )
                if global_verification is None:
                    global_verification = self._audit_verify_global_candidates(
                        start, end, candidates
                    )
                    self._audit_save_checkpoint(
                        run_dir, run_state, "global_verification",
                        "global_verification.json", global_verification,
                        global_verify_hash,
                        dependency_hash=global_dependency_hash,
                    )
                else:
                    self.log("剧情审计断点复用：全局证据复核已完成。")
            elif candidates:
                review_rows = [
                    self._audit_unresolved_global_candidate(
                        candidate,
                        "未开启 Pro 全文终审，候选仅保留供人工复核",
                        disposition="unverified",
                    )
                    for candidate in candidates
                ]
                global_verification = {
                    "status": "YELLOW",
                    "findings": [],
                    "review_findings": review_rows,
                    "false_positives": [],
                    "candidate_dispositions": [
                        {
                            "candidate_id": row.get("candidate_id"),
                            "disposition": "unverified",
                        }
                        for row in review_rows
                    ],
                    "verified_chapters": [],
                }
            global_result["verification"] = global_verification
            global_result["findings"] = (
                (global_verification or {}).get("findings") or []
            )
            global_result["review_findings"] = (
                (global_verification or {}).get("review_findings") or []
            )
            billing = self._audit_billing_snapshot()
            findings = self._audit_collect_findings(
                segments, global_result=global_result
            )
            review_findings = self._audit_collect_review_findings(
                segments, global_result=global_result,
                ready_findings=findings,
            )
            global_result = self._audit_finalize_global_result(
                global_result, findings, review_findings
            )
            source_snapshot = self._audit_assert_source_snapshot(
                run_state, start, end, ranges
            )
            chapter_hashes = source_snapshot["chapter_hashes"]
            run = {
                "schema_version": self.AUDIT_SCHEMA_VERSION,
                "run_id": run_id, "start": int(start), "end": int(end),
                "segment_size": int(segment_size), "source_check": bool(source_check),
                "created_at": datetime.now().isoformat(timespec="seconds"),
                "resumed": resumed,
                "segments": segments, "global": global_result, "billing": billing,
                "chapter_hashes": chapter_hashes,
                "snapshot_hash": hashlib.sha256(
                    json.dumps(chapter_hashes, sort_keys=True).encode("utf-8")
                ).hexdigest(),
                "repair_ready": bool(source_check),
                # Machine-readable contract for the repair planner.  Present even
                # when empty so a structured report is never treated as prose.
                "findings": findings,
                "review_only_findings": review_findings,
                "findings_count": len(findings),
                "review_only_findings_count": len(review_findings),
                "locatable_findings_count": sum(
                    1 for x in findings if str(x.get("evidence_quote") or "").strip()
                ),
            }
            json_rel = Path("reports") / "audit_runs" / run_id / "report.json"
            md_rel = Path("reports") / f"audit_{start:04d}-{end:04d}_{run_id.split('_')[0]}_{run_id.split('_')[1]}.md"
            self.write(str(json_rel), json.dumps(run, ensure_ascii=False, indent=2))
            self.write(str(md_rel), self._audit_render_markdown(run))
            run_state["status"] = "complete"
            run_state["billing"] = billing
            run_state["report_file"] = str(json_rel).replace("\\", "/")
            run_state["completed_at"] = datetime.now().isoformat(timespec="seconds")
            run_state["updated_at"] = run_state["completed_at"]
            run_state["last_error"] = ""
            atomic_write_json(run_dir / "run_state.json", run_state)
            with self.audit_lock:
                self.audit_status["report_file"] = str(md_rel).replace("\\", "/")
                self.audit_status["status"] = global_result.get("status", "")
                self.audit_status["stage"] = "完成"
                self.audit_status["stage_label"] = global_result.get("recommended_action", "审计完成")
            self.log(f"剧情审计完成：第{start}-{end}章；总体 {global_result.get('status', 'UNKNOWN')}；报告 {md_rel}")
        except ProviderCancelledError:
            run_state["status"] = "stopped"
            run_state["billing"] = self._audit_billing_snapshot()
            run_state["updated_at"] = datetime.now().isoformat(timespec="seconds")
            run_state["last_error"] = "用户停止审计"
            atomic_write_json(run_dir / "run_state.json", run_state)
            with self.audit_lock:
                self.audit_status["stage"] = "已停止"; self.audit_status["stage_label"] = "用户停止审计"
            self.log("剧情一致性审计已停止。")
        except Exception as e:
            run_state["status"] = "failed"
            run_state["billing"] = self._audit_billing_snapshot()
            run_state["updated_at"] = datetime.now().isoformat(timespec="seconds")
            run_state["last_error"] = str(e)
            atomic_write_json(run_dir / "run_state.json", run_state)
            with self.audit_lock:
                self.audit_status["last_error"] = str(e); self.audit_status["stage"] = "错误"; self.audit_status["stage_label"] = str(e)
            self.log(f"剧情一致性审计失败：{e}")
        finally:
            with self.audit_lock:
                self.audit_status["running"] = False
            self.audit_stop_event.clear()

    def start_story_audit(self, start, end, segment_size=4, source_check=True):
        start, end = int(start), int(end)
        if start < 1 or end < start:
            raise ValueError("审计章节范围无效")
        segment_size = int(segment_size or 4)
        max_window = int(getattr(self, "AUDIT_MAX_WINDOW_CHAPTERS", 12) or 12)
        if segment_size < 3 or segment_size > max_window:
            raise ValueError(f"全文窗口必须在 3-{max_window} 章之间，不能静默改写请求值")
        existing = [n for n in range(start, end + 1) if (self.root / "chapters" / f"{n:04d}.md").exists()]
        if not existing:
            raise ValueError("所选范围没有已完成 Canon 章节")
        # Avoid silently auditing a partial range that ends before the requested chapter.
        missing = [n for n in range(start, end + 1) if not (self.root / "chapters" / f"{n:04d}.md").exists()]
        if missing:
            preview = ", ".join(map(str, missing[:8])) + ("..." if len(missing) > 8 else "")
            raise ValueError(f"审计范围存在缺失章节：{preview}；请只选择连续已生成范围")
        with self.audit_lock:
            if self.audit_thread and self.audit_thread.is_alive():
                raise RuntimeError("剧情一致性审计已经在运行")
            self.audit_stop_event.clear()
            self.audit_thread = threading.Thread(
                target=self._run_story_audit,
                args=(start, end, segment_size, bool(source_check)),
                name="NovelAgentStoryAudit", daemon=True,
            )
            self.audit_thread.start()
        return self.audit_snapshot()

    def stop_story_audit(self):
        self.audit_stop_event.set()
        try:
            self.audit_router.cancel_current()
        except Exception:
            pass
        return self.audit_snapshot()

    # ---------------- audit-driven multi-chapter repair ----------------
    def _repair_check_cancel(self):
        if self.repair_stop_event.is_set():
            raise ProviderCancelledError("用户请求停止审计修复")

    def _repair_set_stage(self, stage, label=""):
        with self.repair_lock:
            self.repair_status["stage"] = stage
            self.repair_status["stage_label"] = label

    @staticmethod
    def _repair_hash(text):
        return hashlib.sha256((text or "").encode("utf-8")).hexdigest()

    # ---------------- deterministic evidence locator (zero LLM) ----------------
    # Punctuation variants that models routinely swap when quoting Chinese prose.
    # Normalizing them lets a "wrong" quote still resolve to the right span.
    _LOCATE_PUNCT_MAP = {
        "，": ",", "、": ",", "；": ";", "：": ":",
        "？": "?", "！": "!", "（": "(", "）": ")",
        "“": '"', "”": '"', "‘": "'", "’": "'",
        "—": "-", "－": "-", "～": "~", "　": " ",
        "《": "<", "》": ">", "「": '"', "」": '"',
        "…": ".", "。": ".", "·": "",
    }

    @classmethod
    def _locate_fold(cls, text):
        """Fold text for fuzzy matching while tracking original offsets.

        Returns (folded_text, index_map) where index_map[i] is the offset in the
        ORIGINAL string that folded character i came from.  The map is what makes
        it safe to translate a fuzzy match back into an exact original span.
        """
        folded = []
        index_map = []
        for i, ch in enumerate(str(text or "")):
            if ch.isspace():
                # Collapse any whitespace run to nothing: Chinese prose carries no
                # semantic spaces, and models frequently add or drop line breaks.
                continue
            mapped = cls._LOCATE_PUNCT_MAP.get(ch, ch)
            if mapped == "":
                continue
            for c in mapped:
                folded.append(c)
                index_map.append(i)
        return "".join(folded), index_map

    @classmethod
    def _locate_evidence(cls, original, quote, min_ratio=0.82):
        """Locate a quote inside chapter text using four escalating strategies.

        Strategy ladder, cheapest and most reliable first:
          exact          - quote occurs verbatim exactly once
          exact_multi    - quote occurs verbatim several times (ambiguous)
          folded         - matches after whitespace/punctuation folding
          fuzzy          - best difflib window above min_ratio

        Returns a dict with `ok`, `start`, `end`, `method`, `confidence` and
        `matched_text`.  `matched_text` is always a verbatim slice of `original`,
        so callers can use it directly as a patch anchor.
        """
        text = str(original or "")
        q = str(quote or "").strip()
        fail = {
            "ok": False, "start": -1, "end": -1,
            "method": "none", "confidence": 0.0,
            "matched_text": "", "occurrences": 0,
        }
        if not text or not q:
            return dict(fail, reason="引文或原文为空")

        # --- 1. exact ---
        first = text.find(q)
        if first >= 0:
            occurrences = text.count(q)
            if occurrences == 1:
                return {
                    "ok": True, "start": first, "end": first + len(q),
                    "method": "exact", "confidence": 1.0,
                    "matched_text": q, "occurrences": 1,
                }
            # --- 2. exact but ambiguous ---
            return {
                "ok": False, "start": first, "end": first + len(q),
                "method": "exact_multi", "confidence": 0.5,
                "matched_text": q, "occurrences": occurrences,
                "reason": f"引文在原文中出现 {occurrences} 次，无法唯一定位",
            }

        folded_text, index_map = cls._locate_fold(text)
        folded_q, _ = cls._locate_fold(q)
        if not folded_q or not folded_text:
            return dict(fail, reason="引文折叠后为空")

        def to_original_span(f_start, f_len):
            # Map a folded span back onto an exact original slice.
            if f_len <= 0 or f_start < 0 or f_start + f_len > len(index_map):
                return None
            s = index_map[f_start]
            e = index_map[f_start + f_len - 1] + 1
            if s < 0 or e > len(text) or e <= s:
                return None
            return s, e

        # --- 3. folded exact ---
        f_first = folded_text.find(folded_q)
        if f_first >= 0:
            occurrences = folded_text.count(folded_q)
            span = to_original_span(f_first, len(folded_q))
            if span and occurrences == 1:
                s, e = span
                return {
                    "ok": True, "start": s, "end": e,
                    "method": "folded", "confidence": 0.95,
                    "matched_text": text[s:e], "occurrences": 1,
                }
            if span:
                s, e = span
                return {
                    "ok": False, "start": s, "end": e,
                    "method": "folded_multi", "confidence": 0.5,
                    "matched_text": text[s:e], "occurrences": occurrences,
                    "reason": f"引文规范化后仍出现 {occurrences} 次，无法唯一定位",
                }

        # --- 4. fuzzy sliding window ---
        # Search on the folded text so whitespace noise cannot dominate the ratio.
        n = len(folded_q)
        if n < 6:
            # Too short to fuzzy-match responsibly; a false positive here would
            # point the repair at the wrong sentence.
            return dict(fail, reason="引文过短，无法安全模糊定位")

        matcher = difflib.SequenceMatcher(autojunk=False)
        matcher.set_seq2(folded_q)
        best_ratio = 0.0
        best_span = None
        # Allow the window to breathe so insertions/deletions still match.
        for window in (n, int(n * 1.25) + 4, max(6, int(n * 0.8))):
            if window > len(folded_text):
                continue
            step = max(1, window // 8)
            for start in range(0, len(folded_text) - window + 1, step):
                chunk = folded_text[start:start + window]
                matcher.set_seq1(chunk)
                # Cheap upper bounds first: skip hopeless windows without the
                # full O(n*m) comparison.
                if matcher.real_quick_ratio() < best_ratio:
                    continue
                if matcher.quick_ratio() < best_ratio:
                    continue
                ratio = matcher.ratio()
                if ratio > best_ratio:
                    best_ratio = ratio
                    best_span = (start, window)

        if best_span and best_ratio >= float(min_ratio):
            span = to_original_span(best_span[0], best_span[1])
            if span:
                s, e = span
                return {
                    "ok": True, "start": s, "end": e,
                    "method": "fuzzy", "confidence": round(best_ratio, 4),
                    "matched_text": text[s:e], "occurrences": 1,
                }

        return dict(
            fail,
            method="fuzzy_failed",
            confidence=round(best_ratio, 4),
            reason=f"最佳模糊匹配相似度 {best_ratio:.3f} 低于阈值 {min_ratio}",
        )

    @staticmethod
    def _repair_span_window(original, start, end, before=1, after=1):
        """Expand a located span to whole-paragraph boundaries plus neighbours.

        This is what replaces whole-chapter prompt injection: the model sees the
        offending paragraph in context instead of tens of thousands of characters.
        Returns (window_text, window_start, window_end).
        """
        text = str(original or "")
        if not text:
            return "", 0, 0
        start = max(0, min(int(start), len(text)))
        end = max(start, min(int(end), len(text)))

        paragraphs = []
        pos = 0
        for block in text.split("\n\n"):
            paragraphs.append((pos, pos + len(block)))
            pos += len(block) + 2

        hit_first = hit_last = None
        for i, (a, b) in enumerate(paragraphs):
            if b > start and a < max(end, start + 1):
                if hit_first is None:
                    hit_first = i
                hit_last = i
        if hit_first is None:
            hit_first = hit_last = 0

        lo = max(0, hit_first - int(before))
        hi = min(len(paragraphs) - 1, hit_last + int(after))
        w_start = paragraphs[lo][0]
        w_end = paragraphs[hi][1]
        return text[w_start:w_end], w_start, w_end

    @classmethod
    def _repair_patch_context(cls, original, anchors, before=1, after=1):
        """Build the smallest chapter excerpt that covers every repair unit.

        Sending the whole chapter costs tokens proportional to chapter length no
        matter how small the fix is, and it invites the model to wander into
        untargeted text.  When every unit was located we can send just the
        offending paragraphs plus their neighbours.

        Falls back to the full chapter if any unit is unlocated, because a unit
        with no window has no defensible excerpt and guessing one would hide the
        problem instead of fixing it.

        Returns (context_text, meta).  `meta["mode"]` is "windowed" or "full".
        """
        text = str(original or "")
        rows = [a for a in (anchors or []) if isinstance(a, dict)]

        spans = []
        for a in rows:
            try:
                start, end = int(a.get("start", -1)), int(a.get("end", -1))
            except Exception:
                start = end = -1
            if start < 0 or end <= start or end > len(text):
                spans = []
                break
            spans.append((start, end))

        if not text or not spans:
            return text, {
                "mode": "full",
                "chars": len(text),
                "full_chars": len(text),
                "window_count": 0,
                "reason": "存在未定位条目，回退整章原文" if text else "原文为空",
            }

        windows = []
        for start, end in spans:
            _, w_start, w_end = cls._repair_span_window(
                text, start, end, before=before, after=after,
            )
            windows.append((w_start, w_end))

        # Merge overlapping or adjacent windows so shared paragraphs are sent
        # once and the excerpt stays in reading order.
        windows.sort()
        merged = [list(windows[0])]
        for w_start, w_end in windows[1:]:
            if w_start <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], w_end)
            else:
                merged.append([w_start, w_end])

        parts = []
        for i, (w_start, w_end) in enumerate(merged, 1):
            if w_start > 0 and i == 1:
                parts.append("……（前文略）……")
            parts.append(f"[片段{i} 原文偏移 {w_start}-{w_end}]\n{text[w_start:w_end]}")
            if w_end < len(text):
                parts.append("……（中间略）……" if i < len(merged) else "……（后文略）……")
        context = "\n\n".join(parts)

        covered = sum(b - a for a, b in merged)
        if covered >= len(text) * 0.9:
            # Barely a saving, and the excerpt markers only add confusion.
            return text, {
                "mode": "full",
                "chars": len(text),
                "full_chars": len(text),
                "window_count": len(merged),
                "reason": "窗口已覆盖几乎整章，直接发送原文",
            }

        return context, {
            "mode": "windowed",
            "chars": len(context),
            "full_chars": len(text),
            "window_count": len(merged),
            "covered_chars": covered,
            "windows": [{"start": a, "end": b} for a, b in merged],
        }

    @staticmethod
    def _repair_change_ratio(original, candidate):
        a = re.sub(r"\s+", " ", str(original or "")).strip()
        b = re.sub(r"\s+", " ", str(candidate or "")).strip()
        if not a and not b:
            return 0.0
        return round(1.0 - difflib.SequenceMatcher(None, a, b).ratio(), 6)

    @staticmethod
    def _repair_diff(original, candidate, max_chars=12000):
        diff = "\n".join(difflib.unified_diff(
            str(original or "").splitlines(),
            str(candidate or "").splitlines(),
            fromfile="original", tofile="candidate", lineterm=""
        ))
        if len(diff) > int(max_chars):
            diff = diff[:int(max_chars)] + "\n...[diff truncated]..."
        return diff

    # Program-level acceptance thresholds per repair class.  These checks are
    # hard safety gates around the model review: a reviewer may judge meaning,
    # but it may never waive a missing unit, rejected patch or out-of-scope edit.
    REPAIR_VERIFY_LIMITS = {
        "TEXT_ONLY": {"max_ratio": 0.06, "tail_chars": 1200, "tail_must_match": True},
        "CONTINUITY_MINOR": {"max_ratio": 0.14, "tail_chars": 1200, "tail_must_match": True},
    }

    @classmethod
    def _repair_verify_patches(cls_, original, candidate, patch_meta,
                               repair_class, anchors=None):
        """Decide programmatically whether a patched candidate needs a Pro review.

        Every check here is a fact about the text, not a judgement about it:
        which units got patched, whether edits landed inside the located spans,
        how much changed, and whether the chapter tail (where Canon end state
        lives) moved.  Cases that clear all of them do not need a model to
        restate the obvious, which is where most of the per-chapter cost went.

        Anything unclear escalates rather than passing, so the cheap path can
        only ever be taken on candidates that are provably narrow.

        Returns a dict with `verdict` ("pass" or "escalate") and `reasons`.
        """
        cls = str(repair_class or "TEXT_ONLY").upper()
        limits = cls_.REPAIR_VERIFY_LIMITS.get(cls)
        text = str(original or "")
        cand = str(candidate or "")
        meta = patch_meta if isinstance(patch_meta, dict) else {}
        reasons = []
        checks = {}

        if limits is None:
            return {
                "verdict": "escalate",
                "reasons": [f"{cls} 不属于程序可判定的小修类型"],
                "checks": checks,
                "change_ratio": cls_._repair_change_ratio(text, cand),
            }

        ratio = cls_._repair_change_ratio(text, cand)

        # 1. Something must actually have changed.
        checks["changed"] = cand != text and bool(meta.get("patch_count"))
        if not checks["changed"]:
            reasons.append("候选与原文相同或没有任何补丁")

        # 2. Every repair unit must have a patch attributed to it.
        uncovered = list(meta.get("units_uncovered") or [])
        checks["units_covered"] = not uncovered
        if uncovered:
            reasons.append(f"第 {', '.join(str(u) for u in uncovered)} 条要求没有对应补丁")

        # 3. A partially applied set means some requested edits never landed, so
        #    the chapter is not finished even though the text did change.
        checks["fully_applied"] = not int(meta.get("rejected_count") or 0)
        if not checks["fully_applied"]:
            why = "；".join(
                f"第 {r.get('index')} 个补丁{r.get('reason')}"
                for r in (meta.get("rejected") or [])
            )
            reasons.append(f"有补丁未能应用：{why}")

        # 4. Unattributed patches mean we cannot tell what a patch was for.
        checks["all_attributed"] = not int(meta.get("unattributed_patches") or 0)
        if not checks["all_attributed"]:
            reasons.append(
                f"{meta.get('unattributed_patches')} 个补丁没有标明对应条目"
            )

        # 5. Change size must stay inside the class budget.
        checks["ratio_ok"] = ratio <= limits["max_ratio"]
        if not checks["ratio_ok"]:
            reasons.append(
                f"改动比例 {ratio:.4f} 超过 {cls} 程序放行上限 {limits['max_ratio']}"
            )

        # 6. The chapter tail carries the Canon end state.  If it moved, only a
        #    semantic reviewer can say whether the state survived.  The window is
        #    capped at a quarter of the chapter so a short chapter is not treated
        #    as entirely tail, which would make the cheap path unreachable there.
        tail = min(int(limits["tail_chars"]), max(200, len(text) // 4))
        if limits["tail_must_match"]:
            checks["tail_intact"] = text[-tail:] == cand[-tail:]
            if not checks["tail_intact"]:
                reasons.append("章末文本发生变化，需要复核 Canon 结束状态")

        # 7. Edits must land inside the located spans.  A patch outside every
        #    window is touching text nobody reported a problem with.
        spans = []
        for a in (anchors or []):
            if not isinstance(a, dict):
                continue
            try:
                s, e = int(a.get("start", -1)), int(a.get("end", -1))
            except Exception:
                continue
            if 0 <= s < e:
                _, w_start, w_end = cls_._repair_span_window(text, s, e)
                spans.append((w_start, w_end))

        if spans:
            outside = 0
            for p in (meta.get("patches") or []):
                old = str(p.get("old") or "")
                at = text.find(old) if old else -1
                if at < 0:
                    outside += 1
                    continue
                if not any(at >= a and at + len(old) <= b for a, b in spans):
                    outside += 1
            checks["patches_in_scope"] = not outside
            if outside:
                reasons.append(f"{outside} 个补丁落在定位范围之外")
        else:
            # No usable spans means no scope to check against, so this candidate
            # cannot earn the cheap path.
            checks["patches_in_scope"] = False
            reasons.append("没有可用的定位范围，无法程序判定补丁是否越界")

        verdict = "pass" if all(checks.values()) else "escalate"
        return {
            "verdict": verdict,
            "reasons": reasons,
            "checks": checks,
            "change_ratio": ratio,
            "limits": dict(limits),
        }

    @staticmethod
    def _repair_program_hard_safe(verify):
        """Return True only when every non-semantic patch constraint passed.

        `tail_intact` deliberately is not a hard gate: changing a sentence near
        the end of a chapter can be a legitimate requested fix, but it must be
        escalated to Pro for semantic review.  The checks below, in contrast,
        describe mechanical completeness and edit scope and therefore cannot be
        overridden by any model response.
        """
        checks = (verify or {}).get("checks") or {}
        required = (
            "changed",
            "units_covered",
            "fully_applied",
            "all_attributed",
            "ratio_ok",
            "patches_in_scope",
        )
        return all(bool(checks.get(key)) for key in required)

    # A rewrite is allowed to change prose freely, so the patch-style checks
    # (units covered, edits inside located spans) do not apply.  What still must
    # hold is that the chapter remains a chapter and that it stays connected to
    # its neighbours.  These bounds are deliberately loose: they exist to catch
    # a rewrite that collapsed, truncated or ran away, not to police style.
    REPAIR_REWRITE_LIMITS = {
        # A span rewrite touches a passage, so most of the chapter should survive.
        "REWRITE_SPAN": {
            "min_len_ratio": 0.75,
            "max_len_ratio": 1.35,
            "max_change_ratio": 0.45,
            "tail_must_match": True,
            "tail_chars": 320,
        },
        # A chapter rewrite may legitimately rewrite everything, so only the
        # collapse/runaway bounds and the Canon state check remain.
        "REWRITE_CHAPTER": {
            "min_len_ratio": 0.70,
            "max_len_ratio": 1.50,
            "max_change_ratio": 1.0,
            "tail_must_match": False,
            "tail_chars": 0,
        },
    }

    @classmethod
    def _repair_verify_rewrite(cls_, original, candidate, repair_class,
                               must_preserve=(), anchors=()):
        """Programmatically screen a rewrite candidate before it costs a review.

        This is a filter, not an acceptance: passing here means the candidate is
        structurally sane enough to be worth a semantic review, and every rewrite
        still goes to that review.  Failing here means the candidate is broken in
        a way a reviewer should not have to spend tokens explaining.

        `must_preserve` strings are checked verbatim.  A rewrite that dropped a
        phrase the audit said to keep is rejected on the spot, because that is
        the single most damaging thing a rewrite can do and it is cheap to detect.
        """
        cls = str(repair_class or "").upper()
        limits = cls_.REPAIR_REWRITE_LIMITS.get(cls)
        text = str(original or "")
        cand = str(candidate or "")
        reasons = []
        checks = {}

        if limits is None:
            return {
                "verdict": "reject",
                "reasons": [f"{cls} 不属于重写通道可处理的类型"],
                "checks": checks,
                "change_ratio": cls_._repair_change_ratio(text, cand),
            }

        ratio = cls_._repair_change_ratio(text, cand)
        len_ratio = (len(cand.strip()) / len(text.strip())) if text.strip() else 0.0

        # 1. A rewrite that returned nothing, or returned the original, did not
        #    do the job.  Both are silent failures if left unchecked.
        checks["nonempty"] = bool(cand.strip())
        if not checks["nonempty"]:
            reasons.append("重写结果为空")
        checks["changed"] = cand.strip() != text.strip()
        if not checks["changed"]:
            reasons.append("重写结果与原文完全相同")

        # 2. Length bounds catch the two ways a rewrite fails structurally:
        #    truncation (the model stopped early) and runaway expansion.
        checks["length_ok"] = limits["min_len_ratio"] <= len_ratio <= limits["max_len_ratio"]
        if not checks["length_ok"]:
            reasons.append(
                f"重写后长度为原文的 {len_ratio:.2f} 倍，"
                f"超出 {cls} 允许区间 "
                f"[{limits['min_len_ratio']}, {limits['max_len_ratio']}]"
            )

        # 3. For a span rewrite, a near-total change means the model rewrote the
        #    chapter instead of the passage it was asked about.
        checks["scope_ok"] = ratio <= limits["max_change_ratio"]
        if not checks["scope_ok"]:
            reasons.append(
                f"改动比例 {ratio:.4f} 超过 {cls} 上限 {limits['max_change_ratio']}，"
                "疑似越权重写整章"
            )

        # 4. The chapter tail carries the Canon end state that later chapters
        #    were written against.  A span rewrite must not move it.
        if limits["tail_must_match"]:
            tail = min(int(limits["tail_chars"]), max(200, len(text) // 4))
            checks["tail_intact"] = text.strip()[-tail:] == cand.strip()[-tail:]
            if not checks["tail_intact"]:
                reasons.append("段落级重写改动了章末文本，可能改变 Canon 结束状态")

        # 5. Anything the audit named as must-preserve has to survive verbatim.
        #    A None entry carries no requirement and must be skipped rather than
        #    coerced: str(None) is "None", which is absent from every candidate,
        #    so coercing it would reject the chapter on every attempt forever with
        #    no way for the rewriter to ever satisfy it.
        missing = []
        for s in must_preserve or []:
            if s is None:
                continue
            phrase = str(s).strip()
            if phrase and phrase not in cand:
                missing.append(phrase)
        checks["preserved"] = not missing
        if missing:
            reasons.append(
                "以下必须保持的内容在重写后消失："
                + "；".join(str(s)[:40] for s in missing[:5])
            )

        return {
            "verdict": "pass" if all(checks.values()) else "reject",
            "reasons": reasons,
            "checks": checks,
            "change_ratio": ratio,
            "length_ratio": len_ratio,
            "limits": dict(limits),
        }

    # Repair classes ordered from the tightest edit to the broadest.  Used to
    # decide a chapter's channel when its units disagree: the widest class wins,
    # because a chapter must be handled by one writer and the wider budget is
    # the only one that can satisfy every unit.
    _REPAIR_CLASS_ORDER = (
        "TEXT_ONLY", "CONTINUITY_MINOR", "REWRITE_SPAN", "REWRITE_CHAPTER",
    )
    _REPAIR_REWRITE_CLASSES = frozenset({"REWRITE_SPAN", "REWRITE_CHAPTER"})

    @classmethod
    def _repair_widest_class(cls_, *classes):
        """Return the broadest repair class among the arguments.

        Unknown values are ignored rather than defaulting to the narrowest, so a
        typo of a class name can never quietly shrink a chapter's budget below
        what one of its units needs.
        """
        best = -1
        for c in classes:
            name = str(c or "").upper()
            if name in cls_._REPAIR_CLASS_ORDER:
                best = max(best, cls_._REPAIR_CLASS_ORDER.index(name))
        return cls_._REPAIR_CLASS_ORDER[best] if best >= 0 else "CONTINUITY_MINOR"

    @staticmethod
    def _repair_generation_model(repair_class, attempt, preferred=""):
        """Pick the generation model for one patch attempt.

        Exact local patching is a constrained, mechanical job, so the cheap model
        is the right default even for CONTINUITY_MINOR.  Pro is held back as the
        last-attempt fallback, where the extra cost buys an actual retry rather
        than being spent on every chapter up front.
        """
        chosen = str(preferred or "").strip()
        if chosen not in {"deepseek-v4-pro", "deepseek-v4-flash"}:
            chosen = "deepseek-v4-flash"
        if chosen == "deepseek-v4-pro":
            # An explicit Pro request is honoured as-is.
            return "deepseek-v4-pro"
        return "deepseek-v4-pro" if int(attempt or 1) >= 3 else "deepseek-v4-flash"

    # Failures that are the model mis-copying a snippet, and failures that are
    # the model misunderstanding the fix, need different responses.  The first
    # kind is corrected by restating the mechanical constraint and costs a cheap
    # Flash call; the second kind is what the Pro fallback exists for.  Counting
    # them together meant three "old 在原文中找不到" slips burned the whole
    # budget and pushed a fixable chapter into manual review.
    _MECHANICAL_FAILURE_MARKERS = (
        "找不到",
        "出现多次",
        "old 为空",
        "格式不是对象",
        "没有实际变化",
        "重叠",
        "未能应用",
        "JSON 解析失败",
        "没有返回可应用",
    )

    @classmethod
    def _repair_failure_kind(cls_, error_text="", verify_reasons=()):
        """Classify one failed attempt as 'mechanical' or 'semantic'.

        Mechanical means the patch could not be applied as written: a snippet was
        not copied verbatim, was ambiguous, or the response was malformed.  The
        requested edit itself may be perfectly correct.

        Semantic means the patch applied but the result is wrong or unsafe: the
        fix is missing, unrelated text moved, or the chapter tail changed.  Only
        this kind justifies escalating to the expensive model.
        """
        blob = " ".join(
            [str(error_text or "")] + [str(r) for r in (verify_reasons or [])]
        )
        if not blob.strip():
            return "semantic"
        if any(m in blob for m in cls_._MECHANICAL_FAILURE_MARKERS):
            return "mechanical"
        return "semantic"

    # Joint review exists to catch contradictions that only appear when several
    # candidates are combined.  Sending every chapter in the batch to one Pro
    # call priced that check by batch size rather than by how entangled the
    # chapters actually are: twenty unrelated typo fixes paid for a
    # twenty-chapter cross-consistency analysis that had nothing to find.
    #
    # Chapters are entangled when they came from the same audit cluster or when
    # they sit close enough together to share scene continuity.  Everything else
    # is independent and can be judged on its own.
    _REVIEW_ADJACENCY = 2

    @classmethod
    def _repair_review_clusters(cls_, packets, adjacency=None):
        """Partition candidates into joint-review clusters and direct passes.

        Returns `(clusters, direct_pass)`.  `clusters` is a list of chapter-number
        lists that must be reviewed together; `direct_pass` lists chapters that
        can keep their independent verdict with no joint call at all.

        Only isolated TEXT_ONLY chapters are eligible to skip.  A CONTINUITY_MINOR
        fix can move Canon state even when it looks lonely, so it always goes
        through joint review however far it sits from anything else.
        """
        adj = cls_._REVIEW_ADJACENCY if adjacency is None else int(adjacency)

        rows = []
        for p in packets or []:
            if not isinstance(p, dict):
                continue
            try:
                n = int(p.get("chapter_no") or 0)
            except Exception:
                continue
            if n <= 0:
                continue
            rows.append({
                "chapter_no": n,
                "cluster_id": str(p.get("cluster_id") or "").strip(),
                "group_id": str(p.get("group_id") or "").strip(),
                "repair_class": str(p.get("repair_class") or "").strip().upper(),
            })
        if not rows:
            return [], []

        # Deduplicate by chapter: one chapter is one candidate file.
        by_chapter = {}
        for r in rows:
            prev = by_chapter.get(r["chapter_no"])
            if prev is None:
                by_chapter[r["chapter_no"]] = r
                continue
            # Keep the stronger class and any non-empty cluster id.
            if prev["repair_class"] == "TEXT_ONLY" and r["repair_class"]:
                prev["repair_class"] = r["repair_class"]
            prev["cluster_id"] = prev["cluster_id"] or r["cluster_id"]
            prev["group_id"] = prev["group_id"] or r["group_id"]

        chapters = sorted(by_chapter)
        parent = {n: n for n in chapters}

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a, b):
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[max(ra, rb)] = min(ra, rb)

        # Shared audit cluster, or shared plan group, means shared cause.
        for key in ("cluster_id", "group_id"):
            buckets = {}
            for n in chapters:
                v = by_chapter[n][key]
                if v:
                    buckets.setdefault(v, []).append(n)
            for members in buckets.values():
                for other in members[1:]:
                    union(members[0], other)

        # Neighbouring chapters share scene continuity.
        for a, b in zip(chapters, chapters[1:]):
            if b - a <= adj:
                union(a, b)

        groups = {}
        for n in chapters:
            groups.setdefault(find(n), []).append(n)

        clusters, direct_pass = [], []
        for members in groups.values():
            members = sorted(members)
            if len(members) == 1 and by_chapter[members[0]]["repair_class"] == "TEXT_ONLY":
                direct_pass.append(members[0])
            else:
                clusters.append(members)
        clusters.sort(key=lambda m: m[0])
        return clusters, sorted(direct_pass)

    # A cluster can legitimately be large: one foreshadowing thread may touch
    # dozens of chapters.  Handing all of them to a single call pushes the input
    # past the size where the model reasons reliably, and a joint review that
    # quietly stops being careful is worse than one that costs a little more.
    _REVIEW_CHUNK_CHAPTERS = 6
    _REVIEW_CHUNK_CHARS = 40000

    @classmethod
    def _repair_review_chunks(cls_, members, sizes=None,
                              max_chapters=None, max_chars=None):
        """Split one cluster into review-sized chunks that still overlap.

        Chunks carry the previous chunk's last chapter forward, so every pair of
        neighbouring chapters is compared inside at least one call.  Without the
        overlap, splitting a cluster would hide exactly the seam most likely to
        contain a contradiction.
        """
        cap_n = int(max_chapters or cls_._REVIEW_CHUNK_CHAPTERS)
        cap_c = int(max_chars or cls_._REVIEW_CHUNK_CHARS)
        cap_n = max(2, cap_n)
        sizes = sizes or {}

        ordered = [int(n) for n in (members or [])]
        if len(ordered) <= 1:
            return [list(ordered)] if ordered else []

        chunks, current, current_chars = [], [], 0
        for n in ordered:
            size = int(sizes.get(n, 0) or 0)
            too_many = len(current) >= cap_n
            too_big = current and (current_chars + size) > cap_c
            if current and (too_many or too_big):
                chunks.append(current)
                # Overlap by one chapter so the seam is still reviewed.
                current = [current[-1]]
                current_chars = int(sizes.get(current[0], 0) or 0)
            current.append(n)
            current_chars += size
        if current:
            chunks.append(current)
        return [c for c in chunks if len(c) >= 1]

    @staticmethod
    def _repair_joint_resolve(members, safe_chapters, part):
        """Turn one joint-review response into per-chapter verdicts.

        Blocking every chapter the model failed to mention made a single sloppy
        response throw away work on chapters it had no complaint about, which is
        how clean batches ended up entirely in manual review.

        The response is read as an explicit conflict list.  An omitted chapter
        keeps its independent verdict only when the response contains no finding;
        once the reviewer reports a real finding, silence is treated as unresolved
        and blocked. This prevents an unreviewed chapter from being committed
        while still allowing an entirely clean response to preserve independent
        verdicts.

        Returns `(approved, blocked, unresolved)` where `unresolved` records the
        chapters that had no explicit verdict, so the batch report can show them.
        """
        member_set = {int(n) for n in (members or [])}
        safe_set = {int(n) for n in (safe_chapters or [])}
        part = part if isinstance(part, dict) else {}

        def named(key):
            out = []
            for x in part.get(key) or []:
                try:
                    k = int(x)
                except Exception:
                    continue
                # A batch may only rule on its own chapters.
                if k in member_set and k not in out:
                    out.append(k)
            return out

        explicit_approved = named("approved_chapters")
        explicit_blocked = named("blocked_chapters")

        def meaningful(value):
            if isinstance(value, (list, tuple, set)):
                return any(
                    (isinstance(item, str) and item.strip())
                    or (not isinstance(item, str) and bool(item))
                    for item in value
                )
            if isinstance(value, str):
                return bool(value.strip())
            return bool(value)

        has_joint_findings = bool(
            explicit_blocked
            or meaningful(part.get("cross_chapter_findings"))
            or meaningful(part.get("findings"))
        )

        approved, blocked, unresolved = [], [], []
        for n in sorted(member_set):
            if n in explicit_blocked:
                blocked.append(n)
                continue
            if n in explicit_approved:
                # The joint review can only confirm an independent pass, never
                # override one.
                (approved if n in safe_set else blocked).append(n)
                continue
            unresolved.append(n)
            if n not in safe_set or has_joint_findings:
                blocked.append(n)
            else:
                approved.append(n)
        return approved, blocked, unresolved

    @staticmethod
    def _repair_findings_for_chapter(joint, chapter_no):
        """Select the joint-review findings that actually concern one chapter.

        Feeding a chapter every finding in the batch was actively harmful: the
        retry prompt arrived carrying complaints about chapters this one has
        nothing to do with, and the model would either try to "fix" an unrelated
        problem inside the wrong chapter or lose the real instruction in the
        noise.

        Findings are recorded per review chunk, so a chapter only receives the
        findings from chunks it was part of.  Older `joint_review.json` files have
        no `review_clusters`, and for those the flat list is all there is.
        """
        joint = joint if isinstance(joint, dict) else {}
        try:
            n = int(chapter_no)
        except Exception:
            return []

        clusters = joint.get("review_clusters")
        if not isinstance(clusters, list) or not clusters:
            return [
                str(x).strip() for x in (joint.get("cross_chapter_findings") or [])
                if x is not None and str(x).strip()
            ]

        out = []
        for row in clusters:
            if not isinstance(row, dict):
                continue
            members = set()
            for x in row.get("chapters") or []:
                try:
                    members.add(int(x))
                except Exception:
                    continue
            if n not in members:
                continue
            for x in row.get("cross_chapter_findings") or []:
                if x is None:
                    continue
                text = str(x).strip()
                if text and text not in out:
                    out.append(text)
        return out

    @staticmethod
    def _repair_rereview_scope(joint, changed_chapters):
        """Find which chapters must be re-reviewed after some were regenerated.

        Re-running the whole batch made every single-chapter retry cost as much
        as the original review, which is why the round limit had to be kept low.
        Only the clusters that contain a changed chapter can have their verdict
        invalidated, so only those need another call.

        Returns the full set of chapters in the affected clusters, including the
        unchanged ones: their approval was granted in the context of a candidate
        that has since been replaced, so it has to be re-earned.
        """
        joint = joint if isinstance(joint, dict) else {}
        changed = set()
        for x in changed_chapters or []:
            try:
                changed.add(int(x))
            except Exception:
                continue
        if not changed:
            return set()

        clusters = joint.get("review_clusters")
        if not isinstance(clusters, list) or not clusters:
            # No cluster record to narrow with; the caller must review everything.
            return None

        scope = set(changed)
        for row in clusters:
            if not isinstance(row, dict):
                continue
            members = set()
            for key in ("cluster_chapters", "chapters"):
                for x in row.get(key) or []:
                    try:
                        members.add(int(x))
                    except Exception:
                        continue
            if members & changed:
                scope |= members
        return scope

    def _repair_chat_json(self, stage, system, user, model="deepseek-v4-pro",
                          effort="high", max_tokens=6000):
        self._repair_check_cancel()
        text, _ = self.repair_router.chat(
            stage, system, user, temperature=0.1, max_tokens=max_tokens,
            stream=False, label=stage, emit_text=False, routing_context="",
            provider_override="deepseek", model_override=model,
            thinking_override=True, response_format={"type": "json_object"},
            reasoning_effort_override=effort, allow_local_fallback=False,
        )
        obj = _json_obj(text)
        if not isinstance(obj, dict):
            raw_name = f"logs/audit_repair_{stage}_raw_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
            self.write(raw_name, text)
            raise RuntimeError(f"审计修复 {stage} JSON 解析失败，原始输出已保存：{raw_name}")
        return obj

    def _latest_audit_source(self):
        # Prefer structured report.json because it contains segment-level evidence.
        run_root = self.root / "reports" / "audit_runs"
        candidates = []
        if run_root.exists():
            for p in run_root.glob("*/report.json"):
                try:
                    candidates.append((p.stat().st_mtime, p))
                except Exception:
                    pass
        if candidates:
            path = sorted(candidates, reverse=True)[0][1]
            return path, path.read_text(encoding="utf-8")
        reports = []
        rep_root = self.root / "reports"
        if rep_root.exists():
            for p in rep_root.glob("audit_*.md"):
                try:
                    reports.append((p.stat().st_mtime, p))
                except Exception:
                    pass
        if reports:
            path = sorted(reports, reverse=True)[0][1]
            return path, path.read_text(encoding="utf-8")
        raise FileNotFoundError("没有找到剧情一致性审计报告")

    def _audit_source_findings(self, source_text):
        """Extract the structured findings contract from an audit source, if present.

        Returns (schema_version, findings).  A v1 / prose / pasted report yields
        (1, []) and the caller falls back to LLM extraction.  This is a pure
        parse: no model call, no cost.
        """
        text = str(source_text or "").strip()
        if not text.startswith("{"):
            return 1, []
        try:
            obj = json.loads(text)
        except Exception:
            return 1, []
        if not isinstance(obj, dict):
            return 1, []

        try:
            version = int(obj.get("schema_version") or 0)
        except Exception:
            version = 0

        rows = obj.get("findings")
        review_rows = obj.get("review_only_findings")
        findings = [
            dict(x) for x in (rows if isinstance(rows, list) else [])
            if isinstance(x, dict)
        ]
        if version >= 3:
            ready_keys = {
                (
                    str(row.get("chapter_no") or "").strip(),
                    str(row.get("category") or "").strip(),
                    str(row.get("evidence_quote") or "").strip(),
                    str(row.get("issue") or "").strip(),
                    str(row.get("required_fix") or "").strip(),
                )
                for row in findings
            }
            for raw in review_rows if isinstance(review_rows, list) else []:
                if not isinstance(raw, dict):
                    continue
                row = dict(raw)
                content_key = (
                    str(row.get("chapter_no") or "").strip(),
                    str(row.get("category") or "").strip(),
                    str(row.get("evidence_quote") or "").strip(),
                    str(row.get("issue") or "").strip(),
                    str(row.get("required_fix") or "").strip(),
                )
                # Overlapping windows can emit the same issue once as ready and
                # once as review-only.  The stronger, fully evidenced result
                # wins; a weaker duplicate must not block its commit permission.
                if content_key in ready_keys:
                    continue
                row["repair_ready"] = False
                row["gate_reasons"] = list(row.get("gate_reasons") or []) + [
                    "审计证据门未通过，仅供人工复核"
                ]
                findings.append(row)
        if findings:
            # A report carrying findings is v2-capable even if the field is absent,
            # which keeps hand-edited reports usable.
            return (version or 2), findings

        # v2 report with zero findings: audit found nothing locatable to fix.
        if version >= 2:
            return version, []

        # Legacy report.json: findings were never emitted.
        return 1, []

    def _validate_audit_source_snapshot(self, source_text, accepted_hashes=None):
        """Reject a v3 repair report after any audited Canon chapter changed."""
        text = str(source_text or "").strip()
        if not text.startswith("{"):
            return
        try:
            obj = json.loads(text)
            version = int(obj.get("schema_version") or 0)
        except Exception:
            return
        if version < 3:
            return
        hashes = obj.get("chapter_hashes")
        if not isinstance(hashes, dict) or not hashes:
            raise RuntimeError("新版审计报告缺少 Canon 快照哈希，不能用于自动修复")

        hashed_chapters = set()
        for raw_n in hashes:
            try:
                hashed_chapters.add(int(raw_n))
            except Exception:
                continue
        required_chapters = set()
        try:
            audit_start = int(obj.get("start") or 0)
            audit_end = int(obj.get("end") or 0)
        except Exception:
            audit_start = audit_end = 0
        if audit_start > 0 and audit_end >= audit_start:
            required_chapters.update(range(audit_start, audit_end + 1))
        for row in obj.get("findings") or []:
            if not isinstance(row, dict):
                continue
            for value in [
                row.get("chapter_no"),
                *(row.get("related_chapters") or []),
                *[
                    evidence.get("chapter_no")
                    for evidence in (row.get("evidence_quotes") or [])
                    if isinstance(evidence, dict)
                ],
            ]:
                try:
                    chapter = int(value or 0)
                except Exception:
                    continue
                if chapter > 0:
                    required_chapters.add(chapter)
        missing_hashes = sorted(required_chapters - hashed_chapters)
        if missing_hashes:
            preview = "、".join(str(x) for x in missing_hashes[:8])
            if len(missing_hashes) > 8:
                preview += "等"
            raise RuntimeError(
                f"新版审计报告的 Canon 快照不完整：缺少第{preview}章哈希"
            )

        accepted = {}
        for raw_n, values in (accepted_hashes or {}).items():
            try:
                n = int(raw_n)
            except (TypeError, ValueError):
                continue
            if isinstance(values, (list, tuple, set)):
                pool = values
            else:
                pool = [values]
            accepted[n] = {str(value or "") for value in pool if str(value or "")}

        stale = []
        for raw_n, expected in hashes.items():
            try:
                n = int(raw_n)
            except Exception:
                stale.append(str(raw_n))
                continue
            p = self.root / "chapters" / f"{n:04d}.md"
            actual = hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else ""
            if actual != str(expected or "") and actual not in accepted.get(n, set()):
                stale.append(str(n))
        if stale:
            preview = "、".join(stale[:8]) + ("等" if len(stale) > 8 else "")
            raise RuntimeError(
                f"审计报告已过期：第{preview}章在审计后发生变化；请重新运行审计"
            )

    def _validate_repair_batch_audit_snapshot(self, batch_id, plan=None):
        """Recheck the audit snapshot whenever a saved repair batch is resumed."""
        batch_dir = self._repair_batch_dir(batch_id)
        source_path = batch_dir / "audit_source.txt"
        if plan is None:
            plan_path = batch_dir / "plan.json"
            plan = (
                json.loads(plan_path.read_text(encoding="utf-8"))
                if plan_path.exists() else {}
            )
        try:
            version = int((plan or {}).get("schema_version") or 0)
        except Exception:
            version = 0
        if not source_path.exists():
            if version >= 3:
                raise RuntimeError("新版修复计划缺少原始审计快照，不能继续自动修复")
            return
        accepted_hashes = {}
        manifest_path = batch_dir / "commit_manifest.json"
        if manifest_path.exists():
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                for row in self._repair_manifest_active_rows(manifest):
                    n = int(row.get("chapter_no") or 0)
                    digest = str(row.get("new_sha256") or "").strip()
                    if n > 0 and digest:
                        accepted_hashes.setdefault(n, set()).add(digest)
            except Exception as exc:
                raise RuntimeError(f"修复提交清单无法读取，不能校验批次快照：{exc}") from exc
        self._validate_audit_source_snapshot(
            source_path.read_text(encoding="utf-8"),
            accepted_hashes=accepted_hashes,
        )

    def _repair_batch_dir(self, batch_id):
        return self.root / "reports" / "audit_fixes" / str(batch_id)

    def _latest_repair_batch_id(self):
        current = str(self.repair_snapshot().get("batch_id", "") or "").strip()
        if current and self._repair_batch_dir(current).exists():
            return current
        root = self.root / "reports" / "audit_fixes"
        if not root.exists():
            return ""
        rows = []
        for p in root.iterdir():
            if p.is_dir():
                try:
                    rows.append((p.stat().st_mtime, p.name))
                except Exception:
                    pass
        return sorted(rows, reverse=True)[0][1] if rows else ""

    def _load_repair_plan(self, batch_id=""):
        bid = str(batch_id or self._latest_repair_batch_id()).strip()
        if not bid:
            raise FileNotFoundError("尚无审计修复批次")
        path = self._repair_batch_dir(bid) / "plan.json"
        if not path.exists():
            raise FileNotFoundError("修复计划不存在")
        return bid, json.loads(path.read_text(encoding="utf-8"))

    # Task states used by the repair pipeline.  `skipped` is reserved for items
    # the audit itself said to defer, never for items we merely failed to handle.
    REPAIR_TASK_STATES = {
        "planned", "located", "unlocated", "patched",
        "rewritten", "approved", "blocked", "committed", "skipped",
    }

    def _build_repair_tasks_from_findings(self, findings):
        """Turn structured audit findings into located repair tasks. Zero LLM.

        Each finding is anchored into its chapter by the deterministic locator.
        The located span is what allows the patch stage to send a small window
        instead of the whole chapter, and it is also the natural repair unit:
        two findings in one chapter stay two independent tasks.
        """
        tasks = []
        chapter_cache = {}
        stats = {
            "total": 0, "located": 0, "unlocated": 0,
            "no_quote": 0, "deferred": 0, "missing_chapter": 0,
            "evidence_blocked": 0,
            "by_method": {}, "by_class": {},
        }

        for raw in findings or []:
            if not isinstance(raw, dict):
                continue
            stats["total"] += 1
            try:
                n = int(raw.get("chapter_no") or 0)
            except Exception:
                n = 0

            cls = str(raw.get("suggested_class") or "").strip().upper()
            if cls not in self.AUDIT_FIX_CLASSES:
                # No usable hint: let the locator decide how precise we can be,
                # then default to the most conservative editing mode.
                cls = ""

            instruction = str(raw.get("required_fix") or "").strip()
            issue = str(raw.get("issue") or "").strip()
            if not instruction:
                instruction = issue

            related_chapters = [
                int(x) for x in (raw.get("related_chapters") or [])
                if str(x).strip().lstrip("-").isdigit() and int(x) > 0
            ][:12]
            evidence_quotes = []
            for evidence in raw.get("evidence_quotes") or []:
                if not isinstance(evidence, dict):
                    continue
                try:
                    evidence_chapter = int(evidence.get("chapter_no") or 0)
                except Exception:
                    continue
                evidence_quote = str(
                    evidence.get("quote") or evidence.get("evidence_quote") or ""
                ).strip()
                if evidence_chapter > 0 and evidence_quote:
                    evidence_quotes.append({
                        "chapter_no": evidence_chapter,
                        "quote": evidence_quote,
                    })

            confidence = str(raw.get("confidence") or "").strip().lower()
            category = str(raw.get("category") or "").strip().upper()
            target_quote = str(raw.get("evidence_quote") or "").strip()
            evidence_contract_present = (
                "evidence_quotes" in raw or "repair_ready" in raw
            )
            evidence_gate_reasons = []
            if evidence_contract_present:
                validated = []
                for evidence in evidence_quotes:
                    evidence_chapter = int(evidence["chapter_no"])
                    if evidence_chapter not in chapter_cache:
                        p = self.root / "chapters" / f"{evidence_chapter:04d}.md"
                        chapter_cache[evidence_chapter] = (
                            p.read_text(encoding="utf-8") if p.exists() else None
                        )
                    text = chapter_cache.get(evidence_chapter)
                    if text is not None and evidence["quote"] in text:
                        validated.append(evidence)
                target_covered = any(
                    int(x["chapter_no"]) == n and x["quote"] == target_quote
                    for x in validated
                )
                if related_chapters:
                    comparison_covered = any(
                        int(x["chapter_no"]) in related_chapters for x in validated
                    )
                else:
                    comparison_covered = any(
                        (int(x["chapter_no"]), x["quote"]) != (n, target_quote)
                        for x in validated
                    )
                if confidence != "high":
                    evidence_gate_reasons.append("置信度不是 high")
                if category not in self.AUDIT_HARD_CATEGORIES:
                    evidence_gate_reasons.append("不是硬连续性类别")
                if not target_covered:
                    evidence_gate_reasons.append("缺少目标章逐字证据")
                target_text = chapter_cache.get(n)
                if target_text is None:
                    target_path = self.root / "chapters" / f"{n:04d}.md"
                    target_text = (
                        target_path.read_text(encoding="utf-8")
                        if target_path.exists() else None
                    )
                    chapter_cache[n] = target_text
                if not target_quote or not target_text or target_text.count(target_quote) != 1:
                    evidence_gate_reasons.append("目标章问题引文不能唯一定位")
                if not comparison_covered:
                    evidence_gate_reasons.append("缺少关联章或同章对照逐字证据")
                if not instruction:
                    evidence_gate_reasons.append("缺少明确的最小修正目标")
                if cls not in self.AUDIT_FIX_CLASSES or cls == "DEFER_FUTURE":
                    evidence_gate_reasons.append("缺少可执行的小修分类")
                if raw.get("repair_ready") is False:
                    evidence_gate_reasons.extend(
                        str(x) for x in (raw.get("gate_reasons") or ["审计未准入自动修复"])
                    )

            base = {
                "task_id": str(raw.get("finding_id") or "").strip() or f"T{stats['total']:04d}",
                "finding_id": str(raw.get("finding_id") or "").strip(),
                "chapter_no": n,
                "related_chapters": related_chapters,
                "issue": issue,
                "instruction": instruction,
                "must_preserve": [
                    str(x).strip() for x in (raw.get("must_preserve") or []) if str(x).strip()
                ][:16],
                "evidence_quote": target_quote,
                "evidence_quotes": evidence_quotes[:16],
                "audit_confidence": confidence,
                # Candidate files are reversible previews. Evidence gates control
                # commit eligibility, not whether the user may inspect a proposal.
                "auto_candidate_allowed": True,
                "auto_commit_allowed": not evidence_gate_reasons,
                "evidence_gate_reasons": list(dict.fromkeys(evidence_gate_reasons)),
                "repair_class": cls or "CONTINUITY_MINOR",
                "suggested_class": cls,
                "state": "planned",
                "locate": {},
                "attempts": 0,
                "llm_attempts": 0,
                "history": [],
            }
            if evidence_gate_reasons:
                stats["evidence_blocked"] += 1

            # DEFER_FUTURE is the one class the audit can legitimately mark as
            # "do not touch old text"; honour it without spending anything.
            if cls == "DEFER_FUTURE":
                base["auto_candidate_allowed"] = False
                base["auto_commit_allowed"] = False
                base["repair_class"] = "DEFER_FUTURE"
                base["state"] = "skipped"
                base["skip_reason"] = "审计判定应在后续章节自然补足，无需回改旧正文"
                stats["deferred"] += 1
                stats["by_class"]["DEFER_FUTURE"] = stats["by_class"].get("DEFER_FUTURE", 0) + 1
                tasks.append(base)
                continue

            if n < 1:
                base["auto_candidate_allowed"] = False
                base["auto_commit_allowed"] = False
                base["state"] = "unlocated"
                base["locate"] = {"ok": False, "reason": "缺少有效章节号"}
                stats["missing_chapter"] += 1
                tasks.append(base)
                continue

            if n not in chapter_cache:
                p = self.root / "chapters" / f"{n:04d}.md"
                chapter_cache[n] = p.read_text(encoding="utf-8") if p.exists() else None
            original = chapter_cache[n]
            if original is None:
                base["auto_candidate_allowed"] = False
                base["auto_commit_allowed"] = False
                base["state"] = "unlocated"
                base["locate"] = {"ok": False, "reason": f"第{n}章正文不存在"}
                stats["missing_chapter"] += 1
                tasks.append(base)
                continue

            quote = base["evidence_quote"].strip()
            if not quote:
                # The audit explicitly could not point at a sentence.  This is a
                # chapter-level problem, not a failure; route it to rewrite.
                base["state"] = "unlocated"
                base["locate"] = {"ok": False, "reason": "审计未提供正文引文"}
                if base["repair_class"] in {"TEXT_ONLY", "CONTINUITY_MINOR"}:
                    base["repair_class"] = "REWRITE_SPAN"
                stats["no_quote"] += 1
                tasks.append(base)
                continue

            loc = self._locate_evidence(original, quote)
            base["locate"] = {
                k: loc.get(k) for k in
                ("ok", "start", "end", "method", "confidence", "occurrences", "reason")
            }
            method = str(loc.get("method") or "none")
            stats["by_method"][method] = stats["by_method"].get(method, 0) + 1

            if loc.get("ok"):
                base["state"] = "located"
                # matched_text is a verbatim slice, so it is a valid patch anchor
                # even when the audit quote had punctuation drift.
                base["anchor_text"] = loc.get("matched_text") or ""
                window, w_start, w_end = self._repair_span_window(
                    original, loc["start"], loc["end"], before=1, after=1
                )
                base["window"] = {
                    "start": w_start, "end": w_end,
                    "chars": len(window),
                    "chapter_chars": len(original),
                }
                stats["located"] += 1
            else:
                base["state"] = "unlocated"
                # Could not pin the sentence: a targeted patch would be a guess,
                # so widen to span rewrite rather than skipping the issue.
                if base["repair_class"] in {"TEXT_ONLY", "CONTINUITY_MINOR"}:
                    base["repair_class"] = "REWRITE_SPAN"
                stats["unlocated"] += 1

            stats["by_class"][base["repair_class"]] = stats["by_class"].get(base["repair_class"], 0) + 1
            tasks.append(base)

        # Stable ordering: by chapter, then by task id, so reruns are comparable.
        tasks.sort(key=lambda x: (int(x.get("chapter_no") or 0), str(x.get("task_id") or "")))
        for idx, t in enumerate(tasks, 1):
            t["seq"] = idx
        return tasks, stats

    @staticmethod
    def _repair_group_tasks(tasks):
        """Group tasks that can interact, so joint review stays small.

        Two tasks belong together when they touch the same chapter, when one
        lists the other's chapter as related, or when their chapters are
        adjacent. Everything else is independent and needs no joint review.
        """
        rows = [
            t for t in (tasks or [])
            if t.get("state") not in {"skipped"} and int(t.get("chapter_no") or 0) > 0
        ]
        if not rows:
            return []

        parent = {}

        def find(x):
            parent.setdefault(x, x)
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a, b):
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[rb] = ra

        by_chapter = {}
        for t in rows:
            n = int(t["chapter_no"])
            find(n)
            by_chapter.setdefault(n, []).append(t)

        chapters = sorted(by_chapter)
        for t in rows:
            n = int(t["chapter_no"])
            for r in t.get("related_chapters") or []:
                if int(r) in by_chapter:
                    union(n, int(r))
        # Adjacent chapters can share a continuity seam.
        for a, b in zip(chapters, chapters[1:]):
            if b - a <= 1:
                union(a, b)

        clusters = {}
        for n in chapters:
            clusters.setdefault(find(n), []).append(n)

        out = []
        for idx, (_root, members) in enumerate(sorted(clusters.items()), 1):
            members = sorted(members)
            member_tasks = []
            for n in members:
                member_tasks.extend(by_chapter[n])
            out.append({
                "cluster_id": f"C{idx:03d}",
                "chapters": members,
                "task_ids": [t["task_id"] for t in member_tasks],
                # A single chapter with only text-level fixes cannot create a
                # cross-chapter conflict, so it can skip joint review entirely.
                "needs_joint_review": (
                    len(members) > 1
                    or any(
                        str(t.get("repair_class") or "").upper() != "TEXT_ONLY"
                        for t in member_tasks
                    )
                ),
            })
        return out

    @staticmethod
    def _repair_render_units(anchors):
        """Render repair units as an explicitly numbered, independent list.

        The patch model needs to see that these are separate obligations, not
        one compound instruction.  Numbering also lets the reviewer and the
        self-heal feedback refer to a single failing unit by index instead of
        re-litigating the whole chapter.
        """
        rows = [a for a in (anchors or []) if str(a.get("instruction") or "").strip()]
        if not rows:
            return ""
        if len(rows) == 1:
            return str(rows[0]["instruction"]).strip()
        out = []
        for idx, a in enumerate(rows, 1):
            cls = str(a.get("repair_class") or "").upper()
            tag = f"[{cls}]" if cls else ""
            out.append(f"{idx}. {tag}{str(a['instruction']).strip()}")
        return "\n".join(out)

    def _repair_plan_from_tasks(self, tasks, clusters):
        """Fold located tasks into the plan structures each channel consumes.

        Two execution destinations plus one diagnostic view.  Every task lands
        in exactly one execution destination (`items` or `deferred`); rewrite
        tasks are additionally mirrored in `rewrite_queue` for observability:

          items         - runnable patch and rewrite work.  The candidate runner
                          indexes by chapter, so every task for one chapter is
                          folded into one item while its individual anchor is
                          preserved under `anchors`.  The widest class selects
                          the only writer allowed to touch that chapter.
          rewrite_queue - diagnostic rows for work too broad to patch.  The
                          runnable copy remains in `items`; this list alone is
                          never treated as executable.
          deferred      - only DEFER_FUTURE, which the audit itself said to fix
                          in later chapters rather than by editing old text.
        """
        cluster_of = {}
        for c in clusters or []:
            for tid in c.get("task_ids") or []:
                cluster_of[tid] = c["cluster_id"]

        items = []
        rewrite_queue = []
        deferred = []
        by_chapter = {}

        for t in tasks or []:
            cls = str(t.get("repair_class") or "").upper()
            cluster_id = cluster_of.get(t.get("task_id"), "")

            if t.get("state") == "skipped" or cls == "DEFER_FUTURE":
                deferred.append({
                    "task_id": t.get("task_id"),
                    "chapter_no": int(t.get("chapter_no") or 0),
                    "issue": t.get("issue"),
                    "instruction": t.get("instruction"),
                    "skip_reason": t.get("skip_reason") or "",
                })
                continue

            n = int(t.get("chapter_no") or 0)
            if cls in self._REPAIR_REWRITE_CLASSES:
                # The queue is diagnostic metadata.  The runnable copy is folded
                # into `items` below; queue-only entries are never executable.
                rewrite_queue.append({
                    "task_id": t.get("task_id"),
                    "cluster_id": cluster_id,
                    "chapter_no": int(t.get("chapter_no") or 0),
                    "related_chapters": t.get("related_chapters") or [],
                    "repair_class": cls,
                    "issue": t.get("issue"),
                    "instruction": t.get("instruction"),
                    "must_preserve": t.get("must_preserve") or [],
                    "evidence_quote": t.get("evidence_quote") or "",
                    "evidence_quotes": t.get("evidence_quotes") or [],
                    "evidence_gate_reasons": t.get("evidence_gate_reasons") or [],
                    "anchor_text": t.get("anchor_text") or "",
                    "window": t.get("window") or {},
                    "locate": t.get("locate") or {},
                    "state": t.get("state"),
                    "reason": (
                        (t.get("locate") or {}).get("reason")
                        or "同章包含结构性问题，转入重写通道"
                    ),
                })

            # Each anchor is a self-contained repair unit.  It carries its own
            # class and constraints so a TEXT_ONLY typo fix keeps tight budgets
            # even when the same chapter also holds a CONTINUITY_MINOR unit.
            anchor = {
                "task_id": t.get("task_id"),
                "issue": t.get("issue") or "",
                "instruction": t.get("instruction"),
                "repair_class": cls or "CONTINUITY_MINOR",
                "must_preserve": list(t.get("must_preserve") or [])[:16],
                "anchor_text": t.get("anchor_text") or "",
                "start": (t.get("locate") or {}).get("start", -1),
                "end": (t.get("locate") or {}).get("end", -1),
                "method": (t.get("locate") or {}).get("method", ""),
                "confidence": (t.get("locate") or {}).get("confidence", 0.0),
                "window": t.get("window") or {},
                "evidence_gate_reasons": list(t.get("evidence_gate_reasons") or []),
            }

            if n in by_chapter:
                item = items[by_chapter[n]]
                if t.get("instruction") and t["instruction"] not in item["instruction"]:
                    item["instruction"] = (
                        item["instruction"] + "\n- " + t["instruction"]
                    ).strip()
                item["must_preserve"] = list(dict.fromkeys(
                    item["must_preserve"] + (t.get("must_preserve") or [])
                ))[:16]
                item["related_chapters"] = list(dict.fromkeys(
                    item["related_chapters"] + (t.get("related_chapters") or [])
                ))[:12]
                item["task_ids"].append(t.get("task_id"))
                item["anchors"].append(anchor)
                item["auto_candidate"] = bool(
                    item.get("auto_candidate") and t.get("auto_candidate_allowed", True)
                )
                item["auto_commit_allowed"] = bool(
                    item.get("auto_commit_allowed") and t.get("auto_commit_allowed", True)
                )
                item["evidence_gate_reasons"] = list(dict.fromkeys(
                    (item.get("evidence_gate_reasons") or [])
                    + (t.get("evidence_gate_reasons") or [])
                ))
                # The chapter-level class is the loosest budget any unit in the
                # chapter needs.  Individual units keep their own class on the
                # anchor, so a typo fix is still held to typo-sized limits.
                item["repair_class"] = self._repair_widest_class(
                    item["repair_class"], cls,
                )
                item["instruction"] = self._repair_render_units(item["anchors"])
                continue

            by_chapter[n] = len(items)
            items.append({
                "issue_id": "",
                "group_id": cluster_id,
                "cluster_id": cluster_id,
                "chapter_no": n,
                "related_chapters": list(t.get("related_chapters") or [])[:12],
                "repair_class": cls or "CONTINUITY_MINOR",
                "instruction": t.get("instruction") or "",
                "must_preserve": list(t.get("must_preserve") or [])[:16],
                "reason": t.get("issue") or "",
                "task_ids": [t.get("task_id")],
                "anchors": [anchor],
                "auto_candidate": bool(t.get("auto_candidate_allowed", True)),
                "auto_commit_allowed": bool(t.get("auto_commit_allowed", True)),
                "evidence_gate_reasons": list(t.get("evidence_gate_reasons") or []),
                "status": "planned",
            })

        items.sort(key=lambda x: int(x.get("chapter_no") or 0))
        for idx, item in enumerate(items, 1):
            item["issue_id"] = f"F{idx:03d}"
            if not item["group_id"]:
                item["group_id"] = f"G{idx:03d}"
            # One chapter has exactly one writer.  Any structural unit widens the
            # chapter to the rewrite channel, while small-fix units remain as
            # explicit requirements for that rewriter.
            is_complex = (
                str(item.get("repair_class") or "").upper()
                in self._REPAIR_REWRITE_CLASSES
            )
            if is_complex:
                item["channel"] = "rewrite"
            else:
                item["channel"] = "patch"
        rewrite_queue.sort(key=lambda x: (int(x.get("chapter_no") or 0), str(x.get("task_id") or "")))
        return items, rewrite_queue, deferred

    # Sections whose entire content is "nothing to fix here".  Historical prose
    # reports are mostly made of these, and paying Pro to read them was a large
    # share of the planning bill.
    _REPAIR_NOISE_PATTERNS = (
        r"总体(良好|一致|无(明显)?问题|正常|可控)",
        r"^无(明显)?(问题|异常|冲突|矛盾)",
        r"未(发现|见)(明显)?(问题|异常|冲突|矛盾|不一致)",
        r"(建议)?继续观察",
        r"(暂|均)?无需(修改|处理|回改|调整)",
        r"不影响(阅读|理解|连贯性|一致性)",
        r"(保持|维持)现状(即可)?",
        r"^(本段|该段|此段|本区间|该区间)(整体)?(表现)?(良好|稳定|正常)",
    )

    @classmethod
    def _repair_filter_noise(cls, text):
        """Drop report sections that state no fix is needed.  Zero LLM.

        Splits on Markdown headings and blank-line paragraphs, keeps anything
        that is not confidently noise, and returns (filtered_text, stats).
        The bias is deliberately toward keeping: a dropped real problem is far
        more expensive than a few extra characters of prompt.
        """
        src = str(text or "")
        if not src.strip():
            return "", {"kept_chars": 0, "dropped_chars": 0, "dropped_blocks": 0}

        noise = [re.compile(p, re.M) for p in cls._REPAIR_NOISE_PATTERNS]
        # Keep headings attached to their body so context is not orphaned.
        blocks = re.split(r"\n\s*\n", src)

        kept = []
        dropped_blocks = 0
        dropped_chars = 0
        for block in blocks:
            body = block.strip()
            if not body:
                continue
            # Strip heading markers and list bullets before judging content.
            probe = re.sub(r"^\s*(#{1,6}\s*|[-*+]\s+|\d+[.、)]\s*)", "", body, flags=re.M)
            probe_compact = re.sub(r"\s+", "", probe)
            if not probe_compact:
                continue

            hits = sum(1 for p in noise if p.search(probe))
            if not hits:
                kept.append(block)
                continue

            # A noise phrase inside a long block usually sits next to a real
            # finding, so only short blocks are dropped outright.
            if len(probe_compact) <= 120:
                dropped_blocks += 1
                dropped_chars += len(block)
                continue
            kept.append(block)

        filtered = "\n\n".join(kept).strip()
        if not filtered:
            # Everything looked like noise.  Fall back to the original text
            # rather than reporting an empty report.
            return src, {
                "kept_chars": len(src),
                "dropped_chars": 0,
                "dropped_blocks": 0,
                "fallback": "全部内容被判为非修复项，已保留原文",
            }
        return filtered, {
            "kept_chars": len(filtered),
            "dropped_chars": dropped_chars,
            "dropped_blocks": dropped_blocks,
        }

    @staticmethod
    def _split_audit_repair_source(text, target_chars=30000, overlap_chars=1800):
        """Split a long audit report without silently truncating it.

        Prefer paragraph / heading boundaries and retain a small overlap so a
        cross-boundary issue is still visible to at least one extractor.
        """
        text = str(text or "").strip()
        if not text:
            return []
        target_chars = max(12000, int(target_chars or 30000))
        overlap_chars = max(0, min(int(overlap_chars or 0), target_chars // 5))
        if len(text) <= target_chars + 5000:
            return [text]

        chunks = []
        n = len(text)
        start = 0
        while start < n:
            ideal_end = min(n, start + target_chars)
            if ideal_end >= n:
                end = n
            else:
                low = min(ideal_end, start + max(8000, int(target_chars * 0.68)))
                # Prefer a major heading, then paragraph, then line boundary.
                candidates = []
                for marker in ("\n## ", "\n### ", "\n#### ", "\n\n", "\n"):
                    pos = text.rfind(marker, low, ideal_end + 1)
                    if pos > start:
                        candidates.append(pos)
                end = max(candidates) if candidates else ideal_end
                if end <= start:
                    end = ideal_end

            chunk = text[start:end].strip()
            if chunk:
                chunks.append(chunk)
            if end >= n:
                break

            next_start = max(0, end - overlap_chars)
            # Avoid an infinite loop if a very short boundary was selected.
            if next_start <= start:
                next_start = end
            start = next_start

        return chunks

    # Legacy extraction classes.  MANUAL_ONLY used to absorb two unrelated
    # situations; it is now resolved into a real channel wherever possible.
    LEGACY_REPAIR_CLASSES = {
        "TEXT_ONLY", "CONTINUITY_MINOR", "NEEDS_EVIDENCE",
        "REWRITE_SPAN", "REWRITE_CHAPTER", "MANUAL_ONLY", "DEFER_FUTURE",
    }

    def _repair_resolve_weak_class(self, chapter_no, quote, original):
        """Resolve an under-specified legacy row into a concrete channel.

        The old pipeline collapsed "the audit gave no usable evidence" and
        "this is genuinely too big for a small fix" into MANUAL_ONLY, then
        skipped both.  The first case is recoverable: if the row carries a
        quote we can anchor it ourselves and patch it.  Only the second case
        should widen, and it widens into the rewrite channel rather than being
        dropped, because the audit did find a real problem either way.
        """
        quote = str(quote or "").strip()
        if not quote:
            return "REWRITE_SPAN", {
                "resolution": "no_evidence_to_rewrite",
                "reason": "提取阶段未给出正文引文，无法程序定位，转入重写通道",
                "locate": {"ok": False, "reason": "缺少引文"},
            }

        loc = self._locate_evidence(original, quote)
        if loc.get("ok"):
            # Evidence recovered programmatically: this is a normal small fix.
            return "CONTINUITY_MINOR", {
                "resolution": "evidence_recovered",
                "reason": (
                    f"程序定位成功（{loc.get('method')}），"
                    f"由 MANUAL_ONLY 提升为可自动小修"
                ),
                "locate": {
                    k: loc.get(k) for k in
                    ("ok", "start", "end", "method", "confidence", "occurrences")
                },
                "anchor_text": loc.get("matched_text") or "",
            }

        return "REWRITE_SPAN", {
            "resolution": "unlocatable_to_rewrite",
            "reason": (
                "引文无法在正文中定位（"
                f"{loc.get('reason') or '无匹配'}），转入重写通道"
            ),
            "locate": {"ok": False, "reason": loc.get("reason") or "无匹配"},
        }

    def _normalize_audit_repair_items(self, rows, merge_same_chapter=True):
        """Validate rows and group same-chapter fixes without flattening them.

        Same-chapter rows still share one item, because a chapter file can only
        be written once per batch and that item is the chapter lock.  But each
        row survives as its own entry in `anchors`, carrying its own class and
        constraints.  Concatenating them into a single `instruction` was what
        made one unfixable requirement drag every other fix in the chapter down
        with it.

        Weakly-classified rows are resolved here rather than skipped, so
        MANUAL_ONLY is left meaning only "we cannot even open this chapter".
        """
        cleaned = []
        seen = {}

        for idx, raw in enumerate(rows or [], 1):
            if not isinstance(raw, dict):
                continue
            try:
                n = int(raw.get("chapter_no") or 0)
            except Exception:
                n = 0

            cls = str(raw.get("repair_class") or "NEEDS_EVIDENCE").upper()
            if cls not in self.LEGACY_REPAIR_CLASSES:
                # An unrecognised label is missing information, not a verdict
                # that the problem is unfixable.
                cls = "NEEDS_EVIDENCE"

            resolution = {}
            original = None
            # A historical-body edit must point at a real chapter.  Future-only
            # items may legitimately have chapter_no=0.
            if cls != "DEFER_FUTURE":
                p = self.root / "chapters" / f"{n:04d}.md" if n >= 1 else None
                if p is None or not p.exists():
                    # Nothing to edit and nothing to rewrite: this is the only
                    # remaining reason to genuinely hand an item back.
                    cls = "MANUAL_ONLY"
                    resolution = {
                        "resolution": "chapter_missing",
                        "reason": f"第{n}章正文不存在，无法自动修复",
                    }
                else:
                    original = p.read_text(encoding="utf-8")

            if cls in {"MANUAL_ONLY", "NEEDS_EVIDENCE"} and original is not None:
                cls, resolution = self._repair_resolve_weak_class(
                    n, raw.get("evidence_quote"), original,
                )

            related = []
            for x in raw.get("related_chapters") or []:
                try:
                    x = int(x)
                except Exception:
                    continue
                if x > 0 and x not in related:
                    related.append(x)

            auto_candidate = bool(raw.get(
                "auto_candidate",
                cls in {"TEXT_ONLY", "CONTINUITY_MINOR"},
            ))
            auto_commit = bool(raw.get(
                "auto_commit_allowed",
                cls in {"TEXT_ONLY", "CONTINUITY_MINOR"},
            ))
            if cls not in {"TEXT_ONLY", "CONTINUITY_MINOR"}:
                auto_candidate = False
                auto_commit = False

            source_chunk = 0
            try:
                source_chunk = int(raw.get("_source_chunk") or 0)
            except Exception:
                source_chunk = 0
            raw_issue = str(raw.get("issue_id") or f"F{idx:03d}").strip()
            raw_group = str(raw.get("group_id") or raw_issue or f"G{idx:03d}").strip()
            if source_chunk:
                raw_issue = f"C{source_chunk:03d}_{raw_issue}"
                raw_group = f"C{source_chunk:03d}_{raw_group}"

            instruction = str(raw.get("instruction") or "").strip()
            must_preserve = [
                str(x).strip()
                for x in (raw.get("must_preserve") or [])
                if str(x).strip()
            ][:16]

            # Legacy rows usually have no verbatim quote, so there is normally
            # no located span.  When evidence recovery succeeded above, the
            # anchor carries the real offsets and the patch stage can use them.
            loc = resolution.get("locate") or {}
            anchor = {
                "task_id": raw_issue,
                "issue": str(raw.get("reason") or "").strip(),
                "instruction": instruction,
                "repair_class": cls,
                "must_preserve": must_preserve,
                "anchor_text": resolution.get("anchor_text") or "",
                "start": loc.get("start", -1),
                "end": loc.get("end", -1),
                "method": loc.get("method") or "",
                "confidence": loc.get("confidence") or 0.0,
                "window": {},
            }

            item = {
                "issue_id": raw_issue,
                "group_id": raw_group,
                "chapter_no": n,
                "related_chapters": related[:12],
                "repair_class": cls,
                "instruction": instruction,
                "must_preserve": must_preserve,
                "reason": str(raw.get("reason") or "").strip(),
                "task_ids": [raw_issue],
                "anchors": [anchor],
                "auto_candidate": auto_candidate,
                "auto_commit_allowed": auto_commit,
                "status": "planned",
            }
            if resolution:
                item["resolution"] = resolution.get("resolution") or ""
                item["resolution_reason"] = resolution.get("reason") or ""

            # The report chunks overlap, so the same chapter shows up repeatedly.
            # Those rows share one chapter lock but stay separate repair units.
            if (
                merge_same_chapter
                and n > 0
                and cls in {"TEXT_ONLY", "CONTINUITY_MINOR"}
                and n in seen
            ):
                old = cleaned[seen[n]]
                if instruction and not any(
                    a["instruction"] == instruction for a in old["anchors"]
                ):
                    old["anchors"].append(anchor)
                    old["task_ids"].append(raw_issue)
                old["must_preserve"] = list(dict.fromkeys(
                    old["must_preserve"] + must_preserve
                ))[:16]
                old["related_chapters"] = list(dict.fromkeys(
                    old["related_chapters"] + item["related_chapters"]
                ))[:12]
                # The chapter-level class is the loosest budget any unit needs.
                if cls == "CONTINUITY_MINOR":
                    old["repair_class"] = "CONTINUITY_MINOR"
                old["instruction"] = self._repair_render_units(old["anchors"])
                continue

            if n > 0 and cls in {"TEXT_ONLY", "CONTINUITY_MINOR"}:
                seen[n] = len(cleaned)
            cleaned.append(item)

        return cleaned

    @staticmethod
    def _audit_repair_review_batches(items, max_chars=90000):
        """Create bounded global-review batches without dropping any item."""
        rows = list(items or [])
        if not rows:
            return []
        batches = []
        current = []
        current_chars = 0
        limit = max(30000, int(max_chars or 90000))
        for item in rows:
            size = len(json.dumps(item, ensure_ascii=False))
            if current and current_chars + size > limit:
                batches.append(current)
                current = []
                current_chars = 0
            current.append(item)
            current_chars += size
        if current:
            batches.append(current)
        return batches

    def _create_audit_repair_plan_from_findings(
        self, findings, schema_version, source_text, source_label, model,
    ):
        """Build a repair plan straight from structured audit findings. Zero LLM.

        The audit body-verification pass already read the chapters and quoted the
        offending sentences, so planning is pure bookkeeping: locate each quote,
        cluster what can interact, and route each task to patch, rewrite or defer.
        """
        effective_findings = []
        for raw in findings or []:
            if not isinstance(raw, dict):
                continue
            row = dict(raw)
            # v2 reports predate the two-sided evidence contract. Keep their
            # findings visible in the plan, but never run them automatically.
            if int(schema_version or 0) < 3:
                row["repair_ready"] = False
                row["gate_reasons"] = list(row.get("gate_reasons") or []) + [
                    "旧版审计报告没有 Canon 快照保护，请重新运行剧情一致性审计"
                ]
            elif "evidence_quotes" not in row or "repair_ready" not in row:
                row["repair_ready"] = False
                row["gate_reasons"] = list(row.get("gate_reasons") or []) + [
                    "v3 条目缺少完整双边证据契约"
                ]
            if source_label == "pasted_report":
                row["repair_ready"] = False
                row["gate_reasons"] = list(row.get("gate_reasons") or []) + [
                    "粘贴的外部报告可生成候选，但不能批量提交"
                ]
            effective_findings.append(row)

        batch_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        batch_dir = self._repair_batch_dir(batch_id)
        batch_dir.mkdir(parents=True, exist_ok=True)

        with self.repair_lock:
            self.repair_status.update({
                "batch_id": batch_id,
                "stage": "规划定位",
                "stage_label": f"程序直读审计条目并定位：{len(effective_findings)} 条",
                "last_error": "",
                "model": "local",
                "item_index": 0,
                "item_total": len(effective_findings),
                "candidate_ready": 0,
                "candidate_blocked": 0,
                "joint_safe": None,
                "committed": False,
                "rolled_back": False,
                "last_commit_mode": "",
                "last_commit_forced": False,
                "last_commit_manual": False,
                "last_commit_failed_gates": [],
                "last_commit_chapters": [],
                "last_commit_force_reason": "",
                "prompt_tokens": 0,
                "cache_hit_tokens": 0,
                "completion_tokens": 0,
                "reasoning_tokens": 0,
                "cost_cny": 0.0,
                "afp": 0.0,
                "request_count": 0,
            })

        self._repair_check_cancel()
        tasks, stats = self._build_repair_tasks_from_findings(effective_findings)
        clusters = self._repair_group_tasks(tasks)
        items, rewrite_queue, deferred = self._repair_plan_from_tasks(tasks, clusters)

        (batch_dir / "tasks.json").write_text(
            json.dumps({
                "batch_id": batch_id,
                "stats": stats,
                "clusters": clusters,
                "tasks": tasks,
            }, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        joint_clusters = sum(1 for c in clusters if c.get("needs_joint_review"))
        plan_summary = (
            f"程序直读审计结构化条目 {stats['total']} 条，零 LLM 规划开销："
            f"精确定位 {stats['located']} 条，"
            f"转入重写通道 {len(rewrite_queue)} 条，"
            f"证据门阻止批量提交 {stats.get('evidence_blocked', 0)} 条，"
            f"按审计要求延后 {len(deferred)} 条；"
            f"折叠为 {len(items)} 个章节修复任务，"
            f"{len(clusters)} 个聚类中 {joint_clusters} 个需要联合复核。"
        )

        plan = {
            "batch_id": batch_id,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "source_audit": source_label,
            "schema_version": schema_version,
            "source_chars": len(source_text),
            "planner_mode": "structured_findings_local",
            "planner_llm_calls": 0,
            "model": model,
            "summary": plan_summary,
            "locate_stats": stats,
            "clusters": clusters,
            "items": items,
            "rewrite_queue": rewrite_queue,
            "deferred": deferred,
        }

        (batch_dir / "plan.json").write_text(
            json.dumps(plan, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (batch_dir / "audit_source.txt").write_text(source_text, encoding="utf-8")

        auto_total = sum(1 for x in items if x.get("auto_candidate"))
        patch_total = sum(1 for x in items if x.get("channel") == "patch")
        rewrite_total = sum(1 for x in items if x.get("channel") == "rewrite")
        with self.repair_lock:
            self.repair_status["stage"] = "计划完成"
            self.repair_status["stage_label"] = (
                f"零 LLM 规划：定位 {stats['located']}/{stats['total']}；"
                f"补丁 {patch_total} 章，定向重写 {rewrite_total} 章，"
                f"证据不足 {stats.get('evidence_blocked', 0)} 项仅候选预览，"
                f"延后 {len(deferred)} 项"
            )
            self.repair_status["item_index"] = 0
            self.repair_status["item_total"] = auto_total

        self.log(
            f"审计修复计划已生成（零 LLM 规划）：{batch_id}；"
            f"条目 {stats['total']}，定位成功 {stats['located']}，"
            f"未定位 {stats['unlocated']}，缺引文 {stats['no_quote']}，"
            f"证据门阻止提交 {stats.get('evidence_blocked', 0)}，"
            f"缺章节 {stats['missing_chapter']}，延后 {stats['deferred']}；"
            f"补丁 {patch_total} 章，定向重写 {rewrite_total} 章，"
            f"重写诊断 {len(rewrite_queue)} 项。"
        )
        return {
            "ok": True,
            "batch_id": batch_id,
            "plan": plan,
            "status": self.repair_snapshot(),
        }

    def _create_audit_repair_plan_sync(self, audit_text="", model="deepseek-v4-pro"):
        if self.audit_snapshot().get("running"):
            raise RuntimeError("剧情审计运行中不能生成修复计划")
        if self.status.get("running"):
            raise RuntimeError("Canon 运行中不能生成修复计划")

        model = str(model or "deepseek-v4-pro")
        if model not in {"deepseek-v4-pro", "deepseek-v4-flash"}:
            model = "deepseek-v4-pro"

        source_path = None
        source_text = str(audit_text or "").strip()
        if not source_text:
            source_path, source_text = self._latest_audit_source()
        if not source_text:
            raise RuntimeError("审计报告为空")

        source_label = (
            str(source_path.relative_to(self.root)).replace("\\", "/")
            if source_path else "pasted_report"
        )

        # Fast path: a schema v2 report already carries verbatim evidence quotes,
        # so the plan can be built by reading JSON and matching text locally.
        # This is where the bulk of the old AFP bill disappears: no chunked Pro
        # extraction, no Pro global review, no LLM in the planning stage at all.
        self._validate_audit_source_snapshot(source_text)
        schema_version, findings = self._audit_source_findings(source_text)
        # A schema-v2 report with an empty findings list is a clean modern audit,
        # not a legacy prose report.  Sending that JSON back through the legacy
        # extractor wastes AFP and can invent work from human-readable summaries.
        if schema_version >= self.AUDIT_REPAIR_FINDINGS_MIN_VERSION:
            return self._create_audit_repair_plan_from_findings(
                findings=findings,
                schema_version=schema_version,
                source_text=source_text,
                source_label=source_label,
                model=model,
            )

        # Legacy prose-only report: fall back to LLM extraction.
        # Strip "nothing to fix" sections locally first so the model is never
        # billed for reading them.
        filtered_text, noise_stats = self._repair_filter_noise(source_text)
        # Extraction is text restructuring, not reasoning.  Flash is sufficient,
        # and using Pro here was the single largest avoidable planning cost.
        extract_model = "deepseek-v4-flash"

        # No hard 180k-character truncation.  Long reports are processed in
        # bounded overlapping chunks, then merged and globally reviewed.
        chunks = self._split_audit_repair_source(filtered_text)
        if not chunks:
            raise RuntimeError("审计报告无法分块")

        batch_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        batch_dir = self._repair_batch_dir(batch_id)
        batch_dir.mkdir(parents=True, exist_ok=True)
        chunk_dir = batch_dir / "plan_chunks"
        chunk_dir.mkdir(parents=True, exist_ok=True)

        with self.repair_lock:
            self.repair_status.update({
                "batch_id": batch_id,
                "stage": "规划提取",
                "stage_label": (
                    f"历史报告降噪后分块提取：0/{len(chunks)}"
                    f"（已剔除 {noise_stats['dropped_blocks']} 个非修复段落）"
                ),
                "last_error": "",
                "model": extract_model,
                "item_index": 0,
                "item_total": len(chunks),
                "candidate_ready": 0,
                "candidate_blocked": 0,
                "joint_safe": None,
                "committed": False,
                "rolled_back": False,
                "last_commit_mode": "",
                "last_commit_forced": False,
                "last_commit_manual": False,
                "last_commit_failed_gates": [],
                "last_commit_chapters": [],
                "last_commit_force_reason": "",
                "prompt_tokens": 0,
                "cache_hit_tokens": 0,
                "completion_tokens": 0,
                "reasoning_tokens": 0,
                "cost_cny": 0.0,
                "afp": 0.0,
                "request_count": 0,
            })

        extract_system = """你是超长篇小说的审计修复任务提取器。
输入只是完整审计报告中的一个分块；相邻分块可能有少量重叠。

你的职责：
- 只提取本分块中“审计报告明确支持”的修复问题。
- 不重新审计，不补充报告没有提出的新问题。
- 不因为分块边界而猜测缺失信息。
- 同一问题可能在相邻分块重复出现，这是允许的，后续会统一去重。
- 只要报告指出了真实问题，就必须输出，不要因为“不好改”而丢弃；分类只表示规模。
- 尽可能逐字照抄报告中引用的正文片段到 evidence_quote，后续会靠它程序定位。
- TEXT_ONLY：日期、时间词、称谓、笔误、局部措辞等，不改变 Canon 状态。
- CONTINUITY_MINOR：少量补句/短段、跨章衔接、小型遗漏闭环，但必须保持目标章结束 Canon 状态不变。
- REWRITE_SPAN / REWRITE_CHAPTER：超出小修边界（核心事件结果、人物关系结果、正式知识边界、能力等级、重要物品/地点状态等），按需要重写的范围选择。
- NEEDS_EVIDENCE：报告信息不足以判断规模，交由后续程序定位补齐。
- DEFER_FUTURE：报告明确说应在未来章节自然补足，不必回改旧正文。
必须输出严格 JSON。"""

        extracted_rows = []
        chunk_summaries = []
        for i, chunk in enumerate(chunks, 1):
            self._repair_check_cancel()
            self._repair_set_stage(
                "规划提取",
                f"长审计分块提取：{i}/{len(chunks)}",
            )
            with self.repair_lock:
                self.repair_status["item_index"] = i - 1
                self.repair_status["item_total"] = len(chunks)

            extract_user = f"""【审计报告分块 {i}/{len(chunks)}】
说明：这是原始审计报告的一部分，不是独立审计。只提取有明确证据的问题。

{chunk}

输出：
{{
  "summary": "本分块涉及的修复问题概述；没有问题则说明没有明确修复项",
  "items": [
    {{
      "issue_id": "F001",
      "group_id": "G001",
      "chapter_no": 123,
      "related_chapters": [122,124],
      "repair_class": "TEXT_ONLY/CONTINUITY_MINOR/REWRITE_SPAN/REWRITE_CHAPTER/NEEDS_EVIDENCE/DEFER_FUTURE",
      "evidence_quote": "审计报告里引用的正文原文片段，逐字照抄，没有就留空",
      "instruction": "只描述需要怎样修，不写新剧情",
      "must_preserve": ["必须保持不变的既有事实"],
      "reason": "审计报告为什么要求修",
      "auto_candidate": true,
      "auto_commit_allowed": true
    }}
  ]
}}

规则：
- chapter_no 必须是明确需要修改正文的章节号。
- evidence_quote 只能逐字照抄报告中出现的正文引文，绝不改写或编造；报告没有引文就留空。
- TEXT_ONLY / CONTINUITY_MINOR / REWRITE_SPAN / REWRITE_CHAPTER 都允许
  auto_candidate=true；程序会按分类选择精确补丁或定向重写通道。
- 分类只描述问题规模，不要用它表示“放弃”：
  - 改字词、称谓、数字、日期 → TEXT_ONLY
  - 补一两句衔接、补小遗漏 → CONTINUITY_MINOR
  - 需要重写一整段或整场 → REWRITE_SPAN
  - 需要重写整章 → REWRITE_CHAPTER
  - 报告信息不足以判断规模 → NEEDS_EVIDENCE
  - 只需在后续章节自然补足 → DEFER_FUTURE
- 不要输出“总体良好”“继续观察”等非修复项。
"""
            obj = self._repair_chat_json(
                f"repair_plan_extract_{batch_id}_{i:03d}",
                extract_system,
                extract_user,
                model=extract_model,
                effort="low",
                max_tokens=6000,
            )
            (chunk_dir / f"chunk_{i:03d}.json").write_text(
                json.dumps(obj, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            summary = str(obj.get("summary") or "").strip()
            if summary:
                chunk_summaries.append({
                    "chunk": i,
                    "summary": summary,
                })
            rows = obj.get("items") if isinstance(obj.get("items"), list) else []
            for raw in rows:
                if isinstance(raw, dict):
                    raw = dict(raw)
                    raw["_source_chunk"] = i
                    extracted_rows.append(raw)

            with self.repair_lock:
                self.repair_status["item_index"] = i

        # First local pass removes overlap duplicates and enforces safety rules.
        preliminary = self._normalize_audit_repair_items(
            extracted_rows,
            merge_same_chapter=True,
        )
        (batch_dir / "plan_preliminary.json").write_text(
            json.dumps({
                "source_chars": len(source_text),
                "filtered_chars": len(filtered_text),
                "noise_stats": noise_stats,
                "extract_model": extract_model,
                "chunk_count": len(chunks),
                "chunk_summaries": chunk_summaries,
                "items": preliminary,
            }, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        review_system = """你是超长篇小说审计修复计划的全局复核器。
前一步已经把完整审计报告分块提取成结构化修复项。你现在只做全局合并与安全复核，不重新审计正文。

必须做到：
1. 删除由分块重叠造成的重复项。
2. 同一章节的多个小修保留为各自独立的条目，不要合并成一条笼统要求；后续会自动共享同一章的写入锁。
3. 同一跨章节问题应使用相同 group_id，并保留 related_chapters。
4. 不得创造提取结果没有支持的新问题。
5. 不要丢弃任何真实问题。存在状态改变风险或范围过大时改为 REWRITE_SPAN / REWRITE_CHAPTER，交给重写通道，而不是跳过。
6. 证据不足、目标不明确时标 NEEDS_EVIDENCE，后续由程序定位补齐。
7. 原样保留每条的 evidence_quote，不要改写或删除。
8. DEFER_FUTURE 只保留“未来自然补足，无需回改旧正文”的事项。
9. TEXT_ONLY/CONTINUITY_MINOR 必须保持目标章结束时长期 Canon 状态不变。
10. 最终计划应该适合逐章生成最小 diff、独立验收、跨章联合复核。
必须输出严格 JSON。"""

        review_batches = self._audit_repair_review_batches(preliminary, max_chars=90000)
        reviewed_rows = []
        review_summaries = []

        if not review_batches:
            reviewed_rows = []
        else:
            for bi, batch_items in enumerate(review_batches, 1):
                self._repair_check_cancel()
                self._repair_set_stage(
                    "全局复核",
                    f"Pro 全局合并复核：{bi}/{len(review_batches)}",
                )
                with self.repair_lock:
                    self.repair_status["item_index"] = bi - 1
                    self.repair_status["item_total"] = len(review_batches)

                review_user = f"""【来源统计】
原始审计字符数：{len(source_text)}
降噪后字符数：{len(filtered_text)}
原始分块数：{len(chunks)}
本次复核批次：{bi}/{len(review_batches)}

【分块提取后的修复项】
{json.dumps(batch_items, ensure_ascii=False)}

输出：
{{
  "summary": "本批次全局复核结果",
  "items": [
    {{
      "issue_id": "F001",
      "group_id": "G001",
      "chapter_no": 123,
      "related_chapters": [122,124],
      "repair_class": "TEXT_ONLY/CONTINUITY_MINOR/REWRITE_SPAN/REWRITE_CHAPTER/NEEDS_EVIDENCE/DEFER_FUTURE",
      "evidence_quote": "原样保留输入中的正文引文",
      "instruction": "最小修复要求",
      "must_preserve": ["必须保持的事实"],
      "reason": "保留/合并/改类该项的依据",
      "auto_candidate": true,
      "auto_commit_allowed": true
    }}
  ]
}}

不要新增输入列表之外的问题；允许删除重复项、调整分类；但不允许丢弃真实问题。
"""
                reviewed = self._repair_chat_json(
                    f"repair_plan_global_{batch_id}_{bi:03d}",
                    review_system,
                    review_user,
                    model=model,
                    effort="high",
                    max_tokens=8000,
                )
                (chunk_dir / f"global_review_{bi:03d}.json").write_text(
                    json.dumps(reviewed, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                rs = reviewed.get("items") if isinstance(reviewed.get("items"), list) else []
                reviewed_rows.extend(x for x in rs if isinstance(x, dict))
                summary = str(reviewed.get("summary") or "").strip()
                if summary:
                    review_summaries.append(summary)
                with self.repair_lock:
                    self.repair_status["item_index"] = bi

        # Second local pass also catches duplicate chapters that landed in
        # different global-review batches.
        cleaned = self._normalize_audit_repair_items(
            reviewed_rows if review_batches else preliminary,
            merge_same_chapter=True,
        )

        # Assign stable final IDs.  Preserve model-created group links, but
        # remove chunk-local prefixes from the public issue IDs.
        group_map = {}
        next_group = 1
        for idx, item in enumerate(cleaned, 1):
            old_group = str(item.get("group_id") or "").strip()
            if old_group not in group_map:
                group_map[old_group] = f"G{next_group:03d}"
                next_group += 1
            item["issue_id"] = f"F{idx:03d}"
            item["group_id"] = group_map[old_group]

        # Route into the same three channels the structured path uses, so a
        # legacy report cannot quietly park a real problem in `items` where
        # nothing will ever pick it up.
        run_items, rewrite_queue, deferred = [], [], []
        run_by_chapter = {}
        for item in cleaned:
            cls = str(item.get("repair_class") or "").upper()
            if cls == "DEFER_FUTURE":
                deferred.append({
                    "task_id": item.get("issue_id"),
                    "chapter_no": item.get("chapter_no"),
                    "issue": item.get("reason") or "",
                    "skip_reason": "审计判定应在后续章节自然补足，无需回改旧正文",
                })
                continue
            if cls == "MANUAL_ONLY":
                # Only survives when the chapter file itself is unavailable.
                deferred.append({
                    "task_id": item.get("issue_id"),
                    "chapter_no": item.get("chapter_no"),
                    "issue": item.get("reason") or "",
                    "skip_reason": item.get("resolution_reason")
                                   or "目标章节正文不存在，无法自动修复",
                })
                continue

            if cls in self._REPAIR_REWRITE_CLASSES:
                rewrite_queue.append({
                    "task_id": item.get("issue_id"),
                    "chapter_no": item.get("chapter_no"),
                    "repair_class": cls,
                    "issue": item.get("reason") or "",
                    "instruction": item.get("instruction") or "",
                    "must_preserve": item.get("must_preserve") or [],
                    "related_chapters": item.get("related_chapters") or [],
                    "reason": item.get("resolution_reason")
                              or "超出小修边界，转入重写通道",
                })

            # A candidate is only a reversible preview, so old reports may still
            # generate one. They have no complete snapshot/evidence contract and
            # therefore can never gain batch commit eligibility.
            legacy_gate = "旧版/纯文本审计报告可生成候选，但禁止批量提交；提交资格需要最新版 v3 report.json"
            item["auto_candidate"] = True
            item["auto_commit_allowed"] = False
            item["evidence_gate_reasons"] = list(dict.fromkeys(
                (item.get("evidence_gate_reasons") or []) + [legacy_gate]
            ))
            item["status"] = "planned"
            n = int(item.get("chapter_no") or 0)
            if n in run_by_chapter:
                target = run_items[run_by_chapter[n]]
                known_units = {
                    (str(a.get("repair_class") or ""), str(a.get("instruction") or ""))
                    for a in target.get("anchors") or []
                }
                for anchor in item.get("anchors") or []:
                    key = (
                        str(anchor.get("repair_class") or ""),
                        str(anchor.get("instruction") or ""),
                    )
                    if key not in known_units:
                        target.setdefault("anchors", []).append(anchor)
                        known_units.add(key)
                target["task_ids"] = list(dict.fromkeys(
                    list(target.get("task_ids") or [])
                    + list(item.get("task_ids") or [])
                ))
                target["must_preserve"] = list(dict.fromkeys(
                    list(target.get("must_preserve") or [])
                    + list(item.get("must_preserve") or [])
                ))[:16]
                target["related_chapters"] = list(dict.fromkeys(
                    list(target.get("related_chapters") or [])
                    + list(item.get("related_chapters") or [])
                ))[:12]
                target["repair_class"] = self._repair_widest_class(
                    target.get("repair_class"), cls,
                )
                target["instruction"] = self._repair_render_units(
                    target.get("anchors") or [],
                )
                continue

            run_by_chapter[n] = len(run_items)
            run_items.append(item)

        run_items.sort(key=lambda x: int(x.get("chapter_no") or 0))
        for item in run_items:
            item["channel"] = (
                "rewrite"
                if str(item.get("repair_class") or "").upper()
                in self._REPAIR_REWRITE_CLASSES
                else "patch"
            )

        plan_summary = "；".join(x for x in review_summaries if x).strip()
        if not plan_summary:
            plan_summary = (
                f"历史格式报告：本地降噪剔除 {noise_stats['dropped_blocks']} 个非修复段落后，"
                f"按 {len(chunks)} 个分块用 Flash 提取并经 Pro 全局复核，"
                f"形成 {len(cleaned)} 项最小修复任务。"
            )

        plan = {
            "batch_id": batch_id,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "source_audit": source_label,
            "schema_version": schema_version,
            "source_chars": len(source_text),
            "filtered_chars": len(filtered_text),
            "noise_stats": noise_stats,
            "planner_mode": "chunked_extract_global_review",
            "extract_model": extract_model,
            "chunk_count": len(chunks),
            "review_batch_count": len(review_batches),
            "model": model,
            "summary": plan_summary,
            "items": run_items,
            "rewrite_queue": rewrite_queue,
            "deferred": deferred,
        }

        (batch_dir / "plan.json").write_text(
            json.dumps(plan, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        # Save the COMPLETE report, never a prefix.
        (batch_dir / "audit_source.txt").write_text(
            source_text,
            encoding="utf-8",
        )

        auto_total = sum(1 for x in run_items if x["auto_candidate"])
        patch_total = sum(1 for x in run_items if x["channel"] == "patch")
        rewrite_total = sum(1 for x in run_items if x["channel"] == "rewrite")
        with self.repair_lock:
            self.repair_status["stage"] = "计划完成"
            self.repair_status["stage_label"] = (
                f"历史报告 {len(source_text):,} 字符 → 降噪 {len(filtered_text):,} 字符"
                f" / {len(chunks)} 分块；"
                f"补丁 {patch_total} 章，定向重写 {rewrite_total} 章，"
                f"可执行 {auto_total} 章，延后 {len(deferred)} 项"
            )
            self.repair_status["item_index"] = 0
            self.repair_status["item_total"] = auto_total

        self.log(
            f"审计修复计划已生成（历史报告兼容路径）：{batch_id}；"
            f"原始 {len(source_text)} 字符，降噪后 {len(filtered_text)} 字符"
            f"（剔除 {noise_stats['dropped_blocks']} 段），"
            f"{len(chunks)} 分块用 {extract_model} 提取；"
            f"补丁 {patch_total} 章，定向重写 {rewrite_total} 章，"
            f"延后 {len(deferred)} 项。"
        )
        return {
            "ok": True,
            "batch_id": batch_id,
            "plan": plan,
            "status": self.repair_snapshot(),
        }

    def _run_audit_repair_plan(self, audit_text, model):
        try:
            self._repair_set_stage("规划", "Pro 正在把审计问题拆成最小修复项")
            result = self._create_audit_repair_plan_sync(audit_text, model)
            with self.repair_lock:
                self.repair_status["stage"] = "计划完成"
                self.repair_status["stage_label"] = (
                    f"修复计划已生成：{result.get('batch_id','')}"
                )
        except ProviderCancelledError:
            with self.repair_lock:
                self.repair_status["stage"] = "已停止"
                self.repair_status["stage_label"] = "用户停止修复计划生成"
            self.log("审计修复计划生成已停止。")
        except Exception as e:
            with self.repair_lock:
                self.repair_status["last_error"] = str(e)
                self.repair_status["stage"] = "错误"
                self.repair_status["stage_label"] = str(e)
            self.log(f"审计修复计划生成失败：{e}")
        finally:
            with self.repair_lock:
                self.repair_status["running"] = False
            self.repair_stop_event.clear()

    def start_audit_repair_plan(self, audit_text="", model="deepseek-v4-pro"):
        if self.status.get("running"):
            raise RuntimeError("Canon 正在运行")
        if self.audit_snapshot().get("running"):
            raise RuntimeError("剧情审计正在运行")
        try:
            if self.dlc_snapshot().get("running"):
                raise RuntimeError("DLC 正在运行")
        except AttributeError:
            pass

        model = str(model or "deepseek-v4-pro")
        if model not in {"deepseek-v4-pro", "deepseek-v4-flash"}:
            model = "deepseek-v4-pro"

        with self.repair_lock:
            if self.repair_thread and self.repair_thread.is_alive():
                raise RuntimeError("审计修复已经在运行")
            self.repair_stop_event.clear()
            # Mark running BEFORE returning to Web so the request is always short
            # and the next status poll immediately sees the background job.
            self.repair_status.update({
                "running": True,
                "stage": "规划",
                "stage_label": "正在启动 Pro 修复规划",
                "started_at": time.time(),
                "last_error": "",
                "model": model,
                "item_index": 0,
                "item_total": 0,
                "candidate_ready": 0,
                "candidate_blocked": 0,
                "joint_safe": None,
                "committed": False,
                "rolled_back": False,
                "last_commit_mode": "",
                "last_commit_forced": False,
                "last_commit_manual": False,
                "last_commit_failed_gates": [],
                "last_commit_chapters": [],
                "last_commit_force_reason": "",
                "prompt_tokens": 0,
                "cache_hit_tokens": 0,
                "completion_tokens": 0,
                "reasoning_tokens": 0,
                "cost_cny": 0.0,
                "afp": 0.0,
                "request_count": 0,
            })
            self.repair_thread = threading.Thread(
                target=self._run_audit_repair_plan,
                args=(str(audit_text or ""), model),
                name="NovelAgentAuditRepairPlan",
                daemon=True,
            )
            self.repair_thread.start()
        return self.repair_snapshot()

    def audit_repair_batch_detail(self, batch_id=""):
        bid, plan = self._load_repair_plan(batch_id)
        d = self._repair_batch_dir(bid)
        joint = {}
        if (d / "joint_review.json").exists():
            try:
                joint = json.loads((d / "joint_review.json").read_text(encoding="utf-8"))
            except Exception:
                joint = {}
        manifest = {}
        if (d / "commit_manifest.json").exists():
            try:
                manifest = json.loads((d / "commit_manifest.json").read_text(encoding="utf-8"))
            except Exception:
                manifest = {}
        approved = set()
        blocked = set()
        for value in joint.get("approved_chapters") or []:
            try:
                approved.add(int(value))
            except (TypeError, ValueError):
                continue
        for value in joint.get("blocked_chapters") or []:
            try:
                blocked.add(int(value))
            except (TypeError, ValueError):
                continue
        committed_rows = {}
        for value in self._repair_manifest_active_rows(manifest):
            try:
                committed_rows[int(value.get("chapter_no"))] = value
            except (AttributeError, TypeError, ValueError):
                continue
        items = []
        for item in plan.get("items", []):
            row = dict(item)
            n = int(row.get("chapter_no") or 0)
            meta = {}
            if n > 0:
                meta_path = d / "candidates" / f"{n:04d}.json"
                if meta_path.exists():
                    try:
                        meta = json.loads(meta_path.read_text(encoding="utf-8"))
                        row["candidate_meta"] = meta
                    except Exception:
                        row["candidate_meta"] = {}
                candidate_path = d / "candidates" / f"{n:04d}.md"
                original_path = self.root / "chapters" / f"{n:04d}.md"
                has_candidate = bool(meta) and candidate_path.exists() and original_path.exists()
                gate_reasons = list(row.get("evidence_gate_reasons") or [])
                if not self._repair_candidate_is_safe(meta) and has_candidate:
                    gate_reasons.append("候选独立验收未通过")
                if n in blocked:
                    gate_reasons.append("跨章节联合复核阻止")
                if n not in approved and has_candidate:
                    gate_reasons.append("尚未获得联合复核批准")
                row["commit_options"] = {
                    "candidate_available": has_candidate,
                    "joint_approved": n in approved,
                    "joint_blocked": n in blocked,
                    "auto_commit_allowed": bool(row.get("auto_commit_allowed")),
                    "manual_selectable": (
                        has_candidate and self._repair_candidate_is_safe(meta)
                        and n not in committed_rows
                    ),
                    "force_selectable": has_candidate and n not in committed_rows,
                    "blocked_reasons": list(dict.fromkeys(str(x) for x in gate_reasons if str(x).strip())),
                }
                if n in committed_rows:
                    row["commit_result"] = committed_rows[n]
            items.append(row)
        return {
            "ok": True, "batch_id": bid, "plan": {**plan, "items": items},
            "joint_review": joint, "commit_manifest": manifest,
            "status": self.repair_snapshot(),
        }

    def _repair_context_for_item(self, item):
        n = int(item["chapter_no"])
        nums = [n - 1, n, n + 1] + [int(x) for x in item.get("related_chapters") or []]
        nums = sorted({x for x in nums if x > 0})
        blocks = []
        for x in nums:
            p = self.root / "chapters" / f"{x:04d}.md"
            if p.exists():
                if x == n:
                    continue
                # Neighbor context is deliberately limited; the target full text is sent separately.
                txt = p.read_text(encoding="utf-8").strip()
                blocks.append(f"## 第{x}章相邻正文\n{txt[:9000]}")
            summary = self._audit_summary_for_chapter(x)
            if summary:
                blocks.append(f"## 第{x}章摘要\n{summary}")
        return "\n\n".join(blocks)

    @staticmethod
    def _repair_apply_exact_patches(original, patches, repair_class, unit_count=1):
        """Apply only model-proposed exact local replacements.

        Every `old` snippet must occur exactly once in the ORIGINAL chapter.
        Patches are validated for overlap and size before any text is changed.
        This prevents a "small fix" from silently turning into a chapter rewrite.

        Budgets scale with `unit_count`: a chapter holding three independent
        audit requirements legitimately needs more edits than one holding a
        single typo, and charging it single-unit limits would reject correct
        work just because the units share a file.
        """
        if not isinstance(patches, list) or not patches:
            raise ValueError("模型没有返回可应用的局部补丁")

        cls = str(repair_class or "TEXT_ONLY").upper()
        units = max(1, int(unit_count or 1))
        max_patches = (8 if cls == "TEXT_ONLY" else 10) * units
        max_old_chars = (2200 if cls == "TEXT_ONLY" else 6500) * units
        max_new_chars = (2600 if cls == "TEXT_ONLY" else 8500) * units
        max_growth = (900 if cls == "TEXT_ONLY" else 3200) * units

        if len(patches) > max_patches:
            raise ValueError(f"局部补丁数量 {len(patches)} 超过 {max_patches}")

        normalized = []
        rejected = []
        spans = []
        total_old = total_new = 0

        def reject(idx, raw, why):
            """Record one unusable patch and keep going.

            One malformed patch used to discard the whole attempt, including the
            patches that were perfectly applicable.  Rejecting individually lets
            the good fixes land and narrows the retry to what actually failed.
            """
            preview = ""
            if isinstance(raw, dict):
                preview = str(raw.get("old") or "")[:40]
            rejected.append({
                "index": idx,
                "reason": why,
                "old_preview": preview,
                "unit": (raw.get("unit") if isinstance(raw, dict) else None),
            })

        for idx, raw in enumerate(patches, 1):
            if not isinstance(raw, dict):
                reject(idx, raw, "补丁格式不是对象")
                continue
            old = str(raw.get("old") or "")
            new = str(raw.get("new") or "")
            if not old:
                reject(idx, raw, "old 为空")
                continue
            if old == new:
                reject(idx, raw, "没有实际变化")
                continue

            first = original.find(old)
            if first < 0:
                reject(idx, raw, "old 在原文中找不到，可能不是逐字复制")
                continue
            second = original.find(old, first + 1)
            if second >= 0:
                reject(idx, raw, "old 在原文中出现多次，需要扩大到唯一匹配")
                continue

            span = (first, first + len(old))
            clash = next(
                (i for i, (a, b) in enumerate(spans, 1)
                 if not (span[1] <= a or span[0] >= b)),
                0,
            )
            if clash:
                # The earlier patch is kept; only the later overlapping one is
                # dropped, so one duplicate cannot void a correct edit.
                reject(idx, raw, f"与已接受的第 {clash} 个补丁范围重叠")
                continue
            spans.append(span)

            try:
                unit = int(raw.get("unit") or 0)
            except Exception:
                unit = 0
            if not (1 <= unit <= units):
                # Attribution is advisory, not a gate: a correct patch must not
                # be thrown away because the model mislabelled its unit.
                unit = 0

            total_old += len(old)
            total_new += len(new)
            normalized.append({
                "unit": unit,
                "old": old,
                "new": new,
                "reason": str(raw.get("reason") or "").strip(),
                "_start": first,
                "_end": first + len(old),
            })

        if not normalized:
            detail = "；".join(
                f"第 {r['index']} 个补丁{r['reason']}" for r in rejected
            ) or "模型没有返回可应用的局部补丁"
            raise ValueError(detail)

        if total_old > max_old_chars:
            raise ValueError(
                f"补丁覆盖原文 {total_old} 字符，超过 {cls} 上限 {max_old_chars}"
            )
        if total_new > max_new_chars:
            raise ValueError(
                f"补丁新文本 {total_new} 字符，超过 {cls} 上限 {max_new_chars}"
            )
        if total_new - total_old > max_growth:
            raise ValueError(
                f"补丁净新增 {total_new-total_old} 字符，超过 {cls} 上限 {max_growth}"
            )

        result = original
        for p in sorted(normalized, key=lambda x: x["_start"], reverse=True):
            result = result[:p["_start"]] + p["new"] + result[p["_end"]:]

        public = [
            {
                "unit": p["unit"],
                "old": p["old"],
                "new": p["new"],
                "reason": p["reason"],
            }
            for p in normalized
        ]
        covered = sorted({p["unit"] for p in normalized if p["unit"] > 0})
        return result, {
            "patch_count": len(public),
            "patch_old_chars": total_old,
            "patch_new_chars": total_new,
            "patch_growth": total_new - total_old,
            "unit_count": units,
            "units_covered": covered,
            "units_uncovered": [u for u in range(1, units + 1) if u not in covered],
            "unattributed_patches": sum(1 for p in normalized if p["unit"] == 0),
            "patches": public,
            # Partial success is reported rather than raised.  The caller decides
            # whether the applied subset is enough, and the retry prompt quotes
            # these reasons so only the failed patches are regenerated.
            "rejected_count": len(rejected),
            "rejected": rejected,
            "partial": bool(rejected),
        }

    def _repair_review_local_candidate(self, batch_id, item, original, candidate,
                                       context, preserve, model, attempt,
                                       verify_reasons=None):
        n = int(item["chapter_no"])
        cls = str(item.get("repair_class") or "TEXT_ONLY")
        diff = self._repair_diff(original, candidate, 14000)
        original_tail = original[-2600:]
        candidate_tail = candidate[-2600:]

        review_system = """你是超长篇小说“小修”的独立自动验收器。
候选由精确局部补丁生成，不允许把文风偏好当作问题。
你的目标是让真正的小错误能够自动通过，同时阻止会改变 Canon 的修改。
必须输出严格 JSON。"""

        # Telling the reviewer why the program check escalated keeps it focused
        # on the doubtful part instead of re-auditing the whole diff.
        concerns = "\n".join(f"- {r}" for r in (verify_reasons or []))
        concern_block = f"""【程序校验存疑点——请优先判断这些】
{concerns}

""" if concerns else ""

        review_user = f"""【修复类型】{cls}
{concern_block}【指定修复要求】
{item.get('instruction','')}

【必须保持】
{preserve}

【相邻/关联章节证据】
{context}

【本次实际局部 Diff】
{diff}

【原文章末】
{original_tail}

【候选章末】
{candidate_tail}

输出：
{{
  "requested_fix_applied": true,
  "unrelated_changes": false,
  "canon_end_state_changed": false,
  "downstream_conflict": false,
  "safe_to_batch_commit": true,
  "findings": ["只写与本次小修直接相关的判断"]
}}

判定原则：
- 只要指定小错误已修复，且局部补丁没有制造新矛盾，就应通过。
- 不得因为“可以写得更好”“措辞还能润色”而阻止。
- TEXT_ONLY 的日期、时间词、称谓、笔误、局部措辞修正，只要事实正确且范围局部，应正常通过。
- CONTINUITY_MINOR 允许为了闭环既有遗漏而替换/补充少量句子或短段，但章末长期 Canon 状态必须不变。
"""
        # Program checks prove only that the edit is small and in scope.  They
        # cannot prove that `new` actually fixes the reported fact, so every
        # candidate receives a semantic review.  Clear, program-safe patches use
        # Flash; uncertain program checks and final Pro attempts use Pro.
        use_pro = bool(verify_reasons) or str(model or "") == "deepseek-v4-pro"
        review_model = "deepseek-v4-pro" if use_pro else "deepseek-v4-flash"
        review = self._repair_chat_json(
            f"repair_review_{batch_id}_{n:04d}_a{attempt}",
            review_system,
            review_user,
            model=review_model,
            effort="high" if use_pro else "low",
            max_tokens=2600,
        )
        review["review_mode"] = "pro_semantic" if use_pro else "flash_semantic"
        review["_review_model"] = review_model
        return review

    def _repair_review_rewrite(self, batch_id, item, original, candidate,
                               context, preserve, attempt, verify_reasons=None):
        """Semantic acceptance for a rewrite candidate.

        Rewrites always come here: the programmatic screen can only prove a
        candidate is structurally sane, never that the prose still says what the
        surrounding chapters assume.  Pro is used unconditionally because a
        rewrite is the one repair class that can silently rewrite Canon.
        """
        n = int(item["chapter_no"])
        cls = str(item.get("repair_class") or "REWRITE_SPAN").upper()
        diff = self._repair_diff(original, candidate, 18000)

        review_system = """你是超长篇小说“重写结果”的独立自动验收器。
候选是对指定问题的重写，允许措辞与句子结构变化，不要把文风偏好当成问题。
你要判断的是：指定问题是否真的解决，以及重写有没有破坏既有设定与上下游衔接。
必须输出严格 JSON。"""

        concerns = "\n".join(f"- {r}" for r in (verify_reasons or []))
        concern_block = f"""【程序校验存疑点——请优先判断这些】
{concerns}

""" if concerns else ""

        review_user = f"""【修复类型】{cls}

{concern_block}【指定修复要求】
{item.get('instruction','')}

【必须保持】
{preserve}

【目标章大纲】
{self.current_chapter_outline(n)}

【目标章开始前状态】
{self.format_current_state(n-1)}

【相邻/关联章节证据】
{context}

【本次重写 Diff】
{diff}

【原文章末】
{original[-2600:]}

【候选章末】
{candidate[-2600:]}

输出：
{{
  "requested_fix_applied": true,
  "unrelated_changes": false,
  "canon_end_state_changed": false,
  "downstream_conflict": false,
  "safe_to_batch_commit": true,
  "findings": ["只写与本次重写直接相关的判断"]
}}

判定原则：
- 重写允许改写措辞与句子结构，只要指定问题解决且没有制造新矛盾，就应通过。
- 不得因为“可以写得更好”而阻止。
- REWRITE_SPAN 只应改动问题所在的段落范围，章末长期 Canon 状态必须不变。
- REWRITE_CHAPTER 可以整章重写，但开始前状态与大纲约定的章末状态都必须成立。
- 如果重写引入了原文没有的主线、伏笔、能力等级或人物关系结果，视为不安全。
"""
        return self._repair_chat_json(
            f"repair_rewrite_review_{batch_id}_{n:04d}_a{attempt}",
            review_system,
            review_user,
            model="deepseek-v4-pro",
            effort="high",
            max_tokens=3000,
        )

    def _generate_rewrite_candidate(self, batch_id, item, model, extra_feedback=""):
        """Generate a rewrite candidate for one chapter and self-heal automatically.

        This is the channel for findings that exact local patching cannot express:
        a passage that has to be re-argued, or a chapter whose premise the audit
        rejected.  Previously these were parked as MANUAL_ONLY, which is what made
        the pipeline look like it converged while real problems were left in the
        text.

        The output is a full chapter body either way.  A REWRITE_SPAN is still
        asked to keep everything outside the target passage byte-identical, and
        the programmatic screen enforces that before any review is paid for.
        """
        n = int(item["chapter_no"])
        d = self._repair_batch_dir(batch_id)
        cdir = d / "candidates"
        cdir.mkdir(parents=True, exist_ok=True)
        original_path = self.root / "chapters" / f"{n:04d}.md"
        original = original_path.read_text(encoding="utf-8")
        original_hash = self._repair_hash(original)
        cls = self._repair_widest_class(item.get("repair_class"))
        if cls not in self._REPAIR_REWRITE_CLASSES:
            raise RuntimeError(f"第 {n} 章不是重写类型：{cls}")

        context = self._repair_context_for_item(item)
        preserve = "\n".join(
            f"- {x}" for x in item.get("must_preserve") or []
        ) or "- 目标章结束时的既有 Canon 状态全部保持不变"
        units = [
            a for a in (item.get("anchors") or [])
            if str(a.get("instruction") or "").strip()
        ]
        unit_count = max(1, len(units))

        # Rewrites are graded the same way patches are: mechanical failures (an
        # empty or truncated body, malformed JSON) are re-asked cheaply, while
        # genuine semantic rejection is what spends the budget and escalates.
        max_semantic_attempts = 3
        max_mechanical_retries = 3
        max_attempts = max_semantic_attempts + max_mechanical_retries
        feedback = str(extra_feedback or "").strip()
        last_candidate = original
        last_review = {}
        last_error = ""
        attempts_used = 0
        semantic_attempts = 0
        mechanical_retries = 0
        failure_kinds = []
        last_verify = {}

        span_rule = (
            "只允许改写与问题直接相关的段落；其余段落必须逐字不变地照抄，"
            "包括最后一段。"
            if cls == "REWRITE_SPAN"
            else "可以整章重写，但必须落在大纲与开始前状态之间，"
            "并保持章末应有的 Canon 状态。"
        )

        for attempt in range(1, max_attempts + 1):
            self._repair_check_cancel()
            if semantic_attempts >= max_semantic_attempts:
                break
            if mechanical_retries >= max_mechanical_retries:
                break
            attempts_used = attempt

            generation_model = self._repair_generation_model(
                cls, semantic_attempts + 1, preferred=model,
            )

            system = f"""你是超长篇小说的“定向重写器”。
你要解决审计指出的具体问题，而不是重新构思这一章。

必须严格输出 JSON：
{{
  "chapter": "重写后的完整章节正文",
  "changed_paragraphs": ["被改写的段落开头前十几个字，便于程序核对"],
  "reason": "各条要求分别是如何解决的"
}}

硬规则：
- {span_rule}
- 输出 chapter 必须是完整正文，不要只给片段，不要加解释性前后缀。
- 逐条解决【审计修复要求】里的每一条，不要遗漏，也不要顺手改别的问题。
- 不新增主线、伏笔、人物关系结果、能力等级、重要物品/地点/伤病状态。
- 保持原有叙事人称、时态与整体篇幅规模。
- 不要输出 Markdown 围栏。"""

            user = f"""【修复类型】
{cls}

【审计修复要求】
{self._repair_render_units(item.get('anchors')) or item.get('instruction','')}

【必须保持】
{preserve}

【目标章大纲】
{self.current_chapter_outline(n)}

【目标章开始前状态】
{self.format_current_state(n-1)}

【相邻/关联章节证据】
{context}

【第{n}章完整原文】
{original}

"""
            if feedback:
                user += f"""【上一轮自动校验/联合复核反馈】
{feedback}

请针对反馈重新给出重写结果；不要要求人工处理。

"""
            user += "只输出严格 JSON，不要 Markdown 围栏。"

            try:
                obj = self._repair_chat_json(
                    f"repair_rewrite_{batch_id}_{n:04d}_a{attempt}",
                    system,
                    user,
                    model=generation_model,
                    effort="high",
                    # A rewrite returns a whole chapter, so the ceiling has to
                    # cover chapter length rather than patch length.
                    max_tokens=max(6000, min(16000, len(original) // 2 + 4000)),
                )
                candidate = ""
                if isinstance(obj, dict):
                    candidate = str(obj.get("chapter") or "").strip()
                if not candidate:
                    raise RuntimeError("重写结果没有返回可用的 chapter 正文")

                verify = self._repair_verify_rewrite(
                    original, candidate, cls,
                    must_preserve=item.get("must_preserve") or [],
                    anchors=item.get("anchors") or [],
                )
                last_verify = verify

                if verify["verdict"] != "pass":
                    # Structurally broken candidates never reach the reviewer.
                    # Paying Pro to explain a truncated chapter is waste.
                    raise RuntimeError(
                        "重写结果未通过程序校验：" + "；".join(verify["reasons"])
                    )

                # Every rewrite gets a semantic review; the programmatic screen
                # is a filter in front of it, never a substitute for it.
                review = self._repair_review_rewrite(
                    batch_id, item, original, candidate,
                    context, preserve, attempt,
                    verify_reasons=verify.get("reasons"),
                )
                review["review_mode"] = "model"

                safe = (
                    bool(review.get("requested_fix_applied"))
                    and not bool(review.get("unrelated_changes"))
                    and not bool(review.get("canon_end_state_changed"))
                    and not bool(review.get("downstream_conflict"))
                    and bool(review.get("safe_to_batch_commit"))
                )

                last_candidate = candidate
                last_review = review
                last_error = ""

                if safe:
                    break

                # Review rejection is semantic by definition: the candidate was
                # well-formed and the reviewer still disagreed with its content.
                semantic_attempts += 1
                failure_kinds.append("semantic")
                findings = "；".join(
                    str(x) for x in (review.get("findings") or []) if str(x).strip()
                )
                feedback = (
                    f"自动验收未通过。"
                    f"requested_fix_applied={bool(review.get('requested_fix_applied'))}；"
                    f"unrelated_changes={bool(review.get('unrelated_changes'))}；"
                    f"canon_end_state_changed={bool(review.get('canon_end_state_changed'))}；"
                    f"downstream_conflict={bool(review.get('downstream_conflict'))}。"
                    f"{findings}。"
                    "请在保持其余段落不变的前提下，只重写仍有问题的部分。"
                )
            except Exception as e:
                last_error = str(e)
                kind = self._repair_failure_kind(last_error)
                failure_kinds.append(kind)
                if kind == "mechanical":
                    mechanical_retries += 1
                else:
                    semantic_attempts += 1
                feedback = (
                    f"上一轮重写结果无法使用：{last_error}。"
                    "请输出完整章节正文，保持未涉及问题的段落逐字不变。"
                )
                last_review = {
                    "requested_fix_applied": False,
                    "unrelated_changes": False,
                    "canon_end_state_changed": False,
                    "downstream_conflict": False,
                    "safe_to_batch_commit": False,
                    "findings": [last_error],
                }

        ratio = self._repair_change_ratio(original, last_candidate)
        safe = (
            bool(last_review.get("requested_fix_applied"))
            and not bool(last_review.get("unrelated_changes"))
            and not bool(last_review.get("canon_end_state_changed"))
            and not bool(last_review.get("downstream_conflict"))
            and bool(last_review.get("safe_to_batch_commit"))
        )

        cpath = cdir / f"{n:04d}.md"
        cpath.write_text(last_candidate.rstrip() + "\n", encoding="utf-8")
        meta = {
            "chapter_no": n,
            "repair_class": cls,
            "channel": "rewrite",
            "instruction": item.get("instruction", ""),
            "unit_count": unit_count,
            "units": [
                {
                    "index": i,
                    "task_id": a.get("task_id"),
                    "repair_class": a.get("repair_class"),
                    "instruction": a.get("instruction"),
                    # A rewrite is not patch-attributed, so coverage is a
                    # reviewer judgement rather than a program fact.  Recording
                    # it as unknown is honest; claiming True would be a lie.
                    "covered": None,
                }
                for i, a in enumerate(units, 1)
            ],
            "task_ids": list(item.get("task_ids") or []),
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "model": model,
            "review_model": "deepseek-v4-pro",
            "original_sha256": original_hash,
            "candidate_sha256": self._repair_hash(last_candidate.rstrip() + "\n"),
            "change_ratio": ratio,
            "generation_mode": "directed_rewrite",
            "review_mode": last_review.get("review_mode"),
            "verify_verdict": last_verify.get("verdict"),
            "verify_reasons": list(last_verify.get("reasons") or []),
            "length_ratio": last_verify.get("length_ratio"),
            "attempts": attempts_used,
            "semantic_attempts": semantic_attempts,
            "mechanical_retries": mechanical_retries,
            "failure_kinds": failure_kinds,
            "auto_self_healed": attempts_used > 1 and safe,
            "auto_repair_exhausted": not safe,
            "last_apply_error": last_error,
            "review": last_review,
            "safe": safe,
        }
        (cdir / f"{n:04d}.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return meta

    def _generate_candidate_for_item(self, batch_id, item, model, extra_feedback=""):
        """Route one plan item to the channel its repair class requires.

        Callers (first pass, self-heal retry, manual single-chapter retry) all go
        through here so a chapter can never be handled by the patch channel in one
        pass and the rewrite channel in another.
        """
        cls = self._repair_widest_class(item.get("repair_class"))
        channel = str(item.get("channel") or "").strip().lower()
        if not channel:
            # Plans written before the rewrite channel existed have no `channel`
            # field; derive it so old batches stay runnable.
            channel = "rewrite" if cls in self._REPAIR_REWRITE_CLASSES else "patch"
        if channel == "rewrite":
            return self._generate_rewrite_candidate(
                batch_id, item, model, extra_feedback=extra_feedback,
            )
        return self._generate_repair_candidate(
            batch_id, item, model, extra_feedback=extra_feedback,
        )

    def _generate_repair_candidate(self, batch_id, item, model, extra_feedback=""):
        """Generate a small repair as exact local patches and self-heal automatically.

        No whole-chapter rewrite is used for TEXT_ONLY / CONTINUITY_MINOR.
        Failed exact matches or failed reviews are fed back into the next attempt.
        """
        n = int(item["chapter_no"])
        d = self._repair_batch_dir(batch_id)
        cdir = d / "candidates"
        cdir.mkdir(parents=True, exist_ok=True)
        original_path = self.root / "chapters" / f"{n:04d}.md"
        original = original_path.read_text(encoding="utf-8")
        original_hash = self._repair_hash(original)
        cls = str(item.get("repair_class") or "TEXT_ONLY").upper()
        if cls not in {"TEXT_ONLY", "CONTINUITY_MINOR"}:
            raise RuntimeError(f"第 {n} 章不是自动小修类型：{cls}")

        context = self._repair_context_for_item(item)
        preserve = "\n".join(
            f"- {x}" for x in item.get("must_preserve") or []
        ) or "- 目标章结束时的既有 Canon 状态全部保持不变"

        # Independent repair units sharing this chapter's lock.  Budgets and
        # per-unit coverage checks are derived from this count.
        units = [
            a for a in (item.get("anchors") or [])
            if str(a.get("instruction") or "").strip()
        ]
        unit_count = max(1, len(units))

        # Attempts are budgeted by failure kind rather than by raw count.
        #
        # A mechanical failure (a snippet not copied verbatim, an ambiguous
        # `old`, malformed JSON) says nothing about whether the model understood
        # the fix, and restating the constraint usually settles it on the next
        # cheap call.  Charging those against the same budget as genuine semantic
        # disagreement meant a chapter could exhaust its retries — and reach Pro,
        # and then manual review — without the model ever having been wrong about
        # the edit itself.
        #
        # So: up to `max_semantic_attempts` rounds of real disagreement, with Pro
        # reserved for the last of them, plus a separate allowance of mechanical
        # re-asks that do not advance the escalation ladder.
        max_semantic_attempts = 3
        max_mechanical_retries = 3
        max_attempts = max_semantic_attempts + max_mechanical_retries
        feedback = str(extra_feedback or "").strip()
        last_candidate = original
        last_review = {}
        last_patch_meta = {}
        last_error = ""
        attempts_used = 0
        semantic_attempts = 0
        mechanical_retries = 0
        failure_kinds = []
        # A windowed excerpt can make an `old` look unique when it is not unique
        # in the chapter.  That failure is caused by the trimming, so the retry
        # sends the whole chapter rather than asking the model to guess wider.
        force_full_context = False
        last_context_meta = {}
        last_verify = {}
        last_program_hard_safe = False

        for attempt in range(1, max_attempts + 1):
            self._repair_check_cancel()
            # Either budget running out ends the loop.  Mechanical re-asks are
            # capped too, so a model that keeps failing to copy verbatim cannot
            # spin here indefinitely.
            if semantic_attempts >= max_semantic_attempts:
                break
            if mechanical_retries >= max_mechanical_retries:
                break
            attempts_used = attempt

            # Escalation tracks semantic attempts only, so mechanical re-asks
            # stay on Flash however many of them a chapter needs.
            generation_model = self._repair_generation_model(
                cls, semantic_attempts + 1, preferred=model,
            )

            system = """你是超长篇小说的“精确局部小修器”。
你绝对不能重写整章。你只能返回需要替换的最小原文片段。

必须严格输出 JSON：
{
  "patches": [
    {
      "unit": 1,
      "old": "从目标章原文逐字复制、且在原文中只出现一次的连续片段",
      "new": "替换后的文本",
      "reason": "此补丁对应哪条审计要求"
    }
  ]
}

硬规则：
- old 必须逐字来自原文，不能省略号，不能改写，不能虚构。
- old 必须足够长，使它在原文中唯一出现。
- unit 必须是【审计修复要求】里的条目序号；每条要求都要有补丁覆盖。
- 各条要求彼此独立，不要为了一条要求去改动另一条涉及的文字。
- 只修指定问题。无关句子一个字都不要动。
- TEXT_ONLY 只做日期、时间词、称谓、笔误、局部措辞等文字级修正。
- CONTINUITY_MINOR 只允许替换/补少量句子或一个短段，不能改变章末 Canon 状态。
- 不新增主线、伏笔、人物关系结果、能力等级、重要物品/地点/伤病状态。
- 不输出完整章节。"""

            # Located units let us send a few paragraphs instead of the whole
            # chapter.  Cost then tracks the size of the fix, not the length of
            # the chapter it happens to live in.
            body_text, body_meta = self._repair_patch_context(
                original, None if force_full_context else item.get("anchors"),
            )
            last_context_meta = body_meta
            if body_meta["mode"] == "windowed":
                body_header = (
                    f"【第{n}章相关片段（已按定位裁剪，"
                    f"共 {body_meta['window_count']} 段）】"
                )
                body_note = (
                    "以上是按程序定位裁剪出的片段，省略部分与本次修复无关。\n"
                    "old 必须逐字来自上面的片段，并且要足够长，"
                    "使它在【整章】范围内唯一出现。\n"
                )
            else:
                body_header = f"【第{n}章完整原文】"
                body_note = ""

            user = f"""【修复类型】
{cls}

【审计修复要求】
{self._repair_render_units(item.get('anchors')) or item.get('instruction','')}

【必须保持】
{preserve}

【目标章大纲】
{self.current_chapter_outline(n)}

【目标章开始前状态】
{self.format_current_state(n-1)}

【相邻/关联章节证据】
{context}

{body_header}
{body_text}

{body_note}"""
            if feedback:
                user += f"""【上一轮自动校验/联合复核反馈】
{feedback}

请针对反馈重新给出更精确、更小的 exact patch；不要要求人工处理。

"""
            user += "只输出严格 JSON，不要 Markdown 围栏。"

            try:
                obj = self._repair_chat_json(
                    f"repair_patch_{batch_id}_{n:04d}_a{attempt}",
                    system,
                    user,
                    model=generation_model,
                    effort="high",
                    max_tokens=4200 if cls == "CONTINUITY_MINOR" else 3000,
                )
                patches = obj.get("patches") if isinstance(obj, dict) else None
                candidate, patch_meta = self._repair_apply_exact_patches(
                    original, patches, cls, unit_count=unit_count
                )
                ratio = self._repair_change_ratio(original, candidate)

                # Program checks constrain the edit; semantic review decides
                # whether the requested fact was actually repaired.
                verify = self._repair_verify_patches(
                    original, candidate, patch_meta, cls,
                    anchors=item.get("anchors"),
                )
                last_verify = verify
                program_hard_safe = self._repair_program_hard_safe(verify)
                last_program_hard_safe = program_hard_safe
                review = self._repair_review_local_candidate(
                    batch_id, item, original, candidate,
                    context, preserve, generation_model, attempt,
                    verify_reasons=(
                        verify["reasons"]
                        if verify["verdict"] != "pass"
                        else []
                    ),
                )

                safe = (
                    program_hard_safe
                    and bool(review.get("requested_fix_applied"))
                    and not bool(review.get("unrelated_changes"))
                    and not bool(review.get("canon_end_state_changed"))
                    and not bool(review.get("downstream_conflict"))
                    and bool(review.get("safe_to_batch_commit"))
                )

                last_candidate = candidate
                last_review = review
                last_patch_meta = patch_meta
                last_error = ""

                if safe:
                    break

                # A rejected or missing patch is a mechanical slip even though
                # the attempt got as far as review, so classify on the concrete
                # gap first and fall back to the review verdict.
                kind = self._repair_failure_kind(
                    "；".join(
                        str(r.get("reason") or "")
                        for r in (patch_meta.get("rejected") or [])
                    ),
                    verify.get("reasons") or [],
                )
                failure_kinds.append(kind)
                if kind == "mechanical":
                    mechanical_retries += 1
                else:
                    semantic_attempts += 1

                findings = "；".join(
                    str(x) for x in (review.get("findings") or []) if str(x).strip()
                )
                # Retry feedback names the specific units and patches that
                # failed, so the next attempt fixes the gap instead of
                # regenerating work that already applied cleanly.
                gaps = []
                for u in (patch_meta.get("units_uncovered") or []):
                    if 1 <= u <= len(units):
                        gaps.append(
                            f"第{u}条要求仍无补丁：{units[u-1].get('instruction','')}"
                        )
                for r in (patch_meta.get("rejected") or []):
                    gaps.append(
                        f"第 {r.get('index')} 个补丁被程序拒绝（{r.get('reason')}），"
                        f"old 开头为“{r.get('old_preview')}”"
                    )
                gap_text = "；".join(gaps)
                keep = (
                    "注意：每次候选都会从原始正文重新应用。下一轮必须重新提交所有"
                    "已经正确的补丁，并修正或补齐失败补丁；不得只提交缺口。"
                    if patch_meta.get("patch_count") else ""
                )
                feedback = (
                    f"自动验收未通过。"
                    f"requested_fix_applied={bool(review.get('requested_fix_applied'))}；"
                    f"unrelated_changes={bool(review.get('unrelated_changes'))}；"
                    f"canon_end_state_changed={bool(review.get('canon_end_state_changed'))}；"
                    f"downstream_conflict={bool(review.get('downstream_conflict'))}。"
                    f"{findings}"
                    f"{('。' + gap_text) if gap_text else ''}"
                    f"{keep}"
                )
            except Exception as e:
                last_error = str(e)
                # Nothing could be applied at all.  This is almost always a
                # verbatim-copy or JSON problem rather than a disagreement about
                # the fix, so it is re-asked cheaply instead of spending the
                # semantic budget.
                kind = self._repair_failure_kind(last_error)
                failure_kinds.append(kind)
                if kind == "mechanical":
                    mechanical_retries += 1
                else:
                    semantic_attempts += 1
                if body_meta.get("mode") == "windowed" and (
                    "出现多次" in last_error or "找不到" in last_error
                ):
                    force_full_context = True
                feedback = (
                    f"上一轮补丁无法被程序安全应用：{last_error}。"
                    "请扩大 old 的上下文使其唯一匹配，并进一步缩小修改范围。"
                    "每次候选都从原始正文重新应用，因此必须重新提交覆盖全部审计要求的"
                    "完整补丁集合，同时修正出问题的补丁。"
                )
                last_review = {
                    "requested_fix_applied": False,
                    "unrelated_changes": False,
                    "canon_end_state_changed": False,
                    "downstream_conflict": False,
                    "safe_to_batch_commit": False,
                    "findings": [last_error],
                }
                last_patch_meta = {}

        ratio = self._repair_change_ratio(original, last_candidate)
        safe = (
            last_program_hard_safe
            and bool(last_review.get("requested_fix_applied"))
            and not bool(last_review.get("unrelated_changes"))
            and not bool(last_review.get("canon_end_state_changed"))
            and not bool(last_review.get("downstream_conflict"))
            and bool(last_review.get("safe_to_batch_commit"))
        )

        cpath = cdir / f"{n:04d}.md"
        cpath.write_text(last_candidate.rstrip() + "\n", encoding="utf-8")
        meta = {
            "chapter_no": n,
            "repair_class": cls,
            "instruction": item.get("instruction", ""),
            "unit_count": unit_count,
            "units": [
                {
                    "index": i,
                    "task_id": a.get("task_id"),
                    "repair_class": a.get("repair_class"),
                    "instruction": a.get("instruction"),
                    "covered": i in set(last_patch_meta.get("units_covered") or []),
                }
                for i, a in enumerate(units, 1)
            ],
            "task_ids": list(item.get("task_ids") or []),
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "model": model,
            "review_model": last_review.get("_review_model") or "",
            "original_sha256": original_hash,
            "candidate_sha256": self._repair_hash(last_candidate.rstrip() + "\n"),
            "change_ratio": ratio,
            "change_threshold": None,
            "generation_mode": "exact_local_patch",
            "context_mode": last_context_meta.get("mode"),
            "context_chars": last_context_meta.get("chars"),
            "context_full_chars": last_context_meta.get("full_chars"),
            "review_mode": last_review.get("review_mode"),
            "verify_verdict": last_verify.get("verdict"),
            "verify_reasons": list(last_verify.get("reasons") or []),
            "program_hard_safe": last_program_hard_safe,
            "patches_rejected": int(last_patch_meta.get("rejected_count") or 0),
            "patch_rejections": list(last_patch_meta.get("rejected") or []),
            "attempts": attempts_used,
            # Split counters make the retry budget auditable: a chapter that
            # failed three times on verbatim-copy slips looks very different from
            # one the model genuinely could not fix, and only the latter should
            # ever have reached Pro.
            "semantic_attempts": semantic_attempts,
            "mechanical_retries": mechanical_retries,
            "failure_kinds": failure_kinds,
            "auto_self_healed": attempts_used > 1 and safe,
            "auto_repair_exhausted": not safe,
            "last_apply_error": last_error,
            "patch_meta": last_patch_meta,
            "review": last_review,
            "safe": safe,
        }
        (cdir / f"{n:04d}.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return meta

    def _joint_repair_review(self, batch_id, plan, model,
                             scope=None, previous=None):
        """Joint-review the batch, optionally only the chapters in `scope`.

        `scope=None` reviews everything.  A narrowed scope is used after a
        single-chapter retry: chapters outside the affected clusters keep the
        verdict they already earned instead of being paid for again.
        """
        d = self._repair_batch_dir(batch_id)
        scope_set = None if scope is None else {int(x) for x in scope}
        previous = previous if isinstance(previous, dict) else {}
        packets = []
        approved_by_item = []
        for item in plan.get("items", []):
            if not item.get("auto_candidate"):
                continue
            n = int(item.get("chapter_no") or 0)
            meta_path = d / "candidates" / f"{n:04d}.json"
            cand_path = d / "candidates" / f"{n:04d}.md"
            orig_path = self.root / "chapters" / f"{n:04d}.md"
            if not (meta_path.exists() and cand_path.exists() and orig_path.exists()):
                continue
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            original = orig_path.read_text(encoding="utf-8")
            candidate = cand_path.read_text(encoding="utf-8")
            packets.append({
                "chapter_no": n,
                "group_id": item.get("group_id"),
                "cluster_id": item.get("cluster_id"),
                "repair_class": item.get("repair_class"),
                "instruction": item.get("instruction"),
                "per_item_safe": bool(meta.get("safe")),
                "per_item_review": meta.get("review") or {},
                "diff": self._repair_diff(original, candidate, 9000),
                "neighbor_summaries": self._repair_context_for_item(item)[:12000],
            })
            if meta.get("safe"):
                approved_by_item.append(n)

        safe_set = set(approved_by_item)
        packet_by_chapter = {int(p["chapter_no"]): p for p in packets}

        # Cross-chapter contradiction is only possible between chapters that are
        # actually related.  Reviewing one cluster at a time keeps each call
        # small enough to reason over and stops unrelated chapters from paying
        # for each other's analysis.
        clusters, direct_pass = self._repair_review_clusters(packets)

        approved, blocked = [], []
        all_findings, manual_notes = [], []
        cluster_reports = []

        # An isolated TEXT_ONLY fix has no second candidate to contradict and
        # cannot move Canon state, so its independent verdict already stands.
        for n in direct_pass:
            if n in safe_set:
                approved.append(n)
            else:
                blocked.append(n)
            cluster_reports.append({
                "chapters": [n],
                "mode": "direct_pass",
                "reason": "孤立 TEXT_ONLY 候选，无跨章组合风险，沿用独立验收结论",
            })

        system = """你是超长篇小说“跨章节修复批次”的最终联合验收员。
你看到的是同一批审计修复中【相互关联的一组】章节的局部 diff 和相邻章节证据。
只检查这些修改组合起来以后是否产生跨章矛盾、时间线冲突、知识边界变化或重复补丁。
不得要求额外润色。必须严格 JSON。"""

        # Chunk sizing uses the diff length, which is what actually dominates the
        # prompt for a review packet.
        packet_sizes = {
            int(p["chapter_no"]): len(str(p.get("diff") or ""))
            for p in packets
        }
        unresolved_all = []

        review_batches = []
        for members in clusters:
            for chunk in self._repair_review_chunks(members, packet_sizes):
                review_batches.append((members, chunk))

        carried = []
        if scope_set is not None:
            kept, skipped = [], []
            for members, chunk in review_batches:
                if scope_set & set(chunk):
                    kept.append((members, chunk))
                else:
                    skipped.append(chunk)
            review_batches = kept
            # Verdicts for chapters nobody touched carry over verbatim.  Anything
            # the previous run left without a verdict stays without one, so a
            # narrowed re-review can never turn a block into a pass by omission.
            prev_approved = set()
            prev_blocked = set()
            for x in previous.get("approved_chapters") or []:
                try:
                    prev_approved.add(int(x))
                except Exception:
                    continue
            for x in previous.get("blocked_chapters") or []:
                try:
                    prev_blocked.add(int(x))
                except Exception:
                    continue
            for chunk in skipped:
                for n in chunk:
                    if n in scope_set:
                        continue
                    if n in prev_blocked:
                        blocked.append(n)
                    elif n in prev_approved:
                        approved.append(n)
                    else:
                        # Unknown previous state: the safe reading is a block.
                        blocked.append(n)
                    if n not in carried:
                        carried.append(n)

        for members, chunk in review_batches:
            group = [packet_by_chapter[n] for n in chunk if n in packet_by_chapter]
            if not group:
                continue
            group_safe = [n for n in chunk if n in safe_set]
            user = f"""【本组章节】
{json.dumps(chunk, ensure_ascii=False)}

【各章候选 diff 与独立验收】
{json.dumps(group, ensure_ascii=False, indent=2)}

输出：
{{
  "safe_to_commit": true,
  "approved_chapters": [1,2],
  "blocked_chapters": [3],
  "cross_chapter_findings": [],
  "manual_only_notes": []
}}

规则：
- 只能批准 per_item_safe=true 的章节。
- 某一章的修复若单独正确但与另一候选组合后形成新矛盾，必须阻止相关章节并说明。
- MANUAL_ONLY / DEFER_FUTURE 不得加入 approved_chapters。
- 只判断本组内部的章节，不要评论未出现在本组的章节。
- 本组每一章都必须出现在 approved_chapters 或 blocked_chapters 之一。
"""
            stage = f"repair_joint_{batch_id}_c{members[0]:04d}_{chunk[0]:04d}"
            part = self._repair_chat_json(
                stage, system, user,
                model="deepseek-v4-pro", effort="high", max_tokens=5500,
            )

            part_approved, part_blocked, part_unresolved = self._repair_joint_resolve(
                chunk, group_safe, part,
            )
            approved.extend(part_approved)
            blocked.extend(part_blocked)
            unresolved_all.extend(part_unresolved)
            all_findings.extend(
                str(x).strip() for x in (part.get("cross_chapter_findings") or [])
                if x is not None and str(x).strip()
            )
            manual_notes.extend(
                str(x).strip() for x in (part.get("manual_only_notes") or [])
                if x is not None and str(x).strip()
            )
            cluster_reports.append({
                "cluster_chapters": members,
                "chapters": chunk,
                "mode": "joint_review",
                "stage": stage,
                "approved_chapters": sorted(part_approved),
                "blocked_chapters": sorted(part_blocked),
                "unresolved_chapters": sorted(part_unresolved),
                "cross_chapter_findings": list(
                    part.get("cross_chapter_findings") or []
                ),
            })

        # Overlapping chunks can approve a chapter in one call and block it in
        # another; a block from any call wins.
        approved = sorted({n for n in approved if n not in set(blocked)})
        blocked = sorted(set(blocked))
        joint = {
            "safe_to_commit": bool(approved) and not blocked,
            "approved_chapters": approved,
            "blocked_chapters": blocked,
            "cross_chapter_findings": all_findings,
            "manual_only_notes": manual_notes,
            "review_clusters": cluster_reports,
            # One Pro call per cluster chunk instead of one per batch; the
            # direct-pass chapters cost nothing.
            "joint_calls": len(review_batches),
            "direct_pass_chapters": list(direct_pass),
            "unresolved_chapters": sorted(set(unresolved_all)),
            "review_scope": None if scope_set is None else sorted(scope_set),
            "carried_chapters": sorted(set(carried)),
        }
        (d / "joint_review.json").write_text(json.dumps(joint, ensure_ascii=False, indent=2), encoding="utf-8")
        return joint

    def _run_audit_repair_candidates(self, batch_id, model):
        try:
            bid, plan = self._load_repair_plan(batch_id)
            items = [x for x in plan.get("items", []) if x.get("auto_candidate")]
            item_by_ch = {
                int(x.get("chapter_no") or 0): x
                for x in items
                if int(x.get("chapter_no") or 0) > 0
            }

            with self.repair_lock:
                self.repair_status.update({
                    "running": True,
                    "batch_id": bid,
                    "model": model,
                    "stage": "候选生成",
                    "stage_label": "准备生成修复候选",
                    "started_at": time.time(),
                    "last_error": "",
                    "item_index": 0,
                    "item_total": len(items),
                    "candidate_ready": 0,
                    "candidate_blocked": 0,
                    "joint_safe": None,
                    "committed": False,
                    "rolled_back": False,
                    "last_commit_mode": "",
                    "last_commit_forced": False,
                    "last_commit_manual": False,
                    "last_commit_failed_gates": [],
                    "last_commit_chapters": [],
                    "last_commit_force_reason": "",
                })

            ready = blocked = 0
            for idx, item in enumerate(items, 1):
                self._repair_check_cancel()
                n = int(item["chapter_no"])
                with self.repair_lock:
                    self.repair_status["item_index"] = idx
                is_rewrite = self._repair_widest_class(
                    item.get("repair_class")
                ) in self._REPAIR_REWRITE_CLASSES
                self._repair_set_stage(
                    "定向重写候选" if is_rewrite else "小修候选",
                    f"{idx}/{len(items)} · 第{n}章 · " + (
                        "定向重写 + 自动验收" if is_rewrite
                        else "精确局部补丁 + 自动验收"
                    ),
                )
                meta = self._generate_candidate_for_item(bid, item, model)
                if meta.get("safe"):
                    ready += 1
                else:
                    blocked += 1
                with self.repair_lock:
                    self.repair_status["candidate_ready"] = ready
                    self.repair_status["candidate_blocked"] = blocked

            self._repair_check_cancel()
            self._repair_set_stage(
                "联合复核",
                f"{len(items)} 个修复候选 · Pro 联合复核",
            )
            joint = self._joint_repair_review(bid, plan, model)

            # Joint review is also self-healing.  If a chapter is individually
            # safe but is blocked only because of cross-chapter interaction,
            # regenerate it with the joint findings and re-review automatically.
            # A re-review now only covers the clusters that changed, so a round
            # costs a fraction of the first pass and the limit no longer has to be
            # one.  Three rounds is where a cross-chapter conflict that is going
            # to resolve has resolved; past that the candidates tend to oscillate.
            max_joint_rescue_rounds = 3
            previous_joint_signature = ""
            for rescue_round in range(1, max_joint_rescue_rounds + 1):
                self._repair_check_cancel()
                blocked_chapters = [
                    int(x) for x in (joint.get("blocked_chapters") or [])
                    if int(x) in item_by_ch
                ]
                if not blocked_chapters:
                    break

                # Stop only when both the blocked set and its chapter-specific
                # guidance are unchanged.  Comparing chapter numbers alone used
                # to discard useful new feedback after the first rescue round.
                joint_signature = json.dumps({
                    "blocked": sorted(blocked_chapters),
                    "findings": {
                        str(n): self._repair_findings_for_chapter(joint, n)
                        for n in sorted(blocked_chapters)
                    },
                }, ensure_ascii=False, sort_keys=True)
                if previous_joint_signature == joint_signature and rescue_round > 1:
                    self.log(
                        f"跨章节自动自愈在第 {rescue_round - 1} 轮后不再收敛，"
                        f"剩余 {len(blocked_chapters)} 章转人工。"
                    )
                    break
                previous_joint_signature = joint_signature

                self._repair_set_stage(
                    "自动自愈",
                    f"联合复核阻止 {len(blocked_chapters)} 章 · 自动修正第 {rescue_round}/{max_joint_rescue_rounds} 轮",
                )

                regenerated = []
                for n in blocked_chapters:
                    self._repair_check_cancel()
                    item = item_by_ch.get(n)
                    if not item:
                        continue
                    # Each chapter gets the findings from the review chunks it was
                    # actually part of.  Broadcasting the batch-wide text made the
                    # prompt argue about unrelated chapters.
                    own = self._repair_findings_for_chapter(joint, n)
                    findings = "；".join(own) if own else (
                        "联合复核认为当前补丁组合存在跨章节连续性风险。"
                    )
                    # The instruction has to match the channel: telling a
                    # rewriter to adjust "exact patch" produces nothing usable.
                    how = (
                        "请只重写仍然造成跨章冲突的段落，其余段落保持逐字不变"
                        if self._repair_widest_class(item.get("repair_class"))
                        in self._REPAIR_REWRITE_CLASSES
                        else "请只调整完成原审计要求所必需的 exact patch"
                    )
                    feedback = (
                        f"这是第 {rescue_round} 轮跨章节自动自愈。"
                        f"联合复核针对本章的反馈：{findings}"
                        f"{how}，"
                        "消除跨章冲突，绝对不要扩大修改范围。"
                    )
                    self._generate_candidate_for_item(
                        bid, item, model, extra_feedback=feedback
                    )
                    regenerated.append(n)

                scope = self._repair_rereview_scope(joint, regenerated)
                scope_label = "整批" if scope is None else f"{len(scope)} 章关联聚类"
                self._repair_set_stage(
                    "联合复核",
                    f"自动自愈第 {rescue_round} 轮后重新进行{scope_label} Pro 联合复核",
                )
                joint = self._joint_repair_review(
                    bid, plan, model, scope=scope, previous=joint,
                )

            # Re-count from the final metadata after all automatic self-healing.
            ready = blocked = 0
            for item in items:
                n = int(item.get("chapter_no") or 0)
                mp = self._repair_batch_dir(bid) / "candidates" / f"{n:04d}.json"
                if not mp.exists():
                    blocked += 1
                    continue
                meta = json.loads(mp.read_text(encoding="utf-8"))
                if meta.get("safe"):
                    ready += 1
                else:
                    blocked += 1

            final_blocked = len(joint.get("blocked_chapters") or [])
            with self.repair_lock:
                self.repair_status["candidate_ready"] = ready
                self.repair_status["candidate_blocked"] = blocked
                self.repair_status["joint_safe"] = bool(joint.get("safe_to_commit"))
                self.repair_status["stage"] = "完成"
                self.repair_status["stage_label"] = (
                    f"自动修复完成：联合批准 {len(joint.get('approved_chapters') or [])} 章；"
                    f"自动未收敛 {final_blocked} 章"
                )

            self.log(
                f"审计自动修复完成：{bid}；"
                f"联合批准 {len(joint.get('approved_chapters') or [])} 章，"
                f"自动未收敛 {final_blocked} 章。"
            )
        except ProviderCancelledError:
            with self.repair_lock:
                self.repair_status["stage"] = "已停止"
                self.repair_status["stage_label"] = "用户停止自动修复"
            self.log("审计自动修复已停止。")
        except Exception as e:
            with self.repair_lock:
                self.repair_status["last_error"] = str(e)
                self.repair_status["stage"] = "错误"
                self.repair_status["stage_label"] = str(e)
            self.log(f"审计自动修复失败：{e}")
        finally:
            with self.repair_lock:
                self.repair_status["running"] = False
            self.repair_stop_event.clear()


    def _run_single_pro_retry(self, batch_id, chapter_no, mode="retry"):
        """Retry one failed chapter with DeepSeek V4 Pro only.

        This is intentionally isolated from batch repair. A difficult chapter
        should not consume the whole batch's AFP budget.
        """
        try:
            bid, plan = self._load_repair_plan(batch_id)
            chapter_no = int(chapter_no)
            item = next(
                (
                    x for x in plan.get("items", [])
                    if int(x.get("chapter_no") or 0) == chapter_no
                ),
                None,
            )
            if not item:
                raise RuntimeError(f"找不到第{chapter_no}章修复任务")

            self._repair_set_stage(
                "Pro单章重试",
                f"DeepSeek V4 Pro 单章重试：第{chapter_no}章",
            )

            is_rewrite = self._repair_widest_class(
                item.get("repair_class")
            ) in self._REPAIR_REWRITE_CLASSES
            feedback = (
                "该章节已经经过自动修复但未收敛。请重新分析失败原因，"
                + (
                    "只重写仍然导致失败的目标段落，保持其他段落逐字不变。"
                    if is_rewrite
                    else "只生成满足审计要求的最小 exact patch。"
                )
                + "不要扩大修改范围。"
            )

            d = self._repair_batch_dir(bid)
            previous_meta_path = d / "candidates" / f"{chapter_no:04d}.json"
            if previous_meta_path.exists():
                try:
                    previous_meta = json.loads(previous_meta_path.read_text(encoding="utf-8"))
                    previous_findings = (previous_meta.get("review") or {}).get("findings") or []
                    previous_error = str(previous_meta.get("last_apply_error") or "").strip()
                    if previous_findings:
                        feedback += "上一版候选的验收反馈：" + "；".join(
                            str(x) for x in previous_findings if str(x).strip()
                        ) + "。"
                    if previous_error:
                        feedback += f"上一版补丁应用错误：{previous_error}。"
                except Exception:
                    pass

            joint_path = d / "joint_review.json"
            previous_joint = {}
            if joint_path.exists():
                try:
                    previous_joint = json.loads(joint_path.read_text(encoding="utf-8"))
                    if chapter_no in {int(x) for x in previous_joint.get("blocked_chapters") or []}:
                        # Only the findings from review chunks this chapter took
                        # part in.  Batch-wide findings used to be pasted into
                        # every retry, which buried the real instruction under
                        # complaints about unrelated chapters.
                        joint_findings = self._repair_findings_for_chapter(
                            previous_joint, chapter_no,
                        )
                        if joint_findings:
                            feedback += "上一轮联合复核针对本章的反馈：" + "；".join(
                                joint_findings
                            ) + "。"
                except Exception:
                    previous_joint = {}

            if mode == "deep":
                feedback += (
                    "这是深度修复模式，可重新检查关联章节上下文，"
                    "重点解决上一轮独立验收或联合复核指出的问题；"
                    "但仍禁止改变既有 Canon 状态。"
                )

            meta = self._generate_candidate_for_item(
                bid,
                item,
                "deepseek-v4-pro",
                extra_feedback=feedback,
            )

            # Only the clusters containing the regenerated chapter can have their
            # verdict invalidated, so the rest of the batch keeps its result
            # instead of being re-reviewed at full price.
            scope = self._repair_rereview_scope(previous_joint, [chapter_no])
            if scope is None:
                scope_label = "整批"
            else:
                scope_label = f"{len(scope)} 章关联聚类"
            self._repair_set_stage(
                "联合复核",
                f"第{chapter_no}章候选已更新，正在重新执行{scope_label}联合复核",
            )
            joint = self._joint_repair_review(
                bid, plan, "deepseek-v4-pro",
                scope=scope, previous=previous_joint,
            )

            approved = {int(x) for x in joint.get("approved_chapters") or []}
            ready = blocked = 0
            for plan_item in plan.get("items", []):
                if not plan_item.get("auto_candidate"):
                    continue
                n = int(plan_item.get("chapter_no") or 0)
                meta_path = d / "candidates" / f"{n:04d}.json"
                try:
                    row_meta = json.loads(meta_path.read_text(encoding="utf-8"))
                    if row_meta.get("safe"):
                        ready += 1
                    else:
                        blocked += 1
                except Exception:
                    blocked += 1

            passed = bool(meta.get("safe")) and chapter_no in approved
            with self.repair_lock:
                self.repair_status.update({
                    "candidate_ready": ready,
                    "candidate_blocked": blocked,
                    "joint_safe": bool(joint.get("safe_to_commit")),
                    "stage": "Pro单章重试完成",
                    "stage_label": f"第{chapter_no}章 {'已通过联合复核' if passed else '仍未通过联合复核'}",
                })

            return {
                "ok": True,
                "chapter_no": chapter_no,
                "mode": mode,
                "meta": meta,
                "joint_review": joint,
            }

        except ProviderCancelledError:
            with self.repair_lock:
                self.repair_status["stage"] = "已停止"
                self.repair_status["stage_label"] = f"第{chapter_no}章 Pro 单章修复已停止"
            self.log(f"第{chapter_no}章 Pro 单章修复已停止。")
        except Exception as e:
            with self.repair_lock:
                self.repair_status["last_error"] = str(e)
                self.repair_status["stage"] = "错误"
                self.repair_status["stage_label"] = f"第{chapter_no}章 Pro 单章修复失败：{e}"
            self.log(f"Pro单章重试失败：{e}")
        finally:
            with self.repair_lock:
                self.repair_status["running"] = False
            self.repair_stop_event.clear()

    def start_single_pro_retry(self, batch_id="", chapter_no=0, mode="retry"):
        if not batch_id:
            raise RuntimeError("缺少 batch_id")
        chapter_no = int(chapter_no or 0)
        if chapter_no <= 0:
            raise RuntimeError("章节号错误")

        mode = str(mode or "retry").strip().lower()
        if mode not in {"retry", "deep"}:
            raise RuntimeError("单章修复模式错误")
        if self.status.get("running"):
            raise RuntimeError("Canon 正在运行")
        if self.audit_snapshot().get("running"):
            raise RuntimeError("剧情审计正在运行")
        try:
            if self.dlc_snapshot().get("running"):
                raise RuntimeError("DLC 正在运行")
        except AttributeError:
            pass

        bid, _plan = self._load_repair_plan(batch_id)
        self._validate_repair_batch_audit_snapshot(bid, _plan)
        with self.repair_lock:
            if self.repair_thread and self.repair_thread.is_alive():
                raise RuntimeError("审计修复已经在运行")
            self.repair_stop_event.clear()
            self.repair_status.update({
                "running": True,
                "batch_id": bid,
                "model": "deepseek-v4-pro",
                "stage": "Pro单章重试",
                "stage_label": f"正在启动第{chapter_no}章 {'深度修复' if mode == 'deep' else '重新生成'}",
                "started_at": time.time(),
                "last_error": "",
                "item_index": 1,
                "item_total": 1,
                "joint_safe": None,
                "committed": False,
                "rolled_back": False,
                "last_commit_mode": "",
                "last_commit_forced": False,
                "last_commit_manual": False,
                "last_commit_failed_gates": [],
                "last_commit_chapters": [],
                "last_commit_force_reason": "",
                "prompt_tokens": 0,
                "cache_hit_tokens": 0,
                "completion_tokens": 0,
                "reasoning_tokens": 0,
                "cost_cny": 0.0,
                "afp": 0.0,
                "request_count": 0,
            })
            self.repair_thread = threading.Thread(
                target=self._run_single_pro_retry,
                args=(bid, chapter_no, mode),
                name=f"NovelAgentProRetry-{chapter_no}",
                daemon=True,
            )
            self.repair_thread.start()

        return {
            "ok": True,
            "message": f"已启动第{chapter_no}章 Pro {'深度修复' if mode == 'deep' else '重新生成'}",
            "chapter_no": chapter_no,
            "mode": mode,
            "status": self.repair_snapshot(),
        }


    def start_audit_repair_candidates(self, batch_id="", model="deepseek-v4-pro"):
        if self.status.get("running"):
            raise RuntimeError("Canon 正在运行")
        if self.audit_snapshot().get("running"):
            raise RuntimeError("剧情审计正在运行")
        try:
            if self.dlc_snapshot().get("running"):
                raise RuntimeError("DLC 正在运行")
        except AttributeError:
            pass
        model = str(model or "deepseek-v4-pro")
        if model not in {"deepseek-v4-pro", "deepseek-v4-flash"}:
            model = "deepseek-v4-pro"
        bid, plan = self._load_repair_plan(batch_id)
        self._validate_repair_batch_audit_snapshot(bid, plan)
        if not any(x.get("auto_candidate") for x in plan.get("items", [])):
            raise RuntimeError(
                "该计划没有可生成的正文候选项；"
                "请检查是否只有 DEFER_FUTURE 或章节文件缺失项"
            )
        with self.repair_lock:
            if self.repair_thread and self.repair_thread.is_alive():
                raise RuntimeError("审计修复已经在运行")
            self.repair_stop_event.clear()
            self.repair_thread = threading.Thread(
                target=self._run_audit_repair_candidates,
                args=(bid, model),
                name="NovelAgentAuditRepair", daemon=True,
            )
            self.repair_thread.start()
        return self.repair_snapshot()

    def stop_audit_repair(self):
        self.repair_stop_event.set()
        try:
            self.repair_router.cancel_current()
        except Exception:
            pass
        return self.repair_snapshot()

    def audit_repair_candidate_detail(self, chapter_no, batch_id=""):
        n = int(chapter_no)
        bid, plan = self._load_repair_plan(batch_id)
        self._validate_repair_batch_audit_snapshot(bid, plan)
        d = self._repair_batch_dir(bid)
        cp = d / "candidates" / f"{n:04d}.md"
        mp = d / "candidates" / f"{n:04d}.json"
        op = self.root / "chapters" / f"{n:04d}.md"
        if not (cp.exists() and mp.exists() and op.exists()):
            raise FileNotFoundError(f"第 {n} 章审计修复候选不存在")
        manifest = {}
        manifest_path = d / "commit_manifest.json"
        if manifest_path.exists():
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except Exception:
                manifest = {}
        committed = {
            int(row.get("chapter_no"))
            for row in self._repair_manifest_active_rows(manifest)
        }
        return {
            "ok": True, "batch_id": bid, "chapter_no": n,
            "original": op.read_text(encoding="utf-8"),
            "candidate": cp.read_text(encoding="utf-8"),
            "meta": json.loads(mp.read_text(encoding="utf-8")),
            "committed": n in committed,
            "force_available": n not in committed,
        }

    @staticmethod
    def _normalize_repair_commit_chapters(chapters):
        """Normalize a user-selected chapter list without silently dropping rows."""
        if chapters is None:
            return None
        if isinstance(chapters, (str, bytes, int)):
            chapters = [chapters]
        if not isinstance(chapters, (list, tuple, set)):
            raise RuntimeError("chapters 必须是章节号数组")
        out = []
        for raw in chapters:
            if isinstance(raw, bool):
                raise RuntimeError("章节号不能是布尔值")
            try:
                n = int(raw)
            except (TypeError, ValueError) as exc:
                raise RuntimeError(f"无效章节号：{raw}") from exc
            if n <= 0:
                raise RuntimeError(f"无效章节号：{raw}")
            if n not in out:
                out.append(n)
        return sorted(out)

    @staticmethod
    def _repair_manifest_active_rows(manifest):
        """Return committed rows that still own the on-disk chapter version.

        Schema-v2 rows carry their own rollback marker. Older manifests only
        recorded rollback at the top level, so both representations are read.
        Keeping historical rows in the manifest gives every submission an audit
        trail while this filtered view prevents duplicate publication.
        """
        manifest = manifest if isinstance(manifest, dict) else {}
        rows = [row for row in (manifest.get("chapters") or []) if isinstance(row, dict)]
        if not rows:
            return []

        legacy_inactive = set()
        for value in manifest.get("rolled_back_chapters") or []:
            try:
                legacy_inactive.add(int(value))
            except (TypeError, ValueError):
                continue
        for value in manifest.get("rollback_already_original") or []:
            try:
                n = value.get("chapter_no") if isinstance(value, dict) else value
                legacy_inactive.add(int(n))
            except (TypeError, ValueError):
                continue
        legacy_full_rollback = bool(manifest.get("rolled_back_at")) and not bool(
            manifest.get("rollback_partial")
        )

        active = []
        for row in rows:
            try:
                n = int(row.get("chapter_no"))
            except (TypeError, ValueError):
                continue
            if row.get("rolled_back_at") or row.get("rollback_status") in {
                "restored", "already_original",
            }:
                continue
            # Row-local submission metadata identifies the new schema. Legacy
            # top-level rollback fields only apply to rows without that marker.
            if not row.get("submission_id") and (
                legacy_full_rollback or n in legacy_inactive
            ):
                continue
            active.append(row)
        return active

    @staticmethod
    def _repair_force_confirmation_valid(confirm):
        value = str(confirm or "").strip()
        return value.upper() in {"FORCE", "CONFIRM FORCE"} or value == "强制提交"

    @staticmethod
    def _repair_candidate_is_safe(meta):
        value = (meta or {}).get("safe")
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)) and value in (0, 1):
            return bool(value)
        return isinstance(value, str) and value.strip().lower() in {"true", "1", "yes", "y"}

    @staticmethod
    def _repair_commit_gate_reasons(item, meta, chapter_no,
                                    approved=None, blocked=None):
        """Return every gate a manual/forced commit is overriding.

        This is deliberately descriptive rather than a boolean helper: the
        exact list is persisted in the commit manifest so a later audit can tell
        why a user chose to bypass the normal route.
        """
        approved = set(approved or [])
        blocked = set(blocked or [])
        reasons = []
        n = int(chapter_no)
        if n not in approved:
            reasons.append("联合复核未批准")
        if n in blocked:
            reasons.append("跨章节联合复核阻止")
        reasons.extend(str(x).strip() for x in (item or {}).get("evidence_gate_reasons") or []
                       if str(x).strip())
        safe_value = (meta or {}).get("safe")
        safe_candidate = (
            safe_value is True
            or safe_value == 1
            or (isinstance(safe_value, str) and safe_value.strip().lower() in {"true", "1", "yes", "y"})
        )
        if not safe_candidate:
            reasons.append("候选独立验收未通过")
        review = (meta or {}).get("review") or {}
        if isinstance(review, dict):
            if str(review.get("severity") or "").upper() not in {"", "PASS"}:
                reasons.append(f"候选 Review severity={review.get('severity')}")
            if review.get("needs_revision"):
                reasons.append("候选 Review 要求返修")
            for key, label in (
                ("requested_fix_applied", "未确认审计要求已应用"),
                ("safe_to_batch_commit", "未获得批量提交安全许可"),
            ):
                if key in review and review.get(key) is False:
                    reasons.append(label)
            for key, label in (
                ("unrelated_changes", "检测到无关改动"),
                ("canon_end_state_changed", "Canon 章末状态发生变化"),
                ("downstream_conflict", "检测到下游连续性冲突"),
            ):
                if review.get(key) is True:
                    reasons.append(label)
        verify = str((meta or {}).get("verify_verdict") or "").strip().lower()
        if verify and verify not in {"pass", "passed"}:
            reasons.append(f"程序校验={verify}")
        return list(dict.fromkeys(reasons))

    def commit_audit_repair(self, batch_id="", chapters=None, *,
                            manual=False, force=False, confirm="",
                            force_reason="", mode="", forced=False,
                            manual_confirmed=False):
        with self.repair_lock:
            if self.repair_status.get("running"):
                raise RuntimeError("审计修复运行中不能提交")
        if self.status.get("running") or self.audit_snapshot().get("running"):
            raise RuntimeError("Canon/剧情审计运行中不能提交修复")
        bid, plan = self._load_repair_plan(batch_id)
        self._validate_repair_batch_audit_snapshot(bid, plan)
        d = self._repair_batch_dir(bid)
        existing_manifest_path = d / "commit_manifest.json"
        existing_manifest = {}
        if existing_manifest_path.exists():
            try:
                existing_manifest = json.loads(
                    existing_manifest_path.read_text(encoding="utf-8")
                )
            except Exception as exc:
                raise RuntimeError(f"已有提交清单无法读取，已阻止重复提交：{exc}") from exc
        active_existing_rows = self._repair_manifest_active_rows(existing_manifest)
        active_existing = {
            int(row.get("chapter_no")): row for row in active_existing_rows
            if int(row.get("chapter_no") or 0) > 0
        }
        joint_path = d / "joint_review.json"
        if not joint_path.exists():
            raise RuntimeError("尚未完成跨章节联合复核")
        joint = json.loads(joint_path.read_text(encoding="utf-8"))
        approved = set()
        blocked = set()
        for value in joint.get("approved_chapters") or []:
            try:
                approved.add(int(value))
            except (TypeError, ValueError):
                continue
        for value in joint.get("blocked_chapters") or []:
            try:
                blocked.add(int(value))
            except (TypeError, ValueError):
                continue

        mode_text = str(mode or "").strip().lower()
        manual = bool(manual) or bool(manual_confirmed) or mode_text in {"manual", "human", "review"}
        force = bool(force) or bool(forced) or mode_text in {"force", "forced"}
        if manual and force:
            raise RuntimeError("人工确认与强制提交不能同时使用")
        requested_list = self._normalize_repair_commit_chapters(chapters)
        if force:
            if requested_list is None or len(requested_list) != 1:
                raise RuntimeError("强制提交必须逐章选择且只能选择一章")
            if not self._repair_force_confirmation_valid(confirm):
                raise RuntimeError("强制提交需要明确确认：confirm 必须为 FORCE 或 强制提交")
            if not str(force_reason or "").strip():
                raise RuntimeError("强制提交必须填写 force_reason")
            selected = requested_list
            commit_mode = "forced"
        elif manual:
            if requested_list is None or not requested_list:
                raise RuntimeError("人工确认提交必须明确选择章节")
            selected = requested_list
            commit_mode = "manual"
        else:
            requested = (
                approved - set(active_existing)
                if requested_list is None else set(requested_list)
            )
            selected = sorted(approved & requested)
            commit_mode = "automatic"
            if not selected:
                raise RuntimeError("没有尚未提交且通过联合复核的章节")

        duplicates = sorted(set(selected) & set(active_existing))
        if duplicates:
            preview = "、".join(str(n) for n in duplicates)
            raise RuntimeError(f"第 {preview} 章已经由本批次提交，不能重复提交")

        plan_by_ch = {
            int(x.get("chapter_no") or 0): x for x in plan.get("items", [])
            if int(x.get("chapter_no") or 0) > 0
        }

        prepared = []
        failed_gates = {}
        for n in selected:
            item = plan_by_ch.get(n) or {}
            final = self.root / "chapters" / f"{n:04d}.md"
            cp = d / "candidates" / f"{n:04d}.md"
            mp = d / "candidates" / f"{n:04d}.json"
            if not (final.exists() and cp.exists() and mp.exists()):
                raise RuntimeError(f"第 {n} 章提交文件不完整")
            meta = json.loads(mp.read_text(encoding="utf-8"))
            if not meta:
                raise RuntimeError(f"第 {n} 章候选元数据为空")

            gates = self._repair_commit_gate_reasons(
                item, meta, n, approved=approved, blocked=blocked,
            )
            failed_gates[n] = list(gates)
            if not force and not manual and not item.get("auto_commit_allowed"):
                raise RuntimeError(
                    f"第 {n} 章被标记为不可自动提交：{'；'.join(gates) or '未知原因'}"
                )
            if manual and not force and not self._repair_candidate_is_safe(meta):
                raise RuntimeError(
                    f"第 {n} 章候选独立验收未通过；人工确认不能替代候选质量门，"
                    "如确需写入请使用逐章强制提交"
                )
            current = final.read_text(encoding="utf-8")
            if self._repair_hash(current) != meta.get("original_sha256"):
                raise RuntimeError(f"第 {n} 章在候选生成后已经被修改；为防覆盖，整批提交已中止")
            candidate = cp.read_text(encoding="utf-8")
            expected_candidate_hash = str(meta.get("candidate_sha256") or "").strip()
            if expected_candidate_hash and self._repair_hash(candidate) != expected_candidate_hash:
                raise RuntimeError(f"第 {n} 章候选文件在生成后已经被修改；为防误写，整批提交已中止")
            if not candidate.strip():
                raise RuntimeError(f"第 {n} 章候选正文为空")
            task_card = self.chapter_task_card(n)
            plan_text = self.read(f"plans/{n:04d}.md") or f"按审计修复计划进行最小纠偏：{meta.get('instruction', '')}"
            final_review_error = ""
            try:
                final_review = self.review_chapter(
                    n, plan_text, candidate, task_card,
                    final_gate=True, prior_review=meta.get("review") or {},
                )
            except Exception as exc:
                final_review_error = str(exc)
                final_review = {
                    "severity": "ERROR",
                    "needs_revision": True,
                    "safe_to_batch_commit": False,
                    "forced_review_error": final_review_error,
                }
                failed_gates[n].append(f"最终质量门请求失败：{final_review_error}")
            quality_failed = (
                str(final_review.get("severity", "PASS")).upper() != "PASS"
                or bool(final_review.get("needs_revision"))
                or final_review.get("safe_to_batch_commit") is False
            )
            if quality_failed:
                failed_gates[n].append("最终 Canon 质量门未通过")
                if not force:
                    raise RuntimeError(
                        f"第 {n} 章审计修复候选未通过新的 Canon 最终质量门；"
                        "如确认接受风险请使用逐章强制提交"
                    )
            sync_error = ""
            try:
                summary, memory_records, handoff, handoff_error = self.summarize_and_extract_memories(n, candidate)
                sync_error = str(handoff_error or "")
            except Exception as exc:
                sync_error = str(exc)
                failed_gates[n].append(f"Summary/Memory/Handoff 重建失败：{sync_error}")
                raise RuntimeError(
                    f"第 {n} 章 Summary/Memory/Handoff 重建失败，未写入正文：{sync_error}"
                ) from exc
            if (
                sync_error or not isinstance(handoff, dict)
                or handoff.get("status") != "complete"
                or handoff.get("structured_complete") is not True
                or not handoff.get("scene_signatures")
            ):
                failed_gates[n].append("handoff 未达到 structured_complete")
                detail = sync_error or "handoff 未达到 structured_complete"
                raise RuntimeError(
                    f"第 {n} 章审计修复候选 handoff 提取失败，未写入正文：{detail}"
                )
            prepared.append((n, final, current, candidate, meta, plan_text, final_review,
                             summary, memory_records, handoff, failed_gates[n], sync_error))

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        submission_id = f"{stamp}_{commit_mode}"
        submission_committed_at = datetime.now().isoformat(timespec="seconds")
        archive = self.root / "archive" / f"audit_fix_{bid}_{stamp}"
        archive.mkdir(parents=True, exist_ok=False)
        archive_rel = str(archive.relative_to(self.root)).replace("\\", "/")
        manifest_rows = []

        # Backup all originals first, together with everything derived from them.
        sidecars = {}
        for n, final, current, candidate, meta, *_rest in prepared:
            shutil.copy2(final, archive / final.name)
            sidecars[n] = self._repair_archive_sidecars(n, archive)
        project_files = self._repair_archive_project_files(archive)
        for project_row in project_files:
            project_row["submission_id"] = submission_id
            project_row["archive_dir"] = archive_rel

        # Then commit all selected candidates.
        try:
            for n, final, current, candidate, meta, plan_text, final_review, summary, memory_records, handoff, gates, sync_error in prepared:
                live_current = final.read_text(encoding="utf-8")
                if self._repair_hash(live_current) != self._repair_hash(current):
                    raise RuntimeError(f"第 {n} 章在备份后发生变化；为防覆盖，整批提交已中止")
                self._commit_canon_bundle(
                    n, plan=plan_text, draft=candidate, final_review=final_review,
                    final=candidate, summary=summary, memories=memory_records,
                    handoff=handoff, generation_seconds=0, revision_seconds=0,
                    honor_stop=False,
                )
                manifest_rows.append({
                    "chapter_no": n,
                    "submission_id": submission_id,
                    "committed_at": submission_committed_at,
                    "archive_dir": archive_rel,
                    "old_sha256": self._repair_hash(current),
                    "new_sha256": self._repair_hash(candidate),
                    "archive_file": str((archive / final.name).relative_to(self.root)).replace("\\", "/"),
                    "repair_class": meta.get("repair_class"),
                    "sidecars": sidecars.get(n) or [],
                    "canon_bundle_rebuilt": True,
                    "forced": bool(force),
                    "manual": bool(manual and not force),
                    "commit_mode": commit_mode,
                    "failed_gates": list(dict.fromkeys(gates)),
                    "force_reason": str(force_reason or "") if force else "",
                    "memory_sync_error": sync_error or "",
                })
        except Exception as commit_exc:
            rollback_errors = []
            rollback_restored = []
            rollback_failed = set()
            for row in reversed(manifest_rows):
                try:
                    bundle = self._archived_canon_bundle(archive_rel, int(row["chapter_no"]))
                    self._commit_canon_bundle(
                        int(row["chapter_no"]), plan=bundle["plan"], draft=bundle["draft"],
                        final_review=bundle["review"], final=bundle["final"],
                        summary=bundle["summary"], memories=bundle["memories"],
                        handoff=bundle["handoff"],
                        generation_seconds=bundle["generation_seconds"],
                        revision_seconds=bundle["revision_seconds"], honor_stop=False,
                    )
                    rollback_restored.append(int(row["chapter_no"]))
                except Exception as rollback_exc:
                    rollback_failed.add(int(row["chapter_no"]))
                    rollback_errors.append(f"第{row['chapter_no']}章：{rollback_exc}")
            failure_event = {
                "submission_id": submission_id,
                "attempted_at": submission_committed_at,
                "archive_dir": archive_rel,
                "chapters": list(selected),
                "commit_mode": commit_mode,
                "forced": bool(force),
                "manual": bool(manual and not force),
                "force_reason": str(force_reason or "") if force else "",
                "commit_failed": str(commit_exc),
                "rollback_restored_chapters": rollback_restored,
                "rollback_errors": rollback_errors,
            }
            failure_manifest = dict(existing_manifest)
            failure_manifest.setdefault("schema_version", 2)
            failure_manifest["batch_id"] = bid
            failure_manifest.setdefault("chapters", list(existing_manifest.get("chapters") or []))
            failure_manifest.setdefault("project_files", list(existing_manifest.get("project_files") or []))
            failure_manifest.setdefault("submissions", list(existing_manifest.get("submissions") or []))
            failure_manifest.setdefault("failed_submissions", list(existing_manifest.get("failed_submissions") or []))
            failure_manifest["failed_submissions"].append(failure_event)
            if rollback_errors:
                surviving = [
                    {**row, "commit_failed": str(commit_exc), "rollback_incomplete": True}
                    for row in manifest_rows
                    if int(row.get("chapter_no") or 0) in rollback_failed
                ]
                failure_manifest["chapters"].extend(surviving)
                failure_manifest["project_files"].extend(project_files)
                failure_manifest["submissions"].append({
                    **failure_event,
                    "status": "rollback_incomplete",
                    "chapters": sorted(rollback_failed),
                    "project_files": project_files,
                })
                failure_manifest["committed"] = True
                failure_manifest["updated_at"] = datetime.now().isoformat(timespec="seconds")
                existing_manifest_path.write_text(
                    json.dumps(failure_manifest, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                raise CanonCommitError(
                    f"审计修复批次提交失败且自动回滚不完整：{commit_exc}；{'；'.join(rollback_errors)}"
                ) from commit_exc
            failure_manifest["committed"] = bool(
                self._repair_manifest_active_rows(failure_manifest)
            )
            failure_manifest["updated_at"] = datetime.now().isoformat(timespec="seconds")
            existing_manifest_path.write_text(
                json.dumps(failure_manifest, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            raise CanonCommitError(f"审计修复批次提交失败，已自动恢复已提交章节：{commit_exc}") from commit_exc

        # The derived layers were rebuilt from the same candidate before the
        # publish transaction. Any recorded failure here is diagnostic only.
        sync_failed = [
            {"chapter_no": n, "error": err}
            for n, *rest in prepared
            for err in [str(rest[-1] or "").strip()]
            if err
        ]
        resummarize = {
            "rebuilt": selected,
            "failed": sync_failed,
            "skipped": [],
            "note": (
                "已从同一候选重建 Summary/Memory/Handoff；同步失败会阻止写入正文"
            ),
        }

        submission_failed_gates = {
            str(n): list(dict.fromkeys(failed_gates.get(n) or []))
            for n in selected if failed_gates.get(n)
        }
        submission = {
            "submission_id": submission_id,
            "committed_at": submission_committed_at,
            "archive_dir": archive_rel,
            "chapters": list(selected),
            "project_files": project_files,
            "resummarized": resummarize,
            "status": "committed",
            "forced": bool(force),
            "manual": bool(manual and not force),
            "commit_mode": commit_mode,
            "force_reason": str(force_reason or "") if force else "",
            "failed_gates": submission_failed_gates,
        }
        old_rows = list(existing_manifest.get("chapters") or [])
        old_submissions = list(existing_manifest.get("submissions") or [])
        old_project_files = list(existing_manifest.get("project_files") or [])
        old_resummarized = existing_manifest.get("resummarized") or {}
        rebuilt_all = list(dict.fromkeys(
            [int(x) for x in (old_resummarized.get("rebuilt") or [])]
            + list(selected)
        ))
        failed_all = list(old_resummarized.get("failed") or []) + sync_failed
        skipped_all = list(old_resummarized.get("skipped") or [])
        all_rows = old_rows + manifest_rows
        active_after = self._repair_manifest_active_rows({
            **existing_manifest, "chapters": all_rows,
        })
        all_modes = {
            str(row.get("commit_mode") or "automatic")
            for row in active_after
        }
        manifest = {
            **existing_manifest,
            "schema_version": 2,
            "batch_id": bid,
            "committed_at": str(existing_manifest.get("committed_at") or submission_committed_at),
            "updated_at": submission_committed_at,
            # Kept for legacy readers. New rollback reads row.archive_dir.
            "archive_dir": str(existing_manifest.get("archive_dir") or archive_rel),
            "chapters": all_rows,
            "submissions": old_submissions + [submission],
            "project_files": old_project_files + project_files,
            "resummarized": {
                "rebuilt": rebuilt_all,
                "failed": failed_all,
                "skipped": skipped_all,
                "note": "各次提交均从对应候选同步重建 Summary/Memory/Handoff",
            },
            "committed": bool(active_after),
            "forced": any(bool(row.get("forced")) for row in active_after),
            "manual": any(bool(row.get("manual")) for row in active_after),
            "commit_mode": next(iter(all_modes)) if len(all_modes) == 1 else "mixed",
            "force_reason": "",
            "failed_gates": {
                str(row.get("chapter_no")): list(row.get("failed_gates") or [])
                for row in active_after if row.get("failed_gates")
            },
            "note": (
                "同一批次可分次提交未提交章节；每次提交均独立归档旧正文、衍生文件和记忆库，"
                "并从同一候选统一重建 Summary、Memory、Handoff 与状态快照。"
            ),
        }
        # These fields describe only the most recent rollback operation. A new
        # successful submission makes them stale; durable history remains on
        # each row and in rollback_history.
        for key in (
            "rolled_back_at", "rolled_back_chapters", "rollback_skipped",
            "rollback_already_original", "rollback_partial",
            "rollback_restored_sidecars",
        ):
            manifest.pop(key, None)
        (d / "commit_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        with self.repair_lock:
            self.repair_status["committed"] = True
            self.repair_status["rolled_back"] = False
            self.repair_status["last_commit_mode"] = commit_mode
            self.repair_status["last_commit_forced"] = bool(force)
            self.repair_status["last_commit_manual"] = bool(manual and not force)
            self.repair_status["last_commit_failed_gates"] = list(dict.fromkeys(
                gate for values in failed_gates.values() for gate in values
            ))
            self.repair_status["last_commit_chapters"] = list(selected)
            self.repair_status["last_commit_force_reason"] = str(force_reason or "") if force else ""
            self.repair_status["stage"] = "已提交"
            label = f"已事务式提交 {len(manifest_rows)} 章"
            if force:
                label += "（逐章强制）"
            elif manual:
                label += "（人工确认）"
            self.repair_status["stage_label"] = label
        self.log(
            f"审计修复已提交：{bid}；{len(manifest_rows)} 章；模式={commit_mode}；"
            f"备份 {archive}"
        )
        return {
            "ok": True,
            "batch_id": bid,
            "chapters": selected,
            "archive": str(archive),
            "commit_mode": commit_mode,
            "forced": bool(force),
            "manual": bool(manual and not force),
            "failed_gates": {
                str(n): list(dict.fromkeys(failed_gates.get(n) or []))
                for n in selected if failed_gates.get(n)
            },
            "resummarized": resummarize,
        }

    # ---------------- commit archive scope ----------------
    # Committing a repaired chapter only ever rewrote chapters/NNNN.md, so that is
    # all the archive held. Everything derived from that text - the summary, the
    # review, the plan, the end-of-chapter state snapshot - stayed on disk
    # describing the pre-repair version. For a TEXT_ONLY tweak that is harmless.
    # For a chapter-level rewrite it is not: rolling back restored the prose but
    # left the derived files describing text that no longer existed, and there was
    # no copy of them anywhere to recover from. So the archive is widened to
    # everything a chapter owns.

    # Folders holding one-or-more files per chapter, named with a 4-digit prefix.
    REPAIR_SIDECAR_DIRS = ("plans", "reviews", "summaries", "handoffs")
    REPAIR_SNAPSHOT_DIR = "runtime/state_snapshots"

    @staticmethod
    def _repair_sidecar_belongs_to(filename, chapter_no):
        """True when `filename` is a per-chapter artifact of `chapter_no`.

        Matching is on the leading 4-digit block only, so every variant a chapter
        can own is covered (0007.md, 0007.task.md, 0007.deep.json, 0007.round2.json)
        without needing to enumerate the suffixes, while 0070.md stays out.
        """
        m = re.match(r"^(\d{4})(?=\D|$)", str(filename or ""))
        if not m:
            return False
        try:
            return int(m.group(1)) == int(chapter_no)
        except (TypeError, ValueError):
            return False

    def _repair_archive_sidecars(self, n, archive):
        """Copy every derived artifact of chapter `n` into `archive`.

        Returns manifest rows describing what was copied and where it came from,
        so rollback can put each file back without re-deriving the layout.
        """
        rows = []
        folders = list(self.REPAIR_SIDECAR_DIRS) + [self.REPAIR_SNAPSHOT_DIR]
        for folder in folders:
            srcd = self.root / folder
            if not srcd.exists():
                continue
            for p in sorted(srcd.iterdir()):
                if not p.is_file() or not self._repair_sidecar_belongs_to(p.name, n):
                    continue
                dd = archive / folder
                dd.mkdir(parents=True, exist_ok=True)
                shutil.copy2(p, dd / p.name)
                rows.append({
                    "source": f"{folder}/{p.name}",
                    "archive_file": str((dd / p.name).relative_to(self.root)).replace("\\", "/"),
                })
        return rows

    def _repair_archive_project_files(self, archive):
        """Snapshot project-wide state and the memory DB before committing.

        These are not restored automatically (see _repair_project_restore_safe):
        they describe the whole project, so a chapter written after this commit
        would have moved them on, and putting them back would undo that work.
        They are archived so a full manual recovery is always possible.
        """
        rows = []
        for name in ("state.json", "current_state.json"):
            p = self.root / name
            if p.exists():
                shutil.copy2(p, archive / name)
                rows.append({"source": name,
                             "archive_file": str((archive / name).relative_to(self.root)).replace("\\", "/")})
        try:
            dbp = archive / "novel_memory_before.sqlite3"
            self.db.backup_to(dbp)
            rows.append({"source": "novel_memory.sqlite3",
                         "archive_file": str(dbp.relative_to(self.root)).replace("\\", "/"),
                         "auto_restore": False})
        except Exception as e:
            # A failed memory snapshot must not block the commit; the chapter text
            # and its sidecars are still fully recoverable without it.
            self.log(f"提交前记忆库快照失败（不影响正文回滚）：{e}")
        return rows

    def _repair_restore_sidecars(self, row):
        """Put a chapter's archived derived files back. Returns what was restored.

        Missing entries are skipped rather than raised: an older batch committed
        before sidecar archiving existed has no rows here, and a sidecar that was
        deleted since the commit is not a reason to abandon a rollback whose prose
        has already been restored.
        """
        done = []
        for item in row.get("sidecars") or []:
            src = self.root / str(item.get("archive_file") or "")
            rel = str(item.get("source") or "")
            if not rel or not src.exists():
                continue
            dest = self.root / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
            done.append(rel)
        return done

    # ---------------- post-commit summary rebuild ----------------
    # Committing repaired prose left `summaries/NNNN.md` describing the text as it
    # was before the repair. The next audit reads those summaries as the cheap
    # index of what each chapter established, so a stale one makes the audit
    # re-report the very inconsistency this batch just fixed - and the repair
    # pipeline then pays full price to "fix" a chapter that is already correct.
    #
    # The rebuild is deliberately narrow:
    #   * Only the summary is regenerated, never the long-term memories. Memory
    #     extraction appends rows, so re-running it would duplicate every fact the
    #     chapter already contributed. Correcting memories needs de-duplication
    #     against the existing store, which is a separate problem.
    #   * A TEXT_ONLY repair cannot change what a summary says: its verified limits
    #     cap the edit at 6% of the chapter and require the tail to match verbatim,
    #     so wording moved but no fact did. Those chapters are skipped.
    #   * Failure here never undoes the commit. The prose on disk is correct and
    #     already recorded in the rollback manifest; a summary that is still stale
    #     is a smaller problem than an aborted commit, so failures are reported
    #     per chapter and the batch stays committed.

    # Repair classes whose committed text can change what later chapters inherit.
    REPAIR_RESUMMARIZE_CLASSES = ("CONTINUITY_MINOR", "REWRITE_SPAN", "REWRITE_CHAPTER")

    @classmethod
    def _repair_resummarize_reason(cls, row, final_exists, summary_exists):
        """Decide whether one committed chapter needs its summary rebuilt.

        Returns (needed, reason). `reason` explains a skip, and is empty when the
        rebuild is wanted.
        """
        if not final_exists:
            return False, "章节文件缺失"
        if not summary_exists:
            # Nothing on disk is claiming anything stale about this chapter, and
            # inventing a summary the normal pipeline never produced is out of
            # scope for a repair commit.
            return False, "无既有摘要可重建"

        old = str(row.get("old_sha256") or "")
        new = str(row.get("new_sha256") or "")
        if old and new and old == new:
            return False, "正文未发生变化"

        repair_class = str(row.get("repair_class") or "").strip().upper()
        if repair_class in cls.REPAIR_RESUMMARIZE_CLASSES:
            return True, ""
        if repair_class == "TEXT_ONLY":
            return False, "TEXT_ONLY 仅改措辞，摘要事实不变"
        # An unrecognised class is treated as summary-affecting. Rebuilding one
        # summary costs a single Flash call; wrongly keeping a stale one costs a
        # whole audit-and-repair round on a chapter that needed nothing.
        return True, ""

    @classmethod
    def _repair_resummarize_plan(cls, rows, probe):
        """Split a commit manifest into chapters to re-summarize and skips.

        `probe(chapter_no, row)` returns (final_exists, summary_exists) so this
        stays a pure decision over I/O results.
        """
        rebuild, skipped = [], []
        for row in rows or []:
            try:
                n = int(row.get("chapter_no"))
            except (TypeError, ValueError):
                skipped.append({"chapter_no": row.get("chapter_no"),
                                "reason": "清单条目缺少有效章号"})
                continue
            final_exists, summary_exists = probe(n, row)
            needed, reason = cls._repair_resummarize_reason(
                row, final_exists, summary_exists)
            if needed:
                rebuild.append(n)
            else:
                skipped.append({"chapter_no": n, "reason": reason})
        return {"rebuild": rebuild, "skipped": skipped}

    def _repair_rebuild_summaries(self, rows):
        """Re-summarize the committed chapters whose facts may have moved.

        Returns a report dict for the commit manifest. Never raises: every failure
        is captured per chapter so a summary problem cannot roll back good prose.
        """
        def probe(n, row):
            return ((self.root / "chapters" / f"{n:04d}.md").exists(),
                    (self.root / "summaries" / f"{n:04d}.md").exists())

        plan = self._repair_resummarize_plan(rows, probe)
        rebuilt, failed = [], []
        for n in plan["rebuild"]:
            try:
                final = (self.root / "chapters" / f"{n:04d}.md").read_text(encoding="utf-8")
                summary = self.summarize(n, final)
                if not isinstance(summary, str) or not summary.strip():
                    raise RuntimeError("模型返回空摘要")
                self.write(f"summaries/{n:04d}.md", summary.strip())
                rebuilt.append(n)
            except Exception as e:
                failed.append({"chapter_no": n, "error": str(e)})
                self.log(f"第 {n} 章摘要重建失败（正文提交不受影响）：{e}")
        if rebuilt:
            self.log(f"提交后已重建 {len(rebuilt)} 章摘要：{rebuilt}")
        if failed:
            self.log(f"提交后有 {len(failed)} 章摘要未能重建，下一轮审计可能仍读到旧摘要。")
        return {"rebuilt": rebuilt, "failed": failed, "skipped": plan["skipped"]}

    # ---------------- rollback safety net (pure decision layer) ----------------
    # Rollback used to be all-or-nothing: the first chapter whose on-disk hash no
    # longer matched what this batch committed aborted the entire rollback. That is
    # the wrong trade. Hand-editing one chapter after a commit is normal, and it
    # left the operator with no way to undo the other nine chapters except by
    # copying files out of the archive by hand. Drift is a property of a single
    # chapter, so it is judged per chapter: the untouched ones are restored, the
    # drifted one is skipped and named in the report.

    ROLLBACK_RESTORE = "restore"
    ROLLBACK_SKIP = "skip"
    ROLLBACK_NOOP = "already"

    @classmethod
    def _repair_rollback_classify(cls, row, final_exists, backup_exists, current_hash):
        """Decide what can be done for one committed chapter. Returns (action, reason).

        `current_hash` is the hash of the chapter as it exists on disk right now.
        """
        if not final_exists:
            return cls.ROLLBACK_SKIP, "章节文件缺失"
        if not backup_exists:
            return cls.ROLLBACK_SKIP, "归档备份缺失"

        committed = str(row.get("new_sha256") or "")
        original = str(row.get("old_sha256") or "")
        current = str(current_hash or "")

        if committed and current == committed:
            return cls.ROLLBACK_RESTORE, ""
        # Already back at the pre-commit text: a repeated rollback, or the operator
        # reverted this chapter by hand. Writing the same bytes again is harmless,
        # but calling it "modified after commit" would be a false alarm, so it is
        # reported as a no-op instead of as drift.
        if original and current == original:
            return cls.ROLLBACK_NOOP, "已是提交前的原文"
        return cls.ROLLBACK_SKIP, "提交后又被修改，跳过以免覆盖新内容"

    @classmethod
    def _repair_rollback_plan(cls, rows, probe):
        """Split a commit manifest into restorable / no-op / skipped chapters.

        `probe(chapter_no, row)` returns (final_exists, backup_exists, current_hash)
        so this stays a pure decision over I/O results.
        """
        restore, noop, skipped = [], [], []
        for row in rows or []:
            try:
                n = int(row.get("chapter_no"))
            except (TypeError, ValueError):
                skipped.append({"chapter_no": row.get("chapter_no"),
                                "reason": "清单条目缺少有效章号"})
                continue
            final_exists, backup_exists, current_hash = probe(n, row)
            action, reason = cls._repair_rollback_classify(
                row, final_exists, backup_exists, current_hash)
            if action == cls.ROLLBACK_RESTORE:
                restore.append(n)
            elif action == cls.ROLLBACK_NOOP:
                noop.append({"chapter_no": n, "reason": reason})
            else:
                skipped.append({"chapter_no": n, "reason": reason})
        return {"restore": restore, "already": noop, "skipped": skipped}

    def _archived_canon_bundle(self, archive_dir, chapter_no):
        """Load an exact pre-repair Canon bundle from the archived SQLite snapshot."""
        n = int(chapter_no)
        archive = self.root / str(archive_dir or "")
        db_path = archive / "novel_memory_before.sqlite3"
        final_path = archive / f"{n:04d}.md"
        if not db_path.exists() or not final_path.exists():
            raise RuntimeError(f"第 {n} 章缺少旧 Canon 记忆库或正文归档，不能安全回滚")
        con = sqlite3.connect(str(db_path), timeout=10)
        con.row_factory = sqlite3.Row
        try:
            row = con.execute("SELECT * FROM chapters WHERE chapter_no=?", (n,)).fetchone()
            memories = [dict(x) for x in con.execute(
                """SELECT kind,entity,key_name AS key,content,importance,status
                   FROM memories WHERE chapter_no=? ORDER BY id""", (n,)
            ).fetchall()]
        finally:
            con.close()
        if not row:
            raise RuntimeError(f"第 {n} 章不在旧 Canon 数据库归档中")
        data = dict(row)
        final = final_path.read_text(encoding="utf-8")
        try:
            review = json.loads(data.get("review") or "{}")
        except Exception:
            review = {"severity": "PASS", "needs_revision": False, "restored_archive": True}
        try:
            raw_handoff = json.loads(data.get("handoff") or "{}")
            handoff = normalize_handoff(
                raw_handoff, n,
                extract_source_tail(final, self._continuity_config()["source_tail_chars"]),
                self._continuity_config()["handoff_max_chars"],
            )
        except Exception as exc:
            raise RuntimeError(f"第 {n} 章归档缺少有效 handoff，不能创建半一致回滚：{exc}") from exc
        return {
            "plan": data.get("plan") or self.read(f"plans/{n:04d}.md"),
            "draft": data.get("draft") or final,
            "review": review, "final": final,
            "summary": data.get("summary") or "",
            "memories": memories, "handoff": handoff,
            "generation_seconds": float(data.get("generation_seconds") or 0),
            "revision_seconds": float(data.get("revision_seconds") or 0),
        }

    def rollback_audit_repair(self, batch_id=""):
        with self.repair_lock:
            if self.repair_status.get("running"):
                raise RuntimeError("审计修复运行中不能回滚")
        bid, _plan = self._load_repair_plan(batch_id)
        d = self._repair_batch_dir(bid)
        mp = d / "commit_manifest.json"
        if not mp.exists():
            raise RuntimeError("该批次尚未提交，无法回滚")
        manifest = json.loads(mp.read_text(encoding="utf-8"))
        rows = self._repair_manifest_active_rows(manifest)
        if not rows:
            raise RuntimeError("提交清单中没有仍处于已提交状态的章节")

        rows_by_ch = {}
        for row in rows:
            try:
                rows_by_ch[int(row.get("chapter_no"))] = row
            except (TypeError, ValueError):
                continue

        def probe(n, row):
            final = self.root / "chapters" / f"{n:04d}.md"
            backup = self.root / str(row.get("archive_file") or "")
            if not final.exists():
                return False, backup.exists(), ""
            return True, backup.exists(), self._repair_hash(final.read_text(encoding="utf-8"))

        split = self._repair_rollback_plan(rows, probe)
        restore = split["restore"]
        skipped = split["skipped"]

        # Only a batch with nothing left to restore is an error, and then the
        # message carries every reason so the operator can see which chapters
        # drifted rather than just the first one.
        if not restore and not split["already"]:
            detail = "；".join(f"第 {r['chapter_no']} 章：{r['reason']}" for r in skipped)
            raise RuntimeError(f"没有可回滚的章节。{detail}")

        restored = []
        sidecars_restored = []
        rollback_at = datetime.now().isoformat(timespec="seconds")
        # Newer submissions are restored first. Each row points at the archive
        # and SQLite snapshot taken immediately before that submission.
        for n in reversed(restore):
            row = rows_by_ch[n]
            row_archive = row.get("archive_dir") or manifest.get("archive_dir")
            bundle = self._archived_canon_bundle(row_archive, n)
            self._commit_canon_bundle(
                n, plan=bundle["plan"], draft=bundle["draft"],
                final_review=bundle["review"], final=bundle["final"],
                summary=bundle["summary"], memories=bundle["memories"],
                handoff=bundle["handoff"],
                generation_seconds=bundle["generation_seconds"],
                revision_seconds=bundle["revision_seconds"], honor_stop=False,
            )
            # Restoring the prose without its summary would leave the two
            # describing different versions of the chapter, which is the drift the
            # next audit round reports again.
            sidecars_restored.extend(self._repair_restore_sidecars(row))
            row["rolled_back_at"] = rollback_at
            row["rollback_status"] = "restored"
            restored.append(n)

        for entry in split["already"]:
            try:
                n = int(entry.get("chapter_no"))
            except (AttributeError, TypeError, ValueError):
                continue
            row = rows_by_ch.get(n)
            if row is not None:
                row["rolled_back_at"] = rollback_at
                row["rollback_status"] = "already_original"

        partial = bool(skipped)
        manifest["rolled_back_at"] = rollback_at
        manifest["rolled_back_chapters"] = restored
        manifest["rollback_skipped"] = skipped
        manifest["rollback_already_original"] = split["already"]
        manifest["rollback_partial"] = partial
        manifest["rollback_restored_sidecars"] = sidecars_restored
        rollback_event = {
            "rolled_back_at": rollback_at,
            "restored_chapters": restored,
            "already_original": split["already"],
            "skipped": skipped,
            "partial": partial,
            "restored_sidecars": sidecars_restored,
        }
        manifest.setdefault("rollback_history", []).append(rollback_event)
        active_after = self._repair_manifest_active_rows(manifest)
        manifest["committed"] = bool(active_after)
        manifest["forced"] = any(bool(row.get("forced")) for row in active_after)
        manifest["manual"] = any(bool(row.get("manual")) for row in active_after)
        active_modes = {str(row.get("commit_mode") or "automatic") for row in active_after}
        manifest["commit_mode"] = (
            next(iter(active_modes)) if len(active_modes) == 1
            else ("mixed" if active_modes else "")
        )
        manifest["failed_gates"] = {
            str(row.get("chapter_no")): list(row.get("failed_gates") or [])
            for row in active_after if row.get("failed_gates")
        }
        for submission in manifest.get("submissions") or []:
            if not isinstance(submission, dict):
                continue
            members = set()
            for value in submission.get("chapters") or []:
                try:
                    members.add(int(value))
                except (TypeError, ValueError):
                    continue
            restored_here = sorted(members & (set(restored) | {
                int(x.get("chapter_no")) for x in split["already"]
                if isinstance(x, dict) and x.get("chapter_no") is not None
            }))
            if restored_here:
                submission.setdefault("rollback_history", []).append({
                    "rolled_back_at": rollback_at,
                    "chapters": restored_here,
                })
                active_members = members & {
                    int(row.get("chapter_no")) for row in active_after
                }
                submission["status"] = "partially_rolled_back" if active_members else "rolled_back"
        (d / "commit_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        with self.repair_lock:
            # A skipped chapter still holds this batch's committed text, so the
            # batch cannot be reported as fully un-committed.
            self.repair_status["committed"] = bool(active_after)
            self.repair_status["rolled_back"] = True
            self.repair_status["stage"] = "部分回滚" if partial else "已回滚"
            label = f"已恢复 {len(restored)} 章原文"
            if partial:
                label += f"，跳过 {len(skipped)} 章"
            self.repair_status["stage_label"] = label
        note = (f"审计修复已回滚：{bid}；恢复 {len(restored)} 章正文"
                f"与 {len(sidecars_restored)} 个衍生文件。")
        if skipped:
            note += "跳过：" + "，".join(
                f"第 {r['chapter_no']} 章（{r['reason']}）" for r in skipped)
        self.log(note)
        return {"ok": True, "batch_id": bid, "chapters": restored,
                "skipped": skipped, "already_original": split["already"],
                "partial": partial, "restored_sidecars": sidecars_restored}

    def _save_current_state(self, n):
        state = self.db.state_as_of(n)
        state["generated_at"] = datetime.now().isoformat(timespec="seconds")
        self.write("current_state.json", json.dumps(state, ensure_ascii=False, indent=2))
        snap = {
            "chapter": int(n), "next_chapter": int(n)+1,
            "generated_at": state["generated_at"], "current_state": state,
        }
        self.write(f"runtime/state_snapshots/{n:04d}.json", json.dumps(snap, ensure_ascii=False, indent=2))

    def _stage(self, n, stage, label=""):
        with self.lock:
            self.status.update({
                "chapter": n, "stage": stage, "stage_label": label,
                "stage_started_at": time.time() if stage != "空闲" else None,
                "stage_elapsed_seconds": 0.0, "stage_first_chunk_at": None,
                "stage_stream_chunks": 0, "stage_estimated_tps": 0.0,
                "stage_completion_tokens": 0, "stage_prompt_tokens": 0,
                "stage_cache_hit_tokens": 0, "stage_reasoning_tokens": 0,
                "stage_cost_cny": 0.0, "model_tps": 0.0, "prompt_tps": 0.0,
                "stage_stream_est_tokens": 0.0, "stage_reasoning_est_tokens": 0.0,
                "stage_output_est_tokens": 0.0, "stage_reasoning_tps": 0.0,
                "stage_output_tps": 0.0, "stage_last_chunk_at": None,
                "stage_first_reasoning_at": None, "stage_first_output_at": None,
                "completion_tokens_last": 0, "prompt_tokens_last": 0,
                "stage_provider": "", "stage_model": "", "stage_thinking": False,
                "auto_nsfw_decision": None,
                "stage_context_target_tokens": 0,
                "stage_context_estimated_tokens": 0,
                "stage_context_trimmed": False,
            })
            if stage in {"正文生成", "修订"}:
                self.status["preview_text"] = ""
                self.status["preview_label"] = label
                self.status["preview_chapter"] = n
        self._emit("stage", chapter=n, stage=stage, label=label)

    def _stop_after_stage(self, stage_name="当前阶段"):
        if self.stop_event.is_set():
            self.log(f"停止标记已生效：{stage_name}结束后停止，不进入下一阶段。")
            raise ProviderCancelledError("用户请求停止")

    def run_one(self, n):
        self._recover_canon_transactions()
        pending = sorted((self.root / "runtime" / "canon_transactions").glob("*.json"))
        if pending:
            raise CanonCommitError(
                "Canon 恢复后仍有未完成事务，已阻止生成："
                + "，".join(path.name for path in pending[:4])
            )
        db_last = self.db.last_canon_chapter()
        if self._writing_quality_config()["canon_commit_verification"] and db_last != int(n) - 1:
            raise CanonCommitError(
                f"生成顺序与 SQLite Canon 不一致：数据库最后一章为 {db_last}，"
                f"当前请求第 {int(n)} 章；必须先完成回档或上一章事务。"
            )
        allowed, message, _spec = self.external_generation_gate(n, deep=True)
        if not allowed:
            raise ExternalCanonGateError(message)
        g = self.config_loader()["generation"]
        self.log(f"===== 第 {n} 章开始 =====")

        task_card = self.chapter_task_card(n)
        if task_card:
            self.write(f"plans/{n:04d}.task.md", task_card)

        plan = self.plan_chapter(n, task_card)
        self.write(f"plans/{n:04d}.md", plan)
        self._stop_after_stage("规划")

        draft, gen_s = self.draft_chapter(n, plan, task_card)
        self.write(f"chapters/{n:04d}.draft.md", draft)
        self._stop_after_stage("正文生成")

        review = self.review_chapter(n, plan, draft, task_card)
        self.write(f"reviews/{n:04d}.initial.json", json.dumps(review, ensure_ascii=False, indent=2))
        review_history = [review]
        cost_cfg = self.config_loader().get("cost_control", {})
        risk_fields = ("plot_drift", "future_leak", "new_mainline")
        risk_flags = review.get("risk_flags", {}) if isinstance(review.get("risk_flags"), dict) else {}
        active_risks = [key for key in risk_fields if risk_flags.get(key) is True]
        severity = str(review.get("severity", "")).upper()
        confidence = str(review.get("confidence", "")).upper()

        # V4.1.2: explanation arrays no longer participate in escalation.
        # Only an explicit boolean risk flag or MAJOR verdict can spend a Pro deep review.
        need_deep = bool(cost_cfg.get("deep_review_on_risk", True)) and (
            severity == "MAJOR" or bool(active_risks)
        )
        if need_deep:
            reasons = []
            if severity == "MAJOR":
                reasons.append("MAJOR")
            reasons.extend(active_risks)
            reason_text = ", ".join(reasons) or "明确剧情风险"
            self.log(f"第 {n} 章初审发现明确剧情级风险（{reason_text}），升级 V4 Pro 深度复核。")
            review = self.review_chapter(n, plan, draft, task_card, deep=True)
            self.write(f"reviews/{n:04d}.deep.json", json.dumps(review, ensure_ascii=False, indent=2))
            review_history.append(review)
        elif bool(cost_cfg.get("deep_review_on_risk", True)) and confidence == "LOW":
            self.log(f"第 {n} 章初审 confidence=LOW，但无 MAJOR/明确剧情级风险，不升级 Deep Review。")
        review_text = json.dumps(review, ensure_ascii=False, indent=2)
        self.write(f"reviews/{n:04d}.candidate.json", review_text)
        self._stop_after_stage("结构化审查")
        if str(review.get("severity", "")).upper() == "MAJOR":
            self.log(f"第 {n} 章 Review 判定 MAJOR 剧情偏航：将执行整章纠偏重写。")

        final = draft
        rev_total = 0.0
        try:
            max_rounds = max(0, min(3, int(g.get("max_revision_rounds", 1))))
        except (TypeError, ValueError):
            max_rounds = 1
        revision_rounds = 0
        regression_retries = 0
        guard_cfg = self.config_loader().get("writing_guardrails", {}) or {}
        try:
            max_regression_retries = max(
                0, min(2, int(guard_cfg.get("revision_regression_retries", 1)))
            )
        except (TypeError, ValueError):
            max_regression_retries = 1
        last_revision_feedback = None
        best_final = final
        best_review = review
        best_round = 0
        current_round = 0
        continuity_lock = ""
        conflict_resolution = None
        conflict_resolution_attempted = False
        candidate_issue_history = [{
            str(key) for key in (review.get("issue_keys") or []) if str(key).strip()
        }]

        def severity_rank(value):
            return {"PASS": 0, "MINOR": 1, "MAJOR": 2}.get(
                str((value or {}).get("severity", "MAJOR")).upper(), 2
            )

        def revision_regressed(previous, current):
            """A local fix may not turn an accepted structure into major drift."""
            previous_rank = severity_rank(previous)
            current_rank = severity_rank(current)
            if previous_rank <= 1 and current_rank >= 2:
                return True
            previous_flags = (previous or {}).get("risk_flags") or {}
            current_flags = (current or {}).get("risk_flags") or {}
            return bool(
                previous_rank == 1
                and not previous_flags.get("plot_drift")
                and current_flags.get("plot_drift")
            )

        def remember_best(candidate, candidate_review, round_no):
            nonlocal best_final, best_review, best_round
            if severity_rank(candidate_review) < severity_rank(best_review):
                best_final = candidate
                best_review = candidate_review
                best_round = int(round_no)

        def review_passed(value):
            return (
                str(value.get("severity", "PASS")).upper() == "PASS"
                and not bool(value.get("needs_revision"))
            )

        def stop_with_candidate(message):
            candidate_path = self.root / "chapters" / f"{n:04d}.candidate.md"
            candidate_path.parent.mkdir(parents=True, exist_ok=True)
            use_best = severity_rank(best_review) < severity_rank(review)
            selected_final = best_final if use_best else final
            selected_review = best_review if use_best else review
            selected_round = best_round if use_best else current_round
            candidate_path.write_text(selected_final.strip() + "\n", encoding="utf-8")
            self.write(
                f"reviews/{n:04d}.best.json",
                json.dumps({
                    "selected_round": selected_round,
                    "normal_revision_limit": max_rounds,
                    "regression_retries_used": regression_retries,
                    "review": selected_review,
                }, ensure_ascii=False, indent=2),
            )
            self.log(
                f"第 {n} 章质量门未通过；已执行 {revision_rounds} 次修订"
                f"（正常上限 {max_rounds}，回退恢复 {regression_retries}/{max_regression_retries}）；"
                f"已保存审查等级最好的候选 {candidate_path.relative_to(self.root)}，未提交 Canon。"
            )
            raise FinalQualityGateError(message)

        def maybe_resolve_repeated_conflicts():
            nonlocal continuity_lock, conflict_resolution, conflict_resolution_attempted, review
            if continuity_lock or conflict_resolution_attempted:
                return
            if len(candidate_issue_history) < 2:
                return
            current_keys = candidate_issue_history[-1]
            previous_keys = set().union(*candidate_issue_history[:-1])
            repeated = sorted(current_keys & previous_keys)
            if not repeated:
                return
            conflict_resolution_attempted = True
            self.log(f"第 {n} 章发现重复连续性问题（{', '.join(repeated)}），启动自动裁决。")
            resolution = self.resolve_review_conflicts(
                n, plan, final, review_history, task_card, repeated
            )
            if resolution is None:
                self.log(f"第 {n} 章自动裁决未返回有效结果，继续按普通审查处理。")
                return
            if resolution.get("ambiguous") or not resolution.get("lock_text"):
                stop_with_candidate(
                    f"第 {n} 章连续性问题重复出现，但自动裁决无法从大纲唯一确定方案；"
                    "已保存候选，未提交 Canon"
                )
            continuity_lock = str(resolution["lock_text"])
            conflict_resolution = resolution
            review = self._apply_review_resolution(review, conflict_resolution)
            self.log(f"第 {n} 章连续性裁决已锁定：{resolution.get('decision', '')}")
            if resolution.get("dismissed_issue_keys"):
                self.log(
                    f"第 {n} 章连续性裁决已撤销误报："
                    + ", ".join(resolution["dismissed_issue_keys"])
                )
            self.log(f"第 {n} 章裁决修订模式：{resolution.get('repair_mode', 'LOCAL')}")

        while True:
            if not review_passed(review):
                allowed_rounds = max_rounds + regression_retries
                if revision_rounds >= allowed_rounds:
                    stop_with_candidate(
                        f"第 {n} 章用完 {max_rounds} 轮修订"
                        f"及 {regression_retries} 次回退恢复后 Review 仍未通过，Canon 提交已阻止"
                    )
                last_revision_feedback = review
                previous_final = final
                previous_review = review
                revision_rounds += 1
                candidate, sec = self.revise_chapter(
                    n, plan, final, review, revision_rounds, task_card, continuity_lock
                )
                rev_total += sec
                candidate_review = self.review_chapter(
                    n, plan, candidate, task_card, continuity_lock=continuity_lock
                )
                review_history.append(candidate_review)
                raw_candidate_issue_keys = {
                    str(key) for key in (candidate_review.get("issue_keys") or []) if str(key).strip()
                }
                if conflict_resolution is not None:
                    candidate_review = self._apply_review_resolution(
                        candidate_review, conflict_resolution
                    )
                self.write(
                    f"reviews/{n:04d}.round{revision_rounds}.json",
                    json.dumps(candidate_review, ensure_ascii=False, indent=2),
                )

                if revision_regressed(previous_review, candidate_review):
                    can_retry = regression_retries < max_regression_retries
                    if can_retry:
                        regression_retries += 1
                    self.write(
                        f"reviews/{n:04d}.round{revision_rounds}.regression.json",
                        json.dumps({
                            "previous_severity": previous_review.get("severity"),
                            "candidate_severity": candidate_review.get("severity"),
                            "restored_previous_candidate": True,
                            "recovery_retry_granted": can_retry,
                        }, ensure_ascii=False, indent=2),
                    )
                    final = previous_final
                    review = dict(previous_review)
                    review["_regression_retry"] = (
                        "上一次针对 MINOR 问题的局部修订把已经通过的核心事件删除，"
                        "审查从 MINOR 回退为 MAJOR，程序已恢复修订前版本。"
                        "本次只能修改 revision_instructions 明确点名的局部文字；"
                        "所有原有核心场景、事件顺序、人物出场和信息揭示必须逐段保留。"
                    )
                    self.log(
                        f"第 {n} 章第 {revision_rounds} 次修订发生质量回退："
                        f"{previous_review.get('severity')}→{candidate_review.get('severity')}；"
                        "已恢复修订前候选。"
                        + (" 获得一次恢复性重试。" if can_retry else " 恢复性重试额度已用完。")
                    )
                    continue

                final = candidate
                review = candidate_review
                current_round = revision_rounds
                remember_best(final, review, revision_rounds)
                candidate_issue_history.append(raw_candidate_issue_keys)
                maybe_resolve_repeated_conflicts()
                remember_best(final, review, revision_rounds)
                self.write(
                    f"reviews/{n:04d}.round{revision_rounds}.json",
                    json.dumps(review, ensure_ascii=False, indent=2),
                )
                continue

            self.log(
                f"第 {n} 章候选 Review 已通过，进入保存前最终质量门"
                f"（已修订 {revision_rounds}/{max_rounds} 轮）。"
            )
            final_review = self.review_chapter(
                n, plan, final, task_card, deep=False,
                final_gate=True, prior_review=last_revision_feedback or review,
                continuity_lock=continuity_lock,
            )
            if conflict_resolution is not None:
                final_review = self._apply_review_resolution(
                    final_review, conflict_resolution
                )
            self.write(
                f"reviews/{n:04d}.final.json",
                json.dumps(final_review, ensure_ascii=False, indent=2),
            )
            self._stop_after_stage("最终质量门")
            if review_passed(final_review):
                review = final_review
                break

            review = final_review
            if best_final == final:
                best_review = final_review
            else:
                remember_best(final, final_review, current_round)
            if revision_rounds >= max_rounds:
                stop_with_candidate(
                    f"第 {n} 章用完 {max_rounds} 轮修订后最终 Review 仍未通过，Canon 提交已阻止"
                )
            self.log(
                f"第 {n} 章最终质量门要求继续修订；将把最终 Review 反馈用于第 "
                f"{revision_rounds + 1} 轮修订，不重新生成 Plan 或 Draft。"
            )

        summary, memory_records, handoff, handoff_error = self.summarize_and_extract_memories(n, final)
        if (
            handoff_error or handoff.get("status") != "complete"
            or handoff.get("structured_complete") is not True
            or not handoff.get("scene_signatures")
        ):
            candidate_path = self.root / "chapters" / f"{n:04d}.candidate.md"
            candidate_path.parent.mkdir(parents=True, exist_ok=True)
            candidate_path.write_text(final.strip() + "\n", encoding="utf-8")
            self.write(f"summaries/{n:04d}.candidate.md", summary)
            self.log(
                f"第 {n} 章结构化 Handoff 未完成；已保留候选正文和候选摘要，"
                "Canon 提交已阻止。"
            )
            raise CanonCommitError(
                f"第 {n} 章结构化 Handoff 提取失败："
                f"{handoff_error or handoff.get('error') or 'unknown'}；未提交 Canon"
            )

        chars = len(final)
        with self.lock:
            self.status["chapter_chars"] = chars
        memory_count = self._commit_canon_bundle(
            n, plan=plan, draft=draft, final_review=review, final=final,
            summary=summary, memories=memory_records, handoff=handoff,
            generation_seconds=gen_s, revision_seconds=rev_total,
        )
        chapter_usage = self.db.usage_stats(n)
        self.log(f"第 {n} 章完成：{chars} 字；新增长期记忆 {memory_count} 条；handoff=complete；API 估算 ¥{chapter_usage.get('cost_cny',0):.4f}。")

    def _clear_plan_overflow_locked(self):
        chapter = int(self.status.get("chapter") or 1)
        history = self._chapter_cost_guard_usage(chapter)
        self.status["plan_overflow"] = {
            "pending": False, "chapter": None, "estimated_tokens": 0,
            "reason": "",
            "target_tokens": 0, "safe_tokens": 0, "provider_safe_tokens": 0,
            "over_tokens": 0, "resume_count": 0, "created_at": None,
            "hard_blocked": False,
            "auto_window_size": history["window_size"], "auto_window_allowed": history["confirm_at"],
            "auto_window_used": history["over_limit"],
            "auto_window_remaining": max(0, history["confirm_at"] - history["over_limit"]),
            "history_chapters_checked": history["checked"],
            "cost_guard_mode": history["mode"], "cost_guard_limit": history["limit"],
        }

    def approve_plan_overflow(self):
        """Approve the blocked Plan once, then resume the original batch tail."""
        with self.lock:
            if self.status["running"]:
                return False, "Canon 正在运行，不能重复确认"
            overflow = dict(self.status.get("plan_overflow") or {})
            if not overflow.get("pending") or not overflow.get("chapter"):
                return False, "当前没有等待确认的 Plan 超限请求"
            if overflow.get("hard_blocked"):
                return False, "本次 Plan 已超过高上下文模式硬上限，不能手动放行"
            chapter = int(overflow["chapter"])
            resume_count = max(1, int(overflow.get("resume_count", 1) or 1))
            self._plan_overflow_approval = {
                "chapter": chapter,
                "estimated_tokens": int(overflow.get("estimated_tokens", 0) or 0),
                "safe_tokens": int(overflow.get("safe_tokens", 0) or 0),
                "approved_at": datetime.now().isoformat(timespec="seconds"),
            }
            self._clear_plan_overflow_locked()

        ok, msg = self.start(start_chapter=chapter, count=resume_count)
        if not ok:
            with self.lock:
                self._plan_overflow_approval = None
                self.status["plan_overflow"] = overflow
            return False, msg
        end_chapter = chapter + resume_count - 1
        tail = f"；完成后继续至第 {end_chapter} 章" if resume_count > 1 else ""
        self.log(f"已确认第 {chapter} 章生成前检查；正在重新预检，仅放行本章本次请求{tail}。")
        return True, f"已一次性确认并恢复第 {chapter} 章起的原任务{tail}"

    def cancel_plan_overflow(self):
        with self.lock:
            if self.status["running"]:
                return False, "Canon 正在运行，请先停止"
            overflow = dict(self.status.get("plan_overflow") or {})
            if not overflow.get("pending"):
                return False, "当前没有等待确认的 Plan 超限请求"
            chapter = overflow.get("chapter")
            self._plan_overflow_approval = None
            self._clear_plan_overflow_locked()
            if "PlanContextOverflowError" in str(self.status.get("last_error", "")):
                self.status["last_error"] = ""
        self._stage(None, "空闲", "")
        self.log(f"已取消第 {chapter} 章 Plan 超限放行；未发送规划请求。")
        return True, "已保持停止，未发送规划请求"

    def start(self, start_chapter=None, count=None):
        with self.external_import_lock:
            if self.external_import_status.get("running"):
                return False, "外部正史导入正在运行；不能同时启动 Canon 生成"
        g = self.config_loader()["generation"]
        state = self.load_state()
        intended_start = int(
            start_chapter if start_chapter is not None
            else state.get("next_chapter", g.get("start_chapter", 1))
        )
        allowed, message, _spec = self.external_generation_gate(intended_start, deep=True)
        if not allowed:
            return False, message
        with self.lock:
            if self.status["running"]:
                return False, "Agent 已在运行"
            if (self.status.get("plan_overflow") or {}).get("pending"):
                return False, "Plan 上下文超限待确认，请先选择‘仍然继续生成’或‘不继续’"
            self.status["running"] = True
            self.status["session_chars"] = 0
            self.status["session_model_tokens"] = 0
            self.status["last_error"] = ""
            self.status["started_at"] = time.time()
            self._run_start_override = int(start_chapter) if start_chapter is not None else None
            self._run_count_override = int(count) if count is not None else None
        self.stop_event.clear(); self.reload_clients()
        self.thread = threading.Thread(target=self._run_loop, daemon=True)
        self.thread.start()
        return True, "已启动"

    def request_stop(self):
        self.stop_event.set()
        cancelled = False
        try:
            cancelled = bool(self.router and self.router.cancel_current())
        except Exception:
            cancelled = False
        if cancelled:
            self.log("收到停止请求：已尝试立即中断当前 DeepSeek 流。")
        else:
            self.log(
                "收到停止请求：已设置停止标记；Plan 本地整理会在当前扫描批次内停止，"
                "若当前网络请求无法立即取消，则在该请求返回后停止。"
            )
        return True

    def _run_loop(self):
        run_end = None
        try:
            g = self.config_loader()["generation"]
            state = self.load_state()
            start = self._run_start_override if self._run_start_override is not None else int(state.get("next_chapter", g.get("start_chapter", 1)))
            count = self._run_count_override if self._run_count_override is not None else int(g.get("chapters_per_run", 1))
            run_end = start + count - 1
            for n in range(start, start + count):
                if self.stop_event.is_set():
                    break
                allowed, message, spec = self.external_generation_gate(n, deep=True)
                if not allowed:
                    label = f"第 {spec['start']}-{spec['end']} 章 · 等待完整导入" if spec else message
                    self._stage(n, "等待外部正史", label)
                    self.log(message)
                    break
                self.run_one(n)
                state["next_chapter"] = n + 1
            self._stage(None, "空闲", "")
        except ProviderCancelledError:
            self.log("当前任务已停止；不会进入下一阶段。")
            self._stage(None, "空闲", "")
        except PlanContextOverflowError as e:
            with self.lock:
                chapter = (self.status.get("plan_overflow") or {}).get("chapter")
                if chapter is not None and run_end is not None:
                    self.status["plan_overflow"]["resume_count"] = max(1, int(run_end) - int(chapter) + 1)
                self.status["last_error"] = repr(e)
            self._stage(chapter, "规划超限待确认", f"第 {chapter} 章 · 等待手动确认")
            self.log(f"PLAN PREFLIGHT STOP: {e}")
            self._emit("error", text=repr(e))
        except CanonContextLimitError as e:
            with self.lock:
                chapter = self.status.get("chapter")
                self.status["last_error"] = repr(e)
            self._stage(chapter, "上下文超过硬上限", f"第 {chapter} 章 · 请求未发送")
            self.log(f"CONTEXT HARD LIMIT STOP: {e}")
            self._emit("error", text=repr(e))
        except Exception as e:
            with self.lock:
                self.status["last_error"] = repr(e)
            self.log(f"ERROR: {repr(e)}"); self._emit("error", text=repr(e))
        finally:
            with self.lock:
                self.status["running"] = False
                self._run_start_override = None; self._run_count_override = None
            self._emit("finished")

    # ---------------- manual DLC expansion (parallel, non-Canon) ----------------
    _DLC_TAG_RE = re.compile(r"<DLC_SCENE\b([^>]*)/?>", re.I)
    _DLC_ATTR_RE = re.compile(r"""([A-Za-z_][\w-]*)\s*=\s*["']([^"']*)["']""")

    def _parse_dlc_markers_from_text(self, text):
        out = []
        for idx, m in enumerate(self._DLC_TAG_RE.finditer(text or ""), start=1):
            attrs = {k.lower(): v for k, v in self._DLC_ATTR_RE.findall(m.group(1) or "")}
            sid = (attrs.get("id") or f"scene_{idx:02d}").strip()
            typ = (attrs.get("type") or "generic").strip().lower()
            out.append({
                "id": sid, "type": typ, "attrs": attrs, "eligible": True,
                "blocked_reason": "", "start": m.start(), "end": m.end(),
            })
        return out

    def _dlc_scene_dir(self, chapter_no, scene_id):
        return self.root / "dlc" / f"{int(chapter_no):04d}" / str(scene_id)

    def _dlc_legacy_paths(self, chapter_no, scene_id):
        base = self.root / "dlc" / f"{int(chapter_no):04d}"
        return base / f"{scene_id}.md", base / f"{scene_id}.json"

    def _dlc_candidate_ids(self, chapter_no, scene_id):
        d = self._dlc_scene_dir(chapter_no, scene_id)
        if not d.exists():
            return []
        ids = []
        for p in d.glob("candidate_*.md"):
            if re.fullmatch(r"candidate_\d+", p.stem):
                ids.append(p.stem)
        return sorted(ids, key=lambda x: int(x.split("_")[-1]))

    def _dlc_selected_candidate(self, chapter_no, scene_id):
        p = self._dlc_scene_dir(chapter_no, scene_id) / "selected.json"
        if not p.exists():
            return ""
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            return str(data.get("candidate_id", "")).strip()
        except Exception:
            return ""

    def _next_dlc_candidate_id(self, chapter_no, scene_id):
        ids = self._dlc_candidate_ids(chapter_no, scene_id)
        nums = [int(x.split("_")[-1]) for x in ids] if ids else []
        return f"candidate_{(max(nums) + 1 if nums else 1):03d}"

    def dlc_markers(self, chapter_no):
        n = int(chapter_no)
        p = self.root / "chapters" / f"{n:04d}.md"
        if not p.exists():
            raise FileNotFoundError(f"第 {n} 章 Canon 不存在")
        text = p.read_text(encoding="utf-8")
        markers = self._parse_dlc_markers_from_text(text)
        for marker in markers:
            legacy_md, _legacy_json = self._dlc_legacy_paths(n, marker["id"])
            candidate_ids = self._dlc_candidate_ids(n, marker["id"])
            selected = self._dlc_selected_candidate(n, marker["id"])
            marker["generated"] = bool(legacy_md.exists() or candidate_ids)
            marker["candidate_count"] = len(candidate_ids) + (1 if legacy_md.exists() else 0)
            marker["selected_candidate"] = selected or ("legacy" if legacy_md.exists() else "")
            marker["output_file"] = ""
            if selected and (self._dlc_scene_dir(n, marker["id"]) / f"{selected}.md").exists():
                marker["output_file"] = str((self._dlc_scene_dir(n, marker["id"]) / f"{selected}.md").relative_to(self.root)).replace("\\", "/")
            elif legacy_md.exists():
                marker["output_file"] = str(legacy_md.relative_to(self.root)).replace("\\", "/")
            marker.pop("start", None); marker.pop("end", None)
        return {"chapter": n, "markers": markers}

    def dlc_snapshot(self):
        with self.dlc_lock:
            s = dict(self.dlc_status)
        if s.get("started_at") and s.get("running"):
            s["elapsed_seconds"] = round(max(0.0, time.time() - float(s["started_at"])), 1)
        last = s.get("last_chunk_at")
        idle = max(0.0, time.time() - float(last)) if last else None
        s["stream_idle_seconds"] = round(idle, 1) if idle is not None else None
        if last and idle is not None and idle > 8.0:
            s["display_tps"] = 0.0
        s["stream_stalled"] = bool(last and idle is not None and idle > 30.0 and s.get("running"))
        return s

    @staticmethod
    def _dlc_json_list(value, limit=40):
        if not isinstance(value, list):
            return []
        out = []
        for item in value:
            if isinstance(item, (str, int, float, bool)):
                item = str(item).strip()
            elif isinstance(item, dict):
                item = json.dumps(item, ensure_ascii=False, sort_keys=True)
            else:
                continue
            if item and item not in out:
                out.append(item)
            if len(out) >= int(limit):
                break
        return out

    def _dlc_normalize_review(self, raw):
        data = _json_obj(raw, None)
        if not isinstance(data, dict):
            return {
                "passed": False, "severity": "BLOCK", "summary": "审查器未返回有效 JSON",
                "violations": ["review_json_invalid"], "checks": {},
            }
        violations = self._dlc_json_list(data.get("violations"), 40)
        checks = data.get("checks") if isinstance(data.get("checks"), dict) else {}
        raw_passed = data.get("passed")
        if isinstance(raw_passed, str):
            raw_passed = raw_passed.strip().lower() in {"true", "1", "yes", "pass", "passed"}
        passed = raw_passed is True and not violations
        def as_bool(value):
            if isinstance(value, str):
                return value.strip().lower() in {"true", "1", "yes", "pass", "passed"}
            return value is True
        return {
            "passed": passed,
            "severity": str(data.get("severity", "PASS" if passed else "BLOCK") or "").upper(),
            "summary": str(data.get("summary", "") or "").strip(),
            "violations": violations,
            "checks": {str(k): as_bool(v) for k, v in list(checks.items())[:24]},
        }

    @staticmethod
    def _dlc_repetition_violations(text):
        """Deterministic duplicate guard; the model review is not trusted alone."""
        src = str(text or "").strip()
        problems = []
        if len(src) < 120:
            problems.append("候选过短，无法构成有效扩写")
        paragraphs = [re.sub(r"\s+", " ", x).strip() for x in re.split(r"\n\s*\n|\n", src) if x.strip()]
        seen = set()
        for para in paragraphs:
            key = re.sub(r"[\s，。！？；：、,.!?;:\"'“”‘’]+", "", para)
            if len(key) >= 50 and key in seen:
                problems.append("存在完全重复的长段落")
                break
            if len(key) >= 50:
                seen.add(key)
        compact = re.sub(r"\s+", "", src)
        if len(compact) >= 800:
            half = len(compact) // 2
            left = compact[:half]
            right = compact[half:half + len(left)]
            if len(left) >= 350 and difflib.SequenceMatcher(None, left, right).ratio() >= 0.88:
                problems.append("候选前后两部分高度重复")
        return problems

    # Grok DLC uses four source layers: full chapter, user instruction, relevant
    # reference entries, and relevant characters' personality/appearance.
    def _grok_dlc_atlas(self, focus, max_chars=9000):
        cfg = self.config_loader().get("dlc", {})
        rel = str(cfg.get("atlas_file", "prompts/expansion_reference.md"))
        path = self.root / rel
        if not path.exists():
            raise RuntimeError(f"缺少 DLC 图鉴：{path}")
        text = path.read_text(encoding="utf-8").strip()
        if len(text) <= max_chars:
            return text
        headings = list(re.finditer(r"(?m)^#{1,6}\s+.+$", text))
        if not headings:
            return text[:max_chars]
        terms = set(re.findall(r"[\u3400-\u9fff]{2,8}|[A-Za-z0-9_-]{3,}", str(focus or "")))
        intro = text[:headings[0].start()].strip()
        blocks = []
        for i, match in enumerate(headings):
            end = headings[i + 1].start() if i + 1 < len(headings) else len(text)
            block = text[match.start():end].strip()
            heading = match.group(0)
            score = sum(8 for term in terms if term in heading)
            score += sum(1 for term in terms if term in block[:1200])
            clean_heading = re.sub(r"^#{1,6}\s+", "", heading).strip()
            clean_heading = re.sub(r"^\d+(?:\.\d+)*[\.、)）\s]+", "", clean_heading)
            heading_phrases = [x.strip() for x in re.split(r"[（(：:、/|]", clean_heading) if 2 <= len(x.strip()) <= 20]
            if any(phrase in str(focus or "") for phrase in heading_phrases):
                score += 20
            if any(word in block for word in ("用途说明", "使用原则", "记号说明")):
                score += 5
            blocks.append((score, i, block))
        ranked = sorted(blocks, key=lambda row: (-row[0], row[1]))
        chosen = []
        used = 0
        for score, idx, block in ranked:
            if score <= 0 and chosen:
                continue
            if used + len(block) > max_chars and chosen:
                continue
            chosen.append((idx, block))
            used += len(block)
            if used >= max_chars * 0.85:
                break
        if not chosen:
            chosen = [(idx, block) for _score, idx, block in blocks[:4]]
        body = "\n\n".join(block for _idx, block in sorted(chosen))
        return (intro + "\n\n" + body).strip()[:max_chars]

    @staticmethod
    def _grok_character_fields(block, max_chars=3500):
        """Keep identity plus appearance/personality-oriented profile fields."""
        block = str(block or "").strip()
        heads = list(re.finditer(r"(?m)^#{2,6}\s+.+$", block))
        if len(heads) <= 1:
            return block[:max_chars]
        keep_words = (
            "外貌", "容貌", "五官", "身材", "体型", "气质", "性格", "年龄",
            "身份", "服装", "穿着", "身体", "特征", "说话", "语言", "偏好",
        )
        selected = [block[:heads[0].end()].strip()]
        for i, match in enumerate(heads[1:], 1):
            end = heads[i + 1].start() if i + 1 < len(heads) else len(block)
            piece = block[match.start():end].strip()
            if any(word in match.group(0) for word in keep_words):
                selected.append(piece)
        result = "\n\n".join(x for x in selected if x)
        return (result or block)[:max_chars]

    def _grok_dlc_characters(self, text, marker, custom_prompt, max_chars=12000):
        start, end = int(marker["start"]), int(marker["end"])
        focus = "\n".join((
            str(custom_prompt or ""),
            text[max(0, start - 8000):min(len(text), end + 8000)],
        ))
        ranked = []
        for entry in self._character_seed_entries():
            positions = [focus.find(name) for name in entry.get("names", []) if focus.find(name) >= 0]
            if positions:
                ranked.append((min(positions), entry))
        ranked.sort(key=lambda row: row[0])
        if not ranked:
            for entry in self._character_seed_entries():
                if any(name in text for name in entry.get("names", [])):
                    ranked.append((len(ranked), entry))
        out = []
        used = 0
        for _pos, entry in ranked[:8]:
            piece = self._grok_character_fields(entry.get("text", ""), 3500)
            if used + len(piece) > max_chars and out:
                continue
            out.append(piece)
            used += len(piece)
        return "\n\n".join(out) if out else "（未在人物种子中识别到相关人物；以完整章节中的人物信息为准）"

    def _grok_dlc_style(self, max_chars=7000):
        """Extract a compact prose-style layer without injecting all of style.md."""
        cfg = self.config_loader().get("dlc", {})
        name = str(cfg.get("style_file", "style.md") or "style.md")
        text = self.read_story(name).strip()
        if not text:
            return "（未找到 style.md；沿用完整章节已经体现的叙事视角、语言与段落风格。）"
        max_chars = max(1200, min(12000, int(max_chars or 7000)))
        headings = list(re.finditer(r"(?m)^#(?!#)\s+.+$", text))
        if not headings:
            return text[:max_chars]
        wanted = (
            "写作风格", "叙事视角", "语言风格", "AI式表达控制", "网文叙事要求",
            "人物描写", "对话", "章节结构", "正文生成核心原则",
        )
        blocks = []
        for i, match in enumerate(headings):
            end = headings[i + 1].start() if i + 1 < len(headings) else len(text)
            heading = re.sub(r"^#\s+", "", match.group(0)).strip()
            if heading in wanted:
                blocks.append((wanted.index(heading), text[match.start():end].strip()))
        if not blocks:
            return text[:max_chars]
        blocks.sort(key=lambda row: row[0])
        per_block = max(500, max_chars // len(blocks))
        selected = []
        for _priority, block in blocks:
            if len(block) <= per_block:
                selected.append(block)
                continue
            cut = max(block.rfind("\n\n", 0, per_block), block.rfind("\n", 0, per_block))
            if cut < 120:
                cut = max(block.rfind("。", 0, per_block), block.rfind("；", 0, per_block)) + 1
            selected.append(block[:max(120, cut)].rstrip())
        return "\n\n".join(selected).strip()[:max_chars]

    @staticmethod
    def _grok_dlc_system():
        return """你是小说的非 Canon DLC 扩写器。
只输出可插入 DLC 标记位置的新增正文，不输出标题、说明、分析、合同或 JSON。
优先级：本次扩写提示词 > 完整章节中标记处的即时状态 > 图鉴定义 > 人物信息 > 精简文风规则。
必须准确执行提示词指定的主体、对象、先后顺序和场景目标，不得颠倒、遗漏或擅增参与对象。
必须持续追踪人物位置、持有物、时间与当前状态；状态改变必须有明确且可行的过渡。
参考资料只用于理解场景关系与行动边界，不要擅自新增复杂设定、关键道具或剧情结论。
保持人物性格、外貌与说话方式一致。避免机械罗列身体部位、相同句式、相同反应、重复台词、重复段落和前后内容复写。
这是额外内容，不要求维持正式剧情走向或后续关系结论；审查重点只限对象、顺序、动作可行性、连续性与重复。"""

    def _grok_dlc_payload(self, n, sid, text, marker, custom_prompt):
        focus = "\n".join((str(custom_prompt or ""), text))
        atlas = self._grok_dlc_atlas(focus, int(self.config_loader().get("dlc", {}).get("atlas_max_chars", 9000)))
        characters = self._grok_dlc_characters(
            text, marker, custom_prompt,
            int(self.config_loader().get("dlc", {}).get("character_max_chars", 12000)),
        )
        style = self._grok_dlc_style(
            int(self.config_loader().get("dlc", {}).get("style_max_chars", 7000))
        )
        user = f"""【任务位置】
chapter={int(n)}
scene_id={sid}
请把新增正文写在完整章节中的对应 DLC 标记位置。

【本次扩写提示词】
{custom_prompt or '请依据标记处前后文自然扩写；不得自行改变参与对象与当前状态。'}

【完整指定章节】
{text}

【相关图鉴】
{atlas}

【相关人物性格与外貌】
{characters}

【DLC 专用精简文风规则】
{style}

只输出新增的 DLC 正文。"""
        return user, atlas, characters, style

    def _grok_dlc_review(self, n, sid, text, custom_prompt, atlas, characters, candidate):
        cfg = self.config_loader().get("dlc", {})
        deterministic = self._dlc_repetition_violations(candidate)
        if not bool(cfg.get("review_enabled", True)):
            return {
                "passed": not deterministic,
                "severity": "PASS" if not deterministic else "BLOCK",
                "summary": "程序重复检查通过" if not deterministic else "程序重复检查未通过",
                "violations": deterministic, "checks": {"no_repetition": not deterministic},
            }
        system = """你是非 Canon DLC 动作校验器，只输出 JSON。
只检查：参与对象和动作主体是否正确；提示词指定顺序是否完成；人物位置、物品、时间和状态是否前后一致；动作是否可行；是否擅增未要求的复杂设定或关键道具；是否存在明显重复。
不得以偏离正式剧情、改变非 Canon 关系、增加原章没有的额外互动或无法承接后续 Canon 为理由判失败。"""
        user = f"""【完整章节】
{text}

【扩写提示词】
{custom_prompt or '（无额外提示）'}

【相关图鉴】
{atlas}

【相关人物】
{characters}

【待审查候选】
{candidate}

输出 JSON：{{"passed":true,"severity":"PASS","summary":"结论","violations":[],"checks":{{"subject_and_object":true,"requested_sequence":true,"state_consistency":true,"action_feasibility":true,"no_unrequested_complex_setup":true,"no_repetition":true}}}}"""
        try:
            raw, _ = self.dlc_router.chat(
                "dlc", system, user, temperature=0.0, max_tokens=1400,
                stream=False, label="grok_dlc_review", emit_text=False,
                provider_override="grok", model_override=cfg.get("review_model", cfg.get("model", "grok-4.6")),
                thinking_override=False, response_format={"type": "json_object"},
                reasoning_effort_override=cfg.get("review_reasoning_effort", "medium"),
                allow_local_fallback=False,
            )
            review = self._dlc_normalize_review(raw)
        except ProviderCancelledError:
            raise
        except Exception as error:
            review = {
                "passed": False, "severity": "BLOCK",
                "summary": "Grok 审查失败；候选已保留但禁止选中",
                "violations": ["review_request_failed"], "checks": {}, "error": repr(error),
            }
        if deterministic:
            review["passed"] = False
            review["severity"] = "BLOCK"
            review["violations"] = list(dict.fromkeys(list(review.get("violations", [])) + deterministic))
            review["summary"] = review.get("summary") or "程序重复检查未通过"
        return review

    def start_dlc(self, chapter_no, scene_id, custom_prompt="", max_tokens=0, draw_count=1):
        n = int(chapter_no)
        sid = str(scene_id or "").strip()
        if not sid:
            return False, "scene_id 不能为空"
        try:
            draw_count = int(draw_count or 1)
        except Exception:
            return False, "抽奖次数必须是整数"
        if not 1 <= draw_count <= 20:
            return False, "抽奖次数必须在 1～20 之间"
        with self.dlc_lock:
            if self.dlc_status.get("running"):
                return False, f"已有 DLC 正在生成：第{self.dlc_status.get('chapter')}章 {self.dlc_status.get('scene_id')}"
        path = self.root / "chapters" / f"{n:04d}.md"
        if not path.exists():
            return False, f"第 {n} 章 Canon 不存在"
        text = path.read_text(encoding="utf-8")
        marker = next((item for item in self._parse_dlc_markers_from_text(text) if item["id"] == sid), None)
        if not marker:
            return False, f"第 {n} 章找不到 DLC 标记：{sid}"
        if not marker.get("eligible", True):
            return False, marker.get("blocked_reason") or "该 DLC 标记不可生成"
        ok, detail = self.dlc_router.grok.health()
        if not ok:
            return False, detail
        cfg = self.config_loader().get("dlc", {})
        effective_max_tokens = max(512, min(12000, int(max_tokens or cfg.get("max_tokens", 3200))))
        self.dlc_stop_event.clear()
        with self.dlc_lock:
            self.dlc_status.update({
                "running": True, "chapter": n, "scene_id": sid, "stage": "Grok DLC 准备",
                "started_at": time.time(), "elapsed_seconds": 0.0, "first_chunk_at": None,
                "last_chunk_at": None, "stream_est_tokens": 0.0, "display_tps": 0.0,
                "model_tps": 0.0, "prompt_tps": 0.0, "output_chars": 0,
                "last_error": "", "output_file": "", "canon_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "custom_prompt": str(custom_prompt or "").strip(), "max_tokens": effective_max_tokens,
                "preview_text": "", "draw_count": draw_count, "current_draw": 0,
                "candidates_completed": 0, "candidate_id": "", "last_candidate_id": "",
                "candidates_passed": 0, "candidates_blocked": 0, "review_status": "",
                "prompt_tokens": 0, "completion_tokens": 0,
                "request_count": 0, "provider": "grok", "model": cfg.get("model", "grok-4.6"),
            })
        self.dlc_thread = threading.Thread(
            target=self._run_dlc,
            args=(n, sid, str(custom_prompt or "").strip(), effective_max_tokens, draw_count), daemon=True,
        )
        self.dlc_thread.start()
        self.log(f"Grok DLC 已启动：第 {n} 章 {sid}；完整章节 + 扩写提示词 + 相关图鉴 + 相关人物 + 精简文风，共 {draw_count} 抽。")
        return True, f"Grok DLC 已启动，共 {draw_count} 抽"

    def request_stop_dlc(self):
        self.dlc_stop_event.set()
        self.dlc_router.cancel_current()
        self.log("收到 Grok DLC 停止请求：正在中断当前网络请求。")
        return True

    def _run_dlc(self, n, sid, custom_prompt="", max_tokens=0, draw_count=1):
        try:
            path = self.root / "chapters" / f"{n:04d}.md"
            text = path.read_text(encoding="utf-8")
            marker = next((item for item in self._parse_dlc_markers_from_text(text) if item["id"] == sid), None)
            if not marker:
                raise RuntimeError("DLC 标记在任务启动后被删除")
            cfg = self.config_loader().get("dlc", {})
            model = str(cfg.get("model", "grok-4.6"))
            canon_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
            with self.dlc_lock:
                self.dlc_status["stage"] = "整理完整章节、图鉴、人物与文风"
                self.dlc_status["review_status"] = "context"
            payload, atlas, characters, style = self._grok_dlc_payload(n, sid, text, marker, custom_prompt)
            self.log(f"Grok DLC 输入准备完成：完整章节 {len(text):,} 字符；相关图鉴 {len(atlas):,}；相关人物 {len(characters):,}；精简文风 {len(style):,}。")
            for draw_index in range(1, int(draw_count) + 1):
                if self.dlc_stop_event.is_set():
                    break
                candidate_id = self._next_dlc_candidate_id(n, sid)
                with self.dlc_lock:
                    self.dlc_status.update({
                        "stage": f"Grok DLC 候选 {draw_index}/{draw_count}", "current_draw": draw_index,
                        "candidate_id": candidate_id, "review_status": "draft", "first_chunk_at": None,
                        "last_chunk_at": None, "stream_est_tokens": 0.0, "display_tps": 0.0,
                        "output_chars": 0, "preview_text": "",
                    })
                attempts = []
                out = ""
                review = {"passed": False, "summary": "尚未审查", "violations": []}
                feedback = ""
                retry_total = 1 + max(0, min(1, int(cfg.get("review_retry_count", 1))))
                for attempt in range(1, retry_total + 1):
                    if self.dlc_stop_event.is_set():
                        break
                    user = payload
                    if feedback:
                        user += "\n\n【上一版必须修正的问题】\n" + feedback + "\n只重新输出完整 DLC 正文。"
                    with self.dlc_lock:
                        self.dlc_status["stage"] = f"Grok DLC 写作 {draw_index}/{draw_count} · {attempt}/{retry_total}"
                        self.dlc_status["review_status"] = "draft" if attempt == 1 else "retry"
                        if attempt > 1:
                            self.dlc_status["preview_text"] = ""
                            self.dlc_status["output_chars"] = 0
                    out, _ = self.dlc_router.chat(
                        "dlc", self._grok_dlc_system(), user,
                        temperature=max(0.0, min(1.0, float(cfg.get("temperature", 0.4)))),
                        max_tokens=int(max_tokens), stream=True, label="grok_dlc", emit_text=False,
                        provider_override="grok", model_override=model, thinking_override=False,
                        reasoning_effort_override=cfg.get("reasoning_effort", "low"),
                        allow_local_fallback=False,
                    )
                    with self.dlc_lock:
                        self.dlc_status["stage"] = f"Grok DLC 审查 {draw_index}/{draw_count}"
                        self.dlc_status["review_status"] = "review"
                    review = self._grok_dlc_review(n, sid, text, custom_prompt, atlas, characters, out)
                    attempts.append({"attempt": attempt, "passed": bool(review.get("passed")),
                                     "summary": review.get("summary", ""), "violations": review.get("violations", [])})
                    if review.get("passed") or "review_request_failed" in review.get("violations", []):
                        break
                    feedback = "\n".join(f"- {item}" for item in review.get("violations", [])[:12])
                if self.dlc_stop_event.is_set() and not out:
                    break
                passed = bool(review.get("passed"))
                out_dir = self._dlc_scene_dir(n, sid)
                out_dir.mkdir(parents=True, exist_ok=True)
                (out_dir / f"{candidate_id}.md").write_text(out, encoding="utf-8")
                meta = {
                    "chapter": n, "scene_id": sid, "candidate_id": candidate_id,
                    "generated_at": datetime.now().isoformat(timespec="seconds"),
                    "canon_sha256": canon_hash, "non_canon": True, "memory_ingest": False,
                    "custom_prompt": custom_prompt, "max_tokens": int(max_tokens),
                    "temperature": float(cfg.get("temperature", 0.4)), "strict_dlc_version": 5,
                    "grok_dlc_version": 2, "provider": "grok", "model": model,
                    "review_model": cfg.get("review_model", model), "selectable": passed,
                    "review_status": "passed" if passed else "blocked", "review": review,
                    "attempts": attempts, "atlas_file": cfg.get("atlas_file", "prompts/expansion_reference.md"),
                    "full_chapter_chars": len(text), "atlas_chars": len(atlas),
                    "character_profile_chars": len(characters), "style_chars": len(style),
                    "style_file": cfg.get("style_file", "style.md"),
                    "prompt_layers": ["full_chapter", "custom_prompt", "relevant_atlas", "relevant_character_personality_appearance", "compact_style_rules"],
                }
                (out_dir / f"{candidate_id}.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
                with self.dlc_lock:
                    self.dlc_status["output_file"] = str((out_dir / f"{candidate_id}.md").relative_to(self.root)).replace("\\", "/")
                    self.dlc_status["last_candidate_id"] = candidate_id
                    self.dlc_status["candidates_completed"] += 1
                    self.dlc_status["review_status"] = "passed" if passed else "blocked"
                    self.dlc_status["candidates_passed" if passed else "candidates_blocked"] += 1
                self.log(f"Grok DLC 候选{'通过' if passed else '已拦截'}：第 {n} 章 {sid} / {candidate_id}。")
                if self.dlc_stop_event.is_set():
                    break
        except ProviderCancelledError:
            self.log(f"Grok DLC 已停止：第 {n} 章 {sid}")
        except Exception as error:
            with self.dlc_lock:
                self.dlc_status["last_error"] = repr(error)
            self.log(f"DLC ERROR: {repr(error)}")
        finally:
            with self.dlc_lock:
                self.dlc_status["running"] = False
                self.dlc_status["stage"] = "空闲"
                self.dlc_status["candidate_id"] = ""
            self.hub.publish({"type": "dlc_finished", "chapter": n, "scene_id": sid})

    def list_dlc_candidates(self, chapter_no, scene_id):
        n = int(chapter_no); sid = str(scene_id)
        canon_p = self.root / "chapters" / f"{n:04d}.md"
        if not canon_p.exists():
            raise FileNotFoundError("Canon 文件不存在")
        current_hash = hashlib.sha256(canon_p.read_bytes()).hexdigest()
        selected = self._dlc_selected_candidate(n, sid)
        rows = []
        legacy_md, legacy_json = self._dlc_legacy_paths(n, sid)
        if legacy_md.exists():
            meta = {}
            if legacy_json.exists():
                try: meta = json.loads(legacy_json.read_text(encoding="utf-8"))
                except Exception: meta = {}
            rows.append({
                "candidate_id": "legacy", "label": "旧版 DLC", "chars": len(legacy_md.read_text(encoding="utf-8")),
                "generated_at": meta.get("generated_at", ""), "custom_prompt": meta.get("custom_prompt", ""),
                "stale": bool(meta.get("canon_sha256") and meta.get("canon_sha256") != current_hash),
                "selected": selected == "legacy" or (not selected and not self._dlc_candidate_ids(n, sid)),
                "strict_dlc_version": 0, "selectable": False, "review_status": "unreviewed",
                "review_summary": "旧版候选未经过通用约束合同与独立审查，禁止选中；请重新生成。",
                "review_violations": [],
            })
        d = self._dlc_scene_dir(n, sid)
        for cid in self._dlc_candidate_ids(n, sid):
            md = d / f"{cid}.md"; mp = d / f"{cid}.json"
            meta = {}
            if mp.exists():
                try: meta = json.loads(mp.read_text(encoding="utf-8"))
                except Exception: meta = {}
            stale = bool(meta.get("canon_sha256") and meta.get("canon_sha256") != current_hash)
            strict_version = int(meta.get("strict_dlc_version", 0) or 0)
            review = meta.get("review") if isinstance(meta.get("review"), dict) else {}
            review_status = str(meta.get("review_status", "") or "")
            if strict_version < 2:
                review_status = "unreviewed"
            selectable = bool(
                strict_version >= 2 and meta.get("selectable") is True and
                review.get("passed") is True and not stale
            )
            rows.append({
                "candidate_id": cid, "label": cid.replace("candidate_", "候选 "),
                "chars": len(md.read_text(encoding="utf-8")),
                "generated_at": meta.get("generated_at", ""), "custom_prompt": meta.get("custom_prompt", ""),
                "stale": stale,
                "selected": selected == cid,
                "strict_dlc_version": strict_version,
                "selectable": selectable,
                "review_status": review_status or ("passed" if selectable else "blocked"),
                "review_summary": review.get("summary", "") if strict_version >= 2 else "旧候选未审查，禁止选中；请重新生成。",
                "review_violations": review.get("violations", []) if strict_version >= 2 else [],
            })
        return {"chapter": n, "scene_id": sid, "selected_candidate": selected, "candidates": rows}

    def select_dlc_candidate(self, chapter_no, scene_id, candidate_id):
        n = int(chapter_no); sid = str(scene_id); cid = str(candidate_id or "").strip()
        if cid == "legacy":
            md, _ = self._dlc_legacy_paths(n, sid)
            if not md.exists(): raise FileNotFoundError("旧版 DLC 不存在")
            raise RuntimeError("旧版 DLC 未经过严格约束与独立审查，禁止选中；请重新生成候选")
        elif not re.fullmatch(r"candidate_\d+", cid):
            raise ValueError("candidate_id 无效")
        elif not (self._dlc_scene_dir(n, sid) / f"{cid}.md").exists():
            raise FileNotFoundError("DLC 候选不存在")
        meta_path = self._dlc_scene_dir(n, sid) / f"{cid}.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
        review = meta.get("review") if isinstance(meta.get("review"), dict) else {}
        if int(meta.get("strict_dlc_version", 0) or 0) < 2:
            raise RuntimeError("该候选由旧版流程生成，未经过严格审查，禁止选中；请重新生成")
        if meta.get("selectable") is not True or review.get("passed") is not True:
            reason = review.get("summary") or "连续性审查未通过"
            raise RuntimeError(f"该候选已被拦截，禁止选中：{reason}")
        canon_p = self.root / "chapters" / f"{n:04d}.md"
        current_hash = hashlib.sha256(canon_p.read_bytes()).hexdigest() if canon_p.exists() else ""
        if meta.get("canon_sha256") and meta.get("canon_sha256") != current_hash:
            raise RuntimeError("生成候选后 Canon 已变更，必须重新生成并审查，不能选中旧候选")
        d = self._dlc_scene_dir(n, sid); d.mkdir(parents=True, exist_ok=True)
        (d / "selected.json").write_text(json.dumps({
            "candidate_id": cid, "selected_at": datetime.now().isoformat(timespec="seconds")
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"chapter": n, "scene_id": sid, "selected_candidate": cid}

    def delete_dlc_candidate(self, chapter_no, scene_id, candidate_id):
        n = int(chapter_no); sid = str(scene_id); cid = str(candidate_id or "").strip()
        with self.dlc_lock:
            if self.dlc_status.get("running") and int(self.dlc_status.get("chapter") or 0) == n and self.dlc_status.get("scene_id") == sid:
                raise RuntimeError("该场景正在生成 DLC，请先停止任务")
        if cid == "legacy":
            md, mp = self._dlc_legacy_paths(n, sid)
        else:
            if not re.fullmatch(r"candidate_\d+", cid): raise ValueError("candidate_id 无效")
            d = self._dlc_scene_dir(n, sid); md, mp = d / f"{cid}.md", d / f"{cid}.json"
        if not md.exists(): raise FileNotFoundError("DLC 候选不存在")
        md.unlink(missing_ok=True); mp.unlink(missing_ok=True)
        if self._dlc_selected_candidate(n, sid) == cid:
            (self._dlc_scene_dir(n, sid) / "selected.json").unlink(missing_ok=True)
        return {"chapter": n, "scene_id": sid, "deleted": cid}

    def read_dlc(self, chapter_no, scene_id, candidate_id=""):
        n = int(chapter_no); sid = str(scene_id); cid = str(candidate_id or "").strip()
        if not cid:
            cid = self._dlc_selected_candidate(n, sid)
        if not cid:
            ids = self._dlc_candidate_ids(n, sid)
            cid = ids[-1] if ids else "legacy"
        if cid == "legacy":
            p, meta_p = self._dlc_legacy_paths(n, sid)
        else:
            if not re.fullmatch(r"candidate_\d+", cid): raise ValueError("candidate_id 无效")
            d = self._dlc_scene_dir(n, sid); p, meta_p = d / f"{cid}.md", d / f"{cid}.json"
        if not p.exists():
            raise FileNotFoundError("DLC 文件不存在")
        meta = json.loads(meta_p.read_text(encoding="utf-8")) if meta_p.exists() else {}
        canon_p = self.root / "chapters" / f"{n:04d}.md"
        current_hash = hashlib.sha256(canon_p.read_bytes()).hexdigest() if canon_p.exists() else ""
        review = meta.get("review") if isinstance(meta.get("review"), dict) else {}
        return {
            "chapter": n, "scene_id": sid, "candidate_id": cid, "text": p.read_text(encoding="utf-8"),
            "meta": meta, "stale": bool(meta.get("canon_sha256") and current_hash and meta.get("canon_sha256") != current_hash),
            "selected": self._dlc_selected_candidate(n, sid) == cid,
            "selectable": bool(int(meta.get("strict_dlc_version", 0) or 0) >= 2 and meta.get("selectable") is True and review.get("passed") is True),
            "review_status": meta.get("review_status", "unreviewed"),
            "review_summary": review.get("summary", ""),
        }

    # ---------------- destructive rewrite / rollback ----------------
    def _archive_before_rewrite(self, n):
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        dest = self.root / "archive" / f"rewrite_{int(n):04d}_{stamp}"
        dest.mkdir(parents=True, exist_ok=False)
        rows = []

        def archive_file(source, relative):
            target = dest / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            source_hash = sha256_file(source)
            target_hash = sha256_file(target)
            if source_hash != target_hash:
                raise RuntimeError(f"重写归档哈希校验失败：{relative}")
            rows.append({
                "source": str(relative).replace("\\", "/"),
                "archive": str(target.relative_to(self.root)).replace("\\", "/"),
                "sha256": source_hash,
                "bytes": source.stat().st_size,
            })

        db_archive = dest / "novel_memory_before.sqlite3"
        self.db.backup_to(db_archive)
        with sqlite3.connect(str(db_archive), timeout=10) as con:
            integrity = con.execute("PRAGMA integrity_check").fetchone()
        if not integrity or str(integrity[0]).lower() != "ok":
            raise RuntimeError(f"重写归档数据库校验失败：{integrity}")
        rows.append({
            "source": "novel_memory.sqlite3",
            "archive": str(db_archive.relative_to(self.root)).replace("\\", "/"),
            "sha256": sha256_file(db_archive),
            "bytes": db_archive.stat().st_size,
            "sqlite_backup": True,
        })
        for name in ("state.json", "current_state.json"):
            p = self.root / name
            if p.exists():
                archive_file(p, Path(name))
        for folder in (
            "chapters", "plans", "reviews", "summaries", "handoffs",
            "runtime/state_snapshots",
        ):
            srcd = self.root / folder
            if not srcd.exists():
                continue
            for p in srcd.iterdir():
                m = re.match(r"^(\d{4})", p.name)
                if m and int(m.group(1)) >= int(n) and p.is_file():
                    archive_file(p, Path(folder) / p.name)

        contract_cache = self.root / "runtime" / "plan_stage_contracts"
        if contract_cache.exists():
            for p in sorted(path for path in contract_cache.iterdir() if path.is_file()):
                archive_file(p, Path("runtime/plan_stage_contracts") / p.name)
        manifest = {
            "schema_version": 1,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "rewrite_from": int(n),
            "archive_complete": True,
            "files": rows,
        }
        (dest / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        return dest

    def rollback_from(self, n, make_archive=True):
        n = int(n)
        with self.lock:
            if self.status.get("running"):
                raise RuntimeError("Agent 运行中不能回档")
        archive = self._archive_before_rewrite(n) if make_archive else None
        for folder in ("chapters", "plans", "reviews", "summaries", "handoffs"):
            d = self.root / folder
            if d.exists():
                for p in list(d.iterdir()):
                    m = re.match(r"^(\d{4})", p.name)
                    if m and int(m.group(1)) >= n and p.is_file():
                        p.unlink(missing_ok=True)
        snapd = self.root / "runtime" / "state_snapshots"
        if snapd.exists():
            for p in list(snapd.glob("*.json")):
                try:
                    if int(p.stem) >= n:
                        p.unlink(missing_ok=True)
                except ValueError:
                    pass
        contract_cache = self.root / "runtime" / "plan_stage_contracts"
        if contract_cache.exists():
            for p in list(contract_cache.iterdir()):
                if p.is_file():
                    p.unlink(missing_ok=True)
        self._stage_contract_cache.clear()
        self.db.delete_from_chapter(n)
        for spec in self._external_ranges():
            if n > int(spec["end"]):
                continue
            external_root = self._external_range_root(spec)
            if not external_root.exists():
                continue
            if archive:
                external_archive = Path(archive) / "external_canon" / range_key(spec)
                external_archive.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(external_root, external_archive, dirs_exist_ok=True)
            shutil.rmtree(external_root)
        self.save_state({"next_chapter": n})
        state = self.db.state_as_of(n-1)
        self.write("current_state.json", json.dumps(state, ensure_ascii=False, indent=2))
        self.log(f"已回档到第 {n-1} 章结束；第 {n} 章及以后内容/记忆已从活动项目删除。")
        return {"ok": True, "next_chapter": n, "archive": str(archive) if archive else ""}

    def rewrite_from(self, n):
        spec = find_range(self._external_ranges(), int(n))
        if spec:
            if int(n) != int(spec["start"]):
                raise RuntimeError(
                    f"第 {spec['start']}-{spec['end']} 章是不可拆分的外部正史卷；"
                    f"如需替换，请从第 {spec['start']} 章整体回档并重新导入定稿 ZIP。"
                )
            result = self.rollback_from(n, make_archive=True)
            result["message"] = (
                f"外部正史卷第 {spec['start']}-{spec['end']} 章已归档并从活动 Canon 移除；"
                "NovelAgent 保持等待，不会生成该范围。请导入新的完整定稿 ZIP。"
            )
            return result
        r = self.rollback_from(n, make_archive=True)
        ok, msg = self.start(start_chapter=int(n), count=1)
        if not ok:
            raise RuntimeError(msg)
        r["message"] = f"已回档并开始重写第 {int(n)} 章"
        return r

    # ---------------- non-destructive reader paragraph reflow ----------------
    @staticmethod
    def _reader_split_blocks(text):
        """Return the chapter's existing non-empty Markdown paragraphs.

        Only blank-line separators are discarded.  The model never receives
        permission to edit a block and the reconstruction path reuses these
        exact strings.
        """
        normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
        normalized = normalized.strip("\ufeff\n")
        if not normalized:
            return []
        return [x.strip("\n") for x in re.split(r"\n[ \t\u3000]*\n+", normalized) if x.strip("\n")]

    @staticmethod
    def _reader_payload_hash(blocks):
        # A separator-free hash proves that all non-separator content and its
        # order survived.  It intentionally ignores only paragraph blank lines.
        payload = "".join(str(x) for x in (blocks or []))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def _reader_locked_breaks(blocks):
        """Hard boundaries the model is not allowed to remove."""
        locked = set()
        for idx, raw in enumerate(blocks, 1):
            txt = str(raw or "").strip()
            compact_len = len(re.sub(r"\s+", "", txt))
            is_heading = bool(re.match(r"^#{1,6}\s+", txt)) or bool(
                idx == 1 and re.match(r"^(?:#{1,6}\s*)?第\s*\d+\s*章", txt)
            )
            is_separator = bool(re.fullmatch(r"(?:[-=_*·•]{3,}|…{2,}|\.\s*\.\s*\.)", txt))
            is_marker = txt.startswith(("<!--", "【", "[SCENE:", "[DLC:"))
            # A paragraph that begins as a direct speech turn stays independent.
            is_dialogue = txt.startswith(("“", "‘", "「", "『", "\"", "——"))
            is_long = compact_len >= 220
            if is_heading or is_separator or is_marker or is_dialogue or is_long:
                if idx > 1:
                    locked.add(idx - 1)
                locked.add(idx)
        if blocks:
            locked.add(len(blocks))
        return locked

    @staticmethod
    def _reader_breaks_from_response(obj, block_count):
        if not isinstance(obj, dict):
            raise ValueError("模型没有返回 JSON 对象")
        values = obj.get("break_after")
        if values is None and isinstance(obj.get("groups"), list):
            values = []
            for group in obj["groups"]:
                if isinstance(group, list) and group:
                    values.append(group[-1])
        if not isinstance(values, list):
            raise ValueError("模型没有返回 break_after 数组")
        breaks = set()
        for value in values:
            if isinstance(value, bool):
                raise ValueError("break_after 含布尔值")
            try:
                number = int(value)
            except Exception as e:
                raise ValueError(f"无效段落编号：{value}") from e
            if number < 1 or number > int(block_count):
                raise ValueError(f"段落编号越界：{number}")
            breaks.add(number)
        if block_count:
            breaks.add(int(block_count))
        return breaks

    @staticmethod
    def _reader_rebuild(blocks, break_after):
        paragraphs = []
        current = []
        for idx, block in enumerate(blocks, 1):
            current.append(block)
            if idx in break_after:
                paragraphs.append("".join(current))
                current = []
        if current:
            paragraphs.append("".join(current))
        return "\n\n".join(paragraphs).rstrip() + "\n"

    def _reader_router_for_thread(self):
        router = getattr(self._reader_router_local, "router", None)
        if router is None:
            router = LLMRouter(
                self.root, self.config_loader,
                on_metrics=self._on_reader_metrics, on_chunk=None,
                logger=self.log, stop_event=self.reader_stop_event,
            )
            self._reader_router_local.router = router
        return router

    def _reader_choose_breaks(self, n, blocks, locked):
        rendered = []
        for idx, block in enumerate(blocks, 1):
            tag = "LOCK" if idx in locked else "FREE"
            rendered.append(f"[P{idx:04d} {tag}]\n{block}")
        system = """你是中文网络小说排版编辑。你的唯一任务是决定现有短段落应在哪里合并。
不得改写、增删、纠错、调序或复述任何正文，只返回 JSON 段落边界。
LOCK 表示程序硬边界，必须在该段之后保留换段。
普通叙述尽量组成约80—180个汉字的自然段；同一动作、环境或心理可以合并。
不同人物的对话轮次、场景切换、时间跳转、标题、分隔符、独立强调句应保留边界。
只输出 JSON：{\"break_after\":[2,5,6]}。数组表示在哪些原段落编号之后换段。"""
        user = f"""请为第{int(n)}章重新安排段落边界。
共 {len(blocks)} 个原段落；最后一段必须出现在 break_after 中。
所有标为 LOCK 的边界都必须保留。不要输出正文或解释。

""" + "\n\n".join(rendered)
        last_error = ""
        for attempt in range(1, 3):
            if self.reader_stop_event.is_set():
                raise ProviderCancelledError("读者版智能分段已停止")
            prompt = user if attempt == 1 else user + f"\n\n上次返回无效：{last_error}。这次只返回合法 JSON。"
            raw, _spec = self._reader_router_for_thread().chat(
                "draft", system, prompt, 0.1, 1800, stream=False,
                label="reader_reflow", emit_text=False, routing_context="reader paragraph reflow",
                provider_override="deepseek", model_override="deepseek-v4-flash",
                thinking_override=False, response_format={"type": "json_object"},
                reasoning_effort_override="low", allow_local_fallback=False,
            )
            try:
                chosen = self._reader_breaks_from_response(_json_obj(raw), len(blocks))
                return chosen | set(locked)
            except Exception as e:
                last_error = str(e)
        raise ValueError(f"两次返回均无法解析：{last_error}")

    def _reader_process_one(self, n, overwrite=False):
        n = int(n)
        source = self.root / "chapters" / f"{n:04d}.md"
        output_dir = self.root / "reader_chapters"
        output = output_dir / f"{n:04d}.md"
        meta_path = output_dir / f"{n:04d}.json"
        if not source.is_file():
            return {"status": "failed", "chapter": n, "error": "Canon 章节不存在"}
        if output.is_file() and not overwrite:
            return {"status": "skipped", "chapter": n}
        original = source.read_text(encoding="utf-8")
        source_sha256 = hashlib.sha256(original.encode("utf-8")).hexdigest()
        blocks = self._reader_split_blocks(original)
        if not blocks:
            return {"status": "failed", "chapter": n, "error": "章节正文为空"}
        locked = self._reader_locked_breaks(blocks)
        if len(blocks) == 1:
            breaks = {1}
        else:
            breaks = self._reader_choose_breaks(n, blocks, locked)
        result = self._reader_rebuild(blocks, breaks)
        rebuilt_blocks = self._reader_split_blocks(result)
        before_payload = self._reader_payload_hash(blocks)
        after_payload = self._reader_payload_hash(rebuilt_blocks)
        if before_payload != after_payload:
            raise ValueError("逐字校验失败：分段结果改变了非换行内容")
        # Refuse a stale write if Canon changed while the API request was in flight.
        latest = source.read_text(encoding="utf-8")
        if hashlib.sha256(latest.encode("utf-8")).hexdigest() != source_sha256:
            raise ValueError("处理期间 Canon 原文发生变化，已拒绝写入")
        output_dir.mkdir(parents=True, exist_ok=True)
        temp = output_dir / f".{n:04d}.{threading.get_ident()}.tmp"
        temp.write_text(result, encoding="utf-8")
        temp.replace(output)
        meta = {
            "chapter_no": n,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "model": "deepseek-v4-flash",
            "thinking": False,
            "source_sha256": source_sha256,
            "payload_sha256": before_payload,
            "source_paragraphs": len(blocks),
            "reader_paragraphs": len(breaks),
            "break_after": sorted(breaks),
            "content_preserved": True,
        }
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return {
            "status": "completed", "chapter": n,
            "source_paragraphs": len(blocks), "reader_paragraphs": len(breaks),
        }

    def _run_reader_reflow(self, start, end, workers, overwrite):
        numbers = list(range(int(start), int(end) + 1))
        executor = ThreadPoolExecutor(max_workers=int(workers), thread_name_prefix="ReaderReflow")
        futures = {}
        try:
            with self.reader_lock:
                self.reader_status["stage"] = "智能分段"
                self.reader_status["stage_label"] = f"第{start}—{end}章"
            for n in numbers:
                if self.reader_stop_event.is_set():
                    break
                futures[executor.submit(self._reader_process_one, n, overwrite)] = n
            for future in as_completed(futures):
                n = futures[future]
                if self.reader_stop_event.is_set():
                    for pending in futures:
                        pending.cancel()
                    break
                try:
                    row = future.result()
                except ProviderCancelledError:
                    if self.reader_stop_event.is_set():
                        break
                    row = {"status": "failed", "chapter": n, "error": "请求被取消"}
                except Exception as e:
                    row = {"status": "failed", "chapter": n, "error": str(e)}
                with self.reader_lock:
                    self.reader_status["chapter"] = n
                    self.reader_status["item_done"] += 1
                    status = row.get("status")
                    if status == "completed":
                        self.reader_status["completed"] += 1
                        self.reader_status["last_output_chapter"] = n
                    elif status == "skipped":
                        self.reader_status["skipped"] += 1
                    else:
                        self.reader_status["failed"] += 1
                        message = f"第{n}章：{row.get('error') or '未知错误'}"
                        self.reader_status["last_error"] = message
                        errors = list(self.reader_status.get("errors") or [])
                        errors.append(message)
                        self.reader_status["errors"] = errors[-30:]
                if row.get("status") == "completed":
                    self.log(
                        f"读者版第 {n} 章完成：{row.get('source_paragraphs')} → "
                        f"{row.get('reader_paragraphs')} 段；逐字校验通过。"
                    )
        except Exception as e:
            with self.reader_lock:
                self.reader_status["last_error"] = str(e)
            self.log(f"读者版智能分段失败：{e}")
        finally:
            executor.shutdown(wait=True, cancel_futures=True)
            stopped = self.reader_stop_event.is_set()
            with self.reader_lock:
                self.reader_status["running"] = False
                self.reader_status["stage"] = "已停止" if stopped else "完成"
                self.reader_status["stage_label"] = "用户停止" if stopped else "批次完成"
            snap = self.reader_snapshot()
            self.log(
                f"读者版智能分段{'已停止' if stopped else '完成'}："
                f"成功 {snap.get('completed', 0)}，跳过 {snap.get('skipped', 0)}，"
                f"失败 {snap.get('failed', 0)}。"
            )

    def start_reader_reflow(self, start, end, workers=4, overwrite=False):
        start, end = int(start), int(end)
        workers = max(1, min(8, int(workers or 4)))
        if start < 1 or end < start or end - start > 10000:
            raise ValueError("智能分段章节范围无效")
        if self.status.get("running") or self.audit_snapshot().get("running") or self.repair_snapshot().get("running"):
            raise RuntimeError("Canon、剧情审计或审计修复正在运行")
        if self.dlc_snapshot().get("running"):
            raise RuntimeError("DLC 正在运行")
        with self.reader_lock:
            if self.reader_status.get("running"):
                raise RuntimeError("读者版智能分段已经在运行")
            self.reader_stop_event.clear()
            self._reader_router_local = threading.local()
            self.reader_status.update({
                "running": True, "start": start, "end": end, "chapter": None,
                "stage": "准备", "stage_label": "创建任务", "started_at": time.time(),
                "workers": workers, "overwrite": bool(overwrite),
                "item_total": end - start + 1, "item_done": 0,
                "completed": 0, "skipped": 0, "failed": 0,
                "last_error": "", "errors": [], "last_output_chapter": None,
                "prompt_tokens": 0, "cache_hit_tokens": 0,
                "completion_tokens": 0, "reasoning_tokens": 0,
                "cost_cny": 0.0, "afp": 0.0, "request_count": 0,
            })
            self.reader_thread = threading.Thread(
                target=self._run_reader_reflow,
                args=(start, end, workers, bool(overwrite)),
                name="NovelAgentReaderReflow", daemon=True,
            )
            self.reader_thread.start()
        return self.reader_snapshot()

    def stop_reader_reflow(self):
        self.reader_stop_event.set()
        with self.reader_lock:
            if self.reader_status.get("running"):
                self.reader_status["stage_label"] = "正在停止；等待当前请求结束"
        return self.reader_snapshot()

    def reader_chapter_detail(self, n):
        n = int(n)
        source = self.root / "chapters" / f"{n:04d}.md"
        reader = self.root / "reader_chapters" / f"{n:04d}.md"
        meta_path = self.root / "reader_chapters" / f"{n:04d}.json"
        if not reader.is_file():
            raise FileNotFoundError(f"第 {n} 章尚无读者版")
        meta = {}
        if meta_path.is_file():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                meta = {}
        return {
            "ok": True, "chapter_no": n,
            "original": source.read_text(encoding="utf-8") if source.is_file() else "",
            "reader": reader.read_text(encoding="utf-8"), "meta": meta,
        }

    # ---------------- fact-preserving expansion / polish ----------------
    def chapter_detail(self, n):
        n = int(n)
        dbrow = self.db.get_chapter(n) or {}
        final_path = self.root / "chapters" / f"{n:04d}.md"
        plan_path = self.root / "plans" / f"{n:04d}.md"
        review_path = self.root / "reviews" / f"{n:04d}.json"
        return {
            "chapter_no": n,
            "final": final_path.read_text(encoding="utf-8") if final_path.exists() else dbrow.get("final", ""),
            "plan": plan_path.read_text(encoding="utf-8") if plan_path.exists() else dbrow.get("plan", ""),
            "review": review_path.read_text(encoding="utf-8") if review_path.exists() else dbrow.get("review", ""),
            "summary": dbrow.get("summary", ""),
        }

    def edit_chapter(self, n, mode="expand", target_chars=0, instruction="",
                     provider="auto", model="", thinking=None):
        n = int(n)
        with self.lock:
            if self.status.get("running"):
                raise RuntimeError("Agent 运行中不能编辑历史章节")
        detail = self.chapter_detail(n)
        original = detail.get("final", "")
        if not original.strip():
            raise FileNotFoundError(f"第 {n} 章正文不存在")
        if mode not in {"expand", "rewrite", "polish", "minor"}:
            raise ValueError("edit mode must be expand, rewrite, polish or minor")
        mode_label = {"expand": "扩写", "rewrite": "改写", "polish": "润色", "minor": "小幅重修"}[mode]
        self._stage(n, "章节编辑", f"{mode_label}第 {n} 章")
        historical_state = self.format_current_state(n-1)
        context = f"""【故事核心】\n{self.read_story('premise.md')}\n
【世界观】\n{self.read_story('world.md')}\n
【初始人物】\n{self.read_story('characters_seed.md')}\n
【第{n}章附近大纲】\n{self.outline_context(n)}\n
【第{n}章开始前状态】\n{historical_state}\n
【原章节计划】\n{detail.get('plan','')}\n"""
        if mode == "expand":
            system = """你是小说扩写编辑。保持所有剧情事实、因果、事件结果、人物知识状态、人物关系结果、物品状态和地点状态不变。
允许增加对白、心理、动作、环境、过渡和感官细节。不得新增重大剧情或改变原结局。只输出完整扩写正文。"""
            task = f"扩写第{n}章到约 {int(target_chars or max(len(original), 4500))} 汉字。"
        elif mode == "rewrite":
            system = """你是长篇小说单章改写编辑。改写对象仅限当前这一章，不回档、不删除后续章节。
必须保持已经发生的核心剧情事实、事件结果、人物知识边界、人物关系结果、物品状态、地点状态，以及后续章节已经依赖的既成状态不变。
允许根据用户要求较大幅度重组当前章的叙述顺序、场景衔接、对白、心理、动作和节奏，也可以重写表达很差的段落。
章节按事件或关系推进划分，不默认按自然日完整展开；无变化的通勤、作息、练功、写作业和睡觉应压缩或跳过，不以“回家—关灯—睡觉”作为默认结尾。不得为了避免单日而强行添加日期或新剧情。
不得新增会改变后续剧情的新事件、新伏笔、新人物关系结果，不得改变本章最终落点。只输出完整改写正文。"""
            if int(target_chars or 0) > 0:
                task = f"按额外要求改写第{n}章，目标约 {int(target_chars)} 汉字。"
            else:
                task = f"按额外要求改写第{n}章，字数可围绕原文自然调整。"
        elif mode == "polish":
            system = """你是小说润色编辑。所有事实、事件顺序和状态必须完全不变，只改善表达、节奏、重复、对白自然度和文风一致性。只输出完整润色正文。"""
            task = f"润色第{n}章，字数尽量接近原文。"
        else:
            system = """你是长篇小说的小幅度重修编辑。只针对用户给出的局部问题做最小必要修正，例如时间线表述、连续性衔接、知识边界、局部遗漏、前后矛盾或少量需要补足的承接。
必须保持章节核心事件、主要场景顺序、事件结果、人物关系结果以及后续章节已经依赖的既成状态不变。允许替换少量错误句子、补一两句或一个短段、删除局部错误；禁止借机新增主线、改写章节走向、制造新伏笔或大段重写。只输出完整修订后的正文。"""
            task = f"按额外要求对第{n}章做小幅度重修；没有被要求修改的部分尽量保持原样，字数无需刻意变化。"
        user = context + f"""\n【原正文】\n{original}\n
【额外要求】\n{instruction or '无'}\n
【任务】\n{task}\n"""
        g = self.config_loader()["generation"]
        chosen_provider = None if provider == "auto" else provider
        candidate = self._chat(
            "draft", system, user, g["temperatures"]["draft"], max(g["max_tokens"]["draft"], 9000),
            True, mode, True, routing_context=detail.get("plan", ""),
            provider_override=chosen_provider, model_override=model or None,
            thinking_override=thinking,
        )

        # Independent structured safety check. Minor revision may repair the requested
        # local issue, but must not create unrelated downstream fact changes.
        if mode == "minor":
            check_system = """你是长篇小说局部修订一致性检查器。比较原文、用户修订要求与编辑版。只允许为完成指定修复所必需的局部变化；禁止产生无关剧情事实变化。必须输出 JSON。"""
            check_user = f"""【用户修订要求】
{instruction or '无明确要求'}

【原文】
{original}

【编辑版】
{candidate}

输出：
{{"requested_fixes_applied": true/false, "unrelated_fact_changes": true/false, "changes": ["实际修改"], "safe_to_accept": true/false}}
允许：为修正指定的时间线、连续性、知识边界、局部遗漏或前后矛盾而进行最小必要变化。
禁止：新增主线、改变核心事件结果、改变人物关系结果、制造后续章节尚未存在的新状态，或对无关段落大范围重写。
"""
        else:
            check_system = """你是小说版本一致性检查器。比较原文与编辑版，只判断编辑版是否改变了剧情事实或状态。必须输出 JSON。"""
            check_user = f"""【原文】
{original}

【编辑版】
{candidate}

输出：
{{"fact_changes": true/false, "changes": ["具体改变"], "safe_to_accept": true/false}}
对白措辞、环境细节、心理描写增加不算事实改变；新增事件、改变结果、知识边界、关系结果、物品/地点/伤病状态算改变。
"""
        raw = self._chat("review", check_system, check_user, 0.1, 1800, False, "edit_review", False, response_format={"type": "json_object"})
        check = _json_obj(raw, {"fact_changes": True, "changes": ["一致性检查 JSON 解析失败"], "safe_to_accept": False})
        candidate_path = self.root / "chapters" / f"{n:04d}.{mode}.candidate.md"
        candidate_path.write_text(candidate.strip() + "\n", encoding="utf-8")
        meta = {
            "chapter_no": n, "mode": mode, "created_at": datetime.now().isoformat(timespec="seconds"),
            "provider_requested": provider, "model_requested": model, "thinking_requested": thinking,
            "target_chars": int(target_chars or 0), "instruction": instruction, "review": check,
        }
        (self.root / "chapters" / f"{n:04d}.{mode}.candidate.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        self._stage(None, "空闲", "")
        return {"ok": True, "candidate": candidate, "review": check, "candidate_file": str(candidate_path)}

    def chapter_candidate_detail(self, n, mode):
        n = int(n)
        mode = str(mode or "").strip()
        if mode not in {"expand", "rewrite", "polish", "minor"}:
            raise ValueError("mode invalid")
        original = self.chapter_detail(n).get("final", "")
        cp = self.root / "chapters" / f"{n:04d}.{mode}.candidate.md"
        mp = self.root / "chapters" / f"{n:04d}.{mode}.candidate.json"
        if not cp.exists():
            raise FileNotFoundError(f"第 {n} 章的 {mode} 候选不存在")
        meta = {}
        if mp.exists():
            try:
                meta = json.loads(mp.read_text(encoding="utf-8"))
            except Exception:
                meta = {}
        return {
            "ok": True,
            "chapter_no": n,
            "mode": mode,
            "original": original,
            "candidate": cp.read_text(encoding="utf-8"),
            "meta": meta,
            "review": meta.get("review") or {},
        }

    def accept_candidate(self, n, mode):
        n = int(n)
        if mode not in {"expand", "rewrite", "polish", "minor"}:
            raise ValueError("mode invalid")
        cp = self.root / "chapters" / f"{n:04d}.{mode}.candidate.md"
        mp = self.root / "chapters" / f"{n:04d}.{mode}.candidate.json"
        if not cp.exists() or not mp.exists():
            raise FileNotFoundError("候选版本不存在")
        meta = json.loads(mp.read_text(encoding="utf-8"))
        review = meta.get("review") or {}
        if mode == "minor":
            if review.get("unrelated_fact_changes") or review.get("safe_to_accept") is False:
                raise RuntimeError("小幅重修候选被检测到无关剧情事实变化；请缩小修改要求或改用“从本章重写”。")
        elif review.get("fact_changes") or review.get("safe_to_accept") is False:
            raise RuntimeError("候选版本被检测到剧情事实变化；请改用“从本章重写”而不是强行接受扩写/润色。")
        final = self.root / "chapters" / f"{n:04d}.md"
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        ed = self.root / "archive" / f"edit_{n:04d}_{stamp}"
        ed.mkdir(parents=True, exist_ok=True)
        if final.exists():
            shutil.copy2(final, ed / final.name)
        text = cp.read_text(encoding="utf-8")
        task_card = self.chapter_task_card(n)
        plan_text = self.read(f"plans/{n:04d}.md") or f"人工 {mode} 候选，保持原 Canon 任务边界"
        final_review = self.review_chapter(
            n, plan_text, text, task_card,
            final_gate=True, prior_review=review,
        )
        if str(final_review.get("severity", "PASS")).upper() != "PASS" or final_review.get("needs_revision"):
            raise FinalQualityGateError("人工候选未通过新的 Canon 最终质量门，未覆盖正文")
        summary, memory_records, handoff, handoff_error = self.summarize_and_extract_memories(n, text)
        if (
            handoff_error or handoff.get("status") != "complete"
            or handoff.get("structured_complete") is not True
            or not handoff.get("scene_signatures")
        ):
            raise CanonCommitError(f"人工候选 handoff 提取失败，未提交：{handoff_error}")
        self._commit_canon_bundle(
            n, plan=plan_text, draft=text, final_review=final_review,
            final=text, summary=summary, memories=memory_records,
            handoff=handoff, generation_seconds=0, revision_seconds=0,
            honor_stop=False,
        )
        cp.unlink(missing_ok=True); mp.unlink(missing_ok=True)
        return {"ok": True, "chapter_no": n, "chars": len(text.strip()), "archive": str(ed)}
