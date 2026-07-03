"""
HTTP 模式的 MCP server —— 把 registry 暴露给 BuildingAI 的 MCP 注册用。

BuildingAI 的 MCP 只支持 sse / streamable-http（不支持 stdio），
所以这里用 FastMCP 起 HTTP transport，streamable_http_app() 返回 ASGI app，
挂到 a2a_server 的 /mcp 路由。

工具设计（避免 FastMCP 不支持动态注册 tool 的痛点）：
  list_a2a_resources  —— 一键列出当前所有 agent / dataset
  call_a2a_resource   —— 调度，参数 kind={agent|dataset} id query
二者配合，LLM 一次 list + 一次 call，零额外配置。

注意：本模块被 import 时不会启动，只导出 `mcp_streamable_app` 给 a2a_server 挂载。
"""

from __future__ import annotations

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
        "kind must be 'agent' or 'dataset'."
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
        "调用一个 BuildingAI 资源（agent 或 dataset）。"
        "先调 list_a2a_resources 拿到 id，再传 (kind, id, query)。"
        "kind 必须是 'agent' 或 'dataset'，id 是 UUID 字符串，query 是问题/检索词。"
    ),
)
async def call_a2a_resource(kind: str, id: str, query: str) -> str:
    kind = (kind or "").strip().lower()
    if kind not in ("agent", "dataset"):
        return f"参数 kind 必须是 'agent' 或 'dataset'，收到: {kind}"
    if not id or not query:
        return "缺少参数 id 或 query"

    client = a2a_client.A2AClient(GATEWAY_BASE)
    try:
        if kind == "agent":
            resp = await client.send_message(id, query)
        else:
            resp = await client.send_dataset_query(id, query)
        return _summarize(resp)
    except Exception as exc:
        log.exception("call_a2a_resource failed")
        return f"调用失败: {exc}"


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


def _patched_validate_host(self, host):  # noqa: ANN001
    """Runtime-aware version that always reads from mcp_whitelist."""
    if not host:
        log.warning("Missing Host header in request")
        return False
    allowed = mcp_whitelist._allowed  # noqa: SLF001
    if host in allowed:
        return True
    for pattern in allowed:
        if pattern.endswith(":*"):
            base = pattern[:-2]
            if host.startswith(base + ":"):
                return True
    log.warning(f"Invalid Host header: {host}")
    return False


_TSM._validate_host = _patched_validate_host  # type: ignore[assignment]
log.info("FastMCP 内部 TransportSecurityMiddleware._validate_host 已替换为运行时读 mcp_whitelist")
