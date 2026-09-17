# 更新日志：v0.3.1 → v0.4.0

> 版本主题：Planner-first 编排、Planner 契约升级、LLM 知识消费节点。

## 一、Planner-first 主链路

- 完整研究任务统一从 `research-agent-study` 入口启动。
- 运行顺序调整为 Planner → collection → Consumer → Content → Reviewer。
- `collection` 显式执行 Planner 的 `retrieval_plan`，内部调用 retrieval → quality → knowledge。
- `research-agent-pipeline` 保留为数据构建工具，不再作为完整研究任务入口。
- Dashboard `/api/study/run` 默认执行完整 Planner-first 流程。

## 二、Planner 升级

新增并规范：

- `retrieval_plan`：查询词、来源、维度、时间范围、覆盖目标和停止条件；
- `analysis_plan`：分析维度、必要比较和开放问题；
- `evidence_policy`：可追溯性、假设标记、最低支持和补检策略；
- `design_contract`：目标结构、创新等级下限、候选数量、差异轴和评价标准；
- `stop_conditions` 与 `budget`。

旧 `creative_contract` 保留兼容，但降级为底层实现提示。

## 三、LLM 驱动知识消费

- 新增 `consumer` 模型角色。
- Consumer 从 LLM 获取机制理解、冲突识别、证据缺口、补检请求和 `design_context`。
- 数据库查询、证据编号、引用过滤和 schema 校验仍由代码完成。
- 无效 pattern/evidence/hyperedge 引用会被过滤并记录。
- 配置了模型但调用失败时返回 `consumer_failed`，不静默回退。
- 仅显式无模型或离线测试模式使用 `offline_fallback`。

## 四、模型绑定

Planner、Consumer、Content、Reviewer 统一使用：

```text
provider = deepseek
model = deepseek-v4-pro
api key = DEEPSEEK_API_KEY
```

CLI 与 Dashboard 均实际注入上述四个模型。

## 五、验证

- Planner-first collection、LLM Consumer 输出与引用过滤新增离线测试。
- 全量 unittest：70 tests passed。
