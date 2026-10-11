"""Secrets enter through environment injection or Azure Key Vault, never Git."""

from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import urlparse

from underwriting_platform.configuration.paths import PROJECT_ROOT


def load_secret_environment() -> None:
    development_file = os.getenv("UW_DEV_ENV_FILE")
    if development_file:
        path = Path(development_file).resolve()
        repository = next(
            (p for p in (PROJECT_ROOT, *PROJECT_ROOT.parents) if (p / ".git").exists()),
            PROJECT_ROOT,
        )
        if path.is_relative_to(repository.resolve()):
            raise ValueError("Development secrets must live outside the repository")
        from dotenv import load_dotenv

        load_dotenv(path, override=False)
    vault_url = os.getenv("UW_KEY_VAULT_URL")
    if not vault_url:
        return
    parsed = urlparse(vault_url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or not parsed.hostname.endswith(".vault.azure.net")
        or parsed.username
        or parsed.query
    ):
        raise ValueError("Expected an Azure Key Vault HTTPS URL")
    aliases = json.loads(os.getenv("UW_KEY_VAULT_SECRET_ALIASES", "{}"))
    if not isinstance(aliases, dict) or not aliases:
        raise ValueError("Key Vault secret aliases must be explicitly configured")
    from azure.identity import DefaultAzureCredential
    from azure.keyvault.secrets import SecretClient

    with DefaultAzureCredential() as credential:
        with SecretClient(vault_url=vault_url, credential=credential) as client:
            values = {}
            for name, alias in aliases.items():
                if (
                    not isinstance(name, str)
                    or not name.replace("_", "").isalnum()
                    or not isinstance(alias, str)
                ):
                    raise ValueError("Invalid secret alias")
                value = client.get_secret(alias).value
                if not value:
                    raise ValueError("Required managed secret unavailable")
                values[name] = value
            os.environ.update(values)
