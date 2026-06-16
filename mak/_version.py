"""Single source of truth for the MAK version.

``__version__`` is the canonical `PEP 440 <https://peps.python.org/pep-0440/>`_
string used for packaging — ``pyproject.toml`` reads it from here via
``[tool.setuptools.dynamic]``, and ``mak/__init__.py`` re-exports it. Bump it here
and both the installed metadata and ``mak.__version__`` follow.

``__version_display__`` is the human-friendly label (e.g. for the README badge);
keep it in step with ``__version__``.
"""

from __future__ import annotations

__version__ = "0.2.0b0"
__version_display__ = "0.2.0 Beta"
