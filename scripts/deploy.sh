#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: scripts/deploy.sh [options]

Options:
  --commit, -m MESSAGE   Commit dirty local changes before deploy
  --remote-host HOST     Override remote host, default: linus@192.168.1.5
  --skip-local           Skip local launchd migration/restart
  --skip-remote          Skip remote Ubuntu pull/restart
  --skip-push            Skip git push
  --dry-run              Print actions without executing them
  --help, -h             Show this help

Environment:
  PLEXUS_REMOTE_PASSWORD Optional SSH password for non-interactive remote deploy
EOF
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

REMOTE_HOST="${PLEXUS_REMOTE_HOST:-linus@192.168.1.5}"
REMOTE_DIR="${PLEXUS_REMOTE_DIR:-/home/linus/workspace/tools/plexus}"
REMOTE_SERVICE="${PLEXUS_REMOTE_SERVICE:-plexus.service}"
REMOTE_BRANCH="${PLEXUS_REMOTE_BRANCH:-}"
REMOTE_CONFIG_DIR="${PLEXUS_REMOTE_CONFIG_DIR:-/home/linus/.config/plexus}"
LEGACY_REMOTE_CONFIG_DIR="${PLEXUS_LEGACY_REMOTE_CONFIG_DIR:-/home/linus/.config/codex-notify}"
REMOTE_STATE_DIR="${PLEXUS_REMOTE_STATE_DIR:-/home/linus/.local/state/plexus}"
LEGACY_REMOTE_STATE_DIR="${PLEXUS_LEGACY_REMOTE_STATE_DIR:-/home/linus/.local/state/codex-notify}"
LOCAL_LABEL="${PLEXUS_LOCAL_LABEL:-com.linus.plexus}"
LEGACY_LOCAL_LABEL="${PLEXUS_LEGACY_LOCAL_LABEL:-com.linus.codex-notify}"
LOCAL_PLIST="${PLEXUS_LOCAL_PLIST:-$HOME/Library/LaunchAgents/${LOCAL_LABEL}.plist}"
LEGACY_LOCAL_PLIST="${PLEXUS_LEGACY_LOCAL_PLIST:-$HOME/Library/LaunchAgents/${LEGACY_LOCAL_LABEL}.plist}"
LOCAL_CONFIG_DIR="${PLEXUS_LOCAL_CONFIG_DIR:-$HOME/.config/plexus}"
LEGACY_LOCAL_CONFIG_DIR="${PLEXUS_LEGACY_LOCAL_CONFIG_DIR:-$HOME/.config/codex-notify}"
LOCAL_STATE_DIR="${PLEXUS_LOCAL_STATE_DIR:-$HOME/.local/state/plexus}"
LEGACY_LOCAL_STATE_DIR="${PLEXUS_LEGACY_LOCAL_STATE_DIR:-$HOME/.local/state/codex-notify}"
LOCAL_PYTHON="${PLEXUS_LOCAL_PYTHON:-/usr/bin/python3}"
LOCAL_ICON_URL="${PLEXUS_LOCAL_ICON_URL:-https://raw.githubusercontent.com/caozhenxiong/muxdeck-assets/baf0a535eb67c5f56a764971c49984fd155efda0/assets/muxdeck-bark-icon.png}"
GIT_REMOTE="${PLEXUS_GIT_REMOTE:-origin}"

COMMIT_MESSAGE=""
SKIP_LOCAL=0
SKIP_REMOTE=0
SKIP_PUSH=0
DRY_RUN=0

