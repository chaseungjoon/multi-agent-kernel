"""Conflict detector subsystem: shallow cross-node validation of agent edits."""

from mak.conflict_detector.detector import (
    Conflict,
    ConflictDetector,
    ConflictReport,
    EditRound,
)
from mak.conflict_detector.import_check import (
    ImportRecord,
    check_import_conflicts,
    extract_imports,
)
from mak.conflict_detector.name_collision_check import (
    SymbolDef,
    check_name_collisions,
    extract_defined_symbols,
)
from mak.conflict_detector.signature_check import (
    CallSite,
    Signature,
    check_call,
    check_signature_compatibility,
    extract_calls,
    extract_signatures,
)

__all__ = [
    "CallSite",
    "Conflict",
    "ConflictDetector",
    "ConflictReport",
    "EditRound",
    "ImportRecord",
    "Signature",
    "SymbolDef",
    "check_call",
    "check_import_conflicts",
    "check_name_collisions",
    "check_signature_compatibility",
    "extract_calls",
    "extract_defined_symbols",
    "extract_imports",
    "extract_signatures",
]
