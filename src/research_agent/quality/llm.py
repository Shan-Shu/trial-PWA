"""质量评估节点 LLM（GLM 4.7 Flash）。

GLM 依据文献元数据给出各权威性子项评分与学科速度判断，A/T/Q 仍由代码按
既定公式计算与路由，保证规则可复现：
    A = 0.5*venue_factor + 0.3*h_factor + 0.2*citation_factor
    T = f(出版年份, 学科半衰期)
    Q = 0.6*A + 0.4*T
模型缺失或输出不可解析时回退确定性评分。
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from langchain_core.messages import HumanMessage

logger = logging.getLogger(__name__)


QUALITY_PROMPT_TEMPLATE = """你是科研文献质量评估专家。请基于下面的文献元数据 JSON，输出一个严格的 JSON 对象，不要包含任何额外文字、代码块标记或注释。字段与取值要求如下：

{
  "venue_quartile": "JCR/SCI 分区，Q1-Q4；若无法确定（如预印本、非 SCI 期刊）填 null",
  "venue_factor": 0-1 小数，表示期刊/出版社权威性，预印本默认 0.45-0.55,
  "h_factor": 0-1 小数或 null，表示作者团队学术影响力（综合 H 指数、团队规模、机构声誉）,
  "citation_factor": 0-1 小数或 null，表示被引情况（结合该领域同年份论文的相对被引位置）,
  "field_velocity": "fast|medium|slow"（该文所属学科前沿迭代速度）,
  "venue_note": "一句话说明分区判断依据，若缺失填 'Not available'",
  "rationale": "两句话以内的评估理由，简要解释各因子取值依据"
}

评分规则与标尺（请遵循以下基准，但不必机械照搬，可根据元数据实际情况微调）：

1. venue_factor：
   - 0.90-0.95：顶刊/顶会/学会旗舰（如 Nature、Science、NeurIPS 等）
   - 0.78-0.88：领域主流 Q1 期刊或 A 类会议
   - 0.65-0.75：一般 Q2 期刊或中等会议
   - 0.50-0.60：预印本、未知来源或低影响期刊
   - 若无任何信息，默认 0.5

2. h_factor：
   - 0.9 ≈ H≥60 的资深团队或知名机构
   - 0.7 ≈ H 20-40 的中坚团队
   - 0.5 ≈ H 5-15 的普通团队
   - 0.4 ≈ 明确可判断的新团队
   - 信息缺失或作者列表未提供时输出 null，不得估计团队真实影响力
   - 可结合作者数量、机构排名微调 ±0.05

3. citation_factor：
   - 前 1% ≈ 0.95，前 10% ≈ 0.8，前 50% ≈ 0.6，接近 0 引用 ≈ 0.3
   - 若元数据未提供 citation_count 或相对被引位置，输出 null
   - 不得估计真实被引次数、百分位或“按期刊水平应该有多少引用”
   - 缺失项由代码使用中性默认值，不改变缺失事实

4. field_velocity：
   - fast：AI/CS、量子计算等快速迭代领域
   - medium：生物医药/材料/化学等
   - slow：数学/基础理论/传统工程等
   - 依据期刊名称、标题关键词、会议主题综合判断

5. 重要约束：
   - 所有分值必须在 0-1 范围内，保留两位小数
   - null 只用于“数据缺失且不能可靠推断”的字段，不得用估计值伪装成真实数据
   - venue_quartile/h_factor/citation_factor 缺失时写 null，并在 rationale 中说明“缺失，代码使用中性默认”
   - 输出必须是合法 JSON，键名与顺序如上，不要有尾逗号

文献元数据：{payload}"""


def assess_with_llm(rec: dict[str, Any], model) -> dict[str, Any] | None:
    """返回 LLM 评分子项；失败返回 None（由调用方回退）。"""
    authors = rec.get("authors") or []
    payload = {
        "title": rec.get("title"),
        "venue": rec.get("venue"),
        "venue_issn": rec.get("venue_issn"),
        "source_type": rec.get("source_type"),
        "pub_year": rec.get("pub_year"),
        "citation_count": rec.get("citation_count"),
        "avg_h_index": rec.get("avg_h_index"),
        "author_h_indices": [a.get("h_index") for a in authors if a.get("h_index")],
        "author_count": len(authors),
    }
    prompt = QUALITY_PROMPT_TEMPLATE.replace(
        "{payload}", json.dumps(payload, ensure_ascii=False))
    try:
        msg = model.invoke([HumanMessage(content=prompt)])
        raw = getattr(msg, "content", str(msg))
        text = re.sub(r"^```(?:json)?\s*", "", str(raw).strip())
        text = re.sub(r"\s*```$", "", text).strip()
        s, e = text.find("{"), text.rfind("}")
        if s == -1 or e <= s:
            raise ValueError("未找到 JSON")
        data = json.loads(text[s:e + 1])
        if not isinstance(data, dict):
            raise ValueError("非对象")
        return data
    except Exception as exc:  # noqa: BLE001
        logger.warning("LLM 质量评估失败，回退规则评分: %s", exc)
        return None
