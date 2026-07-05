"""
HTTP 模式的 MCP server —— 把 registry 暴露给 BuildingAI 的 MCP 注册用。

BuildingAI 的 MCP 只支持 sse / streamable-http（不支持 stdio），
所以这里用 FastMCP 起 HTTP transport，streamable_http_app() 返回 ASGI app，
挂到 a2a_server 的 /mcp 路由。

工具设计（避免 FastMCP 不支持动态注册 tool 的痛点）：
  list_a2a_resources  —— 一键列出当前所有 agent / dataset
  call_a2a_resource   —— 调度，参数 kind={agent|dataset} id query
                         + 可选 chain / parallel_agents / kb_ids：
                           * chain          顺序链：A → B → C，后一个吃前一个输出
                           * parallel_agents 并行调：同 query 广播给多个 agent，结果聚合
                           * kb_ids         调 agent 前先并行检索这些 KB，片段作为上下文注入
二者配合，LLM 一次 list + 一次 call，零额外配置。

注意：本模块被 import 时不会启动，只导出 `mcp_streamable_app` 给 a2a_server 挂载。
"""

from __future__ import annotations

import asyncio
import logging
import re
import sys
from typing import Any

sys.path.insert(0, "/root/a2a-gateway")

from mcp.server.fastmcp import FastMCP  # type: ignore

import a2a_client
import config
import mcp_whitelist
import registry

log = logging.getLogger("a2a.mcp.http")

GATEWAY_BASE = f"http://localhost:{config.GATEWAY_PORT}"


# ─────────────────────────────────────────────────────────────────────────────
# FastMCP 实例
# ─────────────────────────────────────────────────────────────────────────────

_TRANSPORT_SECURITY_MOD = __import__(
    "mcp.server.transport_security", fromlist=["TransportSecuritySettings"]
)
# 把 mcp_whitelist 模块持有的 list 共享给 FastMCP；这样 UI 改白名单时
# 在 _allowed 列表上原地变更即可立刻生效。
_TRANSPORT_SECURITY = _TRANSPORT_SECURITY_MOD.TransportSecuritySettings(
    enable_dns_rebinding_protection=not config.MCP_DISABLE_REBINDING,
    allowed_hosts=mcp_whitelist.get_ref(),  # 传 list 引用，让 UI 改动能立刻生效
    allowed_origins=config.MCP_ALLOWED_ORIGINS,
)

mcp = FastMCP(
    name="buildingai-a2a-gateway",
    instructions=(
        "Use list_a2a_resources first to discover available BuildingAI agents and datasets. "
        "Then call call_a2a_resource(kind, id, query) to invoke them. "
        "kind must be 'agent' or 'dataset'. "
        "When kind='agent', you may optionally pass "
        "chain=[agent_id,...] for sequential agent-to-agent chaining, "
        "parallel_agents=[agent_id,...] for fan-out, and "
        "kb_ids=[dataset_id,...] to first retrieve knowledge bases as context."
    ),
    streamable_http_path="/",
    mount_path="/mcp-sse",  # SSE 用
    # Host 校验：BuildingAI 在容器里访问 gateway 时 Host header 不是 127.0.0.1/localhost，
    # 默认会被 FastMCP 的 DNS rebinding 保护挡（HTTP 421 Invalid Host header）。
    # 用 host="0.0.0.0" 跳过自动启用；具体白名单由配置控制：
    #   MCP_ALLOWED_HOSTS=host1:*,host2:8000,...  → 白名单
    #   MCP_DISABLE_REBINDING=true                → 内网/可信环境可一键关
    host="0.0.0.0",
    port=0,  # 不由 FastMCP 直接 run，由 FastAPI 挂载
    transport_security=_TRANSPORT_SECURITY,
)


# 让 main.py 把 session_manager.run() 包进 FastAPI 的 lifespan，
# 否则 streamable HTTP 会抛 'Task group is not initialized'。
def mcp_lifespan(app):
    """异步上下文管理器：在 FastAPI lifespan 内启用 session_manager。"""
    # streamable_http_app() 第一次会创建 session_manager（懒加载）。
    # 这里立即调用一次确保它存在。
    _ = mcp.streamable_http_app()
    return mcp._session_manager.run()  # type: ignore[attr-defined,return-value]  # noqa: SLF001


@mcp.tool(
    name="list_a2a_resources",
    description=(
        "列出 A2A Gateway 当前缓存的所有 BuildingAI 智能体和知识库。"
        "返回 id + name + capabilities + description，每条一行。"
        "调用 call_a2a_resource 前先调这个拿到 id。"
    ),
)
async def list_a2a_resources() -> str:
    lines = ["# BuildingAI 资源", ""]
    for a in registry.all_agents():
        caps = registry.infer_capabilities(a)
        desc = (a.get("description") or "").replace("\n", " ")[:80]
        lines.append(
            f"- [agent] id={a.get('id')}  name={a.get('name')}  "
            f"cap={caps}  desc={desc}"
        )
    for d in registry.all_datasets():
        desc = (d.get("description") or "").replace("\n", " ")[:80]
        lines.append(
            f"- [dataset] id={d.get('id')}  name={d.get('name')}  desc={desc}"
        )
    if len(lines) == 2:
        lines.append("(暂无资源。请确认 A2A Gateway 已成功登录 BuildingAI 并完成首次 registry 刷新)")
    return "\n".join(lines)


