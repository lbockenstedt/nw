#!/bin/bash

# Lab Manager Autonomous Setup Script for LXC
# This script installs the environment, clones the repos, and sets up the AI-driven issue fix loop.

set -e

echo "🚀 Starting Lab Manager Autonomous Environment Setup..."

# 1. System Updates and Dependencies
echo "📦 Installing system dependencies..."
apt-get update
apt-get install -y \
    git \
    curl \
    python3 \
    python3-pip \
    python3-venv \
    docker.io \
    docker-compose \
    sudo \
    wget \
    jq

# 2. Install GitHub CLI (gh)
echo "🐙 Installing GitHub CLI..."
type -p gh >/dev/null || {
    curl -fsSL https://cli.github.com/packages/githubcli-assetpacks-amd64.tar.gz | sudo tar brz -C /usr/local -C /usr/local bin,share
    # Alternative for Debian/Ubuntu
    sudo mkdir -p -m 755 /etc/apt/keyrings && wget -qO- https://cli.github.com/packages/githubcli-archive-keyring.gpg | sudo tee /etc/apt/keyrings/github-cli.gpg > /dev/null
    sudo mkdir -p /etc/apt/keyrings
    echo "deb [signed-by=/etc/apt/keyrings/github-cli.gpg] https://cli.github.com/packages stable contrib" | sudo tee /etc/apt/sources.list.d/github-cli.list > /dev/null
    sudo apt update
    sudo apt install gh -y
}

# 3. Clone Repositories
# Assuming the user is in /opt or /home/user
WORK_DIR="/opt/labmanager"
mkdir -p $WORK_DIR
cd $WORK_DIR

repos=("lbockenstedt/lm" "lbockenstedt/cppm" "lbockenstedt/cs" "lbockenstedt/ldap" "lbockenstedt/netbox" "lbockenstedt/opnsense" "lbockenstedt/pxmx")

echo "📂 Cloning repositories..."
for repo in "${repos[@]}"; do
    repo_name=$(basename $repo)
    if [ ! -d "$repo_name" ]; then
        git clone "https://$repo"
    else
        echo "Repo $repo_name already exists, skipping clone."
    fi
done

# 4. Setup the AI Automation Bridge
echo "🤖 Setting up AI Automation bridge..."
# Copy the polling and log scripts from the current environment
# (In a real scenario, these would be in a dedicated 'automation' repo)
# For now, we'll create the poll_issues.py based on the current version
cat << 'EOF' > $WORK_DIR/scripts/poll_issues.py
import os
import subprocess
import json
import argparse
from typing import List, Dict, Optional

def get_repo_name(path: str) -> Optional[str]:
    try:
        result = subprocess.run(["git", "-C", path, "remote", "get-url", "origin"], capture_output=True, text=True, check=True)
        url = result.stdout.strip()
        if "github.com" in url:
            parts = url.split("github.com/")[-1].split(".git")[0].split("/")
            if len(parts) >= 2: return f"{parts[0]}/{parts[1]}"
    except Exception: pass
    return None

def get_pending_ai_issues():
    all_issues = []
    for root, dirs, files in os.walk("."):
        if ".git" in dirs:
            repo_name = get_repo_name(root)
            if repo_name:
                cmd = ["gh", "issue", "list", "-R", repo_name, "--label", "automated-fix", "--json", "number,title,body,url"]
                result = subprocess.run(cmd, capture_output=True, text=True)
                if result.returncode == 0:
                    try:
                        issues = json.loads(result.stdout)
                        for i in issues: i["repo"] = repo_name
                        all_issues.extend(issues)
                    except json.JSONDecodeError: pass
            dirs.remove(".git")
    return all_issues

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    issues = get_pending_ai_issues()
    if args.json:
        print(json.dumps(issues))
    else:
        print(f"🔍 Found {len(issues)} pending AI issues.")
        for i in issues: print(f"[{i['repo']}] #{i['number']} - {i['title']}")

if __name__ == "__main__":
    main()
EOF

# 5. Setup Cron Job
echo "⏰ Scheduling issue polling every 5 minutes..."
CRON_JOB="*/5 * * * * cd $WORK_DIR && python3 scripts/poll_issues.py >> /var/log/lm_ai_poll.log 2>&1"
(crontab -l 2>/dev/null; echo "$CRON_JOB") | crontab -

echo "------------------------------------------------------------------"
echo "✅ Setup Complete!"
echo "------------------------------------------------------------------"
echo "NEXT STEPS:"
echo "1. Authenticate GitHub CLI: 'gh auth login'"
echo "2. Start the Hub: 'cd $WORK_DIR/lm && ./start.sh'"
echo "3. When you see issues in the logs, run: 'claude \"Run the issue-fix workflow\"'"
echo "------------------------------------------------------------------"
