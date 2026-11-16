"""插件注册中心 + 3 个内置通用适配器。

补同类采集脚本「加渠道要改 4 处」的短板：按配置里的 adapter 名动态加载，
无硬编码 import、无字典白名单。加渠道 90% 情况只改 channels.yaml 一处。
适配器契约见 references/channel-adapters.md。
"""
from __future__ import annotations

import csv
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

from ur_core import (EVIDENCE_FACT, STATUS_OK, STATUS_SKIPPED, Record,
                     now_iso, sha256_text)
from ur_fetch import fetch_json, four_step_probe

BUILTIN_NAMES = ("public_json_api", "public_html", "manual_import")
HARD_DISABLED_MODES = ("disabled",)


def _get_path(data: Any, path: str, default: Any = None) -> Any:
    """支持 'author.playtime_at_review' 这类点路径字段映射。"""
    cur = data
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list) and part.isdigit():
            idx = int(part)
            cur = cur[idx] if idx < len(cur) else None
        else:
            return default
        if cur is None:
            return default
    return cur


class Adapter:
    """5 方法契约。新增适配器继承本类即可，不用改 ur_collect / ur_core / ur_report。"""
    name = "base"

    def probe(self, cfg: dict, ctx: dict | None = None) -> dict:
        raise NotImplementedError

    def fetch_metrics(self, cfg: dict, day: str, ctx: dict | None = None) -> list[Record]:
        return []

    def fetch_reviews(self, cfg: dict, cursor: str | None,
                      ctx: dict | None = None) -> tuple[list[Record], str | None]:
        return ([], None)

    def normalize(self, raw: dict, cfg: dict, ctx: dict | None = None) -> Record:
        raise NotImplementedError

    def compliance(self, cfg: dict) -> dict:
        """默认最严：未声明 compliance 即禁用。"""
        c = cfg.get("compliance") or {}
        if not c:
            return {"allowed": False, "reason": "未声明 compliance，默认最严即禁用"}
        if cfg.get("mode") in HARD_DISABLED_MODES:
            return {"allowed": False, "reason": f"mode={cfg.get('mode')} 硬禁用"}
        if c.get("require_human_confirm") and not (cfg.get("params") or {}).get("filled_by"):
            return {"allowed": False, "reason": "需人工确认但 params.filled_by 未填（自有账号取数必须带填写人）"}
        return {"allowed": True, "reason": c.get("tos_note", ""),
                "compliance_tag": c.get("compliance_tag", "public_anonymous"),
                "require_human_confirm": bool(c.get("require_human_confirm"))}


