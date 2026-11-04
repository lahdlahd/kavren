// The owner's policy form <-> the contract's Policy struct. The checks mirror the
// contract's validate_policy so mistakes show up before a wallet prompt; the
// contract still enforces them on-chain.

import { nearToYocto, yoctoToNear } from "./near.mjs";

export function policyToForm(p) {
  return {
    maxPerJob: yoctoToNear(p.max_per_job, 24),
    maxOpenJobs: String(p.max_open_jobs),
    templates: p.allowed_templates.join(", "),
    providers: p.allowed_providers.join(", "),
    maxDeadlineHours: String(p.max_deadline_secs / 3600),
    maxAttempts: String(p.max_attempts),
    expiresAt: new Date(p.expires_at * 1000).toISOString().slice(0, 10),
  };
}

export const EMPTY_FORM = {
  maxPerJob: "1", maxOpenJobs: "2", templates: "contacts-v1", providers: "",
  maxDeadlineHours: "24", maxAttempts: "3", expiresAt: "",
};

const list = (s) => s.split(",").map((x) => x.trim()).filter(Boolean);
const ACCOUNT = /^(([a-z\d]+[-_])*[a-z\d]+\.)*([a-z\d]+[-_])*[a-z\d]+$/;

/** Returns { policy } or { errors: { field: message } }. `config` is the contract's get_config. */
export function formToPolicy(f, config, nowSecs) {
  const errors = {};
  let maxPerJob;
  try {
    maxPerJob = nearToYocto(f.maxPerJob);
    if (BigInt(maxPerJob) <= 0n) errors.maxPerJob = "Must be more than 0";
  } catch (e) { errors.maxPerJob = e.message; }
  const openJobs = Number(f.maxOpenJobs);
  if (!Number.isInteger(openJobs) || openJobs < 1 || openJobs > 100) errors.maxOpenJobs = "A whole number from 1 to 100";
  const templates = list(f.templates);
  if (templates.length < 1 || templates.length > 16) errors.templates = "List 1 to 16 templates";
  const providers = list(f.providers);
  if (providers.length < 1 || providers.length > 16) errors.providers = "List 1 to 16 provider accounts";
  else if (providers.some((a) => !ACCOUNT.test(a) || a.length < 2 || a.length > 64)) errors.providers = "One of these is not a valid NEAR account";
  const deadline = Math.round(Number(f.maxDeadlineHours) * 3600);
  if (!(deadline > 0)) errors.maxDeadlineHours = "Enter hours, e.g. 24";
  else if (config && deadline < config.min_deadline_secs) errors.maxDeadlineHours = `At least ${config.min_deadline_secs / 60} minutes`;
  const attempts = Number(f.maxAttempts);
  const cap = config ? config.max_attempts_cap : 5;
  if (!Number.isInteger(attempts) || attempts < 1 || attempts > cap) errors.maxAttempts = `A whole number from 1 to ${cap}`;
  const exp = Math.floor(Date.parse(`${f.expiresAt}T23:59:59Z`) / 1000);
  if (!f.expiresAt || Number.isNaN(exp)) errors.expiresAt = "Pick a date";
  else if (exp <= nowSecs) errors.expiresAt = "Must be in the future";
  if (Object.keys(errors).length) return { errors };
  return {
    policy: {
      max_per_job: maxPerJob, max_open_jobs: openJobs, allowed_templates: templates,
      allowed_providers: providers, max_deadline_secs: deadline, max_attempts: attempts, expires_at: exp,
    },
  };
}
