"""只读请求层（零业务耦合，标准库实现，离线零依赖）。

沿用只读采集六规矩：真实浏览器头、随机抖动、仅对 5xx/429 指数退避、
连续失败熔断、同日快照幂等、异常不入账。
本 skill 额外三条：仅 GET/HEAD、禁 cookie/authorization、检测到登录跳转即停。
--offline 下任何联网调用直接抛 FetchGuardError（供 ur_verify 第 6 条判定）。
"""
from __future__ import annotations

import json
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from ur_core import now_iso, redact, save_snapshot, sha256_text

ALLOWED_METHODS = {"GET", "HEAD"}
FORBIDDEN_HEADERS = {"cookie", "authorization", "proxy-authorization", "x-api-key", "x-auth-token"}
DEFAULT_RETRY = (429, 500, 502, 503, 504)
LOGIN_MARKERS = ("/login", "/signin", "/account/login", "/passport", "sso.")
UA = "channel-user-research/1.0 (read-only public-data adapter)"

FOUR_STEPS = [
    "第1步 页面渲染文字",
    "第2步 内嵌数据块（__INITIAL_STATE__ / __NEXT_DATA__ / __NUXT__ / application/json）",
    "第3步 页面 JS 引用的公开接口",
    "第4步 以上都没有 → 才判死",
]


class FetchGuardError(RuntimeError):
    """踩红线：立即停止整轮，不重试、不降级。"""


class CircuitOpen(RuntimeError):
    """连续失败熔断。"""


def assert_readonly(method: str = "GET", headers: dict | None = None,
                    url: str = "", *, offline: bool = False) -> None:
    """所有联网请求的唯一入口守卫。"""
    if offline:
        raise FetchGuardError("--offline 下禁止任何联网请求（ur_fetch 被调用即视为违规）")
    m = (method or "GET").upper()
    if m not in ALLOWED_METHODS:
        raise FetchGuardError(
            f"只允许 GET/HEAD，收到 {m}；采集全程只读，绝不触发改变对方系统状态的写操作")
    for k in (headers or {}):
        if str(k).lower() in FORBIDDEN_HEADERS:
            raise FetchGuardError(f"禁止携带 {k} 头：不复用登录态、不登录他人账号")
    u = urllib.parse.urlparse(url)
    if u.scheme not in ("http", "https"):
        raise FetchGuardError(f"只允许 http/https，收到 {u.scheme!r}")
    for marker in LOGIN_MARKERS:
        if marker in u.path.lower():
            raise FetchGuardError(f"检测到登录页跳转 {url!r}，立即停止：不登录他人账号")


def _throttle(rate_qps: float, jitter: bool = True) -> None:
    if rate_qps <= 0:
        return
    delay = 1.0 / rate_qps
    if jitter:
        delay *= random.uniform(0.8, 1.3)
    time.sleep(delay)


