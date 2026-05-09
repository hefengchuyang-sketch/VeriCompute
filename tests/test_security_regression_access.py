import base64
import hashlib
import os

from core.file_transfer import ChunkedFileManager
from core.rpc.models import RPCRequest
from core.rpc.models import RPCError
from core.rpc.server import RPCHTTPHandler
from core.rpc_service import NodeRPCService
from core.sandbox_executor import SandboxExecutor


def _make_auth(user: str, is_admin: bool = False):
    return {
        "user": user,
        "user_address": user,
        "is_admin": is_admin,
    }


def test_upload_session_and_file_owner_access_control(tmp_path):
    service = NodeRPCService()
    service._file_manager = ChunkedFileManager(base_dir=str(tmp_path))

    payload = b"hello-owner-control"
    checksum = hashlib.sha256(payload).hexdigest()

    init = service._file_init_upload(
        filename="input.json",
        totalSize=len(payload),
        checksumSha256=checksum,
        auth_context=_make_auth("owner_user"),
    )
    upload_id = init["uploadId"]

    service._file_upload_chunk(
        uploadId=upload_id,
        chunkIndex=0,
        data=base64.b64encode(payload).decode("ascii"),
        auth_context=_make_auth("owner_user"),
    )

    try:
        service._file_get_upload_progress(
            uploadId=upload_id,
            auth_context=_make_auth("other_user"),
        )
        assert False, "non-owner should not read upload progress"
    except RPCError as e:
        assert "only uploader" in str(e)

    finalized = service._file_finalize_upload(
        uploadId=upload_id,
        auth_context=_make_auth("owner_user"),
    )
    file_ref = finalized["fileRef"]

    try:
        service._file_get_info(fileRef=file_ref, auth_context=_make_auth("other_user"))
        assert False, "non-owner should not read file info"
    except RPCError as e:
        assert "only file owner" in str(e)

    info = service._file_get_info(fileRef=file_ref, auth_context=_make_auth("owner_user"))
    assert info["owner"] == "owner_user"


def test_sandbox_fail_closed_without_docker_or_inprocess_fallback(monkeypatch):
    monkeypatch.setenv("ALLOW_INPROCESS_FALLBACK", "false")

    executor = SandboxExecutor(force_simulate=True, log_fn=lambda _msg: None)
    ctx = executor.create_context(
        miner_id="miner_demo",
        job_id="job_demo",
        task_data_hash="abc123",
        task_code="result = {'ok': True}",
        task_data={"k": "v"},
    )

    result = executor.execute(ctx.context_id, simulate_computation=False)
    assert result is not None
    assert result.success is False
    assert result.error_message == "docker_required_for_real_execution"


def test_admin_can_access_foreign_file(tmp_path):
    service = NodeRPCService()
    service._file_manager = ChunkedFileManager(base_dir=str(tmp_path))

    payload = b"admin-check"
    checksum = hashlib.sha256(payload).hexdigest()

    init = service._file_init_upload(
        filename="input.json",
        totalSize=len(payload),
        checksumSha256=checksum,
        auth_context=_make_auth("owner_user"),
    )
    upload_id = init["uploadId"]

    service._file_upload_chunk(
        uploadId=upload_id,
        chunkIndex=0,
        data=base64.b64encode(payload).decode("ascii"),
        auth_context=_make_auth("owner_user"),
    )

    finalized = service._file_finalize_upload(
        uploadId=upload_id,
        auth_context=_make_auth("owner_user"),
    )
    file_ref = finalized["fileRef"]

    info = service._file_get_info(
        fileRef=file_ref,
        auth_context=_make_auth("admin_user", is_admin=True),
    )
    assert info["file_ref"] == file_ref


def test_rpc_method_whitelist_helper_blocks_non_allowed_method():
    assert RPCHTTPHandler.is_method_allowed(None, "chain_getInfo") is True
    allowed = {"chain_getInfo", "tx_getStatus"}
    assert RPCHTTPHandler.is_method_allowed(allowed, "chain_getInfo") is True
    assert RPCHTTPHandler.is_method_allowed(allowed, "wallet_getInfo") is False


def test_wallet_get_info_requires_authenticated_user_permission():
    service = NodeRPCService()
    req = RPCRequest(method="wallet_getInfo", params={}, id=1)

    guest_resp = service.handle_request(req, auth_context={})
    guest_payload = guest_resp.to_dict()
    assert "error" in guest_payload
    assert guest_payload["error"]["code"] == -32403

    user_resp = service.handle_request(req, auth_context=_make_auth("u1"))
    user_payload = user_resp.to_dict()
    assert "result" in user_payload


def test_transaction_history_endpoints_require_authenticated_user_permission():
    service = NodeRPCService()

    for method in ("account_getTransactions", "wallet_getTransactions"):
        req = RPCRequest(method=method, params={}, id=1)

        guest_resp = service.handle_request(req, auth_context={})
        guest_payload = guest_resp.to_dict()
        assert "error" in guest_payload
        assert guest_payload["error"]["code"] == -32403

        user_resp = service.handle_request(req, auth_context=_make_auth("u1"))
        user_payload = user_resp.to_dict()
        assert "result" in user_payload
