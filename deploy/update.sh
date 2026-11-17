#!/usr/bin/env bash
# tak-bridge: update an installed bridge to a reviewed commit. Run as root:
#
#   sudo bash /opt/takbridge/src/deploy/update.sh <commit>
#
# Fetches GrokBridge, checks out <commit>, reinstalls the package into
# /opt/takbridge/venv, and restarts tak-bridged. Leaves the configuration,
# certificates, database, users and SSH gate as they are. The database is
# upgraded in place by the service when it starts (new columns only).

set -euo pipefail

die() { echo "update: $*" >&2; exit 1; }
[ "$(id -u)" = 0 ] || die "run as root"
REF="${1:-}"
[[ "$REF" =~ ^[0-9a-f]{7,40}$ ]] || die "give the commit to install, e.g. sudo bash $0 1a2b3c4"
SRC=/opt/takbridge/src
[ -d "$SRC/.git" ] || die "$SRC is not installed; run deploy/install.sh first"

echo "== from: $(git -C "$SRC" log --oneline -1)"
git -C "$SRC" fetch -q origin
git -C "$SRC" cat-file -e "$REF^{commit}" 2>/dev/null || die "commit $REF not found on GitHub"
git -C "$SRC" checkout -q "$REF"
echo "== to:   $(git -C "$SRC" log --oneline -1)"

/opt/takbridge/venv/bin/pip install -q "$SRC"
chown -R root:root /opt/takbridge
if ! cmp -s "$SRC/deploy/tak-bridged.service" /etc/systemd/system/tak-bridged.service; then
    install -m 0644 -o root -g root "$SRC/deploy/tak-bridged.service" /etc/systemd/system/tak-bridged.service
    systemctl daemon-reload
    echo "== systemd unit updated"
fi
systemctl restart tak-bridged
sleep 5
systemctl is-active tak-bridged
journalctl -u tak-bridged -n 5 --no-pager
