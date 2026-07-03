"""
后台管理 API：
  GET  /api/admin/status               整体状态（登录、registry、配置脱敏）
  POST /api/admin/refresh             手动触发 registry 一次刷新
  GET  /api/admin/config              配置脱敏（不返回密码）
  GET  /api/admin/users               多用户池状态（已配置/已登录）
  POST /api/admin/users               加用户（自动 encrypt + 写 .env）
  DELETE /api/admin/users/{key}       删用户
  GET  /api/admin/system/env          基础段变量（密码脱敏）
  POST /api/admin/system/env          改 .env 基础段
  POST /api/admin/system/master-key   生成/重置 MASTER_KEY
  POST /api/admin/system/restart      重启 a2a-gateway 进程
  GET  /api/admin/mcp/whitelist       当前 MCP 白名单
  PUT  /api/admin/mcp/whitelist       整组替换（持久化 + 立即生效）

admin 端点鉴权（除 /status, /mcp/whitelist 外）：
  环境变量 A2A_ADMIN_TOKEN 非空时，所有 admin 写端点要求 X-Admin-Token header
  未配时向后兼容（不鉴权）—— 仅内网用
"""

from __future__ import annotations

import logging
import os
import signal
import threading
import time
from pathlib import Path

from cryptography.fernet import Fernet
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

import mcp_whitelist
import registry
from config import (
    BAI_BASE,
    BAI_PASSWORD,
    BAI_USERNAME,
    DATASET_SCORE_THRESHOLD,
    DATASET_TOP_K,
    GATEWAY_HOST,
    GATEWAY_PORT,
    POLL_INTERVAL,
    USER_KEYS,
    bai,
    reload_user_keys,
    user_pool as _user_pool,
)

log = logging.getLogger("a2a.admin")

router = APIRouter(prefix="/api/admin", tags=["admin"])


# ─────────────────────────────────────────────────────────────────────────────
# admin 鉴权
# ─────────────────────────────────────────────────────────────────────────────

ADMIN_TOKEN: str | None = os.getenv("A2A_ADMIN_TOKEN")
# .env 文件路径（A2A_GATEWAY_ENV_FILE 配时用，否则跟 a2a-gateway 同目录）
ENV_FILE: Path = Path(os.getenv("A2A_GATEWAY_ENV_FILE", str(Path(__file__).parent / ".env"))).resolve()


async def _verify_admin(x_admin_token: str | None = Header(default=None)) -> None:
    """admin 写端点鉴权。

    A2A_ADMIN_TOKEN 未配 → 跳过（向后兼容，仅内网用）。
    A2A_ADMIN_TOKEN 配了 → 校验 X-Admin-Token header 一致才放行。
    """
    if not ADMIN_TOKEN:
        return
    if x_admin_token != ADMIN_TOKEN:
        raise HTTPException(401, "invalid or missing X-Admin-Token")


# ─────────────────────────────────────────────────────────────────────────────
# .env 读写辅助（保留原行 / 注释 / 顺序）
# ─────────────────────────────────────────────────────────────────────────────

def _parse_env_file(path: Path) -> dict[str, str]:
    """解析 .env，保留键值对。注释 / 空行不返回。"""
    if not path.exists():
        return {}
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def _update_env_file(path: Path, updates: dict[str, str]) -> None:
    """原子更新 .env（保留顺序 / 注释）。

    - 已存在的 key 替换值
    - 不存在的 key 追加到末尾
    - 写 .env.tmp 然后 rename（防半写）
    """
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(f"{k}={v}" for k, v in updates.items()) + "\n", encoding="utf-8")
        return

    lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    seen: set[str] = set()
    new_lines: list[str] = []
    for line in lines:
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            new_lines.append(line)
            continue
        key = s.split("=", 1)[0].strip()
        if key in updates:
            new_lines.append(f"{key}={updates[key]}")
            seen.add(key)
        else:
            new_lines.append(line)
    for k, v in updates.items():
        if k not in seen:
            new_lines.append(f"{k}={v}")
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    tmp.replace(path)
    log.info(".env 已更新：%s", list(updates.keys()))


@router.get("/status")
async def status() -> dict:
    """整体状态：登录态、registry 摘要、上次刷新时间、BuildingAI 基础信息。"""
    last = registry.last_refresh_at()
    return {
        "ok": True,
        "now": time.time(),
        "buildingai": {
            "base": BAI_BASE,
            "logged_in": bai()._logged_in,  # noqa: SLF001（内部字段只读状态）
            "username": bai()._username,    # 用户名不算敏感，回显便于排错
        },
        "registry": {
            "last_refresh_at": last,
            "last_refresh_age_sec": (time.time() - last) if last > 0 else None,
            "poll_interval": POLL_INTERVAL,
            "agent_count": len(registry.all_agents()),
            "dataset_count": len(registry.all_datasets()),
        },
        "agents": [
            {
                "id": str(a.get("id")),
                "name": a.get("name"),
                "description": a.get("description"),
                "capabilities": registry.infer_capabilities(a),
            }
            for a in registry.all_agents()
        ],
        "datasets": [
            {"id": str(d.get("id")), "name": d.get("name"), "description": d.get("description")}
            for d in registry.all_datasets()
        ],
    }


