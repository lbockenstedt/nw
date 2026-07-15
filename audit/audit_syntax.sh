#!/bin/bash
# Syntax Audit Script
# Performs a static syntax check on all Python files to ensure no syntax errors exist.

echo "🔍 Auditing Python syntax..."
echo "--------------------------------------------------"

# Path Configuration
REPOS=("lm" "cs" "pxmx" "opnsense" "cppm")
BASE_DIR="/Users/lbockenstedt/vscode"
FAILED=0

for repo in "${REPOS[@]}"; do
    echo "Checking $repo..."
    TARGET_DIR="$BASE_DIR/$repo"

    if [ ! -d "$TARGET_DIR" ]; then
        echo "⚠️  Skipping $repo: Directory $TARGET_DIR not found."
        continue
    fi

    # Find all .py files and compile them to check for syntax errors
    ERRORS=$(find "$TARGET_DIR" -name "*.py" -exec python3 -m py_compile {} + 2>&1)

    if [ -n "$ERRORS" ]; then
        echo "❌ Syntax errors found in $repo:"
        echo "$ERRORS"
        FAILED=1
    else
        echo "✅ $repo syntax is clean."
    fi
done

if [ $FAILED -eq 0 ]; then
    echo "--------------------------------------------------"
    echo "✅ All files passed the syntax check."
    exit 0
else
    echo "--------------------------------------------------"
    echo "❌ Syntax errors detected. Please fix them before syncing."
    exit 1
fi
