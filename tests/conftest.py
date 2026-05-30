from __future__ import annotations

import pytest

import errsight
from errsight import hub


@pytest.fixture(autouse=True)
def _errsight_test_isolation():
    """Each test gets a clean scope stack + an explicit transport close on teardown.

    Without this, a test that calls `errsight.set_user(...)` outside a
    with_scope mutates the root scope for every subsequent test in the
    same pytest process.
    """
    hub.reset()
    yield
    errsight.close()
    hub.reset()
