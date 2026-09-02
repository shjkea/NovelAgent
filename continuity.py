"""Continuity primitives shared by Canon generation and full-text audit.

This module is deliberately free of provider and database dependencies so the
critical boundary rules can be tested offline.
"""
from __future__ import annotations

import json
import re
from datetime import datetime


HANDOFF_SCHEMA_VERSION = 2
HANDOFF_LIST_FIELDS = (
    "present_characters",
    "last_actions",
    "completed_events",
    "ongoing_events",
    "new_information",
    "state_changes",
    "do_not_repeat",
    "future_boundaries",
    "uncertainties",
)


def extract_source_tail(text: str, max_chars: int = 2600) -> str:
    """Return a deterministic suffix of Canon prose, never a model paraphrase."""
    text = str(text or "").strip()
    limit = max(500, min(12000, int(max_chars or 2600)))
    if len(text) <= limit:
        return text
    tail = text[-limit:]
    # Prefer a paragraph boundary without sacrificing most of the configured window.
    boundary = tail.find("\n\n")
    if 0 <= boundary <= limit // 3:
        tail = tail[boundary + 2 :]
    return tail.strip()


def _short(value, limit=1200, unknown="unknown"):
    text = str(value or "").strip()
    return (text or unknown)[:limit]


def _string_list(value, item_limit=500, count_limit=24):
    if not isinstance(value, list):
        return []
    out = []
    for item in value:
        if isinstance(item, dict):
            text = json.dumps(item, ensure_ascii=False, separators=(",", ":"))
        else:
            text = str(item or "").strip()
        if text and text not in out:
            out.append(text[:item_limit])
        if len(out) >= count_limit:
            break
    return out


def degraded_handoff(chapter_no: int, source_tail: str, reason: str) -> dict:
    """Safe fallback: retain the real suffix and label every inferred field unknown."""
    return {
        "schema_version": HANDOFF_SCHEMA_VERSION,
        "structured_complete": False,
        "chapter_no": int(chapter_no),
        "status": "degraded",
        "error": _short(reason, 800, "handoff extraction failed"),
        "end_time": "unknown",
        "end_location": "unknown",
        "present_characters": [],
        "last_actions": [],
        "completed_events": [],
        "ongoing_events": [],
        "new_information": [],
        "state_changes": [],
        "scene_closed": "unknown",
        "next_start": "unknown; continue only from the attached Canon source tail",
        "do_not_repeat": [],
        "future_boundaries": [],
        "uncertainties": ["Structured handoff unavailable; do not infer missing facts."],
        "item_states": [],
        "numeric_facts": [],
        "knowledge_states": [],
        "active_decisions": [],
        "evidence_claims": [],
        "scene_signatures": [],
        "source_tail": str(source_tail or ""),
        "source_tail_chars": len(str(source_tail or "")),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }


def normalize_handoff(raw, chapter_no: int, source_tail: str, max_chars: int = 12000,
                      require_structured: bool = False) -> dict:
    """Validate, bound, and attach the deterministic Canon suffix to model output."""
    if not isinstance(raw, dict):
        raise ValueError("handoff is not a JSON object")
    try:
        returned_chapter = int(raw.get("chapter_no"))
    except Exception as exc:
        raise ValueError("handoff.chapter_no is missing or invalid") from exc
    if returned_chapter != int(chapter_no):
        raise ValueError(f"handoff.chapter_no={returned_chapter}, expected {chapter_no}")

    out = {
        "schema_version": HANDOFF_SCHEMA_VERSION,
        "structured_complete": raw.get("structured_complete") is True,
        "chapter_no": int(chapter_no),
        "status": "degraded" if raw.get("status") == "degraded" else "complete",
        "error": _short(raw.get("error"), 800, "") if raw.get("status") == "degraded" else "",
        "end_time": _short(raw.get("end_time")),
        "end_location": _short(raw.get("end_location")),
        "scene_closed": raw.get("scene_closed")
        if raw.get("scene_closed") in (True, False, "unknown")
        else "unknown",
        "next_start": _short(raw.get("next_start")),
        "source_tail": str(source_tail or ""),
        "source_tail_chars": len(str(source_tail or "")),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }
    for field in HANDOFF_LIST_FIELDS:
        out[field] = _string_list(raw.get(field))

    # Imported lazily so continuity primitives remain usable by small tools
    # that only need the legacy seam checks.
    from canon_guard import LEDGER_FIELDS, normalize_ledger_rows, normalize_scene_signatures
    for field in LEDGER_FIELDS:
        out[field] = normalize_ledger_rows(field, raw.get(field), chapter_no)
    out["scene_signatures"] = normalize_scene_signatures(raw.get("scene_signatures"), chapter_no)
    if require_structured:
        if raw.get("structured_complete") is not True:
            raise ValueError("handoff structured_complete is not true")
        if not out["scene_signatures"]:
            raise ValueError("handoff.scene_signatures is required for Canon generation")

    encoded = json.dumps(out, ensure_ascii=False)
    hard_limit = max(4000, min(30000, int(max_chars or 12000)))
    if len(encoded) > hard_limit:
        # Preserve the source tail, scalar end state, and the earliest/highest-value rows.
        trim_fields = list(reversed(HANDOFF_LIST_FIELDS)) + [
            "evidence_claims", "active_decisions",
            "numeric_facts", "item_states", "knowledge_states",
            "scene_signatures",
        ]
        for field in trim_fields:
            minimum = 1 if require_structured and field == "scene_signatures" else 0
            while len(out[field]) > minimum and len(json.dumps(out, ensure_ascii=False)) > hard_limit:
                out[field].pop()
        if len(json.dumps(out, ensure_ascii=False)) > hard_limit:
            raise ValueError("handoff exceeds hard limit after bounded list trimming")
    if require_structured and not out["scene_signatures"]:
        raise ValueError("handoff.scene_signatures was lost during normalization")
    return out


