"""research-agent：基于 LangGraph 的多模型协作科研辅助 Agent（环境骨架）。"""
from __future__ import annotations

__version__ = "0.4.4"


def main() -> None:
    """CLI 入口：运行多模型协作演示（`uv run research-agent`）。"""
    from research_agent.demo import run_demo

    run_demo()


if __name__ == "__main__":
    main()
