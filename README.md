# token-tracker

单文件、零依赖的 **LLM Token 用量统计器**。以反向代理的方式透明地夹在你的客户端与
`llama-server`（或任意兼容 OpenAI `/v1/chat/completions` 接口的服务）之间，自动记录每一次
调用的 token 消耗、缓存命中、金额，并提供可视化 Web 页面按日期区间查询与统计。

> 纯 Python 标准库实现，**无需安装任何第三方依赖**，Python 3.8+ 即可运行。

---

## 功能特性

- **反向代理**：监听 `0.0.0.0:11435` → 转发到后端 `http://localhost:11434`，对客户端完全透明，
  无需修改业务代码（仅改一下 `base_url`）。
- **流式友好**：完整支持 SSE 流式输出（`stream=true`），逐行增量解析，不缓存整段原始响应，
  内存占用极低；客户端中途断开时也会及时中止后端生成、释放 GPU/解码资源。
- **用量落库**：所有调用记录写入本地 SQLite（`usage.db`），进程重启数据不丢。
- **分时计费**：按北京时间区分「高峰 / 闲时」单价（闲时价格为高峰的一半），自动按调用时刻计价。
- **任务维度**：通过请求头 `X-Task-Id` 或请求体 `user` 字段区分不同任务，Web 页可按任务汇总统计。
- **金额双币种**：以人民币（¥）为主，并按汇率折算美元（$）展示。
- **余额管理**：额度以流水形式持久化在 SQLite（表 `ledger`），`GET /api/balance` 返回
  「剩余余额 + 累计/今日/本月消耗」；Web 页面可直接**增加 / 扣减 / 设定总额 / 撤销流水**，
  并兼容常见客户端的 billing 探测路径。
- **可视化 Web**：内置 `web.html`，按日期区间查询记录、按任务汇总、查看输入输出原文
  （含深度思考 `reasoning`、原始 `messages` JSON），支持批量删除记录。
- **优雅退出**：收到终止信号时立即关闭所有在途连接与监听端口，及时释放资源。

---

## 文件结构

| 文件 | 说明 |
| --- | --- |
| `tracker.py` | 主程序：反向代理 + 用量统计 + Web 服务，单文件实现 |
| `web.html`   | 查询统计前端页面（含余额增减） |
| `usage.db`   | 运行后自动生成，SQLite 数据库：`usage` 用量表 + `ledger` 额度流水表（请勿手改） |

---

## 快速开始

### 1. 启动后端推理服务

先在 `11434` 端口启动你的 `llama-server`（或其他 OpenAI 兼容服务）：

```bash
llama-server -m your-model.gguf --port 11434
```

### 2. 启动 token-tracker

```bash
cd token-tracker
python tracker.py
```

启动后输出：

```
token-tracker 启动: http://0.0.0.0:11435  ->  http://localhost:11434
SQLite: .../token-tracker/usage.db
浏览器打开 http://127.0.0.1:11435/web.html 按日期查询；客户端 base_url 改连 11435 并带 X-Task-Id/user 区分任务。Ctrl+C 退出。
```

### 3. 客户端接入

把客户端/SDK 的 `base_url` 从 `11434` 改为 `11435` 即可，**其余代码无需改动**。

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:11435/v1",   # 指向 token-tracker
    api_key="sk-no-key",                     # llama-server 通常不需要 key
)

# 方式一：通过请求头区分任务
client.chat.completions.create(
    model="local",
    messages=[{"role": "user", "content": "你好"}],
    extra_headers={"X-Task-Id": "my-task-1"},   # 标记任务
)

# 方式二：通过 user 字段区分任务（task_id 缺省时取 user）
client.chat.completions.create(
    model="local",
    messages=[{"role": "user", "content": "你好"}],
    user="my-task-2",
)
```

### 4. 打开统计页面

浏览器访问 <http://127.0.0.1:11435/web.html>：

- 顶部「余额」区：查看剩余余额、累计额度、消耗与使用进度，可**增加 / 扣减 / 设定总额**，
  下方「额度流水」可查看并撤销每笔额度变动；
- 顶部选择日期区间，或用「本日 / 本周 / 本月 / 本年」快捷筛选；
- 卡片区展示调用次数、输入/输出 token、缓存命中、金额合计；
- 「按任务汇总」表按任务聚合；
- 「明细记录」表支持点开查看输入/输出原文、深度思考内容、原始 `messages` JSON，
  并支持勾选后批量删除。

---

## 配置说明

所有配置均为 `tracker.py` 顶部常量，按需修改后重启生效：

| 配置 | 默认值 | 说明 |
| --- | --- | --- |
| `BACKEND` | `http://localhost:11434` | 后端推理服务地址 |
| `LISTEN` | `("0.0.0.0", 11435)` | 代理监听地址与端口 |
| `DB_PATH` | `./usage.db` | SQLite 数据库路径 |
| `PEAK_PRICING` | 见文件 | **高峰时段**单价（每 1M token，单位 ¥） |
| `OFFPEAK_PRICING` | 见文件 | **闲时时段**单价（每 1M token，单位 ¥） |
| `EXCHANGE_RATE_RMB_PER_USD` | `7.00` | 汇率，用于前端折算美元展示 |
| `INITIAL_BALANCE_RMB` | `100.00` | **初始余额额度（¥）**，仅首次启动入库；`None` 或 `<=0` 表示初始不限额 |
| `EXTRA_USED_RMB` | `0.0` | 自定义：额度之外已消耗的金额（接续旧账用） |
| `MAX_INPUT` / `MAX_OUTPUT` | `0` | 入库的输入/输出文本最大长度，`0` = 不限制（存全文） |

