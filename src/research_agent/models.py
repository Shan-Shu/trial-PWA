"""多模型提供商工厂 + “角色 → LLM”绑定。

模型分工约定（用户指定）：
- 研究任务节点 Planner/Consumer/Content/Reviewer/FactCheck 统一使用 DeepSeek V4 Pro
- 数据构建节点继续按各自默认绑定运行
"""
from __future__ import annotations

import os

from dotenv import load_dotenv

# 加载项目根目录 .env（幂等）
load_dotenv()

SUPPORTED_PROVIDERS: dict[str, str] = {
    "openai": "OpenAI 官方 API",
    "deepseek": "DeepSeek 官方 API（V4 家族）",
    "qwen": "通义千问 DashScope（OpenAI 兼容模式）",
    "glm": "智谱 GLM（BigModel，OpenAI 兼容模式）",
    "anthropic": "Anthropic Claude",
    "google": "Google Gemini",
}

# 节点角色 → LLM 默认绑定（provider / model / 所需 API Key）
ROLE_PROVIDER = {
    "retriever": "deepseek",
    "quality": "deepseek",
    "knowledge": "deepseek",
    "planner": "deepseek",
    "consumer": "deepseek",
    "content": "deepseek",
    "review": "deepseek",
    "fact_check": "deepseek",
}
ROLE_MODEL_ENV = {
    "retriever": "RETRIEVAL_MODEL",
    "quality": "QUALITY_MODEL",
    "knowledge": "KNOWLEDGE_MODEL",
    "planner": "PLANNER_MODEL",
    "consumer": "CONSUMER_MODEL",
    "content": "CONTENT_MODEL",
    "review": "REVIEW_MODEL",
    "fact_check": "FACT_CHECK_MODEL",
}
ROLE_MODEL_DEFAULT = {
    "retriever": "deepseek-v4-flash",
    "quality": "deepseek-v4-flash",
    "knowledge": "deepseek-v4-flash",
    "planner": "deepseek-v4-pro",
    "consumer": "deepseek-v4-pro",
    "content": "deepseek-v4-pro",
    "review": "deepseek-v4-pro",
    "fact_check": "deepseek-v4-pro",
}
ROLE_KEY_ENV = {
    "retriever": "DEEPSEEK_API_KEY",
    "quality": "DEEPSEEK_API_KEY",
    "knowledge": "DEEPSEEK_API_KEY",
    "planner": "DEEPSEEK_API_KEY",
    "consumer": "DEEPSEEK_API_KEY",
    "content": "DEEPSEEK_API_KEY",
    "review": "DEEPSEEK_API_KEY",
    "fact_check": "DEEPSEEK_API_KEY",
}
ROLE_LABEL = {
    "retriever": "文献检索节点",
    "quality": "质量控制节点",
    "knowledge": "知识提取节点",
    "planner": "工作规划节点",
    "consumer": "知识消费节点",
    "content": "内容形成节点",
    "review": "审核校对节点",
    "fact_check": "事实核查节点",
}


