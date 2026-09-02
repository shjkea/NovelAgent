"""Structured cross-chapter Canon guards.

The normal memory store is optimized for semantic retrieval.  This module is
deliberately smaller and stricter: it folds chapter Handoff deltas into a
deterministic ledger, formats the ledger for prose-producing stages, and runs
only high-confidence local checks that a model verdict may not override.
"""
from __future__ import annotations

import json
import hashlib
import re
from pathlib import Path


LEDGER_FIELDS = (
    "item_states",
    "numeric_facts",
    "knowledge_states",
    "active_decisions",
    "evidence_claims",
)

_KEY_FIELDS = {
    "item_states": "item_id",
    "numeric_facts": "fact_id",
    "knowledge_states": "knowledge_id",
    "active_decisions": "decision_id",
    "evidence_claims": "claim_id",
}

_SENTENCE_RE = re.compile(r"[^。！？!?\r\n]{1,260}[。！？!?]?")
_ARABIC_NUMBER_RE = re.compile(r"(?<![A-Za-z0-9.])(\d+(?:\.\d+)?)(?![A-Za-z0-9.])")
_CHINESE_NUMBER_RE = re.compile(r"[零〇一二两三四五六七八九十百千万]+")
_HARD_CONCLUSION_TOKEN_RE = re.compile(
    r"确认|确定|证明|排除|必然|不存在|毫无关系|因果|可重复|导致|造成|使得|让"
)
_EXPLICIT_THIN_SAMPLE_RE = re.compile(
    r"(?:(?:只|仅)在|只有|仅有|只新增|仅新增|只得到|仅得到|只拿到|仅拿到|"
    r"只收集到|仅收集到|只采到|仅采到|只测得|仅测得|只记录了|仅记录了|"
    r"只填写了|仅填写了)[^。！？!?\r\n]{0,28}"
    r"(?:一(?:次|条|份|组|个|例|行))"
    r"(?:新)?(?:样本|记录|观测|数据|结果|测试)?|"
    r"(?:第一次|首次|单次|首条|首份)[^。！？!?\r\n]{0,16}"
    r"(?:样本|记录|观测|数据|测试|实验)"
)
_EVIDENCE_CONTEXT_RE = re.compile(r"AI|分析|对照|变量|样本|观测|数据|实验|测试|记录")
_EVIDENCE_CONCLUSION_LINK_RE = re.compile(
    r"据此|因此|由此|所以|这(?:说明|表明|证明)|"
    r"AI|分析|数据|样本|观测|观察|记录|结果|"
    r"(?:这|它|该)(?:一|个|次|条|组|份|项|些)?(?:现象|结果|数据|记录|样本|变化)?"
)
_NEGATED_OR_UNCERTAIN_CONCLUSION_RE = re.compile(
    r"(?:不|未)(?:确认|确定|证明|排除|说明|表明|判定|建立|支持)|"
    r"(?:无法|不能|不可|未能|尚未|尚不能|仍不能|并不能|并未|没有|"
    r"不足以|难以|不代表|不等于|不构成|不具有|"
    r"(?:暂无|尚无)(?:依据|证据)|缺乏[^，,；;。！？!?]{0,8}(?:依据|证据))"
    r"[^，,；;。！？!?]{0,24}"
    r"(?:确认|确定|证明|排除|说明|表明|判定|建立|支持|因果|可重复)|"
    r"(?:可能|也许|或许|似乎|看起来|未必|不一定|假设|推测)"
    r"[^，,；;。！？!?]{0,18}(?:导致|造成|使得|让|因果|可重复)"
)
_EVIDENCE_CLAIM_TARGET_RE = re.compile(
    r"因果|关联|关系|规律|现象|变量|因素|原因|解释|可能性|"
    r"可重复|结论|影响|作用|效果|有效|无效|排除|不存在"
)
_CLAUSE_RE = re.compile(r"[^，,；;。！？!?\r\n]{1,320}[，,；;。！？!?]?")
_KNOWLEDGE_REGRESSION_RE = re.compile(
    r"第一次(?:听到|听说|知道|见到)|从未(?:听到|听说|知道|见过)|"
    r"(?:还|并|完全)?不知道(?:这个|该|其)?(?:名字|名称|事情|消息)?|毫不知情"
)
_KNOWLEDGE_ANAPHORA_RE = re.compile(
    r"(?:这个|那个|该|其)(?:名字|名称|事情|消息|地点|位置|身份|组织)|"
    r"(?:这|那)(?:件事|个名字|个名称|条消息|个地点|个位置)"
)
_KNOWLEDGE_GENERIC_TERMS = {
    "知道", "不知", "不知道", "得知", "了解", "听说", "见过",
    "名字", "名称", "事情", "消息", "情况", "相关", "状态",
}
_ITEM_RETURN_RE = re.compile(
    r"(?:给|回复|回给)(?P<recipient>[\u3400-\u4dbf\u4e00-\u9fff]{1,10})"
    r"[^。！？!?\r\n]{0,40}[“\"](?:[^”\"。！？!?]{0,30}[。！？!?]?){0,2}"
    r"[^”\"。！？!?]{0,20}东西你留着(?:吧)?[。！？!?]?[”\"]"
)
_TRANSIT_RE = re.compile(r"公交车|大巴|出租车|网约车|地铁|车厢|车上|后排|车窗")
_HOME_ACTION_RE = re.compile(
    r"(?:书桌|抽屉|卧室|房间|家里|家中)[^。！？!?\r\n]{0,50}"
    r"(?:拿出|放进|翻出|打开|拉开|合上|收进|摆在|放在)|"
    r"(?:拿出|放进|翻出|打开|拉开|合上|收进|摆在|放在)"
    r"[^。！？!?\r\n]{0,50}(?:书桌|抽屉|卧室|房间|家里|家中)"
)
_ARRIVAL_RE = re.compile(r"下(?:了)?车|到家|回到家|回家后|进(?:了)?屋|走进家门|进了房间")
_SCENE_SWITCH_RE = re.compile(
    r"与此同时|同一时间|另一边|另一头|镜头一转|画面一转|"
    r"(?:第二天|次日|翌日|隔天|当晚|次日上午|次日下午)|"
    r"(?:周|星期)[一二三四五六日天](?:上午|下午|晚上|放学)?"
)
_NUMERIC_ROLE_WORDS = (
    "预算", "余额", "找零", "原价", "报价", "定金", "押金", "上限",
    "随身", "带了", "借了", "收到", "准备", "打算",
)
_NUMERIC_GENERIC_TERMS = {
    "成交价", "最终成交价", "价格", "售价", "原价", "报价", "预算",
    "余额", "定金", "押金", "费用", "金额", "总价", "单价", "数量",
    "时长", "持续时长", "日期", "时间", "测量值", "读数", "数值",
}
_NUMERIC_CHANGE_RE = re.compile(
    r"而是|改为|改成|变为|调整为|修正为|应为|实为|"
    r"降到|降至|涨到|涨至|最后(?:是|为)|最终(?:是|为)"
)
_NUMERIC_ASSERTION_RE = re.compile(
    r"而是|实为|实际(?:是|为)|应为|定为|定在|固定为|等于|"
    r"共计|合计|总计|总共|成交价(?:是|为)?|售价(?:是|为)?|"
    r"价格(?:是|为)?|数量(?:是|为)?|时长(?:是|为)?|持续|耗时|"
    r"花了|支付(?:了)?|付了|卖了|卖到|买了|报价为|"
    r"改为|改成|变为|调整为|修正为|降到|降至|涨到|涨至|是|为"
)
_NUMERIC_NEGATION_OR_COMPARISON_RE = re.compile(
    r"(?:不是|并非|不等于|未达到|不到|不止|高于|低于|超过|少于|多于|"
    r"接近|大约|约|将近|至少|至多|从|由)\s*$"
)