**分时规则**（北京时间，中国无夏令时，固定 UTC+8）：

- 高峰：工作日（周一~周五）的 `09:00–12:00`、`14:00–18:00`
- 闲时：其余时间（价格为高峰的一半）

**默认计价基准**：参考 DeepSeek 最新模型等价 API 折算价，可自行修改。
文件内已附 `DeepSeek-V4-Pro` 高峰/闲时单价注释示例，替换 `PEAK_PRICING` / `OFFPEAK_PRICING` 即可。

---

## HTTP 接口（供二次开发）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/` `/index.html` `/web.html` | 返回统计页面 |
| `GET` | `/api/stats?from=&to=&task=` | 区间汇总统计（含总计与按任务明细） |
| `GET` | `/api/records?from=&to=&task=` | 区间明细记录列表 |
| `GET` | **`/api/balance?task=`** | **余额查询（累计发放额度 − 累计消耗），`task` 可选，按任务核算** |
| `GET` | **`/api/balance/log?limit=50`** | **额度流水（倒序，含初始/增加/扣减/设定总额）** |
| `POST` | **`/api/balance`** | **增减余额：body `{"delta":50,"note":"充值"}` 增减、`{"set":200}` 直接设定总额，落库** |
| `POST` | **`/api/balance/delete`** | **撤销额度流水，body `{"ids":[...]}`，额度按剩余流水自动重算** |
| `POST` | `/api/delete` | 删除记录，body 支持 `{"ids":[...]}` 或 `{"from","to","task"}` |
| `GET` | `/balance` `/user/balance` | 余额查询别名（同 `/api/balance`） |
| `GET` | `/v1/dashboard/billing/subscription` | 余额查询（OpenAI 旧版 billing 兼容格式） |
| `GET` | `/dashboard/billing/credit_grants` | 余额查询（`credit_grants` 兼容格式） |
| 其他 | `/v1/...` | 反向代理到后端推理服务 |

---

## 余额管理（落库 + Web 增减）

余额 = **累计发放额度**（`ledger` 流水合计）− 累计消耗金额（− `EXTRA_USED_RMB`）。

额度**全部存放在 SQLite**（表 `ledger`），不再依赖任何配置文件；Web 页面顶部可直接增减。

### Web 页面操作

- **增加余额 / 扣减余额**：填写金额（可填备注）后点击按钮，立即写入流水并刷新余额卡片；
- **设定总额**：把累计额度直接改成指定值（自动换算增减额并记流水）；
- **额度流水**：列出全部额度变动（初始额度 / 增加 / 扣减 / 设定总额），点「撤销」可删除误操作记录，余额自动重算；
- 余额卡片含剩余余额、累计发放额度、累计消耗、今日/本月消耗与额度使用进度条（≥80% 变红）。

### 接口用法

```bash
# 增减额度（delta 为有符号金额）
curl -X POST http://127.0.0.1:11435/api/balance -d "{\"delta\": 50, \"note\": \"充值\"}"
curl -X POST http://127.0.0.1:11435/api/balance -d "{\"delta\": -20, \"note\": \"扣减\"}"

# 直接设定累计额度为 200
curl -X POST http://127.0.0.1:11435/api/balance -d "{\"set\": 200}"

# 撤销流水
curl -X POST http://127.0.0.1:11435/api/balance/delete -d "{\"ids\": [3]}"

# 查询余额 / 流水
curl http://127.0.0.1:11435/api/balance
curl "http://127.0.0.1:11435/api/balance/log?limit=50"
```

