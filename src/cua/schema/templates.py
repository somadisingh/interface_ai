"""Value templates: ``{{inputs.member_id}}`` and ``{{secrets.MOCKCORE_PASSWORD}}``.

Templates are how an artifact stays parameterised: the recorded flow never contains the
concrete values from the discovery run, and never contains credentials. Secrets are resolved
from the runtime environment at the moment of use and are never persisted or logged.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Literal

Namespace = Literal["inputs", "secrets"]

_TEMPLATE_RE = re.compile(r"\{\{\s*(inputs|secrets)\.([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


class TemplateError(ValueError):
    pass


def references(text: str) -> list[tuple[Namespace, str]]:
    """Return the (namespace, name) pairs referenced by ``text``.

    Raises ``TemplateError`` on malformed template syntax (e.g. ``{{ inputs }}``)."""
    refs: list[tuple[Namespace, str]] = []
    for m in _TEMPLATE_RE.finditer(text):
        ns: Namespace = "inputs" if m.group(1) == "inputs" else "secrets"
        refs.append((ns, m.group(2)))
    leftover = _TEMPLATE_RE.sub("", text)
    if "{{" in leftover or "}}" in leftover:
        raise TemplateError(f"malformed template in {text!r}")
    return refs


def render(text: str, inputs: Mapping[str, str], secrets: Mapping[str, str]) -> str:
    """Substitute template references. Raises ``TemplateError`` if a value is missing."""

    def sub(m: re.Match[str]) -> str:
        source = inputs if m.group(1) == "inputs" else secrets
        name = m.group(2)
        if name not in source:
            raise TemplateError(f"no value for {m.group(1)}.{name}")
        return source[name]

    references(text)  # validate syntax first
    return _TEMPLATE_RE.sub(sub, text)


def has_secret(text: str) -> bool:
    return any(ns == "secrets" for ns, _ in references(text))
