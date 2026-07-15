#!/bin/bash
# Serialization Audit Script
# Checks for unsafe json.dumps calls in security-critical files.
# Deterministic serialization requires sort_keys=True and separators=(',', ':').

echo "🔍 Auditing JSON serialization..."
echo "--------------------------------------------------"

BASE_DIR="/Users/lbockenstedt/vscode"
REPOS=("lm" "cs" "pxmx" "opnsense" "cppm")
FAILED=0

# Security-critical files usually contain these keywords
CRITICAL_KEYWORDS=("signer" "security" "key_manager" "control_plane")

for repo in "${REPOS[@]}"; do
    TARGET_DIR="$BASE_DIR/$repo"
    if [ ! -d "$TARGET_DIR" ]; then continue; fi

    # Find files that look security-critical
    for keyword in "${CRITICAL_KEYWORDS[@]}"; do
        FILES=$(find "$TARGET_DIR" -name "*.py" | grep "$keyword")
        for file in $FILES; do
            # Look for json.dumps calls
            if grep -q "json.dumps" "$file"; then
                if ! grep -q "separators=(',', ':')" "$file"; then
                    echo "❌ Unsafe serialization in $file"
                    echo "Expected separators=(',', ':') to ensure deterministic output."
                    FAILED=1
                fi
            fi
        done
    done
done

if [ $FAILED -eq 0 ]; then
    echo "--------------------------------------------------"
    echo "✅ JSON serialization looks deterministic."
    exit 0
else
    echo "--------------------------------------------------"
    echo "❌ Unsafe serialization detected. This will cause signature mismatches."
    exit 1
fi
