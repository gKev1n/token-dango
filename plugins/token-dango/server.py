#!/usr/bin/env python3
"""Token 团子：给 Codex 会话面板提供用量快照的 MCP stdio 服务。

只读 ~/.codex/state_5.sqlite、~/.codex/config.toml 与会话 rollout JSONL；
不读 auth.json，不写 Codex 文件，不联网。仅依赖 Python 标准库（macOS 自带的 /usr/bin/python3 即可运行）。
"""
import json
import os
import re
import sqlite3
import sys
import time
from contextlib import closing
from pathlib import Path

UI_URI = "ui://token-dango/panel"
HERE = Path(__file__).resolve().parent
THREAD_KEYS = ("threadId", "thread_id", "openai/threadId", "openai/thread_id", "codexThreadId", "codex_thread_id")
QUOTA_SCAN_THREADS = 6   # 额度是账号级的：在最近活跃的几条会话里找最新一条
KEEP_TURNS = 4           # 只为最近几轮保留工具时间与花费明细
MIN_TPS_TOKENS = 200     # 本轮输出少于这个数时不算速度
# 这些条目执行期间模型在等结果，不算生成时间
TOOL_ITEMS = ("McpToolCall", "CommandExecution", "FileChange", "ImageView", "Extension", "SubAgentActivity")
# 只有含这些字样的行才值得 json 解析，大会话首次全量扫描时能省掉大部分开销
MARKERS = (b'"token_count"', b'"token_usage_record"', b'"task_started"', b'"task_complete"', b'"item_completed"')


def codex_home():
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


# ---------- 计价 ----------

def empty_bucket():
    return {"uncached": 0, "cached": 0, "write": 0, "output": 0, "requests": 0}


def add_usage(buckets, usage, threshold):
    """按单次请求的输入大小分到短 / 长上下文两档，累加各类 token。"""
    inp = usage.get("input_tokens") or 0
    cached = usage.get("cached_input_tokens") or 0
    write = usage.get("cache_write_input_tokens") or 0
    b = buckets.setdefault("long" if inp > threshold else "short", empty_bucket())
    b["uncached"] += max(0, inp - cached - write)
    b["cached"] += cached
    b["write"] += write
    b["output"] += usage.get("output_tokens") or 0  # 推理 token 已包含在输出里
    b["requests"] += 1


