import type { Config, Policy } from "./near.mjs";
export interface PolicyForm { maxPerJob: string; maxOpenJobs: string; templates: string; providers: string; maxDeadlineHours: string; maxAttempts: string; expiresAt: string }
export const EMPTY_FORM: PolicyForm;
export function policyToForm(p: Policy): PolicyForm;
export function formToPolicy(f: PolicyForm, config: Config | null, nowSecs: number): { policy?: Policy; errors?: Partial<Record<keyof PolicyForm, string>> };
