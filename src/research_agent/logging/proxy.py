"""模型的透明日志代理：不调用点一行代码，也能拿到"谁在调、多慢、多大"。

**动机**：裸 ``model.invoke(...)`` 散布在十几个模块里，逐个改写既啰嗦又容易
漏。给 ``build_role_model`` 的返回值套一层代理后，**所有**调用自动落入统一
事件日志（``llm.call.start/.end/.error``），耗时、输入输出字符数、角色、模型
一目了然；失败时带异常类型与消息，不必重新复现。

**边界**：代理只认 ``invoke``，管理链条的操作（``bind_tools``、``with_config``
等）与其余属性一律透传，因此对 LangChain/LangGraph 是透明的。

**隐私**：提示词与返回内容一律**不落盘**，只记字符数。
"""
from __future__ import annotations

import inspect
import os
from typing import Any

#: 由"调用点所在文件"推断节点名——无需每个调用点都写明。
#: 键是文件名（含 .py），值是节点名；未命中则用角色名兜底。
NODE_HINTS: dict[str, str] = {
    "interview_loop.py": "interview",
    "section_compose.py": "content_builder",
    "section_graph.py": "content_builder",
    "section_service.py": "content_builder",
    "section_judge.py": "interview",
    "gap_planner.py": "planner",
    "collaboration.py": "collection",
    "service.py": "planner",
    "planner.py": "planner",
    "consumer.py": "knowledge_consumer",
    "content.py": "content_builder",
    "reviewer.py": "reviewer",
    "fact_check.py": "fact_checker",
    "extractor.py": "quality",
    "retrieval": "retrieval",
    "quality": "quality",
    "writer.py": "content_builder",
    "demo.py": "demo",
}


#: 角色 → 节点名（栈推断不出来时用它兜底，保证日志里 node 字段不为空）
ROLE_NODE: dict[str, str] = {
    "retriever": "retrieval",
    "quality": "quality",
    "knowledge": "knowledge",
    "planner": "planner",
    "consumer": "knowledge_consumer",
    "content": "content_builder",
    "review": "reviewer",
    "fact_check": "fact_checker",
}


def node_for_caller(depth: int = 3) -> str:
    """从调用栈里推断节点名（找不到就返回空串）。"""
    try:
        stack = inspect.stack()
    except Exception:  # noqa: BLE001 —— 某些精简运行时没有栈
        return ""
    try:
        for frame in stack[depth:depth + 4]:
            name = os.path.basename(frame.filename or "")
            if name in NODE_HINTS:
                return NODE_HINTS[name]
            stem = os.path.splitext(name)[0]
            if stem in NODE_HINTS:
                return NODE_HINTS[stem]
    finally:
        del stack
    return ""


def resolve_node(role: str, node: str = "", depth: int = 4) -> str:
    """节点名解析顺序：显式指定 → 调用点推断 → 角色映射。"""
    return node or node_for_caller(depth) or ROLE_NODE.get(role, "") or role


class LoggedModel:
    """``invoke`` 落日志、其余透传的模型代理。"""

    def __init__(self, model: Any, role: str = "", node: str = "",
                 db: Any = None) -> None:
        self._model = model
        self._role = role
        self._node = node
        self._db = db

    # -------------------------------------------------------- 透明属性
    def __getattr__(self, name: str) -> Any:
        # 只在实例字典里找不到时才走到这里（_model 等已在 __init__ 里）
        return getattr(self._model, name)

    def __repr__(self) -> str:  # pragma: no cover —— 仅调试
        return f"LoggedModel({self._model!r}, role={self._role!r})"

    # -------------------------------------------------------- 记录调用
    def invoke(self, input: Any, *args: Any, **kwargs: Any) -> Any:
        from research_agent.logging import log_event, truncate
        import time

        role = self._role
        node = resolve_node(role, self._node)
        model_name = (str(getattr(self._model, "model_name", "") or "")
                      or str(getattr(self._model, "model", "") or ""))
        prompt_chars = _chars(input)
        started = time.monotonic()
        log_event("llm.call.start", node=node, db=self._db,
                  data={"role": role, "model": model_name,
                        "prompt_chars": prompt_chars})
        try:
            out = self._model.invoke(input, *args, **kwargs)
        except BaseException as exc:  # noqa: BLE001 —— 记现场后原样抛出
            log_event("llm.call.error", node=node, level="ERROR", db=self._db,
                      ms=(time.monotonic() - started) * 1000,
                      data={"role": role, "model": model_name,
                            "prompt_chars": prompt_chars,
                            "error": truncate(f"{type(exc).__name__}: {exc}",
                                              300)})
            raise
        log_event("llm.call.end", node=node, db=self._db,
                  ms=(time.monotonic() - started) * 1000,
                  data={"role": role, "model": model_name,
                        "prompt_chars": prompt_chars,
                        "output_chars": _chars(getattr(out, "content", out))})
        return out

    async def ainvoke(self, input: Any, *args: Any, **kwargs: Any) -> Any:
        """异步调用同样记录（KPI 与同步一致）。"""
        from research_agent.logging import log_event, truncate
        import time

        role = self._role
        node = resolve_node(role, self._node)
        model_name = (str(getattr(self._model, "model_name", "") or "")
                      or str(getattr(self._model, "model", "") or ""))
        prompt_chars = _chars(input)
        started = time.monotonic()
        log_event("llm.call.start", node=node, db=self._db,
                  data={"role": role, "model": model_name,
                        "prompt_chars": prompt_chars, "async": True})
        try:
            out = await self._model.ainvoke(input, *args, **kwargs)
        except BaseException as exc:  # noqa: BLE001
            log_event("llm.call.error", node=node, level="ERROR", db=self._db,
                      ms=(time.monotonic() - started) * 1000,
                      data={"role": role, "model": model_name,
                            "prompt_chars": prompt_chars,
                            "error": truncate(f"{type(exc).__name__}: {exc}",
                                              300)})
            raise
        log_event("llm.call.end", node=node, db=self._db,
                  ms=(time.monotonic() - started) * 1000,
                  data={"role": role, "model": model_name,
                        "prompt_chars": prompt_chars, "async": True,
                        "output_chars": _chars(getattr(out, "content", out))})
        return out


def _chars(value: Any) -> int:
    """估算输入/输出规模：只记字符数，绝不落内容。"""
    if value is None:
        return 0
    if isinstance(value, str):
        return len(value)
    if isinstance(value, (list, tuple)):
        total = 0
        for item in value:
            content = getattr(item, "content", None)
            total += len(content) if isinstance(content, str) else len(str(item))
        return total
    content = getattr(value, "content", None)
    if isinstance(content, str):
        return len(content)
    return len(str(value))


__all__ = ["LoggedModel", "NODE_HINTS", "ROLE_NODE", "node_for_caller",
           "resolve_node"]
