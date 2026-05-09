import os

import pytest

from core.pouw_executor import PoUWExecutor, RealPoUWTask, RealTaskType
from core.security import create_ssl_context, get_runtime_environment


def test_runtime_environment_prefers_app_env(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("POUW_ENV", "development")
    monkeypatch.delenv("MAINCOIN_ENV", raising=False)
    monkeypatch.delenv("MAINCOIN_PRODUCTION", raising=False)

    assert get_runtime_environment() == "production"


def test_runtime_environment_uses_maincoin_production_flag(monkeypatch):
    monkeypatch.delenv("APP_ENV", raising=False)
    monkeypatch.delenv("POUW_ENV", raising=False)
    monkeypatch.delenv("MAINCOIN_ENV", raising=False)
    monkeypatch.setenv("MAINCOIN_PRODUCTION", "true")

    assert get_runtime_environment() == "production"


def test_tls_client_requires_ca_in_production(tmp_path, monkeypatch):
    cert = tmp_path / "node.crt"
    key = tmp_path / "node.key"
    cert.write_text("dummy")
    key.write_text("dummy")

    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.delenv("MAINCOIN_CA_CERT", raising=False)

    with pytest.raises(RuntimeError, match="MAINCOIN_CA_CERT"):
        create_ssl_context(str(cert), str(key), server=False)


def test_custom_code_blocked_in_production(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.delenv("POUW_ALLOW_CUSTOM_CODE", raising=False)

    executor = PoUWExecutor()
    task = RealPoUWTask(
        task_id="t1",
        task_type=RealTaskType.CUSTOM_CODE,
        params={"code": "result = {'ok': True}", "data": {}},
        difficulty=1,
        expected_threshold=0.5,
    )

    result = executor.execute_task(task, miner_id="m1")
    assert result.verified is False
    assert isinstance(result.result, dict)
    assert "disabled" in str(result.result.get("error", "")).lower()


def test_custom_code_can_be_explicitly_enabled(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("POUW_ALLOW_CUSTOM_CODE", "true")

    executor = PoUWExecutor()
    task = RealPoUWTask(
        task_id="t2",
        task_type=RealTaskType.CUSTOM_CODE,
        params={"code": "result = {'ok': True}", "data": {}},
        difficulty=1,
        expected_threshold=0.5,
    )

    result = executor.execute_task(task, miner_id="m1")
    assert isinstance(result.result, dict)
    assert result.result.get("ok") is True
