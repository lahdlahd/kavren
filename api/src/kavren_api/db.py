"""Database tables (SQLite locally, Postgres in deployment via DATABASE_URL)."""
from __future__ import annotations

from sqlalchemy import (BigInteger, Boolean, Column, Integer, LargeBinary, MetaData, String, Table, Text,
                        create_engine)

metadata = MetaData()

challenges = Table(
    "auth_challenges", metadata,
    Column("nonce_b64", String(64), primary_key=True),
    Column("account_id", String(64), nullable=False),
    Column("expires_at", BigInteger, nullable=False),
    Column("used", Boolean, nullable=False, default=False),
)

sessions = Table(
    "sessions", metadata,
    Column("token_sha256", String(64), primary_key=True),
    Column("account_id", String(64), nullable=False),
    Column("expires_at", BigInteger, nullable=False),
)

drafts = Table(
    "drafts", metadata,
    Column("draft_id", String(40), primary_key=True),
    Column("created_by", String(64), nullable=False),
    Column("created_at", BigInteger, nullable=False),
    Column("template_id", String(64), nullable=False),
    Column("spec_json", Text, nullable=False),
    Column("spec_hash", String(64), nullable=False),
    Column("input_hash", String(64), nullable=False),
    Column("work_hash", String(64), nullable=False),
    Column("canary_commitment", String(64), nullable=False),
    Column("canary_bytes", LargeBinary, nullable=False),  # SECRET until the job is final
    Column("input_rows", Integer, nullable=False),
    Column("work_rows", Integer, nullable=False),
    Column("files_deleted", Boolean, nullable=False, default=False),
)

jobs = Table(
    "jobs", metadata,
    Column("job_id", BigInteger, primary_key=True),
    Column("draft_id", String(40), nullable=False),
    Column("registered_at", BigInteger, nullable=False),
    Column("final_seen_at", BigInteger),
    Column("revealed_tx", String(64)),
)

submissions = Table(
    "submissions", metadata,
    Column("job_id", BigInteger, primary_key=True),
    Column("seq", Integer, primary_key=True),
    Column("output_hash", String(64), nullable=False),
    Column("uploaded_at", BigInteger, nullable=False),
    Column("state", String(16), nullable=False),  # pending | evaluated | sent | stale | expired
    Column("verdict", String(4)),
    Column("report_hash", String(64)),
    Column("report_json", Text),
    Column("verdict_tx", String(64)),
    Column("tries", Integer, nullable=False, default=0),
    Column("last_error", Text),
)


# What the provider says produced each submission (model, service, completion ids). A separate table so
# existing databases gain it via create_all without a migration. Unverified unless `verified` says so.
submission_provenance = Table(
    "submission_provenance", metadata,
    Column("job_id", BigInteger, primary_key=True),
    Column("seq", Integer, primary_key=True),
    Column("provenance_json", Text, nullable=False),
    Column("recorded_at", BigInteger, nullable=False),
)


def make_engine(url: str):
    if url.startswith("sqlite:///"):
        import os
        path = url[len("sqlite:///"):]
        if path and path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        engine = create_engine(url, connect_args={"check_same_thread": False})
    else:
        engine = create_engine(url, pool_pre_ping=True)
    metadata.create_all(engine)
    return engine
