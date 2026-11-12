from __future__ import annotations

import pytest

from src.application.catalog import ModelCatalog
from src.domain.errors import UnknownProviderError
from tests.fakes import ScriptedChatModel


def _catalog() -> tuple[ModelCatalog, ScriptedChatModel, ScriptedChatModel]:
    local = ScriptedChatModel([], provider="ollama")
    cloud = ScriptedChatModel([], provider="anthropic")
    return ModelCatalog({"ollama": local, "anthropic": cloud}, default="ollama"), local, cloud


def test_get_returns_the_default_when_no_alias_is_given():
    # Arrange
    catalog, local, _ = _catalog()

    # Act / Assert
    assert catalog.get(None) is local


def test_get_returns_the_requested_model():
    catalog, _, cloud = _catalog()
    assert catalog.get("anthropic") is cloud


def test_get_rejects_an_unknown_alias():
    catalog, _, _ = _catalog()
    with pytest.raises(UnknownProviderError, match="gemini"):
        catalog.get("gemini")


def test_aliases_and_default_are_exposed():
    catalog, _, _ = _catalog()
    assert catalog.aliases == ("ollama", "anthropic")
    assert catalog.default == "ollama"


def test_catalog_requires_at_least_one_model():
    with pytest.raises(ValueError, match="At least one"):
        ModelCatalog({}, default="ollama")


def test_catalog_requires_the_default_to_be_configured():
    with pytest.raises(ValueError, match="Default provider"):
        ModelCatalog({"ollama": ScriptedChatModel([])}, default="gemini")
