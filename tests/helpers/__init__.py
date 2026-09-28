"""Test-support helpers for the areaDef E2E suite.

Importable as ``helpers.*`` because ``tests/conftest.py`` puts the ``tests/``
directory on ``sys.path``; ``scripts/gen_fixtures.py`` does the same so the
fixture builders have exactly one home.

Keep this file import-free: ``tests/test_areadef.py`` imports ``helpers.images``
and must stay runnable under plain ``python -m unittest`` with no non-stdlib
test dependencies, so anything imported here (pytest above all) would break that
contract for the whole legacy suite.
"""
