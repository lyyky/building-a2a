"""
MCP stdio server — 把 A2A 能力反向暴露给 BuildingAI。

每个 agent 生成一个 call_agent_<slug>_<id前8位> 工具，
每个 KB 生成一个 search_kb_<slug>_<id前8位> 工具，
外加一个 list_resources 工具用于自省。

BuildingAI 通过 MCP 启动这个进程（stdio 模式），然后通过 stdio JSON-RPC 通信。
"""

from __future__ import annotations

import json
import logging
import re
import sys
from typing import Any

# 让 config/registry 可被 import
sys.path.insert(0, "/root/a2a-gateway")

from mcp.server import Server  # type: ignore
from mcp.server.stdio import stdio_server  # type: ignore
from mcp.types import Tool, TextContent  # type: ignore

import a2a_client
import registry
import config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("a2a.mcp")


GATEWAY_BASE = f"http://localhost:{config.GATEWAY_PORT}"


# ─────────────────────────────────────────────────────────────────────────────
# 工具
# ─────────────────────────────────────────────────────────────────────────────

def _slugify(name: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9_\-]+", "_", (name or "").strip().lower()).strip("_")
    return s or "x"


def _agent_tool_name(agent: dict) -> str:
    return f"call_agent_{_slugify(agent.get('name') or '')}_{str(agent.get('id',''))[:8]}"


def _dataset_tool_name(dataset: dict) -> str:
    return f"search_kb_{_slugify(dataset.get('name') or '')}_{str(dataset.get('id',''))[:8]}"


def _index_tools() -> dict[str, dict]:
    """返回 {tool_name: {"kind": "agent"|"dataset", "id": ...}}。"""
    idx: dict[str, dict] = {}
    for a in registry.all_agents():
        idx[_agent_tool_name(a)] = {"kind": "agent", "id": str(a.get("id"))}
    for d in registry.all_datasets():
        idx[_dataset_tool_name(d)] = {"kind": "dataset", "id": str(d.get("id"))}
    return idx


# ─────────────────────────────────────────────────────────────────────────────
# MCP server
# ─────────────────────────────────────────────────────────────────────────────

server = Server("buildingai-a2a-gateway")


@server.list_tools()
async def list_tools() -> list[Tool]:
    tools: list[Tool] = [
        Tool(
            name="list_resources",
            description=(
                "列出当前可调用的所有 BuildingAI 智能体和知识库。"
                f"agent_count={len(registry.all_agents())},"
                f" dataset_count={len(registry.all_datasets())}"
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        )
    ]
    for agent in registry.all_agents():
        caps = registry.infer_capabilities(agent)
        desc = (agent.get("description") or "").strip() or f"调 agent: {agent.get('name')}"
        caps_desc = "、".join(caps)
        tools.append(
            Tool(
                name=_agent_tool_name(agent),
                description=f"[智能体 · {caps_desc}] {desc}",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "要问的问题"},
                    },
                    "required": ["query"],
                },
            )
        )
    for ds in registry.all_datasets():
        desc = (ds.get("description") or "").strip() or f"在知识库 '{ds.get('name')}' 检索"
        tools.append(
            Tool(
                name=_dataset_tool_name(ds),
                description=f"[知识库检索] {desc}",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "检索关键词"},
                    },
                    "required": ["query"],
                },
            )
        )
    return tools


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    log.info("MCP tool called: %s args=%s", name, arguments)
    if name == "list_resources":
        lines = ["# BuildingAI 资源", ""]
        for a in registry.all_agents():
            lines.append(
                f"- agent: {a.get('name')} (id={a.get('id')}) — "
                f"cap={registry.infer_capabilities(a)}"
            )
        for d in registry.all_datasets():
            lines.append(f"- dataset: {d.get('name')} (id={d.get('id')})")
        return [TextContent(type="text", text="\n".join(lines))]

    idx = _index_tools()
    info = idx.get(name)
    if not info:
        return [TextContent(type="text", text=f"未知工具: {name}")]

    query = (arguments or {}).get("query", "")
    if not query:
        return [TextContent(type="text", text="缺少参数 query")]

    client = a2a_client.A2AClient(GATEWAY_BASE)
    try:
        if info["kind"] == "agent":
            resp = await client.send_message(info["id"], query)
        else:
            resp = await client.send_dataset_query(info["id"], query)
        text = _summarize_a2a_result(resp)
        return [TextContent(type="text", text=text)]
    except Exception as exc:
        log.exception("调用 %s 失败", name)
        return [TextContent(type="text", text=f"调用 {name} 失败: {exc}")]


def _summarize_a2a_result(resp: dict) -> str:
    """把 A2A 响应（task + artifacts）压成文本片段。"""
    if not isinstance(resp, dict):
        return json.dumps(resp, ensure_ascii=False)[:2000]
    if "error" in resp:
        return f"A2A 错误: {resp['error']}"
    result = resp.get("result") or {}
    if isinstance(result, dict):
        artifacts = result.get("artifacts") or []
        chunks: list[str] = []
        for art in artifacts:
            parts = art.get("parts") or []
            for p in parts:
                if not isinstance(p, dict):
                    continue
                kind = p.get("kind") or p.get("type")
                if kind == "text":
                    chunks.append(p.get("text", ""))
                elif kind == "data":
                    data = p.get("data") or {}
                    chunks.append(
                        f"[{data.get('documentName','?')} score={data.get('score')}] "
                        f"{(data.get('content') or '')[:400]}"
                    )
                elif p.get("text"):
                    chunks.append(str(p.get("text")))
        if chunks:
            return "\n\n".join(chunks)
    return json.dumps(resp, ensure_ascii=False)[:2000]


# ─────────────────────────────────────────────────────────────────────────────
# 入口
# ─────────────────────────────────────────────────────────────────────────────

async def main() -> None:
    """先尝试触发一次 registry 刷新（避免 BuildingAI 启动 MCP 时 registry 还没跑过）。"""
    try:
        await registry.refresh_once()
        log.info(
            "MCP 启动，registry: agents=%d datasets=%d",
            len(registry.all_agents()),
            len(registry.all_datasets()),
        )
    except Exception as exc:
        log.warning("MCP 启动时 registry 刷新失败（继续）: %s", exc)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
