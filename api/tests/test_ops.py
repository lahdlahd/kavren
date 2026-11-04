"""Operational guarantees checked before hosting:
- /health reports the evaluator worker's heartbeat (503 when it is down or stuck);
- every route is classified public or authenticated, so a new route can't be added
  without being reviewed here;
- authenticated routes refuse anonymous callers; file routes refuse non-parties;
- public routes never return private file content."""
import csv
import io
import time

from test_api import EVALUATOR, INPUT, clean, env, login, make_job, sha256_hex, load_spec  # noqa: F401

from kavren_api.worker import beat, heartbeat_path

PUBLIC = {
    ("GET", "/health"), ("POST", "/auth/challenge"), ("POST", "/auth/verify"),
    ("GET", "/drafts/{draft_id}"),          # hashes + spec only, never files
    ("GET", "/jobs/{job_id}/check"),        # does the on-chain job match a draft (yes/no + status)
    ("GET", "/jobs/{job_id}/reveal"),       # canary opening, only once the job is final (synthetic rows)
}
AUTHED = {
    ("POST", "/drafts"), ("POST", "/jobs/{job_id}/register"),
    ("GET", "/jobs/{job_id}/work"), ("GET", "/jobs/{job_id}/input"), ("POST", "/jobs/{job_id}/submissions"),
    ("GET", "/jobs/{job_id}/reports/{seq}"), ("GET", "/jobs/{job_id}/deliverable"),
    ("GET", "/jobs/{job_id}/audit/work"), ("GET", "/jobs/{job_id}/audit/submissions/{seq}"),
}
FILE_ROUTES = AUTHED - {("POST", "/drafts"), ("POST", "/jobs/{job_id}/register")}
DOCS = {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}


def routes(client):
    out = set()
    for r in client.app.routes:
        if getattr(r, "path", None) in DOCS:
            continue
        for m in getattr(r, "methods", set()) - {"HEAD", "OPTIONS"}:
            out.add((m, r.path))
    return out


def url(path, job_id=0, seq=1, draft_id="x"):
    return path.format(job_id=job_id, seq=seq, draft_id=draft_id)


def call(c, method, path, headers=None):
    if method == "POST" and path.endswith(("/submissions", "/drafts")):
        return c.post(path, files={"file": ("f.csv", b"a,b\n1,2\n")}, headers=headers or {})
    return c.request(method, path, headers=headers or {})


def full_flow(env_):
    svc, chain, clock, c = env_
    agent = login(c, chain, "buyer-agent.testnet")
    prov = login(c, chain, "provider-agent.testnet")
    jid, draft = make_job(svc, chain, c, agent)
    c.post(f"/jobs/{jid}/register", headers=agent)
    work = c.get(f"/jobs/{jid}/work", headers=prov).content
    good = clean(load_spec(draft["spec_json"].encode()), work)
    chain.submit_work(jid, sha256_hex(good), clock.t)
    c.post(f"/jobs/{jid}/submissions", files={"file": ("s.csv", good)}, headers=prov)
    return jid, draft, work, good


def test_health_reflects_the_worker_heartbeat(env):
    svc, _, _, c = env
    hb = heartbeat_path(svc.settings)
    r = c.get("/health")
    assert r.status_code == 503 and r.json()["worker_ok"] is False and r.json()["api"] is True
    beat(hb)
    r = c.get("/health")
    assert r.status_code == 200 and r.json()["worker_ok"] is True
    beat(hb, time.time() - 600)
    assert c.get("/health").status_code == 503


def test_every_route_is_classified(env):
    assert routes(env[3]) == PUBLIC | AUTHED, "new or removed route: classify it in PUBLIC or AUTHED"


def test_authenticated_routes_refuse_anonymous_callers(env):
    _, _, _, c = env
    for m, p in AUTHED:
        r = call(c, m, url(p))
        assert r.status_code == 401, (m, p, r.status_code)


def test_file_routes_refuse_accounts_that_are_not_parties(env):
    svc, chain, clock, c = env
    jid, draft, work, good = full_flow(env)
    svc.process_pending()
    stranger = login(c, chain, "stranger.testnet")
    for m, p in FILE_ROUTES:
        r = call(c, m, url(p, job_id=jid), stranger)
        assert r.status_code == 403, (m, p, r.status_code, r.text[:100])


def test_public_routes_never_return_private_content(env):
    svc, chain, clock, c = env
    jid, draft, work, good = full_flow(env)
    secrets = {row["email"].strip().lower() for row in csv.DictReader(io.StringIO(INPUT.decode()))
               if row["email"].strip()}
    from kavren_core.canonical import load_canonical
    canary_ids = {x["record_id"] for x in load_canonical(svc._draft(draft["draft_id"]).canary_bytes)["canaries"]}
    real_ids = {line.split(",", 1)[0] for line in work.decode().splitlines()[1:]} - canary_ids
    assert len(real_ids) == 20
    secrets |= real_ids  # the buyer's real records must never be identifiable from public routes
    assert c.get(f"/jobs/{jid}/reveal").status_code == 409  # not final yet: canaries stay secret
    svc.process_pending()
    assert chain.jobs[jid]["status"] == "Passed"
    for m, p in PUBLIC:
        if m != "GET":
            continue
        body = c.get(url(p, job_id=jid, draft_id=draft["draft_id"])).text.lower()
        for s in secrets:
            assert s.lower() not in body, (p, s)


def test_cors_lets_the_web_ui_read_public_routes_only_with_get(env):
    _, _, _, c = env
    r = c.get("/health", headers={"Origin": "http://localhost:3000"})
    assert r.headers.get("access-control-allow-origin") == "http://localhost:3000"
    assert "access-control-allow-credentials" not in r.headers
    r = c.get("/health", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in r.headers
    pre = c.options("/drafts", headers={"Origin": "http://localhost:3000", "Access-Control-Request-Method": "POST"})
    assert pre.status_code == 400  # browsers can't POST cross-origin
