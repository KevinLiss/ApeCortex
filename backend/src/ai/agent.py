"""AI chat agent: orchestrates LLM calls, tool execution, and streaming.

Flow:
1. User message → LLM (with system tools as function definitions)
2. LLM responds with tool_calls → execute tools → feed results back
3. LLM generates final natural-language response
4. Stream the response to client via SSE
"""

import json
import re
from typing import Any, AsyncGenerator

import httpx
from loguru import logger

from src.ai.tools import build_tools_for_llm, execute_tool
from src.core.crypto import decrypt_api_key
from src.core.deps import get_user_permissions
from src.db import SessionLocal
from src.models import User
from src.models.ai import AiProvider


# ---------------------------------------------------------------------------
# LLM HTTP 错误 → 用户可读中文提示
# 供 _call_llm / _call_llm_stream 共用；未命中时回退原始报错（保留诊断信息）。
# ---------------------------------------------------------------------------
_LLM_ERROR_MAP: dict[int, str] = {
    401: "模型密钥无效或已过期，请到「AI 助手 → 模型密钥管理」检查 Key",
    402: "模型账户余额不足，请充值后重试（后台→模型密钥管理可换供应商）",
    403: "模型访问被拒绝：该 Key 无权调用此模型，或账号已被封禁",
    404: "模型不存在或接口地址不正确，请检查供应商 Base URL 与模型名",
    429: "请求过于频繁或超出限流，请稍后重试",
    500: "模型服务内部错误，请稍后重试或切换其他模型",
    502: "模型服务不可用（网关错误），请稍后重试",
    503: "模型服务暂时不可用（过载/维护中），请稍后重试",
    504: "模型服务响应超时，请稍后重试",
}


def _friendly_llm_error(status_code: int, error_body: str) -> str:
    """把 LLM HTTP 错误转成给用户看的中文提示。

    未识别的状态码原样保留；正文截断到 200 字符防长 JSON 刷屏。
    """
    friendly = _LLM_ERROR_MAP.get(status_code)
    if not friendly:
        return f"模型接口返回 {status_code}: {error_body[:200]}"
    return f"{friendly}（接口返回 {status_code}）"


def _clean_error_body(error_body: str) -> str:
    """从 JSON 错误体里提取 message 字段，其余噪声截断。"""
    m = re.search(r'"message"\s*:\s*"([^"]{1,200})', error_body)
    return m.group(1) if m else error_body[:200] if error_body else "(空响应)"

# Provider default base URLs and model mappings
PROVIDER_DEFAULTS: dict[str, dict[str, str]] = {
    "deepseek": {"base_url": "https://api.deepseek.com", "default_model": "deepseek-chat"},
    "qwen": {"base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "default_model": "qwen-plus"},
    "glm": {"base_url": "https://open.bigmodel.cn/api/paas/v4", "default_model": "glm-4-flash"},
    "minimax": {"base_url": "https://api.minimaxi.com/v1", "default_model": "MiniMax-M2.5"},
    "openai": {"base_url": "https://api.openai.com/v1", "default_model": "gpt-4o-mini"},
    "custom": {"base_url": "", "default_model": ""},
}

SYSTEM_PROMPT = """你是 ApeAdmin 智能管理助手，可以帮助用户通过自然语言操作后台管理系统。

你的能力：
1. 用户管理：查询用户列表、查看用户详情、创建用户、更新用户信息、删除用户
2. 角色管理：查询角色列表、创建角色、更新角色、删除角色
3. 部门管理：查询部门树、创建部门、更新部门、删除部门
4. 菜单管理：查询菜单树、创建菜单项、更新菜单、删除菜单
5. 系统工具：系统健康检查、查看已安装插件列表、系统统计信息
6. 插件市场：搜索市场插件、查看插件详情、查询开发者信息、市场统计

使用规则：
- 当用户的请求涉及系统操作时，调用对应的工具完成任务
- 工具调用后，用简洁的中文总结执行结果
- 如果权限不足，告知用户并说明需要什么权限
- 对于普通对话，直接友好地回答
- 创建/更新操作时，如果用户未提供必填参数，先询问用户
"""


def _get_provider_config(provider: AiProvider) -> dict[str, str]:
    """Get base_url and default model for a provider.

    default_model 回退链：provider_type 预设 → 供应商 models 列表首项。
    （MiniMax 等新供应商落库时 provider_type 可能未带预设，models 首项兜底，
    避免「未选模型」时把 deepseek 模型名发给 MiniMax 导致 400 unknown model。）
    """
    defaults = PROVIDER_DEFAULTS.get(provider.provider_type, PROVIDER_DEFAULTS["custom"])
    base_url = provider.base_url or defaults["base_url"]
    default_model = defaults["default_model"]
    if not default_model:
        models_list = provider.models or "[]"
        try:
            parsed = json.loads(models_list) if isinstance(models_list, str) else models_list
            if parsed:
                default_model = parsed[0]
        except (json.JSONDecodeError, TypeError, IndexError):
            pass
    return {"base_url": base_url, "default_model": default_model}


def _build_tools_for_llm(tools_enabled: bool) -> list[dict[str, Any]]:
    """Return the tools list for LLM function calling."""
    return build_tools_for_llm(tools_enabled)


async def _call_llm(
    messages: list[dict[str, Any]],
    api_key: str,
    base_url: str,
    model: str,
    max_tokens: int = 2000,
    temperature: float = 0.7,
    tools: list[dict[str, Any]] | None = None,
    stream: bool = False,
) -> dict[str, Any]:
    """Call LLM API (OpenAI-compatible format). Returns full response JSON."""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    body: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": stream,
    }
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"

    url = f"{base_url.rstrip('/')}/chat/completions"
    async with httpx.AsyncClient(timeout=120.0, trust_env=False) as client:
        try:
            resp = await client.post(url, headers=headers, json=body)
        except httpx.TimeoutException as exc:
            logger.error(f"LLM API 超时 {url}: {exc}")
            raise RuntimeError("模型服务响应超时（120 秒），请稍后重试或切换其他模型") from exc
        except httpx.HTTPError as exc:
            logger.error(f"LLM API 连接失败 {url}: {exc}")
            raise RuntimeError(f"无法连接模型服务，请检查供应商地址是否可达（{exc.__class__.__name__}）") from exc
        if resp.status_code >= 400:
            error_body = resp.text
            logger.error(f"LLM API 调用失败 [{resp.status_code}] {url}: {error_body[:500]}")
            raise RuntimeError(_friendly_llm_error(resp.status_code, _clean_error_body(error_body)))
        return resp.json()