def audit_windows(start: int, end: int, size: int = 4, overlap: int = 1):
    """Create complete overlapping chapter windows; every adjacent seam is covered."""
    start, end = int(start), int(end)
    size = max(2, int(size or 4))
    overlap = max(1, min(size - 1, int(overlap or 1)))
    if start > end:
        return []
    windows = []
    cur = start
    while cur <= end:
        stop = min(end, cur + size - 1)
        windows.append((cur, stop))
        if stop >= end:
            break
        cur = stop - overlap + 1
    return windows


_TIME_RE = re.compile(r"(?<!\d)([01]?\d|2[0-3])[:：]([0-5]\d)(?!\d)")
_DATE_RE = re.compile(r"(?:(\d{4})年)?(1[0-2]|[1-9])月([12]\d|3[01]|[1-9])日")
_AUTUMN = ("秋风", "秋意", "金黄的落叶", "枫叶转红", "桂花飘香", "深秋", "秋季")
_FLASHBACK = ("时间回到", "回忆", "倒叙", "几小时前", "那天早些时候", "梦见")
_IGNORANCE_RE = re.compile(r"不知道|不知情|从未听说|第一次听说|毫不知情|并不清楚|不清楚")
_OPEN_IGNORANCE_RE = re.compile(
    r"谁|哪(?:里|儿|个|一|来)?|什么|为何|为什么|怎么|如何|从何|何人|何处|何时|来源|归属|来历"
)
_KNOWLEDGE_ACQUISITION_RE = re.compile(r"知道|得知|获悉|了解到|听说")
_KNOWLEDGE_MODAL_RE = re.compile(r"已经|曾经|原来|将会|即将|将|会")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[。！？!?])|[\r\n]+")
_CLAUSE_SPLIT_RE = re.compile(r"[，,；;：:]")


def _semantic_tokens(text, minimum=2):
    parts = re.split(
        r"[，。；、\s：:及与并和]|知道|得知|已经|完成|开始|下一章|本章|随后|人物|角色",
        str(text or ""),
    )
    return [x for x in parts if len(x) >= minimum]


def _completed_event_replay_evidence(text, event):
    """Return current-prose evidence only for a high-confidence event replay.

    Completed-event guards are intentionally stricter than semantic Review.
    Adjacent chapters often continue the same investigation, training, or data
    analysis, so generic words such as ``AI`` and ``记录`` are not enough to
    prove that a finished action was executed again.  Require two exact
    event-specific fragments and quote the current prose that contains them.
    """
    text = str(text or "")
    tokens = list(dict.fromkeys(_semantic_tokens(event)))
    for action in ("训练", "测试", "离开", "离场", "调查", "救援", "实验"):
        if action in str(event or "") and action not in tokens:
            tokens.append(action)
    matched = [token for token in tokens[:8] if token in text]
    if len(matched) < 2:
        return ""

    # The evidence must come from one bounded local passage.  Tokens scattered
    # across unrelated scenes are a continuation signal for semantic Review,
    # not a deterministic replay proof.
    positions = sorted((text.find(token), token) for token in matched)
    for index, (start, first) in enumerate(positions):
        if start < 0:
            continue
        for second_start, second in positions[index + 1:]:
            if second_start - start > 360:
                break
            left = max(0, start - 100)
            right = min(len(text), max(start + len(first), second_start + len(second)) + 180)
            return text[left:right].strip()
    return ""