class PublicJsonApiAdapter(Adapter):
    """官方公开 JSON 接口 / 公开 RSS：只读 GET + 游标分页 + 字段映射。"""
    name = "public_json_api"

    def probe(self, cfg: dict, ctx: dict | None = None) -> dict:
        ctx = ctx or {}
        return four_step_probe(cfg.get("entry", ""), offline=bool(ctx.get("offline")),
                               rate_qps=float(ctx.get("rate_qps", 0.3)))

    def normalize(self, raw: dict, cfg: dict, ctx: dict | None = None) -> Record:
        ctx = ctx or {}
        rv = cfg.get("reviews") or {}
        rid = _get_path(raw, rv.get("id_field", "id"), "")
        text = _get_path(raw, rv.get("text_field", "text"), "")
        rating = _get_path(raw, rv.get("rating_field", "rating"))
        if isinstance(rating, bool):
            rating = 5 if rating else 1
        return Record(
            record_id=f"{cfg['key']}:{rid}", record_type="review",
            game_key=ctx.get("game_key", ""), channel_key=cfg["key"],
            channel_name=cfg.get("name", cfg["key"]),
            day=ctx.get("day", now_iso()[:10]), captured_at=now_iso(),
            source=f"{cfg.get('name')} 公开接口", source_url=cfg.get("entry", ""),
            status=STATUS_OK, precision=0,
            raw=json.dumps(raw, ensure_ascii=False)[:4000],
            ingest_batch=ctx.get("ingest_batch", ""),
            compliance_tag=(cfg.get("compliance") or {}).get("compliance_tag", "public_anonymous"),
            review_id=str(rid), review_text=str(text),
            rating=rating, rating_max=rv.get("rating_max", 5),
            review_time=str(_get_path(raw, rv.get("time_field", "time"), "") or ""),
            playtime_hours=_get_path(raw, rv.get("playtime_field", ""), None),
            votes_up=int(_get_path(raw, rv.get("votes_field", ""), 0) or 0),
            is_early_access=bool(_get_path(raw, rv.get("early_access_field", ""), False)),
            evidence_kind=EVIDENCE_FACT,
        )

    def fetch_reviews(self, cfg: dict, cursor: str | None,
                      ctx: dict | None = None) -> tuple[list[Record], str | None]:
        ctx = ctx or {}
        p = cfg.get("params") or {}
        params: dict[str, Any] = {k: v for k, v in p.items()
                                  if not str(k).endswith("_param") and not str(v).startswith("<")}
        page_key = p.get("page_size_param", "limit")
        cursor_key = p.get("cursor_param", "cursor")
        params[page_key] = min(int(ctx.get("page_size", 100)), 100)
        if cursor:
            params[cursor_key] = cursor
        result = fetch_json(
            cfg.get("entry", ""), params, offline=bool(ctx.get("offline")),
            rate_qps=float(ctx.get("rate_qps", 0.3)),
            retry_on=tuple(ctx.get("retry_on", (429, 500, 502, 503, 504))),
            snapshot_root=ctx.get("snapshot_root"), channel_key=cfg["key"],
            day=ctx.get("day", now_iso()[:10]),
            redact_pii=bool(ctx.get("redact_pii", True)))
        ctx["last_fetch"] = result
        if not result["ok"] or result["empty"]:
            return ([], None)
        payload = result["payload"]
        items = None
        for key in ("reviews", "results", "entry", "items", "data", "feed"):
            if isinstance(payload, dict) and key in payload:
                items = payload[key]
                if isinstance(items, dict) and "entry" in items:
                    items = items["entry"]
                break
        if items is None and isinstance(payload, list):
            items = payload
        if not isinstance(items, list):
            items = []
        records = [self.normalize(x, cfg, ctx) for x in items if isinstance(x, dict)]
        next_cursor = None
        if isinstance(payload, dict) and payload.get("cursor") and len(items) >= params[page_key]:
            next_cursor = str(payload["cursor"])
        return (records, next_cursor)


class PublicHtmlAdapter(PublicJsonApiAdapter):
    """公开网页：先走四步法探测，再取公开接口；任何「没数」结论必须说明做到第几步。"""
    name = "public_html"

    def probe(self, cfg: dict, ctx: dict | None = None) -> dict:
        ctx = ctx or {}
        return four_step_probe(cfg.get("entry", ""), offline=bool(ctx.get("offline")),
                               rate_qps=float(ctx.get("rate_qps", 0.3)))


