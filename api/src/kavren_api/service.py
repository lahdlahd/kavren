"""Business logic. Every access decision is made against the ON-CHAIN job record:
the API never trusts a caller's claim about who the buyer or provider is."""
from __future__ import annotations

import base64
import hashlib
import json
import random
import secrets
import time
from dataclasses import dataclass
from typing import Callable, Optional

from sqlalchemy import and_, delete, insert, select, update

from kavren_core.canaries import InputError, build_work_file
from kavren_core.canonical import load_canonical, sha256_hex
from kavren_core.evaluator import build_deliverable, evaluate
from kavren_core.spec import load_spec
from kavren_core.templates import get_template, select_template

from . import nep413
from .chain import Chain
from .config import Settings
from .db import challenges, drafts, jobs, sessions, submissions
from .storage import Storage

FINAL = ("Passed", "Refunded")


class ApiError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status, self.detail = status, detail


@dataclass
class Service:
    settings: Settings
    engine: object
    storage: Storage
    chain: Chain
    clock: Callable[[], float] = time.time
    rng: random.Random = random.SystemRandom()

    def now(self) -> int:
        return int(self.clock())

    # ================================================================ auth (NEP-413)

    def login_message(self, account_id: str) -> str:
        return f"Sign in to KAVREN as {account_id}"

    def challenge(self, account_id: str) -> dict:
        _check_account_id(account_id)
        nonce = secrets.token_bytes(32)
        nb64 = base64.b64encode(nonce).decode()
        exp = self.now() + self.settings.challenge_ttl_secs
        with self.engine.begin() as c:
            c.execute(insert(challenges).values(nonce_b64=nb64, account_id=account_id, expires_at=exp, used=False))
        return {"nonce": nb64, "message": self.login_message(account_id),
                "recipient": self.settings.auth_recipient, "expires_at": exp}

    def verify_login(self, account_id: str, public_key: str, signature: str, nonce_b64: str) -> dict:
        with self.engine.begin() as c:
            row = c.execute(select(challenges).where(challenges.c.nonce_b64 == nonce_b64)).first()
            if not row or row.account_id != account_id or row.used or row.expires_at < self.now():
                raise ApiError(401, "unknown, expired or already used challenge")
            # single use, even if the signature turns out to be wrong
            c.execute(update(challenges).where(challenges.c.nonce_b64 == nonce_b64).values(used=True))
        nonce = base64.b64decode(nonce_b64)
        if not nep413.verify(public_key, signature, self.login_message(account_id), nonce, self.settings.auth_recipient):
            raise ApiError(401, "bad signature")
        perm = self.chain.get_access_key_permission(account_id, public_key)
        if perm is None:
            raise ApiError(401, "key is not an access key of this account")
        if perm != "FullAccess":
            fc = perm.get("FunctionCall", {}) if isinstance(perm, dict) else {}
            if fc.get("receiver_id") != self.settings.contract_id:
                raise ApiError(401, "function-call keys must be scoped to the KAVREN contract")
        token = secrets.token_urlsafe(32)
        exp = self.now() + self.settings.session_ttl_secs
        with self.engine.begin() as c:
            c.execute(insert(sessions).values(token_sha256=_h(token), account_id=account_id, expires_at=exp))
        return {"token": token, "account_id": account_id, "expires_at": exp}

    def account_for_token(self, token: str) -> str:
        with self.engine.begin() as c:
            row = c.execute(select(sessions).where(sessions.c.token_sha256 == _h(token))).first()
        if not row or row.expires_at < self.now():
            raise ApiError(401, "invalid or expired session")
        return row.account_id

    # ================================================================ drafts

    def create_draft(self, caller: str, input_bytes: bytes, template_id: Optional[str]) -> dict:
        if len(input_bytes) > self.settings.max_upload_bytes:
            raise ApiError(413, "input too large")
        if template_id is None:
            header = input_bytes.split(b"\n", 1)[0].decode("utf-8", "replace").split(",")
            template_id = select_template(header)
            if template_id is None:
                raise ApiError(422, "no template matches the input header")
        try:
            spec = get_template(template_id).build_spec()
        except KeyError:
            raise ApiError(422, "unknown template") from None
        try:
            wb = build_work_file(spec, input_bytes, self.rng)
        except InputError as e:
            raise ApiError(422, str(e)) from None
        draft_id = "d" + secrets.token_hex(12)
        self.storage.put(f"drafts/{draft_id}/input.csv", input_bytes)
        self.storage.put(f"drafts/{draft_id}/work.csv", wb.work_bytes)
        spec_json = spec.canonical().decode()
        with self.engine.begin() as c:
            c.execute(insert(drafts).values(
                draft_id=draft_id, created_by=caller, created_at=self.now(), template_id=template_id,
                spec_json=spec_json, spec_hash=spec.hash(), input_hash=wb.input_hash, work_hash=wb.work_hash,
                canary_commitment=wb.canary_commitment, canary_bytes=wb.canary_bytes,
                input_rows=wb.input_rows, work_rows=wb.work_rows, files_deleted=False))
        return self.draft_public(draft_id)

    def draft_public(self, draft_id: str) -> dict:
        d = self._draft(draft_id)
        return {
            "draft_id": d.draft_id, "created_by": d.created_by, "template_id": d.template_id,
            "spec_json": d.spec_json, "spec_hash": d.spec_hash, "input_hash": d.input_hash,
            "work_hash": d.work_hash, "canary_commitment": d.canary_commitment,
            "input_rows": d.input_rows, "work_rows": d.work_rows,
            "evaluator_id": self.settings.evaluator_account_id,
            "price_yocto": str(get_template(d.template_id).price_yocto(d.input_rows)),
        }

    def _draft(self, draft_id: str):
        if not (isinstance(draft_id, str) and len(draft_id) == 25 and draft_id[0] == "d"):
            raise ApiError(404, "no such draft")
        with self.engine.begin() as c:
            d = c.execute(select(drafts).where(drafts.c.draft_id == draft_id)).first()
        if not d:
            raise ApiError(404, "no such draft")
        return d

    # ================================================================ jobs

    def _job(self, job_id: int) -> dict:
        j = self.chain.get_job(job_id)
        if not j:
            raise ApiError(404, "no such job on chain")
        return j

    def _job_and_draft(self, job_id: int):
        """The on-chain job must commit to exactly what the protocol drafted, for the same buyer agent."""
        j = self._job(job_id)
        d = self._draft(j["draft_id"])
        ok = (j["buyer_agent"] == d.created_by and j["spec_json"] == d.spec_json and j["input_hash"] == d.input_hash
              and j["work_hash"] == d.work_hash and j["canary_commitment"] == d.canary_commitment
              and j["template_id"] == d.template_id and j["evaluator_id"] == self.settings.evaluator_account_id)
        if not ok:
            raise ApiError(409, "on-chain job does not match the protocol draft; the evaluator will not act on it")
        return j, d

    def register_job(self, caller: str, job_id: int) -> dict:
        j, d = self._job_and_draft(job_id)
        if caller not in (j["buyer_agent"], j["owner_id"]):
            raise ApiError(403, "only the job's buyer agent or owner")
        with self.engine.begin() as c:
            if not c.execute(select(jobs).where(jobs.c.job_id == job_id)).first():
                c.execute(insert(jobs).values(job_id=job_id, draft_id=d.draft_id, registered_at=self.now()))
        return self.job_check(job_id)

    def job_check(self, job_id: int) -> dict:
        """Public: does the on-chain job match a protocol draft? Providers call this before working."""
        try:
            j, d = self._job_and_draft(job_id)
            return {"job_id": job_id, "matches_draft": True, "draft_id": d.draft_id, "status": j["status"]}
        except ApiError as e:
            if e.status == 409:
                return {"job_id": job_id, "matches_draft": False, "reason": e.detail}
            raise

    def get_work(self, caller: str, job_id: int) -> bytes:
        j, d = self._job_and_draft(job_id)
        if caller != j["provider_id"]:
            raise ApiError(403, "only the job's provider")
        self._ensure_registered(job_id, d.draft_id)
        return self._file(f"drafts/{d.draft_id}/work.csv", d)

    def get_input(self, caller: str, job_id: int) -> bytes:
        j, d = self._job_and_draft(job_id)
        if caller not in (j["owner_id"], j["buyer_agent"]):
            raise ApiError(403, "only the job's owner or buyer agent")
        return self._file(f"drafts/{d.draft_id}/input.csv", d)

    def upload_submission(self, caller: str, job_id: int, data: bytes) -> dict:
        j, d = self._job_and_draft(job_id)
        if caller != j["provider_id"]:
            raise ApiError(403, "only the job's provider")
        if len(data) > self.settings.max_upload_bytes:
            raise ApiError(413, "submission too large")
        if j["status"] != "Submitted":
            raise ApiError(409, "no on-chain submission is awaiting a file")
        h = sha256_hex(data)
        if h != j["current_output_hash"]:
            raise ApiError(409, "file hash does not match the hash submitted on-chain")
        seq = j["submission_seq"]
        self._ensure_registered(job_id, d.draft_id)
        with self.engine.begin() as c:
            existing = c.execute(select(submissions).where(and_(submissions.c.job_id == job_id, submissions.c.seq == seq))).first()
            if existing:
                return {"job_id": job_id, "seq": seq, "output_hash": h, "state": existing.state}
            self.storage.put(f"jobs/{job_id}/sub-{seq}.csv", data)
            c.execute(insert(submissions).values(job_id=job_id, seq=seq, output_hash=h, uploaded_at=self.now(),
                                                 state="pending", tries=0))
        return {"job_id": job_id, "seq": seq, "output_hash": h, "state": "pending"}

    def get_report(self, caller: str, job_id: int, seq: int) -> dict:
        j, d = self._job_and_draft(job_id)
        if caller not in (j["owner_id"], j["buyer_agent"], j["provider_id"]):
            raise ApiError(403, "only parties to the job")
        with self.engine.begin() as c:
            s = c.execute(select(submissions).where(and_(submissions.c.job_id == job_id, submissions.c.seq == seq))).first()
        if not s or not s.report_json:
            raise ApiError(404, "no report yet")
        return {"seq": seq, "state": s.state, "verdict": s.verdict, "report_hash": s.report_hash,
                "verdict_tx": s.verdict_tx, "report": json.loads(s.report_json)}

    def get_deliverable(self, caller: str, job_id: int) -> bytes:
        j, d = self._job_and_draft(job_id)
        if caller not in (j["owner_id"], j["buyer_agent"]):
            raise ApiError(403, "only the job's owner or buyer agent")
        if j["status"] != "Passed":
            raise ApiError(409, "job has not passed")
        passed = next(v for v in j["verdicts"] if v["verdict"] == "PASS")
        sub = self._file(f"jobs/{job_id}/sub-{passed['seq']}.csv", d)
        if sha256_hex(sub) != passed["output_hash"]:
            raise ApiError(500, "stored submission does not match the passed hash")
        return build_deliverable(load_spec(d.spec_json.encode()), sub, load_canonical(d.canary_bytes))

    def get_audit_file(self, caller: str, job_id: int, what: str, seq: int | None = None) -> bytes:
        """After the job is final, the buyer side may download the work file and any
        submission so it can re-run the evaluation itself instead of trusting it."""
        j, d = self._job_and_draft(job_id)
        if caller not in (j["owner_id"], j["buyer_agent"]):
            raise ApiError(403, "only the job's owner or buyer agent")
        if j["status"] not in FINAL:
            raise ApiError(409, "audit files are available once the job is final")
        if what == "work":
            return self._file(f"drafts/{d.draft_id}/work.csv", d)
        if not isinstance(seq, int) or not 1 <= seq <= j["submission_seq"]:
            raise ApiError(404, "no such submission")
        return self._file(f"jobs/{job_id}/sub-{seq}.csv", d)

    def get_reveal(self, job_id: int) -> bytes:
        """Public once the job is final: the opened canary commitment."""
        j, d = self._job_and_draft(job_id)
        if j["status"] not in FINAL:
            raise ApiError(409, "canaries stay secret until the job is final")
        return bytes(d.canary_bytes)

    def _ensure_registered(self, job_id: int, draft_id: str) -> None:
        with self.engine.begin() as c:
            if not c.execute(select(jobs).where(jobs.c.job_id == job_id)).first():
                c.execute(insert(jobs).values(job_id=job_id, draft_id=draft_id, registered_at=self.now()))

    def _file(self, key: str, d) -> bytes:
        if d.files_deleted:
            raise ApiError(410, "files deleted under the retention policy")
        return self.storage.get(key)

    # ================================================================ evaluator worker

    def process_pending(self) -> list[dict]:
        """Evaluate every pending submission and post verdicts. Safe to run repeatedly."""
        done = []
        with self.engine.begin() as c:
            pend = c.execute(select(submissions).where(submissions.c.state.in_(("pending", "evaluated")))).fetchall()
        for s in pend:
            done.append(self._process_one(s))
        return done

    def _process_one(self, s) -> dict:
        key = and_(submissions.c.job_id == s.job_id, submissions.c.seq == s.seq)
        j, d = self._job_and_draft(s.job_id)
        if j["status"] != "Submitted" or j["submission_seq"] != s.seq or j["current_output_hash"] != s.output_hash:
            with self.engine.begin() as c:
                c.execute(update(submissions).where(key).values(state="stale"))
            return {"job_id": s.job_id, "seq": s.seq, "state": "stale"}
        deadline = j["submitted_at"] + j["eval_window_secs"] - self.settings.verdict_safety_margin_secs
        if self.now() >= deadline:
            with self.engine.begin() as c:
                c.execute(update(submissions).where(key).values(state="expired", last_error="evaluation window nearly closed"))
            return {"job_id": s.job_id, "seq": s.seq, "state": "expired"}
        if s.state == "pending":
            spec = load_spec(d.spec_json.encode())
            work = self.storage.get(f"drafts/{d.draft_id}/work.csv")
            sub = self.storage.get(f"jobs/{s.job_id}/sub-{s.seq}.csv")
            if sha256_hex(sub) != s.output_hash:
                raise ApiError(500, "stored submission was altered")
            ev = evaluate(spec, work, sub, load_canonical(d.canary_bytes))
            with self.engine.begin() as c:
                c.execute(update(submissions).where(key).values(
                    state="evaluated", verdict=ev.verdict, report_hash=ev.report_hash, report_json=ev.report_bytes.decode()))
            verdict, report_hash = ev.verdict, ev.report_hash
        else:
            verdict, report_hash = s.verdict, s.report_hash
        try:
            tx = self.chain.submit_verdict(s.job_id, s.seq, s.output_hash, verdict, report_hash)
        except Exception as e:  # retried on the next pass until the safety margin
            with self.engine.begin() as c:
                c.execute(update(submissions).where(key).values(tries=s.tries + 1, last_error=str(e)[:500]))
            return {"job_id": s.job_id, "seq": s.seq, "state": "evaluated", "error": str(e)[:200]}
        with self.engine.begin() as c:
            c.execute(update(submissions).where(key).values(state="sent", verdict_tx=tx))
        return {"job_id": s.job_id, "seq": s.seq, "state": "sent", "verdict": verdict, "tx": tx}

    def reveal_finished(self) -> list[dict]:
        out = []
        with self.engine.begin() as c:
            rows = c.execute(select(jobs).where(jobs.c.revealed_tx.is_(None))).fetchall()
        for r in rows:
            j = self.chain.get_job(r.job_id)
            if not j or j["status"] not in FINAL:
                continue
            if j.get("canaries_revealed"):
                tx = "already-revealed"
            else:
                d = self._draft(r.draft_id)
                try:
                    tx = self.chain.reveal_canaries(r.job_id, bytes(d.canary_bytes))
                except Exception as e:
                    out.append({"job_id": r.job_id, "error": str(e)[:200]})
                    continue
            with self.engine.begin() as c:
                c.execute(update(jobs).where(jobs.c.job_id == r.job_id).values(
                    revealed_tx=tx, final_seen_at=r.final_seen_at or self.now()))
            out.append({"job_id": r.job_id, "revealed_tx": tx})
        return out

    def apply_retention(self) -> list[str]:
        """Delete private job files N days after a job is final, and unused drafts after N days."""
        cutoff = self.now() - self.settings.file_retention_days * 86400
        deleted = []
        with self.engine.begin() as c:
            final_jobs = c.execute(select(jobs).where(and_(jobs.c.final_seen_at.is_not(None), jobs.c.final_seen_at < cutoff))).fetchall()
            used = {r.draft_id for r in c.execute(select(jobs.c.draft_id)).fetchall()}
            stale_drafts = c.execute(select(drafts).where(and_(drafts.c.created_at < cutoff, drafts.c.files_deleted.is_(False)))).fetchall()
        for jr in final_jobs:
            self.storage.delete_prefix(f"jobs/{jr.job_id}/")
            self._delete_draft_files(jr.draft_id)
            deleted.append(f"job {jr.job_id}")
        for d in stale_drafts:
            if d.draft_id not in used:
                self._delete_draft_files(d.draft_id)
                deleted.append(f"draft {d.draft_id}")
        return deleted

    def _delete_draft_files(self, draft_id: str) -> None:
        self.storage.delete_prefix(f"drafts/{draft_id}/")
        with self.engine.begin() as c:
            c.execute(update(drafts).where(drafts.c.draft_id == draft_id).values(files_deleted=True))

    def purge_auth(self) -> None:
        with self.engine.begin() as c:
            c.execute(delete(challenges).where(challenges.c.expires_at < self.now()))
            c.execute(delete(sessions).where(sessions.c.expires_at < self.now()))


def _h(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _check_account_id(a: str) -> None:
    import re
    if not (2 <= len(a) <= 64 and re.fullmatch(r"^(([a-z\d]+[-_])*[a-z\d]+\.)*([a-z\d]+[-_])*[a-z\d]+$", a)):
        raise ApiError(422, "invalid NEAR account id")
