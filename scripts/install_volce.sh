#!/usr/bin/env bash
set -Eeuo pipefail

# Deploy the current local Hermes checkout to the Volce production host.
#
# This is intentionally not a thin wrapper around scripts/install.sh:
# the Volce host runs multiple systemd gateway services from one shared
# /usr/local/lib/hermes-agent checkout, so upgrades need explicit backups,
# comparison, dependency prebuild, config audit, and staged restarts.

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." >/dev/null 2>&1 && pwd)"

SSH_TARGET="${HERMES_VOLCE_SSH_TARGET:-OpenClawVolcengine}"
REMOTE_INSTALL_DIR="${HERMES_VOLCE_INSTALL_DIR:-/usr/local/lib/hermes-agent}"
REMOTE_HERMES_HOME="${HERMES_VOLCE_HOME:-/root/.hermes}"
REMOTE_BACKUP_ROOT="${HERMES_VOLCE_BACKUP_ROOT:-/root/hermes-deploy-backups}"
REMOTE_CONFIG_BACKUP_ROOT="${HERMES_VOLCE_CONFIG_BACKUP_ROOT:-/root/hermes-config-backups}"
REMOTE_BRANCH="${HERMES_VOLCE_DEPLOY_BRANCH:-deploy-claw-volce}"
PYTHON_VERSION="${HERMES_VOLCE_PYTHON_VERSION:-3.11}"
NODE_BIN_DIR="${HERMES_VOLCE_NODE_BIN_DIR:-/root/.nvm/versions/node/v24.14.0/bin}"
COMMIT_MESSAGE="${HERMES_VOLCE_COMMIT_MESSAGE:-deploy: claw volce current workspace}"

ASSUME_YES=false
SKIP_TESTS=false
SKIP_NODE=false
SKIP_CONFIG_SYNC=false
NO_COMMIT=false
COMMAND="deploy"

usage() {
  cat <<'EOF'
Usage:
  scripts/install_volce.sh [deploy|status|config-audit] [options]

Default command:
  deploy                 Commit current workspace if needed, bundle it, upload
                         it to Volce, compare, install, audit config, and
                         restart Hermes gateway services.

Commands:
  deploy                 Full staged deployment.
  status                 Show remote Hermes code, version, and gateway services.
  config-audit           Inspect remote HERMES_HOME/profile config gaps only.

Options:
  --target HOST          SSH target. Default: OpenClawVolcengine
  --yes, -y              Auto-confirm every prompt.
  --no-commit            Refuse to deploy if the local working tree is dirty.
  --skip-tests           Skip remote targeted pytest.
  --skip-node            Skip remote npm dependency install.
  --skip-config-sync     Audit config but do not offer to apply Gemini sync.
  --help, -h             Show this help.

Environment overrides:
  HERMES_VOLCE_SSH_TARGET       SSH target (default: OpenClawVolcengine)
  HERMES_VOLCE_INSTALL_DIR      Remote code dir (default: /usr/local/lib/hermes-agent)
  HERMES_VOLCE_HOME             Remote Hermes home (default: /root/.hermes)
  HERMES_VOLCE_NODE_BIN_DIR     Node bin dir if node is not on PATH
  HERMES_VOLCE_COMMIT_MESSAGE   Commit message used when local tree is dirty

Notes:
  - This script does not push to GitHub. It transfers a git bundle directly.
  - It never silently overwrites remote ~/.hermes files. It reports config gaps
    and only applies the Gemini profile sync after confirmation (or --yes).
  - All gateway services share one checkout and one venv, so the final switch
    is a short maintenance window, not a true zero-downtime rolling deploy.
EOF
}

log() { printf '%s\n' "$*"; }
info() { printf '%s\n' "-> $*"; }
ok() { printf '%s\n' "OK: $*"; }
warn() { printf '%s\n' "WARN: $*" >&2; }
die() { printf '%s\n' "ERROR: $*" >&2; exit 1; }

confirm() {
  local prompt="$1"
  local default="${2:-no}"
  local suffix answer

  if [ "$ASSUME_YES" = true ]; then
    info "$prompt [auto-yes]"
    return 0
  fi

  if [ "$default" = "yes" ]; then
    suffix="[Y/n]"
  else
    suffix="[y/N]"
  fi

  if ! [ -t 0 ]; then
    die "Cannot prompt in a non-interactive shell. Re-run with --yes if intentional."
  fi

  printf '%s %s ' "$prompt" "$suffix"
  IFS= read -r answer || answer=""
  case "$answer" in
    "" )
      [ "$default" = "yes" ]
      ;;
    y|Y|yes|YES|Yes )
      return 0
      ;;
    * )
      return 1
      ;;
  esac
}

