"""检索节点 LLM（DeepSeek V4）：查询规划 + 元数据规整。

确定性 API（arXiv/OpenAlex/Crossref）负责真正的检索与下载，
LLM 负责「动脑」的部分：
1. plan_queries：把用户研究主题拆解为多条更精准的检索式；
2. clean_metadata：把多源原始元数据规整为规范字段，并尽力补全
   作者/单位/发表情况/DOI（供质量节点校验，缺漏再回补）。

模型缺失或输出不可解析时，调用方回退到原确定性实现，不影响流程。
"""
from __future__ import annotations

import json
import logging
import re
from datetime import date
from typing import Any

from langchain_core.messages import HumanMessage

logger = logging.getLogger(__name__)


def _parse_first_json(text: str) -> Any | None:
    """容忍解析模型输出中的首个 JSON 对象/数组。"""
    t = (text or "").strip()
    t = re.sub(r"^```(?:json)?\s*", "", t).strip()
    t = re.sub(r"\s*```$", "", t).strip()
    for open_ch, close_ch in (("{", "}"), ("[", "]")):
        s, e = t.find(open_ch), t.rfind(close_ch)
        if s != -1 and e > s:
            try:
                return json.loads(t[s:e + 1])
            except json.JSONDecodeError:
                continue
    return None


PLAN_PROMPT_TEMPLATE = """你是科研文献检索规划器。用户/规划节点会给出一个简短研究主题。
工作规划节点已经提供分析维度，请围绕这些维度生成英文检索式，让系统逐个执行检索，
全面获取该领域相关文献，而不是套用任何固定的生物医药或材料模板。

分析维度：
{dimensions_block}

要求：
1. 生成 3-6 条英文检索式；窄主题至少 3 条。
2. 每条检索式对应一个或两个紧密相关的分析维度，不要混入无关维度。
3. 若未提供分析维度，请只依据主题自行拆解不同角度，禁止套用固定领域维度表。
4. 每条检索式应为完整英文查询，可用 AND/OR/NOT、双引号与通配符。
5. 避免不同检索式大量重复关键词。
6. 只输出 JSON 数组字符串，不输出解释、注释或代码块。
7. 检索会投递到 PubMed / Europe PMC / OpenAlex / Semantic Scholar / arXiv；
   避免 site:/[Title/Abstract]/[dp] 等单一平台操作符。
8. 当前日期：{date}。

主题：{topic}"""


CLEAN_PROMPT_TEMPLATE = """你是文献元数据规整器。请根据下面的原始元数据 JSON，输出规范化后的 JSON 对象。

输出格式必须严格符合以下结构：
{
  "title": "标准标题",
  "venue": "期刊/会议/预印本库名",
  "venue_issn": "ISSN 或 null",
  "source_type": "journal|repository|proceedings",
  "publication_status": "Published|Preprint|In Press",
  "pub_year": 2025,
  "pub_date": "YYYY-MM-DD 或 null",
  "doi": "DOI 或 null",
  "authors": [{"name": "...", "orcid": null, "affiliations": ["机构全称"]}]
}

处理规则：
1. 标题和期刊名标准化大小写：标题采用每个主要单词首字母大写（Title Case），保留专有名词和缩写原样；期刊名保持官方大小写（如已知），否则使用 Title Case。
2. 作者信息解析：
   - 若原始元数据中作者缺失或为空，则输出 [{"name": "Unknown", "orcid": null, "affiliations": []}]。
   - 若作者姓名格式为 "Last, First" 或 "First Last"，统一转换为 "First Last"（姓名顺序）。
   - 作者 affiliations 应提取为字符串数组，每个元素为机构全称；若有多位作者，保持原始顺序。
   - orcid 仅在明确提供时填写，否则为 null。
3. 对于 venue_issn 和 doi：只使用原始元数据中明确给出的值；若无法确定，必须输出 null，不得编造。
4. source_type 判断优先级：若元数据包含期刊信息且 DOI 以 10.xxxx 开头且发布在期刊平台，则为 "journal"；若来自 arXiv/bioRxiv/medRxiv 等预印本库，则为 "repository"；若来自会议论文集（如 ACM/IEEE 会议），则为 "proceedings"；无法判断时根据元数据中的 container 字段推断，仍然不确定则输出 "journal"（或 null？建议选择最可能的并可在后续人工复核）。
5. publication_status 判断：若元数据中有明确的出版状态标签（如 "Published"、"Preprint"、"In Press"），直接采用；否则根据发布日期和 DOI 是否存在推断：有 DOI 且有正式卷期页码 → "Published"；来自预印本库且无 DOI → "Preprint"；有接收日期无出版日期 → "In Press"。
6. pub_year 必须为四位整数，若无法确定年份则输出 null（而非 0）。
7. pub_date 格式为 "YYYY-MM-DD"，若只有年份或月份，可补全为当年1月1日或当月1日，并在后续人工校验；完全缺失则 null。
8. 只输出 JSON 对象，不要包含任何额外文字、注释或代码块标记。

原始元数据：{payload}"""