async def _call_llm_stream(
    messages: list[dict[str, Any]],
    api_key: str,
    base_url: str,
    model: str,
    max_tokens: int = 2000,
    temperature: float = 0.7,
    tools: list[dict[str, Any]] | None = None,
) -> AsyncGenerator[str, None]:
    """Call LLM API in streaming mode, yield SSE data chunks."""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    body: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": True,
    }
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"

    url = f"{base_url.rstrip('/')}/chat/completions"
    async with httpx.AsyncClient(timeout=120.0, trust_env=False) as client:
        try:
            stream_ctx = client.stream("POST", url, headers=headers, json=body)
        except httpx.TimeoutException as exc:
            logger.error(f"LLM API 建连超时 {url}: {exc}")
            raise RuntimeError("模型服务建连超时，请稍后重试或切换其他模型") from exc
        except httpx.HTTPError as exc:
            logger.error(f"LLM API 连接失败 {url}: {exc}")
            raise RuntimeError(f"无法连接模型服务，请检查供应商地址是否可达（{exc.__class__.__name__}）") from exc
        async with stream_ctx as resp:
            if resp.status_code >= 400:
                error_body = (await resp.aread()).decode("utf-8", errors="replace")
                logger.error(f"LLM API 流式调用失败 [{resp.status_code}] {url}: {error_body[:500]}")
                raise RuntimeError(_friendly_llm_error(resp.status_code, _clean_error_body(error_body)))
            async for line in resp.aiter_lines():
                if line.startswith("data: "):
                    chunk = line[6:]
                    if chunk.strip() == "[DONE]":
                        yield "[DONE]"
                        return
                    yield chunk


async def chat_non_stream(
    messages: list[dict[str, str]],
    provider: AiProvider,
    model: str | None = None,
    max_tokens: int = 2000,
    temperature: float = 0.7,
    enable_tools: bool = True,
    user: User | None = None,
) -> dict[str, Any]:
    """Non-streaming chat with tool execution loop.

    Returns {"content": str, "tool_calls": list, "usage": dict}
    """
    config = _get_provider_config(provider)
    api_key = decrypt_api_key(provider.api_key_enc)
    model_name = model or config["default_model"]
    tools = _build_tools_for_llm(enable_tools)

    # Build message list with system prompt
    llm_messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        *messages,
    ]

    # Get user permissions for tool execution
    user_permissions = get_user_permissions(user) if user else set()

    # Tool call loop (max 5 rounds)
    for _ in range(5):
        resp = await _call_llm(
            llm_messages, api_key, config["base_url"], model_name,
            max_tokens, temperature, tools,
        )
        msg = resp["choices"][0]["message"]

        # If no tool_calls, we're done
        if not msg.get("tool_calls"):
            return {
                "content": msg.get("content", ""),
                "tool_calls": [],
                "usage": resp.get("usage", {}),
            }

        # Execute each tool call
        llm_messages.append({
            "role": "assistant",
            "content": msg.get("content", ""),
            "tool_calls": msg["tool_calls"],
        })

        tool_results = []
        for tc in msg["tool_calls"]:
            fn_name = tc["function"]["name"]
            try:
                fn_args = json.loads(tc["function"]["arguments"])
            except json.JSONDecodeError:
                fn_args = {}

            # Execute tool in a DB session
            async with SessionLocal() as db:
                result = await execute_tool(fn_name, fn_args, db, user_permissions, user)

            tool_results.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": result,
            })
            logger.info(f"Tool executed: {fn_name} -> {result[:200]}")

        llm_messages.extend(tool_results)

    # Fallback: if we exhausted rounds, return last content
    return {"content": "抱歉，工具调用轮次过多，请简化您的请求。", "tool_calls": [], "usage": {}}


