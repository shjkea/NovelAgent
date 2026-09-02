
import io
import json
import hashlib
import hmac
import urllib.error
import urllib.request
import re
import sqlite3
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from urllib.parse import urlparse, urlencode
import uvicorn

from agent_core import EventHub, NovelAgent
from external_canon import ExternalCanonError
from provider_router import (deepseek_price_status, calculate_deepseek_cost, calculate_volcengine_afp, VOLCENGINE_AGENT_PLAN_BASE, CST)
from auth_manager import AuthManager
from embedding_manager import EmbeddingManager
from secret_store import save_secret, load_secret
from md_manager import MdManagerError, commit as md_commit, parse_route as md_parse_route, preview as md_preview, route_dict as md_route_dict

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "config.json"
CONFIG_EXAMPLE = ROOT / "config.example.json"
VERSION = "5.8.0"

DEFAULT_V3 = {
    "embedding_server": {
        "auto_start": True,
        "llama_server_path": "C:\\llama.cpp\\llama-server.exe",
        "model_path": "C:\\Models\\Qwen3-Embedding-0.6B\\Qwen3-Embedding-0.6B-Q8_0.gguf",
        "host": "127.0.0.1",
        "port": 8081,
        "alias": "novel-embed",
        "threads": 4,
        "context": 32768,
        "ubatch": 1024,
        "pooling": "last"
    },
    "deepseek": {
        "source": "official",
        "base_url": "https://api.deepseek.com",
        "api_key_store": "runtime/deepseek_api_key.dpapi",
        "active_slot": "1",
        "api_slots": {
            "1": {"label": "API 1", "api_key_store": "runtime/deepseek_api_key.dpapi"},
            "2": {"label": "API 2", "api_key_store": "runtime/deepseek_api_key_2.dpapi"},
            "3": {"label": "API 3", "api_key_store": "runtime/deepseek_api_key_3.dpapi"},
            "4": {"label": "API 4", "api_key_store": "runtime/deepseek_api_key_4.dpapi"},
            "5": {"label": "API 5", "api_key_store": "runtime/deepseek_api_key_5.dpapi"}
        },
        "volcengine_agent_plan": {
            "base_url": "https://ark.cn-beijing.volces.com/api/plan/v3",
            "api_key_store": "runtime/volcengine_agent_plan_key.dpapi",
            "active_slot": "1",
            "api_slots": {
                "1": {"label": "API 1", "api_key_store": "runtime/volcengine_agent_plan_key.dpapi"},
                "2": {"label": "API 2", "api_key_store": "runtime/volcengine_agent_plan_key_2.dpapi"},
                "3": {"label": "API 3", "api_key_store": "runtime/volcengine_agent_plan_key_3.dpapi"},
                "4": {"label": "API 4", "api_key_store": "runtime/volcengine_agent_plan_key_4.dpapi"},
                "5": {"label": "API 5", "api_key_store": "runtime/volcengine_agent_plan_key_5.dpapi"}
            }
        },
        "retry_attempts": 4,
        "empty_content_retries": 1,
        "connect_timeout_seconds": 15,
        "read_timeout_seconds": 300,
        "max_request_seconds": 900,
        "min_thinking_max_tokens": 12000,
        "plan_reasoning_effort": "low",
        "plan_length_retry_attempts": 1,
        "plan_length_retry_max_tokens": 16000
    },
    "grok": {
        "base_url": "https://api.x.ai/v1",
        "api_source": "xai",
        "api_label": "xAI",
        "api_key_store": "runtime/grok_api_key.dpapi",
        "model": "grok-4.6",
        "review_model": "grok-4.6",
        "reasoning_effort": "low",
        "review_reasoning_effort": "medium",
        "send_reasoning_effort": True,
        "send_response_format": True,
        "send_stream_options": True,
        "compatibility_fallback": True,
        "retry_attempts": 3,
        "connect_timeout_seconds": 15,
        "read_timeout_seconds": 300,
        "max_request_seconds": 900
    },
    "canon": {
        "deepseek_only": True,
        "stage_models": {
            "plan": {"model": "deepseek-v4-pro", "thinking": True, "reasoning_effort": "low"},
            "draft": {"model": "deepseek-v4-flash", "thinking": False, "reasoning_effort": "low"},
            "review": {"model": "deepseek-v4-flash", "thinking": True, "reasoning_effort": "low"},
            "deep_review": {"model": "deepseek-v4-pro", "thinking": True, "reasoning_effort": "high"},
            "revision": {"model": "deepseek-v4-flash", "thinking": False, "reasoning_effort": "low"},
            "summary": {"model": "deepseek-v4-flash", "thinking": False, "reasoning_effort": "low"},
            "memory": {"model": "deepseek-v4-flash", "thinking": False, "reasoning_effort": "low"}
        }
    },
    "cost_control": {
        "profile": "enhanced",
        "peak_warning": True,
        "deep_review_on_risk": True,
        "memory_mode": "deepseek",
        "high_context_mode_enabled": False,
        "high_context_target_tokens": 120000,
        "high_context_max_tokens": 127000,
        "plan_context_trim_enabled": True,
        "plan_context_target_tokens": 30000,
        "plan_context_safe_tokens": 31800,
        "plan_context_recovery_target_tokens": 38000,
        "plan_context_recovery_max_tokens": 42000,
        # Before starting a chapter, inspect the actual whole-chapter spend of
        # the preceding completed chapters.  The sixth over-limit chapter in
        # the rolling ten-chapter window makes the next chapter ask first.
        "chapter_cost_guard_mode": "afp",
        "chapter_cost_guard_afp_limit": 20.0,
        "chapter_cost_guard_cny_limit": 5.0,
        "chapter_cost_guard_window_chapters": 10,
        "chapter_cost_guard_confirm_at_chapters": 6,
        "canon_context_trim_enabled": True,
        "canon_context_safe_margin_tokens": 5000,
        "canon_context_budgets": {
            "draft": {"normal": 40000, "medium": 50000, "complex": 62000},
            "review": {"normal": 50000, "medium": 65000, "complex": 80000},
            "deep_review": {"normal": 60000, "medium": 75000, "complex": 95000},
            "revision": {"normal": 45000, "medium": 58000, "complex": 72000}
        },
        "canon_context_min_state_rows": {
            "draft": 16,
            "review": 24,
            "deep_review": 32,
            "revision": 24
        },
        "chapter_complexity": {
            "medium_score": 4,
            "complex_score": 8,
            "return_gap_chapters": 20,
            "old_memory_chapters": 80,
            "very_old_memory_chapters": 150
        }
    },
    "dlc": {
        "manual_only": True,
        "provider": "grok",
        "model": "grok-4.6",
        "review_enabled": True,
        "review_model": "grok-4.6",
        "reasoning_effort": "low",
        "review_reasoning_effort": "medium",
        "temperature": 0.4,
        "max_tokens": 3200,
        "atlas_file": "prompts/expansion_reference.md",
        "atlas_max_chars": 9000,
        "character_max_chars": 12000,
        "style_file": "style.md",
        "style_max_chars": 7000,
        "review_retry_count": 1
    },
    "routing": {
        "mode": "all_deepseek",
        "stages": {
            "plan": {"provider":"deepseek","model":"deepseek-v4-pro","thinking":True,"reasoning_effort":"low"},
            "draft": {"provider":"deepseek","model":"deepseek-v4-flash","thinking":False},
            "review": {"provider":"deepseek","model":"deepseek-v4-pro","thinking":True,"reasoning_effort":"high"},
            "revision": {"provider":"deepseek","model":"deepseek-v4-flash","thinking":False},
            "summary": {"provider":"deepseek","model":"deepseek-v4-flash","thinking":False},
            "memory": {"provider":"deepseek","model":"deepseek-v4-flash","thinking":False}
        },
        "presets": {
            "recommended": {
                "plan":{"provider":"deepseek","model":"deepseek-v4-pro","thinking":True,"reasoning_effort":"low"},
                "draft":{"provider":"deepseek","model":"deepseek-v4-flash","thinking":False},
                "review":{"provider":"deepseek","model":"deepseek-v4-pro","thinking":True,"reasoning_effort":"high"},
                "revision":{"provider":"deepseek","model":"deepseek-v4-flash","thinking":False},
                "summary":{"provider":"deepseek","model":"deepseek-v4-flash","thinking":False},
                "memory":{"provider":"deepseek","model":"deepseek-v4-flash","thinking":False}
            },
            "all_deepseek": {
                "plan":{"provider":"deepseek","model":"deepseek-v4-pro","thinking":True},
                "draft":{"provider":"deepseek","model":"deepseek-v4-flash","thinking":False},
                "review":{"provider":"deepseek","model":"deepseek-v4-pro","thinking":True},
                "revision":{"provider":"deepseek","model":"deepseek-v4-flash","thinking":False},
                "summary":{"provider":"deepseek","model":"deepseek-v4-flash","thinking":False},
                "memory":{"provider":"deepseek","model":"deepseek-v4-flash","thinking":False}
            }
        }
    },
    "context": {
        "outline_neighbor_chapters": 3,
        "plan_stage_lookbehind_chapters": 3,
        "plan_stage_lookahead_chapters": 8,
        "plan_stage_outline_max_chars": 18000,
        "outline_legacy_max_chars": 24000,
        "outline_range_overrides": [],
    },
    "continuity": {
        "source_tail_chars": 2600,
        "handoff_max_chars": 12000,
        "future_boundary_max_chars": 5000,
        "audit_window_chapters": 4,
        "audit_window_overlap": 1
    },
    "external_canon": {
        "enabled": False,
        "read_outline_markers": False,
        "ranges": [],
        "max_zip_bytes": 134217728,
        "max_chapter_bytes": 2097152,
        "max_total_bytes": 268435456
    },
    "writing_guardrails": {
        "enabled": True,
        "task_card": True,
        "provider_specific": True,
        "major_drift_full_rewrite": True,
        "revision_regression_retries": 1,
        "plan_contract_check": True,
        "plan_contract_retries": 2,
        "plan_stage_contract_model": "deepseek-v4-flash",
        "plan_contract_check_model": "deepseek-v4-flash",
        "silent_constraints": True,
        "recent_fulltext_chapters": 3,
        "recent_fulltext_max_chars": 30000,
        "light_scene_sufficiency": True,
        "soft_style_repetition": True,
        "canon_commit_verification": True,
    },
}

