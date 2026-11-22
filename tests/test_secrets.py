import subprocess

import pytest

from planning_aux import secrets


def test_get_password(monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="s3cret\nlogin: bob\n")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert secrets.get_password("foo/bar") == "s3cret"
    assert calls == [["pass", "show", "foo/bar"]]


def test_get_password_missing_entry(monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        raise subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(subprocess.CalledProcessError):
        secrets.get_password()
    assert calls == [["pass", "show", "Service/FamilleService"]]
