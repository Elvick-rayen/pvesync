#!/bin/sh
# /usr/local/bin/netbox-pve-sync - command line wrapper installed by install.sh.
# When run as root it switches to the service user, so files it creates in
# /var/lib/netbox-pve-sync keep the owner the systemd service needs.
set -e
export PYTHONPATH=/opt/netbox-pve-sync/lib
export PYTHONDONTWRITEBYTECODE=1
if [ "$(id -u)" = "0" ]; then
    exec runuser -u netbox-pve-sync -- /usr/bin/python3 -m netbox_pve_sync "$@"
fi
exec /usr/bin/python3 -m netbox_pve_sync "$@"
