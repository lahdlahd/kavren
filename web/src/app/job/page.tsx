import { Suspense } from "react";
import { JobProof } from "@/components/JobProof";

export default function JobPage() {
  return (
    <Suspense fallback={<p className="muted">Loading…</p>}>
      <JobProof />
    </Suspense>
  );
}
