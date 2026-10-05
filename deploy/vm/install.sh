#!/bin/bash
# =============================================================================
#  Dify 内网离线部署安装脚本（不使用 Docker）
#  用法：sudo bash install.sh [--skip-build] [--only <step>]
#  前提：已填写 config/dify.env
#  适用：RHEL 8 / Rocky 8 / openEuler 20.03+ / 麒麟 V10（glibc >= 2.28），x86_64
# =============================================================================
set -euo pipefail

BUNDLE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_FILE="${BUNDLE_DIR}/config/dify.env"
SKIP_BUILD=0

while [ $# -gt 0 ]; do
  case "$1" in
    --skip-build) SKIP_BUILD=1; shift ;;
    *) echo "未知参数: $1"; exit 2 ;;
  esac
done

# ---------------------------------------------------------------- 输出工具
RED=$'\033[31m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; BLUE=$'\033[36m'; NC=$'\033[0m'
STEP_NO=0
log()  { echo -e "${BLUE}[$(date +%H:%M:%S)]${NC} $*"; }
ok()   { echo -e "  ${GREEN}[ OK ]${NC} $*"; }
warn() { echo -e "  ${YELLOW}[WARN]${NC} $*"; }
die()  { echo -e "  ${RED}[FAIL]${NC} $*" >&2; exit 1; }
step() { STEP_NO=$((STEP_NO+1)); echo; echo -e "${GREEN}═══ 步骤 ${STEP_NO}: $* ═══${NC}"; }

[ "$(id -u)" -eq 0 ] || die "请用 root 运行：sudo bash install.sh"
[ -f "${CONFIG_FILE}" ] || die "找不到配置文件 ${CONFIG_FILE}，请先复制 config/dify.env.template 为 config/dify.env 并填写"

# ---------------------------------------------------------------- 读取配置
log "读取配置 ${CONFIG_FILE}"
set -a
# shellcheck disable=SC1090
source "${CONFIG_FILE}"
set +a

DIFY_ROOT="${DIFY_ROOT:-/opt/dify}"
DIFY_DATA_ROOT="${DIFY_DATA_ROOT:-/data/dify}"
DIFY_HOST="${DIFY_HOST:-127.0.0.1}"
DIFY_HTTP_PORT="${DIFY_HTTP_PORT:-80}"
LOG_LEVEL="${LOG_LEVEL:-INFO}"

# 派生变量（用户不用填）
CONFIG_DIR="${DIFY_ROOT}/config"
PY_DIR="${DIFY_ROOT}/python"
PG_PREFIX="${DIFY_ROOT}/pgsql"
REDIS_PREFIX="${DIFY_ROOT}/redis"
SANDBOX_DIR="${DIFY_ROOT}/sandbox"
DAEMON_DIR="${DIFY_ROOT}/plugin-daemon"
WEB_DIR="${DIFY_ROOT}/web"
API_DIR="${DIFY_ROOT}/api"
LOG_DIR="${DIFY_DATA_ROOT}/logs"

export CONSOLE_API_URL="http://${DIFY_HOST}:${DIFY_HTTP_PORT}"
export CONSOLE_WEB_URL="http://${DIFY_HOST}:${DIFY_HTTP_PORT}"
export SERVICE_API_URL="http://${DIFY_HOST}:${DIFY_HTTP_PORT}"
export APP_API_URL="http://${DIFY_HOST}:${DIFY_HTTP_PORT}"
export APP_WEB_URL="http://${DIFY_HOST}:${DIFY_HTTP_PORT}"
export FILES_URL="http://${DIFY_HOST}:${DIFY_HTTP_PORT}"
export NEXT_PUBLIC_SOCKET_URL="ws://${DIFY_HOST}:${DIFY_HTTP_PORT}"

# =============================================================================
step "环境检查"
# =============================================================================
ARCH="$(uname -m)"
case "${ARCH}" in
  x86_64|aarch64) : ;;
  *) die "不支持的系统架构：${ARCH}（本方案支持 x86_64 与 aarch64/飞腾鲲鹏）" ;;
