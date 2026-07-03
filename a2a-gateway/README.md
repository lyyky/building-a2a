# BuildingAI 内部 A2A Gateway 方案

## 目标

让 BuildingAI 的多个智能体通过 **Google A2A 协议**互相调用，
实现"主 agent 自动派单给专家 agent"的效果。

**不改 BuildingAI 一行代码**——纯外挂 Python 服务。

---

## 架构图

```
┌─────────────────────────────────────────┐
│  BuildingAI 容器 (不动)                  │
│  ┌─────────┐  ┌─────────┐  ┌─────────┐ │
│  │ 销售    │  │ 技术    │  │ 数据    │ │
│  │ 顾问    │  │ 工程师  │  │ 分析师  │ │
│  │ agent   │  │ agent   │  │ agent   │ │
│  └─────────┘  └─────────┘  └─────────┘ │
│              REST API (4090)            │
└────────────────────┬────────────────────┘
                     │ HTTP
                     ▼
┌─────────────────────────────────────────┐
│  A2A Gateway (新建, 独立 Python 进程)   │
│                                         │
│  ┌──────────────┐  ┌──────────────────┐ │
│  │ Agent        │  │ 30s 轮询 BuildingAI│ │
│  │ Registry     │◄─┤ 拉 agent 列表     │ │
│  └──────┬───────┘  └──────────────────┘ │
│         │                                │
│  ┌──────▼───────┐  ┌──────────────────┐ │
│  │ A2A Server   │  │ A2A Client       │ │
│  │ 暴露 agent   │  │ 调 BuildingAI    │ │
│  │ 为 A2A 端点  │  │ chat API         │ │
│  └──────┬───────┘  └────────┬─────────┘ │
│         │                   │            │
│  ┌──────▼───────────────────▼─────────┐ │
│  │  MCP Server (stdio)                │ │
│  │  把 A2A 能力暴露给 BuildingAI      │ │
│  └──────┬─────────────────────────────┘ │
└─────────┼───────────────────────────────┘
          │ stdio (MCP 协议)
          ▼
┌─────────────────────────────────────────┐
│  BuildingAI MCP 配置 (UI 添加)          │
│  → a2a-gateway                          │
└─────────────────────────────────────────┘
```

## 文件结构

```
/root/a2a-gateway/
├── README.md              # 本文档
├── requirements.txt       # Python 依赖
├── config.py              # 配置 (BuildingAI URL/Key)
├── registry.py            # 自动发现 BuildingAI agent
├── a2a_server.py          # A2A 协议服务端 (FastAPI)
├── a2a_client.py          # A2A 协议客户端
├── a2a_mcp.py             # MCP server (stdio)
├── main.py                # FastAPI 入口
├── start.sh               # 一键启动
└── test.sh                # 测试脚本
```

---

## 核心机制

### 1. 自动注册（无需手动配置）

```python
# registry.py 核心逻辑
class AgentRegistry:
    async def refresh(self):
        # 30 秒拉一次 BuildingAI
        r = await client.get(f"{BAI_BASE}/consoleapi/ai-agents",
                             headers={"Authorization": f"Bearer {BAI_KEY}"})
        for agent in data['items']:
            self.agents[agent['id']] = agent  # 自动入库

    def all(self):
        return list(self.agents.values())      # 动态返回当前所有

# 后台任务
async def poller():
    while True:
        await registry.refresh()
        await asyncio.sleep(30)                # 30 秒一轮
```

**结果**：你在 BuildingAI 后台新建/删除 agent，30 秒内 A2A Gateway 自动感知，
Agent Card 自动更新，MCP 工具自动出现/消失。

### 2. Agent Card 自动生成

