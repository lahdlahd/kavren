"""Live end-to-end check of attested planning (costs about $0.01), before any deploy.

No chain, no hosted API. Exactly the two halves KAVREN will run:
  provider side  - NearAIPlanner(attested=True) plans the fixture work file with a NON-streamed call through
                   NEAR AI's SDK and keeps the exact bytes + signature as evidence (same code as kavren-provider);
  evaluator side - kavren_api.attest.verify(): fresh model attestation for the signer, signature over the exact
                   bytes, then re-running the signed plan must reproduce the provider's file (same code as Render).

    .venv/bin/python scripts/attestation_live_check.py

Provider key: ~/.kavren/near-ai.env (NEAR_AI_API_KEY). Evaluator key: EVALUATOR_NEAR_AI_API_KEY in
~/.kavren/evaluator-near-ai.env if present, else the provider key (fine for this local check only).
Never prints a key.
"""
import json
import time
from pathlib import Path

from kavren_agents.common import load_env_file
from kavren_agents.planner import DEFAULT_MODEL, NearAIPlanner
from kavren_agents.provider import ProviderAgent
from kavren_api.attest import EvidenceError, TransientError, near_ai_check, verify
from kavren_core.spec import load_spec

FX = Path(__file__).resolve().parents[1] / "fixtures" / "contacts"
env = load_env_file("~/.kavren/near-ai.env")
base = env.get("NEAR_AI_BASE_URL", "https://cloud-api.near.ai/v1")
eval_env = load_env_file("~/.kavren/evaluator-near-ai.env") if Path("~/.kavren/evaluator-near-ai.env").expanduser().exists() else {}
eval_key = eval_env.get("EVALUATOR_NEAR_AI_API_KEY") or env["NEAR_AI_API_KEY"]

spec = load_spec((FX / "spec.json").read_bytes())
work = (FX / "work.csv").read_bytes()
planner = NearAIPlanner(env["NEAR_AI_API_KEY"], base, DEFAULT_MODEL, attested=True)
agent = ProviderAgent("live-check", None, None, planner)

t0 = time.time()
sub, attempts = agent._plan_until_clean(spec, work, None)
out = {"provider": {"seconds": round(time.time() - t0, 1), "local_attempts": attempts,
                    "evidence_attached": agent._evidence is not None, "model": planner.last_served_model,
                    "evaluator_key": "separate" if eval_env else "same as provider (local check only)"}}
if sub is None:
    out["result"] = "no plan passed the public checks; nothing to verify"
elif agent._evidence is None:
    out["result"] = "NO EVIDENCE: the attested call failed and the planner fell back to streaming (see log above)"
else:
    try:
        out["evaluator"] = verify(agent._evidence, spec=spec, work=work, submission=sub,
                                  claimed_model=planner.last_served_model, sdk_check=near_ai_check(eval_key, base))
        out["result"] = "VERIFIED end to end"
    except (EvidenceError, TransientError) as e:
        out["result"] = f"NOT VERIFIED: {type(e).__name__} {e}"
print(json.dumps(out, indent=2, default=str))
