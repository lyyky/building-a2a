"""
A2A Server — FastAPI 实现 A2A 协议的 JSON-RPC 2.0 端点 + Agent Card。

端点：
  GET  /.well-known/agent.json          标准 A2A 发现端点（统一含 agent + KB）
  GET  /a2a/agents/{id}/card           单个 agent 的 Card
  GET  /a2a/datasets/{id}/card         单个 KB 的 Card
  POST /a2a/agents/{id}                A2A JSON-RPC 端点（chat）
  POST /a2a/datasets/{id}              A2A JSON-RPC 端点（retrieve）
  GET  /health                         健康检查
  GET  /agents                         registry 当前缓存的 agent 列表
  GET  /datasets                       registry 当前缓存的 KB 列表
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sse_starlette.sse import EventSourceResponse

import registry
from admin import router as admin_router
from config import GATEWAY_HOST, GATEWAY_PORT, bai
from config import Caller, user_pool as _user_pool_singleton

log = logging.getLogger("a2a.server")

app = FastAPI(title="BuildingAI A2A Gateway", version="0.1.0")
app.include_router(admin_router)


# ─────────────────────────────────────────────────────────────────────────────
# 多用户鉴权：每个外部调用方带自己的 API key
# ─────────────────────────────────────────────────────────────────────────────

async def verify_caller(authorization: str | None = Header(default=None)) -> Caller:
    """FastAPI Depends：从 Authorization: Bearer sk-... 拿 key，验过后返回 Caller。

    失败统一抛 401。如果 A2A_GATEWAY_USERS 是空的（无多用户配置），全部 key 都返回 None。
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "missing Authorization: Bearer <api_key>")
    api_key = authorization[7:].strip()
    if not api_key:
        raise HTTPException(401, "empty api key")
    caller = await _user_pool_singleton().get(api_key)
    if caller is None:
        raise HTTPException(401, "invalid api key or login failed")
    return caller


# ─────────────────────────────────────────────────────────────────────────────
# 会话隔离：每个 caller 自己的 conversationId 命名空间
# ─────────────────────────────────────────────────────────────────────────────

# api_key → {caller_conv_id → buildingai_conv_id}
_caller_convs: dict[str, dict[str, str]] = {}


def _make_caller_conv_id(api_key: str) -> str:
    """生成 caller 视角的 conv_id（短前缀 + uuid，方便外部引用）。"""
    prefix = hashlib.sha1(api_key.encode()).hexdigest()[:6]
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _resolve_buildingai_conv_id(api_key: str, caller_conv_id: str | None) -> str | None:
    """caller_conv_id → buildingai_conv_id。没映射就 None（让 BuildingAI 新建）。"""
    if not caller_conv_id:
        return None
    return _caller_convs.get(api_key, {}).get(caller_conv_id)


def _remember_buildingai_conv_id(api_key: str, caller_conv_id: str, bai_conv_id: str) -> None:
    if not bai_conv_id or not caller_conv_id:
        return
    _caller_convs.setdefault(api_key, {})[caller_conv_id] = bai_conv_id


def _remember_buildingai_conv_from_response(
    api_key: str, caller_conv_id: str, bai_response: Any
) -> None:
    """从 BuildingAI 响应里抽 conversationId 存进映射。"""
    if not isinstance(bai_response, dict):
        return
    data = bai_response.get("data")
    if not isinstance(data, dict):
        return
    bai_conv_id = data.get("conversationId")
    if bai_conv_id:
        _remember_buildingai_conv_id(api_key, caller_conv_id, str(bai_conv_id))

# 静态 UI（/ui 提供管理面板）
_UI_DIR = Path(__file__).parent / "ui"
if _UI_DIR.is_dir():
    app.mount("/ui", StaticFiles(directory=str(_UI_DIR), html=True), name="ui")

    @app.middleware("http")
    async def _ui_no_cache(request, call_next):
        """UI 静态资源调试期禁用缓存，避免浏览器拿到旧版（304 Not Modified）。"""
        if request.url.path.startswith("/ui/"):
            response = await call_next(request)
            response.headers["cache-control"] = "no-cache, must-revalidate"
            return response
        return await call_next(request)


# ─────────────────────────────────────────────────────────────────────────────
# MCP HTTP transport —— 让 BuildingAI 用 sse / streamable-http 接入
# ─────────────────────────────────────────────────────────────────────────────