```python
# a2a_server.py
@app.get("/.well-known/agent.json")
async def well_known_card():
    skills = []
    for agent in registry.all():
        # 从 agent 配置自动推断能力
        caps = ["chat"]
        if agent.get('datasetIds'):    caps.append("rag")
        if agent.get('mcpServerIds'):  caps.append("tools")

        skills.append({
            "id": agent['id'],
            "name": agent['name'],
            "description": agent.get('description', ''),
            "tags": caps,
        })
    return {"name": "BuildingAI Pool", "skills": skills, ...}
```

### 3. 动态 A2A 端点

```python
@app.post("/a2a/agents/{agent_id}")
async def a2a_endpoint(agent_id: str, request: Request):
    body = await request.json()
    method = body.get("method")  # "message/send" 或 "message/stream"

    agent = registry.get(agent_id)   # 动态查
    if not agent:
        return json_rpc_error(...)

    if method == "message/send":
        return await handle_send(agent, ...)   # 同步
    if method == "message/stream":
        return await handle_stream(agent, ...) # SSE 流式
```

### 4. MCP 工具自动出现

```python
# a2a_mcp.py
@app.list_tools()
async def list_tools():
    tools = [Tool(name="list_a2a_agents", ...)]  # 列出所有

    for agent in registry.all():                 # 动态生成
        tools.append(Tool(
            name=f"call_{slugify(agent['name'])}",
            description=f"调 agent: {agent['description']}",
            inputSchema={"type":"object","properties":{"query":{...}}}
        ))
    return tools
```

---

## 部署步骤

### Step 1：装依赖

```bash
cd /root/a2a-gateway
pip install -r requirements.txt
```

### Step 2：配置环境变量

```bash
export BAI_BASE="http://localhost:4090"          # BuildingAI 地址
export BAI_KEY="你的控制台 token"                 # admin 登录后拿
export GATEWAY_PORT="8000"                       # A2A Gateway 端口
```

### Step 3：启动 Gateway

```bash
bash start.sh
# 或: uvicorn main:app --host 0.0.0.0 --port 8000
```

### Step 4：注册到 BuildingAI

在 BuildingAI 后台：

```
AI 应用 → MCP → 添加 MCP
   名称: a2a-gateway
   类型: stdio
   命令: python /root/a2a-gateway/a2a_mcp.py
   → 保存
```

### Step 5：测试

```bash
# 看 agent 是否自动注册
curl http://localhost:8000/.well-known/agent.json | python3 -m json.tool

# A2A 协议调用
curl -X POST http://localhost:8000/a2a/agents/{AGENT_ID} \
  -H 'Content-Type: application/json' \
  -d '{
    "jsonrpc":"2.0",
    "id":1,
    "method":"message/send",
    "params":{"message":{"role":"user","parts":[{"kind":"text","text":"你好"}]}}
  }'
```

---

## 使用流程

### 主 agent 自动派单（用户感知不到）

```
用户: "iPhone 15 信号差怎么办？"
  ↓
主 agent (路由) 看到 MCP 工具:
  - call_sales_expert
  - call_tech_engineer
  - call_data_analyst
  ↓
LLM function calling → 自动调 call_tech_engineer
  ↓
tech_engineer agent 用知识库回答
  ↓
主 agent 整合后回复用户
```

### 外部 A2A client 调用（如果需要）

```python
from a2a_client import A2AClient
client = A2AClient("http://localhost:8000")
card = await client.discover()  # 拿 Agent Card
result = await client.send_message(
    agent_id="uuid-xxx",
    text="问题"
)
```

---

## 知识库（Dataset）的 A2A 支持

**一样的模式，自动注册**。

### 1. Registry 同时拉 KB

```python
# registry.py 扩展
class ResourceRegistry:
    async def refresh(self):
        # 拉 agents
        agents = await self._fetch(f"{BAI_BASE}/consoleapi/ai-agents")
        # 拉 datasets（同一个轮询周期）
        datasets = await self._fetch(f"{BAI_BASE}/consoleapi/ai-datasets")
        # 统一缓存
        self.resources = {
            "agents":   {a['id']: a    for a in agents},
            "datasets": {d['id']: d    for d in datasets},
        }
```

