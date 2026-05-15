"""Provider-neutral agent runtime contract for Adaptive Markdown."""
from __future__ import annotations

from typing import Any, AsyncIterator, Protocol


AgentEvent = dict[str, Any]


class AgentRuntime(Protocol):
    """Common interface implemented by provider-specific coding agents."""

    @property
    def current_model(self) -> str:
        """The provider-specific model id currently backing the session."""
        ...

    @property
    def model_aliases(self) -> list[str]:
        """Friendly model names the viewer may offer for this runtime."""
        ...

    def resolve_model(self, name: str | None) -> str:
        """Map a friendly model alias to a provider-specific model id."""
        ...

    async def start(self, model: str | None = None) -> None:
        """Initialize the runtime session."""
        ...

    async def shutdown(self) -> None:
        """Release any runtime resources."""
        ...

    async def reset(self, model: str | None = None) -> None:
        """Start a new conversation, optionally with a different model."""
        ...

    async def run_turn(self, text: str) -> AsyncIterator[AgentEvent]:
        """Run one agent turn and yield viewer protocol events."""
        ...