esac
if [ -f "${BUNDLE_DIR}/ARCH.txt" ]; then
  WANT="$(tr -d '\r\n ' < "${BUNDLE_DIR}/ARCH.txt")"
  case "${WANT}" in
    arm64|aarch64) [ "${ARCH}" = "aarch64" ] || die "本离线包是 ARM64 版本，当前机器是 ${ARCH}，请改用 x86_64 版本" ;;
    amd64|x86_64)  [ "${ARCH}" = "x86_64" ]  || die "本离线包是 x86_64 版本，当前机器是 ${ARCH}，请改用 ARM64 版本" ;;
  esac
fi
ok "系统架构 ${ARCH} 与离线包匹配"

GLIBC_VER="$(ldd --version 2>/dev/null | head -1 | grep -oE '[0-9]+\.[0-9]+$' || echo 0)"
GLIBC_MAJ="${GLIBC_VER%%.*}"; GLIBC_MIN="${GLIBC_VER##*.}"
if [ "${GLIBC_MAJ}" -lt 2 ] || { [ "${GLIBC_MAJ}" -eq 2 ] && [ "${GLIBC_MIN:-0}" -lt 28 ]; }; then
  die "glibc ${GLIBC_VER} 过低：Dify 1.17.1 的依赖需要 glibc >= 2.28（CentOS 7 不支持，请改用 RHEL8/openEuler）"
fi
ok "glibc ${GLIBC_VER} 满足要求"

for c in tar gzip make sed awk grep; do
  command -v "$c" >/dev/null 2>&1 || die "缺少必需命令: $c（请从系统安装介质补齐）"
done
ok "基础命令齐全"

command -v cc >/dev/null 2>&1 || command -v gcc >/dev/null 2>&1 || {
  [ -x "${BUNDLE_DIR}/toolchain/zig" ] || [ -x "${BUNDLE_DIR}/toolchain/zig/zig" ] || die "系统无 gcc，且离线包中未找到 zig 编译器"
  warn "系统无 gcc，将使用离线包自带的 zig 作为 C 编译器"
}

# 端口占用检查（避免装完起不来）
for p in "${DIFY_HTTP_PORT}" 5001 3000 5002 8194; do
  if command -v ss >/dev/null 2>&1 && ss -lnt 2>/dev/null | awk '{print $4}' | grep -qE "[:.]${p}$"; then
    warn "端口 ${p} 已被占用，请确认不是冲突服务"
  fi
done
ok "环境检查通过"

# =============================================================================
step "生成密钥与创建目录"
# =============================================================================
gen_secret() { openssl rand -base64 42 2>/dev/null | tr -d '/+=' | head -c 42 || head -c 42 /dev/urandom | base64 | tr -d '/+=' ; }
[ -n "${SECRET_KEY:-}" ] || SECRET_KEY="$(gen_secret)"
[ -n "${DB_PASSWORD:-}" ] || DB_PASSWORD="$(gen_secret | head -c 24)"
[ -n "${REDIS_PASSWORD:-}" ] || REDIS_PASSWORD="$(gen_secret | head -c 24)"

id dify >/dev/null 2>&1 || useradd -r -m -d "${DIFY_ROOT}" -s /sbin/nologin dify
ok "系统用户 dify 就绪"

mkdir -p "${DIFY_ROOT}" "${CONFIG_DIR}" "${DIFY_DATA_ROOT}"/{postgres,redis,storage,plugin-storage,logs}
mkdir -p "${DIFY_DATA_ROOT}/plugin-storage"/{plugin,plugin_packages,cwd,assets}
chown -R dify:dify "${DIFY_ROOT}" "${DIFY_DATA_ROOT}"
ok "目录结构创建完成：${DIFY_ROOT} / ${DIFY_DATA_ROOT}"

# =============================================================================
step "部署便携 Python 运行时与依赖树"
# =============================================================================
if [ ! -x "${PY_DIR}/bin/python3" ]; then
  mkdir -p "${PY_DIR}"
  tar -xzf "${BUNDLE_DIR}/packs/02-python-runtime.tar.gz" -C "${PY_DIR}" --strip-components=1
fi
"${PY_DIR}/bin/python3" -V >/dev/null || die "便携 Python 无法运行"
ok "Python 就绪：$("${PY_DIR}/bin/python3" -V 2>&1)"

