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
- **可视化 Web**：内置 `web.html`，按日期区间查询记录、按任务汇总、查看输入输出原文
  （含深度思考 `reasoning`、原始 `messages` JSON），支持批量删除记录。
- **优雅退出**：收到终止信号时立即关闭所有在途连接与监听端口，及时释放资源。

---

## 文件结构

| 文件 | 说明 |
| --- | --- |
| `tracker.py` | 主程序：反向代理 + 用量统计 + Web 服务，单文件实现 |
| `web.html`   | 查询统计前端页面 |
| `usage.db`   | 运行后自动生成，SQLite 用量数据库（请勿手改） |

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
| `POST` | `/api/delete` | 删除记录，body 支持 `{"ids":[...]}` 或 `{"from","to","task"}` |
| 其他 | `/v1/...` | 反向代理到后端推理服务 |

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
