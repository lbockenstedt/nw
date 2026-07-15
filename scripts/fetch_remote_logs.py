import requests
import argparse
import json
import sys
from typing import Optional, List

def fetch_logs(base_url: str, module: Optional[str] = None):
    """
    Fetches logs from the Hub API.
    If module is provided, fetches module-specific logs.
    Otherwise, fetches general system logs.
    """
    endpoint = f"/setup/logs"
    if module:
        endpoint = f"/setup/logs/{module}"

    url = f"{base_url.rstrip('/')}{endpoint}"

    try:
        print(f"🌐 Fetching logs from {url}...")
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()

        logs = data.get("logs", [])
        if not logs:
            print("ℹ️ No logs found.")
            return ""

        return "\n".join(logs)

    except requests.exceptions.HTTPError as e:
        print(f"❌ HTTP Error: {e}")
        if e.response.status_code == 404:
            print(f"   Log for module '{module}' not found.")
        return f"Error: {e}"
    except requests.exceptions.RequestException as e:
        print(f"❌ Request Exception: {e}")
        return f"Error: {e}"
    except json.JSONDecodeError:
        print("❌ Failed to decode JSON response.")
        return "Error: Invalid JSON response"

def main():
    parser = argparse.ArgumentParser(description="Fetch logs from Hub API for AI Agent context")
    parser.add_argument("--host", default="172.16.1.30:8000", help="Hub API host and port")
    parser.add_argument("--module", help="Specific module to fetch logs for (e.g., opn, cppm)")

    args = parser.parse_args()
    base_url = f"http://{args.host}"

    logs = fetch_logs(base_url, args.module)

    if logs:
        print("\n--- START OF LOGS ---")
        print(logs)
        print("--- END OF LOGS ---\n")
    else:
        sys.exit(1)

if __name__ == "__main__":
    main()
