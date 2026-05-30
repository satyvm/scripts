"""Good First Issue Tracker

Monitors 830+ GitHub repositories for beginner-friendly issues
and sends Discord notifications in real-time.

Usage:
    uv run good-first-issues          # via script entrypoint
    uv run python -m scripts.good_first_issue_tracker   # direct module
"""

import requests
import time
import json
import os
import sys
import tomllib
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import quote

from dotenv import load_dotenv

# ==========================================
# PATHS — resolved relative to the project root
# ==========================================
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
DATA_TOML_FILE = DATA_DIR / "github_repos.toml"
CUSTOM_TOML_FILE = DATA_DIR / "custom.toml"
STATE_FILE = DATA_DIR / "last_run_state.json"
ENV_FILE = PROJECT_ROOT / ".env"

# ==========================================
# LOAD ENVIRONMENT
# ==========================================
load_dotenv(ENV_FILE)

WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "7200"))  # 5 minutes default
FIRST_RUN_LOOKBACK_HOURS = int(os.environ.get("FIRST_RUN_LOOKBACK_HOURS", "12"))

# Labels to track — any issue with at least one of these counts
LABELS = [
    "good first issue",
    "good-first-issue",
    "beginner",
    "beginner-friendly",
    "easy",
    "first-timers-only",
    "starter",
    "help wanted",
    "low-hanging-fruit",
]

HEADERS = {
    "Accept": "application/vnd.github.v3+json",
}
if GITHUB_TOKEN:
    HEADERS["Authorization"] = f"token {GITHUB_TOKEN}"


# ==========================================
# LOAD REPOS FROM data.toml
# ==========================================
def load_repos_from_toml(filepath: Path) -> list[str]:
    """Parse data.toml and extract owner/repo pairs.

    The file uses the format: 'github.com/owner/repo'
    We convert each entry to 'owner/repo'.
    """
    repos = []
    if not filepath.exists():
        print(f"⚠️  {filepath} not found. Skipping.")
        return repos

    with open(filepath, "r") as f:
        for line in f:
            line = line.strip().strip(",").strip("'\"").strip()
            if line.startswith("github.com/"):
                parts = line.replace("github.com/", "").split("/")
                if len(parts) >= 2:
                    repos.append(f"{parts[0]}/{parts[1]}")
    return repos


def load_custom_toml(filepath: Path) -> tuple[list[str], list[str]]:
    """Parse custom.toml and extract orgs and repos."""
    if not filepath.exists():
        print(f"⚠️  {filepath} not found. Using defaults.")
        return [], []
    with open(filepath, "rb") as f:
        data = tomllib.load(f)
    return data.get("orgs", []), data.get("repos", [])


def get_latest_org_repos(org: str, limit: int = 10) -> list[str]:
    """Fetch the most recently updated repositories for an organization."""
    url = f"https://api.github.com/orgs/{org}/repos?sort=updated&direction=desc&per_page={limit}"
    try:
        response = requests.get(url, headers=HEADERS, timeout=10)
        if response.status_code == 403:
            reset_time = response.headers.get("X-RateLimit-Reset")
            if reset_time:
                wait = max(int(reset_time) - int(time.time()), 1)
                print(f"  ⏳ Rate limited fetching org repos. Waiting {wait}s...")
                time.sleep(wait + 1)
                response = requests.get(url, headers=HEADERS, timeout=10)
        response.raise_for_status()
        return [repo["full_name"] for repo in response.json()]
    except requests.exceptions.RequestException as e:
        print(f"  ❌ Error fetching repos for org {org}: {e}")
        return []


def build_all_repos() -> tuple[list[str], list[str]]:
    """Combine custom.toml repos, custom.toml orgs, and data.toml repos into a deduplicated list."""
    toml_repos = load_repos_from_toml(DATA_TOML_FILE)
    custom_orgs, custom_repos = load_custom_toml(CUSTOM_TOML_FILE)
    
    all_repos = set(custom_repos) | set(toml_repos)
    all_orgs = sorted(set(custom_orgs))
    
    if all_orgs:
        print(f"🏢 Resolving {len(all_orgs)} orgs to their latest 10 repos...")
        for org in all_orgs:
            latest_repos = get_latest_org_repos(org, limit=10)
            all_repos.update(latest_repos)
            time.sleep(0.5)  # slight pause to avoid rate limits
            
    print(f"📦 Tracking {len(all_repos)} repositories total (orgs resolved)")
    return sorted(all_repos), []


# ==========================================
# STATE MANAGEMENT
# ==========================================
def _read_state() -> dict:
    """Read the full state dict from disk."""
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return {}


