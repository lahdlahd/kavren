export const DEFAULT_TESTNET_RPCS: string[];
export function yoctoToNear(yocto: string | bigint, digits?: number): string;
export function nearToYocto(near: string): string;
export function shortHash(h: string | null | undefined, n?: number): string;
export function fmtTime(secs: number | null | undefined): string;
export function fmtDuration(secs: number): string;
export interface Verdict { seq: number; verdict: "PASS" | "FAIL"; output_hash: string; report_hash: string; at: number }
export interface Job {
  job_id: number; owner_id: string; buyer_agent: string; provider_id: string; evaluator_id: string; amount: string;
  template_id: string; spec_json: string; draft_id: string; input_hash: string; work_hash: string; canary_commitment: string;
  created_at: number; deadline: number; max_attempts: number; attempts_used: number; eval_window_secs: number;
  submission_seq: number; current_output_hash: string | null; submitted_at: number | null;
  status: "Open" | "Submitted" | "Passed" | "Refunded"; verdicts: Verdict[]; canaries_revealed: boolean;
}
export interface Policy {
  max_per_job: string; max_open_jobs: number; allowed_templates: string[]; allowed_providers: string[];
  max_deadline_secs: number; max_attempts: number; expires_at: number;
}
export interface Budget { owner_id: string; agent_id: string; balance: string; open_jobs: number; revoked: boolean; policy: Policy }
export interface Config { eval_window_secs: number; min_deadline_secs: number; max_deadline_secs: number; max_attempts_cap: number }
export interface Chain {
  view(method: string, args?: object): Promise<unknown>;
  getJob(id: number): Promise<Job | null>;
  getBudget(owner: string, agent: string): Promise<Budget | null>;
  getConfig(): Promise<Config>;
  getEvaluator(): Promise<string>;
}
export function makeChain(o: { contractId: string; rpcs?: string[]; fetchImpl?: typeof fetch }): Chain;
export function findLatestJobId(getJob: (id: number) => Promise<unknown>): Promise<number>;
export function sha256Hex(bytes: ArrayBuffer | Uint8Array): Promise<string>;