> 首次启动时把 `tracker.py` 的 `INITIAL_BALANCE_RMB`（默认 `100.00`）作为一条「初始额度」流水写入数据库；
> 之后以数据库为准，改常量不会覆盖已有流水。若把 `INITIAL_BALANCE_RMB` 设为 `None` 或 `<= 0`，
> 则不写入初始流水，接口返回 `unlimited: true`、`balance: null`（不限额）。

查询示例（`GET /api/balance`，可用 `?task=xxx` 按任务核算）：

```json
{
  "object": "balance",
  "balance": 96.71739,
  "currency": "CNY",
  "unlimited": false,
  "overdrawn": false,
  "granted": 100.0,
  "initial_balance": 100.0,
  "ledger_entries": 1,
  "total_used": 3.28261,
  "used_today": 0,
  "used_this_month": 0.370728,
  "extra_used": 0.0,
  "used_percent": 3.2826,
  "remaining_percent": 96.7174,
  "task": null,
  "calls": 236,
  "prompt_tokens": 4903493,
  "cached_tokens": 3975897,
  "uncached_tokens": 927596,
  "completion_tokens": 214201,
  "total_tokens": 5117694,
  "pricing": {"input_per_M": 2.0, "cached_per_M": 0.04, "output_per_M": 8.0, "rmb_per_usd": 7.0},
  "updated_at": "2026-10-06T12:10:54"
}
```

| 字段 | 说明 |
| --- | --- |
| `balance` | 剩余余额（¥）；不限额时为 `null` |
| `granted` | 累计发放额度（¥）= `ledger` 流水合计（`initial_balance` 为兼容同值字段） |
| `ledger_entries` | 额度流水条数（为 0 即不限额） |
| `total_used` / `used_today` / `used_this_month` | 累计 / 今日 / 本月消耗（¥） |
| `used_percent` / `remaining_percent` | 已用、剩余百分比 |
| `unlimited` | 是否不限额（无任何额度流水） |
| `overdrawn` | 是否已超额（`balance < 0`） |
| `extra_used` | 额度之外自定义的已用金额（接续旧账用） |
| `task` | 核算维度，`null` 表示全部任务 |

### `ledger` 表结构（额度流水）

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | INTEGER | 主键自增 |
| `ts` | TEXT | 操作时间（ISO 8601，秒级） |
| `day` | TEXT | 日期（`YYYY-MM-DD`） |
| `kind` | TEXT | `init` 初始额度 / `topup` 增加 / `deduct` 扣减 / `set` 设定总额 |
| `amount` | REAL | 有符号变动额（`+` 增加额度，`-` 扣减额度） |
| `note` | TEXT | 备注（Web 页可填） |

累计额度 = `SELECT SUM(amount) FROM ledger`，因此撤销任意流水后余额自动重算，无需改其他数据。

---

## 数据库表结构（`usage`）

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | INTEGER | 主键自增 |
| `ts` | TEXT | 调用时间（ISO 8601，秒级） |
| `day` | TEXT | 日期（`YYYY-MM-DD`），用于区间筛选 |
| `task` | TEXT | 任务标识（来自 `X-Task-Id` 或 `user`） |
| `prompt_tokens` | INTEGER | 输入 token（含缓存） |
| `cached_tokens` | INTEGER | 命中缓存的输入 token |
| `uncached_tokens` | INTEGER | 未命中的输入 token |
| `completion_tokens` | INTEGER | 输出 token |
| `total_tokens` | INTEGER | 总 token |
| `cost_rmb` | REAL | 本次费用（人民币 ¥） |
| `input_text` / `output_text` / `reasoning_text` | TEXT | 输入/输出/深度思考文本 |
| `input_raw` | TEXT | 原始请求 `messages` JSON（含图片 base64） |
| `has_image` | INTEGER | 是否含图片（0/1） |

---

## 常见问题

**Q：会让请求变慢吗？**
A：几乎无感。流式响应边转发边解析，原始字节不缓存；非流式仅整段缓冲一次以解析 JSON。
额外的开销只是一次 SQLite 写入。

**Q：客户端中途断开，会不会继续烧 token？**
A：不会。代理会主动探测客户端是否断开，并在断开时立即关闭与后端的连接，
中止 `llama-server` 继续生成，及时释放 GPU/解码资源。

**Q：换模型/换计价标准后费用不准？**
A：直接修改 `tracker.py` 顶部的 `PEAK_PRICING` / `OFFPEAK_PRICING` 即可，
按调用时刻自动套用对应单价。

**Q：支持哪些请求格式？**
A：兼容 OpenAI Chat Completions 接口，包括单轮/多轮、多模态（图片）、流式与非流式。
用量统计基于响应中的 `usage` 字段（缓存命中取自 `prompt_tokens_details.cached_tokens`）。

---

## License

Apache License 2.0 — 详见仓库根目录 `LICENSE` 文件。
