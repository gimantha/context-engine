"""Tests for engine-owned environment configuration."""

from pathlib import Path

from context_engine.config import KnowledgeBackendSettings


def test_knowledge_backend_settings_load_only_engine_names(monkeypatch, tmp_path):
    """Engine settings load the public profile without relying on native names."""

    monkeypatch.setenv("CONTEXT_ENGINE_TELEMETRY_ENABLED", "yes")
    monkeypatch.setenv("CONTEXT_ENGINE_ACCESS_CONTROL_REQUIRED", "true")
    monkeypatch.setenv("CONTEXT_ENGINE_MODEL_PROVIDER", "local-model-service")
    monkeypatch.setenv("CONTEXT_ENGINE_MODEL_API_KEY", "model-key")
    monkeypatch.setenv("CONTEXT_ENGINE_EMBEDDING_DIMENSIONS", "768")
    monkeypatch.setenv("CONTEXT_ENGINE_KNOWLEDGE_STORAGE_PATH", str(tmp_path))
    monkeypatch.setenv("CONTEXT_ENGINE_RUN_LIVE_PROVIDER", "on")

    settings = KnowledgeBackendSettings.from_env()

    assert settings.telemetry_enabled is True
    assert settings.access_control_required is True
    assert settings.model_provider == "local-model-service"
    assert settings.model_api_key == "model-key"
    assert settings.embedding_dimensions == 768
    assert settings.storage_path == Path(tmp_path)
    assert settings.live_test_enabled is True
