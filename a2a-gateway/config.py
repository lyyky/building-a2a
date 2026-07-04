"""
A2A Gateway 配置 + BuildingAI HTTP 客户端。

BuildingAI 实际 API（与 README 草案的差异已修正）：
  - Console 前缀  /consoleapi   →  /consoleapi/agents、/consoleapi/datasets
  - Web 前缀      /api           →  /api/auth/login、/api/ai-agents/{id}/chat/stream
                                    /api/ai-datasets/{id}/retrieve
  - 鉴权：cookie-based JWT，无 Bearer token。登录后从 Set-Cookie 取 token。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from cryptography.fernet import Fernet, InvalidToken

log = logging.getLogger("a2a.config")

# .env 文件路径（A2A_GATEWAY_ENV_FILE 配时用，否则跟 a2a-gateway 同目录）
# 容器化部署：compose 把 ./a2a-gateway:/app 整个挂载，再把宿主 ./.env 挂到 /app/host.env
# 所以容器内 ENV_FILE 实际是 /app/host.env（对应宿主 /root/buildingai-clean/.env）。
ENV_FILE: Path = Path(os.getenv("A2A_GATEWAY_ENV_FILE", str(Path(__file__).parent / ".env"))).resolve()


# ─────────────────────────────────────────────────────────────────────────────
# 基础配置
# ─────────────────────────────────────────────────────────────────────────────

BAI_BASE: str = os.getenv("BAI_BASE", "http://localhost:4090").rstrip("/")
# 凭证只从环境变量取，不在源码里留默认——
# 用法：export BAI_USERNAME=xxx BAI_PASSWORD=xxx
BAI_USERNAME: str | None = os.getenv("BAI_USERNAME")
BAI_PASSWORD: str | None = os.getenv("BAI_PASSWORD")
GATEWAY_PORT: int = int(os.getenv("GATEWAY_PORT", "8000"))
GATEWAY_HOST: str = os.getenv("GATEWAY_HOST", "0.0.0.0")

# MCP 传输层 Host 校验
# 默认放行 localhost/loopback；额外放行由 MCP_ALLOWED_HOSTS 逗号分隔加进来（格式 "host:*" 或 "host:port"）。
# 如果 MCP_DISABLE_REBINDING=true，**完全关掉** DNS rebinding 保护（仅内网/可信环境用）。
MCP_ALLOWED_HOSTS: list[str] = [
    h.strip() for h in os.getenv(
        "MCP_ALLOWED_HOSTS",
        "127.0.0.1:*,localhost:*,[::1]:*",
    ).split(",") if h.strip()
]
MCP_ALLOWED_ORIGINS: list[str] = [
    o.strip() for o in os.getenv(
        "MCP_ALLOWED_ORIGINS",
        "http://127.0.0.1:*,http://localhost:*,http://[::1]:*",
    ).split(",") if o.strip()
]
MCP_DISABLE_REBINDING: bool = os.getenv("MCP_DISABLE_REBINDING", "false").lower() in ("1", "true", "yes")

POLL_INTERVAL: int = int(os.getenv("POLL_INTERVAL", "30"))
DATASET_TOP_K: int = int(os.getenv("DATASET_TOP_K", "5"))
DATASET_SCORE_THRESHOLD: float = float(os.getenv("DATASET_SCORE_THRESHOLD", "0.5"))


# ─────────────────────────────────────────────────────────────────────────────
# 多用户鉴权（API key → BuildingAI 账号，密码用 Fernet 加密存 .env）
# ─────────────────────────────────────────────────────────────────────────────

# Fernet master key（用 `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` 生成）
A2A_GATEWAY_MASTER_KEY: str | None = os.getenv("A2A_GATEWAY_MASTER_KEY")
# 格式：api_key1:bai_user1:enc_pwd1,api_key2:bai_user2:enc_pwd2
# enc_pwd 用下面 encrypt_password() 生成
A2A_GATEWAY_USERS_RAW: str = os.getenv("A2A_GATEWAY_USERS", "")

_fernet: Fernet | None = None
if A2A_GATEWAY_MASTER_KEY:
    try:
        _fernet = Fernet(A2A_GATEWAY_MASTER_KEY.encode())
    except Exception as exc:
        log.warning("A2A_GATEWAY_MASTER_KEY 无效（多用户鉴权将不可用）: %s", exc)


def _parse_users(raw: str) -> dict[str, dict]:
    """解析 A2A_GATEWAY_USERS=api_key1:user1:enc_pwd1,api_key2:user2:enc_pwd2"""
    out: dict[str, dict] = {}
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        parts = entry.split(":")
        if len(parts) != 3:
            log.warning(
                "A2A_GATEWAY_USERS 格式不对（要 api_key:username:enc_pwd，跳过这一项）: %r",
                entry,
            )
            continue
        api_key, username, enc_pwd = (p.strip() for p in parts)
        if not api_key or not username or not enc_pwd:
            log.warning("A2A_GATEWAY_USERS 某字段为空，跳过: %r", entry)
            continue
        out[api_key] = {"username": username, "password_enc": enc_pwd}
    return out


USER_KEYS: dict[str, dict] = _parse_users(A2A_GATEWAY_USERS_RAW)


# 启动时从 .env 文件读（容器里 A2A_GATEWAY_USERS env 默认空，必须靠 .env 文件）
# 模块级 import 时调一次，让进程启动就有用户；admin.py addUser/delUser 后续再 reload。
if ENV_FILE.exists():
    try:
        for _line in ENV_FILE.read_text(encoding="utf-8", errors="ignore").splitlines():
            _s = _line.strip()
            if _s.startswith("A2A_GATEWAY_USERS="):
                A2A_GATEWAY_USERS_RAW = _s.split("=", 1)[1].strip()
            elif _s.startswith("A2A_GATEWAY_MASTER_KEY="):
                A2A_GATEWAY_MASTER_KEY = _s.split("=", 1)[1].strip()
        if A2A_GATEWAY_MASTER_KEY:
            try:
                _fernet = Fernet(A2A_GATEWAY_MASTER_KEY.encode())
            except Exception as _exc:
                log.warning("启动时 MASTER_KEY 无效: %s", _exc)
        USER_KEYS = _parse_users(A2A_GATEWAY_USERS_RAW)
        log.info("启动时从 .env 加载：共 %d 个用户 (env_file=%s)", len(USER_KEYS), ENV_FILE)
    except Exception as _exc:
        log.warning("启动时读 .env 失败（继续以空 USERS 运行）: %s", _exc)


def reload_user_keys(env_file: "Path | None" = None) -> int:
    """从 .env 文件（或环境变量）重读 A2A_GATEWAY_USERS + MASTER_KEY，更新 USER_KEYS 和 _fernet。

    admin 端点调这个让新加的用户立刻生效（不需要重启 a2a-gateway）。
    返新解析出的 key 数量。
    """
    global USER_KEYS, A2A_GATEWAY_USERS_RAW, _fernet
    if env_file is not None:
        from pathlib import Path as _P
        p = _P(env_file)
        if p.exists():
            raw_users = ""
            raw_master = ""
            for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
                s = line.strip()
                if s.startswith("A2A_GATEWAY_USERS="):
                    raw_users = s.split("=", 1)[1].strip()
                elif s.startswith("A2A_GATEWAY_MASTER_KEY="):
                    raw_master = s.split("=", 1)[1].strip()
            A2A_GATEWAY_USERS_RAW = raw_users
            A2A_GATEWAY_MASTER_KEY = raw_master
            # 重建 _fernet
            if raw_master:
                try:
                    _fernet = Fernet(raw_master.encode())
                except Exception as exc:
                    log.warning("reload: MASTER_KEY 无效: %s", exc)
                    _fernet = None
            else:
                _fernet = None
        else:
            A2A_GATEWAY_USERS_RAW = ""
            _fernet = None
    else:
        # 不带 env_file 裸调用：从环境变量读 A2A_GATEWAY_USERS。
        # 但容器里 compose 默认 A2A_GATEWAY_USERS=（空），裸调会清空所有用户。
        # 强制要求传 env_file，避免误用。
        raise RuntimeError(
            "reload_user_keys() 必须传 env_file 参数（从 .env 文件读）。"
            "裸调用从环境变量读会清空用户列表（容器里 A2A_GATEWAY_USERS env 为空）。"
        )
    USER_KEYS = _parse_users(A2A_GATEWAY_USERS_RAW)
    log.info("reload_user_keys: 共 %d 个用户", len(USER_KEYS))
    return len(USER_KEYS)


def encrypt_password(plain: str) -> str:
    """生成 A2A_GATEWAY_USERS 配置里要填的密文。

    用法：
        python -c "from config import encrypt_password; print(encrypt_password('xxx'))"
    """
    if _fernet is None:
        raise RuntimeError("A2A_GATEWAY_MASTER_KEY 未配置，无法加密")
    return _fernet.encrypt(plain.encode()).decode()


def decrypt_password(enc: str) -> str:
    if _fernet is None:
        raise RuntimeError("A2A_GATEWAY_MASTER_KEY 未配置，无法解密")
    return _fernet.decrypt(enc.encode()).decode()


@dataclass
class Caller:
    """一个外部调用方的上下文：自己的 api_key、BuildingAI 账号、独立 client。"""

    api_key: str
    username: str
    client: BuildingAIClient


class UserPool:
    """按 api_key 懒加载 Caller，401 时自动重登。"""

    def __init__(self) -> None:
        self._by_key: dict[str, Caller] = {}
        self._lock = asyncio.Lock()

    async def get(self, api_key: str) -> Caller | None:
        if not api_key:
            return None
        # 单用户模式 fallback：A2A_GATEWAY_USERS 未配置时，
        # 任何非空 api_key 都视为"以 BAI_USERNAME/BAI_PASSWORD 身份访问"。
        if not USER_KEYS:
            return await self._get_single_user_caller(api_key)
        # 多用户模式：按 api_key 查表
        # 快路径：已加载 + 已登录
        c = self._by_key.get(api_key)
        if c is not None and c.client._logged_in:  # noqa: SLF001
            return c
        # 慢路径：双检 + 加载 / 重新登录
        async with self._lock:
            c = self._by_key.get(api_key)
            if c is not None and c.client._logged_in:  # noqa: SLF001
                return c
            return await self._load_or_login(api_key)

    async def _get_single_user_caller(self, api_key: str) -> Caller | None:
        """单用户模式：所有 caller 共用一个 BAI_USERNAME 的 client，key 只用于 caller_conv_id 命名空间。"""
        # 快路径
        c = self._by_key.get(api_key)
        if c is not None and c.client._logged_in:  # noqa: SLF001
            return c
        async with self._lock:
            c = self._by_key.get(api_key)
            if c is not None and c.client._logged_in:  # noqa: SLF001
                return c
            if not BAI_USERNAME or not BAI_PASSWORD:
                log.error("单用户模式但 BAI_USERNAME/BAI_PASSWORD 未配")
                return None
            if c is not None:
                # 复用 client（env 改了直接重登）
                c.client._username = BAI_USERNAME  # noqa: SLF001
                c.client._password = BAI_PASSWORD  # noqa: SLF001
                c.client._logged_in = False  # noqa: SLF001
                if await c.client.login():
                    return c
                return None
            client = BuildingAIClient.__new__(BuildingAIClient)
            client._base = BAI_BASE  # noqa: SLF001
            client._username = BAI_USERNAME  # noqa: SLF001
            client._password = BAI_PASSWORD  # noqa: SLF001
            client._client = None  # noqa: SLF001
            client._lock = asyncio.Lock()  # noqa: SLF001
            client._logged_in = False  # noqa: SLF001
            if not await client.login():
                log.warning("单用户模式首次登录失败: user=%s", BAI_USERNAME)
                return None
            caller = Caller(api_key=api_key, username=BAI_USERNAME, client=client)
            self._by_key[api_key] = caller
            log.info("单用户模式：api_key=%s... → BAI user=%s 登录成功", api_key[:8], BAI_USERNAME)
            return caller

    async def _load_or_login(self, api_key: str) -> Caller | None:
        cfg = USER_KEYS.get(api_key)
        if not cfg:
            return None
        if _fernet is None:
            log.error("A2A_GATEWAY_MASTER_KEY 未配置，多用户鉴权不可用")
            return None
        try:
            password = decrypt_password(cfg["password_enc"])
        except InvalidToken:
            log.warning("api_key=%s... 密码解密失败（MASTER_KEY 配错？）", api_key[:8])
            return None
        username = cfg["username"]

        existing = self._by_key.get(api_key)
        if existing is not None:
            # 复用 client（user/password 可能因 MASTER_KEY 重设而变），重登
            existing.client._username = username  # noqa: SLF001
            existing.client._password = password  # noqa: SLF001
            existing.client._logged_in = False  # noqa: SLF001
            if await existing.client.login():
                return existing
            return None

        # 全新 client（绕开 __init__ 的"必须传 username/password"检查，
        # 我们已经显式 set 了）
        client = BuildingAIClient.__new__(BuildingAIClient)
        client._base = BAI_BASE  # noqa: SLF001
        client._username = username  # noqa: SLF001
        client._password = password  # noqa: SLF001
        client._client = None  # noqa: SLF001
        client._lock = asyncio.Lock()  # noqa: SLF001
        client._logged_in = False  # noqa: SLF001
        if not await client.login():
            log.warning("用户池首次登录失败: api_key=%s... user=%s", api_key[:8], username)
            return None
        caller = Caller(api_key=api_key, username=username, client=client)
        self._by_key[api_key] = caller
        log.info("用户池：api_key=%s... → BuildingAI user=%s 登录成功", api_key[:8], username)
        return caller

    def list_known(self) -> list[str]:
        """返回已配置的 api_key 列表（不带密文，admin 排错用）。"""
        return list(USER_KEYS.keys())


_user_pool: UserPool | None = None


def user_pool() -> UserPool:
    """全局用户池（懒加载单例）。"""
    global _user_pool
    if _user_pool is None:
        _user_pool = UserPool()
    return _user_pool


# ─────────────────────────────────────────────────────────────────────────────
# BuildingAI 客户端（cookie-jar + 自动登录）
# ─────────────────────────────────────────────────────────────────────────────

class BuildingAIClient:
    """对 BuildingAI 的封装，自管 cookie 和登录态。"""

    def __init__(self) -> None:
        self._base = BAI_BASE
        self._username = BAI_USERNAME
        self._password = BAI_PASSWORD
        if not self._username or not self._password:
            raise RuntimeError(
                "缺少 BAI_USERNAME / BAI_PASSWORD 环境变量。"
                "A2A Gateway 通过 BuildingAI 账号登录获取 cookie，"
                "请设置后再启动。"
            )
        self._client: httpx.AsyncClient | None = None
        self._lock = asyncio.Lock()
        self._logged_in = False

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30.0)
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def login(self) -> bool:
        """登录 BuildingAI 并保存 cookie + token。失败返回 False。"""
        async with self._lock:
            client = await self._ensure_client()
            try:
                r = await client.post(
                    f"{self._base}/api/auth/login",
                    json={
                        "username": self._username,
                        "password": self._password,
                        "terminal": 1,  # 1=PC, 2=H5, 3=MP, 4=APP
                    },
                )
                # BuildingAI 对成功登录返回 201（Created），同时设 cookie 与返回 token
                if 200 <= r.status_code < 300:
                    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
                    token = (body.get("data") or {}).get("token") if isinstance(body, dict) else None
                    if token:
                        client.headers["Authorization"] = f"Bearer {token}"
                    self._logged_in = True
                    log.info(
                        "BuildingAI 登录成功（username=%s, status=%s, has_cookie=%s, has_bearer=%s）",
                        self._username, r.status_code, bool(r.cookies), bool(token),
                    )
                    return True
                log.warning("BuildingAI 登录失败 %s: %s", r.status_code, r.text[:200])
            except Exception as exc:
                log.warning("BuildingAI 登录异常: %s", exc)
            self._logged_in = False
            return False

    async def _request(self, method: str, path: str, **kw: Any) -> httpx.Response:
        client = await self._ensure_client()
        if not self._logged_in:
            await self.login()
        r = await client.request(method, f"{self._base}{path}", **kw)
        # 401 重试一次
        if r.status_code == 401:
            await self.login()
            r = await client.request(method, f"{self._base}{path}", **kw)
        return r

    async def get_json(self, path: str, params: dict | None = None) -> dict:
        r = await self._request("GET", path, params=params)
        if r.status_code >= 400:
            log.warning("GET %s 失败 %s: %s", path, r.status_code, r.text[:200])
            return {}
        try:
            return r.json()
        except Exception:
            return {}

    async def post_json(self, path: str, body: dict) -> tuple[int, dict | str]:
        r = await self._request("POST", path, json=body)
        try:
            return r.status_code, r.json()
        except Exception:
            return r.status_code, r.text

    # ── 业务封装 ─────────────────────────────────────────────────────────────

    async def list_agents(self) -> list[dict]:
        """列 agent。返回 [{id, name, description, ...}, ...]"""
        data = await self.get_json("/consoleapi/agents")
        if isinstance(data, dict):
            items = data.get("items") or data.get("data") or []
            if isinstance(items, dict):
                items = items.get("items") or []
            return items if isinstance(items, list) else []
        return []

    async def get_agent(self, agent_id: str) -> dict | None:
        data = await self.get_json(f"/consoleapi/agents/{agent_id}")
        if isinstance(data, dict):
            return data.get("data") or data
        return None

    async def list_datasets(self) -> list[dict]:
        """列 KB。"""
        data = await self.get_json("/consoleapi/datasets")
        if isinstance(data, dict):
            items = data.get("items") or data.get("data") or []
            if isinstance(items, dict):
                items = items.get("items") or []
            return items if isinstance(items, list) else []
        return []

    async def get_dataset(self, dataset_id: str) -> dict | None:
        data = await self.get_json(f"/consoleapi/datasets/{dataset_id}")
        if isinstance(data, dict):
            return data.get("data") or data
        return None

    async def chat_agent(
        self,
        agent_id: str,
        text: str,
        *,
        blocking: bool = True,
        conversation_id: str | None = None,
        metadata: dict | None = None,
    ) -> tuple[int, Any]:
        log.warning("chat_agent CALL agent_id=%s text_len=%d", agent_id, len(text or ""))
        """调 BuildingAI agent 聊天。

        BuildingAI 的 /chat/stream 接口是 SSE 流式，blocking 模式在 1.15 里调假
        Response 模拟，会抛 'response.writeHead is not a function'。
        对策：始终走流式端点，httpx 读完 SSE 流后合并文本/text-delta 返回。
        """
        msg: dict[str, Any] = {
            "role": "user",
            "parts": [{"type": "text", "text": text}],
        }
        body: dict[str, Any] = {
            "responseMode": "streaming",
            "message": msg,
            # 默认让 BuildingAI 保存会话（它自带实现，传 false 某些 agent 会空响应）
            "saveConversation": True,
        }
        if conversation_id:
            body["conversationId"] = conversation_id

        client = await self._ensure_client()
        url = f"{self._base}/api/ai-agents/{agent_id}/chat/stream"

        if not self._logged_in:
            await self.login()
        try:
            async with client.stream("POST", url, json=body, timeout=300.0) as r:
                # 401 时重登一次
                if r.status_code == 401:
                    await self.login()
                    async with client.stream("POST", url, json=body, timeout=300.0) as r2:
                        return await self._collect_sse(r2)
                return await self._collect_sse(r)
        except Exception as exc:
            log.warning("chat_agent 异常: %s", exc)
            return 500, {"error": str(exc)}

    async def _collect_sse(self, r: Any) -> tuple[int, Any]:
        """读 SSE 流：合并 text-delta，捕获 conversationId/messageId，识别 type=error
        （BuildingAI 在 LLM provider 配错时会发这种事件，不识别就会"静默空响应"）。
        """
        log.warning("chat-sse-debug _collect_sse enter status=%s ct=%s",
                    r.status_code, r.headers.get("content-type"))
        if r.status_code >= 400:
            text = (await r.aread()).decode("utf-8", errors="ignore")
            log.warning("chat-sse-debug 4xx body: %s", text[:300])
            return r.status_code, {"raw": text[:2000]}

        chunks: list[str] = []
        conversation_id: str | None = None
        message_id: str | None = None
        error_text: str | None = None
        event_count = 0

        # 改用 iter_bytes + 自己切行
        # BuildingAI 用的是简化的 SSE：每行 "data: {...}" 一条事件，没有空行分隔
        #（与 SSE 标准 \n\n 分隔事件不同）。所以按单 \n 切，每行就是一条事件。
        buf = ""
        chunk_count = 0
        total_bytes = 0

        def _process_buf(remaining: str) -> str:
            """切完整行处理，留不到行末的返回剩余 buf。"""
            nonlocal event_count, error_text, conversation_id, message_id, chunks
            lines = remaining.split("\n")
            tail = lines[-1]  # 最后一段可能不完整，保留
            for line in lines[:-1]:
                line = line.strip()
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload in ("", "[DONE]"):
                    continue
                try:
                    obj = json.loads(payload)
                except Exception:
                    continue
                event_count += 1
                t = obj.get("type")
                if t == "text-delta":
                    delta = obj.get("delta") or ""
                    if delta:
                        chunks.append(delta)
                elif t == "data-conversation-id":
                    conversation_id = obj.get("data")
                elif t == "data-assistant-message-id":
                    message_id = obj.get("data")
                elif t == "error":
                    error_text = obj.get("errorText") or obj.get("error") or json.dumps(obj, ensure_ascii=False)
            return tail

        async for chunk in r.aiter_bytes():
            chunk_count += 1
            total_bytes += len(chunk)
            buf += chunk.decode("utf-8", errors="ignore")
            buf = _process_buf(buf)

        # 残留 buf 也处理
        if buf.strip():
            _process_buf(buf + "\n")

        log.warning("chat-sse-debug end chunks=%d events=%d total_bytes=%d err=%s cid=%s",
                    chunk_count, event_count, total_bytes, error_text, conversation_id)

        full_text = "".join(chunks)

        if error_text and not full_text:
            # 真正的失败：返 error，让 a2a_server 包装成 failed task
            return 200, {
                "error": error_text,
                "_events": event_count,
                "_conversationId": conversation_id,
            }

        # 即使有 error，也有可能 text 都拿到了（万一），一并透传
        result: dict[str, Any] = {
            "data": {
                "message": {
                    "role": "assistant",
                    "parts": [{"type": "text", "text": full_text}],
                    "content": full_text,
                },
                "conversationId": conversation_id,
                "messageId": message_id,
            },
            "_events": event_count,
        }
        if error_text:
            result["error"] = error_text  # 文本拿到了但也有 error，UI 能看到
        return 200, result

    async def retrieve_dataset(self, dataset_id: str, query: str) -> tuple[int, Any]:
        """知识库检索。BuildingAI 1.15 接受扁平 topK/scoreThreshold，不要嵌套 retrievalModel（会返 40000）。"""
        body = {
            "query": query,
            "topK": DATASET_TOP_K,
            "scoreThreshold": DATASET_SCORE_THRESHOLD,
        }
        return await self.post_json(f"/api/ai-datasets/{dataset_id}/retrieve", body)


_bai: BuildingAIClient | None = None


def bai() -> BuildingAIClient:
    """全局 BuildingAI 客户端（懒加载）。"""
    global _bai
    if _bai is None:
        _bai = BuildingAIClient()
    return _bai
