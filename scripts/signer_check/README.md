# Signer check (Phase 0)

The question this answers: **can our Python services sign NEAR transactions correctly with a restricted function-call key?** If they can't, the fallback is near-api-js (see the brief, section 11).

## 1. Offline check (done; no network, no real keys)

```
python3 py_offline_sign.py > /tmp/signed.json
npm install
node js_crosscheck.mjs /tmp/signed.json
```

The first command signs a `create_job` call with py-near, using a throwaway key derived from a public seed. The last command has near-api-js independently decode the transaction, rebuild it, verify the signature and re-sign it. Expected output: every field `true`, exit code 0.

## 2. Live testnet check (the owner runs this; it needs a testnet account)

You need:
- one testnet account you control, and
- any testnet contract with a **non-payable** change method to call (for example a hello-world/greeting example contract; first confirm it exists on the explorer, and note its method name).

```
pip install py-near==1.2.22
python3 new_fc_key.py --account YOURNAME.testnet --receiver CONTRACT.testnet --methods METHOD
```

- This writes the **private** key to `.env.signer-check` at the repository root. That file is gitignored, has mode 0600, and the key is never printed.
- It prints the **public** key and how to add it to *your* account as a function-call key. You sign that step yourself with near-cli-rs. The script never touches your account or your full-access key.

Then:

```
SIGNER_CHECK_ARGS='{"greeting":"kavren"}' python3 testnet_fc_check.py
```

(Use the arguments the method expects.)

**Expected result: all checks PASS.**
- The key on-chain is a FunctionCall key for the right contract and methods.
- The allowed call with 0 deposit succeeds.
- The same call with 1 yoctoNEAR attached is rejected.
- A 1 yoctoNEAR transfer is rejected.
- A method outside the list is rejected.

Rejected transactions fail validation and move no funds. Paste the output back into the chat.

When you're done, delete the function-call key from your account if you like, and delete `.env.signer-check`.
