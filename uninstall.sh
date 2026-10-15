#!/usr/bin/env bash
# uninstall.sh - remove netbox-pve-sync from this Proxmox node.
#
#   ./uninstall.sh            stop and remove the service and code; keep config, state and Proxmox token
#   ./uninstall.sh --purge    also remove config, secrets, state, the system user and THIS node's API token
#   ./uninstall.sh --purge --remove-pve-access
#                             also remove the Proxmox user netbox-sync@pve, role NetBoxSync and ACL.
#                             These are cluster-wide: only do this on the last node that runs the agent.
#
# NetBox is never touched: synchronized VMs stay in NetBox (see README "Uninstall").
set -euo pipefail

APP=netbox-pve-sync
SERVICE_USER=netbox-pve-sync
PVE_USER=netbox-sync@pve
PVE_ROLE=NetBoxSync
PURGE=0
REMOVE_PVE=0

for arg in "$@"; do
    case "$arg" in
        --purge) PURGE=1 ;;
        --remove-pve-access) REMOVE_PVE=1 ;;
        -h|--help) sed -n '2,11p' "$0"; exit 0 ;;
        *) echo "Unknown option: $arg" >&2; exit 2 ;;
    esac
done
[ "$(id -u)" = 0 ] || { echo "run as root" >&2; exit 1; }
[ "$REMOVE_PVE" = 0 ] || [ "$PURGE" = 1 ] || { echo "--remove-pve-access requires --purge" >&2; exit 2; }

echo "==> Stopping and removing the service"
systemctl disable --now "$APP.service" 2>/dev/null || true
rm -f "/etc/systemd/system/$APP.service"
systemctl daemon-reload
rm -f "/usr/local/bin/$APP"
rm -rf "/opt/${APP:?}"

if [ "$PURGE" = 1 ]; then
    echo "==> Removing configuration, state and system user"
    rm -rf "/etc/${APP:?}" "/var/lib/${APP:?}"
    if id -u "$SERVICE_USER" >/dev/null 2>&1; then userdel "$SERVICE_USER" 2>/dev/null || true; fi
    if getent group "$SERVICE_USER" >/dev/null; then groupdel "$SERVICE_USER" 2>/dev/null || true; fi
    if command -v pveum >/dev/null; then
        TOKEN_NAME="agent-$(hostname -s)"
        pveum user token remove "$PVE_USER" "$TOKEN_NAME" 2>/dev/null && echo "    removed API token $PVE_USER!$TOKEN_NAME" || true
        if [ "$REMOVE_PVE" = 1 ]; then
            pveum acl delete / --users "$PVE_USER" --roles "$PVE_ROLE" 2>/dev/null || true
            pveum user delete "$PVE_USER" 2>/dev/null && echo "    removed user $PVE_USER" || true
            pveum role delete "$PVE_ROLE" 2>/dev/null && echo "    removed role $PVE_ROLE" || true
        fi
    fi
else
    echo "    kept /etc/$APP and /var/lib/$APP (use --purge to remove them)"
fi
echo "==> netbox-pve-sync removed. NetBox data was not changed."
