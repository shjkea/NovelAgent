import json
import time
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

from secret_store import load_secret

CST = timezone(timedelta(hours=8))
VOLCENGINE_AGENT_PLAN_BASE = "https://ark.cn-beijing.volces.com/api/plan/v3"


def _deepseek_source(cfg: dict) -> str:
    source = str((cfg or {}).get("source", "official") or "official").strip().lower()
    return source if source in {"official", "volcengine_agent_plan"} else "official"


def _clean_api_slot(value) -> str:
    slot = str(value or "1").strip()
    return slot if slot in {"1", "2", "3", "4", "5"} else "1"


def _slot_key_store(node: dict, source: str, slot: str) -> tuple[str, str]:
    node = dict(node or {})
    slot = _clean_api_slot(slot)
    slots = dict(node.get("api_slots", {}) or {})
    item = dict(slots.get(slot, {}) or {})
    if source == "volcengine_agent_plan":
        legacy = "runtime/volcengine_agent_plan_key.dpapi"
        default_store = legacy if slot == "1" else f"runtime/volcengine_agent_plan_key_{slot}.dpapi"
    else:
        legacy = "runtime/deepseek_api_key.dpapi"
        default_store = legacy if slot == "1" else f"runtime/deepseek_api_key_{slot}.dpapi"
    store = str(item.get("api_key_store", default_store) or default_store)
    label = str(item.get("label", f"API {slot}") or f"API {slot}")
    return store, label


def _deepseek_endpoint_cfg(cfg: dict) -> dict:
    cfg = dict(cfg or {})
    source = _deepseek_source(cfg)
    if source == "volcengine_agent_plan":
        node = dict(cfg.get("volcengine_agent_plan", {}) or {})
        slot = _clean_api_slot(node.get("active_slot", "1"))
        store, account_label = _slot_key_store(node, source, slot)
        return {
            "source": source,
            "label": "火山方舟 Agent Plan",
            "base_url": str(node.get("base_url", VOLCENGINE_AGENT_PLAN_BASE) or VOLCENGINE_AGENT_PLAN_BASE).rstrip("/"),
            "api_key_store": store,
            "account_slot": slot,
            "account_label": account_label,
        }
    node = cfg
    slot = _clean_api_slot(node.get("active_slot", "1"))
    store, account_label = _slot_key_store(node, "official", slot)
    return {
        "source": "official",
        "label": "DeepSeek 官方",
        "base_url": str(node.get("base_url", "https://api.deepseek.com") or "https://api.deepseek.com").rstrip("/"),
        "api_key_store": store,
        "account_slot": slot,
        "account_label": account_label,
    }


def _normalize_usage_cache_fields(usage: dict) -> dict:
    """Normalize cache/reasoning fields across DeepSeek/OpenAI-compatible responses."""
    u = dict(usage or {})
    prompt = int(u.get("prompt_tokens", 0) or 0)
    details = u.get("prompt_tokens_details") or {}
    hit = u.get("prompt_cache_hit_tokens")
    if hit is None:
        hit = details.get("cached_tokens", 0)
    hit = max(0, min(prompt, int(hit or 0)))
    miss = u.get("prompt_cache_miss_tokens")
    if miss is None:
        miss = max(0, prompt - hit)
    miss = max(0, int(miss or 0))
    u["prompt_cache_hit_tokens"] = hit
    u["prompt_cache_miss_tokens"] = miss
    return u


def calculate_volcengine_afp(model: str, usage: dict):
    """Estimate Agent Plan AFP using the published per-request input-length bands.

    Cache hits do not reduce AFP here because the published Agent Plan table applies
    its coefficient to prompt tokens; no separate AFP cache-discount coefficient is
    documented. Reasoning tokens are already included in completion_tokens.
    """
    if not usage:
        return 0.0
    usage = _normalize_usage_cache_fields(usage)
    prompt = int(usage.get("prompt_tokens", 0) or 0)
    output = int(usage.get("completion_tokens", 0) or 0)
    is_pro = "pro" in (model or "").lower()

    # Published Agent Plan bands: <=32K, (32K,128K], >128K.
    if is_pro:
        if prompt <= 32768:
            in_coeff = 5.5 * 0.67
        elif prompt <= 131072:
            in_coeff = 5.5
        else:
            in_coeff = 5.5 * 2.0
        out_coeff = 5.5
    else:
        if prompt <= 32768:
            in_coeff = 0.5 * 0.67
        elif prompt <= 131072:
            in_coeff = 0.5
        else:
            in_coeff = 0.5 * 2.0
        out_coeff = 0.5
    return round((prompt * in_coeff + output * out_coeff) / 10000.0, 6)


class ProviderError(RuntimeError):
    pass


class ProviderRefusalError(ProviderError):
    def __init__(self, message, content="", usage=None, request_id="", finish_reason=""):
        super().__init__(message)
        self.content = content or ""
        self.usage = dict(usage or {})
        self.request_id = request_id or ""
        self.finish_reason = finish_reason or ""


