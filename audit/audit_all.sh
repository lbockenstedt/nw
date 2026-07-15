#!/bin/bash
# Master Static Audit & Sync Script
# Performs a series of static analysis checks. Committing and pushing to GitHub
# requires an explicit --push flag and, for each repo with changes, an interactive
# y/n confirmation showing the diff — this script no longer commits or pushes
# anything on its own.

echo "=================================================="
echo "🧪 LAB MANAGER STATIC REGRESSION AUDIT & SYNC"
echo "=================================================="

# 3. Path Configuration
AUDIT_DIR="/Users/lbockenstedt/vscode/audit"
BASE_DIR="/Users/lbockenstedt/vscode"
REPOS=("lm" "cs" "pxmx" "opnsense" "cppm")

PUSH=false
for arg in "$@"; do
    case "$arg" in
        --push) PUSH=true ;;
    esac
done

if [ "$PUSH" = false ]; then
    echo "ℹ️  Running in audit-only mode (no commits/pushes). Pass --push to sync, with a confirmation prompt per repo."
fi

# 1. Run Import Audit (Broken Path Links)
echo ""
$AUDIT_DIR/audit_imports.sh
if [ $? -ne 0 ]; then
    echo "❌ Audit failed at 'Import Audit'. Aborting Sync."
    exit 1
fi

# 2. Run Path Audit (Hardcoded Local Paths)
echo ""
$AUDIT_DIR/audit_paths.sh
if [ $? -ne 0 ]; then
    echo "❌ Audit failed at 'Path Audit'. Aborting Sync."
    exit 1
fi

# 3. Run Serialization Audit (Deterministic JSON)
echo ""
$AUDIT_DIR/audit_serialization.sh
if [ $? -ne 0 ]; then
    echo "❌ Audit failed at 'Serialization Audit'. Aborting Sync."
    exit 1
fi

# 4. Run Dependency Analysis (Missing Requirements)
echo ""
$AUDIT_DIR/audit_dependencies.sh
if [ $? -ne 0 ]; then
    echo "❌ Audit failed at 'Dependency Analysis'. Aborting Sync."
    exit 1
fi

# 5. Run Syntax Audit (Code Errors)
echo ""
$AUDIT_DIR/audit_syntax.sh
if [ $? -ne 0 ]; then
    echo "❌ Audit failed at 'Syntax Audit'. Aborting Sync."
    exit 1
fi

echo ""
echo "✅ ALL STATIC AUDITS PASSED."
echo "--------------------------------------------------"

if [ "$PUSH" = false ]; then
    echo "Audit-only mode: not touching git. Re-run with --push to review and sync changes."
    echo ""
    echo "=================================================="
    echo "Static Audit Complete."
    echo "=================================================="
    exit 0
fi

echo "Proceeding to review changes for sync..."

# 4. Git Sync Logic — every commit/push below requires an explicit y/n confirmation.
# Nothing is committed or pushed without a human looking at the diff first, and a
# rejected push is never auto-merged and retried; it's surfaced for manual resolution.
for repo in "${REPOS[@]}"; do
    echo "Processing $repo..."
    TARGET_DIR="$BASE_DIR/$repo"

    if [ ! -d "$TARGET_DIR" ]; then
        echo "⚠️  Skipping $repo: Directory $TARGET_DIR not found."
        continue
    fi

    cd "$TARGET_DIR"

    # Check for changes
    if [ -n "$(git status --porcelain)" ]; then
        echo ""
        echo "Changes detected in $repo:"
        git status --short
        echo ""
        git --no-pager diff --stat
        read -r -p "Commit and push these changes in $repo to main? [y/N] " confirm
        if [[ ! "$confirm" =~ ^[Yy]$ ]]; then
            echo "⏭️  Skipped $repo (not confirmed)."
            continue
        fi

        git add .
        git commit -m "Auto-sync: Static analysis verified build $(date '+%Y-%m-%d %H:%M')"

        if git push origin HEAD:main; then
            echo "✅ $repo pushed successfully."
        else
            echo "❌ $repo push was rejected (remote has new commits)."
            echo "   Not auto-merging — pull/rebase manually in $TARGET_DIR and push when ready."
        fi
    else
        echo "✨ $repo is clean. Skipping."
    fi
done

echo ""
echo "=================================================="
echo "Static Audit and Sync Complete."
echo "=================================================="