@mcp.tool(
    name="call_a2a_resource",
    description=(
        "调用一个 BuildingAI 资源（agent 或 dataset），可叠加多 agent 协作与 KB 上下文。\n"
        "必填：kind='agent'|'dataset'，id=UUID 字符串，query=问题/检索词。\n"
        "可选（仅 kind='agent' 时生效）：\n"
        "  - chain: 顺序链式 agent ID 列表，前一个 agent 的输出作为下一个的输入，最终返回最后输出\n"
        "  - parallel_agents: 并行 agent ID 列表，每个拿到相同 query，结果聚合成多段文本\n"
        "  - kb_ids: 调主 agent 前先并行检索这些 KB，片段作为额外上下文拼到 query 里\n"
        "示例（先查 KB 再交给专家 agent，再让评审 agent 过一遍）：\n"
        "  call_a2a_resource(kind='agent', id='专家-id', query='X 是什么', kb_ids=['手册-kb'], chain=['评审-id'])"
    ),
)
async def call_a2a_resource(
    kind: str,
    id: str,
    query: str,
    chain: list[str] | None = None,
    parallel_agents: list[str] | None = None,
    kb_ids: list[str] | None = None,
) -> str:
    """调度 BuildingAI 资源（agent / dataset），支持多 agent 协作 + KB 上下文。

    kind='agent'   调主 agent（id），query 是用户问题
    kind='dataset' 检索知识库（id），query 是检索词

    仅 kind='agent' 时以下三个可选参数生效：
      chain          顺序链：A 输出 → 喂给 B → ...，返回最后一个 agent 的输出
      parallel_agents 并行：同 query 广播给列表里每个 agent，结果聚合
      kb_ids         调主 agent 前先并行检索 KB，片段作为额外上下文拼进 query
    """
    kind = (kind or "").strip().lower()
    if kind not in ("agent", "dataset"):
        return f"参数 kind 必须是 'agent' 或 'dataset'，收到: {kind}"
    if not id or not query:
        return "缺少参数 id 或 query"

    # dataset 模式：忽略所有 agent/KB 参数，按原行为走
    if kind == "dataset":
        client = a2a_client.A2AClient(GATEWAY_BASE)
        try:
            resp = await client.send_dataset_query(id, query)
            return _summarize(resp)
        except Exception as exc:
            log.exception("call_a2a_resource (dataset) failed")
            return f"调用失败: {exc}"

    # ─── agent 模式：支持可选 chain / parallel_agents / kb_ids ───
    client = a2a_client.A2AClient(GATEWAY_BASE)

    # 1) 先并行检索 KB（若有），拼成上下文
    kb_context = ""
    if kb_ids:
        kb_context = await _retrieve_kb_context(client, kb_ids, query)
        if kb_context:
            query = f"{kb_context}\n\n---\n用户问题：{query}"

    # 2) 调主 agent
    try:
        main_resp = await client.send_message(id, query)
        main_text = _summarize(main_resp)
    except Exception as exc:
        log.exception("call_a2a_resource (main agent) failed")
        return f"主 agent 调用失败: {exc}"

    # 3) chain：把主 agent 输出喂给链上每个 agent（顺序）
    chain_text = main_text
    if chain:
        chain_text = await _run_chain(client, chain, main_text)

    # 4) parallel_agents：同 query 广播，聚合结果
    parallel_text = ""
    if parallel_agents:
        parallel_text = await _run_parallel_agents(client, parallel_agents, query)

    # 5) 拼最终输出
    return _combine_agent_results(id, chain_text, parallel_text)


async def _retrieve_kb_context(
    client: a2a_client.A2AClient, kb_ids: list[str], query: str
) -> str:
    """并行检索一组 KB，把片段拼成 [KB: name] 块。多 KB 任意一个失败不阻断整体。"""

    async def _one(kb_id: str) -> str:
        try:
            resp = await client.send_dataset_query(kb_id, query)
        except Exception as exc:
            log.warning("KB %s 检索失败: %s", kb_id, exc)
            return f"[KB:{kb_id}] 检索失败: {exc}"
        meta = registry.get_dataset(kb_id) or {}
        name = meta.get("name") or kb_id[:8]
        text = _summarize(resp)
        return f"[KB:{name}]\n{text}"

    results = await asyncio.gather(*[_one(k) for k in kb_ids])
    # 去掉空段
    return "\n\n".join(r for r in results if r and r.strip())


async def _run_chain(
    client: a2a_client.A2AClient, chain: list[str], initial_text: str
) -> str:
    """顺序链式调用：每个 agent 拿到前一个 agent 的输出，返回最终 agent 的输出。"""
    cur = initial_text
    for agent_id in chain:
        try:
            resp = await client.send_message(agent_id, cur)
        except Exception as exc:
            log.warning("chain agent %s 调用失败: %s", agent_id, exc)
            return f"[chain 中断于 {agent_id}] {exc}\n\n前置输出：\n{cur}"
        cur = _summarize(resp)
    return cur


