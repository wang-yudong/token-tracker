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
- **会话识别（任务分组）**：客户端（dsh / CodeBuddy 等）普遍**没有可配置的自定义请求头**，
  因此任务名按优先级自动推导：`X-Task-Id` 头 → 请求体 `user` / `metadata.user_id` →
  **会话指纹**（首条消息哈希，多轮对话自动归并）→ `default`；`GET /api/trace` 可查看客户端实际发了什么。
- **金额双币种**：以人民币（¥）为主，并按汇率折算美元（$）展示。
- **余额管理**：额度以流水形式持久化在 SQLite（表 `ledger`），`GET /api/balance` 返回
  「剩余余额 + 累计/今日/本月消耗」；Web 页顶部「额度管理」弹窗里以卡片展示并可**增加 / 扣减 / 设定总额 / 撤销流水**，
  同时兼容常见客户端的 billing 探测路径。
- **可视化 Web**：内置 `web.html`，按日期区间查询记录、按任务汇总、查看输入输出原文
  （含深度思考 `reasoning`、原始 `messages` JSON），支持批量删除记录。
- **优雅退出**：收到终止信号时立即关闭所有在途连接与监听端口，及时释放资源。

---

## 界面截图

| 主界面：查询条件 + 额度卡片 + 任务汇总 + 明细记录 | 输出内容：深度思考 / 正文分段 |
| --- | --- |
| ![主界面](images/1.png) | ![输出内容](images/2.png) |

| 输入内容：可读输入 + 原始 messages | 额度管理：额度卡 / 增减 / 流水 |
| --- | --- |
| ![输入内容](images/3.png) | ![额度管理](images/4.png) |

> 依次为：主界面（查询条件区、额度卡片、按任务汇总、明细记录）、输出弹窗（深度思考 reasoning 与正文 content 分段）、
> 输入弹窗（按角色转写的可读输入 + 原始 `messages` JSON）、额度管理弹窗（额度卡片、增减额度、额度流水）。

---

## 文件结构

| 文件 | 说明 |
| --- | --- |
| `tracker.py` | 主程序：反向代理 + 用量统计 + Web 服务，单文件实现 |
| `web.html`   | 查询统计前端页面（含余额增减） |
| `usage.db`   | 运行后自动生成，SQLite 数据库：`usage` 用量表 + `ledger` 额度流水表（请勿手改） |
| `images/`    | README 里的界面截图 |

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

- 顶部为**查询条件区**（吸顶浮动）：左侧「日期范围 + 本日/本周/本月/本年 + 删除本区间 + 任务 + 内容」，右侧「搜索 / 重置 / 额度管理」与状态文字；
  「任务」搜索框、「清除任务」以及「**额度管理**」按钮；
- 卡片区分两行并按数量自适应铺满整行：第一行是截止日期维度（截止日期剩余额度 /
  截止日期累计发放额度 / 截止日期累计消耗额度，只看「至」日期，与起始日期无关），
  第二行是用量维度（调用次数 / 输入 token / 命中缓存 / 未命中 / 输出 token / 消耗额度）；
- 所有金额卡同时显示两种单位：`¥xxx / $xxx`（按 EXCHANGE_RATE_RMB_PER_USD 换算，默认 7.00）；
  点「查询」会同时刷新用量与额度；
- 点「额度管理」弹出小窗：顶部 4 张额度卡（剩余额度 / 累计发放额度 / 累计消耗 / 已用）+
  **增加 / 扣减 / 设定总额 / 备注** + 可滚动的「额度流水」
  （可撤销误操作，`Esc` 或点遮罩关闭）；
- 卡片区展示调用次数、输入/输出 token、缓存命中、金额合计；
- **任务筛选**：工具栏「任务」输入框支持任务名**模糊搜索**（回车或点「查询」生效，下拉会给出候选任务及调用次数）；
  另有「内容」输入框对**输入内容**做模糊搜索（`input_like`，同样作用于卡片/汇总/明细）。
  点「按任务汇总」表里任意一行可切换为**精确匹配**该任务；点「清除任务」清空任务与内容两个条件。
  筛选条件同时作用于卡片、按任务汇总与明细记录，并在状态栏回显当前是精确还是模糊匹配；
- 「明细记录」表的「输出内容」列在单元格内上下分块显示：**深度思考 reasoning**（紫色块）与
  **正文 content**（青色块），与弹窗内分段样式一致；整个单元格（含标签与两个块）点击行为一致，
  点开即看到思考 + 正文两段；
