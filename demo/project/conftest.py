"""Make the in-tree ``dataforge`` package importable under pytest.

pytest imports this conftest before collecting the tests, and because this
directory has no ``__init__.py`` that import puts ``demo/project`` on ``sys.path``
— so ``from dataforge import ...`` resolves without an install step. Run the suite
with ``python -m pytest demo/project`` from the repository root.
"""
