"""List open GitHub issues labelled 'automated-fix' across all local git repos.

Walks the current directory tree, finds git checkouts with a GitHub origin and
asks the gh CLI for their 'automated-fix' issues, printing them as text or JSON.
"""
# os.walk finds git repositories under the current directory.
import os
# subprocess runs git and the gh CLI.
import subprocess
# json parses gh output and prints --json output.
import json
# argparse reads the --json option.
import argparse
# Type hints for the function signatures.
from typing import List, Dict, Optional

def get_repo_name(path: str) -> Optional[str]:
    """Gets the owner/repo name from a git directory."""
    # Any git failure (not a repo, no origin) simply means "no repo name".
    try:
        # Ask git for the URL of the 'origin' remote.
        result = subprocess.run(
            ["git", "-C", path, "remote", "get-url", "origin"],
            capture_output=True, text=True, check=True
        )
        # Remove the trailing newline from git's output.
        url = result.stdout.strip()
        # Only GitHub remotes can be queried with gh.
        if "github.com" in url:
            # Extract owner/repo from https://github.com/owner/repo.git
            parts = url.split("github.com/")[-1].split(".git")[0].split("/")
            # Need both owner and repo parts to build "owner/repo".
            if len(parts) >= 2:
                return f"{parts[0]}/{parts[1]}"
    except Exception:
        # Not a GitHub repo or git failed: fall through and return None.
        pass
    # No usable GitHub repository found.
    return None

def get_pending_ai_issues():
    """Fetches issues labeled 'automated-fix' across all detected git repos."""
    # Collected issues from every repo found.
    all_issues = []

    # Find all directories in the current path that are git repos
    # We look for directories containing a .git folder
    for root, dirs, files in os.walk("."):
        if ".git" in dirs:
            # Resolve the GitHub "owner/repo" for this checkout.
            repo_name = get_repo_name(root)
            if repo_name:
                # Query GitHub API for this specific repo
                cmd = ["gh", "issue", "list", "-R", repo_name, "--label", "automated-fix", "--json", "number,title,body,url"]
                # Run gh and capture its JSON output.
                result = subprocess.run(cmd, capture_output=True, text=True)
                # gh succeeded: parse its issue list.
                if result.returncode == 0:
                    try:
                        issues = json.loads(result.stdout)
                        # Add repo name to each issue for context
                        for i in issues:
                            i["repo"] = repo_name
                        all_issues.extend(issues)
                    except json.JSONDecodeError:
                        # Malformed gh output: skip this repo rather than fail the whole scan.
                        pass
            # Don't recurse into .git folders
            if ".git" in dirs:
                dirs.remove(".git")

    # Combined list from all repos.
    return all_issues

def main():
    """Parse options, collect pending issues and print them."""
    # Single option: emit machine-readable JSON instead of text.
    parser = argparse.ArgumentParser(description="Poll GitHub issues for automated fixes")
    parser.add_argument("--json", action="store_true", help="Output issues as JSON")

    # Parse argv into an args namespace.
    args = parser.parse_args()

    # Gather issues from every repo under the current directory.
    issues = get_pending_ai_issues()

    # JSON mode: print the raw list for another program and stop.
    if args.json:
        print(json.dumps(issues))
        return

    # Text mode: human-readable listing.
    print("🔍 Scanning all detected repositories for issues labeled 'automated-fix'...")
    # Nothing to do: say so and stop.
    if not issues:
        print("No pending automated fixes found. 😴")
        return

    # Print a block per issue with a separator line.
    print(f"Found {len(issues)} issues to work on:\n")
    for issue in issues:
        print(f"Repo: {issue['repo']}")
        print(f"#{issue['number']} - {issue['title']}")
        print(f"URL: {issue['url']}")
        print(f"Description: {issue['body']}")
        print("-" * 40)

    # Hint on how to hand an issue to the coding assistant.
    print("\n💡 You can now tell Claude Code: 'Fix issue #<number> from the list above'")

# Script entry point.
if __name__ == "__main__":
    main()
