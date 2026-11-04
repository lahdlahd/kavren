# Operations: running the API and evaluator as a hosted service

This is the plan for the one paid service (a Render Starter web service, about $7/month plus tax). It covers only things the code and tests support. Nothing is deployed until the owner gives the final go.

## 1. One service, two supervised processes

Start command: `python -m kavren_api.supervise`

- It runs the **API** (`uvicorn kavren_api.main:create_app`) and the **evaluator worker** (`python -m kavren_api.worker`) side by side.
- **Crash recovery:** if either process exits, it's restarted with backoff (1 s, 2 s, 4 s … up to 60 s). The backoff resets once the process has stayed up for 5 minutes.
- **Stuck-worker recovery:** after each 15-second pass, the worker writes a heartbeat file. If the heartbeat is more than 5 minutes old, the supervisor kills the worker and starts a new one.
- **Health:** `GET /health` returns `200` only while the worker's heartbeat is under 3 minutes old, and `503` otherwise. The body includes `worker_heartbeat_age_secs`. Render's health check points at `/health`.
- **If the supervisor itself dies,** the host restarts the whole service.
- Tests: `api/tests/test_supervise.py` uses real child processes to test crash, backoff, backoff reset, a stuck worker and clean stop. `api/tests/test_ops.py` tests `/health`.

## 2. Private files stay private

- Every route is listed as either public or authenticated in `api/tests/test_ops.py`. Adding a route without listing it there fails the tests.
- The **public routes** are `/health`, the two login steps, draft metadata (hashes and spec only), `/jobs/{id}/check` (yes/no), and `/jobs/{id}/reveal`. The reveal holds only synthetic canary rows and is available only once the job is final.
- **Authenticated routes refuse anonymous callers (401). File routes refuse logged-in accounts that aren't parties to the job (403).** Both are tested for every route.
- After a complete job, the tests call every public route and check that **none of the buyer's real emails or record IDs** appear in any response.
- The API serves no static files from the disk. Who may read which file is decided from the **on-chain** job: owner, buyer agent or provider.
- Retention: private files are deleted 7 days after a job is final (`FILE_RETENTION_DAYS`).

## 3. Backup, verify, restore, export

`python -m kavren_api.backup create | verify ARCHIVE | restore ARCHIVE [--force]`

- **create** makes `kavren-backup-<UTC time>.tar.gz` (mode 600) containing:
  - a consistent SQLite snapshot (SQLite's online backup API, safe while the service runs);
  - every private file;
  - a manifest with a SHA-256 for each member and table row counts.
- **verify** checks every hash and runs SQLite's `integrity_check`.
- **restore** verifies first and refuses to overwrite a non-empty target unless given `--force`, which you only use after stopping the service. It also rejects unsafe paths in the archive.
- **Schedule:** the supervisor runs `create` once a day, into `BACKUP_DIR` (default `<disk>/backups`).
- **Retention:** backups contain private files, so archives older than `FILE_RETENTION_DAYS` are deleted. A backup never keeps a file longer than the published policy.
- **Export:** copy an archive to the owner's own machine over Render's SSH access for paid services. Run `verify` there before relying on it. Exported copies fall under the same 7-day rule; delete them after that.
- Tests (`api/tests/test_backup.py`):
  - round trip of a real job, with every file byte-identical and the same report served by a fresh API from the restored copy;
  - a tampered archive is rejected;
  - restore refuses to overwrite;
  - archives are pruned;
  - daily scheduling never lets two backups run at once.

## 4. The evaluator's 30-minute window

The contract accepts a verdict only before `submitted_at + 1800 s`. The worker stops trying 300 s before that, so a slow transaction can't land after the window. After the window, the buyer refunds; this was live-tested with job 1.

`api/tests/test_deadline.py` drives the real worker pass against a chain that enforces the window:

| Condition | Result |
|---|---|
| Normal | verdict on the first 15-second pass |
| 10 minutes of RPC failures | verdict still in time |
| Worker down 20 minutes, then restarted | verdict still in time |
| Worker down past the 25-minute mark | no verdict attempted; submission marked `expired`; buyer refunds after 30 minutes |
| Provider uploads 20 minutes late | judged in time |
| One job's RPC lookup failing | other jobs are still judged in the same pass (bug found by this test and fixed) |
| Backlog of 25 submissions | all judged in one pass |
| Verdict transaction lands but the call errors | recorded as sent and never sent twice (bug found by this test and fixed) |

**Live drill:** `scripts/deadline_drill.py` runs on testnet:
1. With the worker stopped, it submits honest work.
2. It waits 20 minutes, then asks you to start the worker.
3. It reports how many seconds before the contract's deadline the verdict landed.

## 5. Environment on the host (set as secrets in the dashboard, never in Git)

`KAVREN_CONTRACT_ID`, `EVALUATOR_ACCOUNT_ID`, `EVALUATOR_FC_PRIVATE_KEY` (function-call key: verdict and reveal only), `DATABASE_URL=sqlite:////var/data/kavren.db`, `STORAGE_DIR=/var/data/files`, `BACKUP_DIR=/var/data/backups`, `FILE_RETENTION_DAYS=7`, `AUTH_RECIPIENT`, and optionally `NEAR_RPC_URL`. The persistent disk is mounted at `/var/data`.

## 6. Deploying on Render (one service)

- `render.yaml` describes the one paid service: Docker runtime, Starter plan, Frankfurt, a 1 GB disk at `/var/data`, health check `/health`, and **auto-deploy off**. Render shows the plan and price before it creates anything.
- `Dockerfile`: builds the static site (`NEXT_PUBLIC_API_URL=same-origin`), installs `kavren_core` and the API, and starts `python -m kavren_api.supervise`. No keys are baked in. `.dockerignore` keeps `.env*` files, virtual environments, local data and bundles out of the image.
- **The only secret is `EVALUATOR_FC_PRIVATE_KEY`.** It is a function-call key that can only call `submit_verdict` and `reveal_canaries`. You enter it in Render's dashboard; it never goes in Git or chat.
- **Only one evaluator may run at a time.** Before the Render service starts, stop the local API and worker: two workers signing with the same key would collide on nonces, and each has its own database. After that, point the agents at the hosted API with `KAVREN_API_URL=https://<service>.onrender.com`.
- Jobs 0–8 were evaluated by the local API, so their private files and canary sets live in the local database. Their on-chain records, verdicts and reveal flags are unaffected. To move that history to the host, restore a local backup onto the disk (section 3).
- **Rehearsed locally:** a clean install, running exactly as configured above, served the site, the API and `/health` (worker heartbeat), sent the security headers, refused private routes (401), served no storage paths, restarted the worker 1 s after a crash, wrote a backup, and shut down cleanly on SIGTERM.
