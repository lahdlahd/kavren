// How the proof page describes what produced each submission. The provider reports its model and
// service when it uploads; until attestation is verified, the page must say so plainly.

const SERVICES = { "cloud-api.near.ai": "NEAR AI Cloud" };

/** One line per submission, from GET /jobs/{id}/provenance. */
export function describeProvenance(p) {
  return (p?.submissions ?? []).map((s) => {
    const host = s.service || null;
    const service = host ? (SERVICES[host] ? `${SERVICES[host]} (${host})` : host) : "an unnamed service";
    const verified = s.verification === "attestation_verified";
    return {
      seq: s.seq,
      text: `${s.model} on ${service}`,
      label: verified ? "Verified by NEAR AI attestation" : "Reported by the provider, not independently verified",
      verified,
    };
  });
}
