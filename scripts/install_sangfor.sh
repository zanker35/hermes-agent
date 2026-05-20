#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." >/dev/null 2>&1 && pwd)"

COMPOSE_FILE="${REPO_ROOT}/docker-compose-sangfor.yml"
DOCKERFILE="${REPO_ROOT}/Dockerfile_sangfor.yaml"
PROJECT_NAME="${HERMES_SANGFOR_PROJECT:-hermes-sangfor}"

usage() {
  cat <<'EOF'
Usage:
  scripts/install_sangfor.sh [deploy|build|up|restart|down|logs|ps|config]

Default:
  deploy    Build the Sangfor image from the current checkout, then recreate
            and start the local gateway + dashboard containers.

Environment overrides:
  HERMES_BUILD_PROXY      Build-time proxy. Defaults come from docker-compose-sangfor.yml.
  HERMES_RUNTIME_PROXY    Runtime proxy. Defaults come from docker-compose-sangfor.yml.
  HERMES_NO_PROXY         no_proxy/NO_PROXY override.
  HERMES_SANGFOR_PROJECT  Docker Compose project name. Default: hermes-sangfor.

Examples:
  scripts/install_sangfor.sh
  HERMES_RUNTIME_PROXY=http://192.168.65.254:7890 scripts/install_sangfor.sh deploy
  scripts/install_sangfor.sh logs
EOF
}

die() {
  printf 'error: %s\n' "$*" >&2
  exit 1
}

compose() {
  if docker compose version >/dev/null 2>&1; then
    docker compose "$@"
  elif command -v docker-compose >/dev/null 2>&1; then
    docker-compose "$@"
  else
    die "Docker Compose is not available. Install Docker Desktop or docker compose."
  fi
}

require_files() {
  [[ -f "${DOCKERFILE}" ]] || die "missing ${DOCKERFILE}"
  [[ -f "${COMPOSE_FILE}" ]] || die "missing ${COMPOSE_FILE}"
}

prepare_env() {
  export HERMES_UID="${HERMES_UID:-$(id -u)}"
  export HERMES_GID="${HERMES_GID:-$(id -g)}"
  mkdir -p "${HOME}/.hermes"
}

run_compose() {
  compose --project-name "${PROJECT_NAME}" --file "${COMPOSE_FILE}" "$@"
}

deploy() {
  require_files
  prepare_env
  cd "${REPO_ROOT}"

  printf 'Using Dockerfile: %s\n' "${DOCKERFILE}"
  printf 'Using compose:    %s\n' "${COMPOSE_FILE}"
  printf 'Compose project:  %s\n' "${PROJECT_NAME}"
  printf 'Hermes home:      %s\n' "${HOME}/.hermes"

  run_compose build
  run_compose up -d --force-recreate --remove-orphans

  printf '\nSangfor Hermes stack is starting.\n'
  printf 'Dashboard: http://127.0.0.1:9119\n'
  printf 'Status:    scripts/install_sangfor.sh ps\n'
  printf 'Logs:      scripts/install_sangfor.sh logs\n'
}

cmd="${1:-deploy}"
case "${cmd}" in
  deploy|install)
    deploy
    ;;
  build)
    require_files
    prepare_env
    cd "${REPO_ROOT}"
    run_compose build
    ;;
  up)
    require_files
    prepare_env
    cd "${REPO_ROOT}"
    run_compose up -d --force-recreate --remove-orphans
    ;;
  restart)
    require_files
    prepare_env
    cd "${REPO_ROOT}"
    run_compose up -d --build --force-recreate --remove-orphans
    ;;
  down)
    require_files
    cd "${REPO_ROOT}"
    run_compose down --remove-orphans
    ;;
  logs)
    require_files
    cd "${REPO_ROOT}"
    run_compose logs -f --tail=200
    ;;
  ps|status)
    require_files
    cd "${REPO_ROOT}"
    run_compose ps
    ;;
  config)
    require_files
    prepare_env
    cd "${REPO_ROOT}"
    run_compose config
    ;;
  help|-h|--help)
    usage
    ;;
  *)
    usage >&2
    die "unknown command: ${cmd}"
    ;;
esac