try:
    from mcp_http import mcp_streamable_app, mcp_sse_app  # noqa: E402

    # mcp_http.py 里设置 streamable_http_path="/" 配合此处 mount 到 "/mcp"，
    # 这样外部路径就是 /mcp，内部匹配 "/"，路径干净。
    # 同样 SSE 用 mount_path="/mcp-sse"，内部 /mcp-sse/sse 和 /mcp-sse/messages/。
    app.mount("/mcp", mcp_streamable_app)
    app.mount("/mcp-sse", mcp_sse_app)
    log.info("MCP HTTP 已挂载：POST/GET /mcp (streamable-http), /mcp-sse/sse (sse)")
except Exception as exc:
    log.warning("MCP HTTP 挂载失败（不影响 A2A 接口）: %s", exc)


# ─────────────────────────────────────────────────────────────────────────────
# 工具函数
# ─────────────────────────────────────────────────────────────────────────────

def _err(req_id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def _ok(req_id: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _extract_text(message: dict) -> str:
    """A2A message → 纯文本。"""
    parts = message.get("parts") or []
    chunks = []
    for p in parts:
        if not isinstance(p, dict):
            continue
        if p.get("kind") == "text" or p.get("type") == "text":
            chunks.append(p.get("text", ""))
        elif "text" in p:
            chunks.append(str(p.get("text", "")))
    if chunks:
        return "\n".join(chunks).strip()
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        return content.get("text", "") or json.dumps(content, ensure_ascii=False)
    return ""


def _agent_card(agent: dict) -> dict:
    """agent → A2A Agent Card。"""
    aid = str(agent.get("id"))
    caps = registry.infer_capabilities(agent)
    return {
        "name": agent.get("name") or f"agent-{aid[:8]}",
        "description": agent.get("description", ""),
        "url": f"/a2a/agents/{aid}",
        "version": "1.0",
        "capabilities": {
            "streaming": True,
            "pushNotifications": False,
        },
        "defaultInputModes": ["text"],
        "defaultOutputModes": ["text"],
        "skills": [
            {
                "id": aid,
                "name": agent.get("name", ""),
                "description": agent.get("description", ""),
                "tags": caps,
            }
        ],
    }


def _dataset_card(dataset: dict) -> dict:
    """dataset → A2A Agent Card（KB 包成"只读 agent"）。"""
    did = str(dataset.get("id"))
    name = dataset.get("name") or f"kb-{did[:8]}"
    desc = dataset.get("description", "")
    return {
        "name": f"KB: {name}",
        "description": desc,
        "url": f"/a2a/datasets/{did}",
        "version": "1.0",
        "capabilities": {
            "streaming": False,
            "pushNotifications": False,
        },
        "defaultInputModes": ["text"],
        "defaultOutputModes": ["text", "data"],
        "skills": [
            {
                "id": did,
                "name": f"知识检索:{name}",
                "description": f"在 '{name}' 知识库中检索相关内容",
                "tags": ["rag", "retrieval", "search"],
            }
        ],
    }


# ─────────────────────────────────────────────────────────────────────────────
# A2A 发现端点
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/.well-known/agent.json")
async def well_known_agent_card() -> dict:
    """统一发现端点：所有 agent + 所有 KB。"""
    skills = []
    for a in registry.all_agents():
        skills.append(_agent_card(a)["skills"][0])
    for d in registry.all_datasets():
        skills.append(_dataset_card(d)["skills"][0])
    return {
        "name": "BuildingAI Pool",
        "description": "由 BuildingAI 管理的智能体与知识库池",
        "version": "1.0",
        "skills": skills,
    }


@app.get("/a2a/agents/{agent_id}/card")
async def get_agent_card(agent_id: str, request: Request) -> Any:
    a = registry.get_agent(agent_id)
    if not a:
        return JSONResponse({"error": "agent not found"}, status_code=404)
    card = _agent_card(a)
    return _negotiate(request, card, "agent")


@app.get("/a2a/datasets/{dataset_id}/card")
async def get_dataset_card(dataset_id: str, request: Request) -> Any:
    d = registry.get_dataset(dataset_id)
    if not d:
        return JSONResponse({"error": "dataset not found"}, status_code=404)
    card = _dataset_card(d)
    return _negotiate(request, card, "dataset")


# ─────────────────────────────────────────────────────────────────────────────
# Content negotiation
# ─────────────────────────────────────────────────────────────────────────────

_CARD_PAGE_HTML = """\
<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width,initial-scale=1" />
<title>{title}</title>
<style>
  :root {{ --bg:#f6f7fb; --panel:#fff; --text:#1f2330; --muted:#6b7280; --border:#e5e7eb; --accent:#4f46e5; --accent-soft:#eef2ff; --code-bg:#0b1020; --code-text:#e2e8f0; }}
  @media (prefers-color-scheme: dark) {{ :root {{ --bg:#0f1115; --panel:#1a1d24; --text:#e6e8ee; --muted:#9ca3af; --border:#2b2f3a; --accent:#818cf8; --accent-soft:#1e1f3a; --code-bg:#06080f; --code-text:#d1d5db; }} }}
  * {{ box-sizing:border-box; }} body {{ margin:0; background:var(--bg); color:var(--text); font:14px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", sans-serif; }}
  .wrap {{ max-width: 760px; margin: 28px auto; padding: 0 20px; }}
  a {{ color: var(--accent); text-decoration: none; }}
  h1 {{ margin: 0 0 4px; font-size: 22px; }}
  .pill {{ display:inline-block; padding:2px 10px; border-radius:999px; background:var(--accent-soft); color:var(--accent); font-size:12px; }}
  .meta {{ color: var(--muted); font-size:13px; margin: 8px 0 16px; }}
  .card {{ background: var(--panel); border:1px solid var(--border); border-radius: 10px; padding: 18px 22px; }}
  .row {{ display:flex; gap:12px; flex-wrap:wrap; padding: 6px 0; border-bottom: 1px dashed var(--border); }}
  .row:last-child {{ border-bottom:none; }}
  .lbl {{ color: var(--muted); width: 100px; flex-shrink:0; }}
  .skill {{ border:1px solid var(--border); border-radius:6px; padding:8px 12px; margin:8px 0; }}
  .skill h4 {{ margin: 0 0 4px; font-size: 14px; }}
  .skill .desc {{ color: var(--muted); font-size: 13px; }}
  .tag {{ display:inline-block; font-size:11px; padding:1px 6px; border-radius:4px; background:var(--accent-soft); color:var(--accent); margin-right:4px; }}
  details {{ margin-top:16px; }}
  pre {{ background: var(--code-bg); color: var(--code-text); padding: 10px 14px; border-radius:6px; overflow-x:auto; font-size:12.5px; }}
  .topbar {{ display:flex; align-items:center; gap:12px; margin-bottom: 16px; }}
  .btn {{ font:inherit; cursor:pointer; border:1px solid var(--border); background: var(--panel); color:var(--text); padding:6px 12px; border-radius:6px; }}
</style></head><body>
<div class="wrap">
  <div class="topbar">
    <a href="/ui/">← 返回管理面板</a>
    <span style="flex:1"></span>
    <a class="btn" href="{json_url}?as=json" target="_blank">在新 tab 看 JSON</a>
  </div>
  <h1>{name}</h1>
  <div class="meta"><span class="pill">{kind_label}</span> · version {version} · <code>{url}</code></div>
  <div class="card">
    {description}
    <div class="row"><div class="lbl">URL</div><div><code>{url_full}</code></div></div>
    <div class="row"><div class="lbl">Default IO</div><div>in={default_input_modes} → out={default_output_modes}</div></div>
    <div class="row"><div class="lbl">Capabilities</div><div>{capabilities}</div></div>
    <div class="row"><div class="lbl">Skills</div><div style="flex:1">
      {skills}
    </div></div>
  </div>
  <details>
    <summary><strong>原始 JSON</strong></summary>
    <pre>{json_text}</pre>
  </details>
</div>
</body></html>
"""


def _negotiate(request: Request, card: dict, kind: str) -> Any:
    """Content negotiation：浏览器（Accept: text/html）给 HTML，其它给 JSON。
    也支持 ?as=json 强制 JSON、?as=html 强制 HTML。
    """
    force = (request.query_params.get("as") or "").lower()
    accept = (request.headers.get("accept") or "").lower()
    wants_html = (
        force == "html"
        or (force not in ("json",) and "text/html" in accept and "application/json" not in accept)
    )
    if not wants_html:
        return card

    caps = card.get("capabilities") or {}
    caps_html = "<br>".join(f"<code>{k}</code>= {v}" for k, v in caps.items()) or "—"

    skills_html_parts = []
    for s in card.get("skills") or []:
        tags_html = "".join(
            f'<span class="tag">{t}</span>' for t in (s.get("tags") or [])
        )
        s_desc = s.get("description") or ""
        desc_html = f'<div class=desc>{_esc(s_desc)}</div>' if s_desc else ""
        skills_html_parts.append(
            f'<div class="skill">'
            f'<h4>{_esc(s.get("name") or s.get("id") or "(skill)")}</h4>'
            f'{desc_html}'
            f'{tags_html}'
            f'</div>'
        )
    skills_html = "\n".join(skills_html_parts) or "<em>(无)</em>"

    description = (
        f'<div class="row"><div class="lbl">Description</div><div>{_esc(card.get("description",""))}</div></div>'
        if card.get("description") else ""
    )

    html = _CARD_PAGE_HTML.format(
        title=_esc(card.get("name") or "A2A Card"),
        name=_esc(card.get("name", "")),
        kind_label="A2A Agent Card" if kind == "agent" else "A2A KB Card",
        version=_esc(card.get("version", "1.0")),
        url=_esc(card.get("url", "")),
        url_full=_esc(card.get("url", "")),
        description=description,
        default_input_modes=_esc(json.dumps(card.get("defaultInputModes") or [])),
        default_output_modes=_esc(json.dumps(card.get("defaultOutputModes") or [])),
        capabilities=caps_html,
        skills=skills_html,
        json_url=request.url.path,
        json_text=_esc(json.dumps(card, ensure_ascii=False, indent=2)),
    )
    return HTMLResponse(html)


def _esc(s: str) -> str:
    """最小化 HTML 转义（仅用于自包含页面，不引外链）。"""
    if not isinstance(s, str):
        s = str(s)
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


# ─────────────────────────────────────────────────────────────────────────────
# A2A JSON-RPC 端点
# ─────────────────────────────────────────────────────────────────────────────

@app.post("/a2a/agents/{agent_id}")
async def a2a_agent_endpoint(
    agent_id: str,
    request: Request,
    caller: Caller = Depends(verify_caller),
) -> Any:
    agent = registry.get_agent(agent_id)
    if not agent:
        return JSONResponse(_err(None, -32602, "agent not registered"), status_code=404)
    body = await request.json()
    req_id = body.get("id")
    method = body.get("method")
    params = body.get("params") or {}

    if method not in ("message/send", "message/stream"):
        return _err(req_id, -32601, f"method not supported: {method}")

    message = params.get("message") or {}
    text = _extract_text(message)
    if not text:
        return _err(req_id, -32602, "empty message")

    # 多用户会话隔离：caller 视角的 conv_id → BuildingAI 视角
    caller_conv_id = params.get("conversationId") or _make_caller_conv_id(caller.api_key)
    bai_conv_id = _resolve_buildingai_conv_id(caller.api_key, caller_conv_id)

    if method == "message/stream":
        async def event_gen():
            status = {
                "kind": "task",
                "id": str(uuid.uuid4()),
                "status": {"state": "submitted"},
            }
            yield {"event": "status", "data": json.dumps(status, ensure_ascii=False)}
            try:
                status["status"]["state"] = "working"
                yield {"event": "status", "data": json.dumps(status, ensure_ascii=False)}
                code, body2 = await caller.client.chat_agent(
                    agent_id, text, blocking=False, conversation_id=bai_conv_id
                )
                _remember_buildingai_conv_from_response(
                    caller.api_key, caller_conv_id, body2
                )
                if code >= 400:
                    err_text = json.dumps(body2, ensure_ascii=False)[:500]
                    err = {"kind": "error", "message": err_text}
                    yield {"event": "error", "data": json.dumps(err, ensure_ascii=False)}
                    status["status"]["state"] = "failed"
                else:
                    err_text = body2.get("error") if isinstance(body2, dict) else None
                    if err_text:
                        err = {"kind": "error", "message": f"[后端报错] {err_text}"}
                        yield {"event": "error", "data": json.dumps(err, ensure_ascii=False)}
                        status["status"]["state"] = "failed"
                    else:
                        reply_text = _extract_reply_text(body2)
                        artifact = {
                            "kind": "artifact",
                            "artifactId": str(uuid.uuid4()),
                            "name": "agent_reply",
                            "parts": [{"kind": "text", "text": reply_text}],
                        }
                        yield {"event": "artifact", "data": json.dumps(artifact, ensure_ascii=False)}
                        status["status"]["state"] = "completed"
            finally:
                yield {"event": "status", "data": json.dumps(status, ensure_ascii=False)}

        return EventSourceResponse(event_gen())

    # message/send（同步）
    code, body2 = await caller.client.chat_agent(agent_id, text, blocking=True, conversation_id=bai_conv_id)
    _remember_buildingai_conv_from_response(caller.api_key, caller_conv_id, body2)
    if code >= 400:
        return _err(req_id, -32000, f"agent chat failed: {body2}")

    # BuildingAI 流里上报了 error（比如 "Dify API Key 未配置"）→ task failed
    err_text = (body2 or {}).get("error") if isinstance(body2, dict) else None
    if err_text:
        return _ok(
            req_id,
            {
                "kind": "task",
                "id": str(uuid.uuid4()),
                "status": {"state": "failed", "message": err_text},
                "artifacts": [
                    {
                        "artifactId": str(uuid.uuid4()),
                        "name": "agent_error",
                        "parts": [
                            {"kind": "text", "text": f"[后端报错] {err_text}"},
                        ],
                    }
                ],
            },
        )

    reply_text = _extract_reply_text(body2)
    return _ok(
        req_id,
        {
            "kind": "task",
            "id": str(uuid.uuid4()),
            "status": {"state": "completed"},
            "artifacts": [
                {
                    "artifactId": str(uuid.uuid4()),
                    "name": "agent_reply",
                    "parts": [{"kind": "text", "text": reply_text}],
                }
            ],
        },
    )


def _extract_reply_text(body: Any) -> str:
    """从 BuildingAI 聊天响应里抽文本。"""
    if not isinstance(body, dict):
        return str(body) if body is not None else ""
    data = body.get("data") if isinstance(body.get("data"), dict) else body
    if isinstance(data, dict):
        # BuildingAI 实际响应路径并不稳定，先把几种都试一遍
        for path in (
            ("data", "message", "content"),
            ("data", "content"),
            ("message", "content"),
            ("content",),
        ):
            cur: Any = data
            for k in path:
                if not isinstance(cur, dict):
                    cur = None
                    break
                cur = cur.get(k)
            if isinstance(cur, str) and cur:
                return cur
        # 退化：把 data 整个序列化当文本（至少有内容）
        try:
            return json.dumps(data, ensure_ascii=False)
        except Exception:
            return str(data)
    if isinstance(data, str):
        return data
    return json.dumps(body, ensure_ascii=False)


@app.post("/a2a/datasets/{dataset_id}")
async def a2a_dataset_endpoint(
    dataset_id: str,
    request: Request,
    caller: Caller = Depends(verify_caller),
) -> Any:
    dataset = registry.get_dataset(dataset_id)
    if not dataset:
        return JSONResponse(_err(None, -32602, "dataset not registered"), status_code=404)
    body = await request.json()
    req_id = body.get("id")
    method = body.get("method")
    params = body.get("params") or {}

    if method != "message/send":
        return _err(req_id, -32601, f"dataset only supports message/send, got: {method}")

    text = _extract_text(params.get("message") or {})
    if not text:
        return _err(req_id, -32602, "empty message")

    code, resp = await caller.client.retrieve_dataset(dataset_id, text)
    if code >= 400:
        return _err(req_id, -32000, f"retrieve failed: {resp}")

    # BuildingAI 实际响应：{code, message, data: {records: [{segment: {...}, score}, ...], total}}
    records: list[dict] = []
    if isinstance(resp, dict):
        data = resp.get("data") if isinstance(resp.get("data"), dict) else {}
        records = data.get("records") or []
    segments_parts = []
    for rec in records:
        seg = (rec.get("segment") or {}) if isinstance(rec, dict) else {}
        segments_parts.append(
            {
                "kind": "data",
                "data": {
                    "documentName": seg.get("documentName") or seg.get("document_name"),
                    "content": seg.get("content", ""),
                    "score": rec.get("score"),
                },
            }
        )
    summary_text = f"找到 {len(segments_parts)} 个相关片段"
    parts = [{"kind": "text", "text": summary_text}, *segments_parts]
    return _ok(
        req_id,
        {
            "kind": "task",
            "id": str(uuid.uuid4()),
            "status": {"state": "completed"},
            "artifacts": [
                {
                    "artifactId": str(uuid.uuid4()),
                    "name": "retrieval_results",
                    "parts": parts,
                }
            ],
        },
    )


# ─────────────────────────────────────────────────────────────────────────────
# 辅助端点
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health() -> dict:
    return {
        "ok": True,
        "now": time.time(),
        "last_refresh_at": registry.last_refresh_at(),
        "agent_count": len(registry.all_agents()),
        "dataset_count": len(registry.all_datasets()),
    }


@app.get("/agents")
async def list_cached_agents() -> dict:
    return {
        "count": len(registry.all_agents()),
        "items": [
            {
                "id": a.get("id"),
                "name": a.get("name"),
                "description": a.get("description"),
                "capabilities": registry.infer_capabilities(a),
            }
            for a in registry.all_agents()
        ],
    }


@app.get("/datasets")
async def list_cached_datasets() -> dict:
    return {
        "count": len(registry.all_datasets()),
        "items": [
            {
                "id": d.get("id"),
                "name": d.get("name"),
                "description": d.get("description"),
            }
            for d in registry.all_datasets()
        ],
    }
