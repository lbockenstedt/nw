#!/bin/bash
# Dependency Analysis Script
# Performs a static analysis of requirements.txt against the imports in the code.

echo "📦 Auditing dependency declarations..."
echo "--------------------------------------------------"

# 3. Path Configuration
REPOS=("lm" "cs" "pxmx" "opnsense" "cppm")
BASE_DIR="/Users/lbockenstedt/vscode"
FAILED=0

for repo in "${REPOS[@]}"; do
    echo "Analyzing $repo..."
    TARGET_DIR="$BASE_DIR/$repo"

    if [ ! -d "$TARGET_DIR" ]; then
        echo "⚠️  Skipping $repo: Directory $TARGET_DIR not found."
        continue
    fi

    # Get all imported modules in the code
    IMPORTS=$(grep -r "import " "$TARGET_DIR" | grep -oE "import [a-zA-Z0-9_]+" | awk '{print $2}' | sort -u)
    # Also catch 'from x import y'
    IMPORTS_FROM=$(grep -r "from " "$TARGET_DIR" | grep -oE "from [a-zA-Z0-9_]+" | awk '{print $2}' | sort -u)
    ALL_IMPORTS=$(echo -e "$IMPORTS\n$IMPORTS_FROM" | sort -u)

    # Check requirements.txt
    if [ "$repo" == "lm" ]; then
        REQ_FILE="$TARGET_DIR/core/requirements.txt"
    else
        REQ_FILE="$TARGET_DIR/requirements.txt"
    fi

    if [ ! -f "$REQ_FILE" ]; then
        echo "❌ $repo: Missing requirements.txt"
        FAILED=1
        continue
    fi

    # Basic check: Ensure common critical libraries are listed
    CRITICAL=("websockets" "fastapi" "uvicorn" "cryptography")
    for crit in "${CRITICAL[@]}"; do
        if grep -q "$crit" "$TARGET_DIR/src" -r && ! grep -q "$crit" "$REQ_FILE"; then
            echo "❌ $repo: $crit is used in code but missing from requirements.txt"
            FAILED=1
        fi
    done
    echo "✅ $repo requirements check complete."
done

if [ $FAILED -eq 0 ]; then
    echo "--------------------------------------------------"
    echo "✅ Dependency declarations look consistent."
    exit 0
else
    echo "--------------------------------------------------"
    echo "❌ Dependency gaps detected."
    exit 1
fi