@router.post("/refresh")
async def refresh() -> dict:
    """手动触发一次 registry 刷新（清空失败缓存、无视 30s 间隔）。"""
    try:
        a, d = await registry.refresh_once()
        return {
            "ok": True,
            "agent_count": a,
            "dataset_count": d,
            "refreshed_at": time.time(),
        }
    except Exception as exc:
        log.exception("手动刷新失败")
        return {"ok": False, "error": str(exc)}


@router.get("/config")
async def get_config_safe() -> dict:
    """脱敏配置（不回显密码）。"""
    return {
        "bai_base": BAI_BASE,
        "bai_username": bai()._username,  # noqa: SLF001
        "gateway_host": GATEWAY_HOST,
        "gateway_port": GATEWAY_PORT,
        "poll_interval": POLL_INTERVAL,
        "dataset_top_k": DATASET_TOP_K,
        "dataset_score_threshold": DATASET_SCORE_THRESHOLD,
    }


@router.get("/users")
async def list_users() -> dict:
    """多用户池状态：哪些 key 已配置、哪些已登录（不暴露密码 / 完整 api_key）。"""
    pool = _user_pool()
    by_key = {c.api_key: c for c in pool._by_key.values()}  # noqa: SLF001
    return {
        "total_configured": len(USER_KEYS),
        "active_logged_in": sum(1 for c in by_key.values() if c.client._logged_in),  # noqa: SLF001
        "items": [
            {
                "api_key_prefix": k[:8] + "...",
                "username": v["username"],
                "logged_in": k in by_key and by_key[k].client._logged_in,  # noqa: SLF001
            }
            for k, v in USER_KEYS.items()
        ],
    }


# ─────────────────────────────────────────────────────────────────────────────
# MCP 白名单（运行时管理）
# ─────────────────────────────────────────────────────────────────────────────

class WhitelistPayload(BaseModel):
    hosts: list[str] = Field(default_factory=list, description="全部白名单 hosts，逐条替换")


@router.get("/mcp/whitelist")
async def whitelist_get() -> dict:
    """查看 MCP Host 白名单。"""
    return {
        "items": mcp_whitelist.get(),
        "data_file": str(mcp_whitelist._DATA_FILE),  # noqa: SLF001
    }


@router.put("/mcp/whitelist")
async def whitelist_put(payload: WhitelistPayload) -> dict:
    """整组替换白名单（保存到磁盘 + 立即对后续请求生效）。"""
    try:
        mcp_whitelist.set_list(payload.hosts)
    except Exception as exc:
        log.exception("白名单更新失败")
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "items": mcp_whitelist.get()}


# ─────────────────────────────────────────────────────────────────────────────
# 多用户管理（写 .env + 自动 encrypt）
# ─────────────────────────────────────────────────────────────────────────────

class AddUserPayload(BaseModel):
    api_key: str = Field(..., min_length=4, description="调用方用的 API key（sk-xxx 风格）")
    bai_username: str = Field(..., min_length=1, description="BuildingAI 后台用户名")
    bai_password: str = Field(..., min_length=1, description="明文密码（后端自动 Fernet 加密）")


@router.post("/users")
async def users_add(payload: AddUserPayload, _: None = Depends(_verify_admin)) -> dict:
    """加用户：自动 encrypt + 写 .env。

    改完要重启 a2a-gateway 才生效（重启端点：/api/admin/system/restart）。
    """
    # 找 MASTER_KEY（先看运行时 env，再看 .env）
    master = os.getenv("A2A_GATEWAY_MASTER_KEY", "")
    if not master:
        env = _parse_env_file(ENV_FILE)
        master = env.get("A2A_GATEWAY_MASTER_KEY", "")
    if not master:
        raise HTTPException(
            400,
            "A2A_GATEWAY_MASTER_KEY 未配置，请先调 /api/admin/system/master-key 生成",
        )
    try:
        f = Fernet(master.encode())
        enc = f.encrypt(payload.bai_password.encode()).decode()
    except Exception as exc:
        raise HTTPException(400, f"MASTER_KEY 无效：{exc}") from exc

    env = _parse_env_file(ENV_FILE)
    users_raw = env.get("A2A_GATEWAY_USERS", "")
    new_entry = f"{payload.api_key}:{payload.bai_username}:{enc}"
    users_raw = (users_raw + "," + new_entry) if users_raw else new_entry
    _update_env_file(ENV_FILE, {"A2A_GATEWAY_USERS": users_raw})
    n = reload_user_keys(ENV_FILE)
    log.info("添加用户 api_key=%s... user=%s（当前共 %d 个）", payload.api_key[:8], payload.bai_username, n)
    return {
        "ok": True,
        "api_key_prefix": payload.api_key[:8] + "...",
        "bai_username": payload.bai_username,
        "total_users": n,
        "restart_required": True,
    }


