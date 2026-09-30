"""Secrets: resolved from the runtime environment at the moment of use, never persisted.

Artifacts only ever contain ``{{secrets.NAME}}`` references. Every value handed out is
registered with the run's redactor first, so it can't leak into logs or evidence even by
accident (e.g. an error message that echoes a field's content).
"""

from __future__ import annotations

import os
from collections.abc import Iterator, Mapping

from cua.policy.redaction import Redactor


class MissingSecretError(KeyError):
    pass


class SecretStore(Mapping[str, str]):
    def __init__(self, redactor: Redactor, source: Mapping[str, str] | None = None) -> None:
        self._redactor = redactor
        self._source = os.environ if source is None else source

    def __getitem__(self, name: str) -> str:
        value = self._source.get(name)
        if not value:
            raise MissingSecretError(f"secret {name!r} is not set in the environment")
        self._redactor.add_secret(value)
        return value

    def __iter__(self) -> Iterator[str]:
        return iter(())  # secrets are fetched by name only, never enumerated

    def __len__(self) -> int:
        return 0

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and bool(self._source.get(name))
