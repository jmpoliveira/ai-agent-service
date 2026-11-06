"""Input sanitisation for text that ends up inside a prompt.

Sanitising reduces the attack surface, it does not remove prompt injection: tool
results remain untrusted data and the system prompt says so explicitly.
"""

from __future__ import annotations

import re
import unicodedata

from src.domain.errors import InvalidPromptError

# Control (Cc), format (Cf), surrogate (Cs), private-use (Co) and unassigned (Cn)
# characters. Cf covers zero-width and bidirectional-override characters, which are
# used to hide instructions from human readers. The price is that emoji ZWJ sequences
# fall apart into their parts, which is acceptable for an agent prompt.
_DROPPED_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn"})
_KEPT_WHITESPACE = frozenset({"\n", "\t"})
_CLOSING_FRAME = re.compile(r"</\s*tool_result", re.IGNORECASE)
_TRUNCATION_MARKER = "\n[output truncated]"


def _strip_unsafe_characters(text: str) -> str:
    return "".join(
        ch
        for ch in text
        if ch in _KEPT_WHITESPACE or unicodedata.category(ch) not in _DROPPED_CATEGORIES
    )


def sanitize_prompt(prompt: str, max_length: int) -> str:
    """Remove unsafe characters and validate that the prompt is non-empty and short enough."""
    cleaned = _strip_unsafe_characters(prompt).strip()
    if not cleaned:
        raise InvalidPromptError("Prompt must not be empty")
    if len(cleaned) > max_length:
        raise InvalidPromptError(f"Prompt exceeds the maximum length of {max_length} characters")
    return cleaned


def sanitize_tool_output(text: str, max_chars: int) -> str:
    """Remove unsafe characters from a tool result and cap its size."""
    cleaned = _strip_unsafe_characters(text).strip()
    if len(cleaned) > max_chars:
        return cleaned[:max_chars] + _TRUNCATION_MARKER
    return cleaned


def frame_tool_output(tool_name: str, text: str) -> str:
    """Wrap a tool result in explicit delimiters so the model can tell data from instructions.

    A closing delimiter inside the data is defused, otherwise a tool result could
    "escape" the frame and masquerade as text outside it.
    """
    defused = _CLOSING_FRAME.sub("<\\/tool_result", text)
    safe_name = re.sub(r"[^A-Za-z0-9_.:-]", "_", tool_name)
    return f'<tool_result name="{safe_name}">\n{defused}\n</tool_result>'
