# Versions

research-agent（LangGraph 多模型科研辅助 Agent）本地提交版本索引。

| 版本 | Commit | 说明 |
|---|---|---|
| v0.0.1 | 4935125b5d56c9fdb101c440992d8d05cdbaa74c | 初始快照（旧检索提示词基线） |
| v0.0.2 | 5ca0a137db6a80763b3d11556d3ccbcf5bd2c346 | 检索/质量/知识提示词升级 + 关系同义归一 |
| v0.0.3 | 0399042fd15908b518dc93b9eb919a1800e85465 | 防衔接语实体过滤 + 关系词表细粒度化 + 配方细节保留 |
| v0.0.4 | 69950303510cf3ccf139936c9678bb7fd7cfdf0a | 事件节点名词化 + involves 克制 + 四版本对比产物 |

| v0.0.5 | a8d92f9125a832900e7b8b7884197b3a55d40328 | 事件旁路化(event_assertions)、实体双轨身份+材料登记(lcmat)、证据等级/scope、细粒度关系、强断言置信门控、合并/方向队列 |
| v0.0.6 | cafb20c0a4e2ea877efa44725fdfa9d70aa81695 | 三开源学习报告(docs/open_source_agents_learning_report.md)、知识提取硬性禁区清单(ERROR_LIST)、低置信/泛化关系定向精修(extract_with_refine)、语料兜底关系提醒、skills 同步、精修开关/阈值(RA_KNOWLEDGE_REFINE 等) |
| v0.0.6.1 | 7009c5033faf56c5a771ab6ad8f2b75a1460607a | 提速补丁：并行批量提取脚本(examples/run_knowledge_batch.py，多 worker/WAL/预建连接/逐篇进度)，知识节点 run_init 开关消除并发建表锁；pro 2 篇并行 268s≈串行一半 |

> 注：v0.0.5 标签随后续“属性质量优化(模板字段/去空值/snake_case/value+unit/扫描器)”前移至最新提交，精确指针以 git tag 为准。
> 注：v0.0.6 标签随本表登记提交前移，精确指针以 git tag 为准。
> 注：v0.0.6.1 标签随本表登记提交前移，精确指针以 git tag 为准。

| v0.1.0 | dd9ab247ff9f4909b2993cf988639cd5c51117c6 | 里程碑版本：研究任务四节点 + 金标准 pilot + 提示词清单/审阅文档 + 模型绑定统一为 DeepSeek V4 Flash / GLM 4.7 Flash 审核 |
| v0.1.1 | 2230cd7d56f15fb07d81f4bce318630ded07aa0f | P1 领域画像优化：工作规划生成 DomainProfile、检索不再内置生物医药维度、质量缺失数据不估计、知识抽取支持领域候选/冻结 schema |
| v0.1.2 | 0ad8b9ea3ded37c063c7c731d24dd7d1e5ce1097 | 通用生成型任务支持：planner 识别 generative 任务、consumer 输出 design_context、content 生成候选 strategies、reviewer 增加创新充分性检查 |

| v0.2.0 | c49f11715e788ddf152f732355a6b14c52a35402 | 检索专项 skills（证据缺口/查询扩展/引用溯源）、质量控制与领域词典全局归并、多库看板与规划交互、人工审核界面、NCPSSD 中文社科语料接入 |

| v0.3.0 | 5e8e60191b6b964e28aa68874c6a69db30d3eeac | 审核节点 4:1 双维度与正确性硬门槛、科研超边本体、语义节点域/关系通道、局部编号清理、可视化超边面板 |

| v0.3.1 | 4aa83f86ee2e436d3547fbad098a7452daa4189c | 语义节点域修正、论文局部编号及别名清理、按语义域筛选图谱 |

| v0.4.0 | working-tree | Planner-first 统一编排、Planner 契约升级、LLM 知识消费节点；四研究角色统一 DeepSeek V4 Pro |

| v0.4.1 | working-tree | 机制—机会—算子闭环：超边类型配额与机制相关性检索、引用校验只认 ID 形状、design_context 固定五键且绑定证据、自研反应算子库与算子链校验、候选池两阶段生成+聚类去重+评分排序、审核设计契约硬校验、修订意见回流内容节点、事实核查节点、study_runs 中间态落库、语料领域相关性硬门、条件/测量透传修复（真实模型重抽实测 0→35 条测量）、研究节点模型调用超时与 reasoning_effort 默认关闭；测试 72→90 |

| v0.4.2 | fddd534 | 领域内容零硬编码：技能包（journal-quartiles/relation-lexicon/mechanism-keywords/task-kind-hints）与领域包（chemistry/biomedicine/materials/humanities_social_science/general）全部移出代码，改由 packs 加载器按 RA_PACKS_DIR/RA_PACKS_FALLBACK 读取；缺包时告警并返回空值，不再静默内联兜底 |

| v0.4.3 | fff12bf | JCR 分区导入（examples/import_jcr_xlsx.py，零依赖 xlsx 解析）+ 期刊匹配归一（大小写/连字符/副标题/冠词）；相关性低信号策略 C（有模型时 LLM 扩展，无模型时 warn 放行不静默丢弃）；.env 先于 settings 加载 |

| v0.4.4 | working-tree | 外部静态评审 22 项（19 有效/3 无效）四批修复：事实核查引用白名单补全、人工复核不再清空 pdf/clean_text（upsert COALESCE 根因修复）、监控水位线先提交+ack/release、超边合并重指向成员与测量、dotenv 顺序、领域相关性命中率与低信号策略、PDF 解析降级链路、按来源隔离检索错误、抽取失败显式标记、planner 解析失败不再伪成功、机制多氮判定去同义反复、引用归属两遍映射、中文分句与长段落切分、证据字符区间入 provenance、轮次快照节点、版本号与 VERSIONS/测试同步、超边条件去重与索引补全；测试 90→169 |

| v0.4.5 | 799f124 | 派工协议 + 统一事件日志 + 研究流程页改造：节点能力登记表（13 节点/12 任务，登记项一律指向既有函数）、派工执行器（幂等/可取消/收敛/逐步回报 + `$stepN` 引用串联）、dispatch_runs 落库、补齐 4 个"登记了但没实现"的任务并修掉质量评估的假实现、统一事件日志（受控词表/脱敏截断/trace/环形缓冲/jsonl + `research-agent-logs` 实时跟随）、`LoggedModel` 透明记账代理、`/api/log` 前端诊断事件、研究流程页改为登记表驱动且状态语义明确（未执行是诚实默认值）、自然语言→派工方案解析（关键词确定性识别 + 模型解析，只认登记表任务）；测试 328→403，新增浏览器交互冒烟 33 项 |
