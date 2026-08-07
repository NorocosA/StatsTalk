"""Compatibility helpers backed by the reviewed capability registry.

Comparison reports are release evidence only. Runtime files cannot modify the
production trust decision automatically.
"""

from snla.capabilities import get_capability, get_public_capabilities


def is_method_trusted(method: str) -> bool:
    """Return whether ``method`` is validated for Python-only execution."""

    capability = get_capability(method)
    return bool(capability and capability.python.supported and capability.python.validated)


def get_trusted_methods() -> set[str]:
    """Return canonical methods validated for Python-only execution."""

    return {
        capability.name
        for capability in get_public_capabilities()
        if capability.python.supported and capability.python.validated
    }


def trust_loaded_from() -> str:
    """Return the production source of truth for trust decisions."""

    return "capability_registry"


__all__ = ["get_trusted_methods", "is_method_trusted", "trust_loaded_from"]
