"use client";
import { chain } from "@/lib/config";
import { findLatestJobId, fmtTime, yoctoToNear, type Job } from "@/lib/near.mjs";
import { sealFor } from "@/lib/timeline.mjs";
import { useAsync } from "./useJob";

const PAGE = 25;

export function JobsList() {
  const s = useAsync(async () => {
    const last = await findLatestJobId((i) => chain.getJob(i));
    const ids = Array.from({ length: Math.min(PAGE, last + 1) }, (_, k) => last - k);
    return Promise.all(ids.map((i) => chain.getJob(i) as Promise<Job>));
  }, []);
  if (s.state === "loading") return <p className="muted" aria-live="polite">Reading jobs from the contract…</p>;
  if (s.state === "error") return <p>Couldn&apos;t reach NEAR testnet: {s.message}</p>;
  if (!s.value.length) return <p>No jobs yet. The buyer agent creates one when a file lands in its inbox.</p>;
  const now = Date.now() / 1000;
  return (
    <div className="table-wrap">
      <table>
        <thead><tr><th>Job</th><th>Status</th><th>Amount</th><th>Attempts</th><th>Created</th><th>Provider</th></tr></thead>
        <tbody>
          {s.value.map((j) => {
            const seal = sealFor(j, now);
            return (
              <tr key={j.job_id}>
                <td><a href={`/job/?id=${j.job_id}`}>Job {j.job_id}</a></td>
                <td><span className="status" data-tone={seal.tone}>{seal.word}</span></td>
                <td className="num">{yoctoToNear(j.amount)} NEAR</td>
                <td className="num">{j.attempts_used} of {j.max_attempts}</td>
                <td>{fmtTime(j.created_at)}</td>
                <td className="mono">{j.provider_id}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