### 2. KB 也生成 Agent Card

每个 KB 被包成一个"只读 agent"——A2A 协议里"agent"是个通用概念，
不只是聊天，**能返回检索结果也算 agent**。

```python
def card_for_dataset(dataset: dict) -> dict:
    return {
        "name": f"KB: {dataset['name']}",
        "description": dataset.get('description', ''),
        "url": f"{GATEWAY_BASE}/a2a/datasets/{dataset['id']}",
        "capabilities": {
            "streaming": False,         # 检索不流式
            "pushNotifications": False,
        },
        "defaultInputModes": ["text"],
        "defaultOutputModes": ["text", "data"],
        "skills": [{
            "id": "retrieve",
            "name": "知识检索",
            "description": f"在 '{dataset['name']}' 知识库中检索相关内容",
            "tags": ["rag", "retrieval", "search"],
            "examples": [
                f"在{dataset['name']}里查找关于 X 的内容",
                f"问{dataset['name']}: Y",
            ]
        }]
    }

# 统一发现端点
@app.get("/.well-known/agent.json")
async def well_known_card():
    skills = []
    # 真正的 agent
    for a in registry.resources["agents"].values():
        skills.append(card_for_agent(a)['skills'][0])
    # KB 包成 agent
    for d in registry.resources["datasets"].values():
        skills.append(card_for_dataset(d)['skills'][0])
    return {"name": "BuildingAI Pool", "skills": skills, ...}
```

### 3. KB 的 A2A 端点

```python
# A2A 协议把"检索"当作一种 message/send
@app.post("/a2a/datasets/{dataset_id}")
async def dataset_a2a_endpoint(dataset_id: str, request: Request):
    body = await request.json()
    method = body.get("method")
    params = body.get("params", {})

    ds = registry.resources["datasets"].get(dataset_id)
    if not ds:
        return json_rpc_error(...)

    if method == "message/send":
        query = extract_text(params.get("message", {}))
        task_id = str(uuid.uuid4())

        # 调 BuildingAI 知识库检索 API
        async with httpx.AsyncClient() as client:
            r = await client.post(
                f"{BAI_BASE}/api/console/ai-datasets/{dataset_id}/retrieve",
                headers={"Authorization": f"Bearer {BAI_KEY}"},
                json={
                    "query": query,
                    "retrievalModel": {
                        "searchMethod": "hybrid_search",
                        "topK": 5,
                        "scoreThreshold": 0.5
                    }
                }
            )
            result = r.json()['data']

        # 拼成 A2A artifact
        segments = [{
            "kind": "data",
            "data": {
                "documentName": s['segment']['documentName'],
                "content":      s['segment']['content'],
                "score":        s['score'],
            }
        } for s in result.get('records', [])]

        return {
            "jsonrpc": "2.0", "id": body.get("id"),
            "result": {
                "kind": "task",
                "id": task_id,
                "status": {"state": "completed"},
                "artifacts": [{
                    "artifactId": str(uuid.uuid4()),
                    "name": "retrieval_results",
                    "parts": [
                        {"kind": "text", "text": f"找到 {len(segments)} 个相关片段"},
                        *segments
                    ]
                }]
            }
        }
```

### 4. MCP 工具自动生成（agent 也能用 KB）

