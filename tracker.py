#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
单文件 token 用量统计（纯标准库，无第三方依赖）

功能:
  - 反向代理: 监听 0.0.0.0:11435 -> llama-server(默认 11434)
  - 用量落库: SQLite (token-tracker/usage.db)
  - Web 页面: 打开 http://host:11435 按日期区间查询记录与统计

客户端: base_url 改为 11435，并加请求头 X-Task-Id 或用请求体 user 字段区分任务
"""
import json
import os
import re
import collections
import sys
import sqlite3
import threading
import atexit
import signal
import socket
import datetime
import hashlib
import select
import urllib.request
import urllib.error
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = "http://localhost:11434"
LISTEN = ("0.0.0.0", 11435)
DB_PATH = os.path.join(HERE, "usage.db")

# ----------------------------- 会话识别（任务分组） -----------------------------
# 实测结论（dsh / CodeBuddy 等客户端均未提供可配置的自定义请求头）：
#   - CodeBuddy 自定义模型配置只有 url/apiKey，无 headers 字段；
#   - dsh 的 llm-pi-ai provider 也只有 apiKeyEnv/api/baseURL，无 headers。
# 因此任务名按下列优先级推导，可用USE_TASK_HEADER/BODY/FINGERPRINT 开关或改候选字段名来调整。
USE_TASK_HEADER = True       # 1) 请求头 X-Task-Id（脚本调用方可显式指定，最高优先）
TASK_HEADER_NAME = "X-Task-Id"
USE_TASK_BODY = True         # 2) 请求体会话字段：user / metadata.user_id / session_id / conversation_id ...
TASK_BODY_FIELDS = ("user", "metadata.user_id")  # OpenAI 兼容协议里真实存在的会话字段
TASK_BODY_SKIP_IF_MODEL = True  # user 值若等于模型名（有些客户端拿它放模型名），视为无效
USE_TASK_FINGERPRINT = True  # 3) 会话指纹：同一会话每轮都会重发完整历史，首条消息稳定 -> 用它做指纹
TASK_FINGERPRINT_PREFIX = 20  # 指纹里保留首条消息前 N 个字符，便于肉眼识别
TASK_FINGERPRINT_MAX = 400    # 参与指纹计算的首条消息最大截取长度
TASK_DEFAULT = "default"      # 都取不到时的兜底名
TRACE_SIZE = 200              # 最近 N 条请求的诊断记录（GET /api/trace），用于确认客户端实际发了什么
TRACE_VERBOSE = os.environ.get("TRACKER_TRACE", "") not in ("", "0")  # 设 TRACKER_TRACE=1 时把诊断打到控制台
TRACE_MASK = ("authorization", "x-api-key", "api-key", "cookie", "proxy-authorization")

# 等价 API 折算价（每 1M token，单位：人民币 ¥，参考 DeepSeek 最新模型，可自行修改）
# 时段定义（北京时间）：高峰 = 工作日 9:00–12:00、14:00–18:00；
#                      闲时 = 其余时间，价格为高峰的一半（已实现，见 _current_pricing）。
# 默认采用 DeepSeek-Flash (V4.1-Flash，最新默认模型) 高峰价
PEAK_PRICING = {
    "input_per_M": 2.00,    # 未命中缓存输入  ¥2.00 / 1M
    "cached_per_M": 0.04,   # 命中缓存输入    ¥0.04 / 1M
    "output_per_M": 8.00,   # 输出            ¥8.00 / 1M
}
OFFPEAK_PRICING = {
    "input_per_M": 1.00,    # 闲时（高峰价一半）¥1.00 / 1M
    "cached_per_M": 0.02,   # 闲时 ¥0.02 / 1M
    "output_per_M": 4.00,   # 闲时 ¥4.00 / 1M
}
EXCHANGE_RATE_RMB_PER_USD = 7.00  # 汇率：1 USD = 7.00 RMB，用于在前端折算美元显示

# ----------------------------- 自定义余额（持久化在 SQLite） -----------------------------
# 余额 = 累计发放额度（ledger 流水合计） - 累计消耗金额（- EXTRA_USED_RMB）
# 额度不再是配置文件：首次启动把 INITIAL_BALANCE_RMB 作为一条「初始额度」流水写入数据库，
# 之后由 Web 页面（增加/扣减/设定总额）或 POST /api/balance 随时增减，全部落库可追溯。
# INITIAL_BALANCE_RMB 设为 None 或 <= 0 表示初始不限额（接口返回 balance=null、unlimited=true）
INITIAL_BALANCE_RMB = 100.00  # 自定义初始余额额度（¥），仅首次启动时入库，之后以数据库为准
EXTRA_USED_RMB = 0.0          # 自定义：额度之外已消耗的金额（计入累计消耗，用于接续旧账）

# 余额查询接口的路径别名 -> 响应格式（full=本项目标准格式；openai/grants=兼容常见客户端探测格式）
BALANCE_ALIASES = {
    "/api/balance": "full",
    "/balance": "full",
    "/user/balance": "full",
    "/api/user/balance": "full",
    "/v1/dashboard/billing/subscription": "openai",
    "/dashboard/billing/subscription": "openai",
    "/dashboard/billing/credit_grants": "grants",
}

# 兼容历史字段/前端：暴露一份“参考单价”（取高峰价）对象，前端用于展示单位价格与汇率
PRICING = dict(PEAK_PRICING)
PRICING["rmb_per_usd"] = EXCHANGE_RATE_RMB_PER_USD

# 备选 DeepSeek-V4-Pro 高峰价（推理更强、不支持图像理解）：
#   PEAK_PRICING = {"input_per_M": 9.00, "cached_per_M": 0.30, "output_per_M": 27.00}
#   OFFPEAK_PRICING = {"input_per_M": 4.50, "cached_per_M": 0.15, "output_per_M": 13.50}

# ----------------------------- 分时计价 -----------------------------
def _beijing_now():
    # 无第三方依赖地取得北京时间（中国不实行夏令时，固定 UTC+8 即可）
    return datetime.datetime.now(datetime.timezone.utc).astimezone(
        datetime.timezone(datetime.timedelta(hours=8)))

def _is_peak(t):
    # 工作日(周一~周五)且落在 9:00–12:00 或 14:00–18:00 视为高峰时段
    if t.weekday() >= 5:  # 5=周六, 6=周日
        return False
    minutes = t.hour * 60 + t.minute
    return (9 * 60 <= minutes < 12 * 60) or (14 * 60 <= minutes < 18 * 60)

def _current_pricing():
    """按当前北京时间返回高峰/闲时单价表。"""
    return PEAK_PRICING if _is_peak(_beijing_now()) else OFFPEAK_PRICING

# ----------------------------- 数据库 -----------------------------
_lock = threading.Lock()
conn = sqlite3.connect(DB_PATH, check_same_thread=False)
atexit.register(conn.close)  # 进程退出时释放 SQLite 句柄

# 进程级资源追踪：用于终止时立即释放所有在途的后端连接
server = None
_active_lock = threading.Lock()
_active_responses = set()  # 当前正在转发、与 llama-server 的连接集合
_active_clients = set()    # 当前在途的客户端（上游调用方）连接集合，便于终止时统一断开
conn.execute("""CREATE TABLE IF NOT EXISTS usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT,
    day TEXT,
    task TEXT,
    prompt_tokens INTEGER,
    cached_tokens INTEGER,
    uncached_tokens INTEGER,
    completion_tokens INTEGER,
    total_tokens INTEGER,
    cost_rmb REAL,
    has_image INTEGER DEFAULT 0
)""")
# 余额流水表：额度增减全部落库（不依赖任何外部配置文件）
#   kind: init 初始额度 / topup 增加 / deduct 扣减 / set 设定总额
#   amount: 有符号变动额（+ 增加额度，- 扣减额度），累计额度 = SUM(amount)
conn.execute("""CREATE TABLE IF NOT EXISTS ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT,
    day TEXT,
    kind TEXT,
    amount REAL,
    note TEXT
)""")
conn.commit()

# 首次启动：把自定义初始额度写入流水（已有流水则跳过，数据库为准）
if INITIAL_BALANCE_RMB and INITIAL_BALANCE_RMB > 0:
    with _lock:
        if conn.execute("SELECT COUNT(*) FROM ledger").fetchone()[0] == 0:
            _now = datetime.datetime.now()
            conn.execute("INSERT INTO ledger (ts, day, kind, amount, note) "
                         "VALUES (?,?,?,?,?)",
                         (_now.isoformat(timespec="seconds"), _now.strftime("%Y-%m-%d"),
                          "init", float(INITIAL_BALANCE_RMB), "初始额度"))
            conn.commit()

# 字段配置：入库的输入/输出文本最大长度，0 = 不限制（存全文）
MAX_INPUT = 0
MAX_OUTPUT = 0

# 兼容已有库：补齐新列
_cols = [r[1] for r in conn.execute("PRAGMA table_info(usage)")]
if "input_text" not in _cols:
    conn.execute("ALTER TABLE usage ADD COLUMN input_text TEXT")
if "output_text" not in _cols:
    conn.execute("ALTER TABLE usage ADD COLUMN output_text TEXT")
if "reasoning_text" not in _cols:
    conn.execute("ALTER TABLE usage ADD COLUMN reasoning_text TEXT")
if "input_raw" not in _cols:
    conn.execute("ALTER TABLE usage ADD COLUMN input_raw TEXT")
if "has_image" not in _cols:
    conn.execute("ALTER TABLE usage ADD COLUMN has_image INTEGER DEFAULT 0")
if "model" not in _cols:
    conn.execute("ALTER TABLE usage ADD COLUMN model TEXT")          # 请求里的模型名（客户端/后端区分）
if "user_agent" not in _cols:
    conn.execute("ALTER TABLE usage ADD COLUMN user_agent TEXT")     # 客户端 UA（区分 dsh / CodeBuddy 等）
if "task_src" not in _cols:
    conn.execute("ALTER TABLE usage ADD COLUMN task_src TEXT")       # 任务名来源：header/body/fingerprint
# 历史库：cost_usd 重命名为 cost_rmb（语义修正——该列实际存的是人民币，非美元）
cost_col = "cost_rmb"
if "cost_usd" in _cols and "cost_rmb" not in _cols:
    try:
        conn.execute("ALTER TABLE usage RENAME COLUMN cost_usd TO cost_rmb")
    except sqlite3.OperationalError:
        # 极老版本 SQLite 不支持 RENAME COLUMN：保留原列名，值仍为人民帀
        cost_col = "cost_usd"
conn.commit()


def compute_cost(u, pricing):
    return (u["uncached_tokens"] / 1_000_000 * pricing["input_per_M"]
            + u["cached_tokens"] / 1_000_000 * pricing["cached_per_M"]
            + u["completion_tokens"] / 1_000_000 * pricing["output_per_M"])


def save_usage(task_id, u, input_text="", output_text="", reasoning_text="",
               input_raw="", has_image=0, model="", user_agent="", task_src=""):
    now = datetime.datetime.now()
    pricing = _current_pricing()  # 按当前北京时间取高峰/闲时单价
    with _lock:
        conn.execute(
            "INSERT INTO usage (ts, day, task, prompt_tokens, cached_tokens, "
            "uncached_tokens, completion_tokens, total_tokens, {cc}, "
            "input_text, output_text, reasoning_text, input_raw, has_image, "
            "model, user_agent, task_src) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)".format(cc=cost_col),
            (now.isoformat(timespec="seconds"), now.strftime("%Y-%m-%d"), task_id,
             u["prompt_tokens"], u["cached_tokens"], u["uncached_tokens"],
             u["completion_tokens"], u["total_tokens"], round(compute_cost(u, pricing), 6),
             input_text, output_text, reasoning_text, input_raw, int(has_image),
             str(model or "")[:120], str(user_agent or "")[:200], str(task_src or "")[:40]))
        conn.commit()


def _like(kw):
    """把用户输入转成 LIKE 的模糊匹配串（转义 % _ \\ 通配符）。"""
    kw = kw.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return "%" + kw + "%"


def _where(from_d, to_d, task=None, task_like=None, input_like=None):
    clauses, params = [], []
    if from_d:
        clauses.append("day >= ?"); params.append(from_d)
    if to_d:
        clauses.append("day <= ?"); params.append(to_d)
    if task:
        clauses.append("task = ?"); params.append(task)
    if task_like and task_like.strip():
        clauses.append("task LIKE ? ESCAPE '\\'"); params.append(_like(task_like))
    if input_like and input_like.strip():
        clauses.append("input_text LIKE ? ESCAPE '\\'"); params.append(_like(input_like))
    return ((" WHERE " + " AND ".join(clauses)) if clauses else ""), params


def _task_rows(rows):
    return [{"task": r[0], "calls": r[1], "prompt": r[2] or 0, "cached": r[3] or 0,
             "uncached": r[4] or 0, "completion": r[5] or 0, "total": r[6] or 0,
             "cost": round(r[7] or 0, 6), "first_ts": r[8] or "", "last_ts": r[9] or ""}
            for r in rows]


_TASK_GROUP_SQL = ("SELECT task, COUNT(1), SUM(prompt_tokens), SUM(cached_tokens), "
                   "SUM(uncached_tokens), SUM(completion_tokens), SUM(total_tokens), "
                   "SUM(" + cost_col + "), MIN(ts), MAX(ts) FROM usage")


def query_tasks(from_d=None, to_d=None, task_like=None, input_like=None, limit=300):
    """任务清单（供搜索框下拉建议）：任务名 + 调用次数 + token + 金额。"""
    where, params = _where(from_d, to_d, None, task_like, input_like)
    with _lock:
        rows = conn.execute(_TASK_GROUP_SQL + where +
                            " GROUP BY task ORDER BY COUNT(1) DESC LIMIT ?",
                            params + [int(limit)]).fetchall()
    return _task_rows(rows)


def query_tasks_page(from_d=None, to_d=None, task_like=None, input_like=None,
                     page=1, size=10):
    """按任务汇总的分页查询：返回 {total, page, size, pages, items}。"""
    page = max(1, _int_arg(page, 1))
    size = max(1, min(200, _int_arg(size, 10)))
    where, params = _where(from_d, to_d, None, task_like, input_like)
    with _lock:
        total = conn.execute("SELECT COUNT(DISTINCT task) FROM usage" + where,
                             params).fetchone()[0] or 0
        rows = conn.execute(_TASK_GROUP_SQL + where +
                            " GROUP BY task ORDER BY SUM(total_tokens) DESC LIMIT ? OFFSET ?",
                            params + [size, (page - 1) * size]).fetchall()
    pages = max(1, (total + size - 1) // size)
    page = min(page, pages)
    return {"total": total, "page": page, "size": size, "pages": pages,
            "items": _task_rows(rows)}


def query_stats(from_d, to_d, task=None, task_like=None, input_like=None):
    where, params = _where(from_d, to_d, task, task_like, input_like)
    with _lock:
        rows = conn.execute(
            f"SELECT task, COUNT(*), SUM(prompt_tokens), SUM(cached_tokens), "
            f"SUM(uncached_tokens), SUM(completion_tokens), SUM(total_tokens), "
            f"SUM({cost_col}) FROM usage" + where +
            " GROUP BY task ORDER BY SUM(total_tokens) DESC", params).fetchall()
        tot = conn.execute(
            f"SELECT COUNT(*), SUM(prompt_tokens), SUM(cached_tokens), "
            f"SUM(uncached_tokens), SUM(completion_tokens), SUM(total_tokens), SUM({cost_col}) "
            f"FROM usage" + where, params).fetchone()
    tasks = [{"task": r[0], "calls": r[1], "prompt": r[2] or 0, "cached": r[3] or 0,
              "uncached": r[4] or 0, "completion": r[5] or 0, "total": r[6] or 0,
              "cost": round(r[7] or 0, 6)} for r in rows]
    total = {"calls": tot[0] or 0, "prompt": tot[1] or 0, "cached": tot[2] or 0,
             "uncached": tot[3] or 0, "completion": tot[4] or 0,
             "total": tot[5] or 0, "cost": round(tot[6] or 0, 6)}
    return {"from": from_d, "to": to_d, "total": total, "tasks": tasks, "pricing": PRICING}


_RECORD_COLS = ("SELECT id, ts, task, prompt_tokens, cached_tokens, uncached_tokens, "
                "completion_tokens, total_tokens, " + cost_col + ", input_text, output_text, "
                "reasoning_text, input_raw, has_image, model, user_agent, task_src FROM usage")


def _record_rows(rows):
    return [{"id": r[0], "ts": r[1], "task": r[2], "prompt": r[3], "cached": r[4],
             "uncached": r[5], "completion": r[6], "total": r[7],
             "cost": round(r[8], 6), "input": r[9] or "", "output": r[10] or "",
             "reasoning": r[11] or "", "raw": r[12] or "", "has_image": r[13] or 0,
             "model": r[14] or "", "client": r[15] or "", "task_src": r[16] or ""}
            for r in rows]


def query_records(from_d, to_d, task=None, task_like=None, input_like=None):
    where, params = _where(from_d, to_d, task, task_like, input_like)
    with _lock:
        rows = conn.execute(_RECORD_COLS + where + " ORDER BY id DESC", params).fetchall()
    return _record_rows(rows)


def query_records_page(from_d, to_d, task=None, task_like=None, input_like=None,
                       page=1, size=20):
    """分页查询明细记录：返回 {total, page, size, pages, items}（列表页用）。"""
    page = max(1, _int_arg(page, 1))
    size = max(1, min(200, _int_arg(size, 20)))
    where, params = _where(from_d, to_d, task, task_like, input_like)
    with _lock:
        total = conn.execute("SELECT COUNT(1) FROM usage" + where, params).fetchone()[0] or 0
        rows = conn.execute(_RECORD_COLS + where + " ORDER BY id DESC LIMIT ? OFFSET ?",
                            params + [size, (page - 1) * size]).fetchall()
    pages = max(1, (total + size - 1) // size)
    page = min(page, pages)
    return {"total": total, "page": page, "size": size, "pages": pages,
            "items": _record_rows(rows)}


def delete_records(ids=None, from_d=None, to_d=None, task=None):
    """批量删除：按 id 列表，或按日期区间(+可选任务)。返回删除条数。"""
    clauses, params = [], []
    if ids:
        ph = ",".join("?" * len(ids))
        clauses.append(f"id IN ({ph})"); params.extend(ids)
    else:
        if from_d:
            clauses.append("day >= ?"); params.append(from_d)
        if to_d:
            clauses.append("day <= ?"); params.append(to_d)
        if task:
            clauses.append("task = ?"); params.append(task)
    if not clauses:
        return 0
    where = " WHERE " + " AND ".join(clauses)
    with _lock:
        cur = conn.execute("DELETE FROM usage" + where, params)
        conn.commit()
        return cur.rowcount


def _exact_task(qs):
    """从查询串取精确任务名：?task=xxx（点「按任务汇总」行时用）。"""
    return (qs.get("task", [""])[0] or "").strip() or None


# ----------------------------- 余额（自定义额度，落库管理） -----------------------------
def _int_arg(v, default=0):
    """把查询串等外部输入安全转成 int，失败返回 default。"""
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def granted_total(as_of=None):
    """累计发放额度 = ledger 流水合计。返回 (额度合计, 流水条数)。

    as_of 给定时只统计该日期（含）之前的流水，即“截止某日的累计发放额度”。
    """
    with _lock:
        if as_of:
            row = conn.execute("SELECT COALESCE(SUM(amount), 0), COUNT(*) FROM ledger "
                               "WHERE day <= ?", (as_of,)).fetchone()
        else:
            row = conn.execute("SELECT COALESCE(SUM(amount), 0), COUNT(*) FROM ledger").fetchone()
    return round(row[0] or 0, 6), row[1] or 0


def adjust_balance(delta, note="", kind=None):
    """增减余额额度：写入一条 ledger 流水（有符号 amount），返回最新余额信息。

    delta > 0 增加额度，delta < 0 扣减额度。传 kind 可自定义类型标签。
    """
    delta = round(float(delta), 6)
    if delta == 0:
        raise ValueError("变动金额不能为 0")
    now = datetime.datetime.now()
    with _lock:
        conn.execute("INSERT INTO ledger (ts, day, kind, amount, note) VALUES (?,?,?,?,?)",
                     (now.isoformat(timespec="seconds"), now.strftime("%Y-%m-%d"),
                      kind or ("topup" if delta > 0 else "deduct"), delta,
                      str(note or "")[:200]))
        conn.commit()
    return query_balance()


def set_granted(value, note=""):
    """把累计额度直接设定为 value（自动换算增减额并记流水）。"""
    target = round(float(value), 6)
    current, _n = granted_total()
    if round(target - current, 6) == 0:
        return query_balance()  # 已是目标额度，不记流水
    return adjust_balance(target - current, note=note or "设定总额", kind="set")


def query_ledger(limit=50):
    """额度流水（倒序）。"""
    limit = _int_arg(limit, 50)
    limit = max(1, min(limit, 500))
    with _lock:
        rows = conn.execute("SELECT id, ts, kind, amount, note FROM ledger "
                            "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [{"id": r[0], "ts": r[1], "kind": r[2], "amount": round(r[3], 6),
             "note": r[4] or ""} for r in rows]


def delete_ledger(ids=None):
    """删除额度流水（撤销误操作），额度自动按剩余流水重算。返回删除条数。"""
    ids = [int(i) for i in (ids or []) if str(i).lstrip("-").isdigit()]
    if not ids:
        return 0
    ph = ",".join("?" * len(ids))
    with _lock:
        cur = conn.execute(f"DELETE FROM ledger WHERE id IN ({ph})", ids)
        conn.commit()
        return cur.rowcount


def query_balance(task=None, as_of=None):
    """查询自定义余额：累计发放额度 - 累计消耗。

    task  按任务维度核算；as_of 额外给出“截止某日”的额度与余额快照。
    """
    granted, entries = granted_total()
    # 没有任何额度流水 -> 未配置额度，按不限额处理
    unlimited = entries == 0
    extra = EXTRA_USED_RMB

    def _sum(from_d=None, to_d=None):
        where, params = _where(from_d, to_d, task)
        with _lock:
            return conn.execute(
                f"SELECT COUNT(*), SUM(prompt_tokens), SUM(cached_tokens), "
                f"SUM(uncached_tokens), SUM(completion_tokens), SUM(total_tokens), "
                f"SUM({cost_col}) FROM usage" + where, params).fetchone()

    today = datetime.datetime.now().strftime("%Y-%m-%d")
    total = _sum()
    used_total = round((total[6] or 0) + extra, 6)
    used_today = round(_sum(today, today)[6] or 0, 6)
    used_month = round(_sum(today[:7] + "-01", today)[6] or 0, 6)
    balance = None if unlimited else round(granted - used_total, 6)
    pct = None if unlimited or granted <= 0 else round(used_total / granted * 100, 4)

    # 截止日快照：额度与消耗都只算该日期（含）之前，即“当时还剩多少额度”
    granted_as_of, balance_as_of, used_as_of = None, None, None
    if as_of:
        granted_as_of, _n = granted_total(as_of)
        used_as_of = round((_sum(None, as_of)[6] or 0) + extra, 6)
        if not unlimited:
            balance_as_of = round(granted_as_of - used_as_of, 6)
    return {
        "object": "balance",
        "balance": balance,                 # 剩余余额（¥），不限额时为 null
        "currency": "CNY",
        "unlimited": unlimited,
        "overdrawn": (balance is not None and balance < 0),
        "granted": granted,                 # 累计发放额度（¥）= 流水合计
        "initial_balance": granted,         # 兼容字段（同 granted）
        "as_of": as_of or None,             # 截止日期（快照口径），未指定为 null
        "balance_as_of": balance_as_of,     # 截止日期剩余额度（¥）
        "granted_as_of": granted_as_of,     # 截止日期累计发放额度（¥）
        "used_as_of": used_as_of,           # 截止日期累计消耗（¥）
        "ledger_entries": entries,          # 额度流水条数
        "total_used": used_total,           # 累计消耗（¥）
        "used_today": used_today,
        "used_this_month": used_month,
        "extra_used": round(extra, 6),      # 自定义的额外已用金额
        "used_percent": pct,                # 已用百分比
        "remaining_percent": None if pct is None else round(100 - pct, 4),
        "task": task or None,               # 核算维度：None=全部
        "calls": total[0] or 0,
        "prompt_tokens": total[1] or 0,
        "cached_tokens": total[2] or 0,
        "uncached_tokens": total[3] or 0,
        "completion_tokens": total[4] or 0,
        "total_tokens": total[5] or 0,
        "pricing": PRICING,
        "updated_at": datetime.datetime.now().isoformat(timespec="seconds"),
    }


# ----------------------------- 会话识别（任务名推导） -----------------------------
# 客户端注入的上下文块：这些内容在同一工作区的每个会话里几乎相同，
# 若拿它们做指纹会把不同会话合并成同一个任务，必须先剔除。
INJECT_TAGS = ("user_info", "environment_details", "additional_data", "system_reminder",
               "command-name", "command-message", "command-args", "local-command-stdout",
               "local-command-stderr", "ide_selection", "ide_opened_file", "attachments")
USER_QUERY_RE = re.compile(r"<user_query>([\s\S]*?)</user_query>", re.I)


def _strip_injected(text):
    """剔除 IDE/客户端注入的上下文块，只留下人写的内容。"""
    for tag in INJECT_TAGS:
        text = re.sub(r"<%s[^>]*>[\s\S]*?</%s>" % (tag, tag), " ", text, flags=re.I)
    # 兜底：其它成对的 XML 风格注入块
    text = re.sub(r"<[a-z][a-z0-9_-]{3,30}>[\s\S]*?</[a-z][a-z0-9_-]{3,30}>", " ", text, flags=re.I)
    return re.sub(r"\s+", " ", text).strip()


def _first_message_text(req, limit=TASK_FINGERPRINT_MAX):
    """取会话的「第一句真实人话」作为指纹素材。

    客户端每轮都会重发完整历史，因此这句在会话内保持不变；优先级：
      1) 第一条 user 消息里的 <user_query>（CodeBuddy/IDE 把真实提问放这里）；
      2) 第一条 user 消息去掉注入块后的剩余内容；
      3) system 首行（仅用于连注入块都没有时的兜底）。
    """
    msgs = [m for m in (req.get("messages") or []) if isinstance(m, dict)]
    users = [m for m in msgs if m.get("role") == "user"]
    for m in users:                      # 1) 真实提问（最可靠，且跨会话唯一）
        found = USER_QUERY_RE.search(_msg_text(m.get("content", "")))
        if found and found.group(1).strip():
            return re.sub(r"\s+", " ", found.group(1).strip())[:limit]
    for m in users:                      # 2) 去掉注入块后的正文
        text = _strip_injected(_msg_text(m.get("content", "")))
        if text:
            return text[:limit]
    for role in ("system", "assistant"):  # 3) 兜底：system 首行
        for m in msgs:
            if m.get("role") == role:
                text = _strip_injected(_msg_text(m.get("content", "")))
                if text:
                    return text[:limit]
    return ""


def _fingerprint(text):
    """首句人话 -> 稳定的短指纹，形如 fp-1a2b3c4d-帮我实现xxx。"""
    if not text:
        return ""
    digest = hashlib.sha1(text.encode("utf-8", "ignore")).hexdigest()[:8]
    flat = re.sub(r"\s+", " ", text).strip()
    cut = flat[:TASK_FINGERPRINT_PREFIX]
    sp = cut.rfind(" ")          # 拉丁文尽量断在词边界，避免出现半截单词
    if sp >= TASK_FINGERPRINT_PREFIX // 2:
        cut = cut[:sp]
    cut = cut.strip(" ,.;:、，。-—")
    return "fp-%s-%s" % (digest, cut) if cut else "fp-%s" % digest


def resolve_task(headers, req):
    """推导任务名与来源：X-Task-Id 头 > 请求体会话字段 > 会话指纹 > default。"""
    if USE_TASK_HEADER:
        h = headers.get(TASK_HEADER_NAME) or headers.get(TASK_HEADER_NAME.lower())
        if h and h.strip():
            return h.strip()[:120], "header"
    if USE_TASK_BODY and isinstance(req, dict):
        model = str(req.get("model") or "")
        for field in TASK_BODY_FIELDS:
            cur = req
            for part in field.split("."):
                cur = cur.get(part) if isinstance(cur, dict) else None
                if cur in (None, "", [], {}):
                    cur = None
                    break
            if cur is None:
                continue
            val = str(cur).strip()
            if not val:
                continue
            if TASK_BODY_SKIP_IF_MODEL and model and val == model:
                continue  # 客户端把模型名塞在 user 里，不能当任务名
            return val[:120], "body:" + field
    if USE_TASK_FINGERPRINT:
        fp = _fingerprint(_first_message_text(req or {}))
        if fp:
            return fp, "fingerprint"
    return TASK_DEFAULT, "default"


_trace_log = collections.deque(maxlen=TRACE_SIZE)   # 最近若干条 chat 请求的诊断信息


def _trace(handler, task_id, task_src, model, user_agent, req):
    """记录一条 chat 请求的会话识别现场，用于确认客户端到底发了什么（GET /api/trace）。

    只保留头名与脱敏后的值，避免泄露 API Key；同时记录 body 顶层字段名与会话字段取值。
    """
    hdrs = {}
    for k, v in handler.headers.items():
        key = k.lower()
        if key in TRACE_MASK or key.startswith("x-api-"):
            hdrs[k] = "***"
        elif key in ("user-agent", "x-task-id", "x-session-id", "session_id",
                     "x-conversation-id", "x-thread-id", "x-request-id", "x-app",
                     "x-client", "referer", "origin"):
            hdrs[k] = v[:120]
        else:
            hdrs[k] = "<%d bytes>" % len(v or "")
    info = {
        "ts": datetime.datetime.now().isoformat(timespec="seconds"),
        "client": handler.client_address[0] if handler.client_address else "",
        "task": task_id,
        "task_src": task_src,
        "model": model,
        "user_agent": (user_agent or "")[:120],
        "body_keys": sorted((req or {}).keys()),
        "body_user": str((req or {}).get("user", ""))[:120],
        "metadata": (req or {}).get("metadata"),
        "first_msg": _first_message_text(req or {})[:60],
        "headers": hdrs,
    }
    _trace_log.append(info)
    if TRACE_VERBOSE:
        sys.stderr.write("[trace] %s" % json.dumps(info, ensure_ascii=False) + "\n")


# ----------------------------- usage 解析 -----------------------------
def extract_usage(body):
    try:
        text = body.decode("utf-8", "ignore")
    except Exception:
        return None
    if body.lstrip().startswith(b"{"):
        try:
            return json.loads(text).get("usage")
        except Exception:
            return None
    usage = None
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[len("data:"):].strip()
        if payload in ("", "[DONE]"):
            continue
        try:
            obj = json.loads(payload)
        except Exception:
            continue
        if obj.get("usage"):
            usage = obj["usage"]
    return usage


def _msg_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):  # 多模态 parts
        return " ".join(p.get("text", "") for p in content if isinstance(p, dict))
    return ""


def extract_input(req):
    parts = []
    for m in req.get("messages") or []:
        role = m.get("role", "?")
        parts.append(f"[{role}]\n{_msg_text(m.get('content', ''))}")
    text = "\n\n".join(parts)
    return text if MAX_INPUT <= 0 else text[:MAX_INPUT]


def detect_images(req):
    """检测 messages 中是否含图片（OpenAI 多模态格式：image_url / image / base64 data:image）。"""
    for m in req.get("messages") or []:
        c = m.get("content")
        if isinstance(c, list):
            for part in c:
                if not isinstance(part, dict):
                    continue
                if part.get("type") in ("image", "image_url") or part.get("image_url"):
                    return 1
                img = part.get("image")
                if isinstance(img, str) and img.startswith("data:image"):
                    return 1
        elif isinstance(c, str) and "data:image" in c:
            return 1
    return 0


def extract_output_text(body):
    """从响应体提取生成的文本，返回 (reasoning, content) 两个独立片段。

    同时抓取 thinking/reasoning 内容：
      非流式 -> message.reasoning_content / message.content
      流式   -> delta.reasoning_content / delta.content
    """
    try:
        text = body.decode("utf-8", "ignore")
    except Exception:
        return "", ""
    reason, out = [], []
    if body.lstrip().startswith(b"{"):
        try:
            msg = json.loads(text)["choices"][0]["message"]
            reason.append(msg.get("reasoning_content", "") or "")
            out.append(_msg_text(msg.get("content", "")))
        except Exception:
            return "", ""
    else:
        for line in text.splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            payload = line[len("data:"):].strip()
            if payload in ("", "[DONE]"):
                continue
            try:
                obj = json.loads(payload)
            except Exception:
                continue
            try:
                delta = obj["choices"][0].get("delta", {})
                if delta.get("reasoning_content"):
                    reason.append(delta["reasoning_content"])
                if delta.get("content"):
                    out.append(delta["content"])
            except Exception:
                continue
    r = "".join(reason)
    c = "".join(out)
    return (r if MAX_OUTPUT <= 0 else r[:MAX_OUTPUT],
            c if MAX_OUTPUT <= 0 else c[:MAX_OUTPUT])


def _parse_sse_line(line, usage_holder, reason_parts, content_parts):
    """增量解析单行 SSE (data: ...)，实时累积 usage / reason / content。

    usage_holder: 长度 1 的列表，用于回写最新的 usage 对象（流式末尾才有完整值）。
    reason_parts / content_parts: 增量拼接的正文片段列表。
    原始响应字节不在此保留，转发后即释放。
    """
    s = line.strip()
    if not s.startswith(b"data:"):
        return
    payload = s[len(b"data:"):].strip()
    if not payload or payload == b"[DONE]":
        return
    try:
        obj = json.loads(payload)
    except Exception:
        return
    if obj.get("usage"):
        usage_holder[0] = obj["usage"]
    try:
        delta = obj["choices"][0].get("delta", {})
        rc = delta.get("reasoning_content")
        cc = delta.get("content")
        if rc:
            reason_parts.append(rc)
        if cc:
            content_parts.append(cc)
    except Exception:
        pass


def norm_usage(usage):
    if not usage:
        return None
    prompt = int(usage.get("prompt_tokens", 0) or 0)
    completion = int(usage.get("completion_tokens", 0) or 0)
    d = usage.get("prompt_tokens_details") or {}
    cached = int(d.get("cached_tokens") or d.get("cache_read_tokens")
                 or usage.get("cached_prompt_tokens") or 0)
    return {
        "prompt_tokens": prompt,
        "cached_tokens": cached,
        "uncached_tokens": max(prompt - cached, 0),
        "completion_tokens": completion,
        "total_tokens": int(usage.get("total_tokens", prompt + completion)
                            or (prompt + completion)),
    }


# ----------------------------- Web 页面（独立文件 web.html） -----------------------------
WEB_PATH = os.path.join(HERE, "web.html")

def load_html():
    try:
        with open(WEB_PATH, encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return "<h1>web.html 未找到，请确认与 tracker.py 同目录</h1>"


# ----------------------------- 代理 / 路由 -----------------------------
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"  # 提升流式(SSE)客户端兼容性；配合 Connection: close 以连接关闭界定消息结束

    def handle(self):
        # 登记客户端连接：退出时 _request_shutdown 可主动断开上游调用方，
        # 避免其因代理进程退出而长时间挂起。
        with _active_lock:
            _active_clients.add(self.connection)
        try:
            super().handle()
        finally:
            with _active_lock:
                _active_clients.discard(self.connection)

    def do_GET(self):
        p = self.path.split("?")[0]
        if p in ("/", "/index.html", "/web.html"):
            self._send_html(load_html())
        elif p in BALANCE_ALIASES:
            self._balance_api(BALANCE_ALIASES[p])
        elif p.startswith("/api/"):
            self._api(p)
        else:
            self._proxy("GET")

    def do_POST(self):
        p = self.path.split("?")[0]
        if p in ("/api/delete", "/api/delete/"):
            self._api_delete()
        elif p in ("/api/balance", "/api/balance/", "/api/balance/set"):
            self._api_adjust_balance()
        elif p in ("/api/balance/delete", "/api/ledger/delete"):
            self._api_delete_ledger()
        elif p.startswith("/v1/"):
            self._proxy("POST")
        else:
            self._send_json(404, {"error": "not found"})

    # ---- Web API ----
    def _api(self, p):
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        f = qs.get("from", [""])[0] or None
        t = qs.get("to", [""])[0] or None
        if p.startswith("/api/tasks"):  # 任务清单（搜索框建议）/ 汇总分页
            kw = qs.get("task_like", [""])[0] or qs.get("kw", [""])[0]
            if qs.get("page"):        # 按任务汇总的分页查询
                self._send_json(200, query_tasks_page(
                    f, t, task_like=kw,
                    input_like=qs.get("input_like", [""])[0],
                    page=qs.get("page", ["1"])[0], size=qs.get("size", ["10"])[0]))
            else:                     # 下拉建议用的清单
                self._send_json(200, query_tasks(f, t, kw,
                                                 input_like=qs.get("input_like", [""])[0],
                                                 limit=_int_arg(qs.get("limit", ["300"])[0], 300)))
        elif p.startswith("/api/stats"):
            self._send_json(200, query_stats(f, t, task=_exact_task(qs),
                                              task_like=qs.get("task_like", [""])[0],
                                              input_like=qs.get("input_like", [""])[0]))
        elif p.startswith("/api/records"):
            if qs.get("page"):        # 分页查询（列表页）
                self._send_json(200, query_records_page(
                    f, t, task=_exact_task(qs), task_like=qs.get("task_like", [""])[0],
                    input_like=qs.get("input_like", [""])[0],
                    page=qs.get("page", ["1"])[0], size=qs.get("size", ["20"])[0]))
            else:                     # 兼容旧调用：一次性返回全部
                self._send_json(200, query_records(f, t, task=_exact_task(qs),
                                                   task_like=qs.get("task_like", [""])[0],
                                                   input_like=qs.get("input_like", [""])[0]))
        elif p.startswith("/api/trace"):  # 会话识别诊断：客户端实际带了哪些标识
            n = _int_arg(qs.get("limit", ["20"])[0], 20)
            self._send_json(200, {"trace": list(_trace_log)[-n:], "size": TRACE_SIZE})
        elif p.startswith("/api/balance/log"):  # 额度流水（须在 /api/balance 之前判断）
            self._send_json(200, query_ledger(_int_arg(qs.get("limit", ["50"])[0], 50)))
        elif p.startswith("/api/balance"):
            self._balance_api("full")
        else:
            self._send_json(404, {"error": "not found"})

    def _balance_api(self, fmt="full"):
        """余额查询：GET，支持 ?task=xx 按任务核算、?to=YYYY-MM-DD 取截止该日的额度快照。"""
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        as_of = qs.get("to", [""])[0] or qs.get("as_of", [""])[0] or None
        b = query_balance(qs.get("task", [""])[0] or None, as_of=as_of)
        remain = b["balance"]
        if fmt == "openai":  # 兼容 OpenAI 旧版 billing 探测格式
            hard = None if not isinstance(remain, (int, float)) else round(
                float(remain) / EXCHANGE_RATE_RMB_PER_USD, 2)
            self._send_json(200, {
                "object": "billing_subscription",
                "has_payment_method": True,
                "soft_limit_usd": hard,
                "hard_limit_usd": hard,
                "system_hard_limit_usd": hard,
                "access_until": int((datetime.datetime.now()
                                     + datetime.timedelta(days=3650)).timestamp()),
            })
        elif fmt == "grants":  # 兼容 credit_grants 格式
            self._send_json(200, {
                "object": "credit_summary",
                "total_granted": b["granted"],
                "total_used": b["total_used"],
                "total_available": b["balance"],
            })
        else:
            self._send_json(200, b)

    def _api_adjust_balance(self):
        """增减余额（落库）：POST {"delta": 50} 增减 / {"set": 200} 设定总额，可带 {"note": "..."}。"""
        body = self._read_body()
        if not isinstance(body, dict):
            self._send_json(400, {"error": "invalid_body",
                                  "detail": "body 需为 JSON 对象"})
            return
        note = body.get("note") or ""
        try:
            if body.get("set") is not None:
                result = set_granted(body["set"], note)
            elif body.get("delta") is not None:
                result = adjust_balance(body["delta"], note)
            else:
                self._send_json(400, {"error": "invalid_body",
                                      "detail": '需要 {"delta": ±金额} 或 {"set": 总额}'})
                return
        except (TypeError, ValueError) as e:
            self._send_json(400, {"error": "invalid_balance", "detail": str(e)})
            return
        except Exception as e:
            self._send_json(500, {"error": "balance_error", "detail": str(e)})
            return
        result["ledger"] = query_ledger(50)
        self._send_json(200, result)

    def _api_delete_ledger(self):
        """删除额度流水：POST {"ids":[...]}（额度按剩余流水自动重算）。"""
        body = self._read_body()
        ids = body.get("ids") if isinstance(body, dict) else None
        if ids is not None and not isinstance(ids, list):
            ids = None
        deleted = delete_ledger(ids=ids)
        self._send_json(200, {"deleted": deleted, "ledger": query_ledger(50),
                              "balance": query_balance()})

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            return {}

    def _api_delete(self):
        body = self._read_body()
        ids = body.get("ids")
        if ids is not None and not isinstance(ids, list):
            ids = None
        deleted = delete_records(ids=ids, from_d=body.get("from"), to_d=body.get("to"),
                                  task=body.get("task"))
        self._send_json(200, {"deleted": deleted})

    def _api_delete(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            body = {}
        ids = body.get("ids")
        if ids is not None and not isinstance(ids, list):
            ids = None
        deleted = delete_records(ids=ids, from_d=body.get("from"), to_d=body.get("to"),
                                  task=body.get("task"))
        self._send_json(200, {"deleted": deleted})

    # ---- 反向代理（保留流式）----
    def _proxy(self, method):
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length) if length else b""
        task_id, task_src = TASK_DEFAULT, "default"
        model = ""
        req = {}
        is_chat = self.path.split("?")[0].rstrip("/").endswith("/chat/completions")
        input_text = ""
        input_raw = ""
        has_image = False
        is_stream = False
        if is_chat:
            try:
                req = json.loads(body) if body else {}
                if req.get("stream"):
                    req.setdefault("stream_options", {})["include_usage"] = True
                    body = json.dumps(req).encode()
                    is_stream = True
                task_id, task_src = resolve_task(self.headers, req)
                model = str(req.get("model") or "")
                input_text = extract_input(req)
                if req.get("messages") is not None:
                    input_raw = json.dumps(req.get("messages"), ensure_ascii=False)
                has_image = detect_images(req)
            except Exception:
                pass
        user_agent = self.headers.get("User-Agent") or ""
        if is_chat:
            _trace(self, task_id, task_src, model, user_agent, req)

        url = BACKEND + self.path
        # 注意：body 为空时必须传 None，否则 urllib 会把空 body 当作有体请求（给 GET 附加空体）
        r = urllib.request.Request(url, data=body if body else None, method=method)
        for k, v in self.headers.items():
            if k.lower() in ("host", "content-length"):
                continue
            r.add_header(k, v)
        if body:
            # 仅当客户端未自带 Content-Type 时默认 application/json，避免覆盖非 JSON 体
            if "content-type" not in (k.lower() for k in self.headers):
                r.add_header("Content-Type", "application/json")
            r.add_header("Content-Length", str(len(body)))

        try:
            resp = urllib.request.urlopen(r, timeout=1800)
            status = resp.status
            resp_headers = resp.headers
        except urllib.error.HTTPError as e:
            status = e.code
            resp_headers = e.headers
            resp = e
        except urllib.error.URLError as e:
            # 后端不可达 / 连接被拒 / DNS 失败等：返回 502，避免客户端拿到空连接
            self._send_json(502, {"error": "backend_unreachable",
                                  "detail": str(getattr(e, "reason", "") or e)})
            return
        except Exception as e:
            self._send_json(502, {"error": "proxy_error", "detail": str(e)})
            return

        with _active_lock:
            _active_responses.add(resp)  # 登记在途后端连接，便于终止时统一释放

        self.send_response(status)
        self.send_header("Content-Type", resp_headers.get("Content-Type", "application/json"))
        self.send_header("Connection", "close")
        self.end_headers()

        client_gone = False
        client_sock = self.connection  # 客户端 socket，用于主动探测对端是否断开
        # 流式：逐行增量解析，不缓存整段原始响应（原始字节转发后即丢，内存边转发边释放）；
        #       仅保留最终 usage 与拼接后的正文文本（体积远小于原始 SSE 字节）。
        # 非流式：仍需整段缓冲以解析单条 JSON。
        pending = bytearray()          # 流式：跨 chunk 的不完整 SSE 行
        reason_parts: list[str] = []   # 流式：增量拼接的 reasoning 片段
        content_parts: list[str] = []  # 流式：增量拼接的 content 片段
        usage_holder = [None]          # 流式：最新的 usage 对象（末尾才有完整值）
        buf = bytearray()              # 非流式：整段缓冲
        try:
            while True:
                # 主动探测客户端是否已断开：不等“下一次 write 抛错”才发现。
                # 否则在 Windows / OS 发送缓冲等情况下 write 不会立刻报错，代理会
                # 继续驱动 llama-server 生成，旧会话残留在后端占着 GPU/解码资源，
                # 导致新会话 token 速度被永久拖慢（即“中断后变慢”的现象）。
                try:
                    r, _, _ = select.select([client_sock], [], [], 0)
                    if r:
                        client_sock.setblocking(False)
                        try:
                            # MSG_PEEK：仅窥探不消费，避免误吞客户端可能发来的数据（如管线请求）
                            probe = client_sock.recv(1, socket.MSG_PEEK)
                        finally:
                            client_sock.setblocking(True)
                        if probe == b"":  # 对端已发 FIN/关闭 (EOF)
                            client_gone = True
                            break
                except OSError:
                    client_gone = True
                    break

                chunk = resp.read(8192)
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                    # 立即 flush：否则写缓冲不刷新，客户端断开时 write 不会立刻抛错，
                    # 会继续把整个响应读进 buf（长上下文下内存暴涨），且无法及时释放后端资源
                    self.wfile.flush()
                except Exception:
                    # 客户端已断开（异常中断）：立刻停止读取并关闭与后端的连接，
                    # 否则 llama-server 仍会继续生成 token，占用解码资源，
                    # 导致后续请求的 token 速度变慢（长上下文尤其明显）
                    client_gone = True
                    break
                if is_stream:
                    # 增量解析：把新到的字节并入 pending，逐行抽取 usage / 正文，
                    # 原始字节不保留，处理完即丢弃。
                    pending.extend(chunk)
                    while True:
                        nl = pending.find(b"\n")
                        if nl < 0:
                            break
                        _parse_sse_line(bytes(pending[:nl + 1]), usage_holder,
                                        reason_parts, content_parts)
                        del pending[:nl + 1]
                elif is_chat:
                    buf.extend(chunk)
        finally:
            with _active_lock:
                _active_responses.discard(resp)  # 从在途集合移除
            # 始终关闭与后端的连接，及时释放 llama-server 的生成资源
            try:
                resp.close()
            except Exception:
                pass

        if is_stream:
            # 处理末尾可能无换行的不完整行（如最后一条 usage / [DONE]）
            if pending:
                _parse_sse_line(bytes(pending), usage_holder, reason_parts, content_parts)
            # 即使客户端中途断开，只要拿到了末尾的 usage 对象，仍计入用量（避免统计被低估）
            u = norm_usage(usage_holder[0])
            if u:
                rt = "".join(reason_parts)
                ct = "".join(content_parts)
                out = ct if MAX_OUTPUT <= 0 else ct[:MAX_OUTPUT]
                rsn = rt if MAX_OUTPUT <= 0 else rt[:MAX_OUTPUT]
                save_usage(task_id, u, input_text, out or "", rsn, input_raw, has_image,
                              model, user_agent, task_src)
            del reason_parts, content_parts, usage_holder, pending
        elif is_chat:
            full = bytes(buf)
            del buf  # 尽快释放大缓冲区
            # 已完整读到后端响应即记账（无论客户端是否已断开），避免中断请求漏记 token
            u = norm_usage(extract_usage(full))
            if u:
                rt, ct = extract_output_text(full)
                save_usage(task_id, u, input_text, ct or "", rt, input_raw, has_image,
                              model, user_agent, task_src)
            del full

    # ---- 响应工具 ----
    def _send_html(self, html):
        data = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        # 页面常被浏览器缓存，导致改web.html 后仍看到旧界面；这里禁用缓存
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)

    def _send_json(self, code, obj):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format, *args):  # noqa: A002 - 必须与 BaseHTTPRequestHandler 基类签名一致
        pass

    def handle_error(self, request, client_address):  # 重写基类：静默客户端断连类异常
        exc = sys.exc_info()[1]
        # 客户端提前断开（RST / 管道破裂 / 连接中止）属正常，不打 traceback 刷屏
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError,
                            BrokenPipeError)):
            return
        # 其余异常仅打印一行摘要，避免完整 traceback 刷屏
        sys.stderr.write(f"[tracker] 请求处理异常 {client_address}: {exc!r}\n")


def _request_shutdown(signum, frame):
    """收到终止信号：立即关闭所有在途后端连接（让 llama-server 停止生成）
    与上游客户端连接（避免调用方长时间挂起），再停止监听循环，
    确保 GPU/解码资源与监听端口被及时释放。"""
    with _active_lock:
        active = list(_active_responses)
        clients = list(_active_clients)
    for r in active:
        try:
            r.close()  # 关闭与 llama-server 的连接，中止其继续生成
        except Exception:
            pass
    for c in clients:
        try:
            c.close()  # 主动断开上游调用方，使其立即收到连接关闭而非超时
        except Exception:
            pass
    srv = server
    if srv is not None:
        try:
            srv.shutdown()   # 让 serve_forever() 立即返回
        except Exception:
            pass


if __name__ == "__main__":
    print(f"token-tracker 启动: http://0.0.0.0:{LISTEN[1]}  ->  {BACKEND}")
    print(f"SQLite: {DB_PATH}")
    print(f"余额接口: GET /api/balance（额度-累计消耗），Web 页可增减额度；"
          f"当前额度 {granted_total()[0]}")
    print(f"浏览器打开 http://127.0.0.1:{LISTEN[1]}/web.html 按日期查询；客户端 base_url 改连 11435 并带 X-Task-Id/user 区分任务。Ctrl+C 退出。")

    server = ThreadingHTTPServer(LISTEN, Handler)
    # 终端终止 / 系统关停时，立即释放在途连接与监听端口
    signal.signal(signal.SIGINT, _request_shutdown)
    signal.signal(signal.SIGTERM, _request_shutdown)
    try:
        signal.signal(signal.SIGHUP, _request_shutdown)  # 终端关闭（类 Unix）
    except (AttributeError, ValueError):
        pass
    try:
        server.serve_forever()
    finally:
        server.server_close()  # 释放监听 socket