SITE="$("${PY_DIR}/bin/python3" -c 'import sysconfig;print(sysconfig.get_paths()["purelib"])')"
if [ ! -d "${SITE}/flask" ]; then
  tar -xzf "${BUNDLE_DIR}/packs/03-api-deps.tar.gz" -C "${SITE}"
fi
"${PY_DIR}/bin/python3" -c "import flask, celery, gunicorn, sqlalchemy, psycopg2" \
  || die "Python 依赖树不完整"
ok "Dify API 依赖树就绪（$(ls "${SITE}" | wc -l) 个包）"
chown -R dify:dify "${PY_DIR}"

# =============================================================================
step "编译安装 PostgreSQL 与 pgvector"
# =============================================================================
if [ "${PG_BUILD_FROM_SOURCE:-yes}" = "yes" ] && [ ! -x "${PG_PREFIX}/bin/postgres" ]; then
  CC_BIN="${CC:-}"
  [ -n "${CC_BIN}" ] || { command -v gcc >/dev/null 2>&1 && CC_BIN=gcc || CC_BIN="${BUNDLE_DIR}/toolchain/zig cc"; }
  WORK="${DIFY_ROOT}/build"; mkdir -p "${WORK}"
  log "解包 PostgreSQL 源码（首次编译约 5-10 分钟）"
  tar -xzf "${BUNDLE_DIR}/packs/05-databases.tar.gz" -C "${WORK}" --strip-components=1
  PG_SRC="$(find "${WORK}" -maxdepth 1 -type d -name 'postgresql-*' | head -1)"
  [ -n "${PG_SRC}" ] || die "未找到 PostgreSQL 源码目录"
  ( cd "${PG_SRC}" && CC="${CC_BIN}" ./configure --prefix="${PG_PREFIX}" \
      --without-readline --without-zlib --without-icu --disable-debug >"${LOG_DIR}/pg-configure.log" 2>&1 ) \
    || die "PostgreSQL configure 失败，详见 ${LOG_DIR}/pg-configure.log"
  ( cd "${PG_SRC}" && make -j"$(nproc)" >"${LOG_DIR}/pg-make.log" 2>&1 ) \
    || die "PostgreSQL 编译失败，详见 ${LOG_DIR}/pg-make.log"
  ( cd "${PG_SRC}" && make install >"${LOG_DIR}/pg-install.log" 2>&1 ) || die "PostgreSQL 安装失败"

  log "编译 pgvector 扩展"
  PGV_SRC="$(find "${WORK}" -maxdepth 1 -type d -name 'pgvector-*' | head -1)"
  [ -n "${PGV_SRC}" ] || die "未找到 pgvector 源码目录"
  ( cd "${PGV_SRC}" && make PG_CONFIG="${PG_PREFIX}/bin/pg_config" >"${LOG_DIR}/pgvector.log" 2>&1 \
    && make install PG_CONFIG="${PG_PREFIX}/bin/pg_config" >>"${LOG_DIR}/pgvector.log" 2>&1 ) \
    || die "pgvector 编译失败，详见 ${LOG_DIR}/pgvector.log"
  ok "PostgreSQL 与 pgvector 编译安装完成"
else
  ok "PostgreSQL 已存在，跳过编译"
fi

# 初始化数据目录
if [ ! -s "${DIFY_DATA_ROOT}/postgres/PG_VERSION" ]; then
  su -s /bin/bash dify -c "${PG_PREFIX}/bin/initdb -D ${DIFY_DATA_ROOT}/postgres -E UTF8 --locale=C --auth-local=trust --auth-host=md5" \
    >"${LOG_DIR}/initdb.log" 2>&1 || die "initdb 失败，详见 ${LOG_DIR}/initdb.log"
  ok "数据库集群初始化完成"
fi

