#!/bin/bash
# Import Audit Script
# Checks for legacy import paths that should have been migrated to lm.hub.src

echo "🔍 Auditing import paths..."
echo "--------------------------------------------------"

# The old path we are looking for
OLD_PATH="lm.spoke.src"
# The new path we expect
NEW_PATH="lm.hub.src"

# Search across all repositories in the root vscode directory, excluding the audit script itself
FILES=$(grep -r "$OLD_PATH" /Users/lbockenstedt/vscode --exclude="audit_imports.sh" 2>/dev/null)

if [ -z "$FILES" ]; then
    echo "✅ No legacy imports found. All files are using the updated structure."
    exit 0
else
    echo "❌ Legacy imports detected!"
    echo "The following files still reference $OLD_PATH:"
    echo "$FILES"
    echo "--------------------------------------------------"
    echo "Action required: Update these imports to $NEW_PATH"
    exit 1
fi
