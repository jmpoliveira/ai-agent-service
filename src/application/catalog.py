"""Registry of the configured chat models, addressed by provider alias."""

from __future__ import annotations

from collections.abc import Mapping

from src.application.ports import ChatModel
from src.domain.errors import UnknownProviderError


class ModelCatalog:
    """Maps a provider alias (``ollama``, ``gemini``...) to a ready ``ChatModel``.

    The caller chooses the provider; the agent never chooses its own model
    (see docs/adr/0003-model-selection.md).
    """

    def __init__(self, models: Mapping[str, ChatModel], default: str) -> None:
        if not models:
            raise ValueError("At least one chat model must be configured")
        if default not in models:
            raise ValueError(f"Default provider '{default}' is not configured")
        self._models = dict(models)
        self._default = default

    @property
    def aliases(self) -> tuple[str, ...]:
        return tuple(self._models)

    @property
    def default(self) -> str:
        return self._default

    def get(self, alias: str | None) -> ChatModel:
        """Return the model for ``alias``, or the default one when ``alias`` is None."""
        key = self._default if alias is None else alias
        model = self._models.get(key)
        if model is None:
            raise UnknownProviderError(f"Provider '{key}' is not available")
        return model
