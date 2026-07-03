"""
A2A Gateway 入口。

一个进程同时跑两件事：
1. FastAPI（HTTP，A2A Server）在 GATEWAY_PORT（默认 8000）。
2. 后台 asyncio 任务跑 registry 轮询。

注意：MCP stdio server 必须独占 stdin/stdout，所以 `a2a_mcp.py` 是单独的进程，
通过 BuildingAI 的 MCP 配置 `python /root/a2a-gateway/a2a_mcp.py` 启动。
"""

from __future__ import annotations

import asyncio
import logging
import sys
from contextlib import asynccontextmanager

import httpx
import uvicorn

import registry
from a2a_server import app
from config import BAI_BASE, BAI_PASSWORD, BAI_USERNAME, GATEWAY_HOST, GATEWAY_PORT, bai


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("a2a.main")


async def _wait_for_buildingai_up() -> None:
    """轮询 BuildingAI 端点，确认 NestJS 已就绪。

    用于同容器部署：a2a-gateway 跟 BuildingAI 先后启动时，先等 BuildingAI listening。

    BuildingAI 的 /consoleapi/system/runtime 在 /install 完成后返 401（要鉴权），
    200 在 /install 之前。两者都说明 server 起来了。
    5xx / connection refused 才是真的没起。
    """
    log.info("等待 BuildingAI 启动（GET %s/consoleapi/system/runtime）...", BAI_BASE)
    while True:
        try:
            async with httpx.AsyncClient(timeout=5.0) as c:
                r = await c.get(f"{BAI_BASE}/consoleapi/system/runtime")
                if r.status_code in (200, 401, 403):
                    log.info("BuildingAI 已就绪（status=%d）", r.status_code)
                    return
        except Exception:
            pass
        await asyncio.sleep(5)


async def _wait_for_admin() -> None:
    """轮询 BuildingAI login，等用户在 Web 端 /install 创建 root 账号。

    BuildingAI 的 root 账号只能通过 system/initialize 端点创建，且在 Web 端
    安装向导完成前不存在。这里每 10s 试一次 login，账号创建出来就成功。
    """
    log.info("等待 root 账号创建（用户在 Web 端 /install 设的 admin）...")
    while True:
        try:
            if await bai().login():
                log.info("登录成功（说明 root 账号已存在）")
                return
        except Exception as exc:
            log.info("login 异常 %s，10s 后重试", exc)
        await asyncio.sleep(10)


@asynccontextmanager
async def lifespan(app_):
    # MCP streamable_http 需要 session_manager.run() 进入异步上下文，
    # 与 FastAPI 的 lifespan 配合。
    from mcp_http import mcp_lifespan  # noqa: PLC0415（延迟 import 避免循环）

    # 凭证检查：未配置就进入"半启动"——HTTP/MCP 端点可访问但调 agent 报 401
    if not BAI_USERNAME or not BAI_PASSWORD:
        log.warning("=" * 60)
        log.warning("BAI_USERNAME / BAI_PASSWORD 未配置")
        log.warning("a2a-gateway 会在 BuildingAI 端就绪后不断重试 login。")
        log.warning("修法：在 .env 填 BAI_USERNAME/BAI_PASSWORD 后重启容器。")
        log.warning("=" * 60)
    else:
        await _wait_for_buildingai_up()
        await _wait_for_admin()
        log.info("已登录 BuildingAI")
        try:
            a, d = await registry.refresh_once()
            log.info("首次 registry 拉取完成：agents=%d datasets=%d", a, d)
        except Exception as exc:
            log.warning("首次 registry 拉取失败（继续运行，poller 会重试）: %s", exc)

    poller_task = await registry.start_poller()

    async with mcp_lifespan(app_) as _:
        try:
            yield
        finally:
            poller_task.cancel()
            try:
                await poller_task
            except asyncio.CancelledError:
                pass
            await bai().close()


# 把 lifespan 绑到 FastAPI app
app.router.lifespan_context = lifespan


from fastapi.responses import RedirectResponse  # noqa: E402


@app.get("/", include_in_schema=False)
async def root_redirect():
    """`/` 自动跳到 `/ui/`。"""
    return RedirectResponse(url="/ui/", status_code=307)


def main() -> None:
    log.info("A2A Gateway → http://%s:%d", GATEWAY_HOST, GATEWAY_PORT)
    uvicorn.run(app, host=GATEWAY_HOST, port=GATEWAY_PORT, log_level="info")


if __name__ == "__main__":
    main()