parse_args() {
  while [ $# -gt 0 ]; do
    case "$1" in
      deploy|status|config-audit)
        COMMAND="$1"
        shift
        ;;
      --target)
        SSH_TARGET="${2:-}"
        [ -n "$SSH_TARGET" ] || die "--target requires a value"
        shift 2
        ;;
      --yes|-y)
        ASSUME_YES=true
        shift
        ;;
      --no-commit)
        NO_COMMIT=true
        shift
        ;;
      --skip-tests)
        SKIP_TESTS=true
        shift
        ;;
      --skip-node)
        SKIP_NODE=true
        shift
        ;;
      --skip-config-sync)
        SKIP_CONFIG_SYNC=true
        shift
        ;;
      --help|-h)
        usage
        exit 0
        ;;
      *)
        die "Unknown argument: $1"
        ;;
    esac
  done
}

require_local_tools() {
  command -v git >/dev/null 2>&1 || die "git is required locally"
  command -v ssh >/dev/null 2>&1 || die "ssh is required locally"
  command -v scp >/dev/null 2>&1 || die "scp is required locally"
}

ssh_cmd() {
  ssh -o BatchMode=yes -o ConnectTimeout=15 "$SSH_TARGET" "$@"
}

scp_to_remote() {
  scp -o BatchMode=yes -o ConnectTimeout=15 -q "$1" "$SSH_TARGET:$2"
}

remote_bash() {
  ssh -o BatchMode=yes -o ConnectTimeout=15 "$SSH_TARGET" bash -s -- "$@"
}

remote_status() {
  remote_bash "$REMOTE_INSTALL_DIR" "$REMOTE_HERMES_HOME" <<'REMOTE'
set -Eeuo pipefail
install_dir="$1"
hermes_home="$2"

printf 'Remote host: %s\n' "$(hostname)"
printf 'User: %s\n' "$(whoami)"
printf 'Install dir: %s\n' "$install_dir"
printf 'Hermes home: %s\n' "$hermes_home"
printf '\n'

if [ -d "$install_dir/.git" ]; then
  git -C "$install_dir" status --short --branch
  printf 'HEAD=%s\n' "$(git -C "$install_dir" rev-parse HEAD)"
  printf 'BRANCH=%s\n' "$(git -C "$install_dir" rev-parse --abbrev-ref HEAD)"
  git -C "$install_dir" remote -v || true
else
  printf 'No git checkout at %s\n' "$install_dir"
fi

printf '\nHermes version:\n'
if command -v hermes >/dev/null 2>&1; then
  hermes version || true
elif [ -x "$install_dir/venv/bin/hermes" ]; then
  "$install_dir/venv/bin/hermes" version || true
else
  printf 'hermes command not found\n'
fi

printf '\nGateway services:\n'
systemctl list-units --type=service --all 'hermes-gateway*.service' --no-pager || true
REMOTE
}

ensure_local_commit() {
  cd "$REPO_ROOT"

  git rev-parse --show-toplevel >/dev/null 2>&1 || die "not inside a git repository"

  local dirty
  dirty="$(git status --porcelain)"
  if [ -n "$dirty" ]; then
    log "Local working tree has changes:"
    git status --short
    log ""
    git diff --stat || true
    log ""

    if [ "$NO_COMMIT" = true ]; then
      die "local working tree is dirty and --no-commit was set"
    fi

    confirm "Create a deployment commit with all local changes?" "yes" \
      || die "deployment cancelled before local commit"

    git add -A
    if git diff --cached --quiet; then
      die "nothing staged after git add -A"
    fi
    git commit -m "$COMMIT_MESSAGE"
  fi

  DEPLOY_SHA="$(git rev-parse HEAD)"
  DEPLOY_BRANCH="$(git rev-parse --abbrev-ref HEAD)"
  export DEPLOY_SHA DEPLOY_BRANCH

  log "Local deployment commit:"
  git show --stat --oneline --decorate --no-renames HEAD
}

create_bundle() {
  cd "$REPO_ROOT"
  WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/hermes-volce.XXXXXX")"
  BUNDLE_PATH="$WORK_DIR/hermes-agent-$DEPLOY_SHA.bundle"
  export WORK_DIR BUNDLE_PATH

  info "Creating git bundle: $BUNDLE_PATH"
  git bundle create "$BUNDLE_PATH" HEAD --branches --tags >/dev/null
  git bundle verify "$BUNDLE_PATH"
  ls -lh "$BUNDLE_PATH"
}

