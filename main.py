"""server-scripts — main entrypoint

Run individual scripts by name:
    uv run python main.py good-first-issues
    uv run python main.py <script-name>

Or run scripts directly via uv entrypoints:
    uv run good-first-issues

Start the dashboard + all scripts:
    uv run python main.py serve
"""

import os
import sys

SCRIPTS = {
    "good-first-issues": {
        "module": "scripts.good_first_issue_tracker",
        "description": "Monitor GitHub repos for beginner-friendly issues → Discord",
    },
    # ── Add new scripts here ──
    # "example": {
    #     "module": "scripts.example_script",
    #     "description": "Description of what it does",
    # },
}


def print_help():
    print("server-scripts — available commands:\n")
    print(f"  {'serve':<25} Start dashboard + all scripts in managed mode")
    print()
    print("  Available scripts:")
    for name, info in SCRIPTS.items():
        print(f"    {name:<23} {info['description']}")
    print(f"\nUsage: uv run python main.py <script-name|serve>")
    print(f"   or: uv run <script-name>  (if entrypoint is configured)")


def run_script(name: str):
    if name not in SCRIPTS:
        print(f"❌ Unknown script: '{name}'")
        print()
        print_help()
        sys.exit(1)

    module_path = SCRIPTS[name]["module"]
    print(f"▶ Running: {name} ({module_path})\n")

    # Dynamically import and call main()
    import importlib
    module = importlib.import_module(module_path)
    module.main()


def serve():
    """Start the dashboard server with all scripts running in managed mode."""
    from dotenv import load_dotenv
    from pathlib import Path

    # Load environment
    env_file = Path(__file__).resolve().parent / ".env"
    load_dotenv(env_file)

    dashboard_token = os.environ.get("DASHBOARD_TOKEN", "")
    if not dashboard_token:
        print("❌ DASHBOARD_TOKEN is not set.")
        print("   Set it in .env or as an environment variable.")
        print("   This is required since the dashboard runs on a public domain.")
        sys.exit(1)

    port = int(os.environ.get("DASHBOARD_PORT", "8080"))
    poll_interval = int(os.environ.get("POLL_INTERVAL", "7200"))

    print("=" * 60)
    print("⚡ Script Dashboard — Starting up")
    print("=" * 60)
    print(f"  🌐 Dashboard: http://0.0.0.0:{port}")
    print(f"  🔑 Auth: Bearer token required")
    print(f"  ⏱️  Poll interval: {poll_interval}s ({poll_interval // 60} min)")
    print(f"  📦 Scripts: {len(SCRIPTS)}")
    for name, info in SCRIPTS.items():
        print(f"     • {name}: {info['description']}")
    print("=" * 60)
    print()

    # Register and start all script runners
    from scripts.runner import register_runner

    for name, info in SCRIPTS.items():
        runner = register_runner(
            name=name,
            module_path=info["module"],
            description=info["description"],
            poll_interval=poll_interval,
        )
        runner.start()
        print(f"  ✅ Started runner: {name}")

    print()
    print(f"  🚀 Starting dashboard server on port {port}...")
    print()

    # Start FastAPI server (blocking)
    import uvicorn
    from dashboard.api import app

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=port,
        log_level="info",
        access_log=False,  # Reduce noise — we have our own logging
    )


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help", "help"):
        print_help()
        sys.exit(0)

    if sys.argv[1] == "serve":
        serve()
    else:
        run_script(sys.argv[1])
