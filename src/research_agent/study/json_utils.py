"""研究任务层共用的 JSON 解析工具。"""
from __future__ import annotations

import json
import re
from typing import Any


def parse_json_object(raw: Any) -> dict[str, Any] | None:
    """从模型文本中提取第一个 JSON 对象；失败返回 None。"""
    text = (raw or "").strip()
    if not text:
        return None
    text = re.sub(r"^```(?:json)?\s*", "", text).strip()
    text = re.sub(r"\s*```$", "", text).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def clean_str(value: Any, fallback: str = "") -> str:
    text = str(value or "").strip()
    return text or fallback
