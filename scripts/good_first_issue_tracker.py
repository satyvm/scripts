"""Good First Issue Tracker

Monitors 3000+ GitHub repositories for beginner-friendly issues
and sends Discord notifications in real-time.

Optimized for high throughput with concurrent API calls, adaptive
rate limiting, and batched operations.

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
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
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

# Allow STATE_FILE to be overridden by an environment variable (crucial for Coolify/Docker deployments)
STATE_FILE_ENV = os.environ.get("STATE_FILE")
if STATE_FILE_ENV:
    STATE_FILE = Path(STATE_FILE_ENV)
else:
    STATE_FILE = DATA_DIR / "last_run_state.json"

ENV_FILE = PROJECT_ROOT / ".env"

# ==========================================
# LOAD ENVIRONMENT
# ==========================================
load_dotenv(ENV_FILE)

WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "7200")) 
FIRST_RUN_LOOKBACK_HOURS = int(os.environ.get("FIRST_RUN_LOOKBACK_HOURS", "12"))

# ── Concurrency settings ──
MAX_GITHUB_WORKERS = int(os.environ.get("MAX_GITHUB_WORKERS", "5"))
CHUNK_SIZE = int(os.environ.get("CHUNK_SIZE", "50"))
ORG_REPO_LIMIT = int(os.environ.get("ORG_REPO_LIMIT", "10"))

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
    # Intermediate / slightly advanced labels
    "good second issue",
    "good-second-issue",
    "medium",
    "intermediate",
    "difficulty/medium",
    "difficulty/intermediate",
    "difficulty: medium",
    "difficulty: intermediate",
    "moderate",
    "size/medium",
    "up-for-grabs",
]

# Pre-compute a frozenset for fast O(1) label lookups
_LABELS_SET = frozenset(LABELS)

HEADERS = {
    "Accept": "application/vnd.github.v3+json",
}
if GITHUB_TOKEN:
    HEADERS["Authorization"] = f"token {GITHUB_TOKEN}"


# ==========================================
# THREAD-SAFE RATE LIMITER
# ==========================================
class GitHubRateLimiter:
    """Tracks GitHub search API rate limits using response headers.
    
    GitHub allows 30 search requests/min (authenticated) or 10/min (unauthenticated).
    This class ensures we stay under the limit by sleeping only when necessary.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._remaining = 30 if GITHUB_TOKEN else 10
        self._reset_time = 0.0
        self._min_interval = 0.25  # Minimum gap between requests (250ms) — prevents secondary abuse detection
        self._last_request_time = 0.0

    def update_from_headers(self, headers: dict) -> None:
        """Update rate limit state from GitHub response headers."""
        with self._lock:
            remaining = headers.get("X-RateLimit-Remaining")
            reset_time = headers.get("X-RateLimit-Reset")
            if remaining is not None:
                self._remaining = int(remaining)
            if reset_time is not None:
                self._reset_time = float(reset_time)

    def wait_if_needed(self) -> None:
        """Sleep only when we're approaching rate limits."""
        with self._lock:
            now = time.time()

            # Enforce minimum interval between requests
            elapsed_since_last = now - self._last_request_time
            if elapsed_since_last < self._min_interval:
                time.sleep(self._min_interval - elapsed_since_last)

            # If we have plenty of remaining requests, proceed quickly
            if self._remaining > 5:
                self._last_request_time = time.time()
                return

            # If we're low on remaining requests, wait for reset
            if self._remaining <= 2 and self._reset_time > now:
                wait_time = self._reset_time - now + 1
                print(f"  ⏳ Rate limit low ({self._remaining} remaining). Waiting {wait_time:.0f}s for reset...")
                time.sleep(wait_time)

            # Moderate remaining — spread requests over time until reset
            elif self._remaining <= 5 and self._reset_time > now:
                time_until_reset = self._reset_time - now
                spacing = time_until_reset / max(self._remaining, 1)
                sleep_time = min(spacing, 5.0)  # Cap at 5s
                time.sleep(sleep_time)

            self._last_request_time = time.time()

    def handle_rate_limit_response(self, response: requests.Response) -> float:
        """Handle a 403 rate limit response. Returns seconds to wait."""
        reset_time = response.headers.get("X-RateLimit-Reset")
        if reset_time:
            wait = max(float(reset_time) - time.time(), 1)
            with self._lock:
                self._remaining = 0
                self._reset_time = float(reset_time)
            return wait + 1
        return 60.0  # Default fallback


