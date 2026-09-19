# 统一事件日志（`research_agent.logging`）

> 目标：**一次复现就能判断断在哪一段**，不必再靠临时 `print` 重跑。
> 落地形态：一行一条 JSON 的 `data/logs/dsh.jsonl` + 现有 `processing_log` 表
> + 进程内环形缓冲。

## 为什么需要它

写作台的"点了没反应"以前只能靠加日志再复现；有了固定事件名 + trace 之后：

| 现象 | 结论 |
| --- | --- |
| 有 `ui.click`，无 `http.req` | **前端**：点击未绑定 / 被遮挡 / 请求未发出 |
| 两者都有，状态没变 | **后端**：状态机 / 幂等 / 校验 |
| 有 `dispatch.step.start`，无对应 `.end` | 卡在该**步骤**（再看同一 trace 的最后一条事件） |
| 有 `llm.call.start`，无 `.end`/`.error` | 卡在**模型**（超时或长输出） |
| 有 `collect.round` 但 `added=0` | 卡在**检索**（查库为空 / 源站限流） |

## 三个落点，各司其职

1. **`data/logs/dsh.jsonl`** —— 实时 tail / grep 用。不进数据库，写失败绝不影响业务
   （所有写入都包了 `try/except`，坏日志不会拖垮请求）。
2. **`processing_log` 表** —— 结构化查询与界面展示。**只镜像"里程碑"事件**
   （`MILESTONE_EVENTS`），避免写放大：每个模型调用都往 SQLite 写一行会明显拖慢链路。
3. **环形缓冲（最近 500 条）** —— 作业失败时用 `recent_for_trace()` 自动附上
   同一 trace 的最近若干条，**失败自带现场**，不必重新复现。

## 事件词表（受控）

事件名写死在 `EVENTS` 里，并有测试守着——避免日志里出现一次性名字。

| 族 | 事件 | 说明 |
| --- | --- | --- |
| 前后端 | `http.req` / `http.res` / `ui.click` / `ui.state` | 请求与交互 |
| 访谈 | `interview.question` / `.answer.received` / `.answer.rejected` / `.step` | 唯一的交互节点 |
| 派工 | `dispatch.created` / `.step.start` / `.step.end` / `.step.skipped` / `.done` | 规划节点下单 |
| 模型与检索 | `llm.call.start` / `.end` / `.error`、`collect.round`、`sufficiency.judge` | 耗时与规模 |
| 节点 | `study.node.enter` / `.exit` / `.skipped` | 研究流程页 |
| 作业 | `job.start` / `job.end` | 作业边界 |

## trace：一条链路一个 id

- HTTP 中间件给每个 `/api/*` 请求生成 trace，写进 `contextvars`，
  并在响应头返回 **`X-Trace-Id`**；
- 作业线程是**独立上下文**，因此 `LibraryJobManager.start(..., trace=…)`
  显式传递，`job.start`/`job.end` 与发起它的点击串在同一条链上；
- 用 `with bind_trace(t): …` 可以手工成链。

## 怎么用

```bash
# 实时跟随（先回放 50 条，再跟随新增）
uv run research-agent-logs --last 50

# 只看派工
uv run research-agent-logs --evt dispatch,interview --last 100

# 只看出问题的（含模型失败现场）
uv run research-agent-logs --level WARN --last 200

# 追一条链路
uv run research-agent-logs --trace t-1a2b3c4d --last 200

# 只回放不跟随（脚本/CI 用）
uv run research-agent-logs --evt llm --last 20 --no-follow
```

服务端默认**不**往 stderr 重复打印；需要边跑边看时设 `RA_LOG_STDERR=1`。
`RA_LOG_LEVEL=WARN` 可降噪，`RA_LOG_FILE` 可换文件。

按 `evt` 过滤是**前缀匹配**：`--evt llm` 命中 `llm.call.*` 全部。

## 脱敏与隐私（硬要求）

- 密钥形状的串（`sk-…`、`ghp_…`、`AKIA…`）一律替换为 `***`；
- 键名含 `api_key` / `token` / `password` / `secret` / `authorization` 的值直接打码；
- 单字段超过 500 字符截断，保留头尾并标注原始长度；
- **模型提示词与返回正文不落盘**，只记 `prompt_chars` / `output_chars`。

## 模型调用的透明记账

`build_role_model()` 的返回值被套上 `LoggedModel` 代理：**所有** `invoke`
自动记 `llm.call.start / .end / .error`（谁在调、多慢、多大、失败原因），
调用点一行都不用改；`bind_tools` / `with_config` 等管理操作与其余属性透传，
对 LangChain/LangGraph 透明。

节点名解析顺序：**显式指定 → 调用点文件推断（`NODE_HINTS`）→ 角色映射
（`ROLE_NODE`）**，因此日志里 `node` 字段不会是空的。

`study/model_call.invoke_with_timeout()` 额外带硬超时，超时也落 `.error`
（`timed_out=true`）——"节点卡在模型上"与"卡在检索上"一眼可辨。

## 相关文件

- `src/research_agent/logging/__init__.py` —— 事件、脱敏、环形缓冲、tail/CLI
- `src/research_agent/logging/proxy.py` —— `LoggedModel` 透明代理与节点名解析
- `tests/test_logging.py` —— 事件词表、脱敏、trace、代理四组测试
