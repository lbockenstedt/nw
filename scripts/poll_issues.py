import os
import subprocess
import json
import argparse
from typing import List, Dict, Optional

def get_repo_name(path: str) -> Optional[str]:
    """Gets the owner/repo name from a git directory."""
    try:
        result = subprocess.run(
            ["git", "-C", path, "remote", "get-url", "origin"],
            capture_output=True, text=True, check=True
        )
        url = result.stdout.strip()
        if "github.com" in url:
            # Extract owner/repo from https://github.com/owner/repo.git
            parts = url.split("github.com/")[-1].split(".git")[0].split("/")
            if len(parts) >= 2:
                return f"{parts[0]}/{parts[1]}"
    except Exception:
        pass
    return None

def get_pending_ai_issues():
    """Fetches issues labeled 'automated-fix' across all detected git repos."""
    all_issues = []

    # Find all directories in the current path that are git repos
    # We look for directories containing a .git folder
    for root, dirs, files in os.walk("."):
        if ".git" in dirs:
            repo_name = get_repo_name(root)
            if repo_name:
                # Query GitHub API for this specific repo
                cmd = ["gh", "issue", "list", "-R", repo_name, "--label", "automated-fix", "--json", "number,title,body,url"]
                result = subprocess.run(cmd, capture_output=True, text=True)
                if result.returncode == 0:
                    try:
                        issues = json.loads(result.stdout)
                        # Add repo name to each issue for context
                        for i in issues:
                            i["repo"] = repo_name
                        all_issues.extend(issues)
                    except json.JSONDecodeError:
                        pass
            # Don't recurse into .git folders
            if ".git" in dirs:
                dirs.remove(".git")

    return all_issues

def main():
    parser = argparse.ArgumentParser(description="Poll GitHub issues for automated fixes")
    parser.add_argument("--json", action="store_true", help="Output issues as JSON")

    args = parser.parse_args()

    issues = get_pending_ai_issues()

    if args.json:
        print(json.dumps(issues))
        return

    print("🔍 Scanning all detected repositories for issues labeled 'automated-fix'...")
    if not issues:
        print("No pending automated fixes found. 😴")
        return

    print(f"Found {len(issues)} issues to work on:\n")
    for issue in issues:
        print(f"Repo: {issue['repo']}")
        print(f"#{issue['number']} - {issue['title']}")
        print(f"URL: {issue['url']}")
        print(f"Description: {issue['body']}")
        print("-" * 40)

    print("\n💡 You can now tell Claude Code: 'Fix issue #<number> from the list above'")

if __name__ == "__main__":
    main()