async def _run_parallel_agents(
    client: a2a_client.A2AClient, agents: list[str], query: str
) -> str:
    """并行调用一组 agent（同 query），把每个 agent 的输出聚合成多段文本。"""

    async def _one(agent_id: str) -> str:
        try:
            resp = await client.send_message(agent_id, query)
            text = _summarize(resp)
        except Exception as exc:
            log.warning("parallel agent %s 调用失败: %s", agent_id, exc)
            text = f"[调用失败] {exc}"
        meta = registry.get_agent(agent_id) or {}
        name = meta.get("name") or agent_id[:8]
        return f"### {name}\n{text}"

    results = await asyncio.gather(*[_one(a) for a in agents])
    return "\n\n".join(results)


def _combine_agent_results(
    main_id: str, chain_text: str, parallel_text: str
) -> str:
    """主结果（可能被 chain 覆盖）+ 并行结果 → 单一文本。"""
    main_meta = registry.get_agent(main_id) or {}
    main_name = main_meta.get("name") or main_id[:8]
    if parallel_text:
        return (
            f"### {main_name}\n{chain_text}\n\n"
            f"---\n\n### 并行 agent 输出\n{parallel_text}"
        )
    return chain_text


def _summarize(resp: Any) -> str:
    """A2A 响应 → 文本。"""
    if not isinstance(resp, dict):
        return str(resp)[:2000]
    if "error" in resp:
        return f"A2A 错误: {resp['error']}"
    result = resp.get("result") or {}
    if isinstance(result, dict):
        artifacts = result.get("artifacts") or []
        chunks: list[str] = []
        for art in artifacts:
            for p in art.get("parts") or []:
                if not isinstance(p, dict):
                    continue
                kind = p.get("kind") or p.get("type")
                if kind == "text":
                    chunks.append(p.get("text", ""))
                elif kind == "data":
                    d = p.get("data") or {}
                    chunks.append(
                        f"[{d.get('documentName','?')} score={d.get('score')}] "
                        f"{(d.get('content') or '')[:400]}"
                    )
        if chunks:
            return "\n\n".join(chunks)
    return str(resp)[:2000]


# ─────────────────────────────────────────────────────────────────────────────
# ASGI app 导出
# ─────────────────────────────────────────────────────────────────────────────

# BuildingAI 1.15 同时支持 sse 和 streamable-http。这里导两份，
# 由 a2a_server 决定挂哪条（默认 streamable-http + 兼容 SSE）。

try:
    mcp_streamable_app = mcp.streamable_http_app()  # type: ignore[attr-defined]
    mcp_sse_app = mcp.sse_app()                      # type: ignore[attr-defined]
except Exception as exc:
    log.error("FastMCP transport app 构造失败: %s", exc)
    raise

# ─────────────────────────────────────────────────────────────────────────────
# 运行时白名单生效的兜底
# ─────────────────────────────────────────────────────────────────────────────
# Pydantic 会复制 list，UI 改 mcp_whitelist 时不会自动影响 FastMCP 中间件。
# 这里把 FastMCP / MCP 内部所有 TransportSecurityMiddleware 实例的
# _validate_host 替换成读 mcp_whitelist 共享 list 的版本，确保立即生效。

from mcp.server.transport_security import TransportSecurityMiddleware as _TSM  # noqa: E402

_orig_validate = _TSM._validate_host


# Loopback 系列（127.0.0.1 / localhost / [::1]）作为内置兜底：
# 不需要走 mcp_whitelist 配置——浏览器/SSH 隧道本机访问的 host header 天然就是 loopback，
# 而 loopback 在容器外的 a2a-gateway 端口绑 127.0.0.1 这条防御链上已是安全默认。
# mcp_whitelist 只需要列出"额外需要放行的非 loopback 容器内 host"（如 buildingai-a2a-gateway:8000）。
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "[::1]"})


def _patched_validate_host(self, host):  # noqa: ANN001
    """Runtime-aware: mcp_whitelist（非 loopback 容器 host）+ loopback 内置兜底。"""
    if not host:
        log.warning("Missing Host header in request")
        return False
    # 1) 运行时白名单（精确匹配 + 通配端口）
    allowed = mcp_whitelist._allowed  # noqa: SLF001
    if host in allowed:
        return True
    for pattern in allowed:
        if pattern.endswith(":*"):
            base = pattern[:-2]
            if host.startswith(base + ":"):
                return True
    # 2) Loopback 兜底（host[:port] 的 base 在 loopback 集合内即放行）
    if ":" in host:
        base = host.rsplit(":", 1)[0]
        if base in _LOOPBACK_HOSTS:
            return True
    log.warning(f"Invalid Host header: {host}")
    return False


_TSM._validate_host = _patched_validate_host  # type: ignore[assignment]
log.info("FastMCP 内部 TransportSecurityMiddleware._validate_host 已替换为运行时读 mcp_whitelist")
