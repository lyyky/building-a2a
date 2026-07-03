"""
A2A 标准客户端（备用）。外部 agent 想通过 A2A 协议调用本 gateway 时用。
"""

from __future__ import annotations

import json
from typing import Any

import httpx


class A2AClient:
    def __init__(self, base_url: str = "http://localhost:8000") -> None:
        self.base = base_url.rstrip("/")

    async def discover(self) -> dict:
        """拉 /.well-known/agent.json。"""
        async with httpx.AsyncClient(timeout=30.0) as c:
            r = await c.get(f"{self.base}/.well-known/agent.json")
            r.raise_for_status()
            return r.json()

    async def _rpc(self, path: str, method: str, message: dict, conversation_id: str | None = None) -> dict:
        params: dict[str, Any] = {"message": message}
        if conversation_id:
            params["conversationId"] = conversation_id
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": params,
        }
        async with httpx.AsyncClient(timeout=60.0) as c:
            r = await c.post(f"{self.base}{path}", json=payload)
            r.raise_for_status()
            return r.json()

    async def send_message(self, agent_id: str, text: str, conversation_id: str | None = None) -> dict:
        message = {"role": "user", "parts": [{"kind": "text", "text": text}]}
        return await self._rpc(f"/a2a/agents/{agent_id}", "message/send", message, conversation_id)

    async def send_dataset_query(self, dataset_id: str, query: str) -> dict:
        message = {"role": "user", "parts": [{"kind": "text", "text": query}]}
        return await self._rpc(f"/a2a/datasets/{dataset_id}", "message/send", message)
