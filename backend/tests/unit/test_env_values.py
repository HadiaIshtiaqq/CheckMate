from app.core.config import _env


def test_env_values_are_trimmed(monkeypatch):
    monkeypatch.setenv("CM_T1", "  abc123 \n")
    monkeypatch.setenv("CM_T2", '"quoted-secret"')
    monkeypatch.setenv("CM_T3", "'  '")
    assert _env("CM_T1") == "abc123"
    assert _env("CM_T2") == "quoted-secret"
    assert _env("CM_T3", "default") == "default"