QUALITY_LOGIC_DIMENSIONS = (
    "numbers",
    "time_place",
    "item_ownership",
    "action_order",
    "evidence_conclusion",
)


def _clean_text(value, limit=500):
    text = str(value or "").strip()
    return text[:limit]


def _clean_terms(value, limit=12, item_limit=80):
    if not isinstance(value, list):
        return []
    out = []
    for item in value:
        text = _clean_text(item, item_limit)
        if text and text not in out:
            out.append(text)
        if len(out) >= limit:
            break
    return out


def recent_chapter_fulltexts(root, before_chapter, count=3, max_chars=30000):
    """Return bounded full prose for style/repetition checks, newest chapters intact."""
    root = Path(root)
    before_chapter = max(1, int(before_chapter))
    count = max(0, min(6, int(count or 0)))
    max_chars = max(6000, min(60000, int(max_chars or 30000)))
    blocks = []
    for chapter_no in range(max(1, before_chapter - count), before_chapter):
        path = root / "chapters" / f"{chapter_no:04d}.md"
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8").strip()
        if text:
            blocks.append((chapter_no, text))
    if not blocks:
        return "（无最近正文；第1章或历史正文不可用。）"

    rendered = "\n\n".join(f"【第{chapter_no}章正文】\n{text}" for chapter_no, text in blocks)
    if len(rendered) <= max_chars:
        return rendered

    # Keep material from every requested chapter.  Trimming only occurs for
    # unusually long chapters; newest chapters receive any remaining budget.
    heading_chars = sum(len(f"【第{chapter_no}章正文】\n") for chapter_no, _ in blocks)
    separators = max(0, len(blocks) - 1) * 2
    body_budget = max(len(blocks) * 1200, max_chars - heading_chars - separators)
    fair_share = max(1200, body_budget // len(blocks))
    clipped = []
    remaining = body_budget
    for index, (chapter_no, text) in enumerate(blocks):
        chapters_left = len(blocks) - index
        allowance = max(1200, remaining // chapters_left)
        allowance = min(len(text), max(fair_share, allowance))
        if len(text) > allowance:
            text = text[:allowance].rstrip() + "\n（本章超长，后文仅因上下文上限省略。）"
        clipped.append(f"【第{chapter_no}章正文】\n{text}")
        remaining -= min(len(text), allowance)
    return "\n\n".join(clipped)[:max_chars]


def normalize_light_quality_checks(value):
    """Normalize the web-fiction quality rubric and separate blockers from advice."""
    raw = value if isinstance(value, dict) else {}
    logic_raw = raw.get("chapter_logic") if isinstance(raw.get("chapter_logic"), dict) else {}
    logic = {}
    blocking = []
    advisories = []
    labels = {
        "numbers": "章内数字",
        "time_place": "章内时间地点",
        "item_ownership": "章内物品归属",
        "action_order": "章内动作顺序",
        "evidence_conclusion": "证据与结论强度",
    }
    for dimension in QUALITY_LOGIC_DIMENSIONS:
        row = logic_raw.get(dimension)
        row = row if isinstance(row, dict) else {}
        status = str(row.get("status") or "UNKNOWN").strip().upper()
        if status not in {"PASS", "ISSUE", "NA", "UNKNOWN"}:
            status = "UNKNOWN"
        evidence = _clean_text(row.get("evidence"), 300)
        # A model label without evidence cannot spend a revision round.
        if status == "ISSUE" and evidence:
            blocking.append({
                "key": f"chapter_logic_{dimension}",
                "bucket": "continuity",
                "message": f"{labels[dimension]}存在明确问题：{evidence}",
            })
        elif status == "ISSUE":
            status = "UNKNOWN"
        logic[dimension] = {"status": status, "evidence": evidence}

    def graded(name, allowed, default, label, severe_status):
        row = raw.get(name)
        row = row if isinstance(row, dict) else {}
        status = str(row.get("status") or default).strip().upper()
        if status not in allowed:
            status = default
        evidence = _clean_terms(row.get("evidence"), 4, 220)
        reason = _clean_text(row.get("reason"), 300)
        detail = "；".join(evidence) or reason
        if status == severe_status and detail:
            blocking.append({
                "key": name,
                "bucket": "repetition" if name == "cross_chapter_repetition" else "style",
                "message": f"{label}：{detail}",
            })
        elif status not in {default, "SUFFICIENT", "CLEAR"} and detail:
            advisories.append(f"{label}：{detail}")
        return {"status": status, "evidence": evidence, "reason": reason}

    scene = graded(
        "scene_sufficiency", {"SUFFICIENT", "THIN", "EMPTY", "UNKNOWN"},
        "UNKNOWN", "场景推进不足", "EMPTY",
    )
    repetition = graded(
        "cross_chapter_repetition", {"CLEAR", "NOTICEABLE", "SEVERE", "UNKNOWN"},
        "UNKNOWN", "最近三章出现低变化重复", "SEVERE",
    )
    leakage = graded(
        "constraint_leakage", {"CLEAR", "NOTICEABLE", "SEVERE", "UNKNOWN"},
        "UNKNOWN", "约束显性泄漏到正文", "SEVERE",
    )
    normalized = {
        "chapter_logic": logic,
        "scene_sufficiency": scene,
        "cross_chapter_repetition": repetition,
        "constraint_leakage": leakage,
    }
    return normalized, blocking, list(dict.fromkeys(advisories))


def verify_canon_publish(root, chapter_no, db_row, db_last_chapter, last_db_row=None,
                         expected_files=None, expected_fields=None, check_state=True):
    """Return exact post-publish integrity errors without mutating Canon."""
    root = Path(root)
    chapter_no = int(chapter_no)
    db_last_chapter = int(db_last_chapter)
    db_row = db_row if isinstance(db_row, dict) else {}
    last_db_row = last_db_row if isinstance(last_db_row, dict) else db_row
    errors = []

    for key, expected in (expected_fields or {}).items():
        if key in {"generation_seconds", "revision_seconds", "model_tokens", "chars"}:
            continue
        if str(db_row.get(key) or "") != str(expected or ""):
            errors.append(f"database field mismatch: {key}")

    for item in expected_files or []:
        target = root / str(item.get("target") or "")
        expected_hash = str(item.get("sha256") or "")
        if not target.is_file():
            errors.append(f"published file missing: {item.get('target')}")
        elif expected_hash and hashlib.sha256(target.read_bytes()).hexdigest() != expected_hash:
            errors.append(f"published file hash mismatch: {item.get('target')}")

    text_pairs = (
        ("final", root / "chapters" / f"{chapter_no:04d}.md"),
        ("summary", root / "summaries" / f"{chapter_no:04d}.md"),
    )
    for field, path in text_pairs:
        if not path.is_file():
            errors.append(f"Canon file missing: {path.relative_to(root)}")
        elif path.read_text(encoding="utf-8").strip() != str(db_row.get(field) or "").strip():
            errors.append(f"Canon file/database mismatch: {field}")

    json_pairs = (
        ("handoff", root / "handoffs" / f"{chapter_no:04d}.json"),
        ("review", root / "reviews" / f"{chapter_no:04d}.json"),
    )
    parsed_handoff = None
    for field, path in json_pairs:
        try:
            file_value = json.loads(path.read_text(encoding="utf-8"))
            db_value = json.loads(str(db_row.get(field) or "{}"))
            if file_value != db_value:
                errors.append(f"Canon file/database mismatch: {field}")
            if field == "handoff":
                parsed_handoff = file_value
        except Exception:
            errors.append(f"Canon JSON invalid: {path.relative_to(root)}")
    if not isinstance(parsed_handoff, dict):
        errors.append("Handoff is not an object")
    elif (
        parsed_handoff.get("status") != "complete"
        or parsed_handoff.get("structured_complete") is not True
        or int(parsed_handoff.get("chapter_no", 0) or 0) != chapter_no
    ):
        errors.append("Handoff is not a complete structured record")

    if check_state and chapter_no == db_last_chapter:
        try:
            state = json.loads((root / "state.json").read_text(encoding="utf-8"))
            if int(state.get("last_canon_chapter", -1)) != db_last_chapter:
                errors.append("state.json last_canon_chapter mismatch")
            if int(state.get("next_chapter", -1)) != db_last_chapter + 1:
                errors.append("state.json next_chapter mismatch")
            # Older journals hashed the exact provider output before file-edge
            # whitespace was normalized. New commits store normalized text, but
            # recovery must still validate an old exact hash correctly.
            last_final = str(last_db_row.get("final") or "")
            expected_hash = hashlib.sha256(last_final.encode("utf-8")).hexdigest()
            if str(state.get("last_canon_hash") or "") != expected_hash:
                errors.append("state.json last_canon_hash mismatch")
        except Exception:
            errors.append("state.json is missing or invalid")
        try:
            current_state = json.loads((root / "current_state.json").read_text(encoding="utf-8"))
            if int(current_state.get("as_of_chapter", -1)) != db_last_chapter:
                errors.append("current_state.json chapter mismatch")
        except Exception:
            errors.append("current_state.json is missing or invalid")
    return list(dict.fromkeys(errors))


def _stable_id(value):
    return re.sub(r"[^0-9A-Za-z\u3400-\u4dbf\u4e00-\u9fff]+", "_", str(value or "").strip()).strip("_")[:120]


def normalize_ledger_rows(field, value, chapter_no=0, count_limit=16):
    """Normalize one structured Handoff delta without inventing missing facts."""
    if field not in LEDGER_FIELDS or not isinstance(value, list):
        return []
    key_field = _KEY_FIELDS[field]
    out = []
    for raw in value:
        if not isinstance(raw, dict):
            continue
        row = {}
        key = _stable_id(raw.get(key_field))
        if not key:
            continue
        row[key_field] = key
        row["chapter_no"] = int(chapter_no or raw.get("chapter_no") or 0)
        row["status"] = _clean_text(raw.get("status") or "active", 40).lower()
        row["evidence"] = _clean_text(raw.get("evidence"), 300)

        if field == "item_states":
            row.update({
                "name": _clean_text(raw.get("name"), 120),
                "aliases": _clean_terms(raw.get("aliases"), 8, 60),
                "owner": _clean_text(raw.get("owner"), 100),
                "holder": _clean_text(raw.get("holder"), 100),
                "location": _clean_text(raw.get("location"), 160),
                "quantity": _clean_text(raw.get("quantity"), 80),
                "condition": _clean_text(raw.get("condition"), 160),
            })
        elif field == "numeric_facts":
            raw_value = raw.get("value")
            if isinstance(raw_value, (int, float)):
                value_text = str(raw_value)
            else:
                value_text = _clean_text(raw_value, 80)
            row.update({
                "subject": _clean_text(raw.get("subject"), 180),
                "value": value_text,
                "unit": _clean_text(raw.get("unit"), 40),
                "aliases": _clean_terms(raw.get("aliases"), 8, 60),
                "kind": _clean_text(raw.get("kind") or "fact", 40),
            })
        elif field == "knowledge_states":
            row.update({
                "character": _clean_text(raw.get("character"), 100),
                "fact": _clean_text(raw.get("fact"), 240),
                "fact_terms": _clean_terms(raw.get("fact_terms"), 10, 80),
                "knows": raw.get("knows") is True,
            })
        elif field == "active_decisions":
            row.update({
                "character": _clean_text(raw.get("character"), 100),
                "decision": _clean_text(raw.get("decision"), 260),
                "change_requires": _clean_text(raw.get("change_requires"), 260),
            })
        elif field == "evidence_claims":
            try:
                sample_count = int(raw.get("sample_count"))
            except (TypeError, ValueError):
                sample_count = 0
            confidence = _clean_text(raw.get("confidence") or "UNKNOWN", 20).upper()
            if confidence not in {"CONFIRMED", "HIGH", "MEDIUM", "LOW", "UNKNOWN"}:
                confidence = "UNKNOWN"
            row.update({
                "subject": _clean_text(raw.get("subject"), 200),
                "observation": _clean_text(raw.get("observation"), 260),
                "conclusion": _clean_text(raw.get("conclusion"), 260),
                "sample_count": max(0, sample_count),
                "confidence": confidence,
                "limits": _clean_text(raw.get("limits"), 260),
            })
        out.append(row)
        if len(out) >= count_limit:
            break
    return out


def normalize_scene_signatures(value, chapter_no=0, count_limit=4):
    if not isinstance(value, list):
        return []
    out = []
    for raw in value:
        if not isinstance(raw, dict):
            continue
        location = _clean_text(raw.get("location"), 160)
        purpose = _clean_text(raw.get("purpose"), 220)
        if not location and not purpose:
            continue
        out.append({
            "scene_id": _stable_id(raw.get("scene_id") or f"c{int(chapter_no):04d}_s{len(out)+1}"),
            "chapter_no": int(chapter_no or raw.get("chapter_no") or 0),
            "location": location,
            "characters": _clean_terms(raw.get("characters"), 8, 80),
            "entry_trigger": _clean_text(raw.get("entry_trigger"), 220),
            "purpose": purpose,
            "props": _clean_terms(raw.get("props"), 12, 80),
            "beats": _clean_terms(raw.get("beats"), 12, 120),
            "outcome": _clean_text(raw.get("outcome"), 260),
            "closed": raw.get("closed") if raw.get("closed") in (True, False, "unknown") else "unknown",
        })
        if len(out) >= count_limit:
            break
    return out


def empty_ledger(before_chapter=1):
    return {
        "schema_version": 1,
        "before_chapter": int(before_chapter),
        "source_chapters": [],
        **{field: [] for field in LEDGER_FIELDS},
        "recent_scenes": [],
    }


def build_canon_ledger(root, before_chapter, recent_scene_limit=10):
    """Fold all trusted structured Handoffs before a chapter into current state."""
    root = Path(root)
    before_chapter = max(1, int(before_chapter))
    ledger = empty_ledger(before_chapter)
    maps = {field: {} for field in LEDGER_FIELDS}
    scenes = []
    handoff_dir = root / "handoffs"
    for chapter_no in range(1, before_chapter):
        path = handoff_dir / f"{chapter_no:04d}.json"
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if (
            not isinstance(data, dict)
            or data.get("status") != "complete"
            or data.get("structured_complete") is not True
        ):
            continue
        structured = any(data.get(field) for field in LEDGER_FIELDS) or bool(data.get("scene_signatures"))
        if not structured:
            continue
        ledger["source_chapters"].append(chapter_no)
        for field in LEDGER_FIELDS:
            key_field = _KEY_FIELDS[field]
            for row in normalize_ledger_rows(field, data.get(field), chapter_no):
                key = row[key_field]
                status = row.get("status")
                # Item terminal states are still Canon: a consumed, lost, or
                # destroyed object must not silently become available again.
                # The other ledgers describe currently binding facts, so their
                # explicit inactive deltas remove the prior row.
                inactive = {
                    "numeric_facts": {"superseded"},
                    "knowledge_states": {"superseded"},
                    "active_decisions": {"resolved", "revoked", "superseded"},
                    "evidence_claims": {"superseded"},
                }.get(field, set())
                if status in inactive:
                    maps[field].pop(key, None)
                else:
                    maps[field][key] = row
        scenes.extend(normalize_scene_signatures(data.get("scene_signatures"), chapter_no))

    for field in LEDGER_FIELDS:
        ledger[field] = sorted(
            maps[field].values(),
            key=lambda row: (int(row.get("chapter_no", 0)), str(row.get(_KEY_FIELDS[field], ""))),
        )
    ledger["recent_scenes"] = scenes[-max(1, int(recent_scene_limit)):]
    return ledger


def format_canon_ledger(ledger, max_chars=14000):
    ledger = ledger if isinstance(ledger, dict) else empty_ledger()
    compact = {
        "before_chapter": ledger.get("before_chapter"),
        "source_chapters": ledger.get("source_chapters", [])[-20:],
        **{field: ledger.get(field, []) for field in LEDGER_FIELDS},
        "recent_scenes": ledger.get("recent_scenes", []),
    }
    text = json.dumps(compact, ensure_ascii=False, indent=2)
    limit = max(3000, int(max_chars or 14000))
    if len(text) <= limit:
        return text
    # Keep the prompt valid JSON under all circumstances. Recent scene history
    # is reduced first; if the durable ledger itself is unusually large, keep
    # the newest rows and state exactly how many older rows were omitted.
    compact["recent_scenes"] = compact["recent_scenes"][-4:]
    compact["_truncated_counts"] = {}
    text = json.dumps(compact, ensure_ascii=False, separators=(",", ":"))
    if len(text) <= limit:
        return text
    removal_order = (
        "recent_scenes", "evidence_claims", "active_decisions",
        "knowledge_states", "numeric_facts", "item_states",
    )
    while len(text) > limit:
        removed = False
        for field in removal_order:
            rows = compact.get(field) or []
            if not rows:
                continue
            rows.pop(0)
            compact["_truncated_counts"][field] = compact["_truncated_counts"].get(field, 0) + 1
            removed = True
            text = json.dumps(compact, ensure_ascii=False, separators=(",", ":"))
            if len(text) <= limit:
                break
        if not removed:
            break
    if len(text) <= limit:
        return text
    minimal = {
        "before_chapter": compact.get("before_chapter"),
        "source_chapters": compact.get("source_chapters", [])[-8:],
        "_truncated_counts": compact.get("_truncated_counts", {}),
        "error": "Canon ledger exceeded prompt limit; Plan gate must not infer omitted facts",
    }
    return json.dumps(minimal, ensure_ascii=False, separators=(",", ":"))


def _semantic_terms(text, minimum=2):
    parts = re.split(r"[^0-9A-Za-z\u3400-\u4dbf\u4e00-\u9fff]+|已经|当前|这个|那个|事情|状态|相关|进行", str(text or ""))
    return [part for part in parts if len(part) >= minimum]


def _row_terms(row, names):
    out = []
    for name in names:
        value = row.get(name)
        if isinstance(value, list):
            values = value
        else:
            values = [value]
        for item in values:
            for term in _semantic_terms(item):
                if term not in out:
                    out.append(term)
    return out


def _knowledge_topic_terms(state, character_names):
    """Return terms specific enough to tie an ignorance claim to one fact."""
    explicit = _row_terms(state, ("fact_terms",))
    candidates = explicit or _row_terms(state, ("fact",))
    character = str(state.get("character") or "").strip()
    out = []
    for term in candidates:
        term = str(term or "").strip()
        if (
            len(term) < 2
            or term in _KNOWLEDGE_GENERIC_TERMS
            or term == character
            or term in character_names
        ):
            continue
        if term not in out:
            out.append(term)
    return out


def _knowledge_regression_window(sentences, index, terms):
    sentence = sentences[index]
    if any(term in sentence for term in terms):
        return sentence
    if index <= 0 or not _KNOWLEDGE_ANAPHORA_RE.search(sentence):
        return ""
    for previous_index in range(index - 1, max(-1, index - 3), -1):
        previous = sentences[previous_index]
        if any(term in previous for term in terms):
            return "".join(sentences[previous_index:index + 1])
    return ""


def _chinese_number(text):
    digits = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    units = {"十": 10, "百": 100, "千": 1000, "万": 10000}
    total = section = number = 0
    for char in str(text or ""):
        if char in digits:
            number = digits[char]
        elif char in units:
            unit = units[char]
            if unit == 10000:
                section = (section + number) * unit
                total += section
                section = number = 0
            else:
                if number == 0:
                    number = 1
                section += number * unit
                number = 0
        else:
            return None
    return total + section + number


def _numbers(text):
    out = []
    for match in _ARABIC_NUMBER_RE.finditer(str(text or "")):
        value = float(match.group(1))
        out.append((int(value) if value.is_integer() else value, match.group(0)))
    for match in _CHINESE_NUMBER_RE.finditer(str(text or "")):
        value = _chinese_number(match.group(0))
        if value is not None:
            out.append((value, match.group(0)))
    return out


def _expected_number(value):
    text = str(value or "").strip()
    try:
        number = float(text)
        return int(number) if number.is_integer() else number
    except ValueError:
        return _chinese_number(text)


def _number_spans(text):
    rows = []
    for regex in (_ARABIC_NUMBER_RE, _CHINESE_NUMBER_RE):
        for match in regex.finditer(str(text or "")):
            label = match.group(0)
            value = _expected_number(match.group(1) if regex is _ARABIC_NUMBER_RE else label)
            if value is not None:
                rows.append((match.start(), match.end(), value, label))
    return sorted(rows, key=lambda row: (row[0], row[1]))


def _numeric_assertion_values(sentence, fact):
    """Return values explicitly assigned to this fact, not nearby budgets/comparisons."""
    sentence = str(sentence or "")
    subject = str(fact.get("subject") or "")
    terms = _row_terms(fact, ("subject", "aliases"))[:10]
    identity_terms = [term for term in terms if term not in _NUMERIC_GENERIC_TERMS]
    if not identity_terms:
        return []
    unit = str(fact.get("unit") or "")
    values = []
    for start, end, value, _label in _number_spans(sentence):
        if unit and not sentence[end:end + len(unit) + 2].lstrip().startswith(unit):
            continue
        for term in identity_terms:
            for term_match in re.finditer(re.escape(term), sentence):
                if term_match.end() <= start and start - term_match.end() <= 32:
                    bridge = sentence[term_match.end():start]
                    if any(word in bridge and word not in subject for word in _NUMERIC_ROLE_WORDS):
                        continue
                    compact = re.sub(r"[\s的：:，,（）()]", "", bridge)
                    if _NUMERIC_NEGATION_OR_COMPARISON_RE.search(compact):
                        continue
                    if re.search(r"[，,；;]", bridge) and not _NUMERIC_CHANGE_RE.search(bridge):
                        continue
                    markers = list(_NUMERIC_ASSERTION_RE.finditer(compact))
                    explicit = bool(markers) and not _NUMERIC_NEGATION_OR_COMPARISON_RE.search(
                        compact[markers[-1].end():]
                    )
                    if explicit or compact in {"", "是", "为", "共", "计", "共计"}:
                        values.append(value)
                        break
                elif end <= term_match.start() and term_match.start() - end <= 16:
                    bridge = sentence[end:term_match.start()]
                    compact = re.sub(r"[\s的：:，,（）()]", "", bridge)
                    if not _NUMERIC_NEGATION_OR_COMPARISON_RE.search(compact):
                        if not compact or re.fullmatch(r"(?:是|为|作为|即)?", compact):
                            values.append(value)
                            break
            if value in values:
                break
    return list(dict.fromkeys(values))


def _sentences(text):
    return [match.group(0).strip() for match in _SENTENCE_RE.finditer(str(text or "")) if match.group(0).strip()]


def _location_matches(location, text):
    location = re.sub(r"[\s（）()【】\[\]，,。·\-]+", "", str(location or ""))
    text = str(text or "")
    if len(location) >= 2 and location in text:
        return True
    place_words = (
        "客厅", "卧室", "书房", "教室", "办公室", "宿舍", "楼道", "走廊",
        "操场", "食堂", "医院", "车站", "公园", "仓库", "院子", "家",
    )
    for place in place_words:
        if place not in location or place not in text:
            continue
        owner = location.split(place, 1)[0]
        if len(owner) >= 2 and owner in text:
            return True
    return False


def _scene_replay_findings(text, scenes):
    findings = []
    opening = str(text or "")[:5000]
    for scene in reversed(list(scenes or [])):
        if scene.get("closed") is not True:
            continue
        location = str(scene.get("location") or "").strip()
        if len(location) < 2 or not _location_matches(location, opening):
            continue
        beat_terms = _row_terms(scene, ("beats", "entry_trigger"))
        matched = None
        # All signals must coexist in one bounded passage.  A chapter-wide scan
        # used to assemble a fake replay from unrelated scenes and paragraphs.
        for start in range(0, max(1, len(opening)), 350):
            passage = opening[start:start + 1400]
            if not _location_matches(location, passage):
                continue
            characters = [x for x in scene.get("characters", []) if x and x in passage]
            props = [x for x in scene.get("props", []) if len(x) >= 2 and x in passage]
            beats = [x for x in beat_terms if x in passage]
            if len(characters) >= 2 and len(props) >= 2 and len(beats) >= 2:
                matched = (characters, props, beats)
                break
        if matched:
            characters, props, beats = matched
            findings.append({
                "code": "RECENT_SCENE_TEMPLATE_REPLAY",
                "severity": "REVIEW",
                "message": f"近期已关闭场景被低变化重开：第{scene.get('chapter_no')}章 {location}",
                "evidence": "；".join([location, "人物=" + "、".join(characters), "道具=" + "、".join(props), "节拍=" + "、".join(beats[:4])])[:500],
            })
            break
    return findings


def _thin_evidence_findings(text):
    """Catch a hard conclusion following an explicitly one-sample premise."""
    text = str(text or "")
    for sample in _EXPLICIT_THIN_SAMPLE_RE.finditer(text):
        sample_context = text[max(0, sample.start() - 120):sample.end() + 180]
        if not _EVIDENCE_CONTEXT_RE.search(sample_context):
            continue
        # Keep the premise and conclusion close.  A chapter-wide scan used to
        # join one small observation to an unrelated conclusion thousands of
        # characters later.
        tail = text[sample.start():sample.start() + 1000]
        for clause_match in _CLAUSE_RE.finditer(tail):
            clause = clause_match.group(0)
            conclusion = _HARD_CONCLUSION_TOKEN_RE.search(clause)
            if not conclusion or not _EVIDENCE_CONCLUSION_LINK_RE.search(clause):
                continue
            if _NEGATED_OR_UNCERTAIN_CONCLUSION_RE.search(clause):
                continue
            token = conclusion.group(0)
            if token in {"确认", "确定", "证明", "排除", "必然", "不存在", "毫无关系"}:
                if not _EVIDENCE_CLAIM_TARGET_RE.search(clause):
                    continue
            if token == "因果" and not re.search(
                r"(?:确认|确定|证明|建立|存在|构成)[^，,；;。！？!?]{0,12}因果|"
                r"因果(?:关系)?(?:成立|明确|已确认|得到确认)",
                clause,
            ):
                continue
            if token == "可重复" and not re.search(
                r"(?:确认|确定|说明|表明|已经|属于|是|具有|可以|能够)"
                r"[^，,；;。！？!?]{0,16}可重复|"
                r"可重复(?:性|现象)(?:已经|已)?(?:成立|明确|确认|得到确认)",
                clause,
            ):
                continue
            if token == "让" and not re.search(
                r"(?:这|它|该(?:现象|结果|数据|记录|样本|变化))"
                r"[^，,；;。！？!?]{0,12}让",
                clause,
            ):
                continue
            window = tail[:clause_match.end()]
            return [{
                "code": "THIN_EVIDENCE_OVERCLAIM",
                "severity": "REVIEW",
                "message": "单次或首条观测被直接升级为排除、确认或因果结论",
                "evidence": _clean_text(window, 500),
            }]
    return []


def _item_holder_findings(text, ledger, plan=""):
    """Catch an explicit instruction that assigns an item to the wrong person."""
    findings = []
    for match in _ITEM_RETURN_RE.finditer(str(text or "")):
        recipient = match.group("recipient")
        # A pronoun in the prose cannot be resolved from author-side Plan text.
        nearby = str(text or "")[max(0, match.start() - 650):match.end()]
        for item in ledger.get("item_states", []):
            holder = str(item.get("holder") or "").strip()
            if not holder or holder in recipient or recipient in holder:
                continue
            terms = _row_terms(item, ("name", "aliases"))
            if not any(term in nearby for term in terms[:12]):
                continue
            findings.append({
                "code": "CANON_ITEM_HOLDER_CONFLICT",
                "severity": "REVIEW",
                "message": f"物品当前由{holder}持有，却让{recipient}‘把东西留着’",
                "evidence": _clean_text(
                    f"Canon物品={item.get('name') or item.get('item_id')}；当前持有人={holder}；正文={match.group(0)}",
                    500,
                ),
            })
            return findings
    return findings


def _location_jump_findings(text):
    """Detect a transit-to-home action jump with no arrival transition."""
    text = str(text or "")
    transit = _TRANSIT_RE.search(text[:1800])
    if not transit:
        return []
    home_action = _HOME_ACTION_RE.search(text, transit.end())
    if not home_action or home_action.start() - transit.end() > 1200:
        return []
    bridge = text[transit.end():home_action.start()]
    if _ARRIVAL_RE.search(bridge) or _SCENE_SWITCH_RE.search(bridge):
        return []
    return [{
        "code": "UNMARKED_LOCATION_JUMP",
        "severity": "REVIEW",
        "message": "人物从交通工具场景直接执行家中物件动作，缺少下车或到家的转场",
        "evidence": _clean_text(text[max(0, transit.start() - 60):home_action.end() + 60], 500),
    }]


def deterministic_canon_findings(current_text, ledger, plan=""):
    """Return only evidence-backed cross-chapter findings.

    Nuanced ownership and decision changes remain visible to the semantic Plan
    gate.  The local checks below intentionally cover facts that can be proven
    directly from a structured row plus a bounded current sentence.
    """
    text = str(current_text or "")
    ledger = ledger if isinstance(ledger, dict) else empty_ledger()
    findings = []

    def add(code, severity, message, evidence):
        findings.append({
            "code": code,
            "severity": severity,
            "message": message,
            "evidence": _clean_text(evidence, 500),
        })

    sentences = _sentences(text)
    for fact in ledger.get("numeric_facts", []):
        expected = _expected_number(fact.get("value"))
        if expected is None:
            continue
        for sentence in sentences:
            asserted_values = _numeric_assertion_values(sentence, fact)
            if any(value != expected for value in asserted_values):
                add(
                    "CANON_NUMERIC_CONFLICT", "MINOR",
                    f"结构化Canon数字冲突：{fact.get('subject')}应为{fact.get('value')}{fact.get('unit') or ''}",
                    sentence,
                )
                break

    knowledge_states = ledger.get("knowledge_states", [])
    knowledge_characters = {
        str(row.get("character") or "").strip()
        for row in knowledge_states
        if str(row.get("character") or "").strip()
    }
    for state in knowledge_states:
        if state.get("knows") is not True:
            continue
        terms = _knowledge_topic_terms(state, knowledge_characters)
        if not terms:
            continue
        character = str(state.get("character") or "").strip()
        for index, sentence in enumerate(sentences):
            if not _KNOWLEDGE_REGRESSION_RE.search(sentence):
                continue
            window = _knowledge_regression_window(sentences, index, terms[:12])
            if not window:
                continue
            if character and character not in window:
                continue
            add(
                "CANON_KNOWLEDGE_REGRESSION", "REVIEW",
                f"人物知识无依据回退：{state.get('character')}此前已知道{state.get('fact')}",
                window,
            )
            break

    findings.extend(_thin_evidence_findings(text))
    findings.extend(_item_holder_findings(text, ledger, plan=plan))
    findings.extend(_location_jump_findings(text))
    findings.extend(_scene_replay_findings(text, ledger.get("recent_scenes", [])))
    return findings
