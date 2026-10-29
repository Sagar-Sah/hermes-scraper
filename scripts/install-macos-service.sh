#!/usr/bin/env bash
# Opt-in per-user service. This script does not install anything unless --install
# is explicitly supplied, and never changes login, sleep or security settings.
set -euo pipefail
umask 077

project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
service_label="com.hermes.product-companion"
service_plist="$HOME/Library/LaunchAgents/$service_label.plist"
service_logs="$HOME/Library/Logs/Hermes"
service_domain="gui/$(id -u)"
service_port="${ATLAS_PORT:-18877}"
action="${1:---help}"
if [[ $# -gt 1 ]]; then
  echo "Choose exactly one service action. Use --help for usage." >&2
  exit 2
fi
if [[ ! "$service_port" =~ ^[0-9]{1,5}$ ]]; then
  echo "ATLAS_PORT must be a number from 1 through 65535." >&2
  exit 2
fi
service_port="$((10#$service_port))"
if ((service_port < 1 || service_port > 65535)); then
  echo "ATLAS_PORT must be a number from 1 through 65535." >&2
  exit 2
fi

usage() {
  cat <<'EOF'
Usage: ./scripts/install-macos-service.sh ACTION

  --dry-run       Print the service plan; do not write files or start Hermes.
  --print-plist   Print the launchd definition; do not install it.
  --install       Install and start a per-user macOS LaunchAgent.
  --status        Read this user's launchd service state.
  --uninstall     Stop this service and remove only its matching LaunchAgent.

Configure the existing project .env before installing. Data and supplier
profiles stay in their configured locations. The default browser limit is four;
this installer does not override a saved limit or automatically start audits.
Normal macOS sign-in is required after reboot. Automatic login is never enabled.
EOF
}

render_plist() {
  python3 - "$project_dir" "$service_label" "$service_logs" "$service_port" <<'PY'
import os, plistlib, sys
project, label, logs, port = sys.argv[1:]
value = {
    "Label": label,
    "ProgramArguments": ["/bin/bash", os.path.join(project, "scripts", "start-local.sh")],
    "WorkingDirectory": project,
    "RunAtLoad": True,
    "KeepAlive": {"SuccessfulExit": False},
    "ThrottleInterval": 30,
    "ExitTimeOut": 30,
    "LimitLoadToSessionType": ["Aqua"],
    "EnvironmentVariables": {
        "ATLAS_PORT": port,
        "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
    },
    "StandardOutPath": os.path.join(logs, "service.stdout.log"),
    "StandardErrorPath": os.path.join(logs, "service.stderr.log"),
}
sys.stdout.buffer.write(plistlib.dumps(value, sort_keys=False))
PY
}

require_macos() {
  if [[ "$(uname -s)" != "Darwin" ]] || ! command -v launchctl >/dev/null; then
    echo "Installation and service control require a signed-in macOS desktop user." >&2
    exit 2
  fi
  if [[ -L "$service_logs" || -L "$(dirname "$service_plist")" ]]; then
    echo "Refusing a symlink for the Hermes service or log directory." >&2
    exit 2
  fi
}

check_existing_plist() {
  if [[ -L "$service_plist" ]]; then
    echo "Refusing a symlink at the Hermes LaunchAgent path." >&2
    exit 2
  fi
  if [[ -e "$service_plist" ]]; then
    python3 - "$service_plist" "$project_dir" "$service_label" <<'PY'
import os, plistlib, stat, sys
path, project, label = sys.argv[1:]
try:
    info = os.stat(path)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError("the service definition is not owned by this user")
    with open(path, "rb") as source:
        value = plistlib.load(source)
    if (value.get("Label") != label or value.get("WorkingDirectory") != project
            or value.get("ProgramArguments") != ["/bin/bash", project + "/scripts/start-local.sh"]):
        raise ValueError("a different project owns this service")
except Exception:
    sys.exit("The existing Hermes service does not match this checkout. Uninstall it from its owning checkout first.")
PY
  fi
}

case "$action" in
  --help|-h) usage ;;
  --dry-run)
    python3 - "$project_dir" "$service_label" "$service_plist" "$service_logs" "$service_port" <<'PY'
import json, sys
project, label, plist, logs, port = sys.argv[1:]
print(json.dumps({"action": "dry_run", "writes": False, "starts_service": False,
    "label": label, "project": project, "plist": plist, "logs": logs,
    "port": int(port), "launcher": "scripts/start-local.sh",
    "browser_limit": "existing .env value, otherwise 4",
    "data_and_profiles": "existing .env locations are preserved",
    "requires_normal_desktop_login": True, "enables_automatic_login": False,
    "starts_audit_schedule": False}, indent=2))
PY
    ;;
  --print-plist) render_plist ;;
  --status)
    require_macos
    launchctl print "$service_domain/$service_label"
    ;;
  --install)
    require_macos
    check_existing_plist
    if launchctl print "$service_domain/$service_label" >/dev/null 2>&1; then
      echo "Hermes is already registered. Use --status to inspect it; no service was replaced."
      exit 0
    fi
    # Do not attach to or terminate another runtime, or create a rapid launchd
    # retry loop because start-local correctly declines an occupied port.
    python3 - "$service_port" <<'PY'
import socket, sys
try:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", int(sys.argv[1])))
except OSError:
    sys.exit("The Hermes port is already in use. Pause and stop the existing Hermes runtime before installing its service. No running process was changed.")
PY
    if [[ ! -f "$project_dir/.env" ]]; then
      echo "Configure the project .env first; this installer will not create or replace it." >&2
      exit 2
    fi
    if [[ -f "$service_plist" ]]; then
      launchctl bootstrap "$service_domain" "$service_plist"
      echo "The matching saved Hermes LaunchAgent was started without rewriting its settings."
      exit 0
    fi
    mkdir -p "$(dirname "$service_plist")" "$service_logs"
    chmod 700 "$service_logs"
    staged_plist="$(mktemp "$(dirname "$service_plist")/.hermes-service.XXXXXX")"
    trap 'rm -f "$staged_plist"' EXIT
    render_plist > "$staged_plist"
    plutil -lint "$staged_plist" >/dev/null
    chmod 600 "$staged_plist"
    mv "$staged_plist" "$service_plist"
    trap - EXIT
    launchctl bootstrap "$service_domain" "$service_plist"
    echo "Hermes LaunchAgent registered. The supervised launcher is preparing or starting the service."
    echo "Check --status, the dashboard health/build, and $service_logs before enabling monthly audits."
    ;;
  --uninstall)
    require_macos
    check_existing_plist
    if [[ ! -f "$service_plist" ]]; then
      echo "No matching Hermes LaunchAgent is installed. Data and supplier profiles are unchanged."
      exit 0
    fi
    if launchctl print "$service_domain/$service_label" >/dev/null 2>&1; then
      launchctl bootout "$service_domain/$service_label"
    fi
    rm "$service_plist"
    echo "Hermes LaunchAgent removed. Project, .env, data, logs and supplier profiles were preserved."
    ;;
  *) usage >&2; exit 2 ;;
esac