def build_chat_model(
    provider: str | None = None,
    model_name: str | None = None,
    temperature: float = 0.2,
) -> object:
    """按提供商构建 ChatModel。

    参数:
        provider: 见 SUPPORTED_PROVIDERS；None 时取环境变量 LLM_PROVIDER，默认 openai。
        model_name: 覆盖默认模型名；None 时取对应 *_MODEL 环境变量。
        temperature: 采样温度。
    """
    provider = (provider or os.getenv("LLM_PROVIDER") or "openai").strip().lower()
    if provider not in SUPPORTED_PROVIDERS:
        raise ValueError(f"不支持的 provider: {provider}，可选: {list(SUPPORTED_PROVIDERS)}")

    if provider == "openai":
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(
            model=model_name or os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
            temperature=temperature,
        )

    if provider == "deepseek":
        from langchain_openai import ChatOpenAI

        kwargs: dict[str, object] = {
            "model": model_name or os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash"),
            "api_key": os.getenv("DEEPSEEK_API_KEY"),
            "base_url": "https://api.deepseek.com",
            "temperature": temperature,
            "max_tokens": int(os.getenv("DEEPSEEK_MAX_TOKENS", "8000")),
            "max_retries": 5,
        }
        # v0.4.1：默认不再发送 reasoning_effort。
        # 实测（内容形成节点，6 万字符提示词）显式传 reasoning_effort="low" 时
        # 请求长时间无响应（>15 分钟），不传该参数时同一提示词约 6 分钟返回。
        # 需要时可显式设置 DEEPSEEK_REASONING_EFFORT 重新启用。
        effort = os.getenv("DEEPSEEK_REASONING_EFFORT")
        if effort:
            kwargs["reasoning_effort"] = effort
        return ChatOpenAI(**kwargs)

    if provider == "qwen":
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(
            model=model_name or os.getenv("QWEN_MODEL", "qwen-plus"),
            api_key=os.getenv("DASHSCOPE_API_KEY"),
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            temperature=temperature,
        )

    if provider == "glm":
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(
            model=model_name or os.getenv("GLM_MODEL", "glm-4.7-flash"),
            api_key=os.getenv("ZHIPU_API_KEY") or os.getenv("GLM_API_KEY"),
            base_url="https://open.bigmodel.cn/api/paas/v4",
            temperature=temperature,
            max_tokens=8000,
            max_retries=5,
        )

    if provider == "anthropic":
        from langchain_anthropic import ChatAnthropic

        return ChatAnthropic(
            model=model_name or os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-5"),
            api_key=os.getenv("ANTHROPIC_API_KEY"),
            temperature=temperature,
        )

    if provider == "google":
        from langchain_google_genai import ChatGoogleGenerativeAI

        return ChatGoogleGenerativeAI(
            model=model_name or os.getenv("GOOGLE_MODEL", "gemini-2.5-flash"),
            api_key=os.getenv("GOOGLE_API_KEY"),
            temperature=temperature,
        )

    raise AssertionError("unreachable")  # pragma: no cover


def build_role_model(
    role: str,
    provider: str | None = None,
    model: str | None = None,
    temperature: float = 0.2,
) -> object:
    """构建“节点角色”绑定的 LLM。

    role: retriever | quality | knowledge | planner | consumer | content |
          review | fact_check。
    默认按 ROLE_PROVIDER/ROLE_MODEL_DEFAULT 绑定目标模型；可用环境变量
    RETRIEVAL_MODEL / QUALITY_MODEL / ... / FACT_CHECK_MODEL 覆盖具体 model id。
    """
    if role not in ROLE_PROVIDER:
        raise ValueError(f"未知角色: {role}，可选: {list(ROLE_PROVIDER)}")
    key_env = ROLE_KEY_ENV[role]
    if not os.getenv(key_env):
        raise ValueError(
            f"角色 {role}({ROLE_LABEL[role]}) 缺少 API Key，请在 .env 中设置 {key_env}"
        )
    provider = provider or os.getenv(
        "ROLE_PROVIDER_" + role.upper(), ROLE_PROVIDER[role]
    )
    model = (model or os.getenv(ROLE_MODEL_ENV[role], ROLE_MODEL_DEFAULT[role])).strip()
    return build_chat_model(provider=provider, model_name=model, temperature=temperature)


def role_model_binding(role: str) -> dict[str, str]:
    """返回角色的解析绑定信息（不含 Key），便于日志/看板展示。"""
    return {
        "role": role,
        "label": ROLE_LABEL[role],
        "provider": os.getenv("ROLE_PROVIDER_" + role.upper(), ROLE_PROVIDER[role]),
        "model": os.getenv(ROLE_MODEL_ENV[role], ROLE_MODEL_DEFAULT[role]),
        "key_env": ROLE_KEY_ENV[role],
        "has_key": bool(os.getenv(ROLE_KEY_ENV[role])),
    }
