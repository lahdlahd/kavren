"""HTTP layer. Thin: every rule lives in service.py."""
from __future__ import annotations

from typing import Optional

from fastapi import Depends, FastAPI, File, Form, Header, Request, UploadFile
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from .chain import NearChain
from .config import Settings
from .db import make_engine
from .service import ApiError, Service
from .storage import LocalStorage


WORKER_STALE_SECS = 180  # the worker beats every ~15 s; 3 minutes of silence means it is stuck or down


class ChallengeIn(BaseModel):
    account_id: str


class VerifyIn(BaseModel):
    account_id: str
    public_key: str
    signature: str
    nonce: str


def create_app(service: Optional[Service] = None) -> FastAPI:
    if service is None:
        s = Settings.from_env()
        service = Service(s, make_engine(s.database_url), LocalStorage(s.storage_dir),
                          NearChain(s.rpc_url, s.contract_id, s.evaluator_account_id, s.evaluator_fc_private_key))
    app = FastAPI(title="KAVREN API", version="0.1.0")
    app.state.svc = service
    # The public web UI fetches the canary opening of a finished job from the browser.
    # Only GET, no cookies or credentials; private routes still need a bearer token.
    import os

    from fastapi.middleware.cors import CORSMiddleware
    origins = [o.strip() for o in os.environ.get(
        "WEB_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000").split(",") if o.strip()]
    app.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=["GET"], allow_credentials=False)

    @app.exception_handler(ApiError)
    async def _err(_: Request, e: ApiError):
        return JSONResponse({"detail": e.detail}, status_code=e.status)

    def caller(authorization: str = Header(default="")) -> str:
        if not authorization.startswith("Bearer "):
            raise ApiError(401, "missing bearer token")
        return service.account_for_token(authorization[len("Bearer "):])

    async def read_limited(f: UploadFile) -> bytes:
        data = await f.read(service.settings.max_upload_bytes + 1)
        if len(data) > service.settings.max_upload_bytes:
            raise ApiError(413, "file too large")
        return data

    csv_ = lambda b: Response(b, media_type="text/csv", headers={"Cache-Control": "no-store"})

    @app.get("/health")
    def health():
        """Liveness of the API and of the evaluator worker (its heartbeat file)."""
        import time as _t

        from .worker import heartbeat_path
        try:
            age = int(_t.time()) - int(heartbeat_path(service.settings).read_text())
        except (OSError, ValueError):
            age = None
        worker_ok = age is not None and age <= WORKER_STALE_SECS
        body = {"ok": worker_ok, "api": True, "worker_ok": worker_ok, "worker_heartbeat_age_secs": age,
                "contract_id": service.settings.contract_id, "evaluator_id": service.settings.evaluator_account_id}
        return JSONResponse(body, status_code=200 if worker_ok else 503)

    @app.post("/auth/challenge")
    def auth_challenge(body: ChallengeIn):
        return service.challenge(body.account_id)

    @app.post("/auth/verify")
    def auth_verify(body: VerifyIn):
        return service.verify_login(body.account_id, body.public_key, body.signature, body.nonce)

    @app.post("/drafts")
    async def create_draft(file: UploadFile = File(...), template_id: Optional[str] = Form(default=None),
                           who: str = Depends(caller)):
        return service.create_draft(who, await read_limited(file), template_id)

    @app.get("/drafts/{draft_id}")
    def get_draft(draft_id: str):
        return service.draft_public(draft_id)

    @app.get("/jobs/{job_id}/check")
    def job_check(job_id: int):
        return service.job_check(job_id)

    @app.post("/jobs/{job_id}/register")
    def register(job_id: int, who: str = Depends(caller)):
        return service.register_job(who, job_id)

    @app.get("/jobs/{job_id}/work")
    def work(job_id: int, who: str = Depends(caller)):
        return csv_(service.get_work(who, job_id))

    @app.get("/jobs/{job_id}/input")
    def input_(job_id: int, who: str = Depends(caller)):
        return csv_(service.get_input(who, job_id))

    @app.post("/jobs/{job_id}/submissions")
    async def submit(job_id: int, file: UploadFile = File(...), provenance: Optional[str] = Form(default=None),
                     who: str = Depends(caller)):
        return service.upload_submission(who, job_id, await read_limited(file), provenance)

    @app.get("/jobs/{job_id}/provenance")
    def provenance(job_id: int):
        return service.get_provenance(job_id)

    @app.get("/jobs/{job_id}/reports/{seq}")
    def report(job_id: int, seq: int, who: str = Depends(caller)):
        return service.get_report(who, job_id, seq)

    @app.get("/jobs/{job_id}/deliverable")
    def deliverable(job_id: int, who: str = Depends(caller)):
        return csv_(service.get_deliverable(who, job_id))

    @app.get("/jobs/{job_id}/audit/work")
    def audit_work(job_id: int, who: str = Depends(caller)):
        return csv_(service.get_audit_file(who, job_id, "work"))

    @app.get("/jobs/{job_id}/audit/submissions/{seq}")
    def audit_sub(job_id: int, seq: int, who: str = Depends(caller)):
        return csv_(service.get_audit_file(who, job_id, "submission", seq))

    @app.get("/jobs/{job_id}/reveal")
    def reveal(job_id: int):
        return Response(service.get_reveal(job_id), media_type="application/json")

    _mount_web(app)
    return app


def _mount_web(app: FastAPI) -> None:
    """Serve the static website (web/out) from this same service when WEB_DIR is set.
    Mounted last, so every API route above wins; StaticFiles only serves files inside
    WEB_DIR (no path traversal), and that folder holds only the built site, never
    private data. Every response gets basic browser hardening headers."""
    import os
    from pathlib import Path

    from fastapi.staticfiles import StaticFiles

    @app.middleware("http")
    async def _headers(request: Request, call_next):
        resp = await call_next(request)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        resp.headers.setdefault("X-Frame-Options", "DENY")  # the owner console must not be framed
        return resp

    web = os.environ.get("WEB_DIR")
    if web and (Path(web) / "index.html").is_file():
        app.mount("/", StaticFiles(directory=web, html=True), name="web")
