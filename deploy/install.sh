#!/usr/bin/env bash
# tak-bridge phase 7: permanent install on the TAK Server host (zodiac).
#
# Review before running. Run as root:
#
#   sudo bash deploy/install.sh
#
# The webhook URL and its key are secrets, and this repository is public, so
# they are never written here. The script reads them, as root, from the
# root-only file WEBHOOK_ENV (below), which holds KEY=VALUE lines. Set the four
# *_VAR names below to the names used in that file. The values are copied
# into /etc/takbridge/takbridge.ini (mode 600, service user only) and are
# never printed.
#
# Safe to run again; each step checks before it changes anything.
#
# What it does, in order:
#   1. installs python3-venv and git if missing
#   2. creates group takbridge, the service user takbridge (no login), the
#      bot login user takbot (key-only, forced command), and group takadmin
#   3. installs tak-bridge from GitHub at a pinned commit into
#      /opt/takbridge/venv (owned by root, read-only to the service)
#   4. copies GROK-1's certificate, key and the CA chain to
#      /etc/takbridge/certs and writes /etc/takbridge/takbridge.ini with a new
#      webhook secret (all mode 0600/0700, owned by takbridge)
#   5. writes takbot's authorized_keys: one line per bot, forced to the SSH
#      gate, with port/agent/X11 forwarding and PTY disabled
#   6. installs and starts the systemd unit tak-bridged
#   7. symlinks /usr/local/bin/tak for admins
#
# It does NOT touch the TAK Server's configuration, users or groups, sshd's
# configuration, or the firewall.

set -euo pipefail

# ------------------------------------------------------------- settings --
BOT_CALLSIGN="GrokBot-Helper"   # what ATAK shows; the certificate below may carry
                                # another name (it was issued as GROK-1)
# The bot's takbot login key (public half; safe to publish).
BOT_SSH_KEY="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIDl1mI3nTnBIW4b1EtGUoKNq4RdILTMlPOMVBn7BclVn GROK-1-takbot@zodiac"

# Webhook settings, read from a root-only file. Each call sends
#   <header name>: <scheme> <key>      e.g.  Authorization: Bearer ...
# plus the bridge's own X-TakBridge-Signature, to the URL, which must be
# https://. Each call carries the message text and the sender's position.
WEBHOOK_ENV="/etc/grok-bridge/webhook.env"
URL_VAR="GROK1_WEBHOOK_URL"         # names of the KEY=VALUE lines in WEBHOOK_ENV;
HEADER_VAR="GROK1_WEBHOOK_AUTH_HEADER"    # change these to match the file
SCHEME_VAR="GROK1_WEBHOOK_AUTH_SCHEME"
KEY_VAR="GROK1_WEBHOOK_KEY"
BOT_CERT="/home/claude/certs/GROK-1.crt.pem"
BOT_KEY="/home/claude/certs/GROK-1.key"
CA_CHAIN="/home/claude/certs/magnolia-ca-chain.pem"
COT_URL="tls://zodiac.snowy-pangolin.ts.net:8089"
BOT_LAT="35.0"          # where the bot appears on the map
BOT_LON="-90.0"
ADMINS="claude"         # users who may run 'tak --bot NAME' locally
REPO="https://github.com/mitchmitchell/GrokBridge.git"
REF="3552b5a"           # code version installed (GrokBridge commit)
# ------------------------------------------------------------------------

die() { echo "install: $*" >&2; exit 1; }
step() { echo; echo "== $*"; }

[ "$(id -u)" = 0 ] || die "run as root"

# Read one KEY=VALUE line from WEBHOOK_ENV without running the file as code.
# Strips surrounding quotes and whitespace (a trailing newline in a header
# value makes Cursor answer HTTP 400).
envval() {
    sed -n "s/^[[:space:]]*\(export[[:space:]]\+\)\?$1[[:space:]]*=[[:space:]]*//p" "$WEBHOOK_ENV" \
        | head -1 | sed -e 's/[[:space:]]*$//' -e "s/^[\"']//" -e "s/[\"']$//"
}
[ -r "$WEBHOOK_ENV" ] || die "cannot read $WEBHOOK_ENV"
case "$(stat -c '%a %U' "$WEBHOOK_ENV")" in 600\ root|400\ root) ;; *) die "$WEBHOOK_ENV must be mode 600, owned by root";; esac
BOT_WEBHOOK_URL=$(envval "$URL_VAR")
WEBHOOK_HEADER=$(envval "$HEADER_VAR")
WEBHOOK_SCHEME=$(envval "$SCHEME_VAR")
WEBHOOK_KEY=$(envval "$KEY_VAR")
for v in BOT_WEBHOOK_URL:$URL_VAR WEBHOOK_HEADER:$HEADER_VAR WEBHOOK_SCHEME:$SCHEME_VAR WEBHOOK_KEY:$KEY_VAR; do
    name=${v%%:*}
    [ -n "${!name}" ] || die "no ${v#*:}= line in $WEBHOOK_ENV (set the *_VAR names at the top)"
done
case "$BOT_WEBHOOK_URL" in https://*) ;; *) die "the webhook URL must start with https://";; esac
[[ "$WEBHOOK_HEADER" =~ ^[A-Za-z0-9-]{1,64}$ ]] || die "the webhook header name is not a valid header name"
[[ "$WEBHOOK_SCHEME$WEBHOOK_KEY" != *[[:space:]]* ]] || die "the webhook scheme or key contains spaces or line breaks"
echo "webhook: ${BOT_WEBHOOK_URL%%/automations/*}/... with header $WEBHOOK_HEADER: $WEBHOOK_SCHEME <key, ${#WEBHOOK_KEY} characters>"
case "$BOT_SSH_KEY" in ssh-ed25519\ *|ssh-rsa\ *|ecdsa-sha2-*) ;; *) die "BOT_SSH_KEY does not look like a public key";; esac
case "$BOT_SSH_KEY" in *PRIVATE*) die "that is a private key; use the .pub line";; esac
for f in "$BOT_CERT" "$BOT_KEY" "$CA_CHAIN"; do [ -r "$f" ] || die "cannot read $f"; done