COST_PROFILES = {
    "quality": {
        "label": "质量优先",
        "deep_review_on_risk": True,
        "memory_mode": "deepseek",
        "stages": {
            "plan": {"model":"deepseek-v4-pro","thinking":True,"reasoning_effort":"low"},
            "draft": {"model":"deepseek-v4-flash","thinking":False,"reasoning_effort":"low"},
            "review": {"model":"deepseek-v4-pro","thinking":True,"reasoning_effort":"low"},
            "deep_review": {"model":"deepseek-v4-pro","thinking":True,"reasoning_effort":"high"},
            "revision": {"model":"deepseek-v4-flash","thinking":False,"reasoning_effort":"low"},
            "summary": {"model":"deepseek-v4-flash","thinking":False,"reasoning_effort":"low"},
            "memory": {"model":"deepseek-v4-flash","thinking":False,"reasoning_effort":"low"},
        },
    },
    "enhanced": {
        "label": "均衡增强（默认）",
        "deep_review_on_risk": True,
        "memory_mode": "deepseek",
        "stages": {
            "plan": {"model":"deepseek-v4-pro","thinking":True,"reasoning_effort":"low"},
            "draft": {"model":"deepseek-v4-flash","thinking":False,"reasoning_effort":"low"},
            "review": {"model":"deepseek-v4-flash","thinking":True,"reasoning_effort":"low"},
            "deep_review": {"model":"deepseek-v4-pro","thinking":True,"reasoning_effort":"high"},
            "revision": {"model":"deepseek-v4-flash","thinking":False,"reasoning_effort":"low"},
            "summary": {"model":"deepseek-v4-flash","thinking":False,"reasoning_effort":"low"},
            "memory": {"model":"deepseek-v4-flash","thinking":False,"reasoning_effort":"low"},
        },
    },
    "balanced": {
        "label": "均衡省钱",
        "deep_review_on_risk": True,
        "memory_mode": "deepseek",
        "stages": {
            "plan": {"model":"deepseek-v4-flash","thinking":True,"reasoning_effort":"low"},
            "draft": {"model":"deepseek-v4-flash","thinking":False,"reasoning_effort":"low"},
            "review": {"model":"deepseek-v4-flash","thinking":True,"reasoning_effort":"low"},
            "deep_review": {"model":"deepseek-v4-pro","thinking":True,"reasoning_effort":"high"},
            "revision": {"model":"deepseek-v4-flash","thinking":False,"reasoning_effort":"low"},
            "summary": {"model":"deepseek-v4-flash","thinking":False,"reasoning_effort":"low"},
            "memory": {"model":"deepseek-v4-flash","thinking":False,"reasoning_effort":"low"},
        },
    },
    "saving": {
        "label": "省钱优先",
        "deep_review_on_risk": True,
        "memory_mode": "deepseek",
        "stages": {
            "plan": {"model":"deepseek-v4-flash","thinking":True,"reasoning_effort":"low"},
            "draft": {"model":"deepseek-v4-flash","thinking":False,"reasoning_effort":"low"},
            "review": {"model":"deepseek-v4-flash","thinking":False,"reasoning_effort":"low"},
            "deep_review": {"model":"deepseek-v4-pro","thinking":True,"reasoning_effort":"high"},
            "revision": {"model":"deepseek-v4-flash","thinking":False,"reasoning_effort":"low"},
            "summary": {"model":"deepseek-v4-flash","thinking":False,"reasoning_effort":"low"},
            "memory": {"model":"deepseek-v4-flash","thinking":False,"reasoning_effort":"low"},
        },
    },
}

def _apply_cost_profile(cfg, profile):
    if profile not in COST_PROFILES:
        raise ValueError(f"未知成本策略：{profile}")
    p = COST_PROFILES[profile]
    cfg.setdefault("cost_control", {})["profile"] = profile
    cfg["cost_control"]["deep_review_on_risk"] = bool(p["deep_review_on_risk"])
    cfg["cost_control"]["memory_mode"] = p["memory_mode"]
    cfg.setdefault("canon", {})["deepseek_only"] = True
    cfg["canon"]["stage_models"] = json.loads(json.dumps(p["stages"]))
    cfg.setdefault("deepseek", {})["plan_reasoning_effort"] = cfg["canon"]["stage_models"]["plan"]["reasoning_effort"]
    return cfg

def _deep_merge(dst, src):
    changed = False
    for k, v in src.items():
        if k not in dst:
            dst[k] = v
            changed = True
        elif isinstance(v, dict) and isinstance(dst[k], dict):
            changed |= _deep_merge(dst[k], v)
    return changed

