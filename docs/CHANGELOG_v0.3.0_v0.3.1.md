# 更新日志：v0.3.0 → v0.3.1

> 版本主题：语义节点域修正、论文局部编号清理和可视化域筛选。

## 一、语义节点域修正

- 节点域不再按 `node_type` 直接聚合，避免所有 Chemical 被并入同一域。
- 采用跨学科关键词语义规则与 `is_a/part_of` 层次传播生成节点域。
- 例如：
  - N-allyl-ynamide 进入 `ynamides` 域；
  - Benzimidazole 进入 `nitrogen heterocycles` 域。
- 节点支持属于多个语义域；域仍是聚合视图，不创建额外节点。

## 二、论文局部编号清理

- 扩展局部编号识别规则，支持 `Compound 4aa`、`compound 10aa`、`Compound 15j`、`Compound 11B` 等形式。
- 无正式名称和有效别名的编号节点会被删除。
- 存在正式名称或描述性别名的节点会自动改名，原编号不再保留为别名。
- 带前缀的内部编号也会清理，例如 `DOTA-conjugated compound 11B` 改为 `DOTA-conjugated compound`。
- 节点删除后产生的空成员超边会被一并删除。
- 新抽取流程在写入节点前执行相同过滤。

## 三、可视化更新

- 图谱接口支持按语义域 `domain_key` 筛选。
- 点击节点域会加载该语义域内的节点，而不是按 `node_type` 加载。
- 结构面板增加语义域、关系通道和超边摘要。

## 四、迁移结果

- `Compound 4aa`、`Compound 32`、`compound 11B` 等节点和别名已清除。
- 通用化学类别节点如 `Phenolic compounds`、`Azo compounds` 会保留，因为它们不是论文局部编号。
- 当前深挖库：2,821 个节点、3,012 条二边、3,851 条超边、260 个语义域、289 条关系通道。

## 五、验证

- 全量测试通过：68 passed。
