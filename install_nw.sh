#!/bin/bash
set -e

# Default Configuration
HUB_URL="auto"   # was ws://localhost:8765 (retired bare listener); auto-discover the unified :443 hub
SPOKE_ID="${SPOKE_ID:-nw-$(hostname -s)}"
SPOKE_SECRET="lm-secret"

# Parse arguments
while [[ "$#" -gt 0 ]]; do
    case $1 in
        --hub) HUB_URL="$2"; shift ;;
        --id|--name) SPOKE_ID="$2"; shift ;;
        --secret) SPOKE_SECRET="$2"; shift ;;
        --hub-secret) HUB_SECRET="$2"; shift ;;
        --all-prereqs) ;;  # no-op; accepted for LM hub compat
        *) echo "Unknown parameter passed: $1"; exit 1 ;;
    esac
    shift
done

if [ -z "$SPOKE_SECRET" ] || [ "$SPOKE_SECRET" == "lm-secret" ]; then
    SPOKE_SECRET=""
    echo "ℹ️  No pre-shared secret — spoke will connect unauthenticated and await admin approval in the LM WebUI."
fi

echo "🚀 Installing Network Devices Manager Module (Native)..."

if [ "$(id -u)" -ne 0 ]; then
    echo "⚠️  This script must be run as root."
    exit 1
fi

apt-get update
apt-get install -y python3-pip python3-venv git curl

INSTALL_DIR="/opt/lm"
OLD_INSTALL_DIR="/opt/lm-manager"

# Cleanup legacy installation
if [ -d "$OLD_INSTALL_DIR" ]; then
    echo "🗑️  Removing legacy installation at $OLD_INSTALL_DIR..."
    rm -rf "$OLD_INSTALL_DIR"
fi

mkdir -p "$INSTALL_DIR"
mkdir -p /var/log/lm   # systemd `append:` won't create the parent dir → unit 206/EXEC on a clean box
cd "$INSTALL_DIR"

if [ -d "nw" ]; then
    echo "📂 Network Devices directory exists. Preparing for update..."
    SPOKE_PATH="$INSTALL_DIR/nw"
    cd "$SPOKE_PATH"
    git fetch origin -q && git reset --hard origin/main   # hard-sync (soft `git pull` no-ops on a diverged/detached clone)
    cd "$INSTALL_DIR"
elif [ -d ".git" ]; then
    # This case is for when we are already inside the nw dir
    git fetch origin -q && git reset --hard origin/main   # hard-sync
    SPOKE_PATH="$(pwd)"
else
    echo "🌐 Cloning Network Devices Manager repository..."
    git clone https://github.com/lbockenstedt/nw.git
    SPOKE_PATH="$INSTALL_DIR/nw"
fi

echo "🛠️ Setting up Network Devices Manager..."
cd "$SPOKE_PATH"

# Always remove existing venv to ensure clean local environment (prevents cross-platform path issues)
echo "♻️ Resetting virtual environment..."
rm -rf venv

python3 -m venv venv
if [ ! -f "venv/bin/python3" ]; then
    echo "❌ Critical Error: venv creation failed."
    exit 1
fi

echo "Installing requirements..."
./venv/bin/python3 -m pip install --upgrade pip -q
if [ -f "requirements.txt" ]; then
    ./venv/bin/python3 -m pip install -r requirements.txt -q
fi

# --- Persistence Configuration ---
echo "⚙️ Configuring Spoke Identity..."
cat <<EOF > .env
HUB_URL=$HUB_URL
SPOKE_ID=$SPOKE_ID
SPOKE_SECRET=$SPOKE_SECRET
HUB_SECRET=$HUB_SECRET
EOF

# --- Systemd Service (For Remote/Independent Deployment) ---
echo "⚙️ Creating systemd service for auto-start..."
# ExecStart uses the equals-attached arg form (--id=VALUE, not --id VALUE) for
# every flag. control_plane.py declares --secret/--hub-secret with argparse
# nargs='?', which REFUSES to consume a following token that starts with '-'
# (treats it as an option flag). A generated hub-secret like "-3s6bmMPW4..."
# therefore made argparse abort with "unrecognized arguments: -3s6bm..." and
# the spoke crash-looped (nw-spoke-1 never registered). The equals form takes
# everything after '=' verbatim, so any value (empty, leading-dash, or
# otherwise) is accepted. Empty SPOKE_SECRET then resolves to "" (matches the
# unauthenticated + await-admin-approval intent above). NOTE: keep this
# rationale HERE, above the unquoted heredoc — backticks in a comment would
# be run as command substitution inside <<EOF.
cat <<EOF > /etc/systemd/system/lm-nw.service
[Unit]
Description=Lab Manager Spoke - Network Devices Manager
After=network.target

[Service]
Type=simple
User=svc_lm
WorkingDirectory=$INSTALL_DIR/nw
EnvironmentFile=$INSTALL_DIR/nw/.env
Environment="PYTHONPATH=$INSTALL_DIR:$INSTALL_DIR/core/src:$INSTALL_DIR/nw/src"
# equals-attached args: accepts values that start with '-' (see installer note above)
ExecStart=$INSTALL_DIR/nw/venv/bin/python3 -m src.control_plane --id=\${SPOKE_ID} --secret=\${SPOKE_SECRET} --hub=\${HUB_URL} --hub-secret=\${HUB_SECRET}
StandardOutput=append:/var/log/lm/lm-nw.log
StandardError=append:/var/log/lm/lm-nw.log
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable lm-nw

echo "🎉 Network Devices Manager installation complete!"
echo "🌐 Hub Target: $HUB_URL"
echo "🆔 Spoke ID: $SPOKE_ID"
echo "📦 Version: $(cat VERSION 2>/dev/null || echo unknown)"