"""One-off check: can we genuinely verify NEAR AI Cloud attestation for the provider's model?

Runs in its OWN venv (the SDK pins httpx/openai versions; keep it away from .venv):
    python3 -m venv ~/kavren-spike
    ~/kavren-spike/bin/pip install nearai-inference-sdk==0.2.1
    ~/kavren-spike/bin/python scripts/attestation_spike.py

Reads the key from ~/.kavren/near-ai.env (NEAR_AI_API_KEY) and never prints it.
Makes 2 tiny chat calls (one plain, one streamed like the provider does): well under $0.01.
Prints a JSON summary; paste it back.
"""
import asyncio
import json
import os
import sys
import traceback
from importlib.metadata import version
from pathlib import Path

MODEL = os.environ.get("NEAR_AI_MODEL", "deepseek-ai/DeepSeek-V4.1-Flash-1017")
KEY_FILE = Path(os.environ.get("NEAR_AI_KEY_FILE", "~/.kavren/near-ai.env")).expanduser()
MESSAGES = [{"role": "user", "content": "Reply with the single word: ok"}]


def load_key() -> str:
    for line in KEY_FILE.read_text().splitlines():
        k, _, v = line.strip().partition("=")
        if k.strip() == "NEAR_AI_API_KEY":
            return v.strip().strip('"').strip("'")
    sys.exit(f"NEAR_AI_API_KEY not found in {KEY_FILE}")


def err(e: BaseException, key: str) -> dict:
    msg = str(e).replace(key, "***")[:600]
    code = getattr(e, "code", None) or getattr(getattr(e, "failure", None), "code", None)
    return {"error_type": type(e).__name__, "code": str(code) if code else None, "message": msg}


def evidence(a) -> dict:
    out = {"kind": type(a).__name__}
    for f in ("tcb_status", "advisory_ids", "deployment_provenance", "gpu_evidence"):
        if hasattr(a, f):
            v = getattr(a, f)
            out[f] = list(v) if isinstance(v, tuple) else str(v)
    s = getattr(a, "signer", None)
    if s is not None:
        out["signer"] = {k: str(getattr(s, k)) for k in ("signing_algo", "signing_address")
                         if hasattr(s, k)} or str(s)[:200]
    return out


def signed(r) -> dict:
    sig = r.signature
    return {"signature_kind": r.signature_kind, "id": r.id,
            "signed_text": getattr(sig, "signed_text", "")[:200],
            "attested_by": evidence(r.attestation)}


async def main() -> None:
    from nearai_inference_sdk import InferenceClient

    key = load_key()
    report: dict = {"sdk_version": version("nearai-inference-sdk"), "model": MODEL}
    async with InferenceClient(key) as client:
        # 1. Deployment attestation (gateway + every model candidate), no chat sent.
        try:
            att = await client.verify(MODEL)
            report["deployment"] = {"gateway": evidence(att.gateway),
                                    "models": [evidence(m) for m in att.models],
                                    "mode": "near_tee" if att.models else "incognito (gateway only)"}
        except Exception as e:
            report["deployment"] = err(e, key)

        # 2. Plain completion + response signature.
        try:
            c = await client.chat.completions.create(model=MODEL, messages=MESSAGES, max_tokens=5, temperature=0)
            report["plain"] = {"reply": (c.choices[0].message.content or "")[:40]}
            report["plain"].update(signed(await client.verify_response(c.id)))
        except Exception as e:
            report.setdefault("plain", {}).update(err(e, key))

        # 3. Streamed completion (the provider streams) + response signature.
        try:
            stream = await client.chat.completions.create(model=MODEL, messages=MESSAGES, max_tokens=5,
                                                          temperature=0, stream=True)
            cid, text = None, ""
            async for chunk in stream:
                cid = cid or chunk.id
                if chunk.choices and chunk.choices[0].delta.content:
                    text += chunk.choices[0].delta.content
            report["streamed"] = {"reply": text[:40]}
            report["streamed"].update(signed(await client.verify_response(cid)))
        except Exception as e:
            report.setdefault("streamed", {}).update(err(e, key))

    print(json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception:
        traceback.print_exc(limit=3)
