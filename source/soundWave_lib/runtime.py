"""Shared symbol binder for soundWave's split implementation modules."""

from __future__ import annotations

_exports = {}
_namespaces = []


def bind(namespace: dict) -> None:
    """Attach a module namespace to the shared runtime symbols."""
    if namespace not in _namespaces:
        _namespaces.append(namespace)
    namespace.update(_exports)


def publish(namespace: dict) -> None:
    """Publish current hub symbols to attached implementation modules."""
    _exports.update({k: v for k, v in namespace.items() if not k.startswith("__")})
    for target in list(_namespaces):
        target.update(_exports)
