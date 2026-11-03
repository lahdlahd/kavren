"""The LLM planner (NEAR AI Cloud, OpenAI-compatible chat completions).

It sees the spec rules and the work file, and returns ONLY decisions:
  {"rejects": [{"record_id", "reason"}], "merges": [{"record_id", "into"}], "notes": "..."}
On a revision it also sees the violation codes from the local self-check or from
the evaluator's report (which never names canaries).
"""
from __future__ import annotations

import json
import re
from typing import Optional, Protocol

import httpx

from kavren_core.spec import Spec

DEFAULT_MODEL = "deepseek-ai/DeepSeek-V4.1-Flash-1017"  # NEAR AI-hosted, attested TEE (ownedBy: nearai)


class Planner(Protocol):
    def plan(self, spec: Spec, work_csv: str, feedback: Optional[list[dict]] = None) -> tuple[dict, dict]:
        """Returns (plan, usage)."""


SYSTEM = """You are a careful data-cleaning agent working for pay under a strict, machine-checked contract.
You do NOT rewrite values: deterministic tools will trim and collapse whitespace in text, trim and lowercase
emails, and reformat dates. You only decide, per record, whether to REJECT it (with a reason code), MERGE it
into another record (a duplicate), or KEEP it (the default; do not list kept records).

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


def _user_prompt(spec: Spec, work_csv: str, feedback: Optional[list[dict]]) -> str:
    cols = ", ".join(f"{c.name} ({c.type}{', required' if c.required else ''})" for c in spec.columns)
    msg = (f"Template: {spec.template_id}\nColumns: {cols}\nDedupe key: {', '.join(spec.dedupe_key)}\n"
           f"Allowed input date formats: {', '.join(spec.date_rules.input_formats)}\n"
           f"Max rejected share: {spec.max_reject_pct}%\n\nWork file (CSV):\n{work_csv}")
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


class NearAIPlanner:
    def __init__(self, api_key: str, base_url: str = "https://cloud-api.near.ai/v1", model: str = DEFAULT_MODEL,
                 max_tokens: int = 12000, http: Optional[httpx.Client] = None):
        self.model, self.max_tokens = model, max_tokens
        self.http = http or httpx.Client(base_url=base_url, timeout=180,
                                         headers={"Authorization": f"Bearer {api_key}"})

    def plan(self, spec, work_csv, feedback=None):
        r = self.http.post("/chat/completions", json={
            "model": self.model, "max_tokens": self.max_tokens, "temperature": 0,
            "messages": [{"role": "system", "content": SYSTEM},
                         {"role": "user", "content": _user_prompt(spec, work_csv, feedback)}]})
        r.raise_for_status()
        d = r.json()
        content = d["choices"][0]["message"].get("content")
        return extract_json(content), d.get("usage", {})