```python
# a2a_mcp.py
@app.list_tools()
async def list_tools():
    tools = [Tool(name="list_resources", ...)]

    # 真正的 agent
    for agent in registry.resources["agents"].values():
        tools.append(Tool(
            name=f"call_agent_{slugify(agent['name'])}_{agent['id'][:8]}",
            description=f"调 agent: {agent['name']}",
            inputSchema={"type":"object","properties":{"query":{...}}}
        ))

    # KB 包成 search tool
    for ds in registry.resources["datasets"].values():
        tools.append(Tool(
            name=f"search_kb_{slugify(ds['name'])}_{ds['id'][:8]}",
            description=f"在知识库 '{ds['name']}' 检索: {ds.get('description','')}",
            inputSchema={"type":"object","properties":{
                "query": {"type":"string", "description":"检索关键词"}
            },"required":["query"]}
        ))
    return tools

@app.call_tool()
async def call_tool(name, args):
    if name.startswith("call_agent_"):
        # 调 A2A 端点
        return await invoke_agent_a2a(name, args)
    elif name.startswith("search_kb_"):
        # 调 KB 检索端点
        return await invoke_kb_search(name, args)
```

### 5. 使用场景

```
主 agent (路由) 看到 MCP 工具:
  - call_agent_sales_expert
  - call_agent_tech_engineer
  - search_kb_product_manual
  - search_kb_faq

用户: "查 iPhone 15 的保修条款"
  ↓
主 agent LLM function calling:
  → 先调 search_kb_product_manual("iPhone 15 保修")
  → 拿到相关片段
  → 整合后回复
  ↓
无需经过子 agent, 直接查 KB 即可
```

### 6. 资源类型汇总

| A2A 视角 | BuildingAI 资源 | 端点 | MCP 工具前缀 |
|---|---|---|---|
| Agent (chat) | `ai-agent` | `/a2a/agents/{id}` | `call_agent_*` |
| Agent (retrieve) | `datasets` | `/a2a/datasets/{id}` | `search_kb_*` |
| 列出所有 | — | `/.well-known/agent.json` | `list_resources` |

### 7. 配置文件扩展

```python
# config.py 新增
DATASET_POLL_TTL = 30       # KB 也 30 秒轮询一次
DATASET_TOP_K = 5           # 默认检索返回几个片段
DATASET_SCORE_THRESHOLD = 0.5
```

### 8. BuildingAI 端需要的 API 权限

```
GET    /consoleapi/ai-agents           # 列 agent
GET    /consoleapi/ai-agents/{id}      # 详情
POST   /api/ai-agents/{id}/chat/blocking       # agent 聊天
POST   /api/ai-agents/{id}/chat/stream        # agent 流式
GET    /consoleapi/ai-datasets         # 列 KB
GET    /consoleapi/ai-datasets/{id}    # KB 详情
POST   /api/console/ai-datasets/{id}/retrieve  # KB 检索
```

全部是 BuildingAI 自带 API，**零代码修改**。

---

## 与"硬编码方案"的对比

| 方案 | 改 BuildingAI？ | 维护成本 | 升级 BuildingAI |
|---|---|---|---|
| **A2A Gateway（这个）** | ❌ 零 | 中（要维护 Python 服务） | ✅ 无影响 |
| 改 BuildingAI 源码加 router | ✅ 改 | 高（要合并代码） | ⚠️ 跟版本走 |
| Dify 套娃 | ❌ 零 | 低 | ✅ 但要装 Dify |

---

## 关键 API 速查

| 端点 | 用途 |
|---|---|
| `GET  /.well-known/agent.json` | A2A 标准发现端点（agent + KB） |
| `GET  /a2a/agents/{id}/card` | 单个 agent 的 Card |
| `GET  /a2a/datasets/{id}/card` | 单个 KB 的 Card |
| `POST /a2a/agents/{id}` | A2A JSON-RPC 2.0 端点（chat） |
| `POST /a2a/datasets/{id}` | A2A JSON-RPC 2.0 端点（retrieve） |
| `GET  /health` | 健康检查 |
| `GET  /agents` | 看 registry 当前缓存的 agent 列表 |
| `GET  /datasets` | 看 registry 当前缓存的 KB 列表 |

## A2A 协议关键点（备忘）

- **协议**：JSON-RPC 2.0
- **方法**：
  - `message/send`（同步，返回完整结果）
  - `message/stream`（SSE 流式）
  - `tasks/get`、`tasks/cancel`（任务管理）