def fetch_json(url: str, params: dict | None = None, *, offline: bool = False,
               headers: dict | None = None, timeout: int = 15,
               rate_qps: float = 0.3, retry_on: tuple = DEFAULT_RETRY,
               max_attempts: int = 3, snapshot_root: str | Path | None = None,
               channel_key: str = "", day: str = "", redact_pii: bool = True,
               circuit_state: dict | None = None) -> dict:
    """只读 GET JSON。返回统一结果字典，异常不入账。

    结果键：ok / status / payload / empty / four_step / snapshot_path / error / attempts
    """
    circuit_state = circuit_state if circuit_state is not None else {}
    full_url = url
    if params:
        full_url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    try:
        assert_readonly("GET", headers, full_url, offline=offline)
    except FetchGuardError as exc:
        return {"ok": False, "status": 0, "payload": None, "empty": True,
                "four_step": "未执行（红线预检拒绝）", "snapshot_path": "",
                "error": str(exc), "attempts": 0}

    if circuit_state.get("open"):
        return {"ok": False, "status": 0, "payload": None, "empty": True,
                "four_step": "未执行（熔断中）", "snapshot_path": "",
                "error": "连续失败熔断中，本轮不再请求", "attempts": 0}

    req_headers = {"User-Agent": UA, "Accept": "application/json, text/plain, */*"}
    for k, v in (headers or {}).items():
        req_headers[k] = v

    last_error = ""
    for attempt in range(1, max_attempts + 1):
        _throttle(rate_qps)
        try:
            req = urllib.request.Request(full_url, headers=req_headers, method="GET")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                final_url = resp.geturl()
                if any(m in urllib.parse.urlparse(final_url).path.lower() for m in LOGIN_MARKERS):
                    raise FetchGuardError(f"请求被重定向到登录页 {final_url!r}，立即停止")
                body = resp.read().decode("utf-8", errors="replace")
                status = resp.status
            circuit_state["consecutive_failures"] = 0
            try:
                payload = json.loads(body)
            except json.JSONDecodeError:
                payload = {"_raw_text": body[:20000]}
            empty = _is_empty_payload(payload)
            snap = ""
            if snapshot_root:
                snap = save_snapshot(
                    snapshot_root, channel_key or "unknown", day or now_iso()[:10],
                    {"request_url": redact(full_url, redact_pii), "method": "GET",
                     "http_status": status, "captured_at": now_iso(),
                     "payload": payload}, redact_pii=redact_pii)
            return {"ok": True, "status": status, "payload": payload, "empty": empty,
                    "four_step": FOUR_STEPS[2], "snapshot_path": snap,
                    "error": "", "attempts": attempt}
        except urllib.error.HTTPError as exc:
            last_error = f"HTTP {exc.code}"
            if exc.code not in retry_on:
                return _fail(last_error, attempt, circuit_state)
            if attempt < max_attempts:
                time.sleep(min(2 ** attempt, 8) + random.random())
                continue
            circuit_state["consecutive_failures"] = circuit_state.get("consecutive_failures", 0) + 1
            if circuit_state["consecutive_failures"] >= 3:
                circuit_state["open"] = True
            return _fail(last_error, attempt, circuit_state)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = f"网络异常 {type(exc).__name__}: {exc}"
            if attempt < max_attempts:
                time.sleep(min(2 ** attempt, 8) + random.random())
                continue
            circuit_state["consecutive_failures"] = circuit_state.get("consecutive_failures", 0) + 1
            if circuit_state["consecutive_failures"] >= 3:
                circuit_state["open"] = True
            return _fail(last_error, attempt, circuit_state)
        except FetchGuardError as exc:
            return {"ok": False, "status": 0, "payload": None, "empty": True,
                    "four_step": "未执行（登录跳转即停）", "snapshot_path": "",
                    "error": str(exc), "attempts": attempt}
    return _fail(last_error or "未知失败", max_attempts, circuit_state)


def _fail(error: str, attempts: int, circuit_state: dict) -> dict:
    return {"ok": False, "status": 0, "payload": None, "empty": True,
            "four_step": "未取到（异常不入账）", "snapshot_path": "",
            "error": error, "attempts": attempts}


def _is_empty_payload(payload: Any) -> bool:
    """空结果告警：HTTP 200 但内容为空 ≠ 成功采到 0 条。"""
    if payload is None:
        return True
    if isinstance(payload, dict):
        for key in ("results", "reviews", "feed", "data", "entry", "items"):
            if key in payload:
                v = payload[key]
                if isinstance(v, dict) and "entry" in v:
                    v = v["entry"]
                return not bool(v)
        return not bool(payload)
    if isinstance(payload, list):
        return len(payload) == 0
    return False


def four_step_probe(url: str, *, offline: bool = False, rate_qps: float = 0.3,
                    timeout: int = 15) -> dict:
    """网页四步法探测：任何「没数」结论必须说明做到第几步。"""
    try:
        assert_readonly("GET", None, url, offline=offline)
    except FetchGuardError as exc:
        return {"step_reached": 0, "steps": FOUR_STEPS, "found": False,
                "note": str(exc), "conclusion": "未执行（红线预检拒绝）"}
    result = fetch_json(url, offline=offline, rate_qps=rate_qps, timeout=timeout)
    if not result["ok"]:
        return {"step_reached": 1, "steps": FOUR_STEPS, "found": False,
                "note": result["error"],
                "conclusion": f"只做到{FOUR_STEPS[0]}，不能判『网页没数』（扫不到≠没有）"}
    payload = result["payload"]
    text = json.dumps(payload, ensure_ascii=False) if not isinstance(payload, str) else payload
    step = 1
    found = False
    if any(marker in text for marker in ("__INITIAL_STATE__", "__NEXT_DATA__", "__NUXT__", "application/json")):
        step, found = 2, True
    if any(marker in text for marker in ("api/", "/ajax", "getJSON", "fetch(")):
        step, found = 3, True
    return {
        "step_reached": step, "steps": FOUR_STEPS, "found": found,
        "note": result["four_step"],
        "conclusion": (f"做到 {FOUR_STEPS[step - 1]}，发现数据源线索" if found
                       else f"做到 {FOUR_STEPS[step - 1]}；仍不能轻率判死，先确认扫描本身完整"),
    }