# Global rate limiter instance
_rate_limiter = GitHubRateLimiter()


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
        response = requests.get(url, headers=HEADERS, timeout=15)
        if response.status_code == 403:
            wait = _rate_limiter.handle_rate_limit_response(response)
            print(f"  ⏳ Rate limited fetching org repos for {org}. Waiting {wait:.0f}s...")
            time.sleep(wait)
            response = requests.get(url, headers=HEADERS, timeout=15)
        response.raise_for_status()
        _rate_limiter.update_from_headers(response.headers)
        return [repo["full_name"] for repo in response.json()]
    except requests.exceptions.RequestException as e:
        print(f"  ❌ Error fetching repos for org {org}: {e}")
        return []


def _fetch_org_repos_parallel(orgs: list[str], limit: int) -> set[str]:
    """Fetch repos for all orgs concurrently."""
    all_repos: set[str] = set()
    with ThreadPoolExecutor(max_workers=min(5, len(orgs))) as executor:
        futures = {
            executor.submit(get_latest_org_repos, org, limit): org
            for org in orgs
        }
        for future in as_completed(futures):
            org = futures[future]
            try:
                repos = future.result()
                all_repos.update(repos)
                if repos:
                    print(f"  ✅ {org}: {len(repos)} repos")
            except Exception as e:
                print(f"  ❌ Error fetching {org}: {e}")
    return all_repos


def build_all_repos() -> tuple[list[str], list[str]]:
    """Combine custom.toml repos and data.toml repos into a deduplicated list.

    Orgs are returned separately for efficient org: search queries
    rather than being resolved into individual repo: queries.
    """
    toml_repos = load_repos_from_toml(DATA_TOML_FILE)
    custom_orgs, custom_repos = load_custom_toml(CUSTOM_TOML_FILE)

    all_repos = sorted(set(custom_repos) | set(toml_repos))
    all_orgs = sorted(set(custom_orgs))

    print(f"📦 Tracking {len(all_repos)} repositories + {len(all_orgs)} orgs")
    return all_repos, all_orgs


# ==========================================
# STATE MANAGEMENT
# ==========================================
_state_lock = threading.Lock()


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


def load_seen_issues() -> dict:
    """Load the set of already-notified issue IDs to prevent duplicates."""
    state = _read_state()
    return dict.fromkeys(state.get("seen_ids", []))


def save_state(timestamp_str: str, seen_ids: dict) -> None:
    """Save both timestamp and seen issue IDs (thread-safe)."""
    with _state_lock:
        # Trim to prevent unbounded growth
        ids = list(seen_ids.keys())[-10_000:] if len(seen_ids) > 10_000 else list(seen_ids.keys())

        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(STATE_FILE, "w") as f:
            json.dump({"last_checked": timestamp_str, "seen_ids": ids}, f)


