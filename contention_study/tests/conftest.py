"""Make the study package and the read-only kernel importable from the tests."""

from __future__ import annotations

import sys
from pathlib import Path

_STUDY_ROOT = Path(__file__).resolve().parent.parent
for candidate in (_STUDY_ROOT, _STUDY_ROOT.parent):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))
