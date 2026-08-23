#!/usr/bin/env bash
set -Eeuo pipefail

umask 077

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
ENV_FILE="${PROJECT_ROOT}/.env"
UI_CONTAINER="bags-ui"
UI_IMAGE="bags-ui:local"

log() {
  printf '\n[Bags] %s\n' "$*"
}

fail() {
  printf '\n[Bags] ERROR: %s\n' "$*" >&2
  exit 1
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || fail "缺少命令：$1"
}

env_value() {
  local key="$1"
  awk -F= -v wanted="${key}" '$1 == wanted { sub(/^[^=]*=/, ""); print; exit }' "${ENV_FILE}"
}

set_env_value() {
  local key="$1"
  local value="$2"
  if grep -q "^${key}=" "${ENV_FILE}"; then
    sed -i "s|^${key}=.*|${key}=${value}|" "${ENV_FILE}"
  else
    printf '%s=%s\n' "${key}" "${value}" >>"${ENV_FILE}"
  fi
}

wait_for_url() {
  local url="$1"
  local label="$2"
  local attempts=60
  local index
  for ((index = 1; index <= attempts; index += 1)); do
    if curl --fail --silent --show-error --max-time 3 "${url}" >/dev/null 2>&1; then
      log "${label} 已就绪"
      return 0
    fi
    sleep 2
  done
  return 1
}

[[ "$(uname -s)" == "Linux" ]] || fail "该脚本仅支持 Linux/Ubuntu"

require_command docker
require_command openssl
require_command curl
require_command awk
require_command sed

docker compose version >/dev/null 2>&1 || fail "缺少 Docker Compose v2"
docker info >/dev/null 2>&1 || fail "当前用户无法访问 Docker；请先安装 Docker 或加入 docker 用户组"

cd "${PROJECT_ROOT}"

fresh_env=false
if [[ ! -f "${ENV_FILE}" ]]; then
  cp "${PROJECT_ROOT}/.env.example" "${ENV_FILE}"
  fresh_env=true
  log "已从 .env.example 创建 .env"
fi
chmod 600 "${ENV_FILE}"

db_password="$(env_value POSTGRES_PASSWORD)"
if [[ -z "${db_password}" || "${db_password}" == "change-me-before-production" ]]; then
  db_password="$(openssl rand -hex 24)"
  set_env_value POSTGRES_PASSWORD "${db_password}"
fi

postgres_user="$(env_value POSTGRES_USER)"
postgres_db="$(env_value POSTGRES_DB)"
postgres_user="${postgres_user:-bags}"
postgres_db="${postgres_db:-bags}"

database_url="$(env_value DATABASE_URL)"
if [[ -z "${database_url}" || "${database_url}" == *"change-me-before-production"* ]]; then
  set_env_value DATABASE_URL "postgresql+psycopg://${postgres_user}:${db_password}@postgres:5432/${postgres_db}"
fi

master_key="$(env_value MASTER_ENCRYPTION_KEY)"
if [[ -z "${master_key}" ]]; then
  master_key="$(openssl rand -base64 32 | tr '+/' '-_' | tr -d '=')"
  set_env_value MASTER_ENCRYPTION_KEY "${master_key}"
fi

bootstrap_token="$(env_value AUTH_BOOTSTRAP_TOKEN)"
if [[ "${fresh_env}" == true && -z "${bootstrap_token}" ]]; then
  bootstrap_token="$(openssl rand -hex 32)"
  set_env_value AUTH_BOOTSTRAP_TOKEN "${bootstrap_token}"
fi

set_env_value BAGS_API_BIND 127.0.0.1
set_env_value AUTH_COOKIE_SECURE false
chmod 600 "${ENV_FILE}"

docker compose config --quiet

log "构建并启动 PostgreSQL、Redis 和 Bags API"
docker compose up -d --build

if ! wait_for_url "http://127.0.0.1:8000/api/v1/ready" "Bags API"; then
  docker compose ps >&2 || true
  docker compose logs --tail=120 api >&2 || true
  fail "API 未能在规定时间内就绪"
fi

user_count="$(docker compose exec -T postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc "SELECT count(*) FROM users;"' | tr -d '[:space:]')"
[[ "${user_count}" =~ ^[0-9]+$ ]] || fail "无法确认管理员账户状态"

bootstrap_token="$(env_value AUTH_BOOTSTRAP_TOKEN)"
if ((user_count == 0)) && [[ -z "${bootstrap_token}" ]]; then
  bootstrap_token="$(openssl rand -hex 32)"
  set_env_value AUTH_BOOTSTRAP_TOKEN "${bootstrap_token}"
  docker compose up -d --force-recreate api
  wait_for_url "http://127.0.0.1:8000/api/v1/ready" "更新后的 Bags API" || fail "API 重启失败"
elif ((user_count > 0)) && [[ -n "${bootstrap_token}" ]]; then
  set_env_value AUTH_BOOTSTRAP_TOKEN ""
  bootstrap_token=""
  docker compose up -d --force-recreate api
  wait_for_url "http://127.0.0.1:8000/api/v1/ready" "已关闭注册的 Bags API" || fail "API 重启失败"
fi

log "构建并启动 Bags 前端容器"
docker build --file deploy/ui.Dockerfile --tag "${UI_IMAGE}" .

if docker container inspect "${UI_CONTAINER}" >/dev/null 2>&1; then
  managed_label="$(docker inspect --format '{{ index .Config.Labels "com.bags.managed" }}' "${UI_CONTAINER}")"
  [[ "${managed_label}" == "true" ]] || fail "已存在同名但不属于本项目的容器：${UI_CONTAINER}"
  docker rm --force "${UI_CONTAINER}" >/dev/null
fi

docker run --detach \
  --name "${UI_CONTAINER}" \
  --label com.bags.managed=true \
  --restart unless-stopped \
  --network host \
  --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,size=16m \
  --cap-drop ALL \
  --security-opt no-new-privileges \
  "${UI_IMAGE}" >/dev/null

if ! wait_for_url "http://127.0.0.1:4173" "Bags 前端"; then
  docker logs --tail=120 "${UI_CONTAINER}" >&2 || true
  fail "前端未能在规定时间内就绪"
fi

log "启动完成"
docker compose ps
printf '\n前端：  http://127.0.0.1:4173\n'
printf 'API：   http://127.0.0.1:8000/api/v1/health\n'
printf 'SSH 隧道：ssh -L 4173:127.0.0.1:4173 -L 8000:127.0.0.1:8000 <用户>@<服务器IP>\n'

if ((user_count == 0)); then
  printf '\n首次管理员启动令牌：\n%s\n' "${bootstrap_token}"
  printf '\n创建管理员后，再运行一次：\n./deploy/ubuntu-bootstrap.sh\n'
  printf '第二次运行会检测到管理员并自动清除启动令牌。\n'
else
  printf '\n管理员账户已存在，公开注册保持关闭。\n'
fi
