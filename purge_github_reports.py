"""Command-line tool that deletes GitHub issue "reports" filed for one tenant.

Lists the open issues of a repository, picks the ones that mention the tenant
and look like automated reports, and deletes them (or only lists them with
--dry-run).
"""
# requests performs the GitHub REST API calls.
import requests
# sys.exit reports failure to the calling shell.
import sys
# argparse reads the command-line options.
import argparse

# Configuration - Defaults
# Repository scanned when --repo is not given.
DEFAULT_REPO = "lbockenstedt/lbockenstedt" # Adjusted to owner/repo format

def main():
    """Parse options, find the tenant's report issues and delete them."""
    # Command-line options: credentials, target repo, tenant and safety switches.
    parser = argparse.ArgumentParser(description="Purge GitHub issues based on tenant and report markers.")
    parser.add_argument("--token", required=True, help="GitHub Personal Access Token")
    parser.add_argument("--repo", default=DEFAULT_REPO, help="Repository in 'owner/repo' format (default: lbockenstedt/lbockenstedt)")
    parser.add_argument("--tenant", required=True, help="The tenant name or ID to scan for in the issue body")
    parser.add_argument("--dry-run", action="store_true", help="List issues that would be deleted without actually deleting them")
    parser.add_argument("--force-all", action="store_true", help="Ignore report markers and delete any issue mentioning the tenant")

    # Parse argv into an args namespace.
    args = parser.parse_args()

    # GitHub auth header plus the v3 REST media type.
    headers = {
        'Authorization': f'token {args.token}',
        'Accept': 'application/vnd.github.v3+json'
    }

    # Tell the operator what is being scanned.
    print(f"🔍 Scanning {args.repo} for reports belonging to tenant: {args.tenant}...")
    # In dry-run mode nothing is deleted; say so up front.
    if args.dry_run:
        print("⚠️ DRY RUN MODE: No issues will be deleted.\n")

    # 1. Fetch open issues
    # Up to 100 open issues in one page (the API maximum per request).
    url = f'https://api.github.com/repos/{args.repo}/issues?state=open&per_page=100'
    response = requests.get(url, headers=headers)

    # Any non-200 answer means we cannot trust the list, so stop.
    if response.status_code != 200:
        print(f"❌ Error fetching issues: {response.status_code} - {response.text}")
        sys.exit(1)

    # The API should return a JSON list of issue objects.
    issues = response.json()
    # Anything else (e.g. an error object) is unexpected: stop rather than guess.
    if not isinstance(issues, list):
        print("❌ Unexpected API response format.")
        sys.exit(1)

    # Running total of issues deleted, for the summary line.
    deleted_count = 0

    # Examine every open issue returned.
    for issue in issues:
        # Skip Pull Requests (GitHub API returns PRs as issues)
        if 'pull_request' in issue:
            continue

        # Pull the fields used for matching; body can be null on GitHub.
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

        # Delete only tenant issues that are reports, unless --force-all widens the match.
        if is_tenant_match and (is_report or args.force_all):
            print(f"🎯 Found match: #{issue_num} - {title}")

            # Dry run: the match was printed above, skip the delete.
            if args.dry_run:
                continue

            # 2. Delete the issue
            del_url = f'https://api.github.com/repos/{args.repo}/issues/{issue_num}'
            res = requests.delete(del_url, headers=headers)

            # 204 No Content is GitHub's success response for a delete.
            if res.status_code == 204:
                print(f"✅ Deleted #{issue_num}")
                deleted_count += 1
            else:
                # Any other status means the delete was refused (e.g. missing permission).
                print(f"❌ Failed to delete #{issue_num}: {res.status_code}")

    # Final summary, worded for dry-run or real mode.
    print(f"\n✨ Done. {'Would have deleted' if args.dry_run else 'Deleted'} {deleted_count} issues.")

# Script entry point.
if __name__ == "__main__":
    # Give a friendly install hint if requests is missing.
    try:
        import requests
    except ImportError:
        # requests is not installed: explain how to fix it and exit with an error.
        print("❌ Error: 'requests' library not found. Please run: pip install requests")
        sys.exit(1)
    # Dependencies are present; run the tool.
    main()
