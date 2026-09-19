"""research_agent：研究引擎（从 trial 原样搬入，见 VERSIONS.md 的来源说明）。

这里只保留引擎本体；界面与适配层在 `desk` 包。
"""
from __future__ import annotations

#: 本项目版本（research-desk）。引擎来源版本见 VERSIONS.md：
#: trial / research-agent v0.4.5 @ 36a14fe（已冻结，tag frozen-before-research-desk）
__version__ = "0.1.1"


def main() -> None:
    """CLI 入口：运行多模型协作演示（`uv run research-agent`）。"""
    from research_agent.demo import run_demo

    run_demo()


if __name__ == "__main__":
    main()
