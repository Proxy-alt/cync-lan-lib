"""Re-export of the shipped simulator, so this suite's imports stay put.

The implementation moved to `cync_lan.testing` when the integration
repository needed it too - see that module's docstring for why it ships
rather than living here.
"""

from __future__ import annotations

from cync_lan.testing import (  # noqa: F401
    HEADER_LEN,
    FakeCloud,
    VirtualCyncDevice,
    build_23_auth,
    build_packet,
    packet_length,
    write_self_signed,
)
