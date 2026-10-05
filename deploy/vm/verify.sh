#!/bin/bash
# =============================================================================
#  部署后自检脚本 —— 逐项验证 Dify 内网部署是否真的可用
#  用法：bash verify.sh
# =============================================================================
set -uo pipefail
BUNDLE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG_FILE="${BUNDLE_DIR}/config/dify.env"
[ -f "${CONFIG_FILE}" ] && { set -a; . "${CONFIG_FILE}"; set +a; }

DIFY_HOST="${DIFY_HOST:-127.0.0.1}"
DIFY_HTTP_PORT="${DIFY_HTTP_PORT:-80}"
DIFY_ROOT="${DIFY_ROOT:-/opt/dify}"
DIFY_DATA_ROOT="${DIFY_DATA_ROOT:-/data/dify}"
PG="${DIFY_ROOT}/pgsql/bin"
REDIS="${DIFY_ROOT}/redis/bin"

PASS=0; FAIL=0; WARN=0
ok()   { echo -e "  \033[32m[ OK ]\033[0m $*"; PASS=$((PASS+1)); }
bad()  { echo -e "  \033[31m[FAIL]\033[0m $*"; FAIL=$((FAIL+1)); }
warn() { echo -e "  \033[33m[WARN]\033[0m $*"; WARN=$((WARN+1)); }
hdr()  { echo; echo "── $* ──"; }

hdr "1. 系统环境"
GLIBC="$(ldd --version 2>/dev/null | head -1 | grep -oE '[0-9]+\.[0-9]+$')"
[ -n "${GLIBC}" ] && ok "glibc ${GLIBC}" || warn "无法识别 glibc 版本"
[ "$(uname -m)" = "x86_64" ] && ok "架构 x86_64" || bad "架构非 x86_64"
FREE_GB="$(df -BG --output=avail "${DIFY_DATA_ROOT}" 2>/dev/null | tail -1 | tr -dc '0-9')"
[ "${FREE_GB:-0}" -ge 10 ] && ok "数据盘剩余 ${FREE_GB}G" || warn "数据盘剩余仅 ${FREE_GB:-?}G"
MEM_GB="$(free -g 2>/dev/null | awk '/^Mem:/{print $2}')"
ok "内存 ${MEM_GB:-?}G"

hdr "2. systemd 服务状态"
for svc in postgresql-dify redis-dify dify-api dify-worker dify-worker-beat dify-web dify-plugin-daemon dify-sandbox; do
  if systemctl is-active --quiet "${svc}" 2>/dev/null; then ok "${svc} 运行中"
  else bad "${svc} 未运行（journalctl -u ${svc} -n 30）"; fi
done

hdr "3. 端口监听"
for p in 5001 3000 5002 8194 5432 6379; do
  if ss -lnt 2>/dev/null | awk '{print $4}' | grep -qE "[:.]${p}$"; then ok "端口 ${p} 已监听"
  else bad "端口 ${p} 未监听"; fi
done

hdr "4. 数据库"
if sudo -u dify "${PG}/psql" -h 127.0.0.1 -U dify -d dify -tAc "SELECT 1" >/dev/null 2>&1; then
  ok "主库 dify 可连接"
  TBLS="$(sudo -u dify "${PG}/psql" -h 127.0.0.1 -U dify -d dify -tAc "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'" 2>/dev/null)"
  [ "${TBLS:-0}" -gt 20 ] && ok "主库已建表（${TBLS} 张）" || bad "主库表数量异常（${TBLS}），迁移可能未完成"
  EXT="$(sudo -u dify "${PG}/psql" -h 127.0.0.1 -U dify -d dify -tAc "SELECT extname FROM pg_extension WHERE extname='vector'" 2>/dev/null)"
  [ "${EXT}" = "vector" ] && ok "pgvector 扩展已启用" || bad "pgvector 扩展缺失"
else
  bad "主库 dify 无法连接"
fi
sudo -u dify "${PG}/psql" -h 127.0.0.1 -U dify -d dify_plugin -tAc "SELECT 1" >/dev/null 2>&1 \
  && ok "插件库 dify_plugin 可连接" || bad "插件库 dify_plugin 无法连接"

hdr "5. Redis"
if "${REDIS}/redis-cli" -h 127.0.0.1 -a "${REDIS_PASSWORD:-}" ping 2>/dev/null | grep -q PONG; then
  ok "Redis 响应 PONG"
else
  bad "Redis 无响应"
fi

hdr "6. API 接口"
if curl -sf http://127.0.0.1:5001/health >/dev/null 2>&1; then ok "后端 /health 正常"
else bad "后端 /health 无响应"; fi
if curl -sf "http://127.0.0.1:${DIFY_HTTP_PORT}/console/api/setup" >/dev/null 2>&1; then ok "控制台 API 经 Nginx 可达"
elif curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:${DIFY_HTTP_PORT}/console/api/setup" 2>/dev/null | grep -qE '200|401|403'; then
  ok "控制台 API 经 Nginx 可达（返回鉴权码属正常）"
else
  bad "控制台 API 经 Nginx 不可达"
fi
curl -sf "http://127.0.0.1:${DIFY_HTTP_PORT}/" >/dev/null 2>&1 && ok "前端页面可访问" || bad "前端页面不可访问"
curl -sf http://127.0.0.1:5002/health/check >/dev/null 2>&1 && ok "插件守护进程健康" || bad "插件守护进程无响应"

hdr "7. 代码沙箱（关键：评分/标注工作流的代码节点依赖它）"
if curl -sf http://127.0.0.1:8194/health >/dev/null 2>&1; then ok "沙箱 HTTP 健康检查通过"
else
  # 沙箱可能没有 /health，用一次真实执行来验证
  RESP="$(curl -s -X POST http://127.0.0.1:8194/v1/sandbox/run \
    -H 'X-Api-Key: dify-sandbox' -H 'Content-Type: application/json' \
    -d '{"language":"python3","code":"print(1+1)","preload":"","enable_network":false}' 2>/dev/null)"
  echo "${RESP}" | grep -q '2' && ok "沙箱实际执行代码成功（返回 1+1=2）" || bad "沙箱不可用: ${RESP:0:200}"
fi

hdr "8. 磁盘与日志"
du -sh "${DIFY_ROOT}" 2>/dev/null | awk '{print "  安装目录占用: "$1}'
du -sh "${DIFY_DATA_ROOT}" 2>/dev/null | awk '{print "  数据目录占用: "$1}'
LOGERR="$(grep -ril 'error\|traceback' "${DIFY_DATA_ROOT}/logs" 2>/dev/null | head -5)"
[ -z "${LOGERR}" ] && ok "日志中未发现明显错误" || warn "以下日志含 error 关键字：${LOGERR}"

echo
echo "════════════════════════════════════════"
echo -e "  通过 \033[32m${PASS}\033[0m 项 ｜ 告警 \033[33m${WARN}\033[0m 项 ｜ 失败 \033[31m${FAIL}\033[0m 项"
echo "════════════════════════════════════════"
[ "${FAIL}" -eq 0 ] && { echo -e "\033[32m全部关键项通过，部署成功。\033[0m"; exit 0; }
echo -e "\033[31m存在失败项，请按 04-验收与排障.md 排查。\033[0m"; exit 1