class ManualImportAdapter(Adapter):
    """人工投放：只读 data/inbox/ 下公开匿名导出的 jsonl / csv，带填写人。"""
    name = "manual_import"

    def probe(self, cfg: dict, ctx: dict | None = None) -> dict:
        folder = Path((ctx or {}).get("workspace", ".")) / cfg.get("entry", "")
        files = sorted([p.name for p in folder.glob("*.jsonl")] + [p.name for p in folder.glob("*.csv")]) if folder.exists() else []
        return {"step_reached": 4 if files else 1,
                "found": bool(files), "files": files,
                "conclusion": (f"人工投放目录发现 {len(files)} 个文件" if files
                               else "投放目录为空：这是正常缺数，不是采集失败；请投公开匿名导出文件")}

    def fetch_reviews(self, cfg: dict, cursor: str | None,
                      ctx: dict | None = None) -> tuple[list[Record], str | None]:
        ctx = ctx or {}
        folder = Path(ctx.get("workspace", ".")) / cfg.get("entry", "")
        if not folder.exists():
            return ([], None)
        out: list[Record] = []
        for path in sorted(folder.glob("*.jsonl")):
            with path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            out.append(self.normalize(json.loads(line), cfg, ctx))
                        except json.JSONDecodeError:
                            continue
        for path in sorted(folder.glob("*.csv")):
            with path.open("r", encoding="utf-8-sig", newline="") as f:
                for row in csv.DictReader(f):
                    out.append(self.normalize(row, cfg, ctx))
        return (out, None)

    def fetch_metrics(self, cfg: dict, day: str, ctx: dict | None = None) -> list[Record]:
        ctx = ctx or {}
        folder = Path(ctx.get("workspace", ".")) / cfg.get("entry", "")
        if not folder.exists():
            return []
        out: list[Record] = []
        for path in sorted(folder.glob("*.csv")):
            with path.open("r", encoding="utf-8-sig", newline="") as f:
                for row in csv.DictReader(f):
                    if not row.get("metric_key"):
                        continue
                    out.append(self._metric_record(row, cfg, ctx))
        return out

    def _metric_record(self, row: dict, cfg: dict, ctx: dict) -> Record:
        filled_by = (cfg.get("params") or {}).get("filled_by", "")
        return Record(
            record_id=f"metric:{cfg['key']}:{row.get('day','')}:{row.get('metric_key','')}:{row.get('metric_kind','outer')}",
            record_type="metric", game_key=ctx.get("game_key", ""),
            channel_key=cfg["key"], channel_name=cfg.get("name", cfg["key"]),
            day=row.get("day", ""), captured_at=row.get("captured_at") or now_iso(),
            source=f"人工投放 {row.get('source', '')}",
            status=row.get("status", STATUS_OK), precision=float(row.get("precision") or 1),
            raw=str(row.get("raw", "")), warn=str(row.get("warn", "")),
            error=str(row.get("error", "")),
            note=f"填写人：{filled_by}" if filled_by else "人工投放",
            ingest_batch=ctx.get("ingest_batch", ""), compliance_tag="manual_confirmed",
            metric_key=row.get("metric_key", ""), metric_value=row.get("metric_value"),
            metric_unit=row.get("metric_unit", ""), metric_kind=row.get("metric_kind", "outer"),
        )

    def normalize(self, raw: dict, cfg: dict, ctx: dict | None = None) -> Record:
        ctx = ctx or {}
        filled_by = (cfg.get("params") or {}).get("filled_by", "")
        rv = cfg.get("reviews") or {}
        rid = raw.get("review_id") or _get_path(raw, rv.get("id_field", "id"), "")
        text = raw.get("review_text") or _get_path(raw, rv.get("text_field", "text"), "")
        return Record(
            record_id=f"{cfg['key']}:{rid}", record_type="review",
            game_key=ctx.get("game_key", ""), channel_key=cfg["key"],
            channel_name=cfg.get("name", cfg["key"]),
            day=str(raw.get("day") or ctx.get("day") or now_iso()[:10]),
            captured_at=str(raw.get("captured_at") or now_iso()),
            source=f"人工投放（{cfg.get('name')}）", source_url="",
            status=raw.get("status", STATUS_OK), precision=0,
            raw=str(raw.get("raw") or text), note=f"填写人：{filled_by}" if filled_by else "人工投放",
            ingest_batch=ctx.get("ingest_batch", ""), compliance_tag="manual_confirmed",
            review_id=str(rid), review_text=str(text),
            rating=raw.get("rating"), rating_max=raw.get("rating_max", 5),
            review_time=str(raw.get("review_time", "")),
            app_version=str(raw.get("app_version", "")),
            playtime_hours=raw.get("playtime_hours"),
            lang=str(raw.get("lang", "")), country=str(raw.get("country", "")),
            votes_up=int(raw.get("votes_up") or 0),
            developer_replied=bool(raw.get("developer_replied", False)),
            is_early_access=bool(raw.get("is_early_access", False)),
            evidence_kind=EVIDENCE_FACT,
        )


_BUILTIN_CLASSES = {
    "public_json_api": PublicJsonApiAdapter,
    "public_html": PublicHtmlAdapter,
    "manual_import": ManualImportAdapter,
}


def load_adapter(name: str, base_dir: str | Path | None = None) -> Adapter:
    """按配置名加载：先内置，再 <scripts>/adapters/<name>.py 动态加载。"""
    if name in _BUILTIN_CLASSES:
        return _BUILTIN_CLASSES[name]()
    base = Path(base_dir) if base_dir else Path(__file__).parent
    path = base / "adapters" / f"{name}.py"
    if not path.exists():
        raise KeyError(f"找不到适配器 {name!r}：内置仅 {BUILTIN_NAMES}，自定义请放 {path}")
    spec = importlib.util.spec_from_file_location(f"ur_adapter_{name}", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    for obj in vars(module).values():
        if isinstance(obj, type) and issubclass(obj, Adapter) and obj is not Adapter:
            return obj()
    raise KeyError(f"{path} 里没有 Adapter 子类")