class ProviderLengthError(ProviderError):
    """Provider stopped because max_tokens was reached.

    Keep the partial content so caller-specific recovery code can persist it for
    diagnostics instead of losing the exact runaway output.
    """
    def __init__(self, message, content="", usage=None, request_id=""):
        super().__init__(message)
        self.content = content or ""
        self.usage = dict(usage or {})
        self.request_id = request_id or ""



class ProviderCancelledError(ProviderError):
    pass


class ProviderEmptyContentError(ProviderRefusalError):
    pass


def _looks_like_refusal(text: str) -> bool:
    t = (text or "").strip()
    if not t or len(t) > 800:
        return False
    low = t.lower()
    needles = [
        "i'm sorry, but i can't", "i cannot assist", "i can’t assist",
        "i'm unable to", "sorry, that's beyond", "content policy",
        "抱歉，我不能", "抱歉，我无法", "我不能帮助", "我无法协助",
        "无法按照你的要求", "不能继续生成"
    ]
    return any(x in low for x in needles)




def deepseek_price_status(when=None) -> dict:
    """Return current DeepSeek peak/off-peak period and published rates.

    Times are evaluated in Beijing/Singapore time (UTC+8). Peak windows are
    09:00-12:00 and 14:00-18:00 local, matching 01:00-04:00 and 06:00-10:00 UTC.
    CNY rates are approximate display/estimation rates used by NovelAgent.
    """
    when = when or datetime.now(CST)
    if when.tzinfo is None:
        when = when.replace(tzinfo=CST)
    else:
        when = when.astimezone(CST)
    hm = when.hour * 60 + when.minute
    peak = (9 * 60 <= hm < 12 * 60) or (14 * 60 <= hm < 18 * 60)
    boundaries = [(9,0),(12,0),(14,0),(18,0)]
    now_minutes = hm + when.second / 60.0
    next_h = next_m = None
    add_day = False
    for h,m in boundaries:
        if h*60+m > now_minutes:
            next_h,next_m = h,m
            break
    if next_h is None:
        next_h,next_m = 9,0
        add_day = True
    next_switch = when.replace(hour=next_h, minute=next_m, second=0, microsecond=0)
    if add_day:
        next_switch += timedelta(days=1)
    rates = {
        "flash": {
            "cache_hit": 0.10 if peak else 0.05,
            "cache_miss": 3.0 if peak else 1.5,
            "output": 9.0 if peak else 4.5,
        },
        "pro": {
            "cache_hit": 0.30 if peak else 0.15,
            "cache_miss": 9.0 if peak else 4.5,
            "output": 27.0 if peak else 13.5,
        },
    }
    return {
        "peak": peak,
        "period": "peak" if peak else "off_peak",
        "label": "峰时" if peak else "谷时",
        "multiplier_vs_off_peak": 2.0 if peak else 1.0,
        "local_time": when.isoformat(timespec="seconds"),
        "next_switch_at": next_switch.isoformat(timespec="seconds"),
        "seconds_to_switch": max(0, int((next_switch-when).total_seconds())),
        "peak_windows_local": ["09:00-12:00", "14:00-18:00"],
        "rates_cny_per_million": rates,
    }


def calculate_deepseek_cost(model: str, usage: dict, when=None) -> float:
    """Estimate CNY cost from DeepSeek's published API prices.

    Before 2026-08-17 00:00 Beijing time: legacy price table.
    From that point onward: peak/off-peak price table.
    reasoning tokens are already part of completion_tokens and are not double-counted.
    """
    if not usage:
        return 0.0
    usage = _normalize_usage_cache_fields(usage)
    when = when or datetime.now(CST)
    if when.tzinfo is None:
        when = when.replace(tzinfo=CST)
    else:
        when = when.astimezone(CST)

    hit = int(usage.get("prompt_cache_hit_tokens", 0) or 0)
    miss = usage.get("prompt_cache_miss_tokens")
    prompt = int(usage.get("prompt_tokens", 0) or 0)
    if miss is None:
        miss = max(0, prompt - hit)
    miss = int(miss or 0)
    output = int(usage.get("completion_tokens", 0) or 0)

    model = (model or "").lower()
    is_pro = "pro" in model
    effective = datetime(2026, 8, 17, 0, 0, tzinfo=CST)

    if when < effective:
        if is_pro:
            rates = (0.025, 3.0, 6.0)
        else:
            rates = (0.02, 1.0, 2.0)
    else:
        status = deepseek_price_status(when)
        rr = status["rates_cny_per_million"]["pro" if is_pro else "flash"]
        rates = (rr["cache_hit"], rr["cache_miss"], rr["output"])

    hit_rate, miss_rate, out_rate = rates
    return round((hit * hit_rate + miss * miss_rate + output * out_rate) / 1_000_000.0, 8)