# =============================================================================
step "编译安装 Redis"
# =============================================================================
if [ ! -x "${REDIS_PREFIX}/bin/redis-server" ]; then
  WORK="${DIFY_ROOT}/build"
  REDIS_SRC="$(find "${WORK}" -maxdepth 1 -type d -name 'redis-*' | head -1)"
  [ -n "${REDIS_SRC}" ] || die "未找到 Redis 源码目录"
  ( cd "${REDIS_SRC}" && make -j"$(nproc)" MALLOC=libc BUILD_TLS=no >"${LOG_DIR}/redis-make.log" 2>&1 ) \
    || die "Redis 编译失败，详见 ${LOG_DIR}/redis-make.log"
  mkdir -p "${REDIS_PREFIX}/bin"
  cp "${REDIS_SRC}/src/redis-server" "${REDIS_SRC}/src/redis-cli" "${REDIS_PREFIX}/bin/"
  ok "Redis 编译完成"
else
  ok "Redis 已存在，跳过编译"
fi

# =============================================================================
step "写入配置文件"
# =============================================================================
# --- Dify 主配置 ---
cat > "${CONFIG_DIR}/dify.env" <<ENVEOF
# 由 install.sh 自动生成，修改后需重启服务
SECRET_KEY=${SECRET_KEY}
INIT_PASSWORD=${INIT_PASSWORD}
DEPLOY_ENV=PRODUCTION
MIGRATION_ENABLED=true
CONSOLE_API_URL=${CONSOLE_API_URL}
CONSOLE_WEB_URL=${CONSOLE_WEB_URL}
SERVICE_API_URL=${SERVICE_API_URL}
APP_API_URL=${APP_API_URL}
APP_WEB_URL=${APP_WEB_URL}
FILES_URL=${FILES_URL}
NEXT_PUBLIC_SOCKET_URL=${NEXT_PUBLIC_SOCKET_URL}
CONSOLE_CORS_ALLOW_ORIGINS=*
DB_TYPE=postgresql
DB_HOST=127.0.0.1
DB_PORT=5432
DB_USERNAME=dify
DB_PASSWORD=${DB_PASSWORD}
DB_DATABASE=dify
SQLALCHEMY_POOL_SIZE=30
SQLALCHEMY_POOL_RECYCLE=3600
SQLALCHEMY_ECHO=false
DB_PLUGIN_DATABASE=dify_plugin
REDIS_HOST=127.0.0.1
REDIS_PORT=6379
REDIS_USERNAME=
REDIS_PASSWORD=${REDIS_PASSWORD}
REDIS_USE_SSL=false
REDIS_DB=0
CELERY_BROKER_URL=redis://:${REDIS_PASSWORD}@127.0.0.1:6379/1
CELERY_BACKEND=redis
BROKER_USE_SSL=false
STORAGE_TYPE=opendal
OPENDAL_SCHEME=fs
OPENDAL_FS_ROOT=${DIFY_DATA_ROOT}/storage
VECTOR_STORE=pgvector
PGVECTOR_HOST=127.0.0.1
PGVECTOR_PORT=5432
PGVECTOR_USER=dify
PGVECTOR_PASSWORD=${DB_PASSWORD}
PGVECTOR_DATABASE=dify
PGVECTOR_MIN_CONNECTION=${PGVECTOR_MIN_CONNECTION:-2}
PGVECTOR_MAX_CONNECTION=${PGVECTOR_MAX_CONNECTION:-20}
CODE_EXECUTION_ENDPOINT=http://127.0.0.1:8194
CODE_EXECUTION_API_KEY=dify-sandbox
SANDBOX_ENABLE_NETWORK=false
PLUGIN_DAEMON_URL=http://127.0.0.1:5002
PLUGIN_DAEMON_KEY=${PLUGIN_DAEMON_KEY}
PLUGIN_DIFY_INNER_API_KEY=${PLUGIN_DIFY_INNER_API_KEY}
PLUGIN_DIFY_INNER_API_URL=http://127.0.0.1:5001
INNER_API_KEY_FOR_PLUGIN=${PLUGIN_DIFY_INNER_API_KEY}
MARKETPLACE_ENABLED=false
MARKETPLACE_API_URL=
MARKETPLACE_URL=
FORCE_VERIFYING_SIGNATURE=false
AGENT_BACKEND_USE_FAKE=true
DIFY_BIND_ADDRESS=127.0.0.1
DIFY_PORT=5001
SERVER_WORKER_AMOUNT=${GUNICORN_WORKERS:-4}
SERVER_WORKER_CONNECTIONS=1000
GUNICORN_TIMEOUT=360
CELERY_WORKER_CLASS=${CELERY_WORKER_CLASS:-gevent}
CELERY_WORKER_AMOUNT=${CELERY_WORKER_AMOUNT:-4}
CELERY_PREFETCH_MULTIPLIER=1
MAX_TASKS_PER_CHILD=50
CELERY_QUEUES=api_token,dataset,dataset_summary,priority_dataset,priority_pipeline,pipeline,mail,ops_trace,app_deletion,app_rbac,plugin,workflow_storage,conversation,workflow,schedule_poller,schedule_executor,triggered_workflow_dispatcher,trigger_refresh_publisher,trigger_refresh_executor,retention,workflow_based_app_execution
LOG_LEVEL=${LOG_LEVEL}
MIGRATION_ENABLED=true
TEXT_GENERATION_TIMEOUT_MS=60000
WORKFLOW_GENERATION_TIMEOUT_MS=180000
ENABLE_AGENT_V2=false
NEXT_PUBLIC_ENABLE_AGENT_V2=false
NEXT_PUBLIC_ALLOW_EMBED=false
ENVEOF
chmod 640 "${CONFIG_DIR}/dify.env"
chown dify:dify "${CONFIG_DIR}/dify.env"
ok "Dify 主配置写入 ${CONFIG_DIR}/dify.env"