def load_prices():
    try:
        return json.loads((HERE / "prices.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def service_tier():
    """读 config.toml 顶层的 service_tier；priority 即 API 的 Fast 档。"""
    try:
        text = (codex_home() / "config.toml").read_text(encoding="utf-8")
    except OSError:
        return "standard"
    top = text.split("\n[", 1)[0]
    m = re.search(r'^\s*service_tier\s*=\s*"([^"]+)"', top, re.M)
    return "fast" if m and m.group(1) in ("priority", "fast") else "standard"


def price_buckets(buckets, model, tier, prices):
    table = (prices.get("models") or {}).get(model or "")
    if not table or not buckets:
        return None
    rates = table.get(tier) or table.get("standard") or {}
    parts = {"input": 0.0, "cached": 0.0, "output": 0.0, "write": 0.0}
    for ctx, b in buckets.items():
        r = rates.get(ctx) or rates.get("short")
        if not r:
            return None
        parts["input"] += b["uncached"] * r[0] / 1e6
        parts["cached"] += b["cached"] * r[1] / 1e6
        parts["output"] += b["output"] * r[2] / 1e6
        parts["write"] += b["write"] * r[3] / 1e6
    return {"usd": round(sum(parts.values()), 4), "parts": {k: round(v, 4) for k, v in parts.items()}}


# ---------- 时间窗 ----------

def busy_ms(intervals, lo, hi):
    """[lo, hi] 内被工具占用的总毫秒数，重叠部分只算一次。"""
    spans = sorted((max(a, lo), min(b, hi)) for a, b in intervals if b > lo and a < hi)
    total, cur_a, cur_b = 0, None, None
    for a, b in spans:
        if cur_b is None or a > cur_b:
            if cur_b is not None:
                total += cur_b - cur_a
            cur_a, cur_b = a, b
        else:
            cur_b = max(cur_b, b)
    if cur_b is not None:
        total += cur_b - cur_a
    return total


# ---------- rollout 增量解析 ----------

class Rollout:
    """记住读到哪里，之后只解析新增的行。文件被截断或替换时从头再来。"""

    def __init__(self, path, threshold=272000):
        self.path = Path(path)
        self.threshold = threshold
        self.reset()

    def reset(self):
        self.offset = 0
        self.inode = None
        self.partial = b""
        self.token_count = None      # (timestamp, payload)
        self.quota = None            # (timestamp, rate_limits)
        self.session = {}            # 整个会话按请求分档的 token 累计
        self.has_records = False
        self.turn_usage = {}         # turn_id -> turn_token_usage
        self.turn_buckets = {}       # turn_id -> 本轮按请求分档的累计
        self.turn_tools = {}         # turn_id -> [(start_ms, end_ms)]
        self.started = None          # 最近一次 task_started payload
        self.completed = None        # 最近一次 task_complete payload

    def refresh(self):
        st = self.path.stat()
        if self.inode != st.st_ino or st.st_size < self.offset:
            self.reset()
            self.inode = st.st_ino
        if st.st_size == self.offset:
            return self
        with self.path.open("rb") as f:
            f.seek(self.offset)
            chunk = f.read(st.st_size - self.offset)
        self.offset = st.st_size
        lines = (self.partial + chunk).split(b"\n")
        self.partial = lines.pop()  # 最后一段可能只写了一半，留到下次
        for raw in lines:
            if any(m in raw for m in MARKERS):
                self._feed(raw)
        self._prune()
        return self

    def _feed(self, raw):
        try:
            ev = json.loads(raw)
        except ValueError:
            return
        kind = ev.get("type")
        payload = ev.get("payload") or {}
        ts = ev.get("timestamp")
        if kind == "event_msg":
            ptype = payload.get("type")
            if ptype == "token_count":
                if payload.get("info"):
                    self.token_count = (ts, payload)
                if payload.get("rate_limits"):
                    self.quota = (ts, payload["rate_limits"])
            elif ptype == "task_started":
                self.started = payload
            elif ptype == "task_complete":
                self.completed = payload
            elif ptype == "item_completed":
                item = payload.get("item") or {}
                a, b = payload.get("started_at_ms"), payload.get("completed_at_ms")
                if item.get("type") in TOOL_ITEMS and a and b and b >= a:
                    self.turn_tools.setdefault(payload.get("turn_id"), []).append((a, b))
        elif kind == "token_usage_record":
            self.has_records = True
            usage = payload.get("usage") or {}
            add_usage(self.session, usage, self.threshold)
            tid = payload.get("turn_id")
            if tid:
                add_usage(self.turn_buckets.setdefault(tid, {}), usage, self.threshold)
                if payload.get("turn_token_usage"):
                    self.turn_usage[tid] = payload["turn_token_usage"]

    def _prune(self):
        keep = {p.get("turn_id") for p in (self.started, self.completed) if p}
        for d in (self.turn_usage, self.turn_buckets, self.turn_tools):
            if len(d) > KEEP_TURNS:
                for tid in list(d)[:-KEEP_TURNS]:
                    if tid not in keep:
                        del d[tid]


_rollouts = {}


def rollout(path, threshold=272000):
    r = _rollouts.get(str(path))
    if r is None:
        r = _rollouts[str(path)] = Rollout(path, threshold)
    return r.refresh()


# ---------- 数据库 ----------

def connect():
    db = codex_home() / "state_5.sqlite"
    con = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=2)
    con.row_factory = sqlite3.Row
    return con


def resolve_path(p):
    p = Path(p)
    return p if p.is_absolute() else codex_home() / p


def find_thread(con, thread_id):
    if thread_id:
        return con.execute("SELECT * FROM threads WHERE id = ?", (thread_id,)).fetchone(), "thread"
    row = con.execute("SELECT * FROM threads WHERE archived = 0 ORDER BY updated_at_ms DESC LIMIT 1").fetchone()
    return row, "recent"


def latest_quota(con, own, threshold):
    """账号额度：在当前会话和最近活跃的会话里取时间最新的那条。"""
    best = own.quota
    rows = con.execute(
        "SELECT rollout_path FROM threads WHERE archived = 0 ORDER BY updated_at_ms DESC LIMIT ?",
        (QUOTA_SCAN_THREADS,),
    ).fetchall()
    for row in rows:
        try:
            q = rollout(resolve_path(row["rollout_path"]), threshold).quota
        except OSError:
            continue
        if q and (best is None or (q[0] or "") > (best[0] or "")):
            best = q
    return best


# ---------- 快照 ----------

def turn_info(r, now, price):
    s, c = r.started, r.completed
    running = bool(s) and (not c or c.get("turn_id") != s.get("turn_id"))
    if running:
        tid = s.get("turn_id")
        lo = int(s.get("started_at", now) * 1000)
        hi = int(now * 1000)
        info = {"running": True, "elapsed_ms": max(0, hi - lo)}
    elif c:
        tid = c.get("turn_id")
        hi = int((c.get("completed_at") or 0) * 1000)
        lo = hi - (c.get("duration_ms") or 0)
        info = {"running": False, "duration_ms": c.get("duration_ms"), "ttft_ms": c.get("time_to_first_token_ms"),
                "completed_at": c.get("completed_at")}
    else:
        return None
    usage = r.turn_usage.get(tid)
    info["usage"] = usage
    tool_ms = busy_ms(r.turn_tools.get(tid, []), lo, hi)
    model_ms = max(0, hi - lo - tool_ms)
    info["tool_ms"] = tool_ms
    info["model_ms"] = model_ms
    out = (usage or {}).get("output_tokens")
    # 输出太少或时间太短时，首字等待占了大头，速度没有参考意义，干脆不给
    info["tps"] = round(out / (model_ms / 1000), 1) if out and out >= MIN_TPS_TOKENS and model_ms >= 1000 else None
    info["cost"] = price(r.turn_buckets.get(tid))
    return info


def snapshot(thread_id=None, now=None):
    now = time.time() if now is None else now
    prices = load_prices()
    threshold = prices.get("long_context_threshold") or 272000
    tier = service_tier()
    try:
        with closing(connect()) as con:
            row, mode = find_thread(con, thread_id)
            if row is None:
                return {"status": "unreadable", "mode": mode, "thread_id": thread_id, "reason": "数据库里找不到这条会话"}
            base = {
                "thread_id": row["id"],
                "title": row["name"] or row["title"] or "未命名会话",
                "model": row["model"],
                "mode": mode,
            }
            try:
                r = rollout(resolve_path(row["rollout_path"]), threshold)
            except OSError:
                return dict(base, status="unreadable", reason="会话日志文件读不到")
            quota = latest_quota(con, r, threshold)
    except sqlite3.Error as e:
        return {"status": "unreadable", "thread_id": thread_id, "reason": "数据库暂时读不了：%s" % e}

    def price(buckets):
        return price_buckets(buckets, base["model"], tier, prices)

    out = dict(base, turn=turn_info(r, now, price))
    if quota:
        rl = quota[1]
        out["quota"] = {
            "as_of": quota[0],
            "plan": rl.get("plan_type"),
            "windows": [dict(rl[k], slot=k) for k in ("primary", "secondary") if rl.get(k)],
        }
    if not r.token_count:
        out["status"] = "waiting"
        return out
    ts, payload = r.token_count
    info = payload["info"]
    total = info.get("total_token_usage") or {}

    cost = None
    if r.has_records:
        cost = price(r.session)
        basis = "per_request"
    elif total:
        # 老会话没有逐次请求记录：按累计数和短上下文价粗估，只会偏低
        inp, cached = total.get("input_tokens") or 0, total.get("cached_input_tokens") or 0
        cost = price({"short": {"uncached": max(0, inp - cached), "cached": cached, "write": 0,
                                "output": total.get("output_tokens") or 0, "requests": 0}})
        basis = "totals"
    if cost:
        cost.update(basis=basis, tier=tier, model=base["model"],
                    source=prices.get("source"), checked=prices.get("checked"))

    out.update(
        status="ok",
        sampled_at=ts,
        total=total,
        last=info.get("last_token_usage"),
        window=info.get("model_context_window"),
        cost=cost,
    )
    return out


# ---------- MCP ----------

TOOLS = [
    {
        "name": "token_dango_open",
        "title": "Token 团子",
        "description": "在会话面板里打开 Token 团子，显示这条会话的上下文占用、Token 用量、折算花费和账号额度。",
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"readOnlyHint": True, "title": "Token 团子"},
        "_meta": {
            "ui": {"resourceUri": UI_URI},
            "openai/ui": {"entrypoints": [{"type": "thread"}]},
            "openai/outputTemplate": UI_URI,
            "openai/widgetAccessible": True,
        },
    },
    {
        "name": "token_dango_snapshot",
        "title": "团子快照",
        "description": "返回指定会话的用量快照，供面板定时刷新。",
        "inputSchema": {"type": "object", "properties": {"thread_id": {"type": "string"}}},
        "annotations": {"readOnlyHint": True},
        "_meta": {"ui": {"visibility": ["app"]}, "openai/widgetAccessible": True},
    },
]


