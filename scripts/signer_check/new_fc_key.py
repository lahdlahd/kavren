"""Generate a fresh ed25519 keypair for a NEAR function-call access key.

- Writes the PRIVATE key only to a local env file (gitignored pattern .env.*),
  with file mode 0600. It is never printed.
- Prints the PUBLIC key, which the account holder adds to their own account as a
  function-call key. This script never touches any account.

Usage: python3 new_fc_key.py --account <your-account.testnet> --receiver <contract.testnet> \
           --methods <method1,method2> [--out ../../.env.signer-check]
"""
import argparse
import os
import sys
from pathlib import Path

import base58
from nacl import signing

ap = argparse.ArgumentParser()
ap.add_argument("--account", required=True)
ap.add_argument("--receiver", required=True)
ap.add_argument("--methods", required=True, help="comma-separated method names")
ap.add_argument("--out", default=str(Path(__file__).resolve().parents[2] / ".env.signer-check"))
a = ap.parse_args()

out = Path(a.out)
if out.exists():
    sys.exit(f"{out} already exists; refusing to overwrite a key file")

sk = signing.SigningKey.generate()
pk = bytes(sk.verify_key)
secret = "ed25519:" + base58.b58encode(bytes(sk) + pk).decode()
public = "ed25519:" + base58.b58encode(pk).decode()

fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w") as f:
    f.write(f"SIGNER_CHECK_ACCOUNT_ID={a.account}\n")
    f.write(f"SIGNER_CHECK_FC_PRIVATE_KEY={secret}\n")
    f.write(f"SIGNER_CHECK_RECEIVER_ID={a.receiver}\n")
    f.write(f"SIGNER_CHECK_METHODS={a.methods}\n")

print(f"Private key written to {out} (mode 0600). It was not printed.\n")
print(f"PUBLIC KEY: {public}\n")
print("Add it to YOUR account as a function-call key (you sign this, not the script).")
print("With near-cli-rs, the interactive flow is:  near account add-key " + a.account)
print("  -> grant-function-call-access")
print(f"  -> allowance: 0.25 NEAR   contract: {a.receiver}   methods: {a.methods}")
print(f"  -> use-manually-provided-public-key {public}")
print("  -> network: testnet -> sign with your usual method -> send")
