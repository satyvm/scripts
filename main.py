"""server-scripts — main entrypoint

Run individual scripts by name:
    uv run python main.py good-first-issues
    uv run python main.py <script-name>

Or run scripts directly via uv entrypoints:
    uv run good-first-issues
"""

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
    print("server-scripts — available scripts:\n")
    for name, info in SCRIPTS.items():
        print(f"  {name:<25} {info['description']}")
    print(f"\nUsage: uv run python main.py <script-name>")
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


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help", "help"):
        print_help()
        sys.exit(0)

    run_script(sys.argv[1])
