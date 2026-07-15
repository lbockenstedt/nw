#!/bin/bash
# Path Audit Script
# Checks for hardcoded local filesystem paths that would break in a container/remote deployment.

echo "🔍 Auditing for hardcoded paths..."
echo "--------------------------------------------------"

BASE_DIR="/Users/lbockenstedt/vscode"
REPOS=("lm" "cs" "pxmx" "opnsense" "cppm")
FAILED=0

# Pattern for local paths to avoid
# Matches /Users/lbockenstedt/vscode and common root-level paths like /root/lm if they look hardcoded
BAD_PATTERN="/Users/lbockenstedt/vscode"

for repo in "${REPOS[@]}"; do
    TARGET_DIR="$BASE_DIR/$repo"
    if [ ! -d "$TARGET_DIR" ]; then continue; fi

    # Search for the bad pattern in all files except audit scripts, venv, data, and common log/text artifacts
    MATCHES=$(grep -r "$BAD_PATTERN" "$TARGET_DIR" --exclude-dir="audit" --exclude-dir="venv" --exclude-dir="data" --exclude="*.log" --exclude="*.txt" 2>/dev/null)

    if [ -n "$MATCHES" ]; then
        echo "❌ Hardcoded paths found in $repo:"
        echo "$MATCHES"
        FAILED=1
    else
        echo "✅ $repo paths are clean."
    fi
done

if [ $FAILED -eq 0 ]; then
    echo "--------------------------------------------------"
    echo "✅ No hardcoded paths detected."
    exit 0
else
    echo "--------------------------------------------------"
    echo "❌ Hardcoded paths detected. Please use relative paths or configuration variables."
    exit 1
fi
