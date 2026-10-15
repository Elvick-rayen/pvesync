#!/usr/bin/env bash
# install.sh - install or upgrade netbox-pve-sync on a Proxmox VE 8 or 9 node.
#
#   git clone <repo> netbox-pve-sync && cd netbox-pve-sync
#   ./install.sh --netbox-url http://192.168.1.218:8000 --cluster PVE-Cyprus
#
# Safe to run again: it upgrades the code, keeps the config and secrets, and
# brings the Proxmox role up to date (e.g. after an upgrade from PVE 8 to 9).
#
# What it changes on the host (and nothing else):
#   * apt packages python3-requests python3-yaml (Debian, only if missing)
#   * system user/group  netbox-pve-sync
#   * /opt/netbox-pve-sync, /usr/local/bin/netbox-pve-sync
#   * /etc/netbox-pve-sync (config.yaml, secrets.env, pve-root-ca.pem)
#   * /var/lib/netbox-pve-sync (state), /etc/systemd/system/netbox-pve-sync.service
#   * Proxmox (cluster-wide): role NetBoxSync, user netbox-sync@pve, an ACL on /
#     and one API token per node (netbox-sync@pve!agent-<node>). All read-only.
set -euo pipefail

APP=netbox-pve-sync
SERVICE_USER=netbox-pve-sync
PREFIX=/opt/$APP
CONF_DIR=/etc/$APP
STATE_DIR=/var/lib/$APP
UNIT=/etc/systemd/system/$APP.service
BIN=/usr/local/bin/$APP
PVE_USER=netbox-sync@pve
PVE_ROLE=NetBoxSync
SRC_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

NETBOX_URL=""
NETBOX_CLUSTER=""
START=ask          # ask | yes | no
SKIP_PVE_TOKEN=0
FORCE=0

usage() {
    cat <<EOF
Usage: ./install.sh [options]

  --netbox-url URL      NetBox URL, e.g. http://192.168.1.218:8000 (first install)
  --cluster NAME        NetBox cluster of this Proxmox environment, e.g. PVE-Cyprus
  --start               start/restart the service at the end without asking
  --no-start            do not start the service (default when not interactive)
  --skip-pve-token      do not create the Proxmox user/role/token (configure it yourself)
  --force               continue on an unsupported Proxmox version
  -h, --help            this help

The NetBox API token is asked for interactively (input hidden) or can be
put into $CONF_DIR/secrets.env afterwards. It is never passed on the command line.
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --netbox-url) NETBOX_URL=${2:?}; shift 2 ;;
        --cluster) NETBOX_CLUSTER=${2:?}; shift 2 ;;
        --start) START=yes; shift ;;
        --no-start) START=no; shift ;;
        --skip-pve-token) SKIP_PVE_TOKEN=1; shift ;;
        --force) FORCE=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
done

step() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
info() { printf '    %s\n' "$*"; }
warn() { printf '    \033[33mWARNING:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }
interactive() { [ -t 0 ] && [ -t 1 ]; }

# Set "key: value" under a top-level YAML section (only touches lines it understands).
yaml_set() {  # file section key value
    python3 - "$1" "$2" "$3" "$4" <<'PY'
import re, sys
path, section, key, value = sys.argv[1:5]
lines = open(path, encoding="utf-8").read().split("\n")
out, in_section, done = [], False, False
for line in lines:
    if re.match(r"^\S", line):
        in_section = line.rstrip() == f"{section}:"
    m = re.match(rf"^(\s+)#?\s*{re.escape(key)}:", line)
    if in_section and m and not done:
        line = f"{m.group(1)}{key}: {value}"
        done = True
    out.append(line)
if not done:
    raise SystemExit(f"could not set {section}.{key} in {path}")
open(path, "w", encoding="utf-8").write("\n".join(out))
PY
}

secret_get() { sed -n "s/^$1=//p" "$CONF_DIR/secrets.env" 2>/dev/null | head -n1 | tr -d "'\""; }
secret_set() {  # key value - never echoed
    local tmp
    tmp=$(mktemp "$CONF_DIR/.secrets.XXXXXX")
    grep -v "^$1=" "$CONF_DIR/secrets.env" > "$tmp" 2>/dev/null || true
    printf '%s=%s\n' "$1" "$2" >> "$tmp"
    chmod 0640 "$tmp"; chown root:"$SERVICE_USER" "$tmp"
    mv "$tmp" "$CONF_DIR/secrets.env"
}
is_placeholder() { [ -z "$1" ] || [ "$1" = "replace-me" ]; }

# --------------------------------------------------------------------------- checks
step "Checking the system"
[ "$(id -u)" = 0 ] || die "run as root (sudo ./install.sh)"
[ -r /etc/os-release ] || die "/etc/os-release missing"
# shellcheck disable=SC1091
. /etc/os-release
[ "${ID:-}" = "debian" ] || [ "${ID_LIKE:-}" = "debian" ] || die "Debian-based system required (found ${ID:-unknown})"
info "OS: ${PRETTY_NAME:-$ID}"

command -v pveversion >/dev/null || die "pveversion not found - this is not a Proxmox VE node"
PVE_VERSION=$(pveversion | sed -n 's#^pve-manager/\([0-9][0-9.]*\).*#\1#p')
PVE_MAJOR=${PVE_VERSION%%.*}
case "$PVE_MAJOR" in
    8|9) info "Proxmox VE $PVE_VERSION (supported)" ;;
    *) [ "$FORCE" = 1 ] || die "Proxmox VE ${PVE_VERSION:-unknown} is not supported (need 8 or 9; --force to try)"
       warn "Proxmox VE $PVE_VERSION is untested" ;;
