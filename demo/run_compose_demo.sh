#!/usr/bin/env bash
# Docker Compose 离线演示：构建并启动 API + 本地示例公告站。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
QUERY_TIME="${1:-2026-11-11T10:00:00+08:00}"

# 示例站镜像直接打包静态 HTML，先确保已生成
python3 "$ROOT/site/build_site.py"

cd "$ROOT"
docker compose up -d --build

for _ in $(seq 1 40); do
  curl -sf http://127.0.0.1:8000/health >/dev/null && break
  sleep 1
done

curl -s -X POST http://127.0.0.1:8000/query \
  -H 'Content-Type: application/json' \
  -d '{"index_url":"http://notice-site:8080/index.html","query_time":"'"$QUERY_TIME"'"}'

echo
echo "演示完成，停止服务: docker compose down"
