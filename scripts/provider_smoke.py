"""Live smoke test of the provider agent's LLM planning (costs about $0.01).

No chain, no API: plans the hand-checked fixture work file with the real model on
NEAR AI Cloud, executes the plan with the deterministic tools, runs the public
checks locally (with up to 3 revisions, exactly like the agent), then evaluates
the final file WITH the fixture's canaries and prints the verdict, revisions,
tokens and the cost at the model's published prices.

    .venv/bin/python scripts/provider_smoke.py
Reads NEAR_AI_API_KEY / NEAR_AI_BASE_URL from ~/.kavren/near-ai.env. Never prints the key.
"""
import json
import time
from pathlib import Path

from kavren_agents.common import load_env_file
from kavren_agents.planner import DEFAULT_MODEL, NearAIPlanner
from kavren_agents.provider import ProviderAgent
from kavren_core.canonical import load_canonical
from kavren_core.evaluator import evaluate
from kavren_core.spec import load_spec

FX = Path(__file__).resolve().parents[1] / "fixtures" / "contacts"
PRICE_IN, PRICE_OUT = 0.30e-6, 1.20e-6  # USD per token, DeepSeek V4.1 Flash 1017 on NEAR AI

env = load_env_file("~/.kavren/near-ai.env")
planner = NearAIPlanner(env["NEAR_AI_API_KEY"], env.get("NEAR_AI_BASE_URL", "https://cloud-api.near.ai/v1"), DEFAULT_MODEL)
spec = load_spec((FX / "spec.json").read_bytes())
work = (FX / "work.csv").read_bytes()

agent = ProviderAgent("smoke", None, None, planner)
t0 = time.time()
sub, attempts = agent._plan_until_clean(spec, work, None)
secs = time.time() - t0
tin = sum(u.get("prompt_tokens", 0) for u in agent.usage)
tout = sum(u.get("completion_tokens", 0) for u in agent.usage)
result = {"model": planner.model, "llm_calls": len(agent.usage), "local_attempts": attempts, "seconds": round(secs, 1),
          "prompt_tokens": tin, "completion_tokens": tout,
          "cost_usd": round(tin * PRICE_IN + tout * PRICE_OUT, 5)}
if sub is None:
    result["verdict"] = "NO PLAN PASSED THE PUBLIC CHECKS"
else:
    ev = evaluate(spec, work, sub, load_canonical((FX / "canary_reveal.json").read_bytes()))
    result.update(verdict_with_canaries=ev.verdict, failing_checks=ev.failing_checks,
                  canary_failures=ev.report["canary_failures"])
print(json.dumps(result, indent=2))