# --- Redis 配置 ---
cat > "${CONFIG_DIR}/redis.conf" <<ENVEOF
bind 127.0.0.1
port 6379
requirepass ${REDIS_PASSWORD}
dir ${DIFY_DATA_ROOT}/redis
appendonly yes
appendfsync everysec
maxmemory 1gb
maxmemory-policy allkeys-lru
save ""
daemonize no
protected-mode yes
logfile ""
ENVEOF
chown dify:dify "${CONFIG_DIR}/redis.conf"
ok "Redis 配置写入"

# --- 代码沙箱配置 ---
cat > "${SANDBOX_DIR}/config.yaml" <<ENVEOF
app:
  port: 8194
  debug: False
  key: dify-sandbox
max_workers: ${SANDBOX_MAX_WORKERS:-4}
max_requests: 50
worker_timeout: 15
python_path: ${SANDBOX_DIR}/python/bin/python3
enable_network: false
enable_preload: False
log_path: "${LOG_DIR}"
allowed_syscalls:
proxy:
  socks5: ''
  http: ''
  https: ''
  no_proxy: ''
allowed_env_vars: []
ENVEOF
ok "沙箱配置写入"

# --- 插件守护进程配置 ---
cat > "${CONFIG_DIR}/plugin-daemon.env" <<ENVEOF
SERVER_HOST=127.0.0.1
SERVER_PORT=5002
SERVER_KEY=${PLUGIN_DAEMON_KEY}
PLATFORM=local
DIFY_INNER_API_URL=http://127.0.0.1:5001
DIFY_INNER_API_KEY=${PLUGIN_DIFY_INNER_API_KEY}
DB_TYPE=postgresql
DB_HOST=127.0.0.1
DB_PORT=5432
DB_USERNAME=dify
DB_PASSWORD=${DB_PASSWORD}
DB_DATABASE=dify_plugin
DB_DEFAULT_DATABASE=dify_plugin
DB_SSL_MODE=disable
REDIS_HOST=127.0.0.1
REDIS_PORT=6379
REDIS_PASSWORD=${REDIS_PASSWORD}
REDIS_DB=0
REDIS_USE_SSL=false
PLUGIN_STORAGE_TYPE=local
PLUGIN_STORAGE_LOCAL_ROOT=${DIFY_DATA_ROOT}/plugin-storage
PLUGIN_WORKING_PATH=${DIFY_DATA_ROOT}/plugin-storage/cwd
PLUGIN_INSTALLED_PATH=plugin
PLUGIN_PACKAGE_CACHE_PATH=plugin_packages
PLUGIN_MEDIA_CACHE_PATH=assets
PLUGIN_MAX_EXECUTION_TIMEOUT=600
PLUGIN_REMOTE_INSTALLING_ENABLED=false
ROUTINE_POOL_SIZE=1024
GIN_MODE=release
# 离线关键：不要在启动时探测外网 PyPI
PIP_MIRROR_AUTO_DETECT=false
PIP_MIRROR_URL=
# 插件依赖从本地 wheel 仓库安装（离线）
PIP_EXTRA_ARGS=--no-index --find-links=${DIFY_ROOT}/pip-wheelhouse
PIP_PREFER_BINARY=true
PYTHON_INTERPRETER_PATH=${PY_DIR}/bin/python3
UV_PATH=${DIFY_ROOT}/bin/uv
UV_CACHE_DIR=${DIFY_DATA_ROOT}/plugin-storage/.uv-cache
FORCE_VERIFYING_SIGNATURE=false
ENFORCE_LANGGENIUS_PLUGIN_SIGNATURES=false
TZ=Asia/Shanghai
LOG_LEVEL=${LOG_LEVEL}
ENVEOF
chmod 640 "${CONFIG_DIR}/plugin-daemon.env"
chown dify:dify "${CONFIG_DIR}/plugin-daemon.env"
ok "插件守护进程配置写入"

