// How the proof page describes what produced each submission. The provider reports its model and
// service when it uploads; until attestation is verified, the page must say so plainly.

const SERVICES = { "cloud-api.near.ai": "NEAR AI Cloud" };

/** One line per submission, from GET /jobs/{id}/provenance. */
export function describeProvenance(p) {
  return (p?.submissions ?? []).map((s) => {
    const host = s.service || null;
    const service = host ? (SERVICES[host] ? `${SERVICES[host]} (${host})` : host) : "an unnamed service";
    const verified = s.verification === "attestation_verified";
    let label = "Reported by the provider, not independently verified";
    if (verified) {
      const gpu = s.attestation?.gpu_evidence === "verified" ? " and NVIDIA GPU evidence" : "";
      label = `Verified by the evaluator: NEAR AI's signature over this exact planning call, from a model key ` +
        `attested by Intel TDX${gpu}; re-running its plan reproduces this file. The TEE's software build is not pinned.`;
    } else if (s.attestation_note) {
      label += ` (the provider's evidence did not verify: ${s.attestation_note})`;
    }
    return { seq: s.seq, text: `${s.model} on ${service}`, label, verified };
  });
}