class DeepSeekClient:
    def __init__(self, root: Path, cfg_loader, on_metrics=None, on_chunk=None,
                 stop_event=None, logger=None):
        self.root = Path(root)
        self.cfg_loader = cfg_loader
        self.on_metrics = on_metrics
        self.on_chunk = on_chunk
        self.stop_event = stop_event
        self.logger = logger or (lambda x: None)
        import threading
        self._active_lock = threading.RLock()
        self._active_response = None

    def _diag_log(self, message: str):
        """Persist DeepSeek failures without storing prompt/body content."""
        try:
            log_dir = self.root / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")
            with (log_dir / "deepseek_errors.log").open("a", encoding="utf-8") as f:
                f.write(f"[{stamp}] {message}\n")
        except Exception:
            pass

    def _cfg(self):
        return self.cfg_loader().get("deepseek", {})

    def source(self):
        return _deepseek_source(self._cfg())

    def endpoint(self):
        return _deepseek_endpoint_cfg(self._cfg())

    def _key(self):
        ep = self.endpoint()
        store = self.root / ep["api_key_store"]
        try:
            return load_secret(store)
        except Exception:
            return ""

    def configured(self):
        return bool(self._key())

    def _cancelled(self):
        return bool(self.stop_event is not None and self.stop_event.is_set())

    def _set_active_response(self, response):
        with self._active_lock:
            self._active_response = response

    def _clear_active_response(self, response=None):
        with self._active_lock:
            if response is None or self._active_response is response:
                self._active_response = None

    def cancel_current(self):
        """Best-effort cancellation of an active DeepSeek streaming response."""
        with self._active_lock:
            r = self._active_response
        if r is None:
            return False
        try:
            r.close()
            return True
        except Exception:
            return False

    def health(self):
        key = self._key()
        ep = self.endpoint()
        if not key:
            return False, f"{ep['label']} API Key 未配置"
        # Agent Plan's OpenAI-compatible gateway does not need a billable chat
        # request on every UI status poll. Explicit /api/deepseek/test does that.
        if ep["source"] == "volcengine_agent_plan":
            return True, f"火山 Agent Plan · {ep.get('account_label', 'API 1')} Key 已配置"
        try:
            r = requests.get(
                ep["base_url"] + "/models",
                headers={"Authorization": f"Bearer {key}"},
                timeout=12,
            )
            if r.status_code == 200:
                return True, "DeepSeek 官方 API connected"
            return False, f"HTTP {r.status_code}: {r.text[:200]}"
        except Exception as e:
            return False, str(e)

    def test_connection(self):
        ep = self.endpoint()
        if ep["source"] != "volcengine_agent_plan":
            return self.health()
        key = self._key()
        if not key:
            return False, f"火山 Agent Plan · {ep.get('account_label', 'API 1')} API Key 未配置"
        payload = {
            "model": "deepseek-v4-flash",
            "messages": [{"role": "user", "content": "回复 OK"}],
            "max_tokens": 2,
            "stream": False,
            "thinking": {"type": "disabled"},
            "temperature": 0.0,
        }
        try:
            r = requests.post(
                ep["base_url"] + "/chat/completions",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json=payload, timeout=(12, 30),
            )
            if r.status_code == 200:
                return True, f"火山 Agent Plan · {ep.get('account_label', 'API 1')} connected"
            return False, f"HTTP {r.status_code}: {r.text[:300]}"
        except Exception as e:
            return False, str(e)

    def _emit_metrics(self, usage, request_id, model, thinking, label, started, request_started_at):
        usage = _normalize_usage_cache_fields(usage)
        reasoning_tokens = int(
            ((usage.get("completion_tokens_details") or {}).get("reasoning_tokens", 0)) or 0
        )
        source = self.source()
        cost_cny = calculate_deepseek_cost(model, usage, when=request_started_at) if source == "official" else 0.0
        afp = calculate_volcengine_afp(model, usage) if source == "volcengine_agent_plan" else None
        usage.update({
            "_request_id": request_id,
            "_provider": "deepseek",
            "_api_source": source,
            "_api_account": self.endpoint().get("account_slot", "1"),
            "_api_account_label": self.endpoint().get("account_label", "API 1"),
            "_model": model,
            "_thinking": bool(thinking),
            "_reasoning_tokens": reasoning_tokens,
            "_estimated_cost_cny": cost_cny,
            "_estimated_afp": afp,
            "_elapsed_seconds": round(time.perf_counter() - started, 4),
            "_request_started_at": request_started_at.isoformat(timespec="seconds"),
        })
        if self.on_metrics:
            self.on_metrics({}, usage, label)
        return usage, reasoning_tokens

    def _chat_once(self, system, user, temperature=0.7, max_tokens=4000,
                   stream=False, label="", emit_text=False, model="deepseek-v4-flash",
                   thinking=False, response_format=None, reasoning_effort="high",
                   retry_attempts_override=None):
        key = self._key()
        ep = self.endpoint()
        if not key:
            raise ProviderError(f"{ep['label']} API Key 未配置")
        if self._cancelled():
            raise ProviderCancelledError("DeepSeek 调用已取消")

        cfg = self._cfg()
        base = ep["base_url"]
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": int(max_tokens),
            "stream": bool(stream),
            "thinking": {"type": "enabled" if thinking else "disabled"},
        }
        if thinking:
            payload["reasoning_effort"] = reasoning_effort or "high"
        else:
            payload["temperature"] = float(temperature)
        if response_format:
            payload["response_format"] = response_format
        if stream:
            payload["stream_options"] = {"include_usage": True}

        headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        }
        attempts = max(1, int(
            cfg.get("retry_attempts", 4)
            if retry_attempts_override is None else retry_attempts_override
        ))
        # Audit calls are identifiable by their stable label.  Applying the cap
        # here keeps agent_core compatible with already-created router objects
        # whose chat() signature predates the per-call override parameter.
        if retry_attempts_override is None and str(label or "").startswith("audit_"):
            attempts = min(attempts, 2)
        connect_timeout = max(1.0, float(cfg.get("connect_timeout_seconds", 15)))
        read_timeout = max(5.0, float(cfg.get("read_timeout_seconds", 90)))
        hard_timeout = max(0.0, float(cfg.get("max_request_seconds", 900)))
        request_id = str(uuid.uuid4())
        request_started_at = datetime.now(CST)
        started = time.perf_counter()
        last = None

        for attempt in range(attempts):
            if self._cancelled():
                raise ProviderCancelledError("DeepSeek 调用已取消")
            r = None
            stream_had_data = False
            try:
                r = requests.post(
                    base + "/chat/completions", headers=headers, json=payload,
                    stream=stream, timeout=(connect_timeout, read_timeout),
                )
                self._set_active_response(r)

                if r.status_code in (429, 500, 502, 503, 504):
                    last = ProviderError(f"DeepSeek HTTP {r.status_code}: {r.text[:300]}")
                    r.close(); self._clear_active_response(r)
                    if attempt + 1 < attempts:
                        self.logger(
                            f"DeepSeek {label or 'request'} HTTP {r.status_code}；"
                            f"重试 {attempt + 2}/{attempts}。"
                        )
                        time.sleep(min(1.0 + attempt * 1.5, 8.0))
                        continue
                    raise last
                r.raise_for_status()

                usage = {}
                finish_reason = None
                reasoning_chars = 0

                if not stream:
                    obj = r.json()
                    choice = (obj.get("choices") or [{}])[0]
                    finish_reason = choice.get("finish_reason")
                    msg = choice.get("message") or {}
                    text = msg.get("content") or ""
                    reasoning_content = msg.get("reasoning_content") or ""
                    reasoning_chars = len(reasoning_content)
                    usage = obj.get("usage") or {}
                else:
                    out = []
                    for raw in r.iter_lines(decode_unicode=False):
                        if self._cancelled():
                            raise ProviderCancelledError("DeepSeek 调用已取消")
                        if hard_timeout and time.perf_counter() - started > hard_timeout:
                            raise ProviderError(
                                f"DeepSeek 单次调用超过 {hard_timeout:.0f}s 硬上限"
                            )
                        if not raw:
                            continue
                        stream_had_data = True
                        line = raw.decode("utf-8", errors="replace")
                        if not line.startswith("data: "):
                            continue
                        data = line[6:]
                        if data == "[DONE]":
                            break
                        try:
                            obj = json.loads(data)
                        except Exception:
                            continue
                        if obj.get("usage"):
                            usage = obj["usage"] or usage
                        choices = obj.get("choices") or []
                        if not choices:
                            continue
                        choice = choices[0]
                        if choice.get("finish_reason"):
                            finish_reason = choice.get("finish_reason")
                        delta = choice.get("delta") or {}

                        reasoning_piece = delta.get("reasoning_content")
                        if reasoning_piece:
                            reasoning_chars += len(reasoning_piece)
                            if self.on_chunk:
                                self.on_chunk(
                                    text=reasoning_piece, label=label,
                                    elapsed=time.perf_counter() - started,
                                    emit_text=False, kind="reasoning",
                                )

                        text_piece = delta.get("content")
                        if text_piece:
                            out.append(text_piece)
                            if self.on_chunk:
                                self.on_chunk(
                                    text=text_piece, label=label,
                                    elapsed=time.perf_counter() - started,
                                    emit_text=emit_text, kind="content",
                                )
                    text = "".join(out)

                usage, reasoning_tokens = self._emit_metrics(
                    usage, request_id, model, thinking, label, started, request_started_at
                )
                prompt_tokens = int(usage.get("prompt_tokens", 0) or 0)
                completion_tokens = int(usage.get("completion_tokens", 0) or 0)
                cache_hit_tokens = int(usage.get("prompt_cache_hit_tokens", 0) or 0)
                cache_miss_tokens = int(
                    usage.get("prompt_cache_miss_tokens",
                              max(0, prompt_tokens - cache_hit_tokens)) or 0
                )
                diag = (
                    f"request_id={request_id} source={ep['source']} label={label or '-'} model={model} "
                    f"thinking={bool(thinking)} max_tokens={int(max_tokens)} "
                    f"finish_reason={finish_reason or 'unknown'} "
                    f"prompt_tokens={prompt_tokens} completion_tokens={completion_tokens} "
                    f"reasoning_tokens={reasoning_tokens} reasoning_chars={reasoning_chars} "
                    f"content_chars={len(text or '')} cache_hit_tokens={cache_hit_tokens} "
                    f"cache_miss_tokens={cache_miss_tokens}"
                )

                if finish_reason == "content_filter":
                    self._diag_log("CONTENT_FILTER " + diag)
                    raise ProviderRefusalError(
                        "模型服务内容过滤；" + diag,
                        content=text, usage=usage, request_id=request_id,
                        finish_reason="content_filter",
                    )
                if finish_reason == "insufficient_system_resource":
                    self._diag_log("SYSTEM_RESOURCE " + diag)
                    raise ProviderError("模型服务系统资源不足；" + diag)
                if finish_reason == "length":
                    self._diag_log("MAX_TOKENS " + diag)
                    raise ProviderLengthError(
                        f"模型服务输出达到 max_tokens={int(max_tokens)} 上限，最终正文可能尚未生成完成；" + diag,
                        content=text, usage=usage, request_id=request_id,
                    )
                if not (text or "").strip():
                    self._diag_log("EMPTY_CONTENT " + diag)
                    raise ProviderEmptyContentError("模型服务返回空正文；" + diag)
                if _looks_like_refusal(text):
                    self._diag_log("REFUSAL " + diag)
                    raise ProviderRefusalError("模型服务返回明显拒答；" + diag)
                return text

            except ProviderCancelledError:
                raise
            except ProviderEmptyContentError:
                raise
            except ProviderRefusalError:
                raise
            except requests.RequestException as e:
                last = e
                if stream_had_data:
                    self._diag_log(
                        f"STREAM_ERROR request_id={request_id} label={label or '-'} "
                        f"model={model} error={type(e).__name__}: {e}"
                    )
                    raise ProviderError(f"模型服务流式读取中断: {e}") from e
                if attempt + 1 >= attempts:
                    raise ProviderError(f"模型服务请求失败: {e}") from e
                self.logger(
                    f"DeepSeek {label or 'request'} 请求中断：{type(e).__name__}；"
                    f"重试 {attempt + 2}/{attempts}。"
                )
                time.sleep(min(1.0 + attempt * 1.5, 8.0))
            finally:
                if r is not None:
                    try:
                        r.close()
                    except Exception:
                        pass
                    self._clear_active_response(r)

        raise last or ProviderError("DeepSeek 请求失败")

    def chat(self, system, user, temperature=0.7, max_tokens=4000,
             stream=False, label="", emit_text=False, model="deepseek-v4-flash",
             thinking=False, response_format=None, reasoning_effort="high",
             retry_attempts_override=None):
        cfg = self._cfg()
        empty_retries = max(0, int(cfg.get("empty_content_retries", 1)))
        last_empty = None
        for empty_attempt in range(empty_retries + 1):
            retry_user = user
            if empty_attempt:
                retry_user = user + (
                    "\n\n【强制输出要求】上一轮只返回了思考过程而没有最终 content。"
                    "本轮完成思考后必须输出最终答案；不得以空 content 结束。"
                )
                self.logger(
                    f"DeepSeek {label or 'request'} 上一轮最终 content 为空，"
                    f"同模型重试 {empty_attempt}/{empty_retries}。"
                )
            try:
                return self._chat_once(
                    system, retry_user, temperature, max_tokens,
                    stream=stream, label=label, emit_text=emit_text, model=model,
                    thinking=thinking, response_format=response_format,
                    reasoning_effort=reasoning_effort,
                    retry_attempts_override=retry_attempts_override,
                )
            except ProviderEmptyContentError as e:
                last_empty = e
                if empty_attempt >= empty_retries:
                    raise ProviderRefusalError(str(e)) from e
        raise last_empty or ProviderRefusalError("DeepSeek 返回空正文")


