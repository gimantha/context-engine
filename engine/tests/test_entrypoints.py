"""Independent API and worker startup tests."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from context_engine.api.main import main as api_main
from context_engine.worker.main import main as worker_main

MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"


def _environment(monkeypatch, database):
    monkeypatch.setenv("CONTEXT_ENGINE_DB_PATH", str(database))
    monkeypatch.setenv("CONTEXT_ENGINE_MIGRATIONS_PATH", str(MIGRATIONS))


def test_api_and_worker_startup_checks_are_independent(tmp_path, monkeypatch, capsys):
    _environment(monkeypatch, tmp_path / "control.db")
    monkeypatch.setattr(sys, "argv", ["context-engine-api", "--check"])
    api_main()
    assert "API startup check passed" in capsys.readouterr().out

    monkeypatch.setattr(sys, "argv", ["context-engine-worker", "--check"])
    worker_main()
    assert "worker startup check passed" in capsys.readouterr().out


def test_worker_starts_in_provider_mode_without_the_provider_installed(
    tmp_path, monkeypatch, capsys
):
    _environment(monkeypatch, tmp_path / "control.db")
    monkeypatch.setenv("CONTEXT_ENGINE_KNOWLEDGE_BACKEND", "provider")
    monkeypatch.setattr(sys, "argv", ["context-engine-worker", "--check"])
    worker_main()
    assert "worker startup check passed" in capsys.readouterr().out

    monkeypatch.setenv("CONTEXT_ENGINE_KNOWLEDGE_BACKEND", "everything")
    with pytest.raises(RuntimeError):
        worker_main()
