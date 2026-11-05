"use client";
import { useSearchParams } from "next/navigation";
import { Fragment, useEffect, useState } from "react";
import { API_ENABLED, API_URL, CONTRACT_ID, RPCS, accountUrl } from "@/lib/config";
import { fmtDuration, fmtTime, sha256Hex, yoctoToNear, type Job } from "@/lib/near.mjs";
import { describeProvenance, type ProvenanceLine } from "@/lib/provenance.mjs";
import { attemptsLine, buildTimeline, sealFor } from "@/lib/timeline.mjs";
import { Seal } from "./Seal";
import { loadJob, useAsync } from "./useJob";

function Acct({ id }: { id: string }) {
  return <a className="mono" href={accountUrl(id)}>{id}</a>;
}

export function JobProof() {
  const params = useSearchParams();
  const raw = params.get("id");
  const id = raw !== null && /^\d+$/.test(raw) ? Number(raw) : null;
  const s = useAsync(async () => (id === null ? null : await loadJob(id)), [id]);

  if (id === null) return <JobPicker />;
  if (s.state === "loading") return <p className="muted" aria-live="polite">Reading job {id} from the contract…</p>;
  if (s.state === "error")
    return <p>Couldn&apos;t read job {id} from NEAR testnet: {s.message}. Try again in a moment.</p>;
  if (!s.value) return <><h1>Job {id}</h1><p className="lede">There is no job {id} on {CONTRACT_ID}.</p><JobPicker /></>;
  return <Proof job={s.value} />;
}

function JobPicker() {
  const [v, setV] = useState("");
  return (
    <form className="panel" onSubmit={(e) => { e.preventDefault(); if (/^\d+$/.test(v)) location.href = `/job/?id=${v}`; }}>
      <div className="field">
        <label htmlFor="jobid">Job number</label>
        <input id="jobid" type="text" inputMode="numeric" value={v} onChange={(e) => setV(e.target.value.trim())} />
      </div>
      <div className="actions"><button type="submit">Open proof page</button><a href="/jobs/">See all jobs</a></div>
    </form>
  );
}

function Proof({ job }: { job: Job }) {
  const now = Date.now() / 1000;
  const seal = sealFor(job, now);
  const events = buildTimeline(job, now);
  const final = job.status === "Passed" || job.status === "Refunded";
  return (
    <>
      <div className="proof-head">
        <div>
          <h1>Job {job.job_id}</h1>
          <p className="lede">
            {job.status === "Passed" && <>The provider&apos;s work passed every check, and the {yoctoToNear(job.amount)} NEAR escrow was released to the provider.</>}
            {job.status === "Refunded" && <>No submission passed, so the {yoctoToNear(job.amount)} NEAR went back to the owner. The provider was paid nothing.</>}
            {!final && <>{yoctoToNear(job.amount)} NEAR is held by the contract until the evaluator passes the work or a refund condition is met.</>}
          </p>
        </div>
        <Seal seal={seal} stamp />
      </div>

      <div className="proof-grid">
        <section aria-labelledby="tl">
          <h2 id="tl">What the contract recorded</h2>
          <ol className="timeline" style={{ marginTop: "1.25rem" }}>
            {events.map((e, i) => (
              <li key={i} data-tone={e.tone}>
                <h3>{e.title}</h3>
                {e.when !== null && <time dateTime={new Date(e.when * 1000).toISOString()}>{fmtTime(e.when)}</time>}
                <p>{e.detail}</p>
                {Object.keys(e.data).length > 0 && (
                  <dl className="hashes">
                    {Object.entries(e.data).map(([k, v]) => (<Fragment key={k}><dt>{k}</dt><dd>{v}</dd></Fragment>))}
                  </dl>
                )}
              </li>
            ))}
          </ol>
        </section>

        <aside className="facts" aria-label="Job facts">
          <dl>
            <div><dt>Amount</dt><dd>{yoctoToNear(job.amount)} NEAR</dd></div>
            <div><dt>Owner</dt><dd><Acct id={job.owner_id} /></dd></div>
            <div><dt>Buyer agent</dt><dd><Acct id={job.buyer_agent} /></dd></div>
            <div><dt>Provider</dt><dd><Acct id={job.provider_id} /></dd></div>
            <div><dt>Evaluator</dt><dd><Acct id={job.evaluator_id} /></dd></div>
            <div><dt>Job type</dt><dd>{job.template_id}</dd></div>
            <div><dt>Attempts</dt><dd>{attemptsLine(job)}</dd></div>
            <div><dt>Deadline</dt><dd>{fmtTime(job.deadline)}</dd></div>
            <div><dt>Evaluation window</dt><dd>{fmtDuration(job.eval_window_secs)} per submission</dd></div>
            {job.submission_seq > 0 && <ModelUsed jobId={job.job_id} />}
          </dl>
        </aside>
      </div>

      {final && <CanaryCheck job={job} />}

      <section className="section" aria-labelledby="yourself">
        <h2 id="yourself">Check it yourself</h2>
        <p>
          Everything above came from one read of the contract, made by your browser. Ask a NEAR node for the same record
          with no tools from us:
        </p>
        <pre className="cmd">{`curl -s ${RPCS[0]} -H 'content-type: application/json' -d '{"jsonrpc":"2.0","id":1,"method":"query","params":{"request_type":"call_function","finality":"final","account_id":"${CONTRACT_ID}","method_name":"get_job","args_base64":"${typeof btoa === "function" ? btoa(JSON.stringify({ job_id: job.job_id })) : ""}"}}' \\
  | python3 -c 'import sys,json; print(bytes(json.load(sys.stdin)["result"]["result"]).decode())'`}</pre>
        <p className="muted small" style={{ marginTop: "1rem" }}>
          The owner and buyer agent can also re-run the whole evaluation from the files, with the same result and report
          hash: <code>kavren verify</code> (see the repository README). The buyer agent does this automatically before it
          accepts a PASS.
        </p>
      </section>
    </>
  );
}