async def chat_stream(
    messages: list[dict[str, str]],
    provider: AiProvider,
    model: str | None = None,
    max_tokens: int = 2000,
    temperature: float = 0.7,
    enable_tools: bool = True,
    user: User | None = None,
    system_prompt: str | None = None,
) -> AsyncGenerator[str, None]:
    """Streaming chat with tool execution. Yields SSE-format data.

    Yields events like:
    - {"type":"content","content":"chunk text"}
    - {"type":"tool_call","name":"get_user_list","arguments":{...}}
    - {"type":"tool_result","name":"get_user_list","result":{...}}
    - {"type":"done"}

    ``system_prompt`` overrides the default admin-assistant prompt, which
    lets plugins (e.g. bot_saas) run their own persona instead.
    """
    config = _get_provider_config(provider)
    api_key = decrypt_api_key(provider.api_key_enc)
    model_name = model or config["default_model"]
    tools = _build_tools_for_llm(enable_tools)

    llm_messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt if system_prompt is not None else SYSTEM_PROMPT},
        *messages,
    ]

    user_permissions = get_user_permissions(user) if user else set()

    # Tool call loop (max 5 rounds)
    for round_num in range(5):
        # Collect streamed content and detect tool calls
        collected_content = ""
        collected_tool_calls: list[dict[str, Any]] = []
        has_tool_calls = False

        async for chunk_str in _call_llm_stream(
            llm_messages, api_key, config["base_url"], model_name,
            max_tokens, temperature, tools,
        ):
            if chunk_str == "[DONE]":
                break
            try:
                chunk = json.loads(chunk_str)
            except json.JSONDecodeError:
                continue

            delta = chunk.get("choices", [{}])[0].get("delta", {})

            # Content delta
            if delta.get("content"):
                collected_content += delta["content"]
                yield json.dumps({"type": "content", "content": delta["content"]}, ensure_ascii=False)

            # Tool call delta
            if delta.get("tool_calls"):
                has_tool_calls = True
                for tc in delta["tool_calls"]:
                    idx = tc.get("index", 0)
                    while len(collected_tool_calls) <= idx:
                        collected_tool_calls.append(
                            {"id": "", "type": "function", "function": {"name": "", "arguments": ""}}
                        )
                    if tc.get("id"):
                        collected_tool_calls[idx]["id"] = tc["id"]
                    if tc.get("function", {}).get("name"):
                        collected_tool_calls[idx]["function"]["name"] += tc["function"]["name"]
                    if tc.get("function", {}).get("arguments"):
                        collected_tool_calls[idx]["function"]["arguments"] += tc["function"]["arguments"]

        if not has_tool_calls:
            # No more tool calls, we're done
            yield json.dumps({"type": "done"}, ensure_ascii=False)
            return

        # Normalize collected tool calls for the next-round request:
        # - "type": "function" is REQUIRED by OpenAI-compatible APIs (DeepSeek returns 400 without it)
        # - fallback id / arguments to avoid empty values being rejected
        for idx, tc in enumerate(collected_tool_calls):
            if not tc.get("id"):
                tc["id"] = f"call_{round_num}_{idx}"
            tc["type"] = "function"
            if not tc["function"].get("arguments"):
                tc["function"]["arguments"] = "{}"

        # Execute tool calls
        llm_messages.append({
            "role": "assistant",
            "content": collected_content or None,
            "tool_calls": collected_tool_calls,
        })

        for tc in collected_tool_calls:
            fn_name = tc["function"]["name"]
            try:
                fn_args = json.loads(tc["function"]["arguments"] or "{}")
            except json.JSONDecodeError:
                fn_args = {}

            yield json.dumps({"type": "tool_call", "name": fn_name, "arguments": fn_args}, ensure_ascii=False)

            async with SessionLocal() as db:
                result = await execute_tool(fn_name, fn_args, db, user_permissions, user)

            yield json.dumps({"type": "tool_result", "name": fn_name, "result": json.loads(result)}, ensure_ascii=False)

            llm_messages.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": result,
            })

        # Continue loop for next round (LLM will summarize tool results)

    yield json.dumps({"type": "done"}, ensure_ascii=False)