# ==========================================
# DISCORD NOTIFICATION
# ==========================================
_discord_lock = threading.Lock()


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

    with _discord_lock:
        try:
            response = requests.post(WEBHOOK_URL, json=payload, timeout=10)
            if response.status_code in (200, 204):
                print(f"  ✅ Notified: {issue['title'][:60]} ({repo_full_name})")
            elif response.status_code == 429:
                retry_after = response.json().get("retry_after", 5)
                print(f"  ⏳ Discord rate-limited. Waiting {retry_after}s...")
                time.sleep(retry_after)
                response = requests.post(WEBHOOK_URL, json=payload, timeout=10)
                if response.status_code in (200, 204):
                    print(f"  ✅ Notified (retry): {issue['title'][:60]} ({repo_full_name})")
                else:
                    print(f"  ❌ Failed after retry: {response.status_code}")
            else:
                print(f"  ❌ Discord error: {response.status_code} — {response.text[:200]}")
        except requests.exceptions.RequestException as e:
            print(f"  ❌ Network error sending to Discord: {e}")
        
        # Minimal delay between Discord messages to avoid rate limits
        # Discord webhook rate limit is ~30 messages/60s per webhook
        time.sleep(0.5)


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
def _create_session() -> requests.Session:
    """Create a reusable HTTP session with connection pooling."""
    session = requests.Session()
    session.headers.update(HEADERS)
    # Increase connection pool size for concurrent requests
    adapter = requests.adapters.HTTPAdapter(
        pool_connections=MAX_GITHUB_WORKERS,
        pool_maxsize=MAX_GITHUB_WORKERS + 5,
        max_retries=requests.adapters.Retry(
            total=2,
            backoff_factor=1,
            status_forcelist=[500, 502, 503, 504],
        ),
    )
    session.mount("https://", adapter)
    return session


def search_github_chunk(
    session: requests.Session,
    entity_query: str,
    since: str,
    chunk_index: int,
    total_chunks: int,
) -> list[dict]:
    """Execute a single GitHub search API call with pagination for one chunk.
    
    Returns all matching issues for this chunk of repos.
    """
    # No label filter in query — we filter in Python for efficiency
    query = f"is:issue is:open {entity_query} created:>{since}"

    all_items = []
    page = 1

    while True:
        _rate_limiter.wait_if_needed()

        url = (
            f"https://api.github.com/search/issues"
            f"?q={quote(query, safe='')}"
            f"&sort=created&order=asc&per_page=100&page={page}"
        )

        try:
            response = session.get(url, timeout=30)

            if response.status_code == 403:
                # Check for secondary rate limit (Retry-After header)
                retry_after = response.headers.get("Retry-After")
                if retry_after:
                    wait = int(retry_after) + 1
                    print(f"  ⏳ Secondary rate limit (chunk {chunk_index}/{total_chunks}). Waiting {wait}s...")
                    time.sleep(wait)
                    continue
                wait = _rate_limiter.handle_rate_limit_response(response)
                print(f"  ⏳ Rate limited (chunk {chunk_index}/{total_chunks}). Waiting {wait:.0f}s...")
                time.sleep(wait)
                continue

            if response.status_code == 422:
                # Query too complex — GitHub rejected it
                print(f"  ⚠️  Chunk {chunk_index}: query too complex, skipping")
                break

            response.raise_for_status()
            _rate_limiter.update_from_headers(response.headers)

            data = response.json()
            items = data.get("items", [])
            all_items.extend(items)

            total_count = data.get("total_count", 0)
            if len(all_items) >= total_count or not items:
                break

            page += 1

        except requests.exceptions.RequestException as e:
            print(f"  ❌ Search error (chunk {chunk_index}): {e}")
            break

    return all_items


