"""Live drill of the evaluator's 30-minute window on testnet.

1. With the evaluator worker STOPPED (and the provider/buyer agents stopped), this
   opens a job and submits honest work, exactly as the agents do.
2. It waits DELAY minutes (default 20) and asks you to start the worker.
3. It measures when the verdict lands relative to the contract's deadline
   (submitted_at + 1800 s) and checks it landed in time, bound to the right file.

The honest PASS pays the provider agent (your own testnet account) for the job.
Run (from ~/kavren):
  BUYER_OWNER_ACCOUNT_ID=lahd-kavren.testnet .venv/bin/python scripts/deadline_drill.py [DELAY_MINUTES]
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from attack_gallery import Gallery  # noqa: E402  (reuses its job-opening code)

from kavren_core.canonical import sha256_hex  # noqa: E402
from kavren_core.spec import load_spec  # noqa: E402
from reference_cleaner import clean  # noqa: E402


def main() -> None:
    from kavren_agents.common import AgentChain, ApiClient, load_env_file
    from kavren_api.chain import quiet_rpc_race_noise

    quiet_rpc_race_noise()
    delay = float(sys.argv[1]) if len(sys.argv) > 1 else 20.0
    buyer = load_env_file("~/.kavren/buyer-agent.env")
    prov = load_env_file("~/.kavren/provider-agent.env")
    rpc, contract = os.environ.get("NEAR_RPC_URL", ""), buyer["KAVREN_CONTRACT_ID"]
    api = os.environ.get("KAVREN_API_URL", "http://127.0.0.1:8000")
    owner = os.environ.get("BUYER_OWNER_ACCOUNT_ID") or sys.exit("set BUYER_OWNER_ACCOUNT_ID")
    b_id, p_id = buyer["BUYER_AGENT_ACCOUNT_ID"], prov["PROVIDER_ACCOUNT_ID"]
    g = Gallery(owner_id=owner, buyer_id=b_id, buyer_chain=AgentChain(rpc, contract, b_id, buyer["BUYER_AGENT_FC_PRIVATE_KEY"]),
                buyer_api=ApiClient(api, b_id, buyer["BUYER_AGENT_FC_PRIVATE_KEY"]), provider_id=p_id,
                provider_chain=AgentChain(rpc, contract, p_id, prov["PROVIDER_FC_PRIVATE_KEY"]),
                provider_api=ApiClient(api, p_id, prov["PROVIDER_FC_PRIVATE_KEY"]),
                evaluator_chain=None, wait=lambda: time.sleep(5))
    print(f"Deadline drill: the worker must be STOPPED now; it will be started after {delay:.0f} minutes.")
    if input("Type 'go' to start: ").strip() != "go":
        sys.exit("cancelled")
    job_id, tx, work, args = g._open_job()
    good = clean(load_spec(args["spec_json"].encode()), work)
    h = sha256_hex(good)
    res = g.pc.call("submit_work", {"job_id": job_id, "output_hash": h})
    up = g._upload(job_id, good)
    j = g._job(job_id)
    t0, window = j["submitted_at"], j["eval_window_secs"]
    print(f"job {job_id}: submitted (tx {res['tx']}), upload {up.status_code}; contract deadline in {window // 60} min")
    while time.time() < t0 + delay * 60:
        left = int(t0 + delay * 60 - time.time())
        print(f"\r  worker stays stopped: {left // 60:02d}:{left % 60:02d} left ", end="", flush=True)
        time.sleep(5)
    print(f"\n>>> START THE WORKER NOW (it is {delay:.0f} min after submission). Waiting for the verdict...")
    while True:
        j = g._job(job_id)
        if j["verdicts"]:
            v = j["verdicts"][-1]
            at = v["at"]
            ok = at < t0 + window and v["output_hash"] == h and v["verdict"] == "PASS"
            print(f"verdict {v['verdict']} landed {at - t0} s after submission, "
                  f"{t0 + window - at} s before the contract deadline -> {'IN TIME' if ok else 'PROBLEM'}")
            print(f"job {job_id}, submit tx {res['tx']}")
            sys.exit(0 if ok else 1)
        if time.time() > t0 + window + 120:
            print("no verdict by the deadline: the worker gave up inside its 5-minute safety margin "
                  "(expected only if it was started too late); the buyer can now refund the job")
            sys.exit(1)
        time.sleep(10)


if __name__ == "__main__":
    main()