- 「明细记录」表还支持点开查看输入原文与原始 `messages` JSON，并支持勾选后批量删除；
- **分页**（表格右下角）：「按任务汇总」与「明细记录」两张表各自独立分页条——
  汇总为「共 N 个任务 / 每页 10·20·50·100 / 首页·上一页·页码·下一页·末页 / 跳至 __ 页」，
  明细为「共 N 条 / 每页 20·50·100·200 / …」；翻页只重查对应表，不重复请求统计与余额，
  改动查询条件或删除记录后自动回到第 1 页。

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
| `GET` | `/api/stats?from=&to=&task=&task_like=&input_like=` | 区间汇总统计；`task` 精确匹配任务、`task_like` 任务名模糊、`input_like` 输入内容模糊 |
| `GET` | `/api/records?from=&to=&task=&task_like=&input_like=` | 区间明细记录列表（含 `model`、`client`、`task_src`） |
| `GET` | `/api/records?...&page=1&size=20` | **分页查询**，返回 `{total, page, size, pages, items}`；不带 `page` 时仍返回全量列表（兼容旧调用） |
| `GET` | `/api/tasks?from=&to=&task_like=&kw=&input_like=&limit=` | 任务清单（任务名 + 调用次数 / token / 金额），供搜索框下拉建议 |
| `GET` | `/api/tasks?...&page=1&size=10` | **按任务汇总的分页查询**，返回 `{total, page, size, pages, items}` |
| `GET` | `/api/trace?limit=20` | 会话识别诊断：最近 chat 请求实际携带的标识（敏感头脱敏） |
| `GET` | **`/api/balance?task=&to=`** | **余额查询（累计发放额度 − 累计消耗）；`to` 额外返回截止该日的额度/余额快照** |
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

额度**全部存放在 SQLite**（表 `ledger`），不再依赖任何配置文件；Web 页面顶部「额度管理」弹窗内可直接增减。

### Web 页面操作

- 顶部工具栏吸顶浮动，「额度管理」按钮（不含金额，金额统一在弹窗内查看）；
- 主界面卡片分两行：第一行是截止日期维度的额度卡（**截止日期剩余额度** / **截止日期累计发放额度** /
  **截止日期累计消耗额度**），第二行是用量卡（调用次数 / token / 消耗额度等）；
  额度卡只按查询区「至」日期做截止日快照（额度与消耗都从最早记录算到该日，**与起始日期无关**）；
- 点「额度管理」打开小弹窗（`Esc` 或点击遮罩关闭），弹窗内为 2×2 的额度卡片（剩余额度 / 累计发放额度 / 累计消耗 / 已用百分比，均带 ¥ 与 $，「已用」卡内含使用进度条，≥80% 变红）+ 增减操作与流水；
- **增加余额 / 扣减余额**：填写金额（可填备注）后点击按钮，立即写入流水并刷新；
- **设定总额**：把累计额度直接改成指定值（自动换算增减额并记流水）；
- **额度流水**：列出最近 50 条额度变动（初始额度 / 增加 / 扣减 / 设定总额），点「撤销」可删除误操作记录，余额自动重算；
- 弹窗内点「刷新」或点主界面的「查询」都会重新拉取余额与流水。

### 接口用法

```bash
# 增减额度（delta 为有符号金额）
curl -X POST http://127.0.0.1:11435/api/balance -d "{\"delta\": 50, \"note\": \"充值\"}"
curl -X POST http://127.0.0.1:11435/api/balance -d "{\"delta\": -20, \"note\": \"扣减\"}"

# 直接设定累计额度为 200
curl -X POST http://127.0.0.1:11435/api/balance -d "{\"set\": 200}"

# 撤销流水
curl -X POST http://127.0.0.1:11435/api/balance/delete -d "{\"ids\": [3]}"

# 查询余额 / 流水（to 可选，给出截止该日的额度快照）
curl http://127.0.0.1:11435/api/balance
curl "http://127.0.0.1:11435/api/balance?to=2026-10-08"
curl "http://127.0.0.1:11435/api/balance/log?limit=50"
```

> 首次启动时把 `tracker.py` 的 `INITIAL_BALANCE_RMB`（默认 `100.00`）作为一条「初始额度」流水写入数据库；
> 之后以数据库为准，改常量不会覆盖已有流水。若把 `INITIAL_BALANCE_RMB` 设为 `None` 或 `<= 0`，
> 则不写入初始流水，接口返回 `unlimited: true`、`balance: null`（不限额）。

查询示例（`GET /api/balance?to=2026-10-08`，`task` 可选按任务核算）：

```json
{
  "object": "balance",
  "balance": 96.71739,
  "currency": "CNY",
  "unlimited": false,
  "overdrawn": false,
  "granted": 100.0,
  "initial_balance": 100.0,
  "as_of": "2026-10-08",
  "balance_as_of": 95.4,
  "granted_as_of": 100.0,
  "used_as_of": 4.6,
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
  "updated_at": "2026-10-08T09:00:00"
}
```