def get_last_check_time() -> str:
    """Get the last check timestamp.

    On first run (no state file), defaults to FIRST_RUN_LOOKBACK_HOURS ago
    so we catch any recent issues created while the script was down.
    """
    state = _read_state()
    ts = state.get("last_checked")
    if ts:
        return ts

    default_time = datetime.now(timezone.utc) - timedelta(hours=FIRST_RUN_LOOKBACK_HOURS)
    return default_time.strftime("%Y-%m-%dT%H:%M:%SZ")


def load_seen_issues() -> set[int]:
    """Load the set of already-notified issue IDs to prevent duplicates."""
    state = _read_state()
    return set(state.get("seen_ids", []))


def save_state(timestamp_str: str, seen_ids: set[int]) -> None:
    """Save both timestamp and seen issue IDs."""
    # Trim to prevent unbounded growth
    ids = list(seen_ids)[-10_000:] if len(seen_ids) > 10_000 else list(seen_ids)

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump({"last_checked": timestamp_str, "seen_ids": ids}, f)


# ==========================================
# DISCORD NOTIFICATION
# ==========================================
def send_to_discord(issue: dict) -> None:
    """Sends a rich embed to Discord with repo link + issue link."""
    try:
        repo_full_name = "/".join(issue["repository_url"].split("/")[-2:])
    except (KeyError, IndexError):
        repo_full_name = "Unknown/Repository"

    repo_url = f"https://github.com/{repo_full_name}"
    issue_url = issue["html_url"]

    body = issue.get("body") or ""
    if len(body) > 300:
        body = body[:297] + "..."

    labels_str = ", ".join(
        f"`{label['name']}`" for label in issue.get("labels", [])
    )

    embed = {
        "title": f"🆕 {issue['title']}",
        "url": issue_url,
        "color": 0x58B09C,
        "author": {
            "name": issue["user"]["login"],
            "url": issue["user"]["html_url"],
            "icon_url": issue["user"]["avatar_url"],
        },
        "fields": [
            {"name": "📁 Repository", "value": f"[{repo_full_name}]({repo_url})", "inline": True},
            {"name": "🔗 Issue Link", "value": f"[#{issue['number']}]({issue_url})", "inline": True},
            {"name": "🏷️ Labels", "value": labels_str or "None", "inline": False},
        ],
        "footer": {"text": "Good First Issue Tracker"},
        "timestamp": issue["created_at"],
    }

    if body:
        embed["description"] = body

    payload = {"embeds": [embed]}

    try:
        response = requests.post(WEBHOOK_URL, json=payload, timeout=10)
        if response.status_code in (200, 204):
            print(f"  ✅ Notified: {issue['title']} ({repo_full_name})")
        elif response.status_code == 429:
            retry_after = response.json().get("retry_after", 5)
            print(f"  ⏳ Discord rate-limited. Waiting {retry_after}s...")
            time.sleep(retry_after)
            response = requests.post(WEBHOOK_URL, json=payload, timeout=10)
            if response.status_code in (200, 204):
                print(f"  ✅ Notified (retry): {issue['title']} ({repo_full_name})")
            else:
                print(f"  ❌ Failed after retry: {response.status_code}")
        else:
            print(f"  ❌ Discord error: {response.status_code} — {response.text[:200]}")
    except requests.exceptions.RequestException as e:
        print(f"  ❌ Network error sending to Discord: {e}")


def send_startup_message() -> None:
    """Send a startup test message to Discord."""
    payload = {
        "content": "🚀 **Good First Issue Tracker** has started up! Checking for new issues..."
    }
    try:
        response = requests.post(WEBHOOK_URL, json=payload, timeout=10)
        if response.status_code in (200, 204):
            print("  ✅ Sent startup test message to Discord")
        else:
            print(f"  ❌ Failed to send startup message: {response.status_code} — {response.text[:200]}")
    except requests.exceptions.RequestException as e:
        print(f"  ❌ Network error sending startup message: {e}")


# ==========================================
# GITHUB SEARCH
# ==========================================
def search_github(entity_query: str, label: str, since: str) -> list[dict]:
    """Execute a single GitHub search API call with pagination."""
    label_q = f'label:"{label}"'
    query = f"is:issue is:open {label_q} {entity_query} created:>{since}"

    all_items = []
    page = 1

    while True:
        url = (
            f"https://api.github.com/search/issues"
            f"?q={quote(query, safe='')}"
            f"&sort=created&order=asc&per_page=100&page={page}"
        )

        try:
            response = requests.get(url, headers=HEADERS, timeout=30)

            if response.status_code == 403:
                reset_time = response.headers.get("X-RateLimit-Reset")
                if reset_time:
                    wait = max(int(reset_time) - int(time.time()), 1)
                    print(f"  ⏳ Rate limited. Waiting {wait}s...")
                    time.sleep(wait + 1)
                    continue
                else:
                    print("  ⏳ Rate limited (no reset header). Waiting 60s...")
                    time.sleep(60)
                    continue

            response.raise_for_status()
            data = response.json()

            items = data.get("items", [])
            all_items.extend(items)

            total_count = data.get("total_count", 0)
            if len(all_items) >= total_count or not items:
                break

            page += 1
            time.sleep(1)

        except requests.exceptions.RequestException as e:
            print(f"  ❌ Search error: {e}")
            break

    return all_items