remote_preflight() {
  info "Running remote preflight on $SSH_TARGET"
  remote_bash "$REMOTE_INSTALL_DIR" "$REMOTE_HERMES_HOME" "$NODE_BIN_DIR" <<'REMOTE'
set -Eeuo pipefail
install_dir="$1"
hermes_home="$2"
node_bin_dir="$3"

[ "$(id -u)" = "0" ] || { echo "Expected root on remote"; exit 1; }
[ -d "$install_dir/.git" ] || { echo "Missing git checkout: $install_dir"; exit 1; }
[ -d "$hermes_home" ] || { echo "Missing HERMES_HOME: $hermes_home"; exit 1; }
command -v git >/dev/null || { echo "git missing"; exit 1; }
command -v systemctl >/dev/null || { echo "systemctl missing"; exit 1; }
uv_bin="$(command -v uv || true)"
if [ -z "$uv_bin" ] && [ -x /root/.local/bin/uv ]; then
  uv_bin=/root/.local/bin/uv
fi
[ -n "$uv_bin" ] || { echo "uv missing"; exit 1; }

printf 'Remote preflight OK\n'
printf 'Host: %s\n' "$(hostname)"
printf 'Git: %s\n' "$(git --version)"
printf 'uv: %s\n' "$("$uv_bin" --version)"
printf 'Current HEAD: %s\n' "$(git -C "$install_dir" rev-parse HEAD)"
printf 'Current branch: %s\n' "$(git -C "$install_dir" rev-parse --abbrev-ref HEAD)"
printf 'Disk:\n'
df -h "$install_dir" "$hermes_home"

printf 'Node:\n'
if command -v node >/dev/null 2>&1; then
  node --version
elif [ -x "$node_bin_dir/node" ]; then
  "$node_bin_dir/node" --version
else
  printf 'node not found; npm install will fail unless --skip-node is used\n'
fi

printf 'Gateway services:\n'
systemctl list-units --type=service --all 'hermes-gateway*.service' --no-pager
REMOTE
}

upload_bundle() {
  local remote_bundle="/tmp/hermes-agent-$DEPLOY_SHA.bundle"
  export REMOTE_BUNDLE="$remote_bundle"

  confirm "Upload bundle to $SSH_TARGET:$remote_bundle?" "yes" \
    || die "deployment cancelled before upload"

  scp_to_remote "$BUNDLE_PATH" "$remote_bundle"
  remote_bash "$remote_bundle" <<'REMOTE'
set -Eeuo pipefail
bundle="$1"
ls -lh "$bundle"
git bundle verify "$bundle" >/dev/null
REMOTE
  ok "Bundle uploaded and verified"
}

