"""Independent check of the provider's NEAR AI attestation evidence (evaluator side).

The provider uploads, with its file, the exact bytes of the planning call that produced it and NEAR AI's
signature over them. The evaluator, with its OWN NEAR AI key and a fresh nonce:

  1. fetches the model's current attestation for the signer named in the signature and verifies it
     (Intel TDX quote + NVIDIA GPU evidence, via NEAR AI's official SDK);
  2. checks the `provider_tee` signature binds that attested key to the exact request and response bytes;
  3. re-applies the plan in that signed response to this job's work file: it must reproduce the submitted
     file byte for byte, so evidence from any other call cannot be attached.

This never affects a verdict or a payment. It only decides whether the proof page may say "verified" or must
keep "reported by the provider". Not checked: which software image the TEE runs (deployment provenance).
"""
from __future__ import annotations

import asyncio
import base64
import json
from typing import Awaitable, Callable, Optional

from kavren_core.canonical import sha256_hex

MAX_EVIDENCE_BYTES = 2 * 1024 * 1024


class EvidenceError(Exception):
    """The evidence itself is wrong: a final 'not verified' result, never retried."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class TransientError(Exception):
    """NEAR AI could not be reached or said 'try again': retried a few times."""


def parse_evidence(raw: bytes) -> Optional[dict]:
    """Shape check at upload time; None if absent or malformed (the file upload still succeeds)."""
    if not raw or len(raw) > MAX_EVIDENCE_BYTES:
        return None
    try:
        e = json.loads(raw)
        sig = e["signature"]
        out = {"request_b64": str(e["request_b64"]), "response_b64": str(e["response_b64"]),
               "signature": {k: str(sig[k]) for k in ("kind", "signed_text", "signature", "signing_algo",
                                                        "signing_address")}}
        base64.b64decode(out["request_b64"], validate=True)
        base64.b64decode(out["response_b64"], validate=True)
    except (ValueError, KeyError, TypeError):
        return None
    if out["signature"]["kind"] != "provider_tee" or out["signature"]["signing_algo"] not in ("ed25519", "ecdsa"):
        return None
    return out


# (model, request bytes, response bytes, signature dict) -> attestation summary; raises on failure
SdkCheck = Callable[[str, bytes, bytes, dict], Awaitable[dict]]


def verify(evidence: dict, *, spec, work: bytes, submission: bytes, claimed_model: str,
           sdk_check: SdkCheck) -> dict:
    """Run all three checks. Returns the public summary on success; raises EvidenceError / TransientError."""
    from kavren_agents.cleaning import PlanError, execute
    from kavren_agents.planner import extract_json

    req = base64.b64decode(evidence["request_b64"])
    resp = base64.b64decode(evidence["response_b64"])
    try:
        body = json.loads(req)
        answer = json.loads(resp)
    except ValueError:
        raise EvidenceError("evidence.not_json")
    model = body.get("model")
    if model != claimed_model:
        raise EvidenceError("evidence.model_differs_from_reported")
    if body.get("stream"):
        raise EvidenceError("evidence.streamed_call")

    summary = asyncio.run(sdk_check(model, req, resp, evidence["signature"]))

    try:
        content = answer["choices"][0]["message"]["content"] or ""
        plan = extract_json(content)
        rebuilt = execute(spec, work, plan)
    except (KeyError, IndexError, TypeError, ValueError, PlanError):
        raise EvidenceError("evidence.plan_does_not_execute")
    if sha256_hex(rebuilt) != sha256_hex(submission):
        raise EvidenceError("evidence.plan_does_not_reproduce_file")
    return {"model": model, **summary}


def near_ai_check(api_key: str, base_url: str) -> SdkCheck:
    """The real network check, with NEAR AI's official SDK (pinned in api/pyproject.toml)."""

    async def check(model: str, req: bytes, resp: bytes, sig: dict) -> dict:
        from nearai_inference_sdk import (ApiError, AttestationClient, VerificationError,
                                          find_model_attestation_for_signature, verify_model_attestation,
                                          verify_model_response)
        from nearai_inference_sdk.types.attestation_common import SigningIdentity
        from nearai_inference_sdk.types.chat import CompletionSignature

        signature = CompletionSignature(
            kind=sig["kind"], signed_text=sig["signed_text"], signature=sig["signature"],
            signer=SigningIdentity(signing_algo=sig["signing_algo"], signing_address=sig["signing_address"]))
        try:
            client = AttestationClient(api_key, base_url=base_url)
            fetched = await client.fetch_model_attestations(model, signing_algo=sig["signing_algo"],
                                                            signing_address=sig["signing_address"])
            verified = [await verify_model_attestation(a, fetched.client_binding) for a in fetched.attestations]
            att = find_model_attestation_for_signature(verified, signature)
            verify_model_response(req, resp, signature, att)
        except ApiError as e:
            if e.retryable:
                raise TransientError(e.failure.code)
            raise EvidenceError(e.failure.code)
        except VerificationError as e:
            raise EvidenceError(e.failure.code)
        except OSError as e:
            raise TransientError(type(e).__name__)
        return {"tcb_status": str(att.tcb_status), "gpu_evidence": str(att.gpu_evidence),
                "deployment_provenance": str(att.deployment_provenance),
                "signing_address": att.signer.signing_address}

    return check
