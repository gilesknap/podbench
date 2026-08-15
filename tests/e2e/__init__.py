"""The opt-in cluster suite: S1-S5 turned from one-off spikes into regressions.

A package rather than a bare directory so that these module names cannot
collide with the unit tests next door (``tests/`` has no ``__init__.py``, so
pytest imports its modules by basename), and so the whole tree is one import
root that :mod:`tests.e2e.conftest` can gate as a unit.

Nothing here runs by default. See ``README.md`` for the opt-in and the
prerequisites.
"""

from __future__ import annotations