remote_backup() {
  info "Creating remote backup"
  remote_bash "$REMOTE_INSTALL_DIR" "$REMOTE_HERMES_HOME" "$REMOTE_BACKUP_ROOT" <<'REMOTE'
set -Eeuo pipefail
install_dir="$1"
hermes_home="$2"
backup_root="$3"

ts="$(date -u +%Y%m%d-%H%M%S)"
backup="$backup_root/$ts"
mkdir -p "$backup/systemd" "$backup/hermes-home"

cp -a /etc/systemd/system/hermes-gateway*.service "$backup/systemd/" 2>/dev/null || true
[ -f /usr/local/bin/hermes ] && cp -a /usr/local/bin/hermes "$backup/hermes-launcher"

git -C "$install_dir" rev-parse HEAD > "$backup/old-head.txt"
git -C "$install_dir" rev-parse --abbrev-ref HEAD > "$backup/old-branch.txt"
git -C "$install_dir" remote -v > "$backup/old-remotes.txt" || true
git -C "$install_dir" status --short --branch > "$backup/old-status.txt" || true

if command -v hermes >/dev/null 2>&1; then
  hermes version > "$backup/old-hermes-version.txt" 2>&1 || true
elif [ -x "$install_dir/venv/bin/hermes" ]; then
  "$install_dir/venv/bin/hermes" version > "$backup/old-hermes-version.txt" 2>&1 || true
fi

systemctl list-units --type=service --all 'hermes-gateway*.service' --no-pager > "$backup/services-before.txt" || true
ps -ef | grep '[h]ermes_cli.main' > "$backup/processes-before.txt" || true

for f in "$hermes_home/.env" "$hermes_home/config.yaml"; do
  [ -f "$f" ] && cp -a "$f" "$backup/hermes-home/"
done
if [ -d "$hermes_home/profiles" ]; then
  mkdir -p "$backup/hermes-home/profiles"
  for p in "$hermes_home"/profiles/*; do
    [ -d "$p" ] || continue
    mkdir -p "$backup/hermes-home/profiles/$(basename "$p")"
    [ -f "$p/.env" ] && cp -a "$p/.env" "$backup/hermes-home/profiles/$(basename "$p")/.env"
    [ -f "$p/config.yaml" ] && cp -a "$p/config.yaml" "$backup/hermes-home/profiles/$(basename "$p")/config.yaml"
  done
fi

tar --exclude="$install_dir/venv" \
    --exclude="$install_dir/.deploy-venv-next" \
    --exclude="$install_dir/node_modules" \
    --exclude="$install_dir/ui-tui/node_modules" \
    --exclude="$install_dir/web/node_modules" \
    -czf "$backup/hermes-agent-code-no-venv.tgz" \
    -C "$(dirname "$install_dir")" "$(basename "$install_dir")"

printf '%s\n' "$backup" > /tmp/hermes-volce-last-backup
du -sh "$backup"
printf 'BACKUP_DIR=%s\n' "$backup"
REMOTE
  REMOTE_BACKUP_DIR="$(ssh_cmd 'cat /tmp/hermes-volce-last-backup')"
  export REMOTE_BACKUP_DIR
}

remote_fetch_and_compare() {
  info "Fetching deployment commit on remote and comparing changes"
  remote_bash "$REMOTE_INSTALL_DIR" "$REMOTE_BUNDLE" "$DEPLOY_SHA" "$REMOTE_BRANCH" "$REMOTE_BACKUP_DIR" <<'REMOTE'
set -Eeuo pipefail
install_dir="$1"
bundle="$2"
deploy_sha="$3"
remote_branch="$4"
backup_dir="$5"

cd "$install_dir"
old_sha="$(git rev-parse HEAD)"
old_branch="$(git rev-parse --abbrev-ref HEAD)"

git bundle verify "$bundle" >/dev/null
git fetch "$bundle" "$deploy_sha:refs/heads/$remote_branch" --force
test "$(git rev-parse "refs/heads/$remote_branch")" = "$deploy_sha"

{
  printf 'OLD_BRANCH=%s\n' "$old_branch"
  printf 'OLD_SHA=%s\n' "$old_sha"
  printf 'NEW_BRANCH=%s\n' "$remote_branch"
  printf 'NEW_SHA=%s\n' "$deploy_sha"
  printf '\nCommit relation (old...new):\n'
  git rev-list --left-right --count "$old_sha...$deploy_sha" || true
  printf '\nNew commits:\n'
  git log --oneline --decorate "$old_sha..$deploy_sha" || true
  printf '\nChanged files:\n'
  git diff --stat "$old_sha" "$deploy_sha" || true
} | tee "$backup_dir/upgrade-comparison.txt"
REMOTE
}

remote_checkout_and_build() {
  confirm "Checkout remote $REMOTE_BRANCH and prebuild dependencies?" "yes" \
    || die "deployment cancelled before checkout/build"

  remote_bash "$REMOTE_INSTALL_DIR" "$DEPLOY_SHA" "$REMOTE_BRANCH" "$PYTHON_VERSION" "$NODE_BIN_DIR" "$SKIP_TESTS" "$SKIP_NODE" "$REMOTE_BACKUP_DIR" <<'REMOTE'
set -Eeuo pipefail
install_dir="$1"
deploy_sha="$2"
remote_branch="$3"
python_version="$4"
node_bin_dir="$5"
skip_tests="$6"
skip_node="$7"
backup_dir="$8"

cd "$install_dir"
uv_bin="$(command -v uv || true)"
if [ -z "$uv_bin" ] && [ -x /root/.local/bin/uv ]; then
  uv_bin=/root/.local/bin/uv
fi
[ -n "$uv_bin" ] || { echo "uv missing"; exit 1; }

if [ -n "$(git status --porcelain)" ]; then
  stash_name="pre-volce-deploy-$(date -u +%Y%m%d-%H%M%S)"
  git stash push --include-untracked -m "$stash_name"
  git rev-parse --verify refs/stash > "$backup_dir/pre-deploy-stash-ref.txt"
fi

git checkout "$remote_branch"
test "$(git rev-parse HEAD)" = "$deploy_sha"
git status --short --branch

rm -rf .deploy-venv-next
"$uv_bin" venv .deploy-venv-next --python "$python_version"
VIRTUAL_ENV="$install_dir/.deploy-venv-next" "$uv_bin" pip install -e ".[all]"
".deploy-venv-next/bin/python" --version
".deploy-venv-next/bin/hermes" version

if [ "$skip_tests" != "true" ] && [ -f tests/tools/test_web_providers_gemini_grounding.py ]; then
  ".deploy-venv-next/bin/python" -m pytest tests/tools/test_web_providers_gemini_grounding.py -q
fi

if [ "$skip_node" != "true" ]; then
  export PATH="$node_bin_dir:$install_dir/.deploy-venv-next/bin:$PATH"
  if ! command -v node >/dev/null 2>&1; then
    echo "node not found. Re-run with --skip-node or set HERMES_VOLCE_NODE_BIN_DIR." >&2
    exit 1
  fi
  node --version
  npm --version
  if [ -f package.json ]; then
    npm install --silent
  fi
  if [ -f ui-tui/package.json ]; then
    (cd ui-tui && npm install --silent)
  fi
fi
REMOTE
}

remote_config_audit() {
  info "Auditing remote Hermes config and profile drift"
  remote_bash "$REMOTE_INSTALL_DIR" "$REMOTE_HERMES_HOME" "$REMOTE_BACKUP_DIR" <<'REMOTE'
set -Eeuo pipefail
install_dir="$1"
hermes_home="$2"
backup_dir="$3"
py="$install_dir/.deploy-venv-next/bin/python"
[ -x "$py" ] || py="$install_dir/venv/bin/python"

"$py" - "$install_dir" "$hermes_home" "$backup_dir" <<'PY'
from pathlib import Path
import os
import sys
import yaml

install_dir = Path(sys.argv[1])
hermes_home = Path(sys.argv[2])
backup_dir = Path(sys.argv[3])
sys.path.insert(0, str(install_dir))

try:
    from hermes_cli.config import DEFAULT_CONFIG
except Exception:
    DEFAULT_CONFIG = {}

known_env_keys = {
    "GEMINI_GROUNDING_API_KEY",
    "GEMINI_GROUNDING_MODEL",
    "GEMINI_GROUNDING_BASE_URL",
}

def parse_env(path: Path) -> dict[str, str]:
    out = {}
    if not path.exists():
        return out
    for raw in path.read_text(errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out

def load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(errors="replace")) or {}
    return data if isinstance(data, dict) else {}

def flatten_missing(defaults, actual, prefix=""):
    missing = []
    if not isinstance(defaults, dict):
        return missing
    if not isinstance(actual, dict):
        actual = {}
    for key, value in defaults.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if key not in actual:
            missing.append(path)
        elif isinstance(value, dict):
            missing.extend(flatten_missing(value, actual.get(key), path))
    return missing

homes = [hermes_home] + sorted((hermes_home / "profiles").glob("*"))
root_env = parse_env(hermes_home / ".env")
root_cfg = load_yaml(hermes_home / "config.yaml")
root_web = root_cfg.get("web") if isinstance(root_cfg.get("web"), dict) else {}
root_gemini = {k: v for k, v in root_env.items() if k in known_env_keys and v}

report = []
sync_needed = False

for home in homes:
    if not home.is_dir():
        continue
    name = "default" if home == hermes_home else home.name
    env = parse_env(home / ".env")
    cfg = load_yaml(home / "config.yaml")
    web = cfg.get("web") if isinstance(cfg.get("web"), dict) else {}
    missing_defaults = flatten_missing(DEFAULT_CONFIG, cfg)

    report.append(f"PROFILE {name}")
    for k in sorted(known_env_keys):
        state = "SET" if env.get(k) else "MISSING"
        report.append(f"  env.{k}={state}")
    report.append(f"  web.backend={web.get('backend')!r}")
    report.append(f"  web.search_backend={web.get('search_backend')!r}")
    report.append(f"  web.extract_backend={web.get('extract_backend')!r}")

    if name != "default" and root_gemini:
        missing_gemini = [k for k in root_gemini if not env.get(k)]
        if missing_gemini:
            sync_needed = True
            report.append("  SUGGEST: sync root Gemini env keys: " + ", ".join(missing_gemini))

    root_search = root_web.get("search_backend") or (
        root_web.get("backend") if root_web.get("backend") == "gemini-grounding" else ""
    )
    if name != "default" and root_search == "gemini-grounding" and web.get("search_backend") != "gemini-grounding":
        sync_needed = True
        report.append("  SUGGEST: set web.search_backend='gemini-grounding'")

    if missing_defaults:
        preview = ", ".join(missing_defaults[:20])
        suffix = "" if len(missing_defaults) <= 20 else f" ... (+{len(missing_defaults) - 20} more)"
        report.append(f"  NOTICE: missing default config fields: {preview}{suffix}")

text = "\n".join(report) + "\n"
(backup_dir / "config-audit.txt").write_text(text)
(backup_dir / "config-sync-needed").write_text("yes\n" if sync_needed else "no\n")
print(text)
print(f"CONFIG_SYNC_NEEDED={'yes' if sync_needed else 'no'}")
PY
REMOTE
  CONFIG_SYNC_NEEDED="$(ssh_cmd "cat '$REMOTE_BACKUP_DIR/config-sync-needed' 2>/dev/null || printf no")"
  export CONFIG_SYNC_NEEDED
}

remote_apply_gemini_sync() {
  if [ "$SKIP_CONFIG_SYNC" = true ]; then
    info "Skipping config sync by request"
    return 0
  fi

  if [ "${CONFIG_SYNC_NEEDED:-no}" != "yes" ]; then
    ok "No Gemini profile config sync needed"
    return 0
  fi

  confirm "Apply suggested Gemini grounding env/config sync to all profiles?" "yes" \
    || { warn "Config sync skipped; deployment will continue with current remote config."; return 0; }

  remote_bash "$REMOTE_INSTALL_DIR" "$REMOTE_HERMES_HOME" "$REMOTE_CONFIG_BACKUP_ROOT" <<'REMOTE'
set -Eeuo pipefail
install_dir="$1"
hermes_home="$2"
config_backup_root="$3"
py="$install_dir/.deploy-venv-next/bin/python"
[ -x "$py" ] || py="$install_dir/venv/bin/python"

"$py" - "$hermes_home" "$config_backup_root" <<'PY'
from pathlib import Path
import shutil
import sys
import yaml

hermes_home = Path(sys.argv[1])
backup_root = Path(sys.argv[2])
backup = backup_root / ("gemini-grounding-" + __import__("datetime").datetime.utcnow().strftime("%Y%m%d-%H%M%S"))
backup.mkdir(parents=True, exist_ok=True)

keys = ["GEMINI_GROUNDING_API_KEY", "GEMINI_GROUNDING_MODEL", "GEMINI_GROUNDING_BASE_URL"]

def parse_env(path: Path) -> dict[str, str]:
    out = {}
    if not path.exists():
        return out
    for raw in path.read_text(errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        if k.strip() in keys:
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out

def backup_file(path: Path, rel: str) -> None:
    if path.exists():
        dest = backup / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, dest)

def upsert_env(path: Path, updates: dict[str, str]) -> None:
    lines = path.read_text(errors="replace").splitlines() if path.exists() else []
    seen = set()
    out = []
    for raw in lines:
        stripped = raw.strip()
        prefix = ""
        line = stripped
        if line.startswith("export "):
            prefix = "export "
            line = line[len("export "):].strip()
        if "=" in line:
            k = line.split("=", 1)[0].strip()
            if k in updates:
                out.append(f"{prefix}{k}={updates[k]}")
                seen.add(k)
                continue
        out.append(raw)
    if out and out[-1].strip():
        out.append("")
    for k, v in updates.items():
        if k not in seen and v:
            out.append(f"{k}={v}")
    path.write_text("\n".join(out).rstrip() + "\n")
    path.chmod(0o600)

def update_config(path: Path) -> None:
    cfg = {}
    if path.exists():
        cfg = yaml.safe_load(path.read_text(errors="replace")) or {}
        if not isinstance(cfg, dict):
            raise SystemExit(f"{path}: config root is not a mapping")
    web = cfg.get("web")
    if web is None:
        web = {}
        cfg["web"] = web
    if not isinstance(web, dict):
        raise SystemExit(f"{path}: web section is not a mapping")
    web["backend"] = ""
    web["search_backend"] = "gemini-grounding"
    if web.get("extract_backend") == "gemini-grounding":
        web["extract_backend"] = ""
    else:
        web.setdefault("extract_backend", "")
    path.write_text(yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True))
    path.chmod(0o600)

source = {k: v for k, v in parse_env(hermes_home / ".env").items() if v}
if not source.get("GEMINI_GROUNDING_API_KEY"):
    raise SystemExit(f"{hermes_home}/.env has no GEMINI_GROUNDING_API_KEY to sync")

homes = [hermes_home] + sorted((hermes_home / "profiles").glob("*"))
for home in homes:
    if not home.is_dir():
        continue
    relbase = "default" if home == hermes_home else f"profiles/{home.name}"
    backup_file(home / ".env", f"{relbase}/.env")
    backup_file(home / "config.yaml", f"{relbase}/config.yaml")
    if home != hermes_home:
        upsert_env(home / ".env", source)
    update_config(home / "config.yaml")

print(f"CONFIG_BACKUP_DIR={backup}")
PY
REMOTE
}

remote_switch_and_restart() {
  log ""
  warn "All gateway services share $REMOTE_INSTALL_DIR/venv."
  warn "The final switch is a short maintenance window, not a true rolling deploy."
  confirm "Switch venv/code now and restart gateway services?" "no" \
    || die "deployment cancelled before production switch"

  remote_bash "$REMOTE_INSTALL_DIR" "$DEPLOY_SHA" "$REMOTE_BRANCH" "$REMOTE_BACKUP_DIR" <<'REMOTE'
set -Eeuo pipefail
install_dir="$1"
deploy_sha="$2"
remote_branch="$3"
backup_dir="$4"

cd "$install_dir"
uv_bin="$(command -v uv || true)"
if [ -z "$uv_bin" ] && [ -x /root/.local/bin/uv ]; then
  uv_bin=/root/.local/bin/uv
fi
[ -n "$uv_bin" ] || { echo "uv missing"; exit 1; }
old_sha="$(cat "$backup_dir/old-head.txt")"
old_venv=""
switch_started=false
services=()
preferred=(
  hermes-gateway.service
  hermes-gateway-auto.service
  hermes-gateway-coder.service
  hermes-gateway-sec.service
  hermes-gateway-product.service
  hermes-gateway-economist.service
  hermes-gateway-zzhservant.service
)

service_exists() {
  systemctl list-unit-files "$1" >/dev/null 2>&1 || systemctl status "$1" >/dev/null 2>&1
}

for svc in "${preferred[@]}"; do
  if service_exists "$svc"; then
    services+=("$svc")
  fi
done
while IFS= read -r svc; do
  [ -n "$svc" ] || continue
  found=false
  for known in "${services[@]}"; do
    [ "$known" = "$svc" ] && found=true
  done
  [ "$found" = true ] || services+=("$svc")
done < <(systemctl list-units --type=service --all 'hermes-gateway*.service' --no-legend --no-pager | awk '{print $1}')

rollback() {
  code=$?
  if [ "$switch_started" != true ]; then
    exit "$code"
  fi
  set +e
  echo "ERROR: production switch failed; rolling back to $old_sha" >&2
  for svc in "${services[@]}"; do
    systemctl stop "$svc"
  done
  if [ -n "$old_venv" ] && [ -d "$old_venv" ]; then
    rm -rf "$install_dir/venv"
    mv "$old_venv" "$install_dir/venv"
  fi
  git checkout "$old_sha"
  if [ -d "$backup_dir/hermes-home" ]; then
    [ -f "$backup_dir/hermes-home/.env" ] && cp -a "$backup_dir/hermes-home/.env" /root/.hermes/.env
    [ -f "$backup_dir/hermes-home/config.yaml" ] && cp -a "$backup_dir/hermes-home/config.yaml" /root/.hermes/config.yaml
    if [ -d "$backup_dir/hermes-home/profiles" ]; then
      for p in "$backup_dir"/hermes-home/profiles/*; do
        [ -d "$p" ] || continue
        name="$(basename "$p")"
        mkdir -p "/root/.hermes/profiles/$name"
        [ -f "$p/.env" ] && cp -a "$p/.env" "/root/.hermes/profiles/$name/.env"
        [ -f "$p/config.yaml" ] && cp -a "$p/config.yaml" "/root/.hermes/profiles/$name/config.yaml"
      done
    fi
  fi
  for svc in "${services[@]}"; do
    systemctl start "$svc"
  done
  systemctl list-units --type=service --all 'hermes-gateway*.service' --no-pager
  exit "$code"
}
trap rollback ERR

test "$(git rev-parse HEAD)" = "$deploy_sha"
test -d .deploy-venv-next

switch_ts="$(date -u +%Y%m%d-%H%M%S)"
printf 'switch_ts=%s\n' "$switch_ts" | tee "$backup_dir/switch-info.txt"
printf '%s\n' "${services[@]}" > "$backup_dir/services-restarted.txt"

switch_started=true
for svc in "${services[@]}"; do
  systemctl stop "$svc"
done
systemctl list-units --type=service --all 'hermes-gateway*.service' --no-pager > "$backup_dir/services-after-stop.txt" || true

if [ -d venv ]; then
  old_venv="$install_dir/venv.backup.$switch_ts"
  mv venv "$old_venv"
  printf '%s\n' "$old_venv" > "$backup_dir/old-venv-path.txt"
fi
mv .deploy-venv-next venv

# uv-created entry point scripts contain the venv path in their shebangs.
# Regenerate them after moving .deploy-venv-next into its final path.
VIRTUAL_ENV="$install_dir/venv" "$uv_bin" pip install -e ".[all]"
"$install_dir/venv/bin/hermes" version

for svc in "${services[@]}"; do
  echo "START $svc"
  systemctl start "$svc"
  sleep 7
  state="$(systemctl is-active "$svc" || true)"
  pid="$(systemctl show -p ExecMainPID --value "$svc" || true)"
  echo "$svc state=$state pid=$pid"
  journalctl -u "$svc" -n 80 --no-pager > "$backup_dir/journal-restart-$svc.txt" || true
  [ "$state" = "active" ]
done

sleep 15
for svc in "${services[@]}"; do
  state="$(systemctl is-active "$svc" || true)"
  pid="$(systemctl show -p ExecMainPID --value "$svc" || true)"
  echo "$svc delayed_state=$state pid=$pid"
  [ "$state" = "active" ]
done

systemctl list-units --type=service --all 'hermes-gateway*.service' --no-pager
ps -ef | grep '[h]ermes_cli.main' || true
REMOTE
}

remote_final_verify() {
  info "Final remote verification"
  remote_bash "$REMOTE_INSTALL_DIR" "$DEPLOY_SHA" <<'REMOTE'
set -Eeuo pipefail
install_dir="$1"
deploy_sha="$2"

cd "$install_dir"
printf 'EXPECTED=%s\n' "$deploy_sha"
printf 'ACTUAL=%s\n' "$(git rev-parse HEAD)"
test "$(git rev-parse HEAD)" = "$deploy_sha"
git status --short --branch
/usr/local/bin/hermes version
"$install_dir/venv/bin/python" -m hermes_cli.main --help >/tmp/hermes-volce-help-check.txt
head -5 /tmp/hermes-volce-help-check.txt
rm -f /tmp/hermes-volce-help-check.txt

for svc in hermes-gateway.service hermes-gateway-auto.service hermes-gateway-coder.service hermes-gateway-sec.service hermes-gateway-product.service hermes-gateway-economist.service hermes-gateway-zzhservant.service; do
  if systemctl status "$svc" >/dev/null 2>&1; then
    state="$(systemctl is-active "$svc" || true)"
    pid="$(systemctl show -p ExecMainPID --value "$svc" || true)"
    printf '%s state=%s pid=%s\n' "$svc" "$state" "$pid"
  fi
done
REMOTE
}

config_audit_only() {
  remote_preflight
  REMOTE_BACKUP_DIR="$(ssh_cmd 'mktemp -d /tmp/hermes-volce-config-audit.XXXXXX')"
  export REMOTE_BACKUP_DIR
  remote_config_audit
}

deploy() {
  require_local_tools
  ensure_local_commit
  create_bundle
  remote_preflight

  log ""
  log "Deployment summary:"
  log "  local branch:  $DEPLOY_BRANCH"
  log "  deploy sha:    $DEPLOY_SHA"
  log "  remote target: $SSH_TARGET"
  log "  install dir:   $REMOTE_INSTALL_DIR"
  log "  hermes home:   $REMOTE_HERMES_HOME"
  log ""

  confirm "Proceed with upload and remote backup?" "yes" \
    || die "deployment cancelled"

  upload_bundle
  remote_backup
  remote_fetch_and_compare

  log ""
  log "Upgrade comparison saved on remote:"
  log "  $REMOTE_BACKUP_DIR/upgrade-comparison.txt"
  log ""

  confirm "The comparison above is the code upgrade. Continue?" "no" \
    || die "deployment cancelled after comparison"

  remote_checkout_and_build
  remote_config_audit
  remote_apply_gemini_sync
  remote_switch_and_restart
  remote_final_verify

  ok "Volce deployment complete"
  log "Remote backup: $REMOTE_BACKUP_DIR"
  log "Local deploy commit: $DEPLOY_SHA"
}

main() {
  parse_args "$@"
  case "$COMMAND" in
    deploy)
      deploy
      ;;
    status)
      require_local_tools
      remote_status
      ;;
    config-audit)
      require_local_tools
      config_audit_only
      ;;
    *)
      die "unknown command: $COMMAND"
      ;;
  esac
}

main "$@"