class RetrievalLLM:
    """包装检索节点绑定的 LLM（默认 DeepSeek V4）。"""

    def __init__(self, model, max_queries: int = 6) -> None:
        self.model = model
        self.max_queries = max_queries

    # ---- 1) 查询规划 ----
    def plan_queries(self, topic: str,
                     dimensions: list[str] | None = None) -> list[str]:
        """基于研究主题与工作规划节点给出的领域维度生成检索式。"""
        if dimensions:
            dimensions_block = "\n".join(f"- {d}" for d in dimensions)
        else:
            dimensions_block = (
                "（未提供固定领域维度。请只依据主题生成检索式，"
                "不要套用任何固定的领域模板。）"
            )
        prompt = PLAN_PROMPT_TEMPLATE.replace("{topic}", topic).replace(
            "{date}", date.today().isoformat()).replace(
                "{dimensions_block}", dimensions_block)
        try:
            msg = self.model.invoke([HumanMessage(content=prompt)])
            raw = getattr(msg, "content", str(msg))
            parsed = _parse_first_json(raw)
            if isinstance(parsed, list):
                queries = [str(q).strip() for q in parsed if str(q).strip()]
            else:
                queries = [ln.strip() for ln in str(raw).splitlines()
                           if ln.strip() and not ln.startswith(("```", "["))]
            out: list[str] = []
            for q in queries:
                if q not in out:
                    out.append(q)
            if not out:
                out = [topic]
            return out[:self.max_queries]
        except Exception as exc:  # noqa: BLE001
            logger.warning("LLM 查询规划失败，回退原主题: %s", exc)
            return [topic]

    # ---- 2) 元数据规整/补全 ----
    def clean_metadata(self, rec: dict[str, Any]) -> dict[str, Any] | None:
        """输入多源原始元数据，返回规范化记录（缺失字段尽力补全）。"""
        payload = {
            "title": rec.get("title"), "doi": rec.get("doi"),
            "venue": rec.get("venue"), "venue_issn": rec.get("venue_issn"),
            "source_type": rec.get("source_type"),
            "publication_status": rec.get("publication_status"),
            "pub_year": rec.get("pub_year"), "pub_date": rec.get("pub_date"),
            "authors": rec.get("authors"),
        }
        prompt = CLEAN_PROMPT_TEMPLATE.replace(
            "{payload}", json.dumps(payload, ensure_ascii=False))
        try:
            msg = self.model.invoke([HumanMessage(content=prompt)])
            parsed = _parse_first_json(getattr(msg, "content", str(msg)))
            if isinstance(parsed, dict):
                return parsed
        except Exception as exc:  # noqa: BLE001
            logger.warning("LLM 元数据规整失败: %s", exc)
        return None
