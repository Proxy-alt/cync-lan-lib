"""Fixtures shared across `cync-lan` core's test suite.

Unlike the Home Assistant integration's own test suite (a separate
project, `tests/components/cync_lan/` on `feature/ha-custom-component`),
this one has no Home Assistant dependency at all - these tests exercise
`cync_lan`'s protocol/device/cloud-auth code directly.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def real_sockets():
    """Let a test open real sockets, if something in the environment has
    decided it may not.

    Neither pytest-socket nor pytest-homeassistant-custom-component is a
    dependency of this package - `[dev]` is pytest and pytest-asyncio. But
    pytest auto-loads plugins from anything installed in the same
    interpreter, and a machine that also works on the Home Assistant
    integration has both. On such a machine the socket-based tests in
    test_e2e_session.py fail with HASocketBlockedError for reasons that have
    nothing to do with this package, while passing in CI, where those
    packages are absent.

    So: if the blocker is present, turn it off for the duration; if it is
    not, do nothing at all.
    """
    try:
        import pytest_socket
    except ImportError:
        yield
        return

    pytest_socket.enable_socket()
    yield
    # phcc's own cleanup asserts nothing was blocked during the test. Any
    # instance recorded here predates the enable above, and failing this
    # test for it would be reporting someone else's plugin's bookkeeping.
    blocked = getattr(pytest_socket, "SocketBlockedError", None)
    for candidate in (blocked, getattr(blocked, "__subclasses__", list)()):
        for cls in candidate if isinstance(candidate, list) else [candidate]:
            instances = getattr(cls, "instances", None)
            if isinstance(instances, list):
                instances.clear()
