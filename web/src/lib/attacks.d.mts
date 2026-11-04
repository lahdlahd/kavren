export interface AttackRow { n: number; title: string; job: number | null; layer: string; expected: string; actual: string; ok: boolean; txs: { label: string; url: string; hash: string }[] }
export interface AttackReport { missing?: boolean; generatedAt: string | null; contractId: string | null; matched: number; total: number; rows: AttackRow[]; money: Record<string, string> }
export function parseAttacks(md: string): AttackReport;