# =============================================================================
step "初始化数据库"
# =============================================================================
su -s /bin/bash dify -c "${PG_PREFIX}/bin/pg_ctl -D ${DIFY_DATA_ROOT}/postgres -l ${LOG_DIR}/postgres.log -o '-c listen_addresses=127.0.0.1 -c port=5432' start" \
  >/dev/null 2>&1 || warn "PostgreSQL 可能已在运行"
sleep 3
PSQL="${PG_PREFIX}/bin/psql -h 127.0.0.1 -p 5432 -U dify -d postgres -tAc"
$PSQL "SELECT 1" >/dev/null 2>&1 || die "PostgreSQL 未就绪，请查看 ${LOG_DIR}/postgres.log"

sudo -u dify "${PG_PREFIX}/bin/psql" -h 127.0.0.1 -U dify -d postgres -c "ALTER USER dify WITH PASSWORD '${DB_PASSWORD}';" >/dev/null
for db in dify dify_plugin; do
  if ! $PSQL "SELECT 1 FROM pg_database WHERE datname='${db}'" | grep -q 1; then
    sudo -u dify "${PG_PREFIX}/bin/createdb" -h 127.0.0.1 -U dify -E UTF8 "${db}"
    ok "创建数据库 ${db}"
  else
    ok "数据库 ${db} 已存在"
  fi
done
sudo -u dify "${PG_PREFIX}/bin/psql" -h 127.0.0.1 -U dify -d dify -c "CREATE EXTENSION IF NOT EXISTS vector;" >/dev/null \
  || die "pgvector 扩展创建失败"
ok "pgvector 扩展已启用"

# =============================================================================
step "部署 Dify API 源码"
# =============================================================================
if [ ! -f "${API_DIR}/app.py" ]; then
  mkdir -p "${API_DIR}"
  tar -xzf "${BUNDLE_DIR}/packs/01-dify-source.tar.gz" -C "${API_DIR}" --strip-components=1
fi
mkdir -p "${API_DIR}/storage"
ln -sfn "${DIFY_DATA_ROOT}/storage" "${API_DIR}/storage" 2>/dev/null || true
chown -R dify:dify "${API_DIR}"
ok "API 源码部署到 ${API_DIR}"

# =============================================================================
step "部署 Web 前端"
# =============================================================================
if [ ! -f "${WEB_DIR}/app/web/server.js" ]; then
  mkdir -p "${WEB_DIR}"
  tar -xzf "${BUNDLE_DIR}/packs/04-web-runtime.tar.gz" -C "${WEB_DIR}"
fi
install -m 0755 "${BUNDLE_DIR}/vm/web-start.sh" "${WEB_DIR}/start.sh"
chown -R dify:dify "${WEB_DIR}"

