#!/usr/bin/env bash
set -Eeuo pipefail

umask 077

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
ENV_FILE="${PROJECT_ROOT}/.env"
CADDY_TEMPLATE="${SCRIPT_DIR}/Caddyfile.production.template"
CADDY_FILE="/etc/caddy/Caddyfile"
DOMAIN="${1:-}"
EXPECTED_PUBLIC_IP="${2:-}"

log() {
  printf '\n[Bags Production] %s\n' "$*"
}

fail() {
  printf '\n[Bags Production] ERROR: %s\n' "$*" >&2
  exit 1
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || fail "缺少命令：$1"
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
  local resolve_arg="${3:-}"
  local index
  for ((index = 1; index <= 60; index += 1)); do
    if [[ -n "${resolve_arg}" ]]; then
      if curl --fail --silent --show-error --max-time 5 --resolve "${resolve_arg}" "${url}" >/dev/null 2>&1; then
        log "${label} 已就绪"
        return 0
      fi
    elif curl --fail --silent --show-error --max-time 5 "${url}" >/dev/null 2>&1; then
      log "${label} 已就绪"
      return 0
    fi
    sleep 2
  done
  return 1
}

install_caddy() {
  if command -v caddy >/dev/null 2>&1; then
    return
  fi

  log "从 Caddy 官方 Ubuntu 仓库安装 Caddy"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update
  apt-get install -y debian-keyring debian-archive-keyring apt-transport-https curl gnupg
  curl --fail --silent --show-error --location \
    'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
    | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl --fail --silent --show-error --location \
    'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
    -o /etc/apt/sources.list.d/caddy-stable.list
  chmod o+r /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  chmod o+r /etc/apt/sources.list.d/caddy-stable.list
  apt-get update
  apt-get install -y caddy
}

[[ "$(uname -s)" == "Linux" ]] || fail "该脚本仅支持 Linux/Ubuntu"
((EUID == 0)) || fail "请使用 root 运行：sudo ./deploy/ubuntu-production.sh <域名> <公网IPv4>"
[[ "${DOMAIN}" =~ ^([a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+[a-zA-Z]{2,63}$ ]] \
  || fail "域名格式无效，例如：nmbags.org"
[[ "${EXPECTED_PUBLIC_IP}" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]] \
  || fail "公网 IPv4 格式无效，例如：43.156.30.192"

require_command docker
require_command openssl
require_command curl
require_command awk
require_command sed
require_command ip
require_command getent
require_command systemctl
docker compose version >/dev/null 2>&1 || fail "缺少 Docker Compose v2"

cd "${PROJECT_ROOT}"
[[ -f "${CADDY_TEMPLATE}" ]] || fail "缺少 Caddy 配置模板：${CADDY_TEMPLATE}"

resolved_ips="$(getent ahostsv4 "${DOMAIN}" 2>/dev/null | awk '{print $1}' | sort -u || true)"
if ! grep -Fxq "${EXPECTED_PUBLIC_IP}" <<<"${resolved_ips}"; then
  printf '当前解析到：\n%s\n' "${resolved_ips:-（无 A 记录）}" >&2
  fail "请先在 Cloudflare 将 ${DOMAIN} 的 A 记录指向 ${EXPECTED_PUBLIC_IP}，并设为仅 DNS（灰色云朵）"
fi

bind_ip="$(ip -4 route show table main default | awk '{for (field = 1; field <= NF; field += 1) if ($field == "src") {print $(field + 1); exit}}')"
if [[ -z "${bind_ip}" ]]; then
  main_interface="$(ip -4 route show table main default | awk '{for (field = 1; field <= NF; field += 1) if ($field == "dev") {print $(field + 1); exit}}')"
  [[ -n "${main_interface}" ]] || fail "无法识别腾讯云主网卡"
  bind_ip="$(ip -o -4 addr show dev "${main_interface}" scope global | awk 'NR == 1 {split($4, address, "/"); print address[1]}')"
fi
[[ -n "${bind_ip}" && "${bind_ip}" != "127.0.0.1" ]] || fail "无法识别腾讯云主网卡 IPv4"
log "公网 ${EXPECTED_PUBLIC_IP} 将通过腾讯云网络映射到本机 ${bind_ip}"

log "启动 Bags 基础服务并保留现有数据库"
bash "${SCRIPT_DIR}/ubuntu-bootstrap.sh"

set_env_value APP_ENV production
set_env_value CORS_ORIGINS "https://${DOMAIN}"
set_env_value AUTH_COOKIE_SECURE true
set_env_value AUTH_TRUST_PROXY_HEADERS true
set_env_value BAGS_API_BIND 127.0.0.1
chmod 600 "${ENV_FILE}"

docker compose config --quiet
docker compose up -d --build --force-recreate api
wait_for_url "http://127.0.0.1:8000/api/v1/ready" "生产模式 API" \
  || fail "生产模式 API 未能启动，请运行 docker compose logs --tail=120 api"

install_caddy

rendered_caddy="$(mktemp)"
trap 'rm -f "${rendered_caddy}"' EXIT
sed \
  -e "s|__DOMAIN__|${DOMAIN}|g" \
  -e "s|__BIND_IP__|${bind_ip}|g" \
  "${CADDY_TEMPLATE}" >"${rendered_caddy}"
caddy validate --config "${rendered_caddy}" --adapter caddyfile

if [[ -f "${CADDY_FILE}" ]] && ! cmp -s "${rendered_caddy}" "${CADDY_FILE}"; then
  backup_file="${CADDY_FILE}.bags-backup-$(date -u +%Y%m%dT%H%M%SZ)"
  cp --preserve=mode,ownership,timestamps "${CADDY_FILE}" "${backup_file}"
  log "原 Caddy 配置已备份到 ${backup_file}"
fi
install -o root -g root -m 0644 "${rendered_caddy}" "${CADDY_FILE}"

if command -v ufw >/dev/null 2>&1 && ufw status | grep -q '^Status: active'; then
  ufw allow 80/tcp
  ufw allow 443/tcp
fi

systemctl enable caddy >/dev/null
systemctl restart caddy

if ! wait_for_url "https://${DOMAIN}/api/v1/ready" "HTTPS API" "${DOMAIN}:443:${bind_ip}"; then
  journalctl -u caddy --no-pager -n 120 >&2 || true
  fail "HTTPS 证书或反向代理未能就绪；请确认腾讯云安全组已放行 TCP 80 和 443"
fi
wait_for_url "https://${DOMAIN}/" "HTTPS 前端" "${DOMAIN}:443:${bind_ip}" \
  || fail "HTTPS 前端未能就绪"

log "生产部署完成"
printf '访问地址：https://%s\n' "${DOMAIN}"
printf 'Caddy 绑定：%s:80 和 %s:443（保留 Tailscale 的独立 443 监听）\n' "${bind_ip}" "${bind_ip}"
printf 'Cloudflare：当前继续保持仅 DNS；若以后启用代理，SSL/TLS 模式必须选择 Full (strict)。\n'