def _normalize_knowledge_evidence(text):
    text = _IGNORANCE_RE.sub("", str(text or ""))
    text = _KNOWLEDGE_ACQUISITION_RE.sub("", text)
    text = _KNOWLEDGE_MODAL_RE.sub("", text)
    return re.sub(r"[^0-9A-Za-z\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]", "", text)


def _knowledge_fact_fragments(fact):
    """Return proposition fragments, excluding who acquired the information."""
    out = []
    for clause in _CLAUSE_SPLIT_RE.split(str(fact or "")):
        clause = clause.strip()
        if not clause:
            continue
        acquired = _KNOWLEDGE_ACQUISITION_RE.split(clause)
        candidate = acquired[-1] if len(acquired) > 1 else clause
        normalized = _normalize_knowledge_evidence(candidate)
        if len(normalized) >= 4 and normalized not in out:
            out.append(normalized)
    return out


def _knowledge_ignorance_contexts(text):
    """Yield tightly bounded clauses that actually negate a known proposition."""
    for sentence in _SENTENCE_SPLIT_RE.split(str(text or "")):
        sentence = sentence.strip()
        if not sentence:
            continue
        clauses = [part.strip() for part in _CLAUSE_SPLIT_RE.split(sentence)]
        for index, clause in enumerate(clauses):
            for match in _IGNORANCE_RE.finditer(clause):
                suffix = clause[match.end():]
                # Unknown actor, source, ownership, reason, place, or time does
                # not mean the character forgot the surrounding proposition.
                if _OPEN_IGNORANCE_RE.search(suffix):
                    continue
                start = max(0, match.start() - 80)
                stop = min(len(clause), match.end() + 120)
                context = clause[start:stop]

                # A bare/deictic expression may introduce or follow exactly one
                # adjacent explanatory clause. Open questions never use this path.
                suffix_key = _normalize_knowledge_evidence(suffix)
                if len(suffix_key) <= 6:
                    neighbors = []
                    if index > 0:
                        neighbors.append(clauses[index - 1][-120:])
                    if index + 1 < len(clauses):
                        neighbors.append(clauses[index + 1][:120])
                    for neighbor in neighbors:
                        yield f"{context}，{neighbor}"
                yield context


def _knowledge_regression_match(text, fact):
    fragments = _knowledge_fact_fragments(fact)
    if not fragments:
        return ""
    for context in _knowledge_ignorance_contexts(text):
        normalized = _normalize_knowledge_evidence(context)
        if any(fragment in normalized for fragment in fragments):
            return context
    return ""


def _last_time(text):
    rows = list(_TIME_RE.finditer(str(text or "")))
    if not rows:
        return None, ""
    m = rows[-1]
    return int(m.group(1)) * 60 + int(m.group(2)), m.group(0)


def _first_time(text):
    m = _TIME_RE.search(str(text or ""))
    if not m:
        return None, ""
    return int(m.group(1)) * 60 + int(m.group(2)), m.group(0)


def _edge_date(text, last=False):
    rows = list(_DATE_RE.finditer(str(text or "")))
    if not rows:
        return None, ""
    m = rows[-1] if last else rows[0]
    year = int(m.group(1) or 0)
    value = year * 372 + int(m.group(2)) * 31 + int(m.group(3))
    return value, m.group(0)


