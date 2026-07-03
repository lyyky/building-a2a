"""
Registry — 每 30s 从 BuildingAI 拉 agent 和 dataset。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from config import POLL_INTERVAL, bai

log = logging.getLogger("a2a.registry")


# ─────────────────────────────────────────────────────────────────────────────
# 单例：agents + datasets
# ─────────────────────────────────────────────────────────────────────────────

_state: dict[str, dict[str, dict]] = {"agents": {}, "datasets": {}}
_last_refresh: dict[str, float] = {"agents": 0.0, "datasets": 0.0}
_lock = asyncio.Lock()


def all_agents() -> list[dict]:
    return list(_state["agents"].values())


def all_datasets() -> list[dict]:
    return list(_state["datasets"].values())


def get_agent(agent_id: str) -> dict | None:
    return _state["agents"].get(agent_id)


def get_dataset(dataset_id: str) -> dict | None:
    return _state["datasets"].get(dataset_id)


def last_refresh_at() -> float:
    return min(_last_refresh["agents"], _last_refresh["datasets"])


# ─────────────────────────────────────────────────────────────────────────────
# 刷新
# ─────────────────────────────────────────────────────────────────────────────

async def _refresh_agents() -> int:
    items = await bai().list_agents()
    new_map: dict[str, dict] = {}
    for it in items:
        if isinstance(it, dict) and it.get("id"):
            new_map[str(it["id"])] = it
    async with _lock:
        _state["agents"] = new_map
        _last_refresh["agents"] = time.time()
    return len(new_map)


async def _refresh_datasets() -> int:
    items = await bai().list_datasets()
    new_map: dict[str, dict] = {}
    for it in items:
        if isinstance(it, dict) and it.get("id"):
            new_map[str(it["id"])] = it
    async with _lock:
        _state["datasets"] = new_map
        _last_refresh["datasets"] = time.time()
    return len(new_map)


async def refresh_once() -> tuple[int, int]:
    """刷一次，返回 (agent 数, dataset 数)。"""
    try:
        a = await _refresh_agents()
    except Exception as exc:
        log.warning("刷新 agent 失败: %s", exc)
        a = len(_state["agents"])
    try:
        d = await _refresh_datasets()
    except Exception as exc:
        log.warning("刷新 dataset 失败: %s", exc)
        d = len(_state["datasets"])
    return a, d


# ─────────────────────────────────────────────────────────────────────────────
# 后台轮询任务
# ─────────────────────────────────────────────────────────────────────────────

async def poller_loop() -> None:
    """后台循环，每 POLL_INTERVAL 秒刷一次。"""
    log.info("Registry 轮询启动，间隔 %ds", POLL_INTERVAL)
    while True:
        try:
            a, d = await refresh_once()
            log.info("Registry 刷新完成：agents=%d, datasets=%d", a, d)
        except Exception as exc:
            log.warning("Registry 刷新失败: %s", exc)
        await asyncio.sleep(POLL_INTERVAL)


async def start_poller() -> asyncio.Task:
    """启动后台轮询 task。"""
    return asyncio.create_task(poller_loop(), name="registry-poller")


# ─────────────────────────────────────────────────────────────────────────────
# 工具：推断 agent 能力
# ─────────────────────────────────────────────────────────────────────────────

def infer_capabilities(agent: dict) -> list[str]:
    """根据 agent 字段推断能力标签：chat/rag/tools。"""
    caps = ["chat"]
    cfg = agent.get("config") or {}
    # 多种 schema 都见过了，模糊匹配兜底
    for key in ("datasetIds", "dataset_ids", "datasets"):
        if cfg.get(key) or agent.get(key):
            caps.append("rag")
            break
    for key in ("mcpServerIds", "mcp_server_ids", "toolIds", "tools"):
        if cfg.get(key) or agent.get(key):
            caps.append("tools")
            break
    return caps