def check_for_issues(all_repos: list[str], all_orgs: list[str]) -> None:
    """Main check cycle: search all repos/orgs for new good-first-issues.
    
    Uses concurrent requests with ThreadPoolExecutor for ~10x speedup.
    """
    last_check = get_last_check_time()
    seen_ids = load_seen_issues()

    current_time = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(
        f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] "
        f"Scanning for issues created after {last_check}..."
    )

    # Build search entities
    all_entities = [f"org:{org}" for org in all_orgs] + [f"repo:{repo}" for repo in all_repos]

    # Chunk into groups — larger chunks = fewer API calls
    entity_chunks = [
        all_entities[i : i + CHUNK_SIZE]
        for i in range(0, len(all_entities), CHUNK_SIZE)
    ]

    total_chunks = len(entity_chunks)
    print(f"  📊 {len(all_entities)} entities → {total_chunks} chunks (size={CHUNK_SIZE}), {MAX_GITHUB_WORKERS} workers")

    all_new_issues: dict[int, dict] = {}
    processed = 0
    processed_lock = threading.Lock()

    session = _create_session()

    def process_chunk(chunk_data: tuple[int, list[str]]) -> list[dict]:
        """Process a single chunk — called by thread pool."""
        nonlocal processed
        idx, chunk = chunk_data
        entity_query = " ".join(chunk)

        items = search_github_chunk(session, entity_query, last_check, idx + 1, total_chunks)

        with processed_lock:
            processed += 1
            if processed % 5 == 0 or processed == total_chunks:
                print(f"  📊 Progress: {processed}/{total_chunks} chunks completed...")

        return items

    # Execute all chunks concurrently
    with ThreadPoolExecutor(max_workers=MAX_GITHUB_WORKERS) as executor:
        futures = {
            executor.submit(process_chunk, (i, chunk)): i
            for i, chunk in enumerate(entity_chunks)
        }

        for future in as_completed(futures):
            chunk_idx = futures[future]
            try:
                items = future.result()
            except Exception as e:
                print(f"  ❌ Chunk {chunk_idx} failed: {e}")
                continue

            # Process items from this chunk — filter labels + deduplicate
            for item in items:
                item_labels = {lbl.get("name", "").lower() for lbl in item.get("labels", [])}
                if not item_labels & _LABELS_SET:
                    continue

                issue_id = item["id"]
                if issue_id not in all_new_issues and issue_id not in seen_ids:
                    all_new_issues[issue_id] = item
                    send_to_discord(item)
                    seen_ids[issue_id] = None

        # Save state once after all chunks are processed
        save_state(current_time, seen_ids)

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
        print("❌ DISCORD_WEBHOOK_URL is not set.")
        print("   If running locally, configure it in your .env file.")
        print("   If running on Coolify/Docker, ensure it is set in the Environment Variables tab.")
        sys.exit(1)

    if not GITHUB_TOKEN or GITHUB_TOKEN == "your_github_token_here":
        print("⚠️  GITHUB_TOKEN is not set. You will hit rate limits very quickly.")

    print("🚀 Good First Issue Tracker — Starting up (optimized)...")
    print(f"⏱️  Poll interval: {POLL_INTERVAL}s ({POLL_INTERVAL // 60} min)")
    print(f"🕐 First-run lookback: {FIRST_RUN_LOOKBACK_HOURS} hours")
    print(f"📂 Data dir: {DATA_DIR}")
    print(f"⚡ Concurrency: {MAX_GITHUB_WORKERS} workers, chunk size {CHUNK_SIZE}")
    print()

    all_repos, all_orgs = build_all_repos()

    total_entities = len(all_repos) + len(all_orgs)
    total_queries = (total_entities + CHUNK_SIZE - 1) // CHUNK_SIZE
    print(f"📡 ~{total_queries} API queries per cycle ({len(all_repos)} repos + {len(all_orgs)} orgs)")

    if not GITHUB_TOKEN:
        print("⚠️  Without a token, GitHub allows only 10 search requests/min.")
        print("   You WILL be rate-limited. Set GITHUB_TOKEN in .env!")
    else:
        print("🔑 Authenticated — 30 search requests/min available.")

    print("=" * 60)

    send_startup_message()

    while True:
        start_time = time.time()
        try:
            check_for_issues(all_repos, all_orgs)
        except KeyboardInterrupt:
            print("\n👋 Shutting down gracefully.")
            sys.exit(0)
        except Exception as e:
            print(f"❌ Unexpected error: {e}")
            import traceback
            traceback.print_exc()

        elapsed = time.time() - start_time
        sleep_time = max(0.0, POLL_INTERVAL - elapsed)

        print(f"💤 Scan took {elapsed:.1f}s. Next check in {int(sleep_time) // 60} min {int(sleep_time) % 60}s...\n")
        try:
            time.sleep(sleep_time)
        except KeyboardInterrupt:
            print("\n👋 Shutting down gracefully.")
            sys.exit(0)


if __name__ == "__main__":
    main()
