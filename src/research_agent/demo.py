"""多模型协作科研辅助 Agent —— 最小可运行演示。

图结构（fan-out / join）::

    START ──> 研究员A（模型 A：理论/方法视角） ─┐
    START ──> 研究员B（模型 B：实验/应用视角） ─┼──> 主持人（模型 C：综合成稿）──> END

用法:
    离线占位模式（默认，无需 API Key）:   uv run research-agent
    真实多模型模式:                       设置 RESEARCH_AGENT_REAL=1 及各角色提供商后运行

本文件同时是可复用模块：build_demo_graph(models) 接受任意三个 ChatModel，
可用于接入真实多模型（OpenAI + DeepSeek + Claude + Gemini 任意组合）。
"""
from __future__ import annotations

import operator
import os
from typing import Annotated, Any, TypedDict

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.graph import END, START, StateGraph

from research_agent.models import build_chat_model


class LocalChatModel(BaseChatModel):
    """确定性本地占位模型：不联网、不需要 API Key，仅用于验证图编排与数据流。"""

    response: str = "（本地占位输出）"

    @property
    def _llm_type(self) -> str:
        return "local-demo"

    def _generate(self, messages: list, stop: list[str] | None = None,
                  run_manager: Any = None, **kwargs: Any) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=self.response))])


# ---------------- 图状态 ----------------
class ResearchState(TypedDict, total=False):
    """图状态。drafts 使用 operator.add 归约，天然支持并行写回。"""
    topic: str
    drafts: Annotated[list[str], operator.add]
    final: str


DEFAULT_TOPIC = "大语言模型智能体（Agent）在科研工作流中的应用：机遇与挑战"


def build_demo_graph(models: dict[str, BaseChatModel]):
    """由三个 ChatModel 组装多模型协作图。models 需含 worker_a / worker_b / chair 三键。"""

    def researcher_a(state: ResearchState) -> dict:
        prompt = (
            f"科研主题：{state['topic']}\n\n"
            "你是一名【理论/方法学】研究员。请从算法架构、prompt 工程、"
            "可解释性与评测方法等角度，给出 3 点有洞察的分析。"
        )
        out = models["worker_a"].invoke([HumanMessage(content=prompt)])
        return {"drafts": [f"[研究员A - 理论/方法视角]\n{out.content}"]}

    def researcher_b(state: ResearchState) -> dict:
        prompt = (
            f"科研主题：{state['topic']}\n\n"
            "你是一名【实验/应用】研究员。请从典型实验设计、落地案例、"
            "数据与计算资源约束等角度，给出 3 点有洞察的分析。"
        )
        out = models["worker_b"].invoke([HumanMessage(content=prompt)])
        return {"drafts": [f"[研究员B - 实验/应用视角]\n{out.content}"]}

    def chair(state: ResearchState) -> dict:
        parts = "\n\n".join(f"草稿 {i + 1}:\n{d}" for i, d in enumerate(state["drafts"]))
        prompt = (
            f"科研主题：{state['topic']}\n\n"
            "你是科研主持/主编。请整合下面两位研究员的草稿，去重、组织为一份"
            "结构化的中文综述（含 小节标题 + 要点 + 简短结论），并标注两视角的分歧点（如有）。\n\n"
            f"{parts}"
        )
        out = models["chair"].invoke([HumanMessage(content=prompt)])
        return {"final": str(out.content)}

    g = StateGraph(ResearchState)
    g.add_node("researcher_a", researcher_a)
    g.add_node("researcher_b", researcher_b)
    g.add_node("chair", chair)

    # 并行 fan-out
    g.add_edge(START, "researcher_a")
    g.add_edge(START, "researcher_b")
    # 收敛 join：两条边都完成后 chair 才会执行
    g.add_edge("researcher_a", "chair")
    g.add_edge("researcher_b", "chair")
    g.add_edge("chair", END)
    return g.compile()


def _role_model(role: str, use_real: bool) -> BaseChatModel:
    provider = os.getenv(f"{role.upper()}_PROVIDER", "openai")
    if use_real:
        return build_chat_model(provider=provider)
    return LocalChatModel(
        response=(
            f"（本地占位 · {role} · provider={provider}）\n"
            "1) 示例要点一：建议在真实模式下让该模型结合 arXiv/Tavily 检索回答；\n"
            "2) 示例要点二：本输出用于验证 LangGraph 图编排与并行数据流是否正常。"
        )
    )


def run_demo(topic: str | None = None) -> dict:
    """运行演示。返回最终状态（含 final 成稿）。"""
    topic = (topic or os.getenv("DEMO_TOPIC") or DEFAULT_TOPIC).strip()
    use_real = os.getenv("RESEARCH_AGENT_REAL", "0").strip() == "1"

    models = {
        "worker_a": _role_model("worker_a", use_real),
        "worker_b": _role_model("worker_b", use_real),
        "chair": _role_model("chair", use_real),
    }

    print("=" * 70)
    print("多模型协作研究 Agent（LangGraph 演示）")
    print(f"主题: {topic}")
    print(f"模式: {'真实模型' if use_real else '离线占位（无 API Key 也可运行）'}")
    if use_real:
        for role, m in models.items():
            print(f"  {role:<10} -> {type(m).__name__}")
    print("=" * 70)

    app = build_demo_graph(models)
    result = app.invoke({"topic": topic, "drafts": []})

    print("\n----- 研究员草稿 -----")
    for d in result["drafts"]:
        print(d)
        print("-" * 50)
    print("\n----- 主持人最终综述 -----")
    print(result["final"])
    print("\n[OK] 图执行成功：fan-out 并行 -> join 汇总 数据流正常。")
    return result


if __name__ == "__main__":
    run_demo()
