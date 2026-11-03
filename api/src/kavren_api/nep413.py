"""NEP-413 signed messages (off-chain login with a NEAR key).

hash = sha256( u32_le(2**31 + 413) || borsh(Payload{message, nonce[32], recipient, callback_url?}) )
signature = ed25519_sign(hash)
"""
from __future__ import annotations

import base64
import hashlib
import struct
from typing import Optional

import base58
from nacl.exceptions import BadSignatureError
from nacl.signing import SigningKey, VerifyKey

TAG = 2**31 + 413


def _borsh_str(s: str) -> bytes:
    b = s.encode("utf-8")
    return struct.pack("<I", len(b)) + b


def payload_hash(message: str, nonce: bytes, recipient: str, callback_url: Optional[str] = None) -> bytes:
    if len(nonce) != 32:
        raise ValueError("nonce must be 32 bytes")
    body = _borsh_str(message) + nonce + _borsh_str(recipient)
    body += b"\x00" if callback_url is None else b"\x01" + _borsh_str(callback_url)
    return hashlib.sha256(struct.pack("<I", TAG) + body).digest()


def parse_public_key(pk: str) -> bytes:
    if not pk.startswith("ed25519:"):
        raise ValueError("only ed25519 keys are supported")
    raw = base58.b58decode(pk[len("ed25519:"):])
    if len(raw) != 32:
        raise ValueError("bad public key length")
    return raw


def verify(public_key: str, signature_b64: str, message: str, nonce: bytes, recipient: str) -> bool:
    try:
        vk = VerifyKey(parse_public_key(public_key))
        sig = base64.b64decode(signature_b64, validate=True)
        vk.verify(payload_hash(message, nonce, recipient), sig)
        return True
    except (BadSignatureError, ValueError, Exception):
        return False


def sign(private_key: str, message: str, nonce: bytes, recipient: str) -> tuple[str, str]:
    """For agents: returns (public_key, signature_b64). private_key is 'ed25519:<base58 64 bytes>'."""
    raw = base58.b58decode(private_key.replace("ed25519:", ""))
    sk = SigningKey(raw[:32])
    sig = sk.sign(payload_hash(message, nonce, recipient)).signature
    pub = "ed25519:" + base58.b58encode(bytes(sk.verify_key)).decode()
    return pub, base64.b64encode(sig).decode()
