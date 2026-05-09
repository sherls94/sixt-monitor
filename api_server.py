from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
import json
import asyncio
from pathlib import Path
from datetime import datetime

# ── Pure ASGI middleware — sits outside the FastAPI router entirely ────────────
# BaseHTTPMiddleware (@app.middleware) in Starlette 1.x doesn't intercept
# requests that are short-circuited by FastAPI's built-in OPTIONS handler.
# A raw ASGI class wrapping the whole app has no such limitation.
_CORS_HEADERS = [
    (b"access-control-allow-origin",          b"*"),
    (b"access-control-allow-methods",         b"GET, POST, PUT, DELETE, OPTIONS"),
    (b"access-control-allow-headers",         b"*"),
    (b"access-control-allow-private-network", b"true"),
    (b"access-control-max-age",               b"86400"),
]


class _PrivateNetworkMiddleware:
    """ASGI middleware that handles CORS + Chrome Private Network Access preflights."""

    def __init__(self, app):
        self._app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        if scope["method"] == "OPTIONS":
            # Respond immediately — don't forward to the app at all.
            await send({
                "type": "http.response.start",
                "status": 204,
                "headers": _CORS_HEADERS,
            })
            await send({"type": "http.response.body", "body": b""})
            return

        # For all other methods, inject CORS headers into the outgoing response.
        async def _send_with_cors(event):
            if event["type"] == "http.response.start":
                # Merge: keep existing headers, add ours (overwrite if key clashes)
                existing = {k: v for k, v in event.get("headers", [])}
                for k, v in _CORS_HEADERS:
                    existing[k] = v
                event = {**event, "headers": list(existing.items())}
            await send(event)

        await self._app(scope, receive, _send_with_cors)


_fastapi_app = FastAPI()


def _make_app():
    return _PrivateNetworkMiddleware(_fastapi_app)


app = _make_app()
# Route decorators must target the inner FastAPI instance
_r = _fastapi_app

RESULTS_FILE = Path(__file__).parent / "latest_results.json"
RUNNING = False


@_r.get("/results")
async def get_results():
    if not RESULTS_FILE.exists():
        return JSONResponse({"error": "No results yet — run a check first"}, status_code=404)
    return json.loads(RESULTS_FILE.read_text(encoding="utf-8"))


@_r.post("/run")
async def trigger_run():
    global RUNNING
    if RUNNING:
        return {"status": "already_running"}
    RUNNING = True
    asyncio.create_task(_run_monitor())
    return {"status": "started"}


@_r.get("/status")
async def get_status():
    return {
        "running": RUNNING,
        "last_results_at": datetime.fromtimestamp(
            RESULTS_FILE.stat().st_mtime
        ).isoformat() if RESULTS_FILE.exists() else None,
    }


async def _run_monitor():
    global RUNNING
    try:
        proc = await asyncio.create_subprocess_exec(
            "python", "price_monitor.py",
            cwd=Path(__file__).parent,
        )
        await proc.wait()
    finally:
        RUNNING = False


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("api_server:app", host="0.0.0.0", port=8000)