esac
NODE=$(hostname -s)
info "Node: $NODE"

command -v python3 >/dev/null || die "python3 not found"
python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' || die "Python 3.11+ required (found $(python3 -V))"
info "$(python3 -V)"
command -v systemctl >/dev/null || die "systemd required"

# --------------------------------------------------------------------------- packages
step "Installing dependencies"
missing=()
python3 -c 'import requests' 2>/dev/null || missing+=(python3-requests)
python3 -c 'import yaml' 2>/dev/null || missing+=(python3-yaml)
if [ ${#missing[@]} -gt 0 ]; then
    info "apt-get install ${missing[*]}"
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "${missing[@]}" >/dev/null \
        || die "could not install ${missing[*]} (run 'apt-get update' first?)"
else
    info "python3-requests and python3-yaml already present"
fi

# --------------------------------------------------------------------------- user + files
step "Installing the agent"
if ! getent group "$SERVICE_USER" >/dev/null; then groupadd --system "$SERVICE_USER"; fi
if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
    useradd --system --gid "$SERVICE_USER" --home-dir "$STATE_DIR" --no-create-home \
        --shell /usr/sbin/nologin --comment "netbox-pve-sync agent" "$SERVICE_USER"
    info "created system user $SERVICE_USER"
fi

[ -d "$SRC_DIR/src/netbox_pve_sync" ] || die "run install.sh from the project directory"
install -d -m 0755 "$PREFIX" "$PREFIX/lib"
rm -rf "$PREFIX/lib/netbox_pve_sync.new"
cp -r "$SRC_DIR/src/netbox_pve_sync" "$PREFIX/lib/netbox_pve_sync.new"
find "$PREFIX/lib/netbox_pve_sync.new" -name '__pycache__' -type d -prune -exec rm -rf {} +
rm -rf "$PREFIX/lib/netbox_pve_sync"
mv "$PREFIX/lib/netbox_pve_sync.new" "$PREFIX/lib/netbox_pve_sync"
chown -R root:root "$PREFIX"; chmod -R u=rwX,go=rX "$PREFIX"
for f in README.md LICENSE config.example.yaml .env.example uninstall.sh; do
    [ -f "$SRC_DIR/$f" ] && install -m 0644 "$SRC_DIR/$f" "$PREFIX/$f"
done
[ -d "$SRC_DIR/docs" ] && { rm -rf "$PREFIX/docs"; cp -r "$SRC_DIR/docs" "$PREFIX/docs"; chmod -R go=rX "$PREFIX/docs"; }
chmod 0755 "$PREFIX/uninstall.sh" 2>/dev/null || true
install -m 0755 "$SRC_DIR/packaging/netbox-pve-sync.sh" "$BIN"
VERSION=$(PYTHONPATH="$PREFIX/lib" python3 -c 'import netbox_pve_sync; print(netbox_pve_sync.__version__)')
info "netbox-pve-sync $VERSION -> $PREFIX"

install -d -m 0750 -o root -g "$SERVICE_USER" "$CONF_DIR"
install -d -m 0750 -o "$SERVICE_USER" -g "$SERVICE_USER" "$STATE_DIR"
FIRST_INSTALL=0
if [ ! -f "$CONF_DIR/config.yaml" ]; then
    FIRST_INSTALL=1
    install -m 0640 -o root -g "$SERVICE_USER" "$SRC_DIR/config.example.yaml" "$CONF_DIR/config.yaml"
    info "created $CONF_DIR/config.yaml"
fi
if [ ! -f "$CONF_DIR/secrets.env" ]; then
    install -m 0640 -o root -g "$SERVICE_USER" "$SRC_DIR/.env.example" "$CONF_DIR/secrets.env"
fi
chown root:"$SERVICE_USER" "$CONF_DIR/config.yaml" "$CONF_DIR/secrets.env"
chmod 0640 "$CONF_DIR/config.yaml" "$CONF_DIR/secrets.env"

if [ -n "$NETBOX_URL" ]; then yaml_set "$CONF_DIR/config.yaml" netbox url "\"$NETBOX_URL\""; fi
if [ -n "$NETBOX_CLUSTER" ]; then yaml_set "$CONF_DIR/config.yaml" netbox cluster "\"$NETBOX_CLUSTER\""; fi
if [ "$FIRST_INSTALL" = 1 ] && interactive; then
    if [ -z "$NETBOX_URL" ]; then
        read -r -p "    NetBox URL [http://192.168.1.218:8000]: " NETBOX_URL
        yaml_set "$CONF_DIR/config.yaml" netbox url "\"${NETBOX_URL:-http://192.168.1.218:8000}\""
    fi
    if [ -z "$NETBOX_CLUSTER" ]; then
        read -r -p "    NetBox cluster name for this Proxmox environment (must exist in NetBox): " NETBOX_CLUSTER
        [ -n "$NETBOX_CLUSTER" ] && yaml_set "$CONF_DIR/config.yaml" netbox cluster "\"$NETBOX_CLUSTER\""
    fi
fi

# --------------------------------------------------------------------------- NetBox token
if is_placeholder "$(secret_get NETBOX_API_TOKEN)"; then
    if interactive; then
        read -r -s -p "    NetBox API token (input hidden, Enter to skip): " NB_TOKEN; echo
        if [ -n "$NB_TOKEN" ]; then secret_set NETBOX_API_TOKEN "$NB_TOKEN"; info "NetBox token saved"; fi
        unset NB_TOKEN
    else
        warn "NetBox token not set: edit NETBOX_API_TOKEN in $CONF_DIR/secrets.env"
    fi
fi

# --------------------------------------------------------------------------- Proxmox token
if [ "$SKIP_PVE_TOKEN" = 0 ]; then
    step "Configuring read-only Proxmox API access"
    if [ "$PVE_MAJOR" -ge 9 ]; then GA_PRIV=VM.GuestAgent.Audit; else GA_PRIV=VM.Monitor; fi
    PRIVS="VM.Audit,Sys.Audit,$GA_PRIV"
    if pveum role list --output-format json | python3 -c "import json,sys; sys.exit(not any(r['roleid']=='$PVE_ROLE' for r in json.load(sys.stdin)))"; then
        pveum role modify "$PVE_ROLE" --privs "$PRIVS"
        info "role $PVE_ROLE updated: $PRIVS"
    else
        pveum role add "$PVE_ROLE" --privs "$PRIVS"
        info "role $PVE_ROLE created: $PRIVS"
    fi
    if ! pveum user list --output-format json | python3 -c "import json,sys; sys.exit(not any(u['userid']=='$PVE_USER' for u in json.load(sys.stdin)))"; then
        pveum user add "$PVE_USER" --comment "netbox-pve-sync (read-only, API token only)"
        info "user $PVE_USER created (no password; it can only be used through API tokens)"
    fi
    pveum acl modify / --users "$PVE_USER" --roles "$PVE_ROLE" --propagate 1
    TOKEN_NAME="agent-$NODE"
    TOKEN_ID="$PVE_USER!$TOKEN_NAME"
    has_token=0
    if pveum user token list "$PVE_USER" --output-format json | python3 -c "import json,sys; sys.exit(not any(t['tokenid']=='$TOKEN_NAME' for t in json.load(sys.stdin)))"; then
        has_token=1
    fi
    if [ "$has_token" = 1 ] && ! is_placeholder "$(secret_get PVE_TOKEN_SECRET)"; then
        info "API token $TOKEN_ID already configured"
    else
        if [ "$has_token" = 1 ]; then
            info "token $TOKEN_ID exists but its secret is not in secrets.env: recreating it"
            pveum user token remove "$PVE_USER" "$TOKEN_NAME"
        fi
        SECRET=$(pveum user token add "$PVE_USER" "$TOKEN_NAME" --privsep 0 \
                 --comment "netbox-pve-sync on $NODE" --output-format json \
                 | python3 -c 'import json,sys; print(json.load(sys.stdin)["value"])')
        [ -n "$SECRET" ] || die "could not create the Proxmox API token"
        secret_set PVE_TOKEN_SECRET "$SECRET"
        unset SECRET
        info "API token $TOKEN_ID created; secret stored in $CONF_DIR/secrets.env"
    fi
    yaml_set "$CONF_DIR/config.yaml" proxmox token_id "\"$TOKEN_ID\""

    # TLS: verify the local API against the cluster CA instead of disabling verification.
    if [ -r /etc/pve/pve-root-ca.pem ]; then
        install -m 0644 /etc/pve/pve-root-ca.pem "$CONF_DIR/pve-root-ca.pem"
        if command -v curl >/dev/null; then
            # Only curl's certificate errors mean a CA problem; anything else is reported by test-connection.
            rc=0
            curl -s -o /dev/null --max-time 10 --cacert "$CONF_DIR/pve-root-ca.pem" \
                https://127.0.0.1:8006/api2/json/version || rc=$?
            case "$rc" in
                0) info "TLS to https://127.0.0.1:8006 verifies against the cluster CA" ;;
                51|60|77|83|90|91)
                   warn "the local API certificate does not verify against the cluster CA (custom certificate?)."
                   warn "set proxmox.api_url to the certificate's hostname, or as a last resort proxmox.verify_ssl: false" ;;
                *) info "could not pre-check TLS (curl exit $rc); test-connection below checks it properly" ;;
            esac
        fi
    fi
