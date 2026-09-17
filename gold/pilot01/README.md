# 金标准试点语料包 pilot01

> 用途：金标准构建第 1 批（3 篇，跑通双人标注→对齐→仲裁流程）

## 文件清单

| 编号 | 文件 | 主题 | 类型 | 文本长度 |
|---|---|---|---|---|
| 01-A | `pilot01_A_*.txt` | An agent-guided peptide hydrogel bio-stabilizer clamps pericellular viscoelastic drift. | 材料 + 体外(in vitro) | 1234 字符 |
| 01-B | `pilot01_B_*.txt` | Biofunctional carboxymethyl chitosan hydrogels with nano-hydroxyapatite gradients accelerate bone defect healing. | 材料 + 动物模型(in vivo) | 977 字符 |
| 01-C | `pilot01_C_*.txt` | Tooth-derived graft for preservation and reconstruction of orofacial bones: A comprehensive review. | 综述(review，验证降档规则) | 1060 字符 |

## 工作流速览
1. A/B 各自独立标注 3 篇（JSON，与模型输出同构）；
2. 对齐→差异四分类→Owner 仲裁；
3. 抽检 evidence 可溯源后锁定；
4. 回填 docs/gold_standard_guide.md 第 13 节指标。

## 提醒
- 语料来自 v0.0.6 同语料 50 篇（摘要级）；对应 v0.0.6 抽取结果在 data/ontology_v06.db，可用于后续 P/R/F1 对齐。
- 本批为摘要级试点；全文级（含方法学）语料待修复 research_agent.db 的 PDF/元数据错位后另行制作。