"""FastAPI application: auth, accounts, tus uploads, REST + SSE, and the built frontend.

Run:  uvicorn quill.app:app --host 0.0.0.0 --port 8000
(`app` is created lazily from QUILL_* env on first access; tests call create_app(settings).)
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import __version__, api, auth, db, sharing, upload, users
from .config import Settings


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    settings.ensure_dirs()
    db.init(settings.db_path)

    app = FastAPI(title="Quill", version=__version__, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.rate_limiter = auth.RateLimiter()
    app.state.sse_interval = 1.0

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        detail = exc.detail if isinstance(exc.detail, str) else "Request failed."
        return JSONResponse({"error": detail}, status_code=exc.status_code, headers=getattr(exc, "headers", None))

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        errors = exc.errors()
        first = errors[0] if errors else {}
        where = ".".join(str(p) for p in first.get("loc", ()) if p != "body")
        msg = first.get("msg", "Invalid request.")
        return JSONResponse({"error": f"{where}: {msg}" if where else msg}, status_code=422)

    @app.get("/api/health")
    def health():
        return {"status": "ok", "version": __version__}

    app.include_router(auth.router)
    app.include_router(upload.router)
    app.include_router(users.router)
    app.include_router(sharing.router)
    app.include_router(api.router)

    @app.api_route("/api/{rest:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"], include_in_schema=False)
    def api_not_found(rest: str):
        raise HTTPException(404, "Not found.")

    dist = settings.frontend_dist_dir

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str):
        if not (dist / "index.html").is_file():
            raise HTTPException(404, "Frontend is not built (frontend/dist missing).")
        if path:
            candidate = (dist / path).resolve()
            try:
                candidate.relative_to(dist.resolve())
            except ValueError:
                raise HTTPException(404, "Not found.")
            if candidate.is_file():
                cache = "public, max-age=31536000, immutable" if path.startswith("assets/") else "no-cache"
                return FileResponse(candidate, headers={"Cache-Control": cache})
        return FileResponse(dist / "index.html", headers={"Cache-Control": "no-cache"})

    return app


_app: FastAPI | None = None


def __getattr__(name: str):
    # Lazy module attribute so `uvicorn quill.app:app` works without building an
    # app (and touching /var/lib/quill) merely on import.
    global _app
    if name == "app":
        if _app is None:
            logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
            _app = create_app()
        return _app
    raise AttributeError(name)