| 字段 | 说明 |
| --- | --- |
| `balance` | 剩余额度（¥）；不限额时为 `null` |
| `granted` | 累计发放额度（¥）= `ledger` 流水合计（`initial_balance` 为兼容同值字段） |
| `as_of` | 本次快照的截止日期（未传 `to` 时为 `null`） |
| `balance_as_of` / `granted_as_of` / `used_as_of` | **截止日期**口径：额度与消耗都从最早记录累算到该日（含），即「当时还剩多少额度 / 累计消耗多少」；与起始日期 `from` 无关 |
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
| `task` | TEXT | 任务标识（`X-Task-Id` / `user` / 会话指纹，见下节） |
| `prompt_tokens` | INTEGER | 输入 token（含缓存） |
| `cached_tokens` | INTEGER | 命中缓存的输入 token |
| `uncached_tokens` | INTEGER | 未命中的输入 token |
| `completion_tokens` | INTEGER | 输出 token |
| `total_tokens` | INTEGER | 总 token |
| `cost_rmb` | REAL | 本次费用（人民币 ¥） |
| `input_text` / `output_text` / `reasoning_text` | TEXT | 输入/输出/深度思考文本 |
| `input_raw` | TEXT | 原始请求 `messages` JSON（含图片 base64） |
| `has_image` | INTEGER | 是否含图片（0/1） |
| `model` | TEXT | 请求里的模型名（区分 dsh / CodeBuddy 等走的不同后端） |
| `user_agent` | TEXT | 客户端 User-Agent（区分是哪个客户端发起的） |
| `task_src` | TEXT | 任务名来源：`header` / `body:user` / `body:metadata.user_id` / `fingerprint` / `default` |

---

## 会话识别（任务分组依据）

### 为什么需要它

实测结论：**dsh 与 CodeBuddy 都没有可配置的自定义请求头**——

- dsh：`llm-pi-ai` provider 只有 `apiKeyEnv` / `api` / `baseURL` / `models`；
- CodeBuddy：自定义模型只有 `url` / `apiKey` / `maxInputTokens`，无 headers 字段。

所以早期版本里所有记录都落到 `default`，「按任务汇总」只有一行。

### 任务名推导优先级

| 优先级 | 来源 | 示例 |
| --- | --- | --- |
| 1 | 请求头 `X-Task-Id` | `codebuddy-abc` |
| 2 | 请求体 `user` / `metadata.user_id` | `alice` |
| 3 | **会话指纹**（首条消息） | `fp-07469084-帮我实现额度管理` |
| 4 | 兜底 | `default` |

会话指纹的原理：客户端每轮都会**重发完整历史**，因此同一会话的「首条消息」在会话生命周期内保持不变；
取首条 user（无则 system/assistant）消息规范化后做 SHA-1 前 8 位，并保留前 20 个字符便于肉眼识别。
这样即使客户端完全不提供会话 id，多轮对话也能自动归并到同一任务；不同话题会得到不同指纹。

> 若客户端把模型名塞进 `user`（CodeBuddy 可能如此），该值会被识别为无效并回退到指纹，避免出现「按模型分组」。

### 确认客户端到底发了什么

```bash
curl http://127.0.0.1:11435/api/trace?limit=20
```

返回最近 N 条 chat 请求的诊断信息：命中的任务名与来源、`model`、`User-Agent`、`client` IP、
body 顶层字段名、`user` / `metadata` 取值、首条消息片段，以及全部请求头（`authorization`
等敏感头以 `***` 脱敏，其余仅显示字节数）。也可设环境变量 `TRACKER_TRACE=1` 把同样的信息打到控制台。

### 相关配置

| 配置 | 默认值 | 说明 |
| --- | --- | --- |
| `USE_TASK_HEADER` / `TASK_HEADER_NAME` | `True` / `X-Task-Id` | 是否启用请求头识别及头名 |
| `USE_TASK_BODY` / `TASK_BODY_FIELDS` | `True` / `("user","metadata.user_id")` | 请求体候选字段（支持 `a.b` 嵌套） |
| `TASK_BODY_SKIP_IF_MODEL` | `True` | `user` 等于模型名时视为无效 |
| `USE_TASK_FINGERPRINT` | `True` | 是否启用会话指纹兜底 |
| `TASK_FINGERPRINT_PREFIX` / `TASK_FINGERPRINT_MAX` | `20` / `400` | 指纹保留的可读前缀长度 / 参与哈希的最大字符数 |
| `TRACE_SIZE` / `TRACKER_TRACE` | `200` / 空 | 诊断缓冲条数；设为 `1` 时同步打印到控制台 |

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
