#!/bin/bash
# Dify 前端启动器（原生部署版）
# 作用：复刻官方 web/docker/entrypoint.sh 的运行时环境变量注入，然后用 musl 加载器启动 Next.js
# 为什么需要 musl 加载器：官方 node 是 Alpine(musl) 构建，不依赖 glibc，可在任何发行版上运行
set -e

LIB=/opt/dify/web/lib:/opt/dify/web/usr/lib
NODE=/opt/dify/web/usr/local/bin/node
SERVER=/opt/dify/web/app/targets/next/web/server.js

# ---- 运行时注入前端可见配置（与官方 entrypoint.sh 一致）----
export NEXT_PUBLIC_DEPLOY_ENV="${DEPLOY_ENV:-PRODUCTION}"
export NEXT_PUBLIC_BASE_PATH="${NEXT_PUBLIC_BASE_PATH:-}"
export NEXT_PUBLIC_API_PREFIX="${CONSOLE_API_URL}/console/api"
export NEXT_PUBLIC_PUBLIC_API_PREFIX="${APP_API_URL}/api"
export NEXT_PUBLIC_WEB_PREFIX="${NEXT_PUBLIC_WEB_PREFIX:-${CONSOLE_WEB_URL}}"
export NEXT_PUBLIC_MARKETPLACE_API_PREFIX="${MARKETPLACE_API_URL}/api/v1"
export NEXT_PUBLIC_MARKETPLACE_URL_PREFIX="${MARKETPLACE_URL}"
export NEXT_PUBLIC_SOCKET_URL="${NEXT_PUBLIC_SOCKET_URL}"
export NEXT_PUBLIC_COOKIE_DOMAIN="${NEXT_PUBLIC_COOKIE_DOMAIN:-}"
export NEXT_PUBLIC_SITE_ABOUT="${SITE_ABOUT:-}"
export NEXT_PUBLIC_TEXT_GENERATION_TIMEOUT_MS="${TEXT_GENERATION_TIMEOUT_MS:-}"
export NEXT_PUBLIC_WORKFLOW_GENERATION_TIMEOUT_MS="${NEXT_PUBLIC_WORKFLOW_GENERATION_TIMEOUT_MS:-180000}"
export NEXT_PUBLIC_TOP_K_MAX_VALUE="${TOP_K_MAX_VALUE:-}"
export NEXT_PUBLIC_INDEXING_MAX_SEGMENTATION_TOKENS_LENGTH="${INDEXING_MAX_SEGMENTATION_TOKENS_LENGTH:-}"
export NEXT_PUBLIC_MAX_TOOLS_NUM="${MAX_TOOLS_NUM:-}"
export NEXT_PUBLIC_ALLOW_EMBED="${ALLOW_EMBED:-false}"
export NEXT_PUBLIC_ENABLE_AGENT_V2="${ENABLE_AGENT_V2:-false}"
export NEXT_PUBLIC_ENABLE_SINGLE_DOLLAR_LATEX="${NEXT_PUBLIC_ENABLE_SINGLE_DOLLAR_LATEX:-false}"

# ---- 以 musl 加载器启动（不污染系统 /lib）----
exec "${LIB}/ld-musl-x86_64.so.1" --library-path "${LIB}" "${NODE}" "${SERVER}"
