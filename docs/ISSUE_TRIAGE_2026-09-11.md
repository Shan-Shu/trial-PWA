# research_agent_issues.md 复核记录与修复进展

> 复核日期：2026-09-11
> 复核方式：**实机运行**（`.venv` Python 3.12 + 全量测试），非静态推断
> 输入清单：`research_agent_issues.md`（外部静态分析，22 条）
> 结论：**19 条成立，3 条不成立**；截至 `766d0b2`，19 条**全部修复完成**（四批，见第三节）

## 一、复核结果总表

| 编号 | 结论 | 复现证据（实机） |
|---|---|---|
| P0-1 MS-/GAP- 被判伪造引用 | ✅ 成立 | 含真实 `MS-0001`/`GAP-0001` 的 draft → `issues=[('fabrication', "引用了不存在的编号：['MS-0001','GAP-0001']")]` |
| P0-2 人工审核清空 PDF/正文 | ✅ 成立 | 带 `pdf_blob`+`clean_text` 的记录走 `make_human_review_node` → `clean_text=None pdf_blob=NULL` |
| P0-3 水位线不提交 | ✅ 成立 | `run_once()` 两次后 `meta_get('wm_test')=None` |
| P0-4 词典归并不重指超边成员 | ✅ 成立 | two-node merge → `悬空成员=[{hyperedge_id:1,node_id:2}]`，成员数仍 2、存活节点 1 |
| P0-5 `.env` 的 RA_* 对 default_settings 失效 | ✅ 成立 → **已修** | 修前：`settings.db_path=data/research_agent.db`、`threshold_flag=0.5`；修后读取 `.env` 生效 |
| P0-6 分区表未补化学期刊 | ✅ 成立 → **已修** | 修前 JACS/Angew/Org.Lett./Chem.Sci./ACS Catal. 命中数 0；修后全部 `Q1`（0.95） |
| P1-1 examples 引用已删符号 | ✅ 成立 | AST+导入冒烟扫 105 处导入，报 3 处（`compare_report.py`×2、`rebuild_ontology_320.py`×1） |
| P1-2 精修测试不被收集 | ✅ 成立 | `tests/test_extractor_refine.py` 是 7 个模块级 `def test_*`；`unittest` 收集不到 |
| P1-3 静默降级却标 `planner_mode="llm"` | ✅ 成立 | 喂非 JSON 假模型 → `status=planned planner_mode=llm` |
| P1-4 `any(... for x in [])` 恒真 | ✅ 成立 | 机制状态只要带 `activation_mode` 就无条件产出"多氮骨架"缺口 |
| P1-5 同超边多证据归到第一篇论文 | ✅ 成立 | 证据来自 `paperA`/`paperB` → `H-0007-1=paperA H-0007-2=paperA` |
| P1-6 硬门对缩写失效 / 中文主题误杀 | ✅ 成立 → **已修** | `_topic_tokens('RAG')=set()`（旧）；中文主题+英文语料 → `kept=[] dropped=[a,b]`（旧） |
| P1-7 `clean_pdf` 失败丢整条记录 | ✅ 成立 | `retrieval/node.py:67` 调用、`:98` except 在三级回退链之外 |
| P1-8 `ApiHub.search` 不隔离单源异常 | ✅ 成立 | `api_clients.py:409-422` 逐源调用无 try/except |
| P1-9 块级抽取失败记为成功 | ✅ 成立 | `knowledge/node.py` 全文无 `get("error")` |
| P2-1 中文分句/单字过滤 | ✅ 成立 | `split_sentences("第一句。第二句。第三句。")` 未切分；`extractor.py:177` 有 `len(t) < 2` |
| P2-2 参数不可经环境变量覆盖 | ✅ 成立 → 部分已修 | 本轮补了 `q_flag_penalty`/`max_extract_*`/`strong_edge_min_conf`/`http_timeout` |
| P2-3 证据位置三列恒为 NULL | ✅ 成立 | provenance 只写 `{paper, evidence}` |
| P2-4 `study_runs` 只落最终单轮 | ✅ 成立 | `graph.py` 只在结束时调一次 `save_study_run` |
| P2-5 `packs.py` 不可达兼容分支 | ✅ 成立 → **已修** | 原 `elif isinstance(extra, dict) is False` 恒假，已改为统一的载荷合并函数 |
| P2-6 版本号/文档未同步 | ⚠️ **部分成立** | 版本号 `0.4.0`、`VERSIONS.md` 无 v0.4.2 行**属实**；但"`packs/README.md` 测试命令无效"**不成立**（实测 `python -m unittest tests.test_packs` → 14 tests OK） |
| P2-7 条件表 UNIQUE 含可空列等 | ✅ 成立 | `UNIQUE(hyperedge_id, condition_key, value_text, unit)`；未接线函数清单复核无误 |

## 二、本轮已修复（3 条）

### P0-5 `.env` 的 `RA_*` 对 `default_settings` 失效
- `config.py` 顶部在求值 `settings` **之前** `load_dotenv(PROJECT_ROOT/".env")` + `load_dotenv()`；
- `.env` 损坏/缺失时只告警不抛错；
- 顺带补齐 P2-2 的一批 `RA_*` 映射（`RA_Q_FLAG_PENALTY`、`RA_MAX_EXTRACT_CHARS`、
  `RA_MAX_EXTRACT_CHUNKS`、`RA_STRONG_EDGE_MIN_CONF`、`RA_HTTP_TIMEOUT`）。