fi

# --------------------------------------------------------------------------- systemd
step "Installing the systemd service"
install -m 0644 "$SRC_DIR/systemd/$APP.service" "$UNIT"
systemctl daemon-reload
systemctl enable "$APP.service" >/dev/null 2>&1
info "enabled $APP.service (starts at boot)"

# --------------------------------------------------------------------------- checks
step "Checking the configuration"
CONFIG_OK=0
if "$BIN" check-config >/dev/null && "$BIN" test-connection; then
    CONFIG_OK=1
else
    warn "the checks above failed - fix $CONF_DIR/config.yaml / secrets.env, then run: $APP test-connection"
fi

if [ "$CONFIG_OK" = 1 ]; then
    if systemctl is-active --quiet "$APP.service"; then
        # Upgrade: restart so the new code is used.
        [ "$START" = no ] || { systemctl restart "$APP.service"; info "service restarted with version $VERSION"; }
    else
        if [ "$START" = ask ] && interactive; then
            echo
            info "Recommended before starting: preview what would change in NetBox:"
            info "    $APP --dry-run"
            read -r -p "    Start the service now (it will synchronize all VMs into NetBox)? [y/N] " answer
            case "$answer" in [yY]*) START=yes ;; *) START=no ;; esac
        fi
        if [ "$START" = yes ]; then systemctl start "$APP.service"; info "service started"; fi
    fi
fi

# --------------------------------------------------------------------------- summary
step "Done"
systemctl --no-pager --lines=0 status "$APP.service" 2>/dev/null | head -n 4 || true
cat <<EOF

    Config:   $CONF_DIR/config.yaml   (secrets: $CONF_DIR/secrets.env)
    Logs:     journalctl -u $APP -f
    Status:   $APP status

    Next steps:
      1. $APP test-connection
      2. $APP --dry-run              # preview, writes nothing
      3. systemctl start $APP        # initial sync + event watching
EOF
