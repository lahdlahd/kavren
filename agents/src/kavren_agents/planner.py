"""The LLM planner (NEAR AI Cloud, OpenAI-compatible chat completions).

It sees the spec rules and the work file, and returns ONLY decisions:
  {"rejects": [{"record_id", "reason"}], "merges": [{"record_id", "into"}], "notes": "..."}
On a revision it also sees the violation codes from the local self-check or from
the evaluator's report (which never names canaries).
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
from typing import Awaitable, Callable, Optional, Protocol

import httpx

from kavren_core.spec import Spec

DEFAULT_MODEL = "deepseek-ai/DeepSeek-V4.1-Flash-1017"  # NEAR AI-hosted (ownedBy: nearai), runs in a NEAR TEE
log = logging.getLogger("kavren.planner")


class Planner(Protocol):
    def plan(self, spec: Spec, work_csv: str, feedback: Optional[list[dict]] = None) -> tuple[dict, dict]:
        """Returns (plan, usage)."""


SYSTEM = """You are a careful data-cleaning agent working for pay under a strict, machine-checked contract.
You do NOT rewrite values: deterministic tools will trim and collapse whitespace in text, trim and lowercase
emails, and reformat dates. You only decide, per record, whether to REJECT it (with a reason code), MERGE it
into another record (a duplicate), or KEEP it (the default; do not list kept records).

Each record comes with FACTS computed by deterministic tools: its normalised dedupe key, which required
columns are missing, whether its email is valid, whether its date parses. Use them; you decide.

Rules you are judged on (they are checked against the original data, so false claims fail):
- Reject with MISSING_REQUIRED only if a required column is empty (after trimming) and the record has no
  duplicate you could merge it into instead.
- Reject with INVALID_EMAIL only if the email, after trimming and lowercasing, is not a valid address
  (needs local@domain.tld; no leading, trailing or double dots; TLD letters only, 2+ chars).
- Reject with INVALID_DATE only if a non-empty date matches none of the allowed input formats or is impossible
  (e.g. 31/02). Dates are day-first: DD/MM/YYYY.
- Merge records whose dedupe key is equal after normalisation (emails trimmed + lowercased; text trimmed,
  whitespace collapsed, case-insensitive). Merge INTO a record that will be kept; never merge into a rejected
  record. Prefer keeping the record with the most complete data.
