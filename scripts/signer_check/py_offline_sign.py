"""Phase 0 offline signer check (no network, no real keys).

Builds and signs a FunctionCall transaction with py-near using a throwaway key
derived from a public test seed, then writes the parameters and the base64 signed
transaction to stdout as JSON for js_crosscheck.mjs to verify independently.
"""
import base64
import hashlib
import json
import sys

import base58
from nacl import signing
from py_near import transactions

SEED = hashlib.sha256(b"kavren signer-check throwaway key - public, never fund").digest()
sk = signing.SigningKey(SEED)
pk = bytes(sk.verify_key)
secret_b58 = "ed25519:" + base58.b58encode(SEED + pk).decode()

params = {
    "signer_id": "buyer-agent.kavren-check.testnet",
    "receiver_id": "kavren.kavren-check.testnet",
    "nonce": 42,
    "block_hash_b58": base58.b58encode(hashlib.sha256(b"fake block").digest()).decode(),
    "method": "create_job",
    "args": {"job": "fixture", "amount": "240000000000000000000000"},
    "gas": 30 * 10**12,
    "deposit": 0,
    "public_key": "ed25519:" + base58.b58encode(pk).decode(),
    "secret_key_for_test_only": secret_b58,
}
action = transactions.create_function_call_action(
    params["method"], json.dumps(params["args"], separators=(",", ":")).encode(), params["gas"], params["deposit"]
)
signed_b64 = transactions.sign_and_serialize_transaction(
    params["signer_id"], secret_b58, params["receiver_id"], params["nonce"], [action],
    base58.b58decode(params["block_hash_b58"]),
)

# self-check: signed = borsh(tx) || 0x00 (ed25519 tag) || 64-byte signature over sha256(borsh(tx))
raw = base64.b64decode(signed_b64)
tx_bytes, tag, sig = raw[:-65], raw[-65], raw[-64:]
assert tag == 0, "expected ed25519 signature tag"
sk.verify_key.verify(hashlib.sha256(tx_bytes).digest(), sig)  # raises if invalid
params["signed_tx_b64"] = signed_b64
params["tx_sha256_hex"] = hashlib.sha256(tx_bytes).hexdigest()
json.dump(params, sys.stdout, indent=2)