- 验收：子进程 import `research_agent.config` 后 `settings.db_path` 指向 `.env` 中的路径。

### P0-6 分区表补入化学期刊
- 新增 `examples/import_jcr_xlsx.py`：**零第三方依赖**（zip + XML 直读）导入 JCR xlsx；
- 产出内置子集 `packs/skills/journal-quartiles/content/quartiles_chemistry.json`（489 条，
  按 CHEMISTRY/CATALYSIS/MATERIALS 等学科类别过滤）；
- 产出全量表 `data/jcr/jcr_quartiles_full.json`（20337 条，`data/` 已被 gitignore）；
  `.env` 增加 `RA_JOURNAL_QUARTILES=data/jcr/jcr_quartiles_full.json`；
- `packs.normalize_journal_key()` 统一大小写/连字符/标点/副标题括号/`the`/`and`，
  使 `Angewandte Chemie (International ed. in English)` 命中
  JCR 的 `ANGEWANDTE CHEMIE-INTERNATIONAL EDITION`；
- 修掉 P2-5 的同时让 `quartiles_*.json` 支持平铺与 `{"quartiles": {...}}` 两种格式。
- 验收（新增测试）：JACS / Angew（两种写法）/ Org. Lett. / Chem. Sci. / ACS Catal.
  全部 `Q1` 且 `factor >= 0.8`。

### P1-6 相关性硬门（策略 C）
- 分词规则修正：`RAG`/`DNA`/`LLM`/`MOF` 等 2–5 字母全大写缩写现在可切出词元
  （旧规则 `[a-z]{4,}` 对纯缩写返回空集 → 门控被静默跳过）；
- 新增 `on_low_signal` 策略与配置 `RA_RELEVANCE_GATE_LOW_SIGNAL`：
  - `warn`（默认）：词元不可判定、或"中文主题 + 英文语料"时**放行并标注**
    `_gate_low_signal`，报告里给出 `low_signal` 计数与提示；
  - `drop`：保留旧的整批丢弃行为；
- 检索报告的 `relevance_gate` 现在同时给出 `considered/kept/dropped` 与低信号原因
  （旧版只报丢弃数，放行情况不可见）；
- 中文主题 + 中文语料仍按正常词元过滤（不被低信号豁免，测试覆盖）。
- 验收（新增测试 5 项）：缩写主题能绑定；中文主题+英文语料放行并标注；
  `drop` 策略保持旧行为；中文语料仍正常过滤。

## 三、四批修复（16 条，全部完成）

按用户要求"按 1、2、3、4 的顺序全修"，每批独立提交并附回归测试：

| 批次 | 提交 | 编号 | 修复结果 |
|---|---|---|---|
| 1（数据安全/阻断） | `7eb6178` | P0-1、P0-2、P0-3、P0-4 | 事实核查白名单补 `design_context` 的 MS-/GAP-/OP- 与超边 `evidence_ids`；`upsert_paper` 冲突分支改 `COALESCE(excluded.X, papers.X)`（人工复核不再清空 pdf/正文）；监控水位线先提交 + `ack()/release()`；归并后 `_repoint_hyperedge_refs` 重指成员与测量并折叠重复行 |
| 2（静默失败/逻辑） | `c6f01a5` | P1-1、P1-3、P1-4、P1-5 | 修好两个 example 的失效导入；planner 解析失败返回 `planning_failed`（仅无模型时才 `offline_fallback`）；机制"多氮"判定改为按真实规则判定；`build_citation_index` 两遍映射，证据句归到自己的论文、基础 `H-xxxx` 才回退首篇 |
| 3（可用性/测试） | `62843a7` | P1-2、P1-7、P1-8、P1-9 | 7 个精修用例收进 `TestCase`；PDF 解析失败降级（记 `pdf_parse_error` 并继续 XML/摘要）；`ApiHub.search` 逐源 try/except 写 `source_errors`；块级抽取失败计数并在全失败时返回 `extract_failed` |
| 4（适配/文档） | `766d0b2` | P2-1、P2-3、P2-4、P2-6、P2-7 | 中文标点无空白分句 + 长段落二次切分 + 截断统计；证据字符区间入 provenance；`round_snapshot` 节点与 `on_round` 逐轮落库；版本统一 0.4.4 并补 `VERSIONS.md`；条件/测量 NULL 安全去重 + 索引补全 + `list_hyperedges` 去 N+1 |

## 四、测试

```
Ran 169 tests in 10.7s
OK      # 90 → 101 → 115 → 121 → 132 → 140 → 153 → 169
```

新增测试文件：`tests/test_issue_batch1.py`（11）、`tests/test_issue_batch2.py`（8）、
`tests/test_issue_batch3.py`（6）、`tests/test_issue_batch4.py`（16）。

## 五、实机验收（v0.3.1 生产库副本）

P2-7 触及既有库结构，因此在一份 v0.3.1 生产库**副本**（`research_agent.db`，803 条超边/
304 条条件/244 条测量）上跑真实迁移与查询：

| 项目 | 结果 |
|---|---|
| 迁移去重 | 条件重复 0 条；测量重复 1 条被清理（NULL `subject_node` 的旧重复行） |
| 索引 | 新增 `uq_hyperedge_conditions_nullsafe`、`uq_hyperedge_measurements_nullsafe`、`idx_hyperedges_created` |
| `list_hyperedges(limit=50)` 实际 SELECT 次数 | **201 → 6**（N+1 消除，次数与条数无关） |

脚本：`.dsh-tmp/check_migration.py`（临时验证脚本，未纳入版本库）。