- Never reject a record that could be kept. The buyer caps rejections.
Answer with ONE JSON object and nothing else:
{"rejects":[{"record_id":"...","reason":"MISSING_REQUIRED|INVALID_EMAIL|INVALID_DATE"}],
 "merges":[{"record_id":"...","into":"..."}],
 "notes":"one short sentence"}"""


def facts_table(spec: Spec, work_csv: str) -> str:
    """Deterministic FACTS per record (computed by tools, not by the model): normalised key,
    email validity, date parse result, missing required columns. The model makes the decisions."""
    import csv as _csv
    import io as _io

    from kavren_core import formats as f
    from kavren_core.evaluator import input_key

    rows = list(_csv.reader(_io.StringIO(work_csv)))
    hdr, out = rows[0], ["record_id|dedupe_key|missing_required|email_ok|date_ok|values"]
    for r in rows[1:]:
        rec = dict(zip(hdr[1:], r[1:]))
        k = input_key(spec, rec)
        miss = [c.name for c in spec.columns if c.required and not rec[c.name].strip()]
        emails = [f.is_valid_email(f.norm_email(rec[c.name])) for c in spec.columns
                  if c.type == "email" and rec[c.name].strip()]
        dates = [f.parse_input_date(rec[c.name], spec.date_rules.input_formats) is not None
                 for c in spec.columns if c.type == "date" and rec[c.name].strip()]
        out.append("|".join([r[0], "/".join(k) if k else "-", ",".join(miss) or "-",
                             "-" if not emails else str(all(emails)).lower(),
                             "-" if not dates else str(all(dates)).lower(), json.dumps(rec, ensure_ascii=False)]))
    return "\n".join(out)


def _user_prompt(spec: Spec, work_csv: str, feedback: Optional[list[dict]]) -> str:
    cols = ", ".join(f"{c.name} ({c.type}{', required' if c.required else ''})" for c in spec.columns)
    msg = (f"Template: {spec.template_id}\nColumns: {cols}\nDedupe key: {', '.join(spec.dedupe_key)}\n"
           f"Max rejected share: {spec.max_reject_pct}%\n\n"
           "Records with FACTS computed by deterministic tools (trust them; '-' means not applicable):\n"
           f"{facts_table(spec, work_csv)}")
    if feedback:
        msg += ("\n\nYour previous plan was checked and FAILED with these violations "
                "(check, code, record_id). Fix them and return a complete new plan:\n"
                + json.dumps(feedback, indent=0))
    return msg


def extract_json(text: str) -> dict:
    """Take the first top-level JSON object out of a reply (tolerates code fences)."""
    if not text:
        raise ValueError("empty reply")
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    start = t.find("{")
    if start < 0:
        raise ValueError("no JSON object in reply")
    obj, _ = json.JSONDecoder().raw_decode(t[start:])
    return obj


# body -> {"content", "usage", "id", "model", "evidence" (or None)}
AttestedCall = Callable[[dict], Awaitable[dict]]


class NearAIPlanner:
    """With `attested=True` (the provider's default) each planning call is made WITHOUT streaming through
    NEAR AI's official SDK, which verifies the model's TEE attestation first; the exact request/response bytes
    and NEAR AI's `provider_tee` signature are kept as evidence for the evaluator to check independently.
    (Streamed replies only carry a gateway signature, which does not prove the model produced them.)
    If that call fails for any reason, it falls back to the streaming path below, with no evidence, so the
    submission is labelled "reported by the provider" rather than lost.

    Streaming path: a request fails only if the server is silent for `idle_timeout` seconds, or the whole
    answer takes longer than `total_timeout`."""

    def __init__(self, api_key: str, base_url: str = "https://cloud-api.near.ai/v1", model: str = DEFAULT_MODEL,
                 max_tokens: int = 8000, http: Optional[httpx.Client] = None,
                 idle_timeout: float = 120, total_timeout: float = 600, attested: bool = False,
                 attested_call: Optional[AttestedCall] = None):
        self.model, self.max_tokens, self.total_timeout = model, max_tokens, total_timeout
        self.base_url = base_url
        self.attested = attested
        self.attested_call = attested_call or (lambda body: sdk_attested_call(api_key, base_url, body, total_timeout))
        self.last_evidence: Optional[dict] = None  # evidence of the last call, None if it has none
        # From the last successful stream: the completion id NEAR AI assigned and the model it says served it.
        # Recorded with each submission (as reported by the provider, not yet attestation-verified).
        self.last_completion_id: Optional[str] = None
        self.last_served_model: Optional[str] = None
        self.http = http or httpx.Client(base_url=base_url, headers={"Authorization": f"Bearer {api_key}"},
                                         timeout=httpx.Timeout(30, read=idle_timeout))
        self.reasoning_effort: Optional[str] = "low"  # dropped automatically if the server rejects it

    def _body(self, spec, work_csv, feedback):
        b = {"model": self.model, "max_tokens": self.max_tokens, "temperature": 0, "stream": True,
             "stream_options": {"include_usage": True},
             "messages": [{"role": "system", "content": SYSTEM},
                          {"role": "user", "content": _user_prompt(spec, work_csv, feedback)}]}
        if self.reasoning_effort:
            b["reasoning_effort"] = self.reasoning_effort
        return b

    def plan(self, spec, work_csv, feedback=None, on_progress=None):
        self.last_evidence = None
        if self.attested:
            body = self._body(spec, work_csv, feedback)
            body.pop("stream"), body.pop("stream_options")
            try:
                if on_progress:
                    on_progress()
                r = asyncio.run(self.attested_call(body))
            except Exception as e:  # infrastructure, not the plan: fall back, unverified
                log.warning("attested planning call failed (%s: %s); streaming instead, without evidence",
                            type(e).__name__, str(e)[:200])
            else:
                if r.get("reasoning_effort_rejected"):
                    self.reasoning_effort = None
                self.last_completion_id, self.last_served_model = r.get("id"), r.get("model")
                self.last_evidence = r.get("evidence")
                if on_progress:
                    on_progress()
                return extract_json(r["content"] or ""), r.get("usage") or {}
        return self._plan_streamed(spec, work_csv, feedback, on_progress)

    def _plan_streamed(self, spec, work_csv, feedback=None, on_progress=None):
        import time as _t
        for _ in range(2):
            content, usage, start = [], {}, _t.time()
            cid, served = None, None
            with self.http.stream("POST", "/chat/completions", json=self._body(spec, work_csv, feedback)) as r:
                if r.status_code == 400 and self.reasoning_effort:
                    r.read()
                    self.reasoning_effort = None  # server does not accept it; retry plainly
                    continue
                if r.status_code != 200:
                    r.read()
                    raise httpx.HTTPStatusError(f"NEAR AI returned {r.status_code}: {r.text[:300]}",
                                                request=r.request, response=r)
                for line in r.iter_lines():
                    if _t.time() - start > self.total_timeout:
                        raise TimeoutError("model answer took too long")
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    chunk = json.loads(data)
                    cid = cid or chunk.get("id")
                    served = served or chunk.get("model")
                    if chunk.get("usage"):
                        usage = chunk["usage"]
                    for ch in chunk.get("choices", []):
                        piece = (ch.get("delta") or {}).get("content")
                        if piece:
                            content.append(piece)
                    if on_progress:
                        on_progress()
            self.last_completion_id, self.last_served_model = cid, served
            return extract_json("".join(content)), usage
        raise RuntimeError("unreachable")


async def sdk_attested_call(api_key: str, base_url: str, body: dict, timeout: float) -> dict:
    """One non-streamed chat call through NEAR AI's SDK: it verifies the Gateway and the model's TEE
    attestation BEFORE sending, then we verify NEAR AI's signature over the exact bytes and keep them."""
    import openai
    from nearai_inference_sdk import InferenceClient

    rejected = False
    async with InferenceClient(api_key, base_url=base_url) as client:
        for _ in range(2):
            try:
                comp = await client.chat.completions.create(**body, timeout=timeout)
                break
            except openai.BadRequestError:
                if "reasoning_effort" not in body:
                    raise
                body = {k: v for k, v in body.items() if k != "reasoning_effort"}
                rejected = True
        out = {"content": comp.choices[0].message.content if comp.choices else "",
               "usage": comp.usage.model_dump() if comp.usage else {}, "id": comp.id,
               "model": body["model"],  # the model name NEAR AI's signature covers
               "evidence": None, "reasoning_effort_rejected": rejected}
        try:
            v = await client.verify_response(comp.id)
            rec = client._get_response_record(comp.id)  # exact wire bytes the SDK verified (SDK pinned)
            if v.signature_kind == "provider_tee" and rec is not None:
                sig = v.signature
                out["evidence"] = {
                    "request_b64": base64.b64encode(rec.request_body).decode(),
                    "response_b64": base64.b64encode(await rec.response_body).decode(),
                    "signature": {"kind": sig.kind, "signed_text": sig.signed_text, "signature": sig.signature,
                                  "signing_algo": sig.signer.signing_algo,
                                  "signing_address": sig.signer.signing_address}}
        except Exception as e:  # the answer is still usable; it just carries no evidence
            log.warning("NEAR AI response signature not verified (%s); no evidence attached", type(e).__name__)
        return out