@router.delete("/users/{api_key}")
async def users_del(api_key: str, _: None = Depends(_verify_admin)) -> dict:
    """删用户（按 api_key 整段匹配）。"""
    env = _parse_env_file(ENV_FILE)
    users_raw = env.get("A2A_GATEWAY_USERS", "")
    if not users_raw:
        raise HTTPException(404, "A2A_GATEWAY_USERS 为空")
    new_items = []
    removed = 0
    for item in users_raw.split(","):
        item = item.strip()
        if not item:
            continue
        if item.split(":", 1)[0].strip() == api_key:
            removed += 1
            continue
        new_items.append(item)
    if removed == 0:
        raise HTTPException(404, f"api_key={api_key[:8]}... 不存在")
    _update_env_file(
        ENV_FILE, {"A2A_GATEWAY_USERS": ",".join(new_items)}
    )
    n = reload_user_keys(ENV_FILE)
    log.info("删除用户 api_key=%s...（当前共 %d 个）", api_key[:8], n)
    return {
        "ok": True,
        "removed": api_key[:8] + "...",
        "remaining": n,
        "restart_required": True,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 基础段配置（BAI_USERNAME / BAI_PASSWORD / BAI_BASE / GATEWAY_HOST / GATEWAY_PORT）
# ─────────────────────────────────────────────────────────────────────────────

_BASE_ENV_KEYS = ("BAI_USERNAME", "BAI_PASSWORD", "BAI_BASE", "GATEWAY_HOST", "GATEWAY_PORT")


@router.get("/system/env")
async def system_env_get(_: None = Depends(_verify_admin)) -> dict:
    """基础段变量（密码脱敏 + 写盘路径）。"""
    pwd = os.getenv("BAI_PASSWORD", "")
    return {
        "env_file": str(ENV_FILE),
        "env_file_exists": ENV_FILE.exists(),
        "values": {
            "BAI_USERNAME": os.getenv("BAI_USERNAME", ""),
            "BAI_PASSWORD_set": bool(pwd),
            "BAI_PASSWORD_masked": ("*" * min(len(pwd), 12)) if pwd else "",
            "BAI_BASE": os.getenv("BAI_BASE", "http://localhost:4090"),
            "GATEWAY_HOST": os.getenv("GATEWAY_HOST", "127.0.0.1"),
            "GATEWAY_PORT": os.getenv("GATEWAY_PORT", "8000"),
        },
    }


class EnvUpdatePayload(BaseModel):
    updates: dict[str, str] = Field(..., description="要改的 key→value，只接受 _BASE_ENV_KEYS 这 5 个")


@router.post("/system/env")
async def system_env_set(payload: EnvUpdatePayload, _: None = Depends(_verify_admin)) -> dict:
    """改 .env 基础段（要重启 a2a-gateway 生效）。"""
    bad = set(payload.updates.keys()) - set(_BASE_ENV_KEYS)
    if bad:
        raise HTTPException(400, f"keys 不允许：{bad}（只接受 {_BASE_ENV_KEYS}）")
    _update_env_file(ENV_FILE, payload.updates)
    return {"ok": True, "updated": list(payload.updates.keys()), "restart_required": True}


@router.get("/system/master-key")
async def system_master_key_get(_: None = Depends(_verify_admin)) -> dict:
    """查看 MASTER_KEY 状态（不返明文，只返 set 标志 + 脱敏前缀）。"""
    env = _parse_env_file(ENV_FILE)
    mk = env.get("A2A_GATEWAY_MASTER_KEY", "")
    return {
        "set": bool(mk),
        "masked_prefix": (mk[:8] + "...") if len(mk) >= 8 else ("*" * len(mk)) if mk else "",
    }


@router.post("/system/master-key")
async def system_master_key(_: None = Depends(_verify_admin)) -> dict:
    """生成新 MASTER_KEY，写到 .env。

    警告：换了之后所有 A2A_GATEWAY_USERS 里的密文都解密不了——需要重新加用户。
    """
    new_key = Fernet.generate_key().decode()
    _update_env_file(ENV_FILE, {"A2A_GATEWAY_MASTER_KEY": new_key})
    reload_user_keys(ENV_FILE)  # 让 _fernet 立即可用
    log.warning("MASTER_KEY 已重新生成（所有用户密文会失效，需要重加）")
    return {
        "ok": True,
        "master_key": new_key,
        "warning": "换 MASTER_KEY 后所有 A2A_GATEWAY_USERS 里的密文都失效，请重新加用户",
        "restart_required": True,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 引导凭证（Web UI 引导页用，无 admin token 也能调，限 bootstrap 阶段）
# ─────────────────────────────────────────────────────────────────────────────

import httpx as _httpx


@router.get("/init-status")
async def init_status() -> dict:
    """Web UI 引导页检测用：是否需要引导用户填 BAI 凭证。

    返回:
      need_init: BAI_USERNAME/BAI_PASSWORD 是否未配置
      bai_initialized: BuildingAI 是否已经 /install 完成（root 账号存在）
    """
    need_init = not (BAI_USERNAME and BAI_PASSWORD)
    bai_initialized = True  # 默认 true 避免 isRoot 端点 5xx 时误报
    try:
        async with _httpx.AsyncClient(timeout=5.0) as c:
            r = await c.get(f"{BAI_BASE}/api/system/isRoot")
            if r.status_code == 200:
                bai_initialized = bool(r.json().get("data", {}).get("isRoot"))
            elif r.status_code in (401, 403):
                # BuildingAI 在 root 不存在时返 401/403
                bai_initialized = False
    except Exception as exc:
        log.warning("isRoot 检查失败: %s", exc)
    return {"need_init": need_init, "bai_initialized": bai_initialized}


class InitCredentialsPayload(BaseModel):
    bai_username: str = Field(..., min_length=1)
    bai_password: str = Field(..., min_length=1)


@router.post("/init-credentials")
async def init_credentials(payload: InitCredentialsPayload) -> dict:
    """Web UI 引导页提交 root 凭证：验证 → 写 .env → 重启。

    不要求 admin token（bootstrap 阶段还没设过）。
    流程：
      1. 试 login，验证凭证对
      2. 写 .env (BAI_USERNAME/BAI_PASSWORD)
      3. SIGTERM 触发 docker restart 拉起新进程
    """
    # 1. 验证 BuildingAI 能用这个 username/password 登录
    try:
        async with _httpx.AsyncClient(timeout=10.0) as c:
            r = await c.post(
                f"{BAI_BASE}/api/auth/login",
                json={
                    "username": payload.bai_username,
                    "password": payload.bai_password,
                    "terminal": 1,
                },
            )
    except Exception as exc:
        raise HTTPException(502, f"连不上 BuildingAI: {exc}") from exc

    if not (200 <= r.status_code < 300):
        # 401 = 凭证错，其他 4xx/5xx = 服务异常
        if r.status_code == 401:
            raise HTTPException(401, "用户名或密码错，请重试")
        raise HTTPException(502, f"BuildingAI 登录失败: {r.status_code}")

    # 2. 写 .env
    _update_env_file(
        ENV_FILE,
        {"BAI_USERNAME": payload.bai_username, "BAI_PASSWORD": payload.bai_password},
    )
    reload_user_keys(ENV_FILE)  # 顺手 reload，不影响（空 USERS 也没事）
    log.info(
        "引导完成：BAI_USERNAME=%s 凭证已写入 .env（即将重启）",
        payload.bai_username,
    )

    # 3. SIGTERM 触发重启（延迟 200ms 让响应先回）
    def _kill() -> None:
        import time as _t
        _t.sleep(0.2)
        os.kill(os.getpid(), signal.SIGTERM)

    threading.Thread(target=_kill, daemon=True).start()
    return {"ok": True, "restarting": True, "message": "凭证已保存，3 秒后重连"}


# ─────────────────────────────────────────────────────────────────────────────
# 重启（自己退出，依赖 docker restart: always 拉起）
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/system/restart")
async def system_restart(_: None = Depends(_verify_admin)) -> dict:
    """退出当前 a2a-gateway 进程。docker restart: always 会自动拉起。

    注意：当前 docker-compose 用的是 restart: on-failure:3，exit 0 不会重启——
    要让这个端点真的拉起新进程，需要把 docker-compose.yml 里 nodejs service
    的 restart 改成 `always`。
    """
    # 延迟 200ms 让响应先回
    def _kill():
        time.sleep(0.2)
        os.kill(os.getpid(), signal.SIGTERM)

    threading.Thread(target=_kill, daemon=True).start()
    return {"ok": True, "restarting": True, "note": "等 1-2s 后再访问 /health"}
