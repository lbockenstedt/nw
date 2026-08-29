import requests
import sys
import argparse

# Configuration - Defaults
DEFAULT_REPO = "lbockenstedt/lbockenstedt" # Adjusted to owner/repo format

def main():
    parser = argparse.ArgumentParser(description="Purge GitHub issues based on tenant and report markers.")
    parser.add_argument("--token", required=True, help="GitHub Personal Access Token")
    parser.add_argument("--repo", default=DEFAULT_REPO, help="Repository in 'owner/repo' format (default: lbockenstedt/lbockenstedt)")
    parser.add_argument("--tenant", required=True, help="The tenant name or ID to scan for in the issue body")
    parser.add_argument("--dry-run", action="store_true", help="List issues that would be deleted without actually deleting them")
    parser.add_argument("--force-all", action="store_true", help="Ignore report markers and delete any issue mentioning the tenant")

    args = parser.parse_args()

    headers = {
        'Authorization': f'token {args.token}',
        'Accept': 'application/vnd.github.v3+json'
    }

    print(f"🔍 Scanning {args.repo} for reports belonging to tenant: {args.tenant}...")
    if args.dry_run:
        print("⚠️ DRY RUN MODE: No issues will be deleted.\n")

    # 1. Fetch open issues
    url = f'https://api.github.com/repos/{args.repo}/issues?state=open&per_page=100'
    response = requests.get(url, headers=headers)

    if response.status_code != 200:
        print(f"❌ Error fetching issues: {response.status_code} - {response.text}")
        sys.exit(1)

    issues = response.json()
    if not isinstance(issues, list):
        print("❌ Unexpected API response format.")
        sys.exit(1)

    deleted_count = 0

    for issue in issues:
        # Skip Pull Requests (GitHub API returns PRs as issues)
        if 'pull_request' in issue:
            continue

        issue_num = issue['number']
        body = issue.get('body') or ""
        title = issue.get('title', "")

        # Logic to determine if this is a "report" for the specific tenant
        is_tenant_match = args.tenant.lower() in body.lower() or args.tenant.lower() in title.lower()
        # AppBuilder stamps "<!-- bf-module: <module> -->" (see ab/github_ops.py),
        # so match the marker prefix -- the exact-string form never matched.
        # Reports filed before the rename carry the same marker, so no legacy
        # name check is needed to catch them.
        is_report = ("<!-- bf-module" in body
                     or "report" in title.lower())

        if is_tenant_match and (is_report or args.force_all):
            print(f"🎯 Found match: #{issue_num} - {title}")

            if args.dry_run:
                continue

            # 2. Delete the issue
            del_url = f'https://api.github.com/repos/{args.repo}/issues/{issue_num}'
            res = requests.delete(del_url, headers=headers)

            if res.status_code == 204:
                print(f"✅ Deleted #{issue_num}")
                deleted_count += 1
            else:
                print(f"❌ Failed to delete #{issue_num}: {res.status_code}")

    print(f"\n✨ Done. {'Would have deleted' if args.dry_run else 'Deleted'} {deleted_count} issues.")

if __name__ == "__main__":
    try:
        import requests
    except ImportError:
        print("❌ Error: 'requests' library not found. Please run: pip install requests")
        sys.exit(1)
    main()
