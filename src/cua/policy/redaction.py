"""Redaction: nothing sensitive reaches artifacts, logs or evidence.

Three layers, applied to every string and structure before it is written anywhere:

1. **Known secret values** (resolved from the runtime environment) -> ``[SECRET]``.
2. **Known PII values** of this run (inputs/outputs the capability declares ``pii``) ->
   masked, keeping the last two characters so evidence stays debuggable (``***45``).
3. **Patterns** for regulated data the run didn't declare: SSNs, payment-card numbers
   (Luhn-checked, so arbitrary digit runs are left alone), e-mail addresses.

Plus: any mapping key that looks like a credential (``password``, ``token``, ...) has its
value replaced outright, whatever it contains.

Limits (stated in REPORT.md): pattern matching is best effort; a value that is neither
declared nor matches a pattern is not caught by this layer.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

SECRET = "[SECRET]"
_SECRET_KEY = re.compile(
    r"(pass(word|wd)?|pwd|secret|token|api[_-]?key|authorization|cookie|session)", re.I
)
_SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_CARD = re.compile(r"\b(?:\d[ -]?){12,18}\d\b")
_EMAIL = re.compile(r"\b[\w.+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b")


def _luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2 == 1:
            n = n * 2 - 9 if n > 4 else n * 2
        total += n
    return total % 10 == 0


def _whole(value: str) -> str:
    """Regex matching ``value`` only as a whole token (not inside a longer number/word).
    Valid in both Python and JavaScript."""
    return rf"(?<![A-Za-z0-9]){re.escape(value)}(?![A-Za-z0-9])"


def mask_value(value: str) -> str:
    """``12345`` -> ``***45``; short values are masked entirely."""
    return "*" * (len(value) - 2) + value[-2:] if len(value) >= 5 else "****"


class Redactor:
    def __init__(self) -> None:
        self._secrets: set[str] = set()
        self._pii: set[str] = set()

    def add_secret(self, value: str) -> None:
        if value:
            self._secrets.add(value)

    def add_pii(self, value: str) -> None:
        if value and len(value) >= 2:
            self._pii.add(value)

    def add_pii_values(self, values: Iterable[str]) -> None:
        for v in values:
            self.add_pii(v)

    # ------------------------------------------------------------------ text

    def text(self, value: str) -> str:
        out = value
        for secret in sorted(self._secrets, key=len, reverse=True):
            out = out.replace(secret, SECRET)
        for pii in sorted(self._pii, key=len, reverse=True):
            out = re.sub(_whole(pii), mask_value(pii).replace("\\", "\\\\"), out)
        out = _SSN.sub("***-**-****", out)
        out = _CARD.sub(self._card, out)
        out = _EMAIL.sub("[EMAIL]", out)
        return out

    @staticmethod
    def _card(m: re.Match[str]) -> str:
        digits = re.sub(r"\D", "", m.group(0))
        return "[CARD]" if 13 <= len(digits) <= 19 and _luhn_ok(digits) else m.group(0)

    # ------------------------------------------------------------------ structures

    def value(self, obj: Any) -> Any:
        """Redact a JSON-like structure (dicts, lists, strings) recursively."""
        if isinstance(obj, str):
            return self.text(obj)
        if isinstance(obj, Mapping):
            return {
                k: (SECRET if isinstance(k, str) and _SECRET_KEY.search(k) and v else self.value(v))
                for k, v in obj.items()
            }
        if isinstance(obj, list | tuple):
            return [self.value(v) for v in obj]
        return obj

    # ------------------------------------------------------------------ screenshots

    def dom_patterns(self) -> list[str]:
        """JavaScript regex sources for masking matching text in screenshots."""
        literal = [re.escape(v) for v in sorted(self._secrets, key=len, reverse=True)]
        literal += [_whole(v) for v in sorted(self._pii, key=len, reverse=True)]
        return [*literal, r"\b\d{3}-\d{2}-\d{4}\b", _EMAIL.pattern]
