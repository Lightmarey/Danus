from __future__ import annotations

import argparse
import os

import pytest

from danus import codex_backend


def test_configure_api_writes_keyless_provider(tmp_path, monkeypatch):
    home = tmp_path / "codex-home"
    monkeypatch.setenv("CODEX_HOME", str(home))
    monkeypatch.setenv("CODEX_API_BASE_URL", "https://example.test/v1")
    monkeypatch.setenv("CODEX_API_MODEL", "gpt-test")
    monkeypatch.setenv("DANUS_CODEX_API_KEY", "secret-not-written")

    config = codex_backend.configure_api()

    text = config.read_text(encoding="utf-8")
    assert 'model_provider = "danus_api"' in text
    assert 'base_url = "https://example.test/v1"' in text
    assert 'env_key = "DANUS_CODEX_API_KEY"' in text
    assert "secret-not-written" not in text


def test_configure_api_refuses_unowned_config(tmp_path, monkeypatch):
    home = tmp_path / "codex-home"
    home.mkdir()
    (home / "config.toml").write_text("model = 'custom'\n", encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(home))
    monkeypatch.setenv("CODEX_API_BASE_URL", "https://example.test/v1")
    monkeypatch.setenv("DANUS_CODEX_API_KEY", "secret")

    with pytest.raises(FileExistsError):
        codex_backend.configure_api()


def test_login_removes_only_generated_provider(tmp_path, monkeypatch):
    home = tmp_path / "codex-home"
    home.mkdir()
    config = home / "config.toml"
    config.write_text(codex_backend._provider_text("https://example.test/v1", "m"))
    monkeypatch.setenv("CODEX_HOME", str(home))
    monkeypatch.setattr(codex_backend.codex, "resolve_bin", lambda: "codex")

    seen = {}

    class Result:
        returncode = 0

    monkeypatch.setattr(
        codex_backend.subprocess,
        "run",
        lambda command, **kwargs: seen.update(command=command, kwargs=kwargs) or Result(),
    )
    args = argparse.Namespace(codex_action="login")
    assert codex_backend.dispatch(args) == 0
    assert not config.exists()
    assert seen["command"] == ["codex", "login"]