class GrokClient:
    """Small OpenAI-compatible Grok client dedicated to non-Canon DLC."""
    def __init__(self, root: Path, cfg_loader, on_metrics=None, on_chunk=None,
                 stop_event=None, logger=None):
        self.root = Path(root)
        self.cfg_loader = cfg_loader
        self.on_metrics = on_metrics
        self.on_chunk = on_chunk
        self.stop_event = stop_event
        self.logger = logger or (lambda x: None)
        import threading
        self._active_lock = threading.RLock()
        self._active_response = None

    def _cfg(self):
        return dict(self.cfg_loader().get("grok", {}) or {})

    def _key(self):
        store = self.root / str(self._cfg().get("api_key_store", "runtime/grok_api_key.dpapi"))
        try:
            return load_secret(store)
        except Exception:
            return ""

    def configured(self):
        return bool(self._key())

    def _cancelled(self):
        return bool(self.stop_event is not None and self.stop_event.is_set())

    def cancel_current(self):
        with self._active_lock:
            response = self._active_response
        if response is None:
            return False
        try:
            response.close()
            return True
        except Exception:
            return False

    def health(self):
        if not self.configured():
            return False, "Grok API Key 未配置"
        return True, "Grok API Key 已配置"

    def test_connection(self):
        cfg = self._cfg()
        key = self._key()
        if not key:
            return False, "Grok API Key 未配置"
        try:
            response = requests.get(
                str(cfg.get("base_url", "https://api.x.ai/v1")).rstrip("/") + "/models",
                headers={"Authorization": f"Bearer {key}"}, timeout=(12, 30),
            )
            if response.status_code == 200:
                label = str(cfg.get("api_label", "Grok API") or "Grok API")
                return True, f"{label} connected"
            return False, f"HTTP {response.status_code}: {response.text[:300]}"
        except Exception as error:
            return False, str(error)

    def chat(self, system, user, temperature=0.4, max_tokens=3200,
             stream=False, label="", emit_text=False, model="grok-4.6",
             response_format=None, reasoning_effort="low"):
        cfg = self._cfg()
        key = self._key()
        if not key:
            raise ProviderError("Grok API Key 未配置")
        base = str(cfg.get("base_url", "https://api.x.ai/v1")).rstrip("/")
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": float(temperature),
            "max_tokens": int(max_tokens),
            "stream": bool(stream),
        }
        effort = str(reasoning_effort or "").strip().lower()
        if effort and effort != "none" and bool(cfg.get("send_reasoning_effort", True)):
            payload["reasoning_effort"] = effort
        if response_format and bool(cfg.get("send_response_format", True)):
            payload["response_format"] = response_format
        if stream and bool(cfg.get("send_stream_options", True)):
            payload["stream_options"] = {"include_usage": True}
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        attempts = max(1, int(cfg.get("retry_attempts", 3)))
        connect_timeout = max(1.0, float(cfg.get("connect_timeout_seconds", 15)))
        read_timeout = max(10.0, float(cfg.get("read_timeout_seconds", 300)))
        hard_timeout = max(0.0, float(cfg.get("max_request_seconds", 900)))
        request_id = str(uuid.uuid4())
        started = time.perf_counter()
        request_started_at = datetime.now(CST)
        last = None
        for attempt in range(attempts):
            if self._cancelled():
                raise ProviderCancelledError("Grok 调用已取消")
            response = None
            stream_had_data = False
            try:
                response = requests.post(
                    base + "/chat/completions", headers=headers, json=payload,
                    stream=stream, timeout=(connect_timeout, read_timeout),
                )
                with self._active_lock:
                    self._active_response = response
                if response.status_code in (429, 500, 502, 503, 504):
                    last = ProviderError(f"Grok HTTP {response.status_code}: {response.text[:500]}")
                    if attempt + 1 < attempts:
                        time.sleep(min(1.0 + attempt * 1.5, 8.0))
                        continue
                    raise last
                if (
                    response.status_code in (400, 422)
                    and bool(cfg.get("compatibility_fallback", True))
                    and attempt + 1 < attempts
                    and any(key in payload for key in (
                        "reasoning_effort", "response_format", "stream_options", "temperature"
                    ))
                ):
                    removed = [
                        key for key in ("reasoning_effort", "response_format", "stream_options", "temperature")
                        if payload.pop(key, None) is not None
                    ]
                    self.logger(
                        "第三方 Grok 端点拒绝可选参数；已移除 "
                        + "、".join(removed)
                        + " 并自动重试。"
                    )
                    time.sleep(0.2)
                    continue
                if response.status_code >= 400:
                    raise ProviderError(f"Grok HTTP {response.status_code}: {response.text[:1000]}")
                usage = {}
                finish_reason = None
                if not stream:
                    obj = response.json()
                    choice = (obj.get("choices") or [{}])[0]
                    finish_reason = choice.get("finish_reason")
                    text = str((choice.get("message") or {}).get("content") or "")
                    usage = obj.get("usage") or {}
                else:
                    pieces = []
                    for raw in response.iter_lines(decode_unicode=False):
                        if self._cancelled():
                            raise ProviderCancelledError("Grok 调用已取消")
                        if hard_timeout and time.perf_counter() - started > hard_timeout:
                            raise ProviderError(f"Grok 单次调用超过 {hard_timeout:.0f}s 硬上限")
                        if not raw:
                            continue
                        stream_had_data = True
                        line = raw.decode("utf-8", errors="replace")
                        if not line.startswith("data: "):
                            continue
                        data = line[6:]
                        if data == "[DONE]":
                            break
                        try:
                            obj = json.loads(data)
                        except Exception:
                            continue
                        if obj.get("usage"):
                            usage = obj.get("usage") or usage
                        choices = obj.get("choices") or []
                        if not choices:
                            continue
                        choice = choices[0]
                        finish_reason = choice.get("finish_reason") or finish_reason
                        piece = str((choice.get("delta") or {}).get("content") or "")
                        if piece:
                            pieces.append(piece)
                            if self.on_chunk:
                                self.on_chunk(text=piece, label=label,
                                              elapsed=time.perf_counter() - started,
                                              emit_text=emit_text, kind="content")
                    text = "".join(pieces)
                usage = _normalize_usage_cache_fields(usage)
                api_source = str(cfg.get("api_source", "grok_compatible") or "grok_compatible")
                api_label = str(cfg.get("api_label", "Grok API") or "Grok API")
                usage.update({
                    "_request_id": request_id, "_provider": "grok", "_api_source": api_source,
                    "_api_account": "", "_api_account_label": api_label,
                    "_model": model, "_thinking": effort not in {"", "none"},
                    "_reasoning_tokens": int(((usage.get("completion_tokens_details") or {}).get("reasoning_tokens", 0)) or 0),
                    "_estimated_cost_cny": 0.0,
                    "_estimated_afp": None, "_elapsed_seconds": round(time.perf_counter() - started, 4),
                    "_request_started_at": request_started_at.isoformat(timespec="seconds"),
                })
                if self.on_metrics:
                    self.on_metrics({}, usage, label)
                if finish_reason == "length":
                    raise ProviderLengthError("Grok 输出达到 max_tokens 上限", text, usage, request_id)
                if not text.strip():
                    raise ProviderEmptyContentError("Grok 返回空正文")
                if _looks_like_refusal(text):
                    raise ProviderRefusalError("Grok 返回明显拒答")
                return text
            except (ProviderCancelledError, ProviderRefusalError, ProviderLengthError, ProviderEmptyContentError):
                raise
            except requests.RequestException as error:
                last = error
                if stream_had_data or attempt + 1 >= attempts:
                    raise ProviderError(f"Grok 请求失败: {error}") from error
                time.sleep(min(1.0 + attempt * 1.5, 8.0))
            finally:
                if response is not None:
                    try:
                        response.close()
                    except Exception:
                        pass
                with self._active_lock:
                    if self._active_response is response:
                        self._active_response = None
        raise last or ProviderError("Grok 请求失败")


