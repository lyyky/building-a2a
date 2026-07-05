# 实施后的差异对照（基于实地核查）

## 与 README 草案的差异（已修正）

| 项 | README 草案 | 实际 | 代码里怎么走 |
|---|---|---|---|
| 列 agent | `/consoleapi/ai-agents` | `/consoleapi/agents` | `config.list_agents` 已用 `agents` |
| 列 KB | `/consoleapi/ai-datasets` | `/consoleapi/datasets` | `config.list_datasets` 已用 `datasets` |
| agent 聊天 | `/api/ai-agents/{id}/chat/blocking` | `/api/ai-agents/{id}/chat/stream`（传 `responseMode:"blocking"` 拿非流式） | `config.chat_agent` 一个端点搞定两种 |
| KB 检索 | `/api/console/ai-datasets/{id}/retrieve` | `/api/ai-datasets/{id}/retrieve` | `config.retrieve_dataset` |
| 认证 | Bearer token | Cookie-based JWT + `terminal` 字段 | `config.BuildingAIClient.login` 自动登录并注入 cookie/Authorization |
| 列表响应 | 自定义字段名 | 实际是 `{code,message,data:{items:[...]}}` | `list_agents`/`list_datasets` 兼容多种 schema |

## 实施文件清单

```
/root/a2a-gateway/
├── README.md            原方案文档（保留，未改）
├── CHANGES.md           本文件：差异对照与测试清单
├── requirements.txt     Python 依赖
├── config.py            配置 + BuildingAI 客户端（cookie 登录 + 自动重试）
├── registry.py          agent+dataset 缓存，30s 轮询
├── a2a_server.py        FastAPI 实现 A2A Server
├── a2a_client.py        A2A 标准客户端
├── a2a_mcp.py           MCP stdio server，工具按 registry 动态生成
├── main.py              FastAPI 入口，注册 lifespan
├── start.sh             一键启动脚本
└── test.sh              冒烟测试脚本
```

## 已静态验证 ✅

- 6 个模块全部可 import（无语法/导入错误）
- 凭证缺失时 `BuildingAIClient()` 抛清晰 RuntimeError
- FastAPI 挂载了完整路由表：
  - `GET /.well-known/agent.json`
  - `GET /a2a/agents/{id}/card`、`GET /a2a/datasets/{id}/card`
  - `POST /a2a/agents/{id}`（message/send + message/stream）
  - `POST /a2a/datasets/{id}`（message/send）
  - `GET /health`、`GET /agents`、`GET /datasets`

## 待运行测试（需凭证）

由于 BuildingAI 已安装但拿不到管理员凭证，**真实启动 + chat/检索调用**没有实跑。
等你 export 后按以下命令一键测完：

```bash
export BAI_USERNAME=你的管理员账号
export BAI_PASSWORD=你的密码
bash /root/a2a-gateway/start.sh        # 后台跑（前台也行）
bash /root/a2a-gateway/test.sh         # 另一终端跑

# 或手工验：
curl http://localhost:8000/health
curl http://localhost:8000/.well-known/agent.json | jq
curl http://localhost:8000/agents | jq

# 找出一个 agent_id 后调 A2A：
curl -X POST http://localhost:8000/a2a/agents/<id> \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"message/send",
       "params":{"message":{"role":"user","parts":[{"kind":"text","text":"hi"}]}}}'

# 在 BuildingAI 控制台 AI 应用 → MCP 添加：
#   名称: a2a-gateway
#   类型: stdio
#   命令: python /root/a2a-gateway/a2a_mcp.py
```

## 关键决策说明

1. **不写死默认账号密码**：源码不放凭证，必须从环境变量来。
2. **MCP stdio 与 FastAPI 分进程**：stdin/stdout 必须独占，所以 MCP 进程独立；
   BuildingAI 后台通过"stdio" MCP 配置拉起 `a2a_mcp.py`。
3. **MCP 调 A2A 时用 HTTP**：MCP 进程内 `A2AClient` 走 HTTP `localhost:8000`，
   不直接调 BuildingAI，避免重复登录。

## 已知限制

- `chat_agent` 当前固定传 `{role:"user",content:text}`；BuildingAI 实际 DTO 是 AI SDK 5 的 `UIMessage` 格式（`{role, parts:[...]}`），不同 agent 配置可能要求不同 message 形态。
  实际跑起来若发现 agent 端校验失败，可在 `config.chat_agent` 里把 message 改成 `{role, parts:[{type:"text", text}]}` 重试。
- 检索响应 BuildingAI 字段结构 `{data:{records:[{segment, score}, ...]}}` 是按已知 schema 解析的；若你看到的字段不一致，告诉我我加上回退路径。
- A2A 流式响应（`message/stream`）会用到 SSE，但实际 BuildingAI `chat/stream` 端点本身就是 SSE 式的、AI SDK 格式；当前实现是「一次取完整结果再切成 event」，
  不是真正的逐 token 流。够用，但严格 A2A 长任务场景可能需要重写 event_gen() 透传。

## `call_a2a_resource` 多 agent 协作 + KB 上下文

`mcp_http.py` 里的 `call_a2a_resource` 现在支持三种可选叠加（仅 `kind="agent"` 时生效）：

| 参数 | 类型 | 行为 |
|---|---|---|
| `chain` | `list[str]` | 顺序链：A 输出 → 喂给 B → ...，返回最后一个 agent 的输出 |
| `parallel_agents` | `list[str]` | 并行广播：同 query 调多个 agent，结果聚合成多段文本 |
| `kb_ids` | `list[str]` | 调主 agent 前先并行检索这些 KB，片段作为上下文拼到 query |

**典型组合**：先查 KB → 调专家 agent → 让评审 agent 过一遍。

```python
call_a2a_resource(
    kind="agent",
    id="专家-agent-id",
    query="X 产品的保修条款？",
    kb_ids=["手册-kb-id", "FAQ-kb-id"],
    chain=["评审-agent-id"],
    parallel_agents=["事实核查-agent-id"],
)
```

`kind="dataset"` 模式下三个参数被忽略，行为与之前一致（保持向后兼容）。
stdio 版 `a2a_mcp.py` 不需要改：每个 agent/KB 各自就是一个 MCP 工具，LLM 可自然串联调用。