def thread_from_meta(meta):
    if isinstance(meta, dict):
        for k in THREAD_KEYS:
            v = meta.get(k)
            if isinstance(v, str) and v:
                return v
    return None


def call_tool(params):
    name = params.get("name")
    args = params.get("arguments") or {}
    if name not in ("token_dango_open", "token_dango_snapshot"):
        raise ValueError("unknown tool: %s" % name)
    snap = snapshot(args.get("thread_id") or thread_from_meta(params.get("_meta")))
    text = "Token 团子：%s" % (snap.get("title") or "未知会话")
    return {"content": [{"type": "text", "text": text}], "structuredContent": {"snapshot": snap}}


def handle(msg):
    method, mid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if mid is None:
        return None
    if method == "initialize":
        result = {
            "protocolVersion": params.get("protocolVersion", "2025-06-18"),
            "capabilities": {"tools": {}, "resources": {}},
            "serverInfo": {"name": "token-dango", "title": "Token 团子", "version": "0.2.0"},
        }
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        result = call_tool(params)
    elif method == "resources/list":
        result = {"resources": [{"uri": UI_URI, "name": "token-dango-panel", "mimeType": "text/html;profile=mcp-app"}]}
    elif method == "resources/templates/list":
        result = {"resourceTemplates": []}
    elif method == "resources/read":
        result = {"contents": [{
            "uri": UI_URI,
            "mimeType": "text/html;profile=mcp-app",
            "text": (HERE / "panel.html").read_text(encoding="utf-8"),
            "_meta": {"openai/ui": {"availableDisplayModes": ["fullscreen"]}, "ui": {"prefersBorder": False}},
        }]}
    else:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "Method not found: %s" % method}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        try:
            reply = handle(msg)
        except Exception as e:  # 不让单个请求拖垮整个服务
            reply = {"jsonrpc": "2.0", "id": msg.get("id"), "error": {"code": -32000, "message": str(e)}}
        if reply is not None:
            sys.stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
