"""全局配置：路径、质量评估权重/阈值、学科前沿迭代速度等。

所有阈值都可经环境变量覆盖（见 Settings.from_env）。

注意：本模块在 **import 期** 就要求值模块级 `settings`，因此必须在这里
先把项目根目录的 `.env` 载入环境，否则 `.env` 中的 `RA_*` 对
`default_settings` 路径（看板默认库、`settings or default_settings` 兜底处）无效。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]

try:  # 可选依赖：缺失时退化为"只读真实环境变量"
    from dotenv import load_dotenv

    load_dotenv(PROJECT_ROOT / ".env")
    load_dotenv()  # 兼容从当前工作目录启动的场景
except Exception:  # noqa: BLE001 —— 配置加载不应因 .env 缺失/损坏而失败
    import logging as _logging

    _logging.getLogger(__name__).warning(
        "未能加载 .env（python-dotenv 缺失或文件不可解析）；"
        "RA_* 环境变量将只取进程环境")


def _env_float(key: str, default: float) -> float:
    raw = os.getenv(key)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_bool(key: str, default: bool) -> bool:
    raw = os.getenv(key)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Settings:
    """集中管理全流水线的路径与超参数。"""

    # ---------- 路径 ----------
    project_root: Path = PROJECT_ROOT
    data_dir: Path = field(default_factory=lambda: PROJECT_ROOT / "data")
    pdf_dir: Path = field(default_factory=lambda: PROJECT_ROOT / "data" / "pdfs")
    db_path: Path = field(default_factory=lambda: PROJECT_ROOT / "data" / "research_agent.db")

    # ---------- 质量评估：Q = Q_WEIGHT_A*A + Q_WEIGHT_T*T ----------
    q_weight_a: float = 0.6
    q_weight_t: float = 0.4
    threshold_direct: float = 0.8   # Q >= 0.8 直接进入知识提取
    threshold_flag: float = 0.5     # 0.5 <= Q < 0.8 标记后进入知识提取；Q < 0.5 人工审核

    # ---------- 权威性 A 的子项权重 ----------
    a_venue_weight: float = 0.5     # 期刊/出版社分级（JCR/SCI 分区）
    a_hindex_weight: float = 0.3    # 作者 H 指数
    a_citation_weight: float = 0.2  # 被引用次数

    # ---------- 时效性 T ----------
    field_half_life: dict = field(default_factory=lambda: {
        "fast": 2.0,     # AI/ML/计算机 等高速迭代学科
        "medium": 4.0,   # 生物医学/化学/物理 等
        "slow": 8.0,     # 数学/基础理论 等
    })
    default_field_velocity: str = "medium"
    current_year: int = field(default_factory=lambda: date.today().year)

    # ---------- 元数据回补 ----------
    max_meta_attempts: int = 3      # 质量节点 → 检索节点回补的最大轮数
    required_meta_fields: tuple = ("authors", "affiliations", "publication", "doi")

    # ---------- 知识提取 ----------
    max_extract_chars: int = 8000   # 单次 LLM 抽取的文本窗口大小
    max_extract_chunks: int = 4     # 一篇论文最多抽取的文本块数（控制成本）
    q_flag_penalty: float = 0.85    # “标记后发送”的文献，质量权重折扣
    strong_edge_min_conf: float = 0.72  # 低于此阈值的强因果/调控边标记 candidate
    review_correctness_min: float = 0.85  # 审核：证据与引用正确性最低阈值

    # ---------- 质量控制：全局领域词典归并 ----------
    global_merge_enabled: bool = True
    global_merge_interval_nodes: int = 300

    # ---------- 知识提取·二次精修（v0.0.6） ----------
    knowledge_refine_enabled: bool = True      # 是否允许“低置信/泛化关系”二次精修
    refine_min_conf: float = 0.6               # 低于此置信度的实体/关系触发点名
    refine_max_items: int = 12                 # 单轮最多点名的问题数（防提示词膨胀）
    refine_max_attempts: int = 2               # 精修最多轮数（收敛信号提前结束）
    generic_fallback_types: tuple = ("related_to",)  # 视为“语义过宽”的兜底关系

    # ---------- 网络 ----------
    http_timeout: int = 30
    user_agent: str = "research-agent/0.1 (mailto:research@example.com)"

    # ---------- 研究任务节点超时（v0.4.1） ----------
    # 实测：推理型模型在长结构化输出上可能要 6 分钟以上才返回，也不保证一定返回；
    # 超时后节点降级为确定性实现，避免整条研究链路被单个请求拖死。
    study_consumer_timeout: int = 900   # 知识消费节点（秒）
    study_content_timeout: int = 900    # 内容形成节点（秒）
    study_review_timeout: int = 600     # 审核节点（秒）
    study_fact_check_timeout: int = 600  # 事实核查节点（秒）
    # 审核最大轮数：每轮都会触发一次内容重生成（真实耗时 5-10 分钟），
    # 综述类任务通常不需要多轮，可用环境变量或入口参数降到 1。
    study_max_review_rounds: int = 3

    # 相关性硬门在"词元无法判定"时的行为（v0.4.3，策略 C）：
    #   "warn" = 放行并在检索报告里标注低信号（默认，避免中文主题 + 英文库整批误杀）
    #   "drop" = 沿用旧行为（整批丢弃）
    relevance_gate_low_signal: str = "warn"

    # ---------- 章节级工作流（写作台 → 工作规划节点）----------
    # 充分性判定：confidence ≥ 阈值 → 直接消费+生成；否则补检；预算用尽 → 明确失败
    section_sufficiency_threshold: float = 0.62
    section_max_collection_rounds: int = 2
    section_sufficiency_min_papers: int = 6
    # 小库自适应：未声明引用数时把 min_papers 下调到"库内已入库总数"，
    # 避免 8 篇的演示库被 6 篇默认值一律判不足。显式收紧阈值（≤2）时不受影响。
    # 设为 False 可强制使用配置值（严格模式，缺一篇就补检）。
    section_adaptive_paper_floor: bool = True
    section_sufficiency_min_knowledge_ratio: float = 0.6
    section_sufficiency_min_condition_ratio: float = 0.3
    section_sufficiency_min_requirement_ratio: float = 0.5
    section_sufficiency_min_evidence: int = 3
    # 要求覆盖只能"如实判定"：要求词元若是中文、而库内知识是英文，用 LIKE 去查
    # 必然 0 命中——那不是"库里缺这个"，而是"我们没法核对"。把这类词元算作
    # **无法核对**（不计入分母、不改判定），否则一个不可能达标的硬闸门会让这一节
    # 永远写不出来。设 False 可恢复旧行为（一律算缺失）。
    section_requirement_unverifiable_ok: bool = True
    # 一次协作里最多抽取多少篇。抽取是逐个 LLM 调用（每篇约 5~20s），
    # 上限太小会让"补检"永远推不动知识覆盖率（实测 20 篇上限时，
    # 需要 ~120 篇才能把覆盖率从 30% 提到 60%）。
    section_extract_max_papers: int = 60
    # 抽取的总时长预算（秒）：超出就停下，剩余部分留给下一轮/下次补检。
    section_extract_max_seconds: int = 900
    # 证据不足时是否仍然产出一版正文（默认 False = 不硬写，返回 needs_data）
    section_allow_write_when_insufficient: bool = False
    # 期刊分区表来自技能包 packs/skills/journal-quartiles（可用
    # RA_JOURNAL_QUARTILES 指向的 JSON 覆盖）；此字段仅为兼容保留，
    # 取值时通过 property 读取包内容，不再内联学科名单。
    @property
    def journal_quartiles_seed(self) -> dict:
        from research_agent import packs

        return packs.journal_quartiles()

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.pdf_dir.mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_env(cls) -> "Settings":
        s = cls()
        s.q_weight_a = _env_float("RA_Q_WEIGHT_A", s.q_weight_a)
        s.q_weight_t = _env_float("RA_Q_WEIGHT_T", s.q_weight_t)
        s.threshold_direct = _env_float("RA_THRESHOLD_DIRECT", s.threshold_direct)
        s.threshold_flag = _env_float("RA_THRESHOLD_FLAG", s.threshold_flag)
        s.a_venue_weight = _env_float("RA_A_VENUE_W", s.a_venue_weight)
        s.a_hindex_weight = _env_float("RA_A_HINDEX_W", s.a_hindex_weight)
        s.a_citation_weight = _env_float("RA_A_CITATION_W", s.a_citation_weight)
        s.max_meta_attempts = int(os.getenv("RA_MAX_META_ATTEMPTS", s.max_meta_attempts))
        s.knowledge_refine_enabled = _env_bool(
            "RA_KNOWLEDGE_REFINE", s.knowledge_refine_enabled)
        s.refine_min_conf = _env_float("RA_REFINE_MIN_CONF", s.refine_min_conf)
        s.refine_max_items = int(os.getenv("RA_REFINE_MAX_ITEMS", s.refine_max_items))
        s.refine_max_attempts = int(
            os.getenv("RA_REFINE_MAX_ATTEMPTS", s.refine_max_attempts))
        s.review_correctness_min = _env_float(
            "RA_REVIEW_CORRECTNESS_MIN", s.review_correctness_min)
        s.study_consumer_timeout = int(
            os.getenv("RA_STUDY_CONSUMER_TIMEOUT", s.study_consumer_timeout))
        s.study_content_timeout = int(
            os.getenv("RA_STUDY_CONTENT_TIMEOUT", s.study_content_timeout))
        s.study_review_timeout = int(
            os.getenv("RA_STUDY_REVIEW_TIMEOUT", s.study_review_timeout))
        s.study_fact_check_timeout = int(
            os.getenv("RA_STUDY_FACT_CHECK_TIMEOUT", s.study_fact_check_timeout))
        s.study_max_review_rounds = int(
            os.getenv("RA_STUDY_MAX_REVIEW_ROUNDS", s.study_max_review_rounds))
        gate = os.getenv("RA_RELEVANCE_GATE_LOW_SIGNAL")
        if gate and gate.strip().lower() in ("warn", "drop"):
            s.relevance_gate_low_signal = gate.strip().lower()
        s.q_flag_penalty = _env_float("RA_Q_FLAG_PENALTY", s.q_flag_penalty)
        s.max_extract_chars = int(
            os.getenv("RA_MAX_EXTRACT_CHARS", s.max_extract_chars))
        s.max_extract_chunks = int(
            os.getenv("RA_MAX_EXTRACT_CHUNKS", s.max_extract_chunks))
        s.strong_edge_min_conf = _env_float(
            "RA_STRONG_EDGE_MIN_CONF", s.strong_edge_min_conf)
        s.http_timeout = int(os.getenv("RA_HTTP_TIMEOUT", s.http_timeout))
        s.global_merge_enabled = _env_bool("RA_GLOBAL_MERGE", s.global_merge_enabled)
        s.global_merge_interval_nodes = int(os.getenv(
            "RA_GLOBAL_MERGE_INTERVAL_NODES", s.global_merge_interval_nodes))
        db = os.getenv("RA_DB_PATH")
        if db:
            s.db_path = Path(db)
        # ---- 章节级工作流（写作台 → 工作规划节点）----
        s.section_sufficiency_threshold = _env_float(
            "RA_SECTION_SUFFICIENCY_THRESHOLD", s.section_sufficiency_threshold)
        s.section_max_collection_rounds = int(os.getenv(
            "RA_SECTION_MAX_COLLECTION_ROUNDS", s.section_max_collection_rounds))
        s.section_sufficiency_min_papers = int(os.getenv(
            "RA_SECTION_MIN_PAPERS", s.section_sufficiency_min_papers))
        s.section_adaptive_paper_floor = os.getenv(
            "RA_SECTION_ADAPTIVE_PAPER_FLOOR",
            str(s.section_adaptive_paper_floor)).strip().lower() not in (
                "0", "false", "no", "off")
        s.section_sufficiency_min_knowledge_ratio = _env_float(
            "RA_SECTION_MIN_KNOWLEDGE_RATIO",
            s.section_sufficiency_min_knowledge_ratio)
        s.section_sufficiency_min_condition_ratio = _env_float(
            "RA_SECTION_MIN_CONDITION_RATIO",
            s.section_sufficiency_min_condition_ratio)
        s.section_sufficiency_min_requirement_ratio = _env_float(
            "RA_SECTION_MIN_REQUIREMENT_RATIO",
            s.section_sufficiency_min_requirement_ratio)
        s.section_sufficiency_min_evidence = int(os.getenv(
            "RA_SECTION_MIN_EVIDENCE", s.section_sufficiency_min_evidence))
        s.section_requirement_unverifiable_ok = _env_bool(
            "RA_SECTION_REQUIREMENT_UNVERIFIABLE_OK",
            s.section_requirement_unverifiable_ok)
        s.section_extract_max_papers = int(os.getenv(
            "RA_SECTION_EXTRACT_MAX_PAPERS", s.section_extract_max_papers))
        s.section_extract_max_seconds = int(os.getenv(
            "RA_SECTION_EXTRACT_MAX_SECONDS", s.section_extract_max_seconds))
        s.section_allow_write_when_insufficient = _env_bool(
            "RA_SECTION_ALLOW_WRITE_WHEN_INSUFFICIENT",
            s.section_allow_write_when_insufficient)
        return s


settings = Settings.from_env()
