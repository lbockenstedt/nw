"""Command-line helper that prints system or module logs from the Hub's HTTP API.

Used to give an AI agent (or a human) quick log context without logging in to
the Hub host.
"""
# requests performs the HTTP GET against the Hub API.
import requests
# argparse reads the --host/--module options.
import argparse
# json is needed only to catch an invalid-JSON response.
import json
# sys.exit signals failure to the calling shell.
import sys
# Type hints for the function signatures.
from typing import Optional, List

def fetch_logs(base_url: str, module: Optional[str] = None):
    """
    Fetches logs from the Hub API.
    If module is provided, fetches module-specific logs.
    Otherwise, fetches general system logs.
    """
    # Default to the Hub's general system log endpoint.
    endpoint = f"/setup/logs"
    # A module name selects that module's own log instead.
    if module:
        endpoint = f"/setup/logs/{module}"

    # Join the base URL and endpoint, avoiding a double slash.
    url = f"{base_url.rstrip('/')}{endpoint}"

    # Any network or decode failure is turned into an "Error: ..." string below.
    try:
        # Fetch with a 10 s timeout so a dead Hub cannot hang the script.
        print(f"🌐 Fetching logs from {url}...")
        response = requests.get(url, timeout=10)
        # Raise for 4xx/5xx so HTTP errors are handled in the except below.
        response.raise_for_status()
        data = response.json()

        # The Hub returns {"logs": [line, ...]}.
        logs = data.get("logs", [])
        # Nothing to show: return an empty string so the caller exits non-zero.
        if not logs:
            print("ℹ️ No logs found.")
            return ""

        # Rejoin the log lines into one printable block.
        return "\n".join(logs)

    except requests.exceptions.HTTPError as e:
        # Server answered with an error status.
        print(f"❌ HTTP Error: {e}")
        # 404 means the requested module has no log on the Hub.
        if e.response.status_code == 404:
            print(f"   Log for module '{module}' not found.")
        return f"Error: {e}"
    except requests.exceptions.RequestException as e:
        # Connection refused, timeout, DNS failure and similar transport errors.
        print(f"❌ Request Exception: {e}")
        return f"Error: {e}"
    except json.JSONDecodeError:
        # Server answered 200 but the body was not JSON.
        print("❌ Failed to decode JSON response.")
        return "Error: Invalid JSON response"

def main():
    """Parse options, fetch the logs and print them between clear markers."""
    # Options: Hub host:port and an optional module name.
    parser = argparse.ArgumentParser(description="Fetch logs from Hub API for AI Agent context")
    parser.add_argument("--host", default="172.16.1.30:8000", help="Hub API host and port")
    parser.add_argument("--module", help="Specific module to fetch logs for (e.g., opn, cppm)")

    # Parse argv and build the Hub base URL (plain HTTP on the LAN).
    args = parser.parse_args()
    base_url = f"http://{args.host}"

    # Fetch the requested logs.
    logs = fetch_logs(base_url, args.module)

    # Print the logs between markers so they are easy to copy.
    if logs:
        print("\n--- START OF LOGS ---")
        print(logs)
        print("--- END OF LOGS ---\n")
    else:
        # No logs (or an error): exit non-zero so callers can detect it.
        sys.exit(1)

# Script entry point.
if __name__ == "__main__":
    main()
