"""节点能力登记表：让工作规划节点"看得见"其他节点能干什么。

**这张表只描述、不实现**——每个 ``TaskSpec.entry`` 都指向**既有函数**。
为什么要集中登记：此前"检索/抽取"的执行入口散落在 ``collaboration.py`` 里，
规划节点只能下这两种单；其余节点（质量、消费、成段、审核、核查）明明都接了
大模型，却没有任何"派工"的通道，于是用户只能逐页手动操作。

登记表要回答四个问题，缺一个就无法派工：

1. 这个节点**能执行哪些任务**；
2. 每个任务**需要什么参数**（``accepts``）；
3. 执行完**产出什么**（``produces``）——后续任务靠它串联（``$step0.paper_keys``）；
4. **成本与耗时量级**——用于界面提示、预算判断与超时。

``needs_model`` 标出该任务是否依赖大模型：接模型的节点必须可派工（用户要求），
纯计算/数据库任务默认不参与自动派工，只在"用户直下指令"时可用。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from research_agent.models import ROLE_LABEL, ROLE_MODEL_DEFAULT, ROLE_PROVIDER

__all__ = [
    "TaskSpec",
    "NodeSpec",
    "NODES",
    "LLM_NODES",
    "DISPATCHABLE_NODES",
    "list_nodes",
    "list_tasks",
    "get_task",
    "get_node",
    "describe_for_prompt",
    "MODEL_ROLES",
]

#: 当前接入大模型的角色（与 models.py 的注册表同源，避免两处漂移）
MODEL_ROLES: dict[str, str] = {
    role: ROLE_MODEL_DEFAULT.get(role, "") for role in ROLE_PROVIDER
}


@dataclass(frozen=True)
class TaskSpec:
    """一个可派工的任务。"""

    task: str
    label: str
    entry: Callable[..., Any] | None
    accepts: tuple[str, ...] = ()
    produces: tuple[str, ...] = ()
    #: network / llm / db / compute
    cost: str = "compute"
    #: 典型耗时（秒）下界与上界，用于界面提示与超时判断
    typical_seconds: tuple[int, int] = (1, 10)
    #: 是否按"篇"计（用于进度估算）
    per_item: bool = False
    needs_model: bool = True
    #: 缺模型时的行为：fallback（确定性兜底可用）/ unavailable（直接不可用）
    model_missing_behavior: str = "fallback"
    timeout_seconds: int = 600
    #: 该任务的产出能不能被后续任务引用（$stepN.field）
    chainable: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "task": self.task, "label": self.label,
            "accepts": list(self.accepts), "produces": list(self.produces),
            "cost": self.cost, "typical_seconds": list(self.typical_seconds),
            "per_item": self.per_item, "needs_model": self.needs_model,
            "model_missing_behavior": self.model_missing_behavior,
            "timeout_seconds": self.timeout_seconds,
            "chainable": self.chainable,
            "implemented": self.entry is not None,
        }


@dataclass(frozen=True)
class NodeSpec:
    """一个可被派工的节点。"""

    node: str
    label: str
    group: str
    desc: str
    #: 对应的 LLM 角色（纯计算节点为 ""）
    role: str = ""
    tasks: tuple[TaskSpec, ...] = ()
    #: 默认是否参与"自动补检"（用户要求：接模型的节点都要能派工；
    #: 纯计算任务默认只允许用户直下指令触发）
    auto_dispatch: bool = True

    @property
    def needs_model(self) -> bool:
        return bool(self.role)

    @property
    def model(self) -> str:
        return MODEL_ROLES.get(self.role, "") if self.role else ""

    @property
    def model_label(self) -> str:
        return ROLE_LABEL.get(self.role, "") if self.role else ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "node": self.node, "label": self.label, "group": self.group,
            "desc": self.desc, "role": self.role, "model": self.model,
            "needs_model": self.needs_model,
            "auto_dispatch": self.auto_dispatch,
            "tasks": [t.as_dict() for t in self.tasks],
        }


# ------------------------------------------------------------------ 入口包装
# 全部做成惰性 import 的薄包装：登记表被导入时不该把整条链路（含 API 客户端、
# 模型对象）都拉起来——那样单测与冷启动都会变慢。

def _retrieve_entry(**kwargs: Any) -> dict[str, Any]:
    from research_agent.writing.collaboration import build_retrieval_services
    from research_agent.study.collection import collect_mission

    request = {
        "seed_terms": list(kwargs.get("seed_terms") or []),
        "max_results": int(kwargs.get("max_results") or 20),
        "source": kwargs.get("source") or None,
        "year_from": kwargs.get("year_from") or None,
        "reason": kwargs.get("reason") or "派工：检索补全",
    }
    services = build_retrieval_services(kwargs.get("settings"))
    return collect_mission(request, services=services) or {}


def _assess_quality_entry(**kwargs: Any) -> dict[str, Any]:
    """质量评估（LLM 角色 quality）：复用质量控制节点的构造器，逐篇评估并落库。

    这里**真的评估**——早期版本只数了一下已有结果就返回，看着"执行成功"
    其实什么也没做（派工形同虚设）。
    """
    from research_agent.db import connect
    from research_agent.config import settings as default_settings
    from research_agent.quality.node import make_quality_node

    keys = [str(k) for k in (kwargs.get("paper_keys") or [])]
    if not keys:
        return {"assessed": 0, "skipped": True,
                "skip_reason": "未提供 paper_keys"}
    settings = kwargs.get("settings") or default_settings
    conn = kwargs.get("conn") or connect(kwargs.get("db_path")
                                         or settings.db_path)
    own_conn = kwargs.get("conn") is None
    try:
        node = make_quality_node(kwargs.get("api"), kwargs.get("model"),
                                 conn=conn, settings=settings)
        decisions: dict[str, int] = {}
        failed: list[dict[str, str]] = []
        for key in keys[:kwargs.get("limit") or 50]:
            try:
                out = node({"current_key": key, "meta_attempts": 0}) or {}
            except Exception as exc:  # noqa: BLE001 —— 单篇失败不影响其余
                failed.append({"paper_key": key, "error": str(exc)[:200]})
                continue
            decision = str(out.get("decision") or out.get("status") or "")
            if decision:
                decisions[decision] = decisions.get(decision, 0) + 1
        return {"assessed": sum(decisions.values()), "requested": len(keys),
                "decisions": decisions, "failed": failed[:10]}
    finally:
        if own_conn:
            conn.close()


def _enrich_metadata_entry(**kwargs: Any) -> dict[str, Any]:
    """元数据回补（无模型）：复用检索节点的 ``enrich_existing_paper``。"""
    from research_agent.db import connect
    from research_agent.config import settings as default_settings
    from research_agent.retrieval.api_clients import ApiHub
    from research_agent.retrieval.node import enrich_existing_paper

    keys = [str(k) for k in (kwargs.get("paper_keys") or [])]
    if not keys:
        return {"enriched": 0, "skipped": True,
                "skip_reason": "未提供 paper_keys"}
    settings = kwargs.get("settings") or default_settings
    conn = kwargs.get("conn") or connect(kwargs.get("db_path")
                                         or settings.db_path)
    own_conn = kwargs.get("conn") is None
    try:
        api = kwargs.get("api") or ApiHub()
        changed = 0
        failed: list[dict[str, str]] = []
        for key in keys[:kwargs.get("limit") or 20]:
            try:
                out = enrich_existing_paper(key, api=api,
                                            model=kwargs.get("model"),
                                            conn=conn, settings=settings) or {}
            except Exception as exc:  # noqa: BLE001
                failed.append({"paper_key": key, "error": str(exc)[:200]})
                continue
            if out.get("changed"):
                changed += 1
        return {"enriched": changed, "requested": len(keys),
                "failed": failed[:10]}
    finally:
        if own_conn:
            conn.close()


def _plan_entry(**kwargs: Any) -> dict[str, Any]:
    """生成任务单（LLM 角色 planner）：复用整篇工作规划入口。"""
    from research_agent.db import connect
    from research_agent.config import settings as default_settings
    from research_agent.writing.section_service import build_project_plan

    project_id = int(kwargs.get("project_id") or 0)
    if not project_id:
        return {"skipped": True, "skip_reason": "未提供 project_id"}
    settings = kwargs.get("settings") or default_settings
    conn = kwargs.get("conn") or connect(kwargs.get("db_path")
                                         or settings.db_path)
    own_conn = kwargs.get("conn") is None
    try:
        result = build_project_plan(
            conn, project_id=project_id,
            topic=str(kwargs.get("topic") or ""),
            instruction=str(kwargs.get("request") or kwargs.get("instruction")
                            or ""),
            settings=settings, planner_model=kwargs.get("model"),
            persist=bool(kwargs.get("persist", True))) or {}
        plan = result.get("plan") or {}
        return {"mode": result.get("mode") or "",
                "sections": len(plan.get("sections") or []) or len(
                    result.get("sections") or []),
                "plan": plan}
    finally:
        if own_conn:
            conn.close()


def _library_maintain_entry(**kwargs: Any) -> dict[str, Any]:
    """文献库维护（无模型）：标签 / 收藏 / 文件夹等既有操作。"""
    from research_agent.db import connect
    from research_agent.config import settings as default_settings
    from research_agent.library import store as libstore

    action = str(kwargs.get("action") or "").strip()
    if not action:
        return {"changed": 0, "skipped": True, "skip_reason": "未提供 action"}
    handler = getattr(libstore, f"set_{action}", None) or getattr(
        libstore, action, None)
    if not callable(handler):
        return {"changed": 0, "skipped": True,
                "skip_reason": f"文献库不支持的动作: {action}"}
    settings = kwargs.get("settings") or default_settings
    conn = kwargs.get("conn") or connect(kwargs.get("db_path")
                                         or settings.db_path)
    own_conn = kwargs.get("conn") is None
    try:
        keys = [str(k) for k in (kwargs.get("paper_keys") or [])]
        payload = kwargs.get("payload")
        try:
            out = handler(conn, keys, payload) if payload is not None else \
                handler(conn, keys)
        except TypeError:
            out = handler(conn, *keys)
        return {"changed": len(keys), "action": action,
                "result": out if isinstance(out, dict) else {"ok": bool(out)}}
    finally:
        if own_conn:
            conn.close()


def _export_entry(**kwargs: Any) -> dict[str, Any]:
    """导出（无模型）：复用写作台的整篇导出。"""
    from research_agent.db import connect
    from research_agent.config import settings as default_settings
    from research_agent.writing.service import export_project_markdown

    project_id = int(kwargs.get("project_id") or 0)
    if not project_id:
        return {"skipped": True, "skip_reason": "未提供 project_id"}
    settings = kwargs.get("settings") or default_settings
    conn = kwargs.get("conn") or connect(kwargs.get("db_path")
                                         or settings.db_path)
    own_conn = kwargs.get("conn") is None
    try:
        out = export_project_markdown(conn, project_id) or {}
        return {"filename": out.get("filename") or "",
                "chars": len(out.get("content") or ""),
                "content": out.get("content") or ""}
    finally:
        if own_conn:
            conn.close()


def _extract_entry(**kwargs: Any) -> dict[str, Any]:
    from research_agent.pipeline import process_papers
    from research_agent.writing.collaboration import build_retrieval_services

    keys = [str(k) for k in (kwargs.get("paper_keys") or [])]
    if not keys:
        return {"extracted": 0, "skipped": True,
                "skip_reason": "未提供 paper_keys"}
    services = build_retrieval_services(kwargs.get("settings"))
    ok = 0
    for key in keys[:20]:
        try:
            results = process_papers([key], services=services)
        except Exception:  # noqa: BLE001 —— 单篇失败不影响其余
            continue
        status = str((results[0] if results else {}).get("status") or "")
        if status in ("extracted", "knowledge"):
            ok += 1
    return {"extracted": ok, "requested": len(keys)}


def _consume_entry(**kwargs: Any) -> dict[str, Any]:
    from research_agent.db import connect
    from research_agent.config import settings as default_settings
    from research_agent.study.consumer import mine_ontology_evidence

    conn = connect(kwargs.get("db_path") or default_settings.db_path)
    try:
        plan = kwargs.get("plan") or {}
        mission = plan.get("mission") or {}
        bundle = mine_ontology_evidence(conn, mission)
        return {"patterns": len(bundle.get("patterns") or []),
                "evidence": len(bundle.get("evidence") or []),
                "hyperedges": len(bundle.get("hyperedges") or []),
                "coverage_score": bundle.get("coverage_score")}
    finally:
        conn.close()


def _rebuild_ontology_entry(**kwargs: Any) -> dict[str, Any]:
    from research_agent.db import connect
    from research_agent.config import settings as default_settings
    from research_agent.ontology.store import rebuild_ontology_views

    conn = connect(kwargs.get("db_path") or default_settings.db_path)
    try:
        return rebuild_ontology_views(conn) or {}
    finally:
        conn.close()


def _compose_section_entry(**kwargs: Any) -> dict[str, Any]:
    """按部分成段（LLM 角色 content）。

    直接复用 `section_compose.compose_section`——它**不落库**，
    落库由 `section_service` 统一做；派工场景下由派工执行器决定是否保存。
    """
    from research_agent.writing.section_compose import compose_section

    section_key = str(kwargs.get("section_key") or "")
    if not section_key:
        return {"skipped": True, "skip_reason": "未提供 section_key"}
    model = kwargs.get("model")
    result = compose_section(
        heading=str(kwargs.get("heading") or section_key),
        instruction=str(kwargs.get("instruction") or ""),
        plan=kwargs.get("plan") or {},
        materials=list(kwargs.get("materials") or []),
        sufficiency=kwargs.get("sufficiency") or {},
        model=model,
        model_reason="" if model else "派工未提供模型",
        words=int(kwargs.get("words") or 0),
    )
    return {
        "content": result.get("content") or "",
        "citations": result.get("citations") or [],
        "generated_by": result.get("generated_by"),
        "invalid_indices": result.get("invalid_indices") or [],
        "content_chars": len(result.get("content") or ""),
    }


def _review_entry(**kwargs: Any) -> dict[str, Any]:
    """审核校对（LLM 角色 review）：复用审核节点的构造器。"""
    from research_agent.study.reviewer import make_review_node

    content = str(kwargs.get("content") or "")
    if not content.strip():
        return {"skipped": True, "skip_reason": "无正文可审"}
    node = make_review_node(kwargs.get("model"),
                            conn=kwargs.get("conn"),
                            settings=kwargs.get("settings"))
    out = node({
        "plan": kwargs.get("plan") or {},
        "knowledge": kwargs.get("knowledge") or {},
        "content": {"markdown": content},
        "review_round": 0,
    }) or {}
    decision = str(out.get("status") or out.get("decision") or "")
    review = out.get("review") or {}
    return {
        "decision": decision,
        "issues": list(review.get("issues") or []),
        "summary": str(review.get("summary") or "")[:500],
    }


def _fact_check_entry(**kwargs: Any) -> dict[str, Any]:
    """事实核查（LLM 角色 fact_check）：复用核查节点的构造器。"""
    from research_agent.study.fact_check import make_fact_check_node

    content = str(kwargs.get("content") or "")
    if not content.strip():
        return {"skipped": True, "skip_reason": "无正文可核查"}
    node = make_fact_check_node(kwargs.get("model"),
                                conn=kwargs.get("conn"),
                                settings=kwargs.get("settings"))
    out = node({
        "plan": kwargs.get("plan") or {},
        "knowledge": kwargs.get("knowledge") or {},
        "content": {"markdown": content},
        "review_round": 0,
    }) or {}
    report = out.get("fact_check") or {}
    return {
        "violations": list(report.get("violations") or []),
        "revised": bool(report.get("revised")),
        "summary": str(report.get("summary") or "")[:500],
    }


NODES: tuple[NodeSpec, ...] = (
    # ---------------------------------------------------------- 数据构建
    NodeSpec(
        node="retrieval", label="文献检索节点", group="数据构建", role="retriever",
        desc="按检索词抓文献 / PDF 入库 / 清洗 / 元数据回补",
        tasks=(
            TaskSpec(
                task="retrieve", label="检索补全", entry=_retrieve_entry,
                accepts=("seed_terms", "max_results", "source", "year_from"),
                produces=("count", "paper_keys", "errors"),
                cost="network", typical_seconds=(20, 120),
                needs_model=True, model_missing_behavior="fallback",
                timeout_seconds=900,
            ),
            TaskSpec(
                task="enrich_metadata", label="元数据回补",
                entry=_enrich_metadata_entry,
                accepts=("paper_keys",), produces=("enriched",),
                cost="network", typical_seconds=(2, 10), per_item=True,
                needs_model=False, model_missing_behavior="unavailable",
                timeout_seconds=300,
            ),
        ),
    ),
    NodeSpec(
        node="quality", label="质量控制节点", group="数据构建", role="quality",
        desc="A/T/Q 评分 / 路由 / 领域词典全局归并",
        tasks=(
            TaskSpec(
                task="assess_quality", label="质量评估", entry=_assess_quality_entry,
                accepts=("paper_keys",), produces=("assessed", "decisions"),
                cost="llm", typical_seconds=(2, 5), per_item=True,
                needs_model=True, model_missing_behavior="fallback",
                timeout_seconds=300,
            ),
        ),
    ),
    NodeSpec(
        node="knowledge", label="知识提取节点", group="数据构建", role="knowledge",
        desc="预处理 / 实体关系事件抽取 / 动态本体写入",
        tasks=(
            TaskSpec(
                task="extract_knowledge", label="知识抽取",
                entry=_extract_entry,
                accepts=("paper_keys", "scope"),
                produces=("extracted", "hyperedges"),
                cost="llm", typical_seconds=(5, 20), per_item=True,
                needs_model=True, model_missing_behavior="unavailable",
                timeout_seconds=900,
            ),
        ),
    ),
    NodeSpec(
        node="human_review", label="人工审核", group="数据构建", role="",
        desc="低质量或元数据无法补全的文献（需人工介入）",
        auto_dispatch=False,
        tasks=(),
    ),
    # ---------------------------------------------------------- 研究流程
    NodeSpec(
        node="planner", label="工作规划节点", group="研究流程", role="planner",
        desc="解析请求 → 生成检索/分析/证据/生成契约（**派工发起者**）",
        auto_dispatch=False,          # 它是发令方，不接受派工
        tasks=(
            TaskSpec(
                task="plan", label="生成任务单", entry=_plan_entry,
                accepts=("request",), produces=("plan",),
                cost="llm", typical_seconds=(5, 30),
                needs_model=True, model_missing_behavior="fallback",
                timeout_seconds=180, chainable=False,
            ),
        ),
    ),
    NodeSpec(
        node="collection", label="检索执行节点", group="研究流程", role="",
        desc="执行 Planner 的 retrieval_plan（检索/评估/知识提取）",
        auto_dispatch=False,          # 由 planner 的计划驱动，不单独派工
        tasks=(),
    ),
    NodeSpec(
        node="knowledge_consumer", label="知识消费节点", group="研究流程",
        role="consumer",
        desc="LLM 机制理解 / 机会发现 / 设计上下文 / 提出补检请求",
        tasks=(
            TaskSpec(
                task="consume_knowledge", label="知识消费",
                entry=_consume_entry,
                accepts=("plan", "mission"), produces=("bundle", "design_context"),
                cost="llm", typical_seconds=(10, 40),
                needs_model=True, model_missing_behavior="fallback",
                timeout_seconds=600,
            ),
        ),
    ),
    NodeSpec(
        node="content_builder", label="内容形成节点", group="研究流程",
        role="content",
        desc="生成可溯源草稿（写作台按部分成段走的也是这个角色）",
        tasks=(
            TaskSpec(
                task="compose_section", label="按部分成段", entry=_compose_section_entry,
                accepts=("section_key", "fields", "materials"),
                produces=("content", "citations"),
                cost="llm", typical_seconds=(10, 60),
                needs_model=True, model_missing_behavior="fallback",
                timeout_seconds=600,
            ),
        ),
    ),
    NodeSpec(
        node="reviewer", label="审核校对节点", group="研究流程", role="review",
        desc="核查引用、证据支持、设计契约与覆盖缺口",
        tasks=(
            TaskSpec(
                task="review", label="审核校对", entry=_review_entry,
                accepts=("section_key", "content"),
                produces=("decision", "issues"),
                cost="llm", typical_seconds=(10, 40),
                needs_model=True, model_missing_behavior="fallback",
                timeout_seconds=600,
            ),
        ),
    ),
    NodeSpec(
        node="fact_checker", label="事实核查节点", group="研究流程", role="fact_check",
        desc="核查无来源断言、伪造引用与数值缺证据，可回流修订",
        tasks=(
            TaskSpec(
                task="fact_check", label="事实核查", entry=_fact_check_entry,
                accepts=("section_key", "content"),
                produces=("violations", "revised"),
                cost="llm", typical_seconds=(10, 40),
                needs_model=True, model_missing_behavior="fallback",
                timeout_seconds=600,
            ),
        ),
    ),
    # ---------------------------------------------------------- 写作台
    NodeSpec(
        node="interview", label="访谈（工作规划）", group="写作台", role="planner",
        desc="逐部分问答：拟 3 个方案 → 判定支撑 → 缺口决定 → 派工 → 写作",
        auto_dispatch=False,
        tasks=(),
    ),
    # ---------------------------------------------------------- 纯计算
    NodeSpec(
        node="ontology", label="动态本体", group="资产", role="",
        desc="本体视图重建（域/通道/超边聚类）",
        auto_dispatch=False,
        tasks=(
            TaskSpec(
                task="rebuild_ontology", label="重建本体视图",
                entry=_rebuild_ontology_entry,
                accepts=(), produces=("nodes", "edges", "hyperedges", "domains"),
                cost="compute", typical_seconds=(5, 60),
                needs_model=False, model_missing_behavior="unavailable",
                timeout_seconds=600,
            ),
        ),
    ),
    NodeSpec(
        node="library", label="文献库", group="资产", role="",
        desc="标签 / 收藏 / 文件夹 / 删除 / 引用导出",
        auto_dispatch=False,
        tasks=(
            TaskSpec(
                task="library_maintain", label="文献库维护",
                entry=_library_maintain_entry,
                accepts=("action", "paper_keys", "payload"),
                produces=("changed",),
                cost="db", typical_seconds=(1, 3),
                needs_model=False, model_missing_behavior="unavailable",
                timeout_seconds=120,
            ),
            TaskSpec(
                task="export", label="导出", entry=_export_entry,
                # 实际实现（`export_project_markdown`）要的是**项目**，不是文献列表。
                # 契约写错会让每次派工都在参数校验处空转——"登记了但永远跳过"。
                accepts=("project_id",), produces=("content", "filename"),
                cost="compute", typical_seconds=(1, 3),
                needs_model=False, model_missing_behavior="unavailable",
                timeout_seconds=120, chainable=False,
            ),
        ),
    ),
)


# ------------------------------------------------------------------ 查询辅助

def list_nodes(*, group: str = "", needs_model: bool | None = None,
               dispatchable_only: bool = False) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for spec in NODES:
        if group and spec.group != group:
            continue
        if needs_model is not None and spec.needs_model != needs_model:
            continue
        if dispatchable_only and not spec.tasks:
            continue
        out.append(spec.as_dict())
    return out


def get_node(node: str) -> NodeSpec | None:
    for spec in NODES:
        if spec.node == node:
            return spec
    return None


def list_tasks(*, node: str = "", implemented_only: bool = False
               ) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for spec in NODES:
        if node and spec.node != node:
            continue
        for task in spec.tasks:
            if implemented_only and task.entry is None:
                continue
            item = task.as_dict()
            item.update({"node": spec.node, "node_label": spec.label,
                         "group": spec.group, "role": spec.role,
                         "model": spec.model})
            out.append(item)
    return out


def get_task(task: str) -> tuple[NodeSpec, TaskSpec] | None:
    for spec in NODES:
        for item in spec.tasks:
            if item.task == task:
                return spec, item
    return None


#: 接了模型的节点（用户要求：这些都要能派工）
LLM_NODES: tuple[str, ...] = tuple(
    spec.node for spec in NODES if spec.needs_model)

#: 有任务、且允许派工的节点
DISPATCHABLE_NODES: tuple[str, ...] = tuple(
    spec.node for spec in NODES if spec.tasks)


def describe_for_prompt() -> str:
    """给规划模型看的节点清单（派工解析时塞进提示词）。"""
    lines: list[str] = []
    for spec in NODES:
        if not spec.tasks:
            continue
        head = f"- {spec.node}（{spec.label}）"
        if spec.needs_model:
            head += f"[模型角色 {spec.role}]"
        if not spec.auto_dispatch:
            head += "[仅用户直下指令]"
        lines.append(head)
        for task in spec.tasks:
            params = "、".join(task.accepts) or "无"
            produces = "、".join(task.produces) or "无"
            cost = {"network": "网络", "llm": "模型", "db": "数据库",
                    "compute": "计算"}.get(task.cost, task.cost)
            lines.append(
                f"    · {task.task}（{task.label}）：参数 {params}；"
                f"产出 {produces}；成本 {cost}；典型 {task.typical_seconds[0]}"
                f"~{task.typical_seconds[1]}s")
    return "\n".join(lines)
