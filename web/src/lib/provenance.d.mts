export type ProvenanceLine = { seq: number; text: string; label: string; verified: boolean };
export function describeProvenance(p: unknown): ProvenanceLine[];