class LLMRouter:
    STAGES = ("plan", "draft", "review", "deep_review", "revision", "summary", "memory")

    def __init__(self, root, cfg_loader, on_metrics=None, on_chunk=None, logger=None, stop_event=None):
        self.root = Path(root)
        self.cfg_loader = cfg_loader
        self.on_metrics = on_metrics
        self.on_chunk = on_chunk
        self.logger = logger or (lambda x: None)
        self.stop_event = stop_event
        self.deepseek = DeepSeekClient(
            self.root, cfg_loader, on_metrics, on_chunk,
            stop_event=stop_event, logger=self.logger,
        )
        self.grok = GrokClient(
            self.root, cfg_loader, on_metrics, on_chunk,
            stop_event=stop_event, logger=self.logger,
        )
        self.reload()

    def reload(self):
        # The 8080 text model has been retired. API clients read current config
        # lazily, so no local client needs to be created or reloaded here.
        return None

    def cancel_current(self):
        return bool(self.deepseek.cancel_current() or self.grok.cancel_current())

    def health(self):
        return {
            "deepseek": dict(zip(("ok", "detail"), self.deepseek.health())),
            "grok": dict(zip(("ok", "detail"), self.grok.health())),
        }

    def _preset_stage(self, stage: str):
        cfg = self.cfg_loader()
        routing = cfg.get("routing", {})
        mode = routing.get("mode", "recommended")
        custom = routing.get("stages", {})
        defaults = routing.get("presets", {})
        if mode == "custom":
            return dict(custom.get(stage, {}))
        preset = defaults.get(mode) or defaults.get("recommended") or {}
        return dict(preset.get(stage, custom.get(stage, {})))

    def resolve(self, stage: str, routing_context="", provider_override=None,
                model_override=None, thinking_override=None, reasoning_effort_override=None):
        cfg = self.cfg_loader()
        routing = cfg.get("routing", {})
        mode = routing.get("mode", "recommended")
        spec = self._preset_stage(stage)

        if provider_override and provider_override != "auto":
            spec["provider"] = provider_override
        if model_override:
            spec["model"] = model_override
        if thinking_override is not None:
            spec["thinking"] = bool(thinking_override)
        if reasoning_effort_override is not None:
            effort = str(reasoning_effort_override).lower()
            spec["reasoning_effort"] = effort if provider_override == "grok" else ("high" if effort == "medium" else effort)

        decision = None
        provider = spec.get("provider", "deepseek")
        if provider == "local":
            raise ProviderError("本地正文模型已移除；请将该阶段改为 DeepSeek，DLC 扩写使用 Grok。")
        if provider == "deepseek" and not spec.get("model"):
            spec["model"] = "deepseek-v4-flash"
        if provider == "grok" and not spec.get("model"):
            spec["model"] = self.cfg_loader().get("grok", {}).get("model", "grok-4.6")
        spec["provider"] = provider
        spec["auto_nsfw_decision"] = decision
        return spec

    def _writing_guardrail(self, stage: str, spec: dict):
        cfg = self.cfg_loader().get("writing_guardrails", {})
        if not bool(cfg.get("enabled", True)) or not bool(cfg.get("provider_specific", True)):
            return ""
        if stage not in {"draft", "revision"}:
            return ""
        if spec.get("provider") == "deepseek":
            return """

【DeepSeek正文附加约束】
严格按照当前章节硬任务卡和章节计划推进。允许补充自然的生活细节、环境和人物互动，但不得创建会改变后续剧情的新主线、新秘密、大型伏笔，也不得提前消费未来章节内容。最近正文中的未解悬念若不属于本章任务，只保持未解决，不主动升级。
"""
        return ""

    def chat(self, stage, system, user, temperature=0.7, max_tokens=4000,
             stream=False, label="", emit_text=False, routing_context="",
             provider_override=None, model_override=None, thinking_override=None,
             response_format=None, reasoning_effort_override=None, allow_local_fallback=True,
             deepseek_retry_attempts_override=None):
        cfg = self.cfg_loader()
        spec = self.resolve(
            stage, routing_context=routing_context,
            provider_override=provider_override,
            model_override=model_override,
            thinking_override=thinking_override,
            reasoning_effort_override=reasoning_effort_override,
        )
        provider = spec.get("provider")
        base_system = system or ""
        guardrail = self._writing_guardrail(stage, spec)
        system = base_system + guardrail if guardrail else base_system
        source_suffix = ""
        if provider == "deepseek":
            ep = self.deepseek.endpoint()
            source_suffix = f"；API={ep['label']}"
        self.logger(
            f"路由 {stage}: {provider}/{spec.get('model')}" + source_suffix
        )

        if provider == "grok":
            text = self.grok.chat(
                system, user, temperature, int(max_tokens), stream=stream,
                label=label, emit_text=emit_text,
                model=spec.get("model", "grok-4.6"),
                response_format=response_format,
                reasoning_effort=spec.get("reasoning_effort", "low"),
            )
            return text, spec

        try:
            effective_max_tokens = int(max_tokens)
            if bool(spec.get("thinking", False)):
                min_thinking = int(
                    cfg.get("deepseek", {}).get("min_thinking_max_tokens", 12000)
                )
                if effective_max_tokens < min_thinking:
                    self.logger(
                        f"DeepSeek {stage} Thinking 输出预算："
                        f"{effective_max_tokens} → {min_thinking} max_tokens"
                    )
                    effective_max_tokens = min_thinking

            reasoning_effort = spec.get("reasoning_effort", "high")
            if stage == "plan" and bool(spec.get("thinking", False)) and reasoning_effort_override is None:
                # Plan should be deliberate but not spend most of its budget on hidden reasoning.
                reasoning_effort = str(
                    cfg.get("deepseek", {}).get("plan_reasoning_effort", "low")
                ) or "low"

            try:
                text = self.deepseek.chat(
                    system, user, temperature, effective_max_tokens,
                    stream=stream, label=label, emit_text=emit_text,
                    model=spec.get("model", "deepseek-v4-flash"),
                    thinking=bool(spec.get("thinking", False)),
                    response_format=response_format,
                    reasoning_effort=reasoning_effort,
                    retry_attempts_override=deepseek_retry_attempts_override,
                )
            except ProviderLengthError as length_error:
                length_retries = max(0, int(
                    cfg.get("deepseek", {}).get("plan_length_retry_attempts", 1)
                )) if stage == "plan" else 0
                if length_retries <= 0:
                    raise
                retry_max = max(
                    effective_max_tokens + 1000,
                    int(cfg.get("deepseek", {}).get("plan_length_retry_max_tokens", 16000)),
                )
                self.logger(
                    f"DeepSeek {stage} 达到 max_tokens={effective_max_tokens}；"
                    f"保持同模型并以 {retry_max} max_tokens 重试 1 次（reasoning_effort={reasoning_effort}）。"
                )
                retry_user = user + (
                    "\n\n【规划重试要求】上一轮因输出长度上限未完成。"
                    "请减少不必要的内部推理，优先尽快给出完整、简洁、可执行的章节规划；"
                    "必须完整结束最终答案，不要在列表或 JSON 中途截断。"
                )
                text = self.deepseek.chat(
                    system, retry_user, temperature, retry_max,
                    stream=stream, label=label, emit_text=emit_text,
                    model=spec.get("model", "deepseek-v4-flash"),
                    thinking=bool(spec.get("thinking", False)),
                    response_format=response_format,
                    reasoning_effort=reasoning_effort,
                    retry_attempts_override=deepseek_retry_attempts_override,
                )
            return text, spec
        except ProviderCancelledError:
            raise
        except Exception:
            # No 8080 fallback remains.  Surface the API failure so the current
            # stage stops without silently switching models.
            raise
