// Phase 0: verify a py-near signed transaction with near-api-js (independent implementation).
// Usage: python3 py_offline_sign.py > /tmp/signed.json && node js_crosscheck.mjs /tmp/signed.json
import { readFileSync } from "node:fs";
import { createHash } from "node:crypto";
import {
  actions, createTransaction, decodeSignedTransaction, encodeTransaction,
  KeyPair, PublicKey, baseDecode,
} from "near-api-js";

const p = JSON.parse(readFileSync(process.argv[2], "utf8"));
const signed = decodeSignedTransaction(Buffer.from(p.signed_tx_b64, "base64"));
const tx = signed.transaction;

// 1. rebuild the same transaction with near-api-js and compare bytes
const pub = PublicKey.fromString(p.public_key);
const rebuilt = createTransaction(
  p.signer_id, pub, p.receiver_id, BigInt(p.nonce),
  [actions.functionCall(p.method, Buffer.from(JSON.stringify(p.args)), BigInt(p.gas), BigInt(p.deposit))],
  baseDecode(p.block_hash_b58),
);
const a = Buffer.from(encodeTransaction(tx));
const b = Buffer.from(encodeTransaction(rebuilt));
const sameBytes = a.equals(b);

// 2. verify the signature with near-api-js
const digest = createHash("sha256").update(a).digest();
const sigOk = pub.verify(new Uint8Array(digest), new Uint8Array(signed.signature.ed25519Signature.data));

// 3. the same key loaded by near-api-js signs identically (ed25519 is deterministic)
const kp = KeyPair.fromString(p.secret_key_for_test_only);
const jsSig = Buffer.from(kp.sign(new Uint8Array(digest)).signature);
const sameSig = jsSig.equals(Buffer.from(new Uint8Array(signed.signature.ed25519Signature.data)));

const out = { sameTransactionBytes: sameBytes, signatureValid: sigOk, identicalSignature: sameSig,
  txSha256Matches: digest.toString("hex") === p.tx_sha256_hex,
  decoded: { signer: tx.signerId, receiver: tx.receiverId, nonce: tx.nonce.toString(),
    method: tx.actions[0].functionCall.methodName, deposit: tx.actions[0].functionCall.deposit.toString() } };
console.log(JSON.stringify(out, null, 2));
process.exit(sameBytes && sigOk && sameSig && out.txSha256Matches ? 0 : 1);
