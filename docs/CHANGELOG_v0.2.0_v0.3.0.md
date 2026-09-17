# 更新日志：v0.2.0 → v0.3.0

> 版本主题：双维度审核、科研超边本体、域/通道可视化

## 一、审核节点

- 审核一级维度收敛为：用户指令符合度 80%、证据与引用正确性 20%。
- 增加独立正确性最低阈值，默认 0.85；低于阈值不得通过。
- 创新、归纳、总结、证明、格式、数量等要求全部作为指令符合度的检查项。
- Planner 新增 `instruction_contract`，Reviewer 同时读取用户原话和契约。
- 审核结果输出分项要求、证据检查和可执行 revision_actions。

## 二、科研超边本体

- 新增 `ontology_hyperedges` 及成员角色、条件、测量、证据、超边链接表。
- 科研事件、实验和多角色关系由超边承载，不创建 Reaction/Event 额外节点。
- 支持 substrate/catalyst/product、intervention/outcome、actor/period 等跨学科角色。
- 条件以键值记录，测量值以 metric/value/unit 记录，原文位置进入 evidence 表。
- 旧 relation/event 可通过兼容适配器投影为超边。

## 三、域与通道

- 新增节点域、域成员、关系通道、通道超边聚合表。
- 节点域支持重叠成员关系；通道支持多角色、多分支。
- 节点域不再按 `node_type` 直接聚合，而采用关键词语义规则与 `is_a/part_of` 层次传播；例如 N-allyl-ynamide 进入炔酰胺域，Benzimidazole 进入氮杂环域。
- 增加论文局部编号节点清理：Compound 32 等无语义编号会被删除，有规范别名的节点会自动改名并保留原名作为别名。
- 增加 `rebuild_ontology_views()`，自动生成语义节点域和关系通道统计。

## 四、可视化看板

- 图谱接口返回 hyperedges、domains、channels。
- 总览增加科研超边、域、通道统计。
- 图形页增加节点域、关系通道和超边摘要面板。
- 点击节点域可直接筛选并加载对应节点。

## 五、验证

- 新增 4:1 审核权重、正确性硬门槛、超边角色/条件/测量保存测试。
- 全量测试通过：67 passed。
