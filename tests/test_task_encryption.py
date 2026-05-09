# -*- coding: utf-8 -*-
"""Tests for task payload privacy helpers."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.task_encryption import (  # noqa: E402
    EncryptedTaskInput,
    EncryptedTaskOutput,
    create_input_commitment,
    create_result_commitment,
    create_worker_key_grant,
    decrypt_payload,
    encrypt_payload,
    generate_data_key,
    generate_x25519_keypair,
    open_worker_key_grant,
    redact_secret,
)


def test_payload_encrypt_decrypt_roundtrip() -> None:
    key = generate_data_key()
    plain = b"private user task input"
    encrypted = encrypt_payload(plain, key, aad=b"task-1")

    assert encrypted.payload_hash
    assert plain not in encrypted.ciphertext_b64.encode("utf-8")
    assert decrypt_payload(encrypted, key, aad=b"task-1") == plain


def test_payload_hash_tampering_is_rejected() -> None:
    key = generate_data_key()
    encrypted = encrypt_payload(b"payload", key)
    tampered = type(encrypted)(
        ciphertext_b64=encrypted.ciphertext_b64,
        nonce_b64=encrypted.nonce_b64,
        payload_hash="0" * 64,
    )

    with pytest.raises(ValueError, match="hash mismatch"):
        decrypt_payload(tampered, key)


def test_task_input_metadata_excludes_plaintext() -> None:
    plain = b"do not put this raw payload on chain"
    key = generate_data_key()
    encrypted = encrypt_payload(plain, key)
    metadata = EncryptedTaskInput(
        task_id="task_1",
        input_uri="ipfs://example",
        input_commitment=create_input_commitment(plain),
        encrypted_payload_hash=encrypted.payload_hash,
        client_public_key="client_pub",
    )

    data = metadata.to_dict()
    assert "do not put" not in repr(data)
    assert data["input_commitment"] == create_input_commitment(plain)
    assert EncryptedTaskInput.from_dict(data) == metadata
    assert len(metadata.canonical_hash()) == 64


def test_task_output_metadata_excludes_plaintext() -> None:
    result = b"private result bytes"
    key = generate_data_key()
    encrypted = encrypt_payload(result, key)
    metadata = EncryptedTaskOutput(
        task_id="task_1",
        output_uri="ipfs://result",
        result_commitment=create_result_commitment(result),
        encrypted_output_hash=encrypted.payload_hash,
        recipient_public_key="client_pub",
    )

    data = metadata.to_dict()
    assert "private result" not in repr(data)
    assert EncryptedTaskOutput.from_dict(data) == metadata
    assert len(metadata.canonical_hash()) == 64


def test_worker_key_grant_roundtrip() -> None:
    grantor_private, _grantor_public = generate_x25519_keypair()
    worker_private, worker_public = generate_x25519_keypair()
    data_key = generate_data_key()

    grant = create_worker_key_grant(
        task_id="task_1",
        worker_id="worker_1",
        data_key=data_key,
        grantor_private_key_b64=grantor_private,
        worker_public_key_b64=worker_public,
        expires_height=123,
    )

    assert grant.worker_public_key == worker_public
    assert grant.encrypted_data_key
    assert data_key not in grant.encrypted_data_key.encode("utf-8")
    assert open_worker_key_grant(grant, worker_private) == data_key
    assert len(grant.canonical_hash()) == 64


def test_wrong_worker_key_cannot_open_grant() -> None:
    grantor_private, _grantor_public = generate_x25519_keypair()
    _worker_private, worker_public = generate_x25519_keypair()
    wrong_private, _wrong_public = generate_x25519_keypair()
    data_key = generate_data_key()

    grant = create_worker_key_grant(
        task_id="task_1",
        worker_id="worker_1",
        data_key=data_key,
        grantor_private_key_b64=grantor_private,
        worker_public_key_b64=worker_public,
        expires_height=123,
    )

    with pytest.raises(Exception):
        open_worker_key_grant(grant, wrong_private)


def test_redact_secret() -> None:
    assert redact_secret("") == ""
    assert redact_secret("short") == "***"
    assert redact_secret("abcdefghijklmnopqrstuvwxyz") == "abcdef...wxyz"
