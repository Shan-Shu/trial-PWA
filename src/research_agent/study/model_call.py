"""研究任务节点的模型调用封装：带超时，避免单个请求拖死整条链路。

背景（v0.4.1 实测）：推理型模型在长结构化输出上可能长时间不返回
（内容形成节点一次请求曾 >15 分钟无响应）。LangChain 的 ``invoke`` 没有
请求级超时，因此这里用守护线程 + ``join(timeout)`` 做硬超时：

- 超时或异常都返回 ``None``，由各节点回退到确定性实现；
- 超时不会杀掉底层 HTTP 请求（Python 线程无法中断），但调用方立刻可以继续，
  保证图不会无限阻塞；
- 返回诊断信息（耗时、是否超时、错误）供事件日志记录。
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any

from langchain_core.messages import HumanMessage

from research_agent.logging import log_event as dsh_log

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 600


def invoke_with_timeout(model: Any, prompt: str,
                        timeout: int | None = None,
                        *, node: str = "", role: str = "",
                        db: Any = None) -> tuple[str | None, dict[str, Any]]:
    """调用模型并返回 ``(原始文本, 诊断信息)``；失败/超时返回 ``(None, 诊断)``。

    ``node``/``role``/``db`` 只用于**统一事件日志**：一次调用落
    ``llm.call.start / .end / .error`` 三条事件，带耗时与输入输出字符数，
    这样"这个节点到底卡在模型上还是卡在检索上"一眼可辨。提示词全文不落盘。
    """
    diag: dict[str, Any] = {"timeout": None, "elapsed": 0.0, "error": None,
                            "timed_out": False}
    if model is None:
        diag["error"] = "model 未配置"
        return None, diag
    limit = max(1, int(timeout or DEFAULT_TIMEOUT))
    diag["timeout"] = limit
    box: dict[str, Any] = {}
    model_name = (str(getattr(model, "model_name", "") or "")
                  or str(getattr(model, "model", "") or ""))
    dsh_log("llm.call.start", node=node, db=db,
            data={"role": role, "model": model_name,
                  "prompt_chars": len(prompt or ""), "timeout": limit})

    def call() -> None:
        try:
            msg = model.invoke([HumanMessage(content=prompt)])
            box["content"] = getattr(msg, "content", str(msg))
        except Exception as exc:  # noqa: BLE001
            box["error"] = str(exc)

    started = time.time()
    thread = threading.Thread(target=call, daemon=True)
    thread.start()
    thread.join(timeout=limit)
    diag["elapsed"] = round(time.time() - started, 1)
    if thread.is_alive():
        diag["timed_out"] = True
        diag["error"] = f"模型调用超时（>{limit}s）"
        dsh_log("llm.call.error", node=node, level="ERROR", db=db,
                ms=diag["elapsed"] * 1000,
                data={"role": role, "model": model_name, "timed_out": True,
                      "prompt_chars": len(prompt or ""),
                      "error": diag["error"]})
        logger.warning("模型调用超时: %.0fs（prompt %d 字符）", diag["elapsed"],
                       len(prompt or ""))
        return None, diag
    if box.get("error"):
        diag["error"] = box["error"]
        dsh_log("llm.call.error", node=node, level="ERROR", db=db,
                ms=diag["elapsed"] * 1000,
                data={"role": role, "model": model_name,
                      "prompt_chars": len(prompt or ""),
                      "error": box["error"]})
        logger.warning("模型调用失败: %s", box["error"])
        return None, diag
    content = box.get("content")
    if content is None:
        diag["error"] = "模型返回空内容"
        return None, diag
    dsh_log("llm.call.end", node=node, db=db, ms=diag["elapsed"] * 1000,
            data={"role": role, "model": model_name,
                  "prompt_chars": len(prompt or ""),
                  "output_chars": len(str(content))})
    return str(content), diag
