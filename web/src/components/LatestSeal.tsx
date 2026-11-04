"use client";
import { chain } from "@/lib/config";
import { findLatestJobId, type Job } from "@/lib/near.mjs";
import { sealFor } from "@/lib/timeline.mjs";
import { Seal } from "./Seal";
import { useAsync } from "./useJob";

export function LatestSeal() {
  const s = useAsync(async () => {
    const id = await findLatestJobId((i) => chain.getJob(i));
    return id < 0 ? null : ((await chain.getJob(id)) as Job);
  }, []);
  if (s.state === "loading") return <p className="muted small" aria-live="polite">Reading the latest job from NEAR…</p>;
  if (s.state === "error") return <p className="muted small">Couldn&apos;t reach NEAR testnet: {s.message}</p>;
  if (!s.value) return <p className="muted small">No jobs on this contract yet.</p>;
  const j = s.value;
  return (
    <a className="seal-link" href={`/job/?id=${j.job_id}`}>
      <Seal seal={sealFor(j, Date.now() / 1000)} stamp />
      <small>Latest job on testnet: job {j.job_id}. Open its proof page</small>
    </a>
  );
}