# 生成 node 包装脚本：复用 web 的 musl 版 node，供沙箱执行 JS 代码节点
mkdir -p "${DIFY_ROOT}/bin"
cat > "${DIFY_ROOT}/bin/node" <<'NODEEOF'
#!/bin/sh
LIB=/opt/dify/web/lib:/opt/dify/web/usr/lib
exec /opt/dify/web/lib/ld-musl-x86_64.so.1 --library-path "${LIB}" /opt/dify/web/usr/local/bin/node "$@"
NODEEOF
chmod 0755 "${DIFY_ROOT}/bin/node"
ok "Web 前端部署到 ${WEB_DIR}（node 包装脚本已生成）"

# =============================================================================
step "部署代码沙箱"
# =============================================================================
mkdir -p "${SANDBOX_DIR}/glibc"

# ① 主程序：官方已编译好的二进制（比自编译更可靠）
if [ ! -x "${SANDBOX_DIR}/main" ]; then
  install -m 0755 "${BUNDLE_DIR}/prebuilt/sandbox-main" "${SANDBOX_DIR}/main" \
    || die "离线包缺少 prebuilt/sandbox-main"
fi

# ② 随包 glibc：与官方二进制同源，供 glibc < 2.34 的系统使用
if [ -d "${BUNDLE_DIR}/prebuilt/sandbox-glibc" ]; then
  cp -a "${BUNDLE_DIR}/prebuilt/sandbox-glibc/." "${SANDBOX_DIR}/glibc/" 2>/dev/null || true
fi

# ③ 沙箱专用 Python（与 Dify 主程序的 Python 隔离，避免代码节点能 import 到 Dify 内部依赖）
if [ ! -x "${SANDBOX_DIR}/python/bin/python3" ]; then
  mkdir -p "${SANDBOX_DIR}/python"
  tar -xzf "${BUNDLE_DIR}/packs/02b-sandbox-python.tar.gz" -C "${SANDBOX_DIR}/python" --strip-components=1
fi
"${SANDBOX_DIR}/python/bin/python3" -V >/dev/null || die "沙箱 Python 不可运行"

# ④ 启动包装脚本
install -m 0755 "${BUNDLE_DIR}/vm/sandbox-start.sh" "${SANDBOX_DIR}/start.sh"
chown -R root:root "${SANDBOX_DIR}"
ok "代码沙箱就绪（官方二进制 + 随包 glibc + 专用 Python）"

# =============================================================================
step "部署插件守护进程"
# =============================================================================
mkdir -p "${DAEMON_DIR}"
install -m 0755 "${BUNDLE_DIR}/prebuilt/plugin-daemon-main" "${DAEMON_DIR}/main"
install -m 0755 "${BUNDLE_DIR}/prebuilt/plugin-daemon-cli" "${DAEMON_DIR}/commandline"
# uv 供插件创建独立 Python 环境使用
if [ -f "${BUNDLE_DIR}/toolchain/uv" ]; then
  mkdir -p "${DIFY_ROOT}/bin"
  install -m 0755 "${BUNDLE_DIR}/toolchain/uv" "${DIFY_ROOT}/bin/uv"
fi
# 离线插件依赖仓库
mkdir -p "${DIFY_ROOT}/pip-wheelhouse"
if [ -f "${BUNDLE_DIR}/packs/09-plugin-wheelhouse.tar.gz" ]; then
  tar -xzf "${BUNDLE_DIR}/packs/09-plugin-wheelhouse.tar.gz" -C "${DIFY_ROOT}/pip-wheelhouse"
fi
chown -R dify:dify "${DAEMON_DIR}" "${DIFY_ROOT}/bin" "${DIFY_ROOT}/pip-wheelhouse" 2>/dev/null || true
ok "插件守护进程就绪"

# =============================================================================
step "安装 systemd 服务"
# =============================================================================
# PostgreSQL 单元（本机私有实例）
cat > /etc/systemd/system/postgresql-dify.service <<ENVEOF
[Unit]
Description=PostgreSQL for Dify
After=network-online.target
[Service]
Type=forking
User=dify
Group=dify
Environment=PGDATA=${DIFY_DATA_ROOT}/postgres
ExecStart=${PG_PREFIX}/bin/pg_ctl -D ${DIFY_DATA_ROOT}/postgres -l ${LOG_DIR}/postgres.log -o "-c listen_addresses=127.0.0.1 -c port=5432" start
ExecStop=${PG_PREFIX}/bin/pg_ctl -D ${DIFY_DATA_ROOT}/postgres stop -m fast
ExecReload=${PG_PREFIX}/bin/pg_ctl -D ${DIFY_DATA_ROOT}/postgres reload
Restart=always
RestartSec=5
[Install]
WantedBy=multi-user.target
ENVEOF

