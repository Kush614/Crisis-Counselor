"""Tiny LLM wrapper for the optimizer.

Picks a provider from whatever key is present (Anthropic preferred for this kind
of careful rewriting, then OpenAI). If no key is set it falls back to a
deterministic STUB so the loop's plumbing — and the --mock demo — run with zero
setup. SDKs are imported lazily so importing this module never requires them.
"""

import os


class LLM:
    def __init__(self) -> None:
        self.provider = self._detect_provider()

    def _detect_provider(self) -> str:
        if os.getenv("ANTHROPIC_API_KEY"):
            return "anthropic"
        if os.getenv("OPENAI_API_KEY"):
            return "openai"
        return "stub"

    @property
    def is_stub(self) -> bool:
        return self.provider == "stub"

    def complete(self, system: str, user: str, max_tokens: int = 4000) -> str:
        """Return the model's text response. Stub returns "" (callers handle it)."""
        if self.provider == "anthropic":
            return self._anthropic(system, user, max_tokens)
        if self.provider == "openai":
            return self._openai(system, user, max_tokens)
        return ""

    def _anthropic(self, system: str, user: str, max_tokens: int) -> str:
        from anthropic import Anthropic  # lazy

        client = Anthropic()
        model = os.getenv("OPTIMIZER_MODEL", "claude-opus-4-8")
        resp = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")

    def _openai(self, system: str, user: str, max_tokens: int) -> str:
        from openai import OpenAI  # lazy

        client = OpenAI()
        model = os.getenv("OPTIMIZER_MODEL", "gpt-4o")
        resp = client.chat.completions.create(
            model=model,
            max_tokens=max_tokens,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        return resp.choices[0].message.content or ""
