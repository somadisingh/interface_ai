"""Finding capabilities on disk, validating caller inputs, and parsing extracted values."""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from cua.schema import Capability, load_capability
from cua.schema.actions import ParseAs
from cua.schema.capability import InputSpec


class CapabilityRegistry:
    """Capabilities stored as ``<root>/<product>/<id>/<version>.yaml``."""

    def __init__(self, root: Path | str = "capabilities") -> None:
        self.root = Path(root)

    def versions(self, product: str, cap_id: str) -> list[Path]:
        folder = self.root / product / cap_id
        files = folder.glob("*.yaml") if folder.is_dir() else []
        return sorted(files, key=lambda p: tuple(int(x) for x in p.stem.split(".")))

    def get(self, product: str, cap_id: str, version: str | None = None) -> Capability:
        if version is not None:
            return load_capability(self.root / product / cap_id / f"{version}.yaml")
        versions = self.versions(product, cap_id)
        if not versions:
            raise FileNotFoundError(f"no capability {product}/{cap_id} under {self.root}")
        return load_capability(versions[-1])

    def resolve_ref(self, ref: str) -> Capability:
        """``product/id@version`` or ``product/id`` (latest)."""
        m = re.fullmatch(r"([a-z][a-z0-9_]*)/([a-z0-9_.]+?)(?:@(\d+\.\d+\.\d+))?", ref)
        if not m:
            raise ValueError(f"not a capability reference: {ref!r}")
        return self.get(m.group(1), m.group(2), m.group(3))


class InputError(ValueError):
    pass


def validate_inputs(specs: Mapping[str, InputSpec], given: Mapping[str, str]) -> dict[str, str]:
    """Check caller inputs against the contract *before* touching the UI."""
    unknown = set(given) - set(specs)
    if unknown:
        raise InputError(f"unknown input(s): {', '.join(sorted(unknown))}")
    out: dict[str, str] = {}
    for name, spec in specs.items():
        value = given.get(name, spec.default)
        if value is None:
            if spec.required:
                raise InputError(f"missing required input {name!r}")
            continue
        if spec.pattern and not re.fullmatch(spec.pattern, value):
            raise InputError(f"input {name!r} does not match {spec.pattern!r}")
        if spec.enum is not None and value not in spec.enum:
            raise InputError(f"input {name!r} must be one of {spec.enum}")
        if spec.type in ("integer", "decimal", "money"):
            try:
                Decimal(value)
            except InvalidOperation as exc:
                raise InputError(f"input {name!r} must be a number") from exc
        out[name] = value
    return out


class ParseError(ValueError):
    pass


_MONEY = re.compile(r"^\(?(-)?\s*\$?\s*(-)?\s*([\d,]+(?:\.\d{1,2})?)\)?$")


def parse_value(text: str, parse: ParseAs, pattern: str | None = None) -> Any:
    raw = text.strip()
    if pattern:
        m = re.search(pattern, raw)
        if not m:
            raise ParseError(f"text does not match extract pattern {pattern!r}")
        raw = (m.group(1) if m.groups() else m.group(0)).strip()
    if parse == "text":
        if not raw:
            raise ParseError("empty text")
        return raw
    if parse == "money":
        m = _MONEY.match(raw)
        if not m:
            raise ParseError(f"not a money amount: {raw!r}")
        amount = Decimal(m.group(3).replace(",", ""))
        negative = bool(m.group(1) or m.group(2) or (raw.startswith("(") and raw.endswith(")")))
        return {"amount": f"{-amount if negative else amount:.2f}", "currency": "USD"}
    if parse == "integer":
        try:
            return int(raw.replace(",", ""))
        except ValueError as exc:
            raise ParseError(f"not an integer: {raw!r}") from exc
    if parse == "decimal":
        try:
            return str(Decimal(raw.replace(",", "")))
        except InvalidOperation as exc:
            raise ParseError(f"not a decimal: {raw!r}") from exc
    if parse == "date":
        for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%d-%b-%Y"):
            try:
                return datetime.strptime(raw, fmt).date().isoformat()
            except ValueError:
                continue
        raise ParseError(f"not a recognised date: {raw!r}")
    raise ParseError(f"unknown parse type {parse!r}")  # pragma: no cover
