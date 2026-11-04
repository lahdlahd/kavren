"use client";
import "@near-wallet-selector/modal-ui/styles.css";
import type { WalletSelector } from "@near-wallet-selector/core";
import type { WalletSelectorModal } from "@near-wallet-selector/modal-ui";
import { useCallback, useEffect, useState } from "react";
import { CONTRACT_ID, DEFAULT_AGENT, NETWORK, accountUrl, chain, txUrl } from "@/lib/config";
import { fmtTime, nearToYocto, yoctoToNear, type Budget, type Config } from "@/lib/near.mjs";
import { EMPTY_FORM, formToPolicy, policyToForm, type PolicyForm } from "@/lib/policy.mjs";

const TGAS = 10n ** 12n;
const ONE_YOCTO = 1n;
type Notice = { tone: "pass" | "fail" | "neutral"; text: string; tx?: string } | null;

// The site never holds a key. Signing in only connects your wallet account (no access key
// is created for this site), and every action below opens your wallet to approve it.
export function OwnerConsole() {
  const [selector, setSelector] = useState<WalletSelector | null>(null);
  const [modal, setModal] = useState<WalletSelectorModal | null>(null);
  const [account, setAccount] = useState<string | null>(null);
  const [agent, setAgent] = useState(DEFAULT_AGENT);
  const [budget, setBudget] = useState<Budget | null | undefined>(undefined);
  const [config, setConfig] = useState<Config | null>(null);
  const [form, setForm] = useState<PolicyForm>(EMPTY_FORM);
  const [fund, setFund] = useState("");
  const [withdraw, setWithdraw] = useState("");
  const [errors, setErrors] = useState<Partial<Record<keyof PolicyForm | "fund" | "withdraw", string>>>({});
  const [busy, setBusy] = useState<string | null>(null);
  const [notice, setNotice] = useState<Notice>(null);

  useEffect(() => {
    let sub: { unsubscribe(): void } | undefined;
    (async () => {
      const [{ setupWalletSelector }, { setupMeteorWallet }, { setupModal }] = await Promise.all([
        import("@near-wallet-selector/core"), import("@near-wallet-selector/meteor-wallet"), import("@near-wallet-selector/modal-ui"),
      ]);
      const s = await setupWalletSelector({ network: NETWORK, modules: [setupMeteorWallet()] });
      setSelector(s);
      setModal(setupModal(s, { theme: "light", description: "KAVREN never receives your keys. Each action asks your wallet for approval." }));
      const pick = (st: { accounts: { accountId: string; active: boolean }[] }) =>
        setAccount(st.accounts.find((a) => a.active)?.accountId ?? null);
      pick(s.store.getState());
      sub = s.store.observable.subscribe(pick);
    })().catch((e) => setNotice({ tone: "fail", text: `Couldn't start the wallet connector: ${e.message}` }));
    chain.getConfig().then(setConfig).catch(() => setConfig(null));
    return () => sub?.unsubscribe();
  }, []);

  const refresh = useCallback(async () => {
    if (!account || !agent) return;
    setBudget(undefined);
    try {
      const b = await chain.getBudget(account, agent.trim());
      setBudget(b);
      if (b) setForm(policyToForm(b.policy));
      else setForm({ ...EMPTY_FORM, expiresAt: new Date(Date.now() + 30 * 86400e3).toISOString().slice(0, 10) });
    } catch (e) {
      setBudget(null);
      setNotice({ tone: "fail", text: `Couldn't read the budget from NEAR: ${(e as Error).message}` });
    }
  }, [account, agent]);
  useEffect(() => { refresh(); }, [refresh]);

  async function send(label: string, method: string, args: object, deposit: bigint, gasT = 30n) {
    if (!selector) return;
    setBusy(label);
    setNotice(null);
    try {
      const { actionCreators } = await import("@near-js/transactions");
      const wallet = await selector.wallet();
      const out = await wallet.signAndSendTransaction({
        receiverId: CONTRACT_ID,
        actions: [actionCreators.functionCall(method, args, gasT * TGAS, deposit)],
      });
      const failure = out && (out.status as { Failure?: unknown })?.Failure;
      if (failure) throw new Error(JSON.stringify(failure));
      setNotice({ tone: "pass", text: `${label}: done.`, tx: out?.transaction?.hash });
      await refresh();
    } catch (e) {
      setNotice({ tone: "fail", text: `${label} didn't go through: ${(e as Error).message}` });
    } finally {
      setBusy(null);
    }
  }

  function policyOrErrors() {
    const r = formToPolicy(form, config, Date.now() / 1000);
    setErrors(r.errors ?? {});
    return r.policy;
  }

  function onFund(e: React.FormEvent) {
    e.preventDefault();
    const policy = policyOrErrors();
    let yocto: string;
    try { yocto = nearToYocto(fund); } catch (err) { setErrors((x) => ({ ...x, fund: (err as Error).message })); return; }
    if (!policy) return;
    send(`Add ${fund} NEAR`, "fund_budget", { agent_id: agent.trim(), policy }, BigInt(yocto), 50n);
  }
  function onUpdate() {
    const policy = policyOrErrors();
    if (policy) send("Save rules", "update_policy", { agent_id: agent.trim(), policy }, ONE_YOCTO);
  }
  function onWithdraw(e: React.FormEvent) {
    e.preventDefault();
    let yocto: string;
    try { yocto = nearToYocto(withdraw); } catch (err) { setErrors((x) => ({ ...x, withdraw: (err as Error).message })); return; }
    if (budget && BigInt(yocto) > BigInt(budget.balance)) { setErrors((x) => ({ ...x, withdraw: "More than the unlocked balance" })); return; }
    setErrors((x) => ({ ...x, withdraw: undefined }));
    send(`Withdraw ${withdraw} NEAR`, "withdraw_budget", { agent_id: agent.trim(), amount: yocto }, ONE_YOCTO);
  }
  function onRevoke() {
    if (!confirm(`Stop ${agent} from creating any new jobs? Jobs already running are not affected. Adding funds later lets it work again.`)) return;
    send("Revoke agent", "revoke_agent", { agent_id: agent.trim() }, ONE_YOCTO);
  }

  const f = (k: keyof PolicyForm, label: string, hint?: string, type = "text") => (
    <div className="field">
      <label htmlFor={k}>{label}</label>
      <input id={k} type={type} value={form[k]} aria-invalid={!!errors[k]} aria-describedby={`${k}-h`}
        onChange={(e) => setForm({ ...form, [k]: e.target.value })} />
      <span id={`${k}-h`} className={errors[k] ? "error" : "hint"}>{errors[k] ?? hint}</span>
    </div>
  );

  if (!account) {
    return (
      <div className="panel">
        <p>Connect the wallet that owns the budget. KAVREN doesn&apos;t receive a key: each change below asks your wallet to approve one transaction.</p>
        <div className="actions"><button onClick={() => modal?.show()} disabled={!modal}>Connect wallet</button></div>
        {notice && <div className="notice" data-tone={notice.tone}>{notice.text}</div>}
      </div>
    );
  }

  return (
    <>
      <div className="panel">
        <p>Signed in as <a className="mono" href={accountUrl(account)}>{account}</a>.{" "}
          <button className="secondary" style={{ padding: "0.2rem 0.6rem", fontSize: "0.9rem" }}
            onClick={async () => { await (await selector!.wallet()).signOut(); }}>Disconnect</button></p>
        <div className="field">
          <label htmlFor="agent">Agent account</label>
          <input id="agent" type="text" value={agent} onChange={(e) => setAgent(e.target.value)} onBlur={refresh} />
          <span className="hint">The buyer agent that spends this budget.</span>
        </div>
        {budget === undefined && <p className="muted" style={{ marginTop: "1rem" }}>Reading the budget…</p>}
        {budget === null && <p style={{ marginTop: "1rem" }}>You haven&apos;t funded this agent yet. Set its rules and add funds below.</p>}
        {budget && (
          <dl className="hashes" style={{ fontSize: "1rem", marginTop: "1.25rem" }}>
            <dt>Unlocked balance</dt><dd style={{ fontFamily: "var(--sans)" }}>{yoctoToNear(budget.balance)} NEAR</dd>
            <dt>Jobs running</dt><dd style={{ fontFamily: "var(--sans)" }}>{budget.open_jobs} of {budget.policy.max_open_jobs} allowed</dd>
            <dt>Agent</dt><dd style={{ fontFamily: "var(--sans)" }}>{budget.revoked ? "Revoked: can't create new jobs" : "Allowed to hire"}</dd>
            <dt>Rules expire</dt><dd style={{ fontFamily: "var(--sans)" }}>{fmtTime(budget.policy.expires_at)}</dd>
          </dl>
        )}
      </div>

      {notice && (
        <div className="notice" data-tone={notice.tone} role="status">
          {notice.text}{notice.tx && <> <a href={txUrl(notice.tx)}>View the transaction</a></>}
        </div>
      )}

      <form className="panel" onSubmit={onFund} style={{ marginTop: "1.5rem" }}>
        <h2>Rules the agent must follow</h2>
        <p className="muted small" style={{ marginTop: "0.5rem" }}>The contract enforces these on every job the agent creates.</p>
        <div className="row2">
          {f("maxPerJob", "Most it may pay per job (NEAR)")}
          {f("maxOpenJobs", "Jobs running at once")}
        </div>
        {f("providers", "Providers it may hire", "NEAR accounts, separated by commas")}
        {f("templates", "Job types", "Template IDs, separated by commas, e.g. contacts-v1")}
        <div className="row2">
          {f("maxDeadlineHours", "Longest job deadline (hours)")}
          {f("maxAttempts", "Attempts per job", config ? `1 to ${config.max_attempts_cap}` : undefined)}
        </div>
        {f("expiresAt", "Rules expire on", "After this date the agent can't create jobs", "date")}
        <div className="actions">
          {budget && <button type="button" className="secondary" disabled={!!busy} onClick={onUpdate}>{busy === "Save rules" ? "Waiting for wallet…" : "Save rules"}</button>}
        </div>
        <h2 style={{ marginTop: "2rem" }}>Add funds</h2>
        <div className="field">
          <label htmlFor="fund">Amount (NEAR)</label>
          <input id="fund" type="text" inputMode="decimal" placeholder="Type an amount" value={fund} aria-invalid={!!errors.fund} onChange={(e) => setFund(e.target.value)} />
          <span className={errors.fund ? "error" : "hint"}>{errors.fund ?? "Saves the rules above at the same time. A small part pays for on-chain storage."}</span>
        </div>
        <div className="actions"><button type="submit" disabled={!!busy || !fund.trim()}>{busy?.startsWith("Add") ? "Waiting for wallet…" : fund.trim() ? `Add ${fund.trim()} NEAR` : "Add funds"}</button></div>
      </form>

      {budget && (
        <form className="panel" onSubmit={onWithdraw}>
          <h2>Take money back</h2>
          <div className="field">
            <label htmlFor="withdraw">Amount (NEAR)</label>
            <input id="withdraw" type="text" inputMode="decimal" value={withdraw} aria-invalid={!!errors.withdraw} onChange={(e) => setWithdraw(e.target.value)} />
            <span className={errors.withdraw ? "error" : "hint"}>{errors.withdraw ?? `Up to ${yoctoToNear(budget.balance)} NEAR. Money held in running jobs stays there until they finish.`}</span>
          </div>
          <div className="actions">
            <button type="submit" disabled={!!busy}>{busy?.startsWith("Withdraw") ? "Waiting for wallet…" : "Withdraw"}</button>
            <button type="button" className="danger" disabled={!!busy || budget.revoked} onClick={onRevoke}>Revoke agent</button>
          </div>
        </form>
      )}
    </>
  );
}
