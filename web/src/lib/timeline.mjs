// Turns the contract's job record into the proof page's story. Pure: every line
// is derived from fields the contract stores; nothing is inferred from off-chain data.

import { fmtDuration, yoctoToNear } from "./near.mjs";

/** The seal shown on the proof page. */
export function sealFor(job, now) {
  if (job.status === "Passed") return { word: "Pass", tone: "pass", line: "Accepted and paid" };
  if (job.status === "Refunded") return { word: "Refunded", tone: "fail", line: "No PASS; escrow returned to the owner" };
  if (job.status === "Submitted") return { word: "Judging", tone: "wait", line: "Waiting for the evaluator's verdict" };
  const refundable = now >= job.deadline || job.attempts_used >= job.max_attempts;
  return refundable
    ? { word: "Refundable", tone: "wait", line: "No attempts or time left; anyone can trigger the refund" }
    : { word: "Open", tone: "wait", line: "Waiting for the provider's work" };
}

/** Ordered events. Each has: when (secs or null), title, detail, tone, data (key -> value). */
export function buildTimeline(job, now) {
  const ev = [];
  const near = `${yoctoToNear(job.amount)} NEAR`;
  ev.push({
    when: job.created_at, tone: "neutral",
    title: `Job created by ${job.buyer_agent}`,
    detail: `${near} moved from ${job.owner_id}'s budget into escrow. The provider is ${job.provider_id}; ` +
      `only ${job.evaluator_id} may judge it. ${job.max_attempts} attempts, deadline in ${fmtDuration(job.deadline - job.created_at)}.`,
    data: { "Input hash": job.input_hash, "Work file hash": job.work_hash, "Canary commitment": job.canary_commitment },
  });
  for (const v of job.verdicts) {
    const pass = v.verdict === "PASS";
    ev.push({
      when: v.at, tone: pass ? "pass" : "fail",
      title: `Submission ${v.seq} judged: ${v.verdict}`,
      detail: pass
        ? "The evaluator's PASS is bound to this exact file and report. The payment became claimable by the provider."
        : "The evaluator's FAIL is bound to this exact file and report. The escrow stayed locked and one attempt was used.",
      data: { "Submitted file hash": v.output_hash, "Report hash": v.report_hash },
    });
  }
  if (job.status === "Submitted") {
    const closes = job.submitted_at + job.eval_window_secs;
    ev.push({
      when: job.submitted_at, tone: "wait",
      title: `Submission ${job.submission_seq} waiting for a verdict`,
      detail: now < closes
        ? `The evaluator has until ${fmtDuration(closes - now)} from now. After that, the buyer can refund.`
        : "The evaluation window has closed without a verdict. Anyone can now refund the job to the owner.",
      data: { "Submitted file hash": job.current_output_hash },
    });
  }
  if (job.status === "Refunded") {
    const unjudged = job.submission_seq > job.verdicts.length && job.submitted_at != null;
    if (unjudged) {
      ev.push({
        when: job.submitted_at, tone: "wait",
        title: `Submission ${job.submission_seq} was never judged`,
        detail: `No verdict reached the contract within the ${fmtDuration(job.eval_window_secs)} evaluation window.`,
        data: { "Submitted file hash": job.current_output_hash },
      });
    }
    ev.push({
      when: null, tone: "fail", title: "Refunded",
      detail: `${refundReason(job)} ${near} went back to ${job.owner_id}'s budget. The provider was paid nothing.`,
      data: {},
    });
  }
  if (job.status === "Passed" || job.status === "Refunded") {
    ev.push({
      when: null, tone: job.canaries_revealed ? "pass" : "neutral",
      title: job.canaries_revealed ? "Hidden control records revealed" : "Hidden control records not revealed yet",
      detail: job.canaries_revealed
        ? "The evaluator published the canary set, and the contract checked that its SHA-256 equals the commitment made when the job was created."
        : "The evaluator publishes the canary set once a job is final; the contract will check it against the commitment.",
      data: {},
    });
  }
  return ev;
}

/** Why a refunded job was refunded, from stored fields only (the contract's three refund conditions). */
export function refundReason(job) {
  if (job.submission_seq > job.verdicts.length && job.submitted_at != null)
    return "The evaluator's window closed without a verdict, so the contract allowed a refund.";
  if (job.attempts_used >= job.max_attempts) return `All ${job.max_attempts} attempts failed.`;
  return "The deadline passed without a PASS.";
}

export function attemptsLine(job) {
  return `${job.attempts_used} of ${job.max_attempts} attempts used`;
}
