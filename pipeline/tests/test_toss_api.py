import logging
import subprocess

from pipeline.utils import toss_api


def test_credentials_use_env_override_and_keychain_for_only_missing_value(monkeypatch):
    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, stdout="test-only-keychain-secret\n", stderr="ignored")

    monkeypatch.setenv("TOSS_API_KEY", "test-env-client-id")
    monkeypatch.delenv("TOSS_SECRET_KEY", raising=False)

    assert toss_api._credentials(run=fake_run) == ("test-env-client-id", "test-only-keychain-secret")
    assert calls == [(
        ["security", "find-generic-password", "-s", "toss-api-clientSecret", "-w"],
        {"capture_output": True, "check": False, "text": True, "timeout": 3},
    )]


def test_credentials_return_empty_on_keychain_failure_without_logging_output(monkeypatch, caplog):
    marker = "test-only-keychain-output"
    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 44, stdout=marker, stderr=marker)

    monkeypatch.delenv("TOSS_API_KEY", raising=False)
    monkeypatch.delenv("TOSS_SECRET_KEY", raising=False)

    with caplog.at_level(logging.DEBUG):
        assert toss_api._credentials(run=fake_run) == ("", "")

    assert calls == [
        (["security", "find-generic-password", "-s", "toss-api-clinetId", "-w"], {
            "capture_output": True, "check": False, "text": True, "timeout": 3,
        }),
        (["security", "find-generic-password", "-s", "toss-api-clientSecret", "-w"], {
            "capture_output": True, "check": False, "text": True, "timeout": 3,
        }),
    ]
    assert marker not in caplog.text