cat > /etc/systemd/system/redis-dify.service <<ENVEOF
[Unit]
Description=Redis for Dify
After=network-online.target
[Service]
Type=simple
User=dify
Group=dify
ExecStart=${REDIS_PREFIX}/bin/redis-server ${CONFIG_DIR}/redis.conf
Restart=always
RestartSec=5
[Install]
WantedBy=multi-user.target
ENVEOF

cp "${BUNDLE_DIR}"/vm/systemd/*.service /etc/systemd/system/
systemctl daemon-reload
ok "systemd 单元安装完成"

# =============================================================================
step "启动服务"
# =============================================================================
systemctl enable --now postgresql-dify.service redis-dify.service >/dev/null 2>&1
sleep 2
systemctl is-active --quiet postgresql-dify.service || die "PostgreSQL 启动失败：journalctl -u postgresql-dify -n 50"
ok "PostgreSQL 已启动"
systemctl is-active --quiet redis-dify.service || die "Redis 启动失败：journalctl -u redis-dify -n 50"
ok "Redis 已启动"

# 先让 api 跑数据库迁移
systemctl enable dify-api.service dify-worker.service dify-worker-beat.service dify-web.service dify-plugin-daemon.service dify-sandbox.service >/dev/null 2>&1
systemctl start dify-sandbox.service
systemctl start dify-api.service
log "等待数据库迁移与 API 就绪（最多 3 分钟）"
READY=0
for i in $(seq 1 90); do
  if curl -sf http://127.0.0.1:5001/health >/dev/null 2>&1; then READY=1; break; fi
  sleep 2
done
[ "${READY}" = "1" ] || die "API 未就绪：journalctl -u dify-api -n 80"
ok "API 已就绪"

systemctl start dify-plugin-daemon.service
systemctl start dify-worker.service dify-worker-beat.service
systemctl start dify-web.service
ok "全部服务已启动"

# =============================================================================
step "配置 Nginx"
# =============================================================================
install -m 0644 "${BUNDLE_DIR}/vm/nginx/dify-proxy.inc" /etc/nginx/conf.d/dify-proxy.inc
sed -e "s/listen       80;/listen       ${DIFY_HTTP_PORT};/" "${BUNDLE_DIR}/vm/nginx/dify.conf" > /etc/nginx/conf.d/dify.conf
if command -v nginx >/dev/null 2>&1; then
  nginx -t >/dev/null 2>&1 && { systemctl reload nginx 2>/dev/null || systemctl restart nginx; ok "Nginx 已重载"; } \
    || warn "nginx -t 未通过，请手工检查 /etc/nginx/conf.d/dify.conf"
else
  warn "系统未安装 nginx，请手动安装后使用 ${BUNDLE_DIR}/vm/nginx/dify.conf"
fi

# =============================================================================
step "完成"
# =============================================================================
echo
echo -e "${GREEN}部署完成！${NC}"
echo "  访问地址 : http://${DIFY_HOST}:${DIFY_HTTP_PORT}"
echo "  管理员密码: ${INIT_PASSWORD}（首次登录后请立即修改）"
echo "  安装目录 : ${DIFY_ROOT}"
echo "  数据目录 : ${DIFY_DATA_ROOT}"
echo "  日志目录 : ${LOG_DIR}"
echo
echo "常用命令："
echo "  systemctl status dify-api dify-worker dify-web dify-plugin-daemon dify-sandbox"
echo "  journalctl -u dify-api -f"
echo "  bash ${BUNDLE_DIR}/vm/verify.sh"
echo
echo -e "${YELLOW}下一步：登录控制台 → 设置 → 模型供应商 → 安装插件（离线 .difypkg 见 03-配置填写清单.md）${NC}"
