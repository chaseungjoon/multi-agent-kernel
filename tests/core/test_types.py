"""Smoke tests for shared core value objects."""

from mak.core import LockMode, ResourceKind, ResourceRef


def test_resource_ref_defaults_to_file_without_symbol() -> None:
    """Resource references can represent file-level locks."""
    resource = ResourceRef(kind=ResourceKind.FILE, path="mak/core/types.py")

    assert resource.kind is ResourceKind.FILE
    assert resource.path == "mak/core/types.py"
    assert resource.symbol is None


def test_lock_mode_values_match_wire_contract() -> None:
    """Lock mode enum values stay stable for serialized contracts."""
    assert LockMode.READ.value == "read"
    assert LockMode.WRITE.value == "write"
