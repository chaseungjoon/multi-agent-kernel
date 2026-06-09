"""Put this project's root on sys.path so ``import toolkit`` resolves under pytest.

pytest imports this conftest before collecting; because this directory has no
``__init__.py`` that import inserts the project root into ``sys.path``.
"""