def deterministic_boundary_findings(previous_text: str, current_text: str,
                                    previous_handoff=None, current_task="",
                                    next_task="", month=None) -> list[dict]:
    """High-confidence local guards. Semantic review remains responsible for nuance."""
    previous_text = str(previous_text or "")
    current_text = str(current_text or "")
    joined = current_text[:2500]
    findings = []

    def add(code, message, evidence="", severity="REVIEW"):
        findings.append({
            "code": code,
            "severity": severity,
            "message": message,
            "evidence": str(evidence or "")[:500],
        })

    is_flashback = any(mark in joined for mark in _FLASHBACK)
    prev_min, prev_label = _last_time(previous_text[-5000:])
    cur_min, cur_label = _first_time(joined)
    if prev_min is not None and cur_min is not None and cur_min < prev_min and not is_flashback:
        add("TIME_REGRESSION", f"相邻章节时间无说明倒退：{prev_label} -> {cur_label}", cur_label, "REVIEW")
    prev_date, prev_date_label = _edge_date(previous_text[-5000:], last=True)
    cur_date, cur_date_label = _edge_date(joined, last=False)
    if prev_date is not None and cur_date is not None:
        comparable = (prev_date >= 372 and cur_date >= 372) or (prev_date < 372 and cur_date < 372)
        if comparable and cur_date < prev_date and not is_flashback:
            add("DATE_REGRESSION", f"相邻章节日期无说明倒退：{prev_date_label} -> {cur_date_label}", cur_date_label, "REVIEW")

    handoff = previous_handoff if isinstance(previous_handoff, dict) else {}
    closed = handoff.get("scene_closed") is True
    location = str(handoff.get("end_location") or "").strip()
    location_stem = re.sub(r"(?:之?内|之?外|里面|外面|门口)$", "", location)
    if closed and location and location != "unknown" and (location in joined or (len(location_stem) >= 2 and location_stem in joined)):
        transition_words = (
            "来到", "返回", "回到", "抵达", "走进", "赶到", "第二天", "翌日", "次日",
            "紧接着", "随后", "继续", "醒来", "睁开眼", "起床", "当天", "当晚",
            "几分钟后", "过了一会儿",
        )
        if not any(word in joined[:800] for word in transition_words) and not is_flashback:
            add("CLOSED_SCENE_REOPENED", f"上一章已关闭的场景在无过渡情况下重开：{location}", location, "REVIEW")

    completed = _string_list(handoff.get("completed_events"), item_limit=180)
    for event in completed:
        evidence = _completed_event_replay_evidence(joined, event)
        if evidence:
            add("COMPLETED_EVENT_REPEATED", f"疑似重新执行上一章已完成事件：{event}", evidence, "REVIEW")
            break

    known = _string_list(handoff.get("new_information"), item_limit=180)
    for fact in known:
        evidence = _knowledge_regression_match(joined, fact)
        if evidence:
            add("KNOWLEDGE_REGRESSION", f"人物对上一章新获信息表现出无依据的不知情：{fact}", evidence, "REVIEW")
            break

    state_rows = _string_list(handoff.get("state_changes"), item_limit=220)
    reversal_words = ("毫发无伤", "伤势痊愈", "已经痊愈", "物品不在身边", "东西丢了", "身份变成", "改任", "完好如初")
    transition_words = ("治疗", "康复", "归还", "交给", "遗失", "调任", "被任命", "前往", "抵达", "转移")
    if any(word in joined for word in reversal_words) and not any(word in joined[:1200] for word in transition_words):
        for state in state_rows:
            tokens = _semantic_tokens(state)
            bigrams = {state[i:i + 2] for i in range(max(0, len(state) - 1))}
            if any(token in joined for token in tokens[:4]) or sum(x in joined for x in bigrams) >= 2:
                add("STATE_REGRESSION", f"人物身份、伤势、物品或地点状态疑似无依据变化：{state}", state, "REVIEW")
                break

    future_rows = _string_list(handoff.get("future_boundaries"), item_limit=240)
    if next_task:
        future_rows.append(str(next_task)[:500])
    for boundary in future_rows:
        tokens = _semantic_tokens(boundary, minimum=3)
        if tokens and sum(token in current_text for token in tokens[:6]) >= min(3, len(tokens)):
            done_words = ("完成", "结束", "解决", "查明", "成功", "正式确定", "已经")
            if any(word in current_text for word in done_words):
                add("FUTURE_TASK_CONSUMED", "当前章疑似提前完成后续明确任务", boundary, "REVIEW")
                break

    try:
        month_num = int(month) if month is not None else None
    except Exception:
        month_num = None
    if month_num == 3:
        for marker in _AUTUMN:
            if marker in current_text:
                add("SEASON_CONFLICT", f"三月正文出现明确秋季标记：{marker}", marker, "REVIEW")
                break

    # Strong repeated-process signal: four distinctive actions appearing in both seams.
    process_terms = ("训练", "测试", "喝水", "记录", "离开", "离场", "收拾", "签到")
    shared = [term for term in process_terms if term in previous_text[-5000:] and term in current_text[:5000]]
    for row in _string_list(handoff.get("do_not_repeat"), item_limit=240):
        for term in re.split(r"[，。；、\s]+", row):
            if len(term) >= 2 and term in previous_text[-5000:] and term in current_text[:5000] and term not in shared:
                shared.append(term)
    if len(shared) >= 4 and not is_flashback:
        add("ADJACENT_PROCESS_REPLAY", "相邻章节疑似大段重演同一流程", "、".join(shared), "REVIEW")

    return findings


def adjacent_seams_covered(windows, start: int, end: int) -> bool:
    covered = set()
    for a, b in windows:
        covered.update((n, n + 1) for n in range(int(a), int(b)))
    return all((n, n + 1) in covered for n in range(int(start), int(end)))