function CanaryCheck({ job }: { job: Job }) {
  const [res, setRes] = useState<{ tone: "pass" | "fail" | "neutral"; text: string } | null>(null);
  useEffect(() => {
    if (!API_ENABLED) {
      setRes({ tone: "neutral", text: "This site isn't connected to the KAVREN API, so it can't fetch the published canary set here." });
      return;
    }
    fetch(`${API_URL}/jobs/${job.job_id}/reveal`).then(async (r) => {
      if (r.status !== 200) {
        setRes({ tone: "neutral", text: `This server doesn't hold the canary set for this job (${r.status}), so your browser can't re-check it here. Jobs judged before the hosted evaluator went live were evaluated on the owner's machine, and files are deleted 7 days after a job ends. The contract's own check at reveal time, shown above, still stands.` });
        return;
      }
      const h = await sha256Hex(await r.arrayBuffer());
      setRes(h === job.canary_commitment
        ? { tone: "pass", text: `Your browser hashed the published canary set: ${h}. It matches the commitment recorded when the job was created, so the hidden checks couldn't have been changed afterwards.` }
        : { tone: "fail", text: `The published canary set hashes to ${h}, which does NOT match the on-chain commitment ${job.canary_commitment}.` });
    }).catch(() => setRes({ tone: "neutral", text: "Couldn't reach the KAVREN API to fetch the canary set." }));
  }, [job]);
  return (
    <section className="section" aria-labelledby="canary">
      <h2 id="canary">Hidden control records</h2>
      <p>
        Before the work started, the protocol mixed synthetic control records into the file and put only their hash on-chain.
        The provider couldn&apos;t tell them apart from real rows.
      </p>
      {res && <div className="check" data-tone={res.tone} aria-live="polite"><p className="mono" style={{ fontFamily: "var(--sans)" }}>{res.text}</p></div>}
    </section>
  );
}

function ModelUsed({ jobId }: { jobId: number }) {
  const [lines, setLines] = useState<ProvenanceLine[] | null>(null);
  useEffect(() => {
    if (!API_ENABLED) return;
    fetch(`${API_URL}/jobs/${jobId}/provenance`)
      .then((r) => (r.ok ? r.json() : null))
      .then((p) => setLines(describeProvenance(p)))
      .catch(() => setLines(null));
  }, [jobId]);
  if (!API_ENABLED || lines === null) return null;
  return (
    <div>
      <dt>Model used</dt>
      <dd>
        {lines.length === 0 && <span className="muted">Not recorded for this job</span>}
        {lines.map((l) => (
          <p key={l.seq} style={{ margin: 0 }}>
            {lines.length > 1 && <>Submission {l.seq}: </>}<span className="mono">{l.text}</span>
            <br /><span className="muted small">{l.label}</span>
          </p>
        ))}
      </dd>
    </div>
  );
}
