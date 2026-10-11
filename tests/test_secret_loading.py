"""Managed-secret behavior without reading real credentials or calling Azure."""

import json
import sys
from types import ModuleType, SimpleNamespace

import pytest

from underwriting_platform.configuration import secrets


def test_repository_env_is_not_automatically_loaded(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("TEST_UNREQUESTED_SECRET=dummy-test-value\n")
    monkeypatch.setattr(secrets, "PROJECT_ROOT", tmp_path)
    monkeypatch.delenv("UW_DEV_ENV_FILE", raising=False)
    monkeypatch.delenv("UW_KEY_VAULT_URL", raising=False)
    monkeypatch.delenv("TEST_UNREQUESTED_SECRET", raising=False)
    secrets.load_secret_environment()
    import os

    assert "TEST_UNREQUESTED_SECRET" not in os.environ


def test_model_configuration_repr_does_not_expose_keys():
    from underwriting_platform.llm.client import LLMConfig

    assert "dummy-test-key" not in repr(LLMConfig("fixture", "model", "dummy-test-key"))


def test_development_secret_file_inside_repository_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(secrets, "PROJECT_ROOT", tmp_path)
    monkeypatch.setenv("UW_DEV_ENV_FILE", str(tmp_path / ".env"))
    with pytest.raises(ValueError, match="outside the repository"):
        secrets.load_secret_environment()


def test_key_vault_managed_identity_and_alias_mapping(monkeypatch):
    calls = []

    class Credential:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    class Client(Credential):
        def __init__(self, vault_url, credential):
            calls.append(("vault", vault_url))
            assert isinstance(credential, Credential)

        def get_secret(self, name):
            calls.append(("secret", name))
            return SimpleNamespace(value="synthetic-secret-not-real")

    identity = ModuleType("azure.identity")
    identity.DefaultAzureCredential = Credential
    keyvault = ModuleType("azure.keyvault.secrets")
    keyvault.SecretClient = Client
    monkeypatch.setitem(sys.modules, "azure", ModuleType("azure"))
    monkeypatch.setitem(sys.modules, "azure.identity", identity)
    monkeypatch.setitem(sys.modules, "azure.keyvault", ModuleType("azure.keyvault"))
    monkeypatch.setitem(sys.modules, "azure.keyvault.secrets", keyvault)
    monkeypatch.delenv("UW_DEV_ENV_FILE", raising=False)
    monkeypatch.setenv("UW_KEY_VAULT_URL", "https://synthetic-test-vault.vault.azure.net")
    monkeypatch.setenv(
        "UW_KEY_VAULT_SECRET_ALIASES", json.dumps({"AZURE_CLAUDE_API_KEY": "azure-claude-api-key"})
    )
    monkeypatch.setenv("AZURE_CLAUDE_API_KEY", "temporary-test-placeholder")
    secrets.load_secret_environment()
    import os

    assert os.environ["AZURE_CLAUDE_API_KEY"] == "synthetic-secret-not-real"
    assert ("secret", "azure-claude-api-key") in calls


@pytest.mark.parametrize(
    "url",
    [
        "http://vault.vault.azure.net",
        "https://unrelated.example",
        "https://user:pass@vault.vault.azure.net",
    ],
)
def test_invalid_vault_destinations_fail_closed(monkeypatch, url):
    monkeypatch.delenv("UW_DEV_ENV_FILE", raising=False)
    monkeypatch.setenv("UW_KEY_VAULT_URL", url)
    with pytest.raises(ValueError, match="Key Vault"):
        secrets.load_secret_environment()
