"""YAML (de)serialization with a strict loader.

Plain YAML 1.1 loaders silently change values: ``01234`` becomes the integer 668 (octal),
``no`` becomes ``False``, ``12:30`` becomes 750. For an artifact that carries member ids,
account suffixes and times, that is a correctness bug. The strict loader:

* resolves only ``true``/``false`` booleans and ``null``; every other scalar stays a string,
  and the typed models convert explicitly (``"10000"`` -> ``timeout_ms: int``),
* rejects duplicate mapping keys instead of silently keeping the last one.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, TypeVar

import yaml
from pydantic import BaseModel

from cua.schema.app_profile import AppProfile
from cua.schema.capability import Capability

M = TypeVar("M", bound=BaseModel)

_DROPPED_TAGS = {
    "tag:yaml.org,2002:int",
    "tag:yaml.org,2002:float",
    "tag:yaml.org,2002:bool",
    "tag:yaml.org,2002:timestamp",
}


class _StrictLoader(yaml.SafeLoader):
    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
        seen: set[Any] = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=deep)
            if key in seen:
                raise yaml.constructor.ConstructorError(
                    None, None, f"duplicate key {key!r}", key_node.start_mark
                )
            seen.add(key)
        return super().construct_mapping(node, deep=deep)


_StrictLoader.yaml_implicit_resolvers = {
    first: [(tag, rx) for tag, rx in resolvers if tag not in _DROPPED_TAGS]
    for first, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
_StrictLoader.add_implicit_resolver(
    "tag:yaml.org,2002:bool", re.compile(r"^(?:true|false)$"), list("tf")
)


class ArtifactLoadError(ValueError):
    pass


def load_yaml(text: str) -> Any:
    try:
        return yaml.load(text, Loader=_StrictLoader)  # noqa: S506 - SafeLoader subclass
    except yaml.YAMLError as exc:
        raise ArtifactLoadError(str(exc)) from exc


def dump_yaml(model: BaseModel) -> str:
    data = model.model_dump(mode="json", exclude_none=True, exclude_defaults=False)
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=100)


def _load(path: Path, model: type[M]) -> M:
    data = load_yaml(Path(path).read_text(encoding="utf-8"))
    return model.model_validate(data)


def load_capability(path: Path | str) -> Capability:
    return _load(Path(path), Capability)


def load_app_profile(path: Path | str) -> AppProfile:
    return _load(Path(path), AppProfile)


def save_capability(capability: Capability, path: Path | str) -> Path:
    """Write a capability, stamping its content hash into provenance."""
    stamped = capability.model_copy(
        update={
            "provenance": capability.provenance.model_copy(
                update={"content_hash": capability.content_hash()}
            )
        }
    )
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(dump_yaml(stamped), encoding="utf-8")
    return out
