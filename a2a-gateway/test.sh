#!/bin/bash
# A2A Gateway 冒烟测试。
# 前置：start.sh 已经跑起来。

set -euo pipefail

HOST="${HOST:-localhost}"
PORT="${PORT:-${GATEWAY_PORT:-8000}}"
BASE="http://${HOST}:${PORT}"

green() { printf "\033[32m%s\033[0m\n" "$1"; }
red()   { printf "\033[31m%s\033[0m\n" "$1"; }
head()  { printf "\n\033[1m== %s ==\033[0m\n" "$1"; }

head "1. /health"
out=$(curl -fsS "${BASE}/health" || true)
echo "${out}"
echo "${out}" | python3 -c 'import sys,json; d=json.load(sys.stdin); assert d["ok"]==True; print("  ok ✓")'

head "2. /.well-known/agent.json  是否返回有效结构"
out=$(curl -fsS "${BASE}/.well-known/agent.json")
echo "${out}" | python3 -c '
import sys, json
d = json.load(sys.stdin)
assert d["name"], "missing name"
skills = d.get("skills") or []
print(f"  skills={len(skills)}")
for s in skills[:10]:
    print("   -", s.get("id"), s.get("name"))
'

head "3. /agents 列表（看 BuildingAI 是否已装好 agent）"
curl -fsS "${BASE}/agents" || true
echo

head "4. /datasets 列表"
curl -fsS "${BASE}/datasets" || true
echo

head "5. message/send 端点（找一个真实 agent_id 测）"
agent_id=$(curl -fsS "${BASE}/agents" | python3 -c '
import sys, json
d = json.load(sys.stdin)
items = d.get("items") or []
print(items[0]["id"] if items else "")
' 2>/dev/null || true)

if [[ -n "${agent_id}" ]]; then
  curl -fsS -X POST "${BASE}/a2a/agents/${agent_id}" \
    -H 'Content-Type: application/json' \
    -d '{
      "jsonrpc":"2.0","id":1,"method":"message/send",
      "params":{"message":{"role":"user","parts":[{"kind":"text","text":"hi"}]}}
    }' || true
  echo
else
  red "  当前 registry 无可用 agent。请先在 BuildingAI 后台创建至少一个 agent 后再测。"
fi

head "Done"
