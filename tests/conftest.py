"""Shared fixtures. Every test stays inside tmp_path or the repository."""

from __future__ import annotations

from pathlib import Path

import pytest

from garbage_collection_automation import configuration
from garbage_collection_automation.configuration import (
    AddressConfig,
    CollectionConfig,
    Config,
    ExportConfig,
    TodoistExportConfig,
    WebConfig,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"

MINIMAL = """
[address]
postcode = "1234AB"
house_number = "56"
"""


def read_fixture(name: str) -> str:
    """Captured mijnafvalwijzer.nl responses; see the Data source section in the README."""
    return (FIXTURES / name).read_text(encoding="utf-8")


def make_config(
    *,
    postcode: str = "1234AB",
    house_number: str = "21",
    addition: str = "",
    todoist: TodoistExportConfig | None = None,
    web: WebConfig | None = None,
    **collection: object,
) -> Config:
    """A Config for the address the fixtures were captured from.

    The API key is filled in unless a test says otherwise: every query needs one,
    and the real one is not in this repository.
    """
    collection.setdefault("api_key", "test-api-key")
    return Config(
        address=AddressConfig(postcode=postcode, house_number=house_number, addition=addition),
        collection=CollectionConfig(**collection),
        export=ExportConfig(todoist=todoist or TodoistExportConfig()),
        web=web or WebConfig(),
    )


@pytest.fixture(autouse=True)
def _no_secrets_in_env(monkeypatch):
    """Both secret env vars leak into every load(); never inherit a real one."""
    monkeypatch.delenv(configuration.TOKEN_ENV_VAR, raising=False)
    monkeypatch.delenv(configuration.API_KEY_ENV_VAR, raising=False)


@pytest.fixture
def write_config(tmp_path):
    def write(text: str) -> Path:
        path = tmp_path / "config.toml"
        path.write_text(text)
        return path

    return write
