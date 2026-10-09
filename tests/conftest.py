"""Offline tests must never consume credentials from the developer's environment."""
import pytest
from regtrace import extraction


@pytest.fixture(autouse=True)
def isolate_provider_credentials(monkeypatch, tmp_path):
    for name in ('GROQ_API_KEY', 'GROQ_API_KEY_FILE', 'GROQ_SECRETS_FILE'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(extraction, 'DEFAULT_SECRETS_FILE', tmp_path / 'unconfigured-secrets.json')
