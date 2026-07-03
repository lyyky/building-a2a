"""
MCP Host 白名单的运行时持久化。

设计要点：
  - 内存里用单个 list 持有白名单，**mcp_http 初始化 FastMCP 时把这个 list 当引用
    传进去**。FastMCP 中间件的 _validate_host 是 `host in self.settings.allowed_hosts`，
    它每次检查都重新读列表——所以原地 mutate 这个 list 就能让下次请求立刻生效，
    无需重启 gateway。
  - 数据落盘到 /root/a2a-gateway/data/mcp_whitelist.json（首次缺失时用默认）。
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

log = logging.getLogger("a2a.mcp_whitelist")

_DEFAULT = [
    "127.0.0.1:*",
    "localhost:*",
    "[::1]:*",
]

_DATA_DIR = Path(os.getenv("A2A_DATA_DIR", "/root/a2a-gateway/data"))
_DATA_FILE = _DATA_DIR / "mcp_whitelist.json"


# 这两个全局变量是同一份数据，"内存"和"共享给 FastMCP"都是它。
_allowed: list[str] = list(_DEFAULT)


def _ensure_dir() -> None:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)


def init_from_disk_or_env() -> None:
    """启动时调用一次：优先用磁盘持久化的值；否则用 MCP_ALLOWED_HOSTS env；否则用默认。"""
    global _allowed
    if _DATA_FILE.is_file():
        try:
            data = json.loads(_DATA_FILE.read_text())
            if isinstance(data, list):
                _allowed = [str(x) for x in data]
                log.info("白名单从 %s 加载：%d 项", _DATA_FILE, len(_allowed))
                return
        except Exception as exc:
            log.warning("白名单文件 %s 解析失败：%s", _DATA_FILE, exc)

    env_value = os.getenv("MCP_ALLOWED_HOSTS", "").strip()
    if env_value:
        _allowed = [h.strip() for h in env_value.split(",") if h.strip()]
        log.info("白名单从 MCP_ALLOWED_HOSTS env 加载：%d 项", len(_allowed))
        # 顺手持久化一份，免得不一致
        try:
            persist()
        except Exception:
            pass
        return

    _allowed = list(_DEFAULT)
    log.info("白名单使用默认：%d 项", len(_allowed))


def get() -> list[str]:
    """返回当前白名单的**副本**（给 API 响应用，防止外部 mutate）。"""
    return list(_allowed)


def get_ref() -> list[str]:
    """返回当前白名单的**引用**（仅供 mcp_http 在 import 时传给 FastMCP
    共享同一 list；外部不要 mutate，要改走 set_list/add/remove）。"""
    return _allowed


def set_list(new_list: list[str]) -> None:
    """**就地更新**，让 FastMCP 中间件下次请求就看到新值。"""
    global _allowed
    normalized = []
    for item in new_list:
        if not isinstance(item, str):
            continue
        s = item.strip()
        if s:
            normalized.append(s)
    _allowed.clear()
    _allowed.extend(normalized)
    persist()
    log.info("白名单已更新：%d 项", len(_allowed))


def add(host: str) -> None:
    """加一条，自动 trim。"""
    h = host.strip()
    if not h:
        return
    if h not in _allowed:
        _allowed.append(h)
        persist()


def remove(host: str) -> None:
    h = host.strip()
    if h in _allowed:
        _allowed.remove(h)
        persist()


def persist() -> None:
    _ensure_dir()
    _DATA_FILE.write_text(json.dumps(_allowed, ensure_ascii=False, indent=2))


# 启动即初始化一次（从磁盘读）
init_from_disk_or_env()
