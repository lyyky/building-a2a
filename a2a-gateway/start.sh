#!/bin/bash
# 一键启动 A2A Gateway。
# 启动前需要：已装 BuildingAI（http://localhost:4090/install 可访问），
# 且在后台初始化过管理员账号，再把账号密码 export 到环境变量。

set -euo pipefail

cd "$(dirname "$0")"

: "${BAI_BASE:=http://localhost:4090}"
: "${GATEWAY_PORT:=8000}"
export BAI_BASE GATEWAY_PORT

if [[ -z "${BAI_USERNAME:-}" || -z "${BAI_PASSWORD:-}" ]]; then
  echo "错误：缺少 BAI_USERNAME / BAI_PASSWORD 环境变量。"
  echo "请先："
  echo "  export BAI_USERNAME=your-admin"
  echo "  export BAI_PASSWORD=your-password"
  exit 1
fi

# 依赖
if ! python3 -c "import fastapi, uvicorn, httpx, sse_starlette, mcp" 2>/dev/null; then
  echo "安装依赖..."
  pip install -q -r requirements.txt
fi

echo "启动 A2A Gateway on http://localhost:${GATEWAY_PORT}"
echo "BuildingAI 后台: ${BAI_BASE}"
exec python3 main.py
