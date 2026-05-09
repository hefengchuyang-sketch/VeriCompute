# -*- coding: utf-8 -*-
"""Task payload privacy helpers for verifiable compute tasks.

This module is the first concrete implementation slice for the privacy design
in the technical review document. It intentionally lives at ``core/task_encryption.py``
for the current repository layout; after the planned folder migration it can move
to ``core/security/task_encryption.py`` with a compatibility shim.
"""

from __future__ import annotations

import base64
import hashlib
import os
from dataclasses import dataclass
from typing import Any, Mapping, Optional

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import x25519
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives import hashes

from core.serialization import hash_canonical


AES_256_GCM = "AES_256_GCM"
X25519_HKDF_SHA256 = "X25519_HKDF_SHA256"
DATA_KEY_BYTES = 32
NONCE_BYTES = 12


def _b64e(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _b64d(data: str) -> bytes:
    return base64.b64decode(data.encode("ascii"))


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def redact_secret(value: str, prefix: int = 6, suffix: int = 4) -> str:
    """Return a log-safe representation of a secret-like value."""
    if not value:
        return ""
    if len(value) <= prefix + suffix:
        return "***"
    return f"{value[:prefix]}...{value[-suffix:]}"


def generate_data_key() -> bytes:
    """Generate a per-task AES-256 data key."""
    return os.urandom(DATA_KEY_BYTES)


def create_input_commitment(plain_payload: bytes) -> str:
    """Commit to the original task input without exposing it on-chain."""
    return sha256_hex(plain_payload)


def create_result_commitment(plain_result: bytes) -> str:
    """Commit to the original task result without exposing it on-chain."""
    return sha256_hex(plain_result)


@dataclass(frozen=True)
class EncryptedPayload:
    """AES-GCM encrypted payload container."""

    ciphertext_b64: str
    nonce_b64: str
    payload_hash: str
    encryption_scheme: str = AES_256_GCM

    def to_dict(self) -> dict:
        return {
            "ciphertext_b64": self.ciphertext_b64,
            "nonce_b64": self.nonce_b64,
            "payload_hash": self.payload_hash,
            "encryption_scheme": self.encryption_scheme,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "EncryptedPayload":
        return cls(
            ciphertext_b64=str(data["ciphertext_b64"]),
            nonce_b64=str(data["nonce_b64"]),
            payload_hash=str(data["payload_hash"]),
            encryption_scheme=str(data.get("encryption_scheme", AES_256_GCM)),
        )


def encrypt_payload(plain_payload: bytes, data_key: bytes, aad: bytes = b"") -> EncryptedPayload:
    """Encrypt a task input or output with AES-256-GCM."""
    if len(data_key) != DATA_KEY_BYTES:
        raise ValueError(f"data_key must be {DATA_KEY_BYTES} bytes")
    nonce = os.urandom(NONCE_BYTES)
    ciphertext = AESGCM(data_key).encrypt(nonce, plain_payload, aad or None)
    return EncryptedPayload(
        ciphertext_b64=_b64e(ciphertext),
        nonce_b64=_b64e(nonce),
        payload_hash=sha256_hex(ciphertext),
    )


def decrypt_payload(payload: EncryptedPayload, data_key: bytes, aad: bytes = b"") -> bytes:
    """Decrypt an AES-256-GCM payload and verify its encrypted hash."""
    if len(data_key) != DATA_KEY_BYTES:
        raise ValueError(f"data_key must be {DATA_KEY_BYTES} bytes")
    ciphertext = _b64d(payload.ciphertext_b64)
    if sha256_hex(ciphertext) != payload.payload_hash:
        raise ValueError("encrypted payload hash mismatch")
    return AESGCM(data_key).decrypt(_b64d(payload.nonce_b64), ciphertext, aad or None)


@dataclass(frozen=True)
class EncryptedTaskInput:
    task_id: str
    input_uri: str
    input_commitment: str
    encrypted_payload_hash: str
    encryption_scheme: str = AES_256_GCM
    key_exchange_scheme: str = X25519_HKDF_SHA256
    client_public_key: str = ""

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "input_uri": self.input_uri,
            "input_commitment": self.input_commitment,
            "encrypted_payload_hash": self.encrypted_payload_hash,
            "encryption_scheme": self.encryption_scheme,
            "key_exchange_scheme": self.key_exchange_scheme,
            "client_public_key": self.client_public_key,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "EncryptedTaskInput":
        return cls(
            task_id=str(data["task_id"]),
            input_uri=str(data["input_uri"]),
            input_commitment=str(data["input_commitment"]),
            encrypted_payload_hash=str(data["encrypted_payload_hash"]),
            encryption_scheme=str(data.get("encryption_scheme", AES_256_GCM)),
            key_exchange_scheme=str(data.get("key_exchange_scheme", X25519_HKDF_SHA256)),
            client_public_key=str(data.get("client_public_key", "")),
        )

    def canonical_hash(self) -> str:
        return hash_canonical(self.to_dict())


@dataclass(frozen=True)
class EncryptedTaskOutput:
    task_id: str
    output_uri: str
    result_commitment: str
    encrypted_output_hash: str
    encryption_scheme: str = AES_256_GCM
    recipient_public_key: str = ""

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "output_uri": self.output_uri,
            "result_commitment": self.result_commitment,
            "encrypted_output_hash": self.encrypted_output_hash,
            "encryption_scheme": self.encryption_scheme,
            "recipient_public_key": self.recipient_public_key,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "EncryptedTaskOutput":
        return cls(
            task_id=str(data["task_id"]),
            output_uri=str(data["output_uri"]),
            result_commitment=str(data["result_commitment"]),
            encrypted_output_hash=str(data["encrypted_output_hash"]),
            encryption_scheme=str(data.get("encryption_scheme", AES_256_GCM)),
            recipient_public_key=str(data.get("recipient_public_key", "")),
        )

    def canonical_hash(self) -> str:
        return hash_canonical(self.to_dict())


def generate_x25519_keypair() -> tuple[str, str]:
    """Return ``(private_key_b64, public_key_b64)`` for X25519 key exchange."""
    private_key = x25519.X25519PrivateKey.generate()
    public_key = private_key.public_key()
    private_bytes = private_key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_bytes = public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return _b64e(private_bytes), _b64e(public_bytes)


def _load_x25519_private(private_key_b64: str) -> x25519.X25519PrivateKey:
    return x25519.X25519PrivateKey.from_private_bytes(_b64d(private_key_b64))


def _load_x25519_public(public_key_b64: str) -> x25519.X25519PublicKey:
    return x25519.X25519PublicKey.from_public_bytes(_b64d(public_key_b64))


def _derive_wrap_key(shared_secret: bytes, task_id: str, worker_id: str) -> bytes:
    info = f"maincoin-task-key-grant:{task_id}:{worker_id}".encode("utf-8")
    return HKDF(
        algorithm=hashes.SHA256(),
        length=DATA_KEY_BYTES,
        salt=None,
        info=info,
    ).derive(shared_secret)


@dataclass(frozen=True)
class WorkerKeyGrant:
    task_id: str
    worker_id: str
    worker_public_key: str
    grantor_public_key: str
    encrypted_data_key: str
    nonce_b64: str
    expires_height: int
    key_exchange_scheme: str = X25519_HKDF_SHA256

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "worker_id": self.worker_id,
            "worker_public_key": self.worker_public_key,
            "grantor_public_key": self.grantor_public_key,
            "encrypted_data_key": self.encrypted_data_key,
            "nonce_b64": self.nonce_b64,
            "expires_height": self.expires_height,
            "key_exchange_scheme": self.key_exchange_scheme,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "WorkerKeyGrant":
        return cls(
            task_id=str(data["task_id"]),
            worker_id=str(data["worker_id"]),
            worker_public_key=str(data["worker_public_key"]),
            grantor_public_key=str(data["grantor_public_key"]),
            encrypted_data_key=str(data["encrypted_data_key"]),
            nonce_b64=str(data["nonce_b64"]),
            expires_height=int(data["expires_height"]),
            key_exchange_scheme=str(data.get("key_exchange_scheme", X25519_HKDF_SHA256)),
        )

    def canonical_hash(self) -> str:
        return hash_canonical(self.to_dict())


def create_worker_key_grant(
    *,
    task_id: str,
    worker_id: str,
    data_key: bytes,
    grantor_private_key_b64: str,
    worker_public_key_b64: str,
    expires_height: int,
) -> WorkerKeyGrant:
    """Encrypt a task data key for a specific worker public key."""
    if len(data_key) != DATA_KEY_BYTES:
        raise ValueError(f"data_key must be {DATA_KEY_BYTES} bytes")
    grantor_private = _load_x25519_private(grantor_private_key_b64)
    worker_public = _load_x25519_public(worker_public_key_b64)
    shared = grantor_private.exchange(worker_public)
    wrap_key = _derive_wrap_key(shared, task_id, worker_id)
    nonce = os.urandom(NONCE_BYTES)
    aad = f"{task_id}:{worker_id}".encode("utf-8")
    encrypted_data_key = AESGCM(wrap_key).encrypt(nonce, data_key, aad)
    grantor_public_b64 = _b64e(
        grantor_private.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
    )
    return WorkerKeyGrant(
        task_id=task_id,
        worker_id=worker_id,
        worker_public_key=worker_public_key_b64,
        grantor_public_key=grantor_public_b64,
        encrypted_data_key=_b64e(encrypted_data_key),
        nonce_b64=_b64e(nonce),
        expires_height=expires_height,
    )


def open_worker_key_grant(grant: WorkerKeyGrant, worker_private_key_b64: str) -> bytes:
    """Decrypt a data key grant with the worker private key."""
    worker_private = _load_x25519_private(worker_private_key_b64)
    grantor_public = _load_x25519_public(grant.grantor_public_key)
    shared = worker_private.exchange(grantor_public)
    wrap_key = _derive_wrap_key(shared, grant.task_id, grant.worker_id)
    aad = f"{grant.task_id}:{grant.worker_id}".encode("utf-8")
    return AESGCM(wrap_key).decrypt(
        _b64d(grant.nonce_b64),
        _b64d(grant.encrypted_data_key),
        aad,
    )