step "1. packages"
need=()
python3 -c "import ensurepip" 2>/dev/null || need+=(python3-venv)
command -v git >/dev/null || need+=(git)
if [ ${#need[@]} -gt 0 ]; then apt-get update -q && apt-get install -y -q "${need[@]}"; fi

step "2. users and groups"
getent group takbridge >/dev/null || groupadd --system takbridge
getent group takadmin >/dev/null || groupadd --system takadmin
id takbridge >/dev/null 2>&1 || useradd --system --gid takbridge --home-dir /var/lib/takbridge \
    --no-create-home --shell /usr/sbin/nologin takbridge
id takbot >/dev/null 2>&1 || useradd --create-home --shell /bin/sh takbot
passwd -l takbot >/dev/null            # key-only: no password login
usermod -aG takbridge takbot           # may reach the control socket
for u in $ADMINS; do
    id "$u" >/dev/null 2>&1 && usermod -aG takadmin,takbridge "$u"
done

step "3. code"
install -d -m 0755 -o root -g root /opt/takbridge
if [ -d /opt/takbridge/src/.git ]; then
    git -C /opt/takbridge/src fetch -q origin
else
    git clone -q "$REPO" /opt/takbridge/src
fi
git -C /opt/takbridge/src checkout -q "$REF"
git -C /opt/takbridge/src log --oneline -1
[ -x /opt/takbridge/venv/bin/python ] || python3 -m venv /opt/takbridge/venv
/opt/takbridge/venv/bin/pip install -q --upgrade pip
/opt/takbridge/venv/bin/pip install -q /opt/takbridge/src
chown -R root:root /opt/takbridge

step "4. configuration and certificates"
install -d -m 0750 -o root -g takbridge /etc/takbridge
install -d -m 0700 -o takbridge -g takbridge /etc/takbridge/certs
install -m 0600 -o takbridge -g takbridge "$BOT_CERT" "/etc/takbridge/certs/$BOT_CALLSIGN.crt.pem"
install -m 0600 -o takbridge -g takbridge "$BOT_KEY" "/etc/takbridge/certs/$BOT_CALLSIGN.key"
install -m 0600 -o takbridge -g takbridge "$CA_CHAIN" /etc/takbridge/certs/ca-chain.pem
CONF=/etc/takbridge/takbridge.ini
if [ -f "$CONF" ]; then
    echo "$CONF exists; leaving it (and its webhook secret) unchanged"
else
    SECRET=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
    umask 077
    cat > "$CONF" <<INI
[bridge]
cot_url = $COT_URL
lat = $BOT_LAT
lon = $BOT_LON
group = Cyan
role = Team Member
presence_interval_s = 30
presence_stale_s = 120
db_path = /var/lib/takbridge/takbridge.db
socket_path = /run/takbridge/takbridge.sock
webhook_secret = $SECRET
bot_login_user = takbot
admin_group = takadmin

[bot:$BOT_CALLSIGN]
webhook_url = $BOT_WEBHOOK_URL
webhook_header_$WEBHOOK_HEADER = $WEBHOOK_SCHEME $WEBHOOK_KEY
PYTAK_TLS_CLIENT_CERT = /etc/takbridge/certs/$BOT_CALLSIGN.crt.pem
PYTAK_TLS_CLIENT_KEY = /etc/takbridge/certs/$BOT_CALLSIGN.key
PYTAK_TLS_CLIENT_CAFILE = /etc/takbridge/certs/ca-chain.pem
INI
    chown takbridge:takbridge "$CONF"
    chmod 0600 "$CONF"
    echo "wrote $CONF with a new webhook secret."
    echo "Give the bot the secret with:  sudo grep webhook_secret $CONF"
fi
# Check the config parses, as the service user.
sudo -u takbridge /opt/takbridge/venv/bin/python -c \
    "from takbridge.config import load; c=load('$CONF'); print('config ok:', ', '.join(c.bots))"

step "5. SSH gate for the bot"
install -d -m 0700 -o takbot -g takbot /home/takbot/.ssh
AK=/home/takbot/.ssh/authorized_keys
LINE="command=\"/opt/takbridge/venv/bin/takbridge-gate $BOT_CALLSIGN\",no-port-forwarding,no-agent-forwarding,no-X11-forwarding,no-pty,no-user-rc $BOT_SSH_KEY"
touch "$AK"
# Replace any earlier line for this bot, keep others.
grep -v "takbridge-gate $BOT_CALLSIGN\"" "$AK" > "$AK.new" || true
echo "$LINE" >> "$AK.new"
mv "$AK.new" "$AK"
chown takbot:takbot "$AK"
chmod 0600 "$AK"
echo "takbot authorized_keys: $(wc -l < "$AK") key(s)"

step "6. service"
install -m 0644 -o root -g root /opt/takbridge/src/deploy/tak-bridged.service \
    /etc/systemd/system/tak-bridged.service
systemctl daemon-reload
systemctl enable --now tak-bridged
sleep 5
systemctl --no-pager --lines=5 status tak-bridged || true

step "7. admin command"
ln -sf /opt/takbridge/venv/bin/tak /usr/local/bin/tak
echo
echo "Done. Check with:  tak --bot $BOT_CALLSIGN status"
echo "Stop the bridge:   sudo systemctl stop tak-bridged"
echo "Remove the bot's SSH access: delete its line in $AK"
