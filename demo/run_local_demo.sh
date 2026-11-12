#!/usr/bin/env bash
# 离线演示：不依赖 Docker，用本地 venv 启动示例公告站与采集 API。
# 用法: ./demo/run_local_demo.sh [查询时刻 ISO8601]
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$ROOT/.venv/bin/python"
QUERY_TIME="${1:-2026-11-11T10:00:00+08:00}"
SITE_URL="http://127.0.0.1:8080/index.html"

if [[ ! -x "$PY" ]]; then
  echo "缺少虚拟环境，请先创建: python3 -m venv --without-pip .venv && 引导 pip 后 pip install -r app/requirements.txt" >&2
  exit 1
fi

# 1) 生成示例静态公告站
"$PY" -I "$ROOT/site/build_site.py"

cleanup() {
  [[ -n "${SITE_PID:-}" ]] && kill "$SITE_PID" 2>/dev/null || true
  [[ -n "${API_PID:-}" ]] && kill "$API_PID" 2>/dev/null || true
}
trap cleanup EXIT

# 2) 启动示例公告站（8080）
( cd "$ROOT/site/html" && "$PY" -I -m http.server 8080 --bind 127.0.0.1 ) &
SITE_PID=$!

# 3) 启动采集 API（8000）
( cd "$ROOT/app" && "$PY" -I -m uvicorn app.main:app --host 127.0.0.1 --port 8000 ) &
API_PID=$!

for _ in $(seq 1 40); do
  curl -sf http://127.0.0.1:8000/health >/dev/null && curl -sf "$SITE_URL" >/dev/null && break
  sleep 0.5
done

echo "== 查询时刻: $QUERY_TIME =="
curl -s -X POST http://127.0.0.1:8000/query \
  -H 'Content-Type: application/json' \
  -d "{\"index_url\":\"$SITE_URL\",\"query_time\":\"$QUERY_TIME\"}" | "$PY" -I -m json.tool