while (($#)); do
  case "$1" in
    --commit|-m)
      [[ $# -ge 2 ]] || { echo "missing value for $1" >&2; exit 1; }
      COMMIT_MESSAGE="$2"
      shift 2
      ;;
    --remote-host)
      [[ $# -ge 2 ]] || { echo "missing value for $1" >&2; exit 1; }
      REMOTE_HOST="$2"
      shift 2
      ;;
    --skip-local)
      SKIP_LOCAL=1
      shift
      ;;
    --skip-remote)
      SKIP_REMOTE=1
      shift
      ;;
    --skip-push)
      SKIP_PUSH=1
      shift
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      echo "unknown option: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

log() {
  printf '[plexus-deploy] %s\n' "$*"
}

run() {
  log "+ $*"
  if ((DRY_RUN)); then
    return 0
  fi
  "$@"
}

run_bash() {
  log "+ bash -lc $1"
  if ((DRY_RUN)); then
    return 0
  fi
  bash -lc "$1"
}

fail() {
  printf '[plexus-deploy] error: %s\n' "$*" >&2
  exit 1
}

require_repo() {
  git -C "$REPO_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1 || fail "not inside a git repository: $REPO_DIR"
}

current_branch() {
  git -C "$REPO_DIR" rev-parse --abbrev-ref HEAD
}

working_tree_dirty() {
  [[ -n "$(git -C "$REPO_DIR" status --porcelain)" ]]
}

verify_python() {
  run_bash "PYTHONPYCACHEPREFIX=/tmp/plexus-pyc python3 -m py_compile '$REPO_DIR/watch_sessions.py'"
}

migrate_local_config() {
  run mkdir -p "$LOCAL_CONFIG_DIR" "$LOCAL_STATE_DIR" "$(dirname "$LOCAL_PLIST")"

  if [[ -f "$LEGACY_LOCAL_CONFIG_DIR/config.toml" && ! -f "$LOCAL_CONFIG_DIR/config.toml" ]]; then
    run cp "$LEGACY_LOCAL_CONFIG_DIR/config.toml" "$LOCAL_CONFIG_DIR/config.toml"
  fi

  if [[ -f "$LEGACY_LOCAL_STATE_DIR/state.json" && ! -f "$LOCAL_STATE_DIR/state.json" ]]; then
    run cp "$LEGACY_LOCAL_STATE_DIR/state.json" "$LOCAL_STATE_DIR/state.json"
  fi

  if [[ ! -f "$LOCAL_CONFIG_DIR/config.toml" ]]; then
    log "local config not found at $LOCAL_CONFIG_DIR/config.toml; skipping config migration"
    return 0
  fi

  run_bash "python3 - '$LOCAL_CONFIG_DIR/config.toml' '$LOCAL_ICON_URL' <<'PY'
from pathlib import Path
import re
import sys

path = Path(sys.argv[1])
icon_url = sys.argv[2]
text = path.read_text(encoding='utf-8')
text = text.replace('.config/codex-notify/', '.config/plexus/')
text = text.replace('.local/state/codex-notify/', '.local/state/plexus/')
text = re.sub(r'^notification_group\\s*=\\s*\".*\"$', 'notification_group = \"plexus\"', text, flags=re.M)
if re.search(r'^notification_icon\\s*=\\s*\".*\"$', text, flags=re.M):
    text = re.sub(r'^notification_icon\\s*=\\s*\".*\"$', f'notification_icon = \"{icon_url}\"', text, flags=re.M)
else:
    suffix = '' if text.endswith('\\n') else '\\n'
    text = f'{text}{suffix}notification_icon = \"{icon_url}\"\\n'
path.write_text(text, encoding='utf-8')
PY"
}

write_local_plist() {
  local stdout_path="$LOCAL_STATE_DIR/launchd.stdout.log"
  local stderr_path="$LOCAL_STATE_DIR/launchd.stderr.log"

  if ((DRY_RUN)); then
    log "+ write $LOCAL_PLIST"
    return 0
  fi

  cat >"$LOCAL_PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>${LOCAL_LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>${LOCAL_PYTHON}</string>
    <string>${REPO_DIR}/watch_sessions.py</string>
    <string>--config</string>
    <string>${LOCAL_CONFIG_DIR}/config.toml</string>
  </array>
  <key>WorkingDirectory</key>
  <string>${REPO_DIR}</string>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>${stdout_path}</string>
  <key>StandardErrorPath</key>
  <string>${stderr_path}</string>
</dict>
</plist>
EOF
}

restart_local_launch_agent() {
  local uid backup_path
  uid="$(id -u)"
  backup_path="${LEGACY_LOCAL_PLIST}.backup-$(date +%Y%m%d-%H%M%S)"

  migrate_local_config
  write_local_plist

  run_bash "launchctl bootout 'gui/${uid}' '$LEGACY_LOCAL_PLIST' >/dev/null 2>&1 || true"
  if [[ -f "$LEGACY_LOCAL_PLIST" ]]; then
    run mv "$LEGACY_LOCAL_PLIST" "$backup_path"
  fi

  run_bash "launchctl bootout 'gui/${uid}' '$LOCAL_PLIST' >/dev/null 2>&1 || true"
  run launchctl bootstrap "gui/${uid}" "$LOCAL_PLIST"
  run_bash "launchctl enable 'gui/${uid}/${LOCAL_LABEL}' >/dev/null 2>&1 || true"
  run launchctl kickstart -k "gui/${uid}/${LOCAL_LABEL}"
  run_bash "launchctl print 'gui/${uid}/${LOCAL_LABEL}' | sed -n '1,20p'"
}

run_expect_copy() {
  local source_file="$1"
  local target="$2"
  expect <<EOF
set timeout -1
spawn scp -o StrictHostKeyChecking=accept-new "$source_file" "${REMOTE_HOST}:${target}"
expect {
  "*yes/no*" { send "yes\r"; exp_continue }
  "*assword:*" { send "$PLEXUS_REMOTE_PASSWORD\r"; exp_continue }
  eof
}
EOF
}

run_expect_remote() {
  local remote_command="$1"
  expect <<EOF
set timeout -1
spawn ssh -o StrictHostKeyChecking=accept-new "$REMOTE_HOST" "$remote_command"
expect {
  "*yes/no*" { send "yes\r"; exp_continue }
  "*assword:*" { send "$PLEXUS_REMOTE_PASSWORD\r"; exp_continue }
  eof
}
EOF
}

restart_remote_service() {
  local branch repo_url remote_tmp remote_script
  branch="${REMOTE_BRANCH:-$(current_branch)}"
  repo_url="$(git -C "$REPO_DIR" remote get-url "$GIT_REMOTE")"
  remote_tmp="/tmp/plexus-deploy-$$.sh"
  remote_script="$(mktemp -t plexus-deploy)"

  cat >"$remote_script" <<EOF
set -euo pipefail
REPO_DIR=$(printf '%q' "$REMOTE_DIR")
REPO_URL=$(printf '%q' "$repo_url")
BRANCH=$(printf '%q' "$branch")
SERVICE=$(printf '%q' "$REMOTE_SERVICE")
CONFIG_DIR=$(printf '%q' "$REMOTE_CONFIG_DIR")
LEGACY_CONFIG_DIR=$(printf '%q' "$LEGACY_REMOTE_CONFIG_DIR")
STATE_DIR=$(printf '%q' "$REMOTE_STATE_DIR")
LEGACY_STATE_DIR=$(printf '%q' "$LEGACY_REMOTE_STATE_DIR")
ICON_URL=$(printf '%q' "$LOCAL_ICON_URL")

mkdir -p "\$(dirname "\$REPO_DIR")"

if [ -d "\$REPO_DIR/.git" ]; then
  git -C "\$REPO_DIR" fetch --all --prune
  git -C "\$REPO_DIR" checkout "\$BRANCH"
  git -C "\$REPO_DIR" pull --ff-only origin "\$BRANCH"
else
  rm -rf "\$REPO_DIR"
  git clone --branch "\$BRANCH" "\$REPO_URL" "\$REPO_DIR"
fi

mkdir -p "\$CONFIG_DIR" "\$STATE_DIR"

if [ -f "\$LEGACY_CONFIG_DIR/config.toml" ] && [ ! -f "\$CONFIG_DIR/config.toml" ]; then
  cp "\$LEGACY_CONFIG_DIR/config.toml" "\$CONFIG_DIR/config.toml"
fi

if [ -f "\$LEGACY_STATE_DIR/state.json" ] && [ ! -f "\$STATE_DIR/state.json" ]; then
  cp "\$LEGACY_STATE_DIR/state.json" "\$STATE_DIR/state.json"
fi

if [ -f "\$CONFIG_DIR/config.toml" ]; then
  python3 - "\$CONFIG_DIR/config.toml" "\$ICON_URL" <<'PY'
from pathlib import Path
import re
import sys

path = Path(sys.argv[1])
icon_url = sys.argv[2]
text = path.read_text(encoding="utf-8")
text = text.replace(".config/codex-notify/", ".config/plexus/")
text = text.replace(".local/state/codex-notify/", ".local/state/plexus/")
text = re.sub(r'^notification_group\s*=\s*".*"$', 'notification_group = "plexus"', text, flags=re.M)
if re.search(r'^notification_icon\s*=\s*".*"$', text, flags=re.M):
    text = re.sub(r'^notification_icon\s*=\s*".*"$', f'notification_icon = "{icon_url}"', text, flags=re.M)
else:
    suffix = "" if text.endswith("\n") else "\n"
    text = f'{text}{suffix}notification_icon = "{icon_url}"\n'
path.write_text(text, encoding="utf-8")
PY
else
  echo "__REMOTE_CONFIG__"
  echo "missing"
fi

systemctl --user daemon-reload
systemctl --user restart "\$SERVICE"
sleep 2
echo __REMOTE_GIT__
git -C "\$REPO_DIR" rev-parse --short HEAD
echo __REMOTE_SERVICE__
systemctl --user is-active "\$SERVICE"
EOF

  if ((DRY_RUN)); then
    log "+ remote deploy to $REMOTE_HOST ($REMOTE_DIR, branch $branch)"
    rm -f "$remote_script"
    return 0
  fi

  if [[ -n "${PLEXUS_REMOTE_PASSWORD:-}" ]]; then
    command -v expect >/dev/null 2>&1 || fail "PLEXUS_REMOTE_PASSWORD is set but expect is not installed"
    run_expect_copy "$remote_script" "$remote_tmp"
    run_expect_remote "bash $remote_tmp; rm -f $remote_tmp"
  else
    run scp -o StrictHostKeyChecking=accept-new "$remote_script" "${REMOTE_HOST}:${remote_tmp}"
    run ssh -o StrictHostKeyChecking=accept-new "$REMOTE_HOST" "bash $remote_tmp; rm -f $remote_tmp"
  fi

  rm -f "$remote_script"
}

main() {
  local branch
  require_repo
  branch="$(current_branch)"
  log "deploying branch $branch from $REPO_DIR"

  verify_python

  if working_tree_dirty; then
    [[ -n "$COMMIT_MESSAGE" ]] || fail "working tree is dirty; re-run with --commit \"message\" or commit manually first"
    run git -C "$REPO_DIR" add -A
    run git -C "$REPO_DIR" commit -m "$COMMIT_MESSAGE"
  fi

  if (( !SKIP_PUSH )); then
    run git -C "$REPO_DIR" push "$GIT_REMOTE" "$branch"
  fi

  if (( !SKIP_LOCAL )); then
    restart_local_launch_agent
  fi

  if (( !SKIP_REMOTE )); then
    restart_remote_service
  fi

  log "done"
}

main "$@"
