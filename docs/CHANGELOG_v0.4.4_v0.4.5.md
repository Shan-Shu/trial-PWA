# v0.4.4 → v0.4.5：派工、统一日志、研究流程页

这次改动的主线是**把"工作规划节点"从名词变成动词**：它能看的节点清单来自
一张登记表，它能对任何一个接了模型的节点下单，下单过程逐步可查；同时补上一套
统一事件日志，让"点了没反应"这类问题一次复现就能定位。

## 一、节点登记表（`writing/node_registry.py`）

**问题**：接了模型的节点有 9 个，但能"被派工"的只有检索与抽取两条硬编码通道；
其余节点（质量、消费、成段、审核、核查）没有任何发令口，用户只能逐页手点。

**做法**：一张**只描述不实现**的登记表。`TaskSpec.entry` 一律指向**既有函数**，
不引入新的执行逻辑。每个任务写清四件事，缺一个就无法派工：

| 字段 | 回答 |
|---|---|
| `accepts` | 需要什么参数 |
| `produces` | 产出什么（后续任务靠它串联） |
| `cost` / `typical_seconds` | 成本与耗时量级（界面提示、预算、超时） |
| `needs_model` | 是否依赖大模型 |

13 个节点、12 个任务，全部有实现。其中 `assess_quality` 是**假实现**被修掉的
一例：它原来只数了数 `quality_results` 里已有几行就返回成功，派工等于没派——
现在真的逐篇调质量控制节点并落库。

## 二、派工协议（`writing/dispatch.py`）

```
gap_decision / user_direct / interview
        │
        ▼
   work_plan（步骤 + 预算）──► run_dispatch ──► dispatch_runs（逐步回报）
                                   │
                        $step0.paper_keys 引用串联
```

四条必需性质：

- **幂等**：`dispatch_id` 已存在且非 failed 时直接返回既有结果；失败的单可重跑；
- **可取消**：每步开始前、引用展开后都查 `cancel_event`；
- **收敛**：轮数上限 + 连续零新增即停，另有 `budget.max_seconds` 兜总时长；
- **可回报**：每步的产出 / 错误 / 跳过原因都写进 `steps`。

**步骤间引用**（`$step0.paper_keys`）是"检索 → 抽取"能自动串成一条链、
而不是两步各干各的关键。引用不到值时**跳过并说明**，绝不传空值硬跑。

取消与进度经 `contextvars` 传递，既有的执行函数（`process_papers`、
`make_quality_node` …）签名不必被污染。

## 三、统一事件日志（`research_agent/logging/`）

```bash
uv run research-agent-logs --last 50            # 实时跟随
uv run research-agent-logs --evt dispatch,llm   # 只看派工与模型
uv run research-agent-logs --level WARN         # 只看出问题的
uv run research-agent-logs --trace t-1a2b3c4d   # 追一条链路
```

一次复现就能判断断在哪一段：

| 现象 | 结论 |
|---|---|
| 有 `ui.click`，无 `http.req` | **前端**：点击未绑定 / 被遮挡 |
| 两者都有，状态没变 | **后端**：状态机 / 幂等 / 校验 |
| 有 `dispatch.step.start`，无 `.end` | 卡在该步骤 |
| 有 `llm.call.start`，无 `.end`/`.error` | 卡在**模型** |
| 有 `collect.round` 但 `added=0` | 卡在**检索** |

三个落点：`data/logs/dsh.jsonl`（实时 tail）、`processing_log`（**只镜像里程碑
事件**，避免写放大）、进程内环形缓冲（失败时自动附上同 trace 的现场）。

**脱敏是硬要求**：密钥形状替换、键名敏感值打码、单字段超 500 字符截断、
**模型提示词与返回正文只记字符数不落盘**。

`build_role_model()` 的返回值套上 `LoggedModel` 代理，因此**所有** `invoke`
自动记账（谁在调 / 多慢 / 多大 / 失败原因），调用点一行未改。

HTTP 中间件给每个 `/api` 请求建 trace 并回写 `X-Trace-Id`；前端沿用同一 trace，
于是"点击 → 请求 → 作业 → 模型"落在同一条链上。

## 四、研究流程页

**三个病根**：

1. 节点清单写死在 `dashboard/api.py` 里，只有 10 个——写作台的访谈节点不在其中；
2. 状态是"**有事件就算 done**"推出来的，于是研究流程页对绝大多数节点显示
   "已完成"，包括从未真正执行过的；
3. 界面上看不到"谁在给谁下单"。

**改法**：`nodes_overview()` 改为登记表驱动（新增节点自动出现），状态语义
明确（`idle`/`running`/`done`/`partial`/`failed`/`stale`/`waiting`，
**"未执行"是诚实的默认值**），并带上「当前派工」看板与每步回报。

## 五、新增接口

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/api/registry/nodes` | 登记表：能派什么工 |
| GET/POST | `/api/dispatches` | 派工单列表 / 下单 |
| GET | `/api/dispatches/{id}` | 单张派工单的逐步回报 |
| POST | `/api/log` | 前端诊断事件（`ui.click`/`ui.state`，白名单） |

## 六、验证

| 层 | 项数 | 结果 |
|---|---|---|
| Python 单元测试 | 378 | 全绿 |
| 前端模块加载与页面契约 | 47 | 全绿 |
| 前端接口 ↔ 后端路由配对 | 54 | 全绿 |
| 端到端 HTTP 冒烟 | 139 | 全绿 |
| 浏览器交互冒烟（Playwright + Edge） | 33 | 全绿 |

新增的浏览器检查覆盖了研究流程页：访谈节点在清单里、状态含诚实的「未执行」、
派工板存在、分组筛选与节点详情在重渲染后仍可点。

## 七、已知未做

- **把用户自然语言指令解析成派工单**（方案 v8 §A3 的触发源③）：执行器与接口
  已就绪，尚缺"自然语言 → plan"的解析入口 + 3 方案交互；
- `research_article` 体裁仍无固定模板（5 个体裁中唯一缺的）；
- 检索源限流与查询构造：OpenAlex 对引号+通配查询会返回 `400`，
  Semantic Scholar 会 `429`，目前只是记录错误、没有退避重试；
- 「作答」与「推进」仍是两次请求，中间存在一个用户可见的状态窗口。