def check_for_issues(all_repos: list[str], all_orgs: list[str]) -> None:
    """Main check cycle: search all repos/orgs for new good-first-issues."""
    last_check = get_last_check_time()
    seen_ids = load_seen_issues()

    current_time = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(
        f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] "
        f"Scanning for issues created after {last_check}..."
    )

    # Build search entities
    all_entities = [f"org:{org}" for org in all_orgs] + [f"repo:{repo}" for repo in all_repos]

    # GitHub search has a query length limit (~256 chars for qualifiers).
    chunk_size = 4
    entity_chunks = [
        all_entities[i : i + chunk_size]
        for i in range(0, len(all_entities), chunk_size)
    ]

    all_new_issues: dict[int, dict] = {}
    total_chunks = len(entity_chunks) * len(LABELS)
    processed = 0

    for label in LABELS:
        for chunk in entity_chunks:
            processed += 1
            entity_query = " ".join(chunk)

            if processed % 50 == 0:
                print(f"  📊 Progress: {processed}/{total_chunks} search queries...")

            items = search_github(entity_query, label, last_check)

            for item in items:
                issue_id = item["id"]
                if issue_id not in all_new_issues and issue_id not in seen_ids:
                    all_new_issues[issue_id] = item
                    # Send immediately
                    send_to_discord(item)
                    seen_ids.add(issue_id)
                    save_state(current_time, seen_ids)
                    time.sleep(1)  # Prevent Discord webhook rate-limiting

            time.sleep(2)  # Respect GitHub's secondary rate limit

    # Summary log
    if not all_new_issues:
        print(f"  ℹ️  No new issues found. ({len(seen_ids)} previously seen)")
    else:
        print(f"  🎯 Total of {len(all_new_issues)} new issue(s) found and notified in this cycle.")

    # Final state save to capture the current timestamp even if no new issues
    save_state(current_time, seen_ids)


# ==========================================
# ENTRYPOINT
# ==========================================
def main():
    """Main entrypoint — called by `uv run good-first-issues`."""
    if not WEBHOOK_URL or WEBHOOK_URL == "your_discord_webhook_url_here":
        print("❌ DISCORD_WEBHOOK_URL is not set. Configure your .env file.")
        print(f"   Expected at: {ENV_FILE}")
        sys.exit(1)

    if not GITHUB_TOKEN or GITHUB_TOKEN == "your_github_token_here":
        print("⚠️  GITHUB_TOKEN is not set. You will hit rate limits very quickly.")

    print("🚀 Good First Issue Tracker — Starting up...")
    print(f"⏱️  Poll interval: {POLL_INTERVAL}s ({POLL_INTERVAL // 60} min)")
    print(f"🕐 First-run lookback: {FIRST_RUN_LOOKBACK_HOURS} hours")
    print(f"📂 Data dir: {DATA_DIR}")
    print()

    all_repos, all_orgs = build_all_repos()

    total_queries = ((len(all_repos) + len(all_orgs) + 3) // 4) * len(LABELS)
    print(f"📡 ~{total_queries} API queries per cycle")

    if not GITHUB_TOKEN:
        print("⚠️  Without a token, GitHub allows only 10 search requests/min.")
        print("   You WILL be rate-limited. Set GITHUB_TOKEN in .env!")
    else:
        print("🔑 Authenticated — 30 search requests/min available.")

    print("=" * 60)

    send_startup_message()

    while True:
        try:
            check_for_issues(all_repos, all_orgs)
        except KeyboardInterrupt:
            print("\n👋 Shutting down gracefully.")
            sys.exit(0)
        except Exception as e:
            print(f"❌ Unexpected error: {e}")
            import traceback
            traceback.print_exc()

        print(f"💤 Next check in {POLL_INTERVAL // 60} min {POLL_INTERVAL % 60}s...\n")
        try:
            time.sleep(POLL_INTERVAL)
        except KeyboardInterrupt:
            print("\n👋 Shutting down gracefully.")
            sys.exit(0)


if __name__ == "__main__":
    main()
