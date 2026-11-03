"""Settings come only from environment variables (see repo .env.example)."""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    database_url: str
    storage_dir: str
    contract_id: str
    rpc_url: str
    evaluator_account_id: str
    evaluator_fc_private_key: str
    auth_recipient: str
    file_retention_days: int
    challenge_ttl_secs: int = 300
    session_ttl_secs: int = 1800
    max_upload_bytes: int = 5 * 1024 * 1024
    verdict_safety_margin_secs: int = 300

    @staticmethod
    def from_env() -> "Settings":
        e = os.environ
        return Settings(
            database_url=e.get("DATABASE_URL", "sqlite:///./data/kavren.db"),
            storage_dir=e.get("STORAGE_DIR", "./data/files"),
            contract_id=e.get("KAVREN_CONTRACT_ID", ""),
            rpc_url=e.get("NEAR_RPC_URL", ""),  # empty -> NEAR-docs-listed providers with failover
            evaluator_account_id=e.get("EVALUATOR_ACCOUNT_ID", ""),
            evaluator_fc_private_key=e.get("EVALUATOR_FC_PRIVATE_KEY", ""),
            auth_recipient=e.get("AUTH_RECIPIENT", "kavren-api"),
            file_retention_days=int(e.get("FILE_RETENTION_DAYS", "7")),
        )
