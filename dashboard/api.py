"""Dashboard API — FastAPI app for script monitoring and control.

Endpoints:
    GET  /              → Dashboard HTML (auth required)
    GET  /api/status    → All script statuses
    GET  /api/logs      → Recent log entries
    GET  /api/scripts   → List of registered scripts
    POST /api/rerun/:n  → Trigger immediate re-run
    GET  /api/output/:n → Captured stdout for a script
    GET  /health        → Health check (no auth)
"""

import os
from pathlib import Path

from fastapi import FastAPI, Request, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse

# ==========================================
# AUTH
# ==========================================
DASHBOARD_TOKEN = os.environ.get("DASHBOARD_TOKEN", "")


def _check_auth(request: Request) -> None:
    """Validate bearer token from header or query param."""
    # Try Authorization header first
    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:]
        if token == DASHBOARD_TOKEN:
            return

    # Fall back to query parameter (for browser initial load)
    token = request.query_params.get("token", "")
    if token == DASHBOARD_TOKEN:
        return

    raise HTTPException(status_code=401, detail="Unauthorized")


# ==========================================
# APP
# ==========================================
app = FastAPI(
    title="Script Dashboard",
    docs_url=None,  # Disable Swagger UI in production
    redoc_url=None,
)


@app.get("/health")
async def health():
    """Health check — no auth required. Used by Coolify."""
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    """Serve the dashboard HTML."""
    _check_auth(request)

    html_path = Path(__file__).parent / "index.html"
    if not html_path.exists():
        raise HTTPException(status_code=500, detail="Dashboard HTML not found")

    # Inject the token into the HTML so JS can use it for API calls
    token = request.query_params.get("token", "")
    html = html_path.read_text()
    html = html.replace("__DASHBOARD_TOKEN__", token)
    return HTMLResponse(content=html)


@app.get("/api/status")
async def get_status(request: Request):
    """Return all script statuses."""
    _check_auth(request)

    from scripts.runner import get_all_statuses
    return JSONResponse(content=get_all_statuses())


@app.get("/api/scripts")
async def get_scripts(request: Request):
    """Return the list of registered scripts with descriptions."""
    _check_auth(request)

    from scripts.runner import get_all_runners
    runners = get_all_runners()
    return JSONResponse(content={
        name: {
            "name": r.name,
            "description": r.description,
            "module": r.module_path,
            "poll_interval": r.poll_interval,
        }
        for name, r in runners.items()
    })


@app.get("/api/logs")
async def get_logs(
    request: Request,
    limit: int = Query(default=50, le=500, ge=1),
    script: str | None = Query(default=None),
):
    """Return recent log entries."""
    _check_auth(request)

    from scripts.runner import get_logs as _get_logs
    return JSONResponse(content=_get_logs(limit=limit, script=script))


@app.post("/api/rerun/{script_name}")
async def rerun_script(request: Request, script_name: str):
    """Trigger an immediate re-run of a script."""
    _check_auth(request)

    from scripts.runner import get_runner
    runner = get_runner(script_name)
    if not runner:
        raise HTTPException(status_code=404, detail=f"Script '{script_name}' not found")

    success = runner.trigger_rerun()
    if not success:
        raise HTTPException(status_code=409, detail="Script is already running")

    return JSONResponse(content={"status": "triggered", "script": script_name})


@app.get("/api/output/{script_name}")
async def get_output(request: Request, script_name: str):
    """Return captured stdout lines for a script."""
    _check_auth(request)

    from scripts.runner import get_runner
    runner = get_runner(script_name)
    if not runner:
        raise HTTPException(status_code=404, detail=f"Script '{script_name}' not found")

    return JSONResponse(content={"lines": runner.get_output()})