- **Task 状态**：`submitted` → `working` → `completed` / `failed` / `canceled`
- **Part 类型**：`text`、`file`、`data`
- **Artifact**：任务的输出结果

---

## 多用户鉴权（API key + Fernet 加密）

默认 `BAI_USERNAME/BAI_PASSWORD` 是单用户模式（gateway 用一个固定 BuildingAI 账号去调所有 agent）。
要支持**多个外部调用方各自用独立 BuildingAI 账号**（隔离会话、agent 列表、配额），开启多用户模式。

### 1. 生成 MASTER_KEY（一次性）

```bash
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# 输出形如：b'k7jH...='  （base64 字符串）
```

写进 `.env`：
```bash
A2A_GATEWAY_MASTER_KEY=b'k7jH...='
```

### 2. 给每个调用方配一套凭证

**a) 在 BuildingAI 后台"用户管理"创建普通用户**（不能用 /install 那个 root）
**b) 加密这个用户的密码**：

容器内执行（拿到密文）：
```bash
docker exec -it buildingai-nodejs python3 -c "from a2a.config import encrypt_password; print(encrypt_password('该用户的明文密码'))"
# 输出形如：gAAAAABl...=
```

**c) 在 `.env` 加进 `A2A_GATEWAY_USERS`**：

```bash
A2A_GATEWAY_USERS=sk-alice:alice_bai:gAAAAABl...,sk-bob:bob_bai:gAAAAABl...
# 格式：api_key:BuildingAI用户名:enc_password，逗号分隔多个
```

**d) 重启容器**让配置生效：
```bash
docker compose restart nodejs
```

### 3. 客户端调用方式

```bash
curl -X POST http://gateway:8000/a2a/agents/<agent_id> \
  -H "Authorization: Bearer sk-alice" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"message/send","params":{...}}'
```

- 漏 `Authorization` → 401
- `Authorization` 不对 → 401
- `MASTER_KEY` 配错 → decrypt 失败 → 401
- 任意一个验证通过 → 用对应 BuildingAI 账号去调 agent

### 4. 隔离行为

- **会话隔离**：每个调用方有自己的 `conversationId` 命名空间，互不可见
- **agent 列表**：从各自 BuildingAI 账号下的 agent 列表里取（要确保 BuildingAI 上 agent 配对用户可见）
- **会话历史**：存到各自 BuildingAI 账号下，互不干扰
- **配额 / 用量**：BuildingAI 端按用户账号统计

### 5. 管理端点

```bash
# 查看多用户池状态（已配置 / 已登录）
curl http://gateway:8000/api/admin/users
```

返回：
```json
{
  "total_configured": 2,
  "active_logged_in": 2,
  "items": [
    {"api_key_prefix": "sk-alice...", "username": "alice_bai", "logged_in": true},
    {"api_key_prefix": "sk-bob...",   "username": "bob_bai",   "logged_in": true}
  ]
}
```

### 6. 单/多用户兼容

- `A2A_GATEWAY_USERS` **留空** → 走单用户模式（用 `BAI_USERNAME/BAI_PASSWORD`），所有端点直接放行，不验 key
- `A2A_GATEWAY_USERS` **有值** → 走多用户模式，所有 `/a2a/*` 端点要求 `Authorization: Bearer <key>`
- `MASTER_KEY` 配错 → 多用户鉴权整体不可用（`/api/admin/users` 会显示 `logged_in: false`）

---

## 状态

- [x] 方案设计
- [x] 代码实现（v0.2.0：单用户 + 同容器集成 + 多用户鉴权）
- [ ] 部署测试

BuildingAI 内核: langgenius/dify-api:1.15.0 + ccr.ccs.tencentyun.com/buildingai/node:22.20.0
BuildingAI 端口: 4090
A2A Gateway 计划端口: 8000