def load_config():
    if CONFIG.exists():
        cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    elif CONFIG_EXAMPLE.exists():
        cfg = json.loads(CONFIG_EXAMPLE.read_text(encoding="utf-8"))
        CONFIG.write_text(
            json.dumps(cfg, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    else:
        cfg = json.loads(json.dumps(DEFAULT_V3))
        CONFIG.write_text(
            json.dumps(cfg, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    had_cost_control = "cost_control" in cfg
    changed = _deep_merge(cfg, DEFAULT_V3)

    # V5.5.3 separates the automatic compression target from the confirmation
    # threshold. Upgrade the exact V5.5.2 pair while preserving custom values.
    cost_cfg = cfg.setdefault("cost_control", {})
    try:
        old_plan_target = int(cost_cfg.get("plan_context_target_tokens", 30000))
        old_plan_safe = int(cost_cfg.get("plan_context_safe_tokens", 31000))
    except Exception:
        old_plan_target, old_plan_safe = 30000, 31000
    if (old_plan_target, old_plan_safe) == (28500, 30000):
        cost_cfg["plan_context_target_tokens"] = 30000
        cost_cfg["plan_context_safe_tokens"] = 31000
        changed = True
    # V5.8 uses 31.8K as the low-price safety line. The configurable
    # 38K/42K recovery band handles prompts that cannot reach it.
    if int(cost_cfg.get("plan_context_safe_tokens", 31800) or 31800) in {31000, 31800}:
        if int(cost_cfg.get("plan_context_safe_tokens", 31800) or 31800) != 31800:
            cost_cfg["plan_context_safe_tokens"] = 31800
            changed = True
    for obsolete in (
        "plan_overflow_auto_window_size", "plan_overflow_auto_allowed",
        "plan_overflow_auto_min_tokens", "plan_overflow_auto_max_tokens",
        "plan_overflow_history_window_chapters", "plan_overflow_history_confirm_at_chapters",
        "plan_overflow_history_min_tokens", "plan_overflow_history_max_tokens",
    ):
        if obsolete in cost_cfg:
            cost_cfg.pop(obsolete, None)
            changed = True
    if "plan_context_send_reserve_tokens" in cost_cfg:
        cost_cfg.pop("plan_context_send_reserve_tokens", None)
        changed = True

    # Normalize legacy embedding and model defaults once while preserving
    # user-selected provider endpoints.
    migration_cfg = cfg.setdefault("_migrations", {})
    if not bool(migration_cfg.get("v5_8_public_defaults", False)):
        embed_cfg = cfg.setdefault("embedding_server", {})
        try:
            old_ubatch = int(embed_cfg.get("ubatch", 1024) or 1024)
        except Exception:
            old_ubatch = 1024
        if old_ubatch in {512, 2048}:
            embed_cfg["ubatch"] = 1024
        grok_cfg = cfg.setdefault("grok", {})
        dlc_cfg = cfg.setdefault("dlc", {})
        for node in (grok_cfg, dlc_cfg):
            if str(node.get("model", "")) in {"grok-4.3", "grok-4.5"}:
                node["model"] = "grok-4.6"
            if str(node.get("review_model", "")) in {"grok-4.3", "grok-4.5"}:
                node["review_model"] = "grok-4.6"
        migration_cfg["v5_8_public_defaults"] = True
        changed = True

    # V5.0 long-thinking requests (especially Volcengine Agent Plan + Pro)
    # may legitimately stay silent for more than the old 90-second read timeout.
    # Upgrade only the legacy 90s-or-lower value; preserve any user's larger
    # custom timeout.
    ds_cfg = cfg.setdefault("deepseek", {})
    # V5.5.1 retires the 8080 text model. Purge legacy launch/routing keys so
    # upgrading app.py without manually cleaning an older config is still safe.
    for legacy_key in ("llm", "management"):
        if legacy_key in cfg:
            cfg.pop(legacy_key, None)
            changed = True
    if "fallback_to_local" in ds_cfg:
        ds_cfg.pop("fallback_to_local", None)
        changed = True
    routing_cfg = cfg.setdefault("routing", {})
    if routing_cfg.get("mode") in {"all_local", "auto_nsfw"}:
        routing_cfg["mode"] = "all_deepseek"
        changed = True
    presets = routing_cfg.setdefault("presets", {})
    for legacy_preset in ("all_local", "auto_nsfw"):
        if legacy_preset in presets:
            presets.pop(legacy_preset, None)
            changed = True
    if "auto_nsfw" in routing_cfg:
        routing_cfg.pop("auto_nsfw", None)
        changed = True
    for stage_cfg in (routing_cfg.get("stages", {}) or {}).values():
        if isinstance(stage_cfg, dict) and stage_cfg.get("provider") == "local":
            stage_cfg.update({"provider": "deepseek", "model": "deepseek-v4-flash", "thinking": False})
            changed = True
    try:
        _read_timeout = float(ds_cfg.get("read_timeout_seconds", 300) or 300)
    except Exception:
        _read_timeout = 300.0
    if _read_timeout <= 90:
        ds_cfg["read_timeout_seconds"] = 300
        changed = True

    # First upgrade from <=3.1.2: apply the new balanced cost preset once.
    if not had_cost_control:
        _apply_cost_profile(cfg, "enhanced")
        changed = True

    # Named cost profiles are authoritative.  If an older config says e.g.
    # profile=enhanced but still contains stale per-stage routes (such as Review=Pro),
    # repair the stage routes from the named preset.  Only profile=custom may own
    # independent per-stage values.
    cc = cfg.setdefault("cost_control", {})
    if str(cc.get("memory_mode", "deepseek")) == "auto_local":
        cc["memory_mode"] = "deepseek"
        changed = True
    profile = str(cc.get("profile", "enhanced"))
    if profile in COST_PROFILES:
        expected = COST_PROFILES[profile]
        current_stages = cfg.get("canon", {}).get("stage_models", {})
        profile_mismatch = (
            current_stages != expected["stages"]
            or bool(cc.get("deep_review_on_risk", True)) != bool(expected["deep_review_on_risk"])
            or str(cc.get("memory_mode", "deepseek")) != str(expected["memory_mode"])
        )
        if profile_mismatch:
            _apply_cost_profile(cfg, profile)
            changed = True

    if changed:
        save_config(cfg)
    return cfg

def save_config(cfg):
    CONFIG.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

# Ensure config migration before managers instantiate.
_ = load_config()

hub = EventHub()
agent = NovelAgent(ROOT, load_config, hub)
agent.reload_clients()
embedding_models = EmbeddingManager(ROOT, load_config, save_config)
auth = AuthManager(ROOT, session_days=7)
embedding_models.ensure_started_async()

app = FastAPI(title=f"NovelAgent V{VERSION}", docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")

class AuthCredentials(BaseModel):
    username: str
    password: str

class ConfigPatch(BaseModel):
    chapters_per_run: int
    target_chapter_chars: int
    recent_summary_count: int
    max_revision_rounds: int = 1
    retrieval_top_k: int = 14
    retrieval_min_score: float = 0.22
    plan_context_trim_enabled: bool = True
    plan_context_recovery_target_tokens: int = 38000
    plan_context_recovery_max_tokens: int = 42000
    chapter_cost_guard_mode: str = "afp"
    chapter_cost_guard_afp_limit: float = 20.0
    chapter_cost_guard_cny_limit: float = 5.0

class LivePatch(BaseModel):
    enabled: bool

class HighContextPatch(BaseModel):
    enabled: bool

class DeepSeekKeyPatch(BaseModel):
    api_key: str
    source: str | None = None
    slot: str | None = None

class GrokKeyPatch(BaseModel):
    api_key: str

class GrokConfigPatch(BaseModel):
    base_url: str = "https://api.x.ai/v1"
    model: str = "grok-4.6"
    review_model: str = "grok-4.6"
    reasoning_effort: str = "low"
    review_reasoning_effort: str = "medium"
    review_enabled: bool = True

class DeepSeekProviderPatch(BaseModel):
    source: str

class DeepSeekAccountPatch(BaseModel):
    slot: str
    source: str | None = None

class VolcengineOpenApiPatch(BaseModel):
    access_key: str
    secret_key: str
    slot: str | None = None

class RoutingPatch(BaseModel):
    mode: str
    stages: dict | None = None

class CanonRoutingPatch(BaseModel):
    stages: dict

class CostProfilePatch(BaseModel):
    profile: str

class RewritePatch(BaseModel):
    chapter_no: int

class ChapterEditPatch(BaseModel):
    chapter_no: int
    mode: str = "expand"
    target_chars: int = 0
    instruction: str = ""
    provider: str = "auto"
    model: str = ""
    thinking: bool | None = None

class CandidateAcceptPatch(BaseModel):
    chapter_no: int
    mode: str

class DLCGeneratePatch(BaseModel):
    chapter_no: int
    scene_id: str
    custom_prompt: str = ""
    max_tokens: int = 0
    draw_count: int = 1

class DLCCandidatePatch(BaseModel):
    chapter_no: int
    scene_id: str
    candidate_id: str

class PromptSavePatch(BaseModel):
    name: str
    content: str

class MDManagerTextPatch(BaseModel):
    text: str

class MDManagerCommitPatch(BaseModel):
    text: str
    before_hash: str

class AuditStartPatch(BaseModel):
    start: int
    end: int
    segment_size: int = 4
    source_check: bool = True


class AuditRepairPlanPatch(BaseModel):
    audit_text: str = ""
    model: str = "deepseek-v4-pro"

class AuditRepairStartPatch(BaseModel):
    batch_id: str = ""
    model: str = "deepseek-v4-pro"

class AuditRepairCommitPatch(BaseModel):
    batch_id: str = ""
    chapters: list[int] | None = None
    manual: bool = False
    manual_confirmed: bool = False
    force: bool = False
    forced: bool = False
    confirm: str = ""
    force_reason: str = ""
    mode: str = ""

class AuditRepairBatchPatch(BaseModel):
    batch_id: str = ""

class ReaderReflowStartPatch(BaseModel):
    start: int
    end: int
    workers: int = 4
    overwrite: bool = False

SESSION_COOKIE = "novelagent_session"

def _client_ip(request: Request):
    # Do not trust arbitrary forwarded-IP headers. Direct LAN and SSH/FRP
    # tunnel access work correctly with request.client.host.
    return request.client.host if request.client else "unknown"

def _session_from_request(request: Request):
    return auth.validate_session(request.cookies.get(SESSION_COOKIE, ""))

def _check_same_origin(request: Request):
    # Browsers include Origin on fetch/XHR state-changing requests.
    # Local CLI requests may omit it and remain usable.
    origin = request.headers.get("origin")
    if not origin:
        return
    netloc = urlparse(origin).netloc.lower()
    host = (request.headers.get("host") or "").lower()
    if not netloc or netloc != host:
        raise HTTPException(status_code=403, detail="Origin 校验失败")


def _ensure_external_import_idle():
    if agent.external_canon_snapshot().get("running"):
        raise HTTPException(409, "外部正史导入正在运行；请先停止导入任务。")

@app.post("/api/audit/repair/single_pro")
def audit_repair_single_pro(req: dict):
    try:
        return agent.start_single_pro_retry(
            req.get("batch_id", ""),
            req.get("chapter_no", 0),
            req.get("mode", "retry"),
        )
    except Exception as e:
        raise HTTPException(409, str(e))



@app.middleware("http")
async def lightweight_auth_middleware(request: Request, call_next):
    path = request.url.path
    public = {
        "/",
        "/api/auth/status",
        "/api/auth/login",
        "/api/auth/setup",
        "/favicon.ico",
    }

    if path not in public:
        if path.startswith("/api/") or path.startswith("/static/"):
            if not _session_from_request(request):
                if path.startswith("/api/"):
                    return Response(
                        content=json.dumps({"detail":"未登录"}, ensure_ascii=False),
                        status_code=401,
                        media_type="application/json"
                    )
                return Response(status_code=401)

    if request.method in ("POST","PUT","PATCH","DELETE") and path not in {
        "/api/auth/login", "/api/auth/setup"
    }:
        try:
            _check_same_origin(request)
        except HTTPException as e:
            return Response(
                content=json.dumps({"detail":e.detail}, ensure_ascii=False),
                status_code=e.status_code,
                media_type="application/json"
            )

    return await call_next(request)

@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    name = "index.html" if _session_from_request(request) else "login.html"
    html = (ROOT / "static" / name).read_text(encoding="utf-8-sig")
    if name == "index.html":
        html = re.sub(r"NovelAgent V\d+\.\d+(?:\.\d+)?", f"NovelAgent V{VERSION}", html)
    return HTMLResponse(
        content=html,
        headers={
            "Content-Type": "text/html; charset=utf-8",
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"
        }
    )

@app.get("/api/auth/status")
def auth_status(request: Request):
    return {
        "configured": auth.configured(),
        "authenticated": bool(_session_from_request(request)),
        "username": auth.username() if auth.configured() else "admin"
    }

@app.post("/api/auth/setup")
def auth_setup(p: AuthCredentials, request: Request, response: Response):
    if auth.configured():
        raise HTTPException(409, "认证已经配置")
    try:
        auth.setup(p.username, p.password)
    except Exception as e:
        raise HTTPException(400, str(e))
    token, _ = auth.create_session(p.username.strip())
    response.set_cookie(
        SESSION_COOKIE, token,
        httponly=True,
        samesite="strict",
        secure=False,
        max_age=7*86400,
        path="/"
    )
    auth.clear_failures(_client_ip(request))
    return {"ok": True}

@app.post("/api/auth/login")
def auth_login(p: AuthCredentials, request: Request, response: Response):
    if not auth.configured():
        raise HTTPException(409, "请先完成首次设置")
    ip = _client_ip(request)
    allowed, retry = auth.login_allowed(ip)
    if not allowed:
        raise HTTPException(429, f"登录失败过多，请 {retry} 秒后再试")
    if not auth.verify(p.username, p.password):
        allowed, retry = auth.register_failure(ip)
        if not allowed:
            raise HTTPException(429, f"登录失败过多，请 {retry} 秒后再试")
        raise HTTPException(401, "用户名或密码错误")
    auth.clear_failures(ip)
    token, _ = auth.create_session(p.username.strip())
    response.set_cookie(
        SESSION_COOKIE, token,
        httponly=True,
        samesite="strict",
        secure=False,
        max_age=7*86400,
        path="/"
    )
    return {"ok": True}

@app.post("/api/auth/logout")
def auth_logout(request: Request, response: Response):
    auth.logout(request.cookies.get(SESSION_COOKIE, ""))
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}

@app.get("/api/config")
def get_config():
    c = load_config()
    return {
        "title": c.get("title", "未命名小说"),
        "chapters_per_run": c["generation"]["chapters_per_run"],
        "target_chapter_chars": c["generation"]["target_chapter_chars"],
        "recent_summary_count": c["generation"]["recent_summary_count"],
        "max_revision_rounds": c["generation"].get("max_revision_rounds", 1),
        "retrieval_top_k": c["embedding"].get("top_k", 14),
        "retrieval_min_score": c["embedding"].get("min_score", 0.22),
        "plan_context_trim_enabled": bool(c.get("cost_control", {}).get("plan_context_trim_enabled", True)),
        "plan_context_target_tokens": int(c.get("cost_control", {}).get("plan_context_target_tokens", 30000)),
        "plan_context_safe_tokens": int(c.get("cost_control", {}).get("plan_context_safe_tokens", 31800)),
        "plan_context_recovery_target_tokens": int(c.get("cost_control", {}).get("plan_context_recovery_target_tokens", 38000)),
        "plan_context_recovery_max_tokens": int(c.get("cost_control", {}).get("plan_context_recovery_max_tokens", 42000)),
        "chapter_cost_guard_mode": str(c.get("cost_control", {}).get("chapter_cost_guard_mode", "afp")),
        "chapter_cost_guard_afp_limit": float(c.get("cost_control", {}).get("chapter_cost_guard_afp_limit", 20.0)),
        "chapter_cost_guard_cny_limit": float(c.get("cost_control", {}).get("chapter_cost_guard_cny_limit", 5.0)),
        "high_context_mode_enabled": bool(c.get("cost_control", {}).get("high_context_mode_enabled", False)),
        "high_context_target_tokens": int(c.get("cost_control", {}).get("high_context_target_tokens", 120000)),
        "high_context_max_tokens": int(c.get("cost_control", {}).get("high_context_max_tokens", 127000)),
        "continuity": c.get("continuity", {}),
        "live_output_default": c["web"].get("live_output_default", False),
        "routing_mode": c.get("routing",{}).get("mode","recommended")
    }

@app.post("/api/config")
def set_config(p: ConfigPatch):
    with agent.lock:
        running = bool(agent.status.get("running"))
    if running:
        raise HTTPException(409, "Agent 运行中时请不要修改批次参数；先停止后再保存。")
    if agent.reader_snapshot().get("running"):
        raise HTTPException(409, "读者版智能分段运行中不能重载配置。")
    c = load_config()
    c["generation"]["chapters_per_run"] = max(1, min(999, p.chapters_per_run))
    c["generation"]["target_chapter_chars"] = max(500, min(30000, p.target_chapter_chars))
    c["generation"]["recent_summary_count"] = max(1, min(30, p.recent_summary_count))
    c["generation"]["max_revision_rounds"] = max(0, min(3, p.max_revision_rounds))
    c["embedding"]["top_k"] = max(1, min(100, p.retrieval_top_k))
    c["embedding"]["min_score"] = max(-1.0, min(1.0, p.retrieval_min_score))
    ccfg = c.setdefault("cost_control", {})
    ccfg["plan_context_trim_enabled"] = bool(p.plan_context_trim_enabled)
    recovery_target = max(32000, min(127000, int(p.plan_context_recovery_target_tokens)))
    recovery_max = max(recovery_target, min(127000, int(p.plan_context_recovery_max_tokens)))
    mode = str(p.chapter_cost_guard_mode or "afp").strip().lower()
    if mode not in {"afp", "cny", "unlimited"}:
        raise HTTPException(422, "费用限制模式必须是 AFP、人民币或无限制。")
    ccfg["plan_context_recovery_target_tokens"] = recovery_target
    ccfg["plan_context_recovery_max_tokens"] = recovery_max
    ccfg["chapter_cost_guard_mode"] = mode
    ccfg["chapter_cost_guard_afp_limit"] = max(0.0, float(p.chapter_cost_guard_afp_limit))
    ccfg["chapter_cost_guard_cny_limit"] = max(0.0, float(p.chapter_cost_guard_cny_limit))
    save_config(c)
    agent.reload_clients()
    return {"ok": True}

def _deepseek_slot_id(value) -> str:
    slot = str(value or "1").strip()
    return slot if slot in {"1", "2", "3", "4", "5"} else "1"


def _deepseek_provider_node(ds: dict, source: str) -> dict:
    if source == "volcengine_agent_plan":
        return dict((ds or {}).get("volcengine_agent_plan", {}) or {})
    return dict(ds or {})


def _deepseek_slot_meta(node: dict, source: str, slot: str) -> dict:
    slot = _deepseek_slot_id(slot)
    slots = dict((node or {}).get("api_slots", {}) or {})
    item = dict(slots.get(slot, {}) or {})
    if source == "volcengine_agent_plan":
        legacy = "runtime/volcengine_agent_plan_key.dpapi"
        default_store = legacy if slot == "1" else f"runtime/volcengine_agent_plan_key_{slot}.dpapi"
    else:
        legacy = "runtime/deepseek_api_key.dpapi"
        default_store = legacy if slot == "1" else f"runtime/deepseek_api_key_{slot}.dpapi"
    return {
        "slot": slot,
        "label": str(item.get("label", f"API {slot}") or f"API {slot}"),
        "api_key_store": str(item.get("api_key_store", default_store) or default_store),
    }


def _deepseek_active_meta(cfg=None):
    cfg = cfg or load_config()
    ds = cfg.get("deepseek", {})
    source = str(ds.get("source", "official") or "official").lower()
    if source not in {"official", "volcengine_agent_plan"}:
        source = "official"
    node = _deepseek_provider_node(ds, source)
    slot = _deepseek_slot_id(node.get("active_slot", "1"))
    account = _deepseek_slot_meta(node, source, slot)
    if source == "volcengine_agent_plan":
        return {
            "source": source,
            "label": "火山方舟 Agent Plan",
            "base_url": str(node.get("base_url", VOLCENGINE_AGENT_PLAN_BASE) or VOLCENGINE_AGENT_PLAN_BASE).rstrip("/"),
            "api_key_store": account["api_key_store"],
            "account_slot": account["slot"],
            "account_label": account["label"],
            "billing_mode": "afp",
        }
    return {
        "source": "official",
        "label": "DeepSeek 官方",
        "base_url": str(node.get("base_url", "https://api.deepseek.com") or "https://api.deepseek.com").rstrip("/"),
        "api_key_store": account["api_key_store"],
        "account_slot": account["slot"],
        "account_label": account["label"],
        "billing_mode": "cny_peak_offpeak",
    }


def _provider_node_and_slot(cfg: dict, source: str, slot: str | None = None):
    ds = cfg.get("deepseek", {})
    source = str(source or "official").strip().lower()
    if source not in {"official", "volcengine_agent_plan"}:
        raise ValueError("未知 API 来源")
    node = _deepseek_provider_node(ds, source)
    active_slot = _deepseek_slot_id(slot or node.get("active_slot", "1"))
    meta = _deepseek_slot_meta(node, source, active_slot)
    return node, active_slot, meta


def _secret_text(path: Path) -> str:
    try:
        return str(load_secret(path) or "").strip()
    except Exception:
        return ""


def _volc_openapi_store(slot: str, kind: str) -> Path:
    slot = _deepseek_slot_id(slot)
    suffix = "" if slot == "1" else f"_{slot}"
    if kind == "ak":
        return ROOT / f"runtime/volcengine_openapi_ak{suffix}.dpapi"
    if kind == "sk":
        return ROOT / f"runtime/volcengine_openapi_sk{suffix}.dpapi"
    raise ValueError("unknown volcengine credential kind")


def _http_json(req: urllib.request.Request, timeout: int = 20) -> dict:
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            data = json.loads(raw)
            detail = (
                data.get("error", {}).get("message")
                or data.get("ResponseMetadata", {}).get("Error", {}).get("Message")
                or data.get("message")
                or data.get("detail")
                or raw
            )
        except Exception:
            detail = raw or str(e)
        raise RuntimeError(f"HTTP {e.code}: {detail}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"网络请求失败：{getattr(e, 'reason', e)}") from e

    try:
        return json.loads(raw)
    except Exception as e:
        raise RuntimeError(f"接口返回的不是 JSON：{raw[:300]}") from e


def _deepseek_balance_status(cfg: dict) -> dict:
    node, slot, meta = _provider_node_and_slot(cfg, "official")
    key = _secret_text(ROOT / meta["api_key_store"])
    result = {
        "source": "official",
        "slot": slot,
        "account_label": meta["label"],
        "configured": bool(key),
        "ok": False,
        "is_available": False,
        "balance_infos": [],
    }
    if not key:
        result["error"] = "当前 DeepSeek API 槽位尚未配置 API Key"
        return result

    base_url = str(node.get("base_url", "https://api.deepseek.com") or "https://api.deepseek.com").rstrip("/")
    req = urllib.request.Request(
        base_url + "/user/balance",
        method="GET",
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {key}",
            "User-Agent": f"NovelAgent/{VERSION}",
        },
    )
    try:
        data = _http_json(req, timeout=20)
        infos = data.get("balance_infos") or []
        result.update({
            "ok": True,
            "is_available": bool(data.get("is_available")),
            "balance_infos": infos if isinstance(infos, list) else [],
        })
    except Exception as e:
        result["error"] = str(e)
    return result


def _hmac_bytes(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()


def _volcengine_openapi_get_afp(ak: str, sk: str) -> dict:
    """
    Query personal Agent Plan AFP usage through Volcengine control-plane OpenAPI.

    This uses the control-plane gateway/signature shape used by current Ark tooling:
      host: open.volcengineapi.com
      query: Action + Region + Version
      service: ark
      region: cn-beijing
      body: empty
      signed headers (fixed order):
        host;x-date;x-content-sha256;content-type
    """
    host = "open.volcengineapi.com"
    region = "cn-beijing"
    service = "ark"
    action = "GetAFPUsage"
    version = "2024-01-01"
    content_type = "application/json; charset=utf-8"
    body = b""

    x_date = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    short_date = x_date[:8]
    payload_hash = hashlib.sha256(body).hexdigest()

    # The exact same canonical query string is used for signing and the real URL.
    query = urlencode(sorted({
        "Action": action,
        "Region": region,
        "Version": version,
    }.items()))

    # Ark control-plane signing uses this fixed order; do not alphabetically reorder it.
    signed_headers = "host;x-date;x-content-sha256;content-type"
    canonical_headers = (
        f"host:{host}\n"
        f"x-date:{x_date}\n"
        f"x-content-sha256:{payload_hash}\n"
        f"content-type:{content_type}\n"
    )
    canonical_request = (
        "POST\n"
        "/\n"
        f"{query}\n"
        f"{canonical_headers}\n"
        f"{signed_headers}\n"
        f"{payload_hash}"
    )

    credential_scope = f"{short_date}/{region}/{service}/request"
    canonical_hash = hashlib.sha256(
        canonical_request.encode("utf-8")
    ).hexdigest()
    string_to_sign = (
        "HMAC-SHA256\n"
        f"{x_date}\n"
        f"{credential_scope}\n"
        f"{canonical_hash}"
    )

    # Volcengine derivation: SK is used directly; there is no AWS4 prefix.
    k_date = _hmac_bytes(sk.encode("utf-8"), short_date)
    k_region = _hmac_bytes(k_date, region)
    k_service = _hmac_bytes(k_region, service)
    k_signing = _hmac_bytes(k_service, "request")
    signature = hmac.new(
        k_signing,
        string_to_sign.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    authorization = (
        "HMAC-SHA256 "
        f"Credential={ak}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, "
        f"Signature={signature}"
    )

    req = urllib.request.Request(
        f"https://{host}/?{query}",
        data=body,
        method="POST",
        headers={
            "X-Date": x_date,
            "X-Content-Sha256": payload_hash,
            "Content-Type": content_type,
            "Authorization": authorization,
            "User-Agent": f"NovelAgent/{VERSION}",
        },
    )

    data = _http_json(req, timeout=20)

    err = (data.get("ResponseMetadata") or {}).get("Error")
    if err:
        code = str(err.get("Code") or "").strip()
        msg = str(err.get("Message") or "").strip()
        detail = ": ".join(x for x in (code, msg) if x)
        raise RuntimeError(detail or "火山 OpenAPI 调用失败")

    result = data.get("Result")
    if not isinstance(result, dict):
        raise RuntimeError("GetAFPUsage 返回中没有 Result")
    return result


def _volcengine_afp_status(cfg: dict) -> dict:
    _, slot, meta = _provider_node_and_slot(cfg, "volcengine_agent_plan")
    plan_key = _secret_text(ROOT / meta["api_key_store"])
    ak = _secret_text(_volc_openapi_store(slot, "ak"))
    sk = _secret_text(_volc_openapi_store(slot, "sk"))

    out = {
        "source": "volcengine_agent_plan",
        "slot": slot,
        "account_label": meta["label"],
        "configured": bool(plan_key),
        "openapi_configured": bool(ak and sk),
        "ok": False,
        "plan_type": "",
        "usage": {},
    }
    if not (ak and sk):
        out["error"] = "AFP 查询需要为当前火山槽位配置 OpenAPI AK/SK"
        return out

    try:
        result = _volcengine_openapi_get_afp(ak, sk)
        out["ok"] = True
        out["plan_type"] = str(result.get("PlanType") or "")
        out["usage"] = {
            "five_hour": result.get("AFPFiveHour") or {},
            "daily": result.get("AFPDaily") or {},
            "weekly": result.get("AFPWeekly") or {},
            "monthly": result.get("AFPMonthly") or {},
        }
    except Exception as e:
        out["error"] = str(e)
    return out


def _parse_usage_time(value):
    txt = str(value or "").strip()
    if not txt:
        return datetime.now(CST)
    try:
        dt = datetime.fromisoformat(txt.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=CST)
        return dt.astimezone(CST)
    except Exception:
        return datetime.now(CST)


def _recent_billing_by_chapter(chapter_nos):
    """Return accumulated actual-provider billing for recent Canon chapters.

    DeepSeek official: prefer the request-time stored CNY estimate/cost.
    Agent Plan: calculate AFP per request from its model and prompt-length band.
    Legacy rows pre-dating llm_billing_meta are inferred from cost_cny: positive=official,
    zero DeepSeek rows=Agent Plan. This matches this project's historical transition.
    """
    nums = sorted({int(x) for x in chapter_nos if int(x or 0) > 0})
    if not nums:
        return {}
    db_path = ROOT / "novel_memory.sqlite3"
    if not db_path.exists():
        return {}
    marks = ",".join("?" for _ in nums)
    out = {n:{"cny":0.0,"afp":0.0,"official_requests":0,"agent_plan_requests":0,"local_requests":0} for n in nums}
    with sqlite3.connect(db_path, timeout=5) as con:
        con.row_factory = sqlite3.Row
        has_meta = bool(con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='llm_billing_meta'").fetchone())
        meta_join = "LEFT JOIN llm_billing_meta m ON m.request_id=u.request_id" if has_meta else ""
        meta_cols = ", m.api_source, m.request_started_at, m.estimated_afp, m.estimated_cost_cny" if has_meta else ", '' AS api_source, '' AS request_started_at, NULL AS estimated_afp, NULL AS estimated_cost_cny"
        sql = f"""SELECT u.chapter_no,u.request_id,u.provider,u.model,u.prompt_tokens,u.cache_hit_tokens,u.cache_miss_tokens,
                         u.completion_tokens,u.reasoning_tokens,u.cost_cny,u.created_at {meta_cols}
                  FROM llm_usage u {meta_join}
                  WHERE u.chapter_no IN ({marks}) ORDER BY u.id"""
        for r in con.execute(sql, nums):
            ch = int(r["chapter_no"] or 0)
            if ch not in out:
                continue
            src = str(r["api_source"] or "").strip().lower()
            provider = str(r["provider"] or "").strip().lower()
            stored_cost = float(r["cost_cny"] or 0)
            if not src:
                if provider in {"local", "qwen"}:
                    src = "local"
                elif provider in {"volcengine_agent_plan", "volcengine", "ark_agent_plan"}:
                    src = "volcengine_agent_plan"
                elif stored_cost > 0:
                    src = "official"
                elif provider == "deepseek":
                    src = "volcengine_agent_plan"
                else:
                    src = provider or "unknown"
            usage = {
                "prompt_tokens": int(r["prompt_tokens"] or 0),
                "prompt_cache_hit_tokens": int(r["cache_hit_tokens"] or 0),
                "prompt_cache_miss_tokens": int(r["cache_miss_tokens"] or 0),
                "completion_tokens": int(r["completion_tokens"] or 0),
            }
            if src == "official":
                meta_cost = r["estimated_cost_cny"]
                if meta_cost is not None and float(meta_cost) > 0:
                    cost = float(meta_cost)
                elif stored_cost > 0:
                    cost = stored_cost
                else:
                    when = _parse_usage_time(r["request_started_at"] or r["created_at"])
                    cost = calculate_deepseek_cost(str(r["model"] or ""), usage, when=when)
                out[ch]["cny"] += float(cost or 0)
                out[ch]["official_requests"] += 1
            elif src == "volcengine_agent_plan":
                meta_afp = r["estimated_afp"]
                afp = float(meta_afp) if meta_afp is not None else float(calculate_volcengine_afp(str(r["model"] or ""), usage) or 0)
                out[ch]["afp"] += afp
                out[ch]["agent_plan_requests"] += 1
            elif src == "local":
                out[ch]["local_requests"] += 1
    for x in out.values():
        x["cny"] = round(x["cny"], 6)
        x["afp"] = round(x["afp"], 3)
        if x["official_requests"] and x["agent_plan_requests"]:
            x["mode"] = "mixed"
        elif x["agent_plan_requests"]:
            x["mode"] = "afp"
        elif x["official_requests"]:
            x["mode"] = "cny"
        else:
            x["mode"] = "none"
    return out


def _augment_recent_chapter_billing(rows):
    items = [dict(x) for x in (rows or [])]
    nums = [int(x.get("chapter_no") or x.get("chapter") or x.get("id") or 0) for x in items]
    billing = _recent_billing_by_chapter(nums)
    for x, n in zip(items, nums):
        x["billing"] = billing.get(n, {"mode":"none","cny":0.0,"afp":0.0})
    return items


def _deepseek_billing_status(cfg=None):
    meta = _deepseek_active_meta(cfg)
    if meta["source"] == "volcengine_agent_plan":
        return {
            "source": meta["source"],
            "provider_label": meta["label"],
            "billing_mode": "afp",
            "peak": False,
            "period": "agent_plan",
            "label": "AFP 套餐",
            "base_url": meta["base_url"],
            "seconds_to_switch": 0,
            "next_switch_at": None,
            "peak_windows_local": [],
        }
    out = deepseek_price_status()
    out["source"] = "official"
    out["provider_label"] = "DeepSeek 官方"
    out["billing_mode"] = "cny_peak_offpeak"
    out["base_url"] = meta["base_url"]
    return out


@app.get("/api/status")
def status():
    s = agent.snapshot()
    s["version"] = VERSION
    s["dlc"] = agent.dlc_snapshot()
    s["audit"] = agent.audit_snapshot()
    s["audit_repair"] = agent.repair_snapshot()
    s["reader_reflow"] = agent.reader_snapshot()
    s["external_canon"] = agent.external_canon_snapshot()
    s["recent_chapters"] = _augment_recent_chapter_billing(agent.db.recent_chapters(12))
    s["embedding_control"] = embedding_models.status()
    c = load_config()
    s["deepseek_pricing"] = _deepseek_billing_status(c)
    _ds_meta = _deepseek_active_meta(c)
    s["deepseek_source"] = _ds_meta["source"]
    s["deepseek_account_slot"] = _ds_meta.get("account_slot", "1")
    s["deepseek_account_label"] = _ds_meta.get("account_label", "API 1")
    s["routing"] = {
        "mode": c.get("routing",{}).get("mode","recommended"),
        "canon_deepseek_only": bool(c.get("canon",{}).get("deepseek_only", True)),
        "effective": "canon_deepseek_only" if bool(c.get("canon",{}).get("deepseek_only", True)) else c.get("routing",{}).get("mode","recommended"),
    }
    try:
        meta = _deepseek_active_meta(c)
        s["deepseek_configured"] = bool(load_secret(ROOT / meta["api_key_store"]))
    except Exception:
        s["deepseek_configured"] = False
    grok = c.get("grok", {})
    dlc = c.get("dlc", {})
    atlas_rel = str(dlc.get("atlas_file", "prompts/expansion_reference.md"))
    try:
        grok_configured = bool(load_secret(ROOT / str(grok.get("api_key_store", "runtime/grok_api_key.dpapi"))))
    except Exception:
        grok_configured = False
    s["grok"] = {
        "configured": grok_configured,
        "base_url": str(grok.get("base_url", "https://api.x.ai/v1")),
        "api_label": str(grok.get("api_label", "Grok API")),
        "model": str(dlc.get("model", grok.get("model", "grok-4.6"))),
        "review_model": str(dlc.get("review_model", grok.get("review_model", "grok-4.6"))),
        "reasoning_effort": str(dlc.get("reasoning_effort", grok.get("reasoning_effort", "low"))),
        "review_reasoning_effort": str(dlc.get("review_reasoning_effort", grok.get("review_reasoning_effort", "medium"))),
        "review_enabled": bool(dlc.get("review_enabled", True)),
        "atlas_file": atlas_rel,
        "atlas_exists": bool((ROOT / atlas_rel).exists()),
    }
    return s


@app.get("/api/external-canon/status")
def external_canon_status():
    return {"ok": True, "status": agent.external_canon_snapshot()}


@app.post("/api/external-canon/import")
async def external_canon_import(request: Request, start: int | None = None):
    cfg = load_config().get("external_canon", {}) or {}
    max_bytes = int(cfg.get("max_zip_bytes", 134217728) or 134217728)
    try:
        declared = int(request.headers.get("content-length") or 0)
    except Exception:
        declared = 0
    if declared > max_bytes:
        raise HTTPException(413, "ZIP 文件超过外部正史导入大小上限")
    payload = await request.body()
    if len(payload) > max_bytes:
        raise HTTPException(413, "ZIP 文件超过外部正史导入大小上限")
    try:
        ok, message = agent.start_external_canon_import(payload, range_start=start)
    except ExternalCanonError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        raise HTTPException(409, str(exc))
    if not ok:
        raise HTTPException(409, message)
    return {"ok": True, "message": message, "status": agent.external_canon_snapshot()}


@app.post("/api/external-canon/stop")
def external_canon_stop():
    ok, message = agent.request_stop_external_canon_import()
    if not ok:
        raise HTTPException(409, message)
    return {"ok": True, "message": message}

@app.post("/api/start")
def start(confirm_peak: bool = False):
    if agent.external_canon_snapshot().get("running"):
        raise HTTPException(409, "外部正史导入正在运行；请先停止导入。")
    if agent.reader_snapshot().get("running"):
        raise HTTPException(409, "读者版智能分段正在运行；请先停止该任务。")
    if agent.repair_snapshot().get("running"):
        raise HTTPException(409, "跨章节审计修复正在运行；请先停止修复。")
    if agent.audit_snapshot().get("running"):
        raise HTTPException(409, "剧情一致性审计运行中；请先停止审计再启动 Canon。")
    c = load_config()
    price = _deepseek_billing_status(c)
    if price.get("source") == "official" and bool(c.get("cost_control", {}).get("peak_warning", True)) and price.get("peak") and not confirm_peak:
        raise HTTPException(409, "当前处于 DeepSeek API 峰时，价格约为谷时 2 倍；确认后才能启动 Canon。")
    ok, msg = agent.start()
    if not ok:
        raise HTTPException(409, msg)
    return {"ok": True, "message": msg}

@app.post("/api/stop")
def stop():
    agent.request_stop()
    return {"ok": True}

@app.post("/api/plan-overflow/continue")
def continue_plan_overflow():
    if agent.reader_snapshot().get("running"):
        raise HTTPException(409, "读者版智能分段正在运行；请先停止该任务。")
    if agent.repair_snapshot().get("running"):
        raise HTTPException(409, "跨章节审计修复正在运行；请先停止修复。")
    if agent.audit_snapshot().get("running"):
        raise HTTPException(409, "剧情一致性审计运行中；请先停止审计。")
    ok, msg = agent.approve_plan_overflow()
    if not ok:
        raise HTTPException(409, msg)
    return {"ok": True, "message": msg}

@app.post("/api/plan-overflow/cancel")
def cancel_plan_overflow():
    ok, msg = agent.cancel_plan_overflow()
    if not ok:
        raise HTTPException(409, msg)
    return {"ok": True, "message": msg}

@app.post("/api/live")
def live(p: LivePatch):
    agent.set_live_output(p.enabled)
    return {"ok": True, "enabled": p.enabled}

@app.post("/api/high-context")
def high_context(p: HighContextPatch):
    with agent.lock:
        if agent.status.get("running"):
            raise HTTPException(409, "Canon 运行中不能切换高上下文模式；请先停止当前任务。")

    c = load_config()
    ccfg = c.setdefault("cost_control", {})
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
    ccfg["high_context_mode_enabled"] = bool(p.enabled)
    ccfg["high_context_target_tokens"] = target
    ccfg["high_context_max_tokens"] = hard_max
    save_config(c)

    if (agent.snapshot().get("plan_overflow") or {}).get("pending"):
        agent.cancel_plan_overflow()
    agent.reload_clients()
    enabled, target, hard_max = agent.high_context_policy()
    label = "开启" if enabled else "关闭"
    agent.log(
        f"高上下文模式已{label}：裁剪目标 {target:,} tokens，硬上限 {hard_max:,} tokens。"
    )
    return {
        "ok": True,
        "enabled": enabled,
        "target_tokens": target,
        "max_tokens": hard_max,
        "message": f"高上下文模式已{label}",
    }

# ---------- DeepSeek / routing / chapter editing ----------
@app.get("/api/deepseek/config")
def deepseek_config():
    cfg = load_config()
    ds = cfg.get("deepseek", {})
    active = _deepseek_active_meta(cfg)
    providers = {}
    for source, label, default_base in [
        ("official", "DeepSeek 官方", "https://api.deepseek.com"),
        ("volcengine_agent_plan", "火山方舟 Agent Plan", VOLCENGINE_AGENT_PLAN_BASE),
    ]:
        node = _deepseek_provider_node(ds, source)
        accounts = []
        for slot in ("1", "2", "3", "4", "5"):
            meta = _deepseek_slot_meta(node, source, slot)
            try:
                configured = bool(load_secret(ROOT / meta["api_key_store"]))
            except Exception:
                configured = False
            accounts.append({
                "slot": slot,
                "label": meta["label"],
                "configured": configured,
            })
        active_slot = _deepseek_slot_id(node.get("active_slot", "1"))
        base_url = str(node.get("base_url", default_base) or default_base).rstrip("/")
        providers[source] = {
            "label": label,
            "base_url": base_url,
            "active_slot": active_slot,
            "accounts": accounts,
            "configured": next((x["configured"] for x in accounts if x["slot"] == active_slot), False),
        }
    current = providers[active["source"]]
    return {
        "source": active["source"],
        "label": active["label"],
        "base_url": active["base_url"],
        "account_slot": active.get("account_slot", "1"),
        "account_label": active.get("account_label", "API 1"),
        "configured": current["configured"],
        "providers": providers,
    }

@app.post("/api/deepseek/provider")
def deepseek_set_provider(p: DeepSeekProviderPatch):
    source = str(p.source or "").strip().lower()
    if source not in {"official", "volcengine_agent_plan"}:
        raise HTTPException(400, "未知 DeepSeek API 来源")
    if agent.status.get("running") or agent.audit_snapshot().get("running"):
        raise HTTPException(409, "Canon/剧情审计运行中不能切换 API 来源；先停止当前任务。")
    c = load_config()
    c.setdefault("deepseek", {})["source"] = source
    save_config(c)
    agent.reload_clients()
    return {"ok": True, "source": source, "provider": _deepseek_active_meta(c)}

@app.post("/api/deepseek/account")
def deepseek_set_account(p: DeepSeekAccountPatch):
    slot = _deepseek_slot_id(p.slot)
    if str(p.slot or "").strip() not in {"1", "2", "3", "4", "5"}:
        raise HTTPException(400, "API 槽位必须是 1～5")
    if agent.status.get("running") or agent.audit_snapshot().get("running"):
        raise HTTPException(409, "Canon/剧情审计运行中不能切换 API 账号；先停止当前任务。")

    c = load_config()
    ds = c.setdefault("deepseek", {})
    source = str(p.source or ds.get("source", "official") or "official").strip().lower()
    if source not in {"official", "volcengine_agent_plan"}:
        raise HTTPException(400, "未知 API 来源")

    if source == "volcengine_agent_plan":
        ds.setdefault("volcengine_agent_plan", {})["active_slot"] = slot
        node = _deepseek_provider_node(ds, source)
    else:
        ds["active_slot"] = slot
        node = _deepseek_provider_node(ds, source)

    save_config(c)
    agent.reload_clients()
    meta = _deepseek_slot_meta(node, source, slot)
    return {
        "ok": True,
        "source": source,
        "slot": slot,
        "account_label": meta["label"],
    }


@app.post("/api/deepseek/key")
def deepseek_set_key(p: DeepSeekKeyPatch):
    if agent.status.get("running") or agent.audit_snapshot().get("running"):
        raise HTTPException(409, "Canon/剧情审计运行中不能修改 API Key；先停止当前任务。")
    key = p.api_key.strip()
    if not key:
        raise HTTPException(400, "API Key 不能为空")

    c = load_config()
    ds = c.get("deepseek", {})
    source = str(p.source or ds.get("source", "official") or "official").strip().lower()
    if source not in {"official", "volcengine_agent_plan"}:
        raise HTTPException(400, "未知 API 来源")

    node, slot, meta = _provider_node_and_slot(c, source, p.slot)
    save_secret(ROOT / meta["api_key_store"], key)
    agent.reload_clients()
    return {
        "ok": True,
        "source": source,
        "label": "火山方舟 Agent Plan" if source == "volcengine_agent_plan" else "DeepSeek 官方",
        "slot": slot,
        "account_label": meta["label"],
    }


@app.post("/api/deepseek/test")
def deepseek_test():
    ok, detail = agent.router.deepseek.test_connection()
    if not ok:
        raise HTTPException(502, detail)
    return {"ok": True, "detail": detail, "source": agent.router.deepseek.source()}


@app.get("/api/grok/config")
def grok_config():
    c = load_config()
    grok = c.get("grok", {})
    dlc = c.get("dlc", {})
    atlas_rel = str(dlc.get("atlas_file", "prompts/expansion_reference.md"))
    try:
        configured = bool(load_secret(ROOT / str(grok.get("api_key_store", "runtime/grok_api_key.dpapi"))))
    except Exception:
        configured = False
    return {
        "configured": configured,
        "base_url": str(grok.get("base_url", "https://api.x.ai/v1")),
        "api_label": str(grok.get("api_label", "Grok API")),
        "model": str(dlc.get("model", grok.get("model", "grok-4.6"))),
        "review_model": str(dlc.get("review_model", grok.get("review_model", "grok-4.6"))),
        "reasoning_effort": str(dlc.get("reasoning_effort", grok.get("reasoning_effort", "low"))),
        "review_reasoning_effort": str(dlc.get("review_reasoning_effort", grok.get("review_reasoning_effort", "medium"))),
        "review_enabled": bool(dlc.get("review_enabled", True)),
        "atlas_file": atlas_rel,
        "atlas_exists": bool((ROOT / atlas_rel).exists()),
    }


@app.post("/api/grok/key")
def grok_set_key(p: GrokKeyPatch):
    if agent.dlc_snapshot().get("running"):
        raise HTTPException(409, "Grok DLC 运行中不能修改 API Key；请先停止当前任务。")
    key = p.api_key.strip()
    if not key:
        raise HTTPException(400, "Grok API Key 不能为空")
    c = load_config()
    store = str(c.get("grok", {}).get("api_key_store", "runtime/grok_api_key.dpapi"))
    save_secret(ROOT / store, key)
    agent.reload_clients()
    return {"ok": True, "configured": True}


@app.post("/api/grok/config")
def grok_set_config(p: GrokConfigPatch):
    if agent.dlc_snapshot().get("running"):
        raise HTTPException(409, "Grok DLC 运行中不能修改模型设置；请先停止当前任务。")
    allowed_efforts = {"none", "low", "medium", "high", "xhigh"}
    base_url = str(p.base_url or "").strip().rstrip("/")
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise HTTPException(400, "Grok Base URL 无效")
    if parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise HTTPException(400, "远程 Grok Base URL 必须使用 HTTPS")
    model_pattern = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
    if not model_pattern.fullmatch(p.model) or not model_pattern.fullmatch(p.review_model):
        raise HTTPException(400, "Grok 模型名格式无效")
    if p.reasoning_effort not in allowed_efforts or p.review_reasoning_effort not in allowed_efforts:
        raise HTTPException(400, "reasoning_effort 无效")
    c = load_config()
    grok = c.setdefault("grok", {})
    dlc = c.setdefault("dlc", {})
    grok.update({
        "base_url": base_url,
        "api_source": parsed.hostname or "grok_compatible",
        "api_label": "xAI" if parsed.hostname == "api.x.ai" else (parsed.hostname or "Grok API"),
        "model": p.model, "review_model": p.review_model,
        "reasoning_effort": p.reasoning_effort,
        "review_reasoning_effort": p.review_reasoning_effort,
    })
    dlc.update({
        "provider": "grok", "model": p.model, "review_model": p.review_model,
        "reasoning_effort": p.reasoning_effort,
        "review_reasoning_effort": p.review_reasoning_effort,
        "review_enabled": bool(p.review_enabled),
    })
    save_config(c)
    agent.reload_clients()
    return {"ok": True, "base_url": base_url, "model": p.model, "review_model": p.review_model}


@app.post("/api/grok/test")
def grok_test():
    ok, detail = agent.dlc_router.grok.test_connection()
    if not ok:
        raise HTTPException(502, detail)
    return {"ok": True, "detail": detail}


@app.get("/api/deepseek/balance")
def deepseek_balance():
    return _deepseek_balance_status(load_config())


@app.get("/api/volcengine/afp")
def volcengine_afp():
    return _volcengine_afp_status(load_config())


@app.post("/api/volcengine/openapi-key")
def volcengine_openapi_key(p: VolcengineOpenApiPatch):
    c = load_config()
    _, active_slot, _ = _provider_node_and_slot(c, "volcengine_agent_plan")
    slot = _deepseek_slot_id(p.slot or active_slot)
    ak = p.access_key.strip()
    sk = p.secret_key.strip()
    if not ak or not sk:
        raise HTTPException(400, "AK 和 SK 都不能为空")
    save_secret(_volc_openapi_store(slot, "ak"), ak)
    save_secret(_volc_openapi_store(slot, "sk"), sk)
    return {"ok": True, "slot": slot}


@app.get("/api/routing")
def routing_get():
    r = load_config().get("routing", {})
    c = load_config()
    return {"mode": r.get("mode", "recommended"), "stages": r.get("stages", {}),
            "canon_deepseek_only": bool(c.get("canon", {}).get("deepseek_only", True)),
            "canon_stages": c.get("canon", {}).get("stage_models", {}),
            "cost_control": c.get("cost_control", {}),
            "cost_profiles": {k:{"label":v["label"]} for k,v in COST_PROFILES.items()},
            "effective": "canon_deepseek_only" if bool(c.get("canon", {}).get("deepseek_only", True)) else r.get("mode", "recommended"),
            "modes": ["recommended", "all_deepseek", "custom"]}

@app.post("/api/canon/routing")
def canon_routing_set(p: CanonRoutingPatch):
    if agent.status.get("running"):
        raise HTTPException(409, "Canon 运行中不能修改模型/Thinking；先停止 Canon。")
    if agent.reader_snapshot().get("running"):
        raise HTTPException(409, "读者版智能分段运行中不能修改模型/Thinking。")
    allowed_models = {"deepseek-v4-flash", "deepseek-v4-pro"}
    allowed_efforts = {"low", "high", "max"}
    defaults = COST_PROFILES["enhanced"]["stages"]
    clean = {}
    raw = p.stages or {}
    for stage, default in defaults.items():
        x = dict(raw.get(stage, {}))
        model = str(x.get("model", default["model"]))
        if model not in allowed_models:
            raise HTTPException(400, f"{stage} 不支持模型：{model}")
        thinking = bool(x.get("thinking", default["thinking"]))
        effort = str(x.get("reasoning_effort", default.get("reasoning_effort", "low"))).lower()
        if effort == "medium":
            effort = "high"
        if effort not in allowed_efforts:
            raise HTTPException(400, f"{stage} 不支持思考强度：{effort}")
        clean[stage] = {"model": model, "thinking": thinking, "reasoning_effort": effort}
    c = load_config()
    c.setdefault("canon", {})["deepseek_only"] = True
    c["canon"]["stage_models"] = clean
    c.setdefault("cost_control", {})["profile"] = "custom"
    c.setdefault("deepseek", {})["plan_reasoning_effort"] = clean.get("plan", {}).get("reasoning_effort", "low")
    save_config(c)
    agent.reload_clients()
    return {"ok": True, "canon_stages": clean}


@app.post("/api/cost/profile")
def cost_profile_set(p: CostProfilePatch):
    if agent.status.get("running"):
        raise HTTPException(409, "Canon 运行中不能切换成本策略；先停止 Canon。")
    if agent.reader_snapshot().get("running"):
        raise HTTPException(409, "读者版智能分段运行中不能切换成本策略。")
    c = load_config()
    if p.profile == "custom":
        c.setdefault("cost_control", {})["profile"] = "custom"
        save_config(c)
        agent.reload_clients()
        return {"ok": True, "profile": "custom", "canon_stages": c.get("canon",{}).get("stage_models",{}), "cost_control": c["cost_control"]}
    if p.profile not in COST_PROFILES:
        raise HTTPException(400, "未知成本策略")
    _apply_cost_profile(c, p.profile)
    save_config(c)
    agent.reload_clients()
    return {"ok": True, "profile": p.profile, "canon_stages": c["canon"]["stage_models"], "cost_control": c["cost_control"]}

@app.get("/api/deepseek/pricing-status")
def deepseek_pricing():
    return _deepseek_billing_status(load_config())

@app.post("/api/routing")
def routing_set(p: RoutingPatch):
    allowed = {"recommended", "all_deepseek", "custom"}
    if p.mode not in allowed:
        raise HTTPException(400, "Unknown routing mode")
    if agent.status.get("running"):
        raise HTTPException(409, "Agent 运行中不能修改路由")
    if agent.reader_snapshot().get("running"):
        raise HTTPException(409, "读者版智能分段运行中不能修改路由")
    c = load_config(); c.setdefault("routing", {})["mode"] = p.mode
    if p.stages is not None:
        clean = {}
        for stage in ("plan","draft","review","revision","summary","memory"):
            x = dict((p.stages or {}).get(stage, {}))
            provider = "deepseek"
            model = x.get("model", "deepseek-v4-flash")
            if model not in {"deepseek-v4-flash","deepseek-v4-pro"}:
                model = "deepseek-v4-flash"
            clean[stage] = {"provider":provider,"model":model,"thinking":bool(x.get("thinking",False))}
        c["routing"]["stages"] = clean
    save_config(c); agent.reload_clients()
    return {"ok": True}

# ---------- global story audit ----------
@app.get("/api/audit/status")
def audit_status():
    return {"ok": True, "status": agent.audit_snapshot()}

@app.post("/api/audit/start")
def audit_start(p: AuditStartPatch):
    _ensure_external_import_idle()
    if agent.reader_snapshot().get("running"):
        raise HTTPException(409, "读者版智能分段正在运行；请先停止。")
    if agent.repair_snapshot().get("running"):
        raise HTTPException(409, "审计修复正在运行；请先停止修复。")
    if agent.status.get("running"):
        raise HTTPException(409, "Canon 正在运行；为了保证审计期间章节和 outline 不变化，请先停止 Canon。")
    try:
        if agent.dlc_snapshot().get("running"):
            raise HTTPException(409, "DLC 正在运行；请先停止 DLC 再开始剧情审计。")
    except HTTPException:
        raise
    except Exception:
        pass
    try:
        if not agent.audit_router.deepseek.configured():
            raise HTTPException(409, "当前 DeepSeek API 账号尚未配置 Key。")
    except AttributeError:
        pass
    try:
        st = agent.start_story_audit(p.start, p.end, p.segment_size, p.source_check)
    except (ValueError, RuntimeError) as e:
        raise HTTPException(409, str(e))
    return {"ok": True, "status": st}

@app.post("/api/audit/stop")
def audit_stop():
    return {"ok": True, "status": agent.stop_story_audit()}

@app.get("/api/audit/report")
def audit_report():
    st = agent.audit_snapshot()
    rel = str(st.get("report_file", "") or "").strip()
    if not rel:
        # Fall back to the newest completed report across restarts.
        reports = sorted((ROOT / "reports").glob("audit_*.md"), key=lambda x: x.stat().st_mtime, reverse=True) if (ROOT / "reports").exists() else []
        if not reports:
            raise HTTPException(404, "尚无剧情审计报告")
        path = reports[0]
    else:
        path = (ROOT / rel).resolve()
        reports_root = (ROOT / "reports").resolve()
        try:
            path.relative_to(reports_root)
        except Exception:
            raise HTTPException(400, "无效审计报告路径")
    if not path.is_file():
        raise HTTPException(404, "剧情审计报告不存在")
    return {"ok": True, "name": path.name, "content": path.read_text(encoding="utf-8")}

# ---------- audit-driven multi-chapter repair ----------
@app.get("/api/audit/repair/status")
def audit_repair_status():
    return {"ok": True, "status": agent.repair_snapshot()}

@app.post("/api/audit/repair/plan")
def audit_repair_plan(p: AuditRepairPlanPatch):
    _ensure_external_import_idle()
    if agent.reader_snapshot().get("running"):
        raise HTTPException(409, "读者版智能分段正在运行；请先停止。")
    if agent.status.get("running"):
        raise HTTPException(409, "Canon 正在运行；请先停止 Canon。")
    if agent.audit_snapshot().get("running"):
        raise HTTPException(409, "剧情审计正在运行；请等审计完成。")
    try:
        if agent.dlc_snapshot().get("running"):
            raise HTTPException(409, "DLC 正在运行；请先停止 DLC。")
    except HTTPException:
        raise
    except Exception:
        pass
    try:
        return {"ok": True, "status": agent.start_audit_repair_plan(p.audit_text, p.model)}
    except Exception as e:
        raise HTTPException(409, str(e))

@app.post("/api/audit/repair/start")
def audit_repair_start(p: AuditRepairStartPatch):
    _ensure_external_import_idle()
    if agent.reader_snapshot().get("running"):
        raise HTTPException(409, "读者版智能分段正在运行；请先停止。")
    try:
        return {"ok": True, "status": agent.start_audit_repair_candidates(p.batch_id, p.model)}
    except Exception as e:
        raise HTTPException(409, str(e))

@app.post("/api/audit/repair/stop")
def audit_repair_stop():
    return {"ok": True, "status": agent.stop_audit_repair()}

@app.get("/api/audit/repair/batch")
def audit_repair_batch(batch_id: str = ""):
    try:
        return agent.audit_repair_batch_detail(batch_id)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        raise HTTPException(409, str(e))

@app.get("/api/audit/repair/candidate/{chapter_no}")
def audit_repair_candidate(chapter_no: int, batch_id: str = ""):
    try:
        return agent.audit_repair_candidate_detail(chapter_no, batch_id)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        raise HTTPException(409, str(e))

@app.post("/api/audit/repair/commit")
def audit_repair_commit(p: AuditRepairCommitPatch):
    try:
        return agent.commit_audit_repair(
            p.batch_id,
            p.chapters,
            manual=bool(p.manual or p.manual_confirmed),
            force=bool(p.force or p.forced),
            confirm=p.confirm,
            force_reason=p.force_reason,
            mode=p.mode,
        )
    except Exception as e:
        raise HTTPException(409, str(e))

@app.post("/api/audit/repair/rollback")
def audit_repair_rollback(p: AuditRepairBatchPatch):
    try:
        return agent.rollback_audit_repair(p.batch_id)
    except Exception as e:
        raise HTTPException(409, str(e))

# ---------- prompt maintenance / chapter export ----------
PROMPTS_DIR = ROOT / "story"
PROMPT_BACKUP_DIR = ROOT / "runtime" / "prompt_backups"
CHAPTER_FILE_RE = re.compile(r"^(\d{4,})\.md$")

def _prompt_path(name: str, must_exist: bool = True) -> Path:
    # UI is intentionally limited to direct children of story/*.md.
    # Reject traversal, nested paths and non-Markdown files server-side too.
    clean = str(name or "").strip()
    if not clean or Path(clean).name != clean or "/" in clean or "\\" in clean:
        raise HTTPException(400, "无效提示词文件名")
    if Path(clean).suffix.lower() != ".md":
        raise HTTPException(400, "只允许编辑 story/*.md")
    path = PROMPTS_DIR / clean
    if must_exist and not path.is_file():
        raise HTTPException(404, "提示词文件不存在")
    return path

def _prompt_edit_busy() -> bool:
    if bool(agent.status.get("running")) or bool(agent.audit_snapshot().get("running")):
        return True
    try:
        return bool(agent.dlc_snapshot().get("running"))
    except Exception:
        return False

def _chapter_files(start: int, end: int, folder: Path | None = None):
    if start < 1 or end < 1 or start > end:
        raise HTTPException(400, "章节范围无效")
    if end - start > 10000:
        raise HTTPException(400, "一次最多导出 10001 章")
    folder = folder or (ROOT / "chapters")
    rows = []
    missing = []
    for n in range(start, end + 1):
        path = folder / f"{n:04d}.md"
        if path.is_file():
            rows.append((n, path))
        else:
            missing.append(n)
    if not rows:
        raise HTTPException(404, "所选范围内没有已生成章节")
    return rows, missing

def _chapter_export_text(rows, plain: bool = False, compact: bool = False) -> str:
    parts = []
    for n, path in rows:
        body = path.read_text(encoding="utf-8").strip("\ufeff\r\n")
        if not body:
            body = f"第{n}章"
        # Merged exports always have an explicit chapter boundary. If the
        # generated file already starts with a numbered chapter title, keep it.
        head = "\n".join(body.splitlines()[:3])
        has_numbered_title = bool(re.search(r"(?:^|\n)\s*(?:#{1,6}\s*)?第\s*\d+\s*章", head))
        if not has_numbered_title:
            body = (f"第{n}章\n\n" if plain else f"# 第{n}章\n\n") + body
        # Preserve generated prose. For TXT only remove Markdown heading markers
        # and standalone horizontal rules; other text is left untouched.
        if plain:
            body = re.sub(r"(?m)^\s{0,3}#{1,6}\s+", "", body)
            if not compact:
                body = re.sub(r"(?m)^\s*(?:---+|___+|\*\*\*+)\s*$", "", body)
            body = re.sub(r"\n{3,}", "\n\n", body).strip()
            if compact:
                # Reader TXT uses one physical line per paragraph.  Keep one
                # empty line below a numbered chapter title, but remove all
                # visual blank rows between body paragraphs.
                lines = [line.rstrip() for line in body.splitlines()]
                lines = [line for line in lines if line.strip()]
                if lines:
                    first = lines[0]
                    rest = lines[1:]
                    body = first + (("\n\n" + "\n".join(rest)) if rest else "")
        parts.append(body)
    return "\n\n".join(parts).rstrip() + "\n"

# ---------- non-destructive reader paragraph reflow ----------
@app.get("/api/reader-reflow/info")
def reader_reflow_info():
    def numbers(folder):
        out = []
        if folder.is_dir():
            for path in folder.glob("*.md"):
                m = CHAPTER_FILE_RE.match(path.name)
                if m:
                    out.append(int(m.group(1)))
        return sorted(out)
    source = numbers(ROOT / "chapters")
    reader = numbers(ROOT / "reader_chapters")
    return {
        "ok": True,
        "source_count": len(source),
        "source_first": source[0] if source else None,
        "source_last": source[-1] if source else None,
        "reader_count": len(reader),
        "reader_first": reader[0] if reader else None,
        "reader_last": reader[-1] if reader else None,
        "status": agent.reader_snapshot(),
    }

@app.get("/api/reader-reflow/status")
def reader_reflow_status():
    return {"ok": True, "status": agent.reader_snapshot()}

@app.post("/api/reader-reflow/start")
def reader_reflow_start(p: ReaderReflowStartPatch):
    _ensure_external_import_idle()
    try:
        meta = _deepseek_active_meta(load_config())
        if not load_secret(ROOT / meta["api_key_store"]):
            raise HTTPException(409, "当前 DeepSeek API 账号尚未配置 Key。")
    except HTTPException:
        raise
    except Exception:
        pass
    try:
        return {"ok": True, "status": agent.start_reader_reflow(p.start, p.end, p.workers, p.overwrite)}
    except (ValueError, RuntimeError) as e:
        raise HTTPException(409, str(e))

@app.post("/api/reader-reflow/stop")
def reader_reflow_stop():
    return {"ok": True, "status": agent.stop_reader_reflow()}

@app.get("/api/reader-reflow/chapter/{chapter_no}")
def reader_reflow_chapter(chapter_no: int):
    try:
        return agent.reader_chapter_detail(chapter_no)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))

@app.get("/api/reader-reflow/export")
def reader_reflow_export(start: int, end: int, format: str = "txt"):
    fmt = str(format or "txt").lower()
    if fmt not in {"md", "txt", "zip"}:
        raise HTTPException(400, "读者版导出只支持 md / txt / zip")
    rows, missing = _chapter_files(start, end, ROOT / "reader_chapters")
    base = f"reader_chapters_{start:04d}-{end:04d}"
    headers = {
        "Content-Disposition": f'attachment; filename="{base}.{fmt}"',
        "X-NovelAgent-Export-Count": str(len(rows)),
        "X-NovelAgent-Missing-Count": str(len(missing)),
    }
    if fmt == "zip":
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for _n, path in rows:
                zf.write(path, arcname=path.name)
        return Response(content=buf.getvalue(), media_type="application/zip", headers=headers)
    text = _chapter_export_text(rows, plain=(fmt == "txt"), compact=(fmt == "txt"))
    media = "text/markdown; charset=utf-8" if fmt == "md" else "text/plain; charset=utf-8"
    return Response(content=text.encode("utf-8"), media_type=media, headers=headers)

@app.get("/api/prompts")
def prompt_list():
    PROMPTS_DIR.mkdir(parents=True, exist_ok=True)
    files = []
    for path in sorted(PROMPTS_DIR.glob("*.md"), key=lambda x: x.name.lower()):
        try:
            st = path.stat()
            files.append({
                "name": path.name,
                "chars": len(path.read_text(encoding="utf-8")),
                "modified_at": int(st.st_mtime),
            })
        except OSError:
            continue
    return {"ok": True, "files": files, "busy": _prompt_edit_busy()}

@app.get("/api/prompts/{name}")
def prompt_get(name: str):
    path = _prompt_path(name)
    return {"ok": True, "name": path.name, "content": path.read_text(encoding="utf-8")}

@app.post("/api/prompts/save")
def prompt_save(p: PromptSavePatch):
    if _prompt_edit_busy():
        raise HTTPException(409, "Canon 或 DLC 正在运行；为避免同一任务混用两套提示词，当前禁止保存")
    path = _prompt_path(p.name)
    old = path.read_text(encoding="utf-8")
    if p.content == old:
        return {"ok": True, "name": path.name, "changed": False, "backup": ""}
    PROMPT_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    backup = PROMPT_BACKUP_DIR / f"{stamp}_{path.name}"
    backup.write_text(old, encoding="utf-8")
    path.write_text(p.content, encoding="utf-8")
    return {"ok": True, "name": path.name, "changed": True, "backup": str(backup.relative_to(ROOT)).replace("\\", "/")}


# ---------- V5.0 integrated story MD manager ----------
def _md_manager_busy():
    with agent.lock:
        if bool(agent.status.get("running")):
            return True, "Canon 正在运行"
    try:
        if bool(agent.dlc_snapshot().get("running")):
            return True, "DLC 正在运行"
    except Exception:
        pass
    try:
        if bool(agent.audit_snapshot().get("running")):
            return True, "剧情一致性审计正在运行"
    except Exception:
        pass
    return False, ""

@app.post("/api/md-manager/parse")
def md_manager_parse(p: MDManagerTextPatch):
    try:
        route = md_parse_route(p.text)
        return {"ok": True, **md_route_dict(route)}
    except MdManagerError as e:
        raise HTTPException(400, str(e))

@app.post("/api/md-manager/preview")
def md_manager_preview(p: MDManagerTextPatch):
    try:
        route = md_parse_route(p.text)
        return {"ok": True, **md_preview(ROOT / "story", route)}
    except MdManagerError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"MD 预览失败：{e}")

@app.post("/api/md-manager/commit")
def md_manager_commit(p: MDManagerCommitPatch):
    _ensure_external_import_idle()
    busy, why = _md_manager_busy()
    if busy:
        raise HTTPException(
            409,
            f"{why}；为避免运行中混用两套设定，当前禁止修改 story MD。停止相关任务后再提交。"
        )
    try:
        route = md_parse_route(p.text)
        return md_commit(ROOT / "story", route, p.before_hash)
    except MdManagerError as e:
        raise HTTPException(409, str(e))
    except Exception as e:
        raise HTTPException(500, f"MD 写入失败：{e}")

@app.get("/api/chapters/export-info")
def chapter_export_info():
    folder = ROOT / "chapters"
    nums = []
    if folder.is_dir():
        for path in folder.glob("*.md"):
            m = CHAPTER_FILE_RE.match(path.name)
            if m:
                nums.append(int(m.group(1)))
    nums.sort()
    return {
        "ok": True,
        "count": len(nums),
        "first": nums[0] if nums else None,
        "last": nums[-1] if nums else None,
    }

@app.get("/api/chapters/export")
def chapter_export(start: int, end: int, format: str = "md"):
    fmt = str(format or "md").lower()
    if fmt not in {"md", "txt", "zip"}:
        raise HTTPException(400, "导出格式只支持 md / txt / zip")
    rows, missing = _chapter_files(start, end)
    base = f"chapters_{start:04d}-{end:04d}"
    headers = {
        "Content-Disposition": f'attachment; filename="{base}.{fmt}"',
        "X-NovelAgent-Export-Count": str(len(rows)),
        "X-NovelAgent-Missing-Count": str(len(missing)),
    }
    if fmt == "zip":
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for _n, path in rows:
                zf.write(path, arcname=path.name)
        return Response(content=buf.getvalue(), media_type="application/zip", headers=headers)
    text = _chapter_export_text(rows, plain=(fmt == "txt"))
    media = "text/markdown; charset=utf-8" if fmt == "md" else "text/plain; charset=utf-8"
    return Response(content=text.encode("utf-8"), media_type=media, headers=headers)

@app.get("/api/chapter/candidate/{chapter_no}/{mode}")
def chapter_candidate_get(chapter_no: int, mode: str):
    try:
        return agent.chapter_candidate_detail(chapter_no, mode)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        raise HTTPException(409, str(e))

@app.get("/api/chapter/{chapter_no}")
def chapter_get(chapter_no: int):
    return agent.chapter_detail(chapter_no)

@app.post("/api/chapter/rewrite")
def chapter_rewrite(p: RewritePatch):
    _ensure_external_import_idle()
    if agent.reader_snapshot().get("running"):
        raise HTTPException(409, "读者版智能分段运行中不能修改 Canon 章节。")
    if agent.repair_snapshot().get("running"):
        raise HTTPException(409, "跨章节审计修复运行中不能修改 Canon 章节。")
    if agent.audit_snapshot().get("running"):
        raise HTTPException(409, "剧情审计运行中不能修改 Canon 章节；请先停止审计。")
    try:
        return agent.rewrite_from(p.chapter_no)
    except Exception as e:
        raise HTTPException(409, str(e))

@app.post("/api/chapter/edit")
def chapter_edit(p: ChapterEditPatch):
    _ensure_external_import_idle()
    if agent.reader_snapshot().get("running"):
        raise HTTPException(409, "读者版智能分段运行中不能修改 Canon 章节。")
    if agent.repair_snapshot().get("running"):
        raise HTTPException(409, "跨章节审计修复运行中不能修改 Canon 章节。")
    if agent.audit_snapshot().get("running"):
        raise HTTPException(409, "剧情审计运行中不能修改 Canon 章节；请先停止审计。")
    try:
        return agent.edit_chapter(
            p.chapter_no, mode=p.mode, target_chars=p.target_chars,
            instruction=p.instruction, provider=p.provider, model=p.model,
            thinking=p.thinking,
        )
    except Exception as e:
        raise HTTPException(409, str(e))

@app.post("/api/chapter/candidate/accept")
def chapter_candidate_accept(p: CandidateAcceptPatch):
    _ensure_external_import_idle()
    if agent.reader_snapshot().get("running"):
        raise HTTPException(409, "读者版智能分段运行中不能修改 Canon 章节。")
    if agent.repair_snapshot().get("running"):
        raise HTTPException(409, "跨章节审计修复运行中不能修改 Canon 章节。")
    if agent.audit_snapshot().get("running"):
        raise HTTPException(409, "剧情审计运行中不能修改 Canon 章节；请先停止审计。")
    try:
        return agent.accept_candidate(p.chapter_no, p.mode)
    except Exception as e:
        raise HTTPException(409, str(e))

# ---------- manual / parallel DLC ----------
@app.get("/api/dlc/status")
def dlc_status():
    return {"ok": True, "status": agent.dlc_snapshot(), "version": VERSION}

@app.get("/api/dlc/markers/{chapter_no}")
def dlc_markers(chapter_no: int):
    try:
        return {"ok": True, **agent.dlc_markers(chapter_no)}
    except Exception as e:
        raise HTTPException(404, str(e))

@app.post("/api/dlc/generate")
def dlc_generate(p: DLCGeneratePatch):
    _ensure_external_import_idle()
    if agent.reader_snapshot().get("running"):
        raise HTTPException(409, "读者版智能分段正在运行；请先停止。")
    ok, msg = agent.start_dlc(
        p.chapter_no, p.scene_id,
        custom_prompt=p.custom_prompt,
        max_tokens=p.max_tokens,
        draw_count=p.draw_count,
    )
    if not ok:
        raise HTTPException(409, msg)
    return {"ok": True, "message": msg}

@app.post("/api/dlc/stop")
def dlc_stop():
    agent.request_stop_dlc()
    return {"ok": True}

@app.get("/api/dlc/candidates/{chapter_no}/{scene_id}")
def dlc_candidates(chapter_no: int, scene_id: str):
    try:
        return {"ok": True, **agent.list_dlc_candidates(chapter_no, scene_id)}
    except Exception as e:
        raise HTTPException(404, str(e))

@app.get("/api/dlc/read/{chapter_no}/{scene_id}")
def dlc_read(chapter_no: int, scene_id: str, candidate_id: str = ""):
    try:
        return {"ok": True, **agent.read_dlc(chapter_no, scene_id, candidate_id=candidate_id)}
    except Exception as e:
        raise HTTPException(404, str(e))

@app.post("/api/dlc/candidate/select")
def dlc_candidate_select(p: DLCCandidatePatch):
    try:
        return {"ok": True, **agent.select_dlc_candidate(p.chapter_no, p.scene_id, p.candidate_id)}
    except Exception as e:
        raise HTTPException(409, str(e))

@app.post("/api/dlc/candidate/delete")
def dlc_candidate_delete(p: DLCCandidatePatch):
    try:
        return {"ok": True, **agent.delete_dlc_candidate(p.chapter_no, p.scene_id, p.candidate_id)}
    except Exception as e:
        raise HTTPException(409, str(e))

# ---------- embedding model control ----------
@app.get("/api/embedding/status")
def embedding_status():
    return embedding_models.status()

@app.post("/api/embedding/start")
def embedding_start():
    try:
        return embedding_models.start()
    except Exception as e:
        raise HTTPException(500, repr(e))

@app.post("/api/embedding/stop")
def embedding_stop():
    try:
        return embedding_models.stop(force=False)
    except Exception as e:
        raise HTTPException(500, repr(e))

@app.post("/api/embedding/restart")
def embedding_restart():
    try:
        return embedding_models.restart()
    except Exception as e:
        raise HTTPException(500, repr(e))

@app.get("/api/events")
def events(request: Request):
    q = hub.subscribe()

    def gen():
        try:
            yield "data: " + json.dumps({"type": "hello"}, ensure_ascii=False) + "\n\n"
            last = time.time()
            while True:
                if time.time() - last > 15:
                    yield ": ping\n\n"
                    last = time.time()
                try:
                    ev = q.get(timeout=1)
                    yield "data: " + json.dumps(ev, ensure_ascii=False) + "\n\n"
                except Exception:
                    pass
        finally:
            hub.unsubscribe(q)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Content-Type": "text/event-stream; charset=utf-8",
            "Cache-Control": "no-cache"
        }
    )

if __name__ == "__main__":
    c = load_config()
    uvicorn.run(
        app,
        host=c["web"]["host"],
        port=int(c["web"]["port"]),
        log_level="info"
    )
