"""End-to-end: a real nCyncServer, a real socket, a device on the other end.

Everything here goes through TLS and asyncio's own transport. Nothing is
patched out except the environment. See tests/simulator.py for what a
simulator can and cannot prove.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

import pytest
from simulator import (
    FakeCloud,
    VirtualCyncDevice,
    build_23_auth,
    build_packet,
    write_self_signed,
)

from cync_lan.server import nCyncServer
from cync_lan.structs import GlobalObject


@pytest.fixture(autouse=True)
def reset_singletons():
    """nCyncServer and GlobalObject are both process-wide singletons, so a
    test that does not reset them inherits whatever the last file left
    behind - the reason test_server.py carries the same fixture."""
    nCyncServer._instance = None
    g = GlobalObject()
    previous_server, previous_mqtt = g.ncync_server, g.mqtt_client
    g.mqtt_client = None
    yield
    g.ncync_server, g.mqtt_client = previous_server, previous_mqtt
    nCyncServer._instance = None


@pytest.fixture
def certs(tmp_path: Path) -> tuple[str, str]:
    return write_self_signed(tmp_path)


async def _serve(certs: tuple[str, str]) -> tuple[nCyncServer, int, asyncio.Task]:
    """Bind a real server on an ephemeral port and return it running.

    Port 0 rather than 23779: the suite must not fight a real cync-lan on the
    developer's own machine, and CI runners reuse ports across jobs.
    """
    cert_file, key_file = certs
    server = nCyncServer({})
    server.cert_file, server.key_file = cert_file, key_file
    server.host, server.port = "127.0.0.1", 0
    GlobalObject().ncync_server = server

    server.ssl_context = await server.create_ssl_context()
    server._server = await asyncio.start_server(
        server._register_new_connection,
        host=server.host,
        port=0,
        ssl=server.ssl_context,
    )
    server.running = True
    port = server._server.sockets[0].getsockname()[1]
    task = asyncio.create_task(server._server.serve_forever())
    return server, port, task


async def _shutdown(server: nCyncServer, task: asyncio.Task) -> None:
    sessions = list(server.tcp_connections.values())
    for session in sessions:
        # stop_proxy() before close(): close() does not tear the cloud
        # connection down, and a relay left open keeps the fake cloud's
        # handler in read() forever.
        with contextlib.suppress(Exception):
            await session.stop_proxy()
        with contextlib.suppress(Exception):
            await session.close()
    server._server.close()
    with contextlib.suppress(Exception):
        await server._server.wait_closed()
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task
    # Let the loop run the cancellation to completion. Without this the task
    # is cancelled-but-not-yet-finished when the test returns, which reads as
    # a leak to anything counting live tasks.
    await asyncio.sleep(0)
    assert task.done(), "serve_forever outlived the test"
    # Any per-session task add_tcp_device() started outlives the session
    # object otherwise - the receive loop in particular.
    for session in sessions:
        tasks = getattr(session, "tasks", None)
        for name in (
            "receive",
            "callback_cleanup",
            "dev_conn_watcher",
            "proxy_task",
            "proxy_conn_watcher",
        ):
            pending = getattr(tasks, name, None) if tasks else None
            if pending is not None and not pending.done():
                pending.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await pending


async def test_handshake_over_a_real_socket(real_sockets, certs, monkeypatch):
    """The path nothing else covers: TLS up, 0x23 in, 0x28 back, and the
    server volunteering its 0xA3 control request half a second later."""
    monkeypatch.setenv("CYNC_CLOUD_PASSTHROUGH", "0")
    server, port, task = await _serve(certs)
    try:
        async with VirtualCyncDevice("127.0.0.1", port) as device:
            await device.send(build_23_auth())

            ack = await device.read_packet()
            assert ack[0] == 0x28, f"expected the 0x23 ack, got {ack.hex(' ')}"

            # send_a3() is what makes a device controllable, and it is sent
            # unprompted after a 0.5s sleep rather than in reply to anything.
            control = await device.read_until(0xA3)
            assert control is not None, "server never sent its 0xA3 control request"

            assert "127.0.0.1" in server.tcp_connections
            session = server.tcp_connections["127.0.0.1"]
            assert session.queue_id == b"\x39\x87\xc8\x57"
            assert session.ready_to_control is True
    finally:
        await _shutdown(server, task)


async def test_packet_split_inside_its_header_is_reassembled(
    real_sockets, certs, monkeypatch
):
    """Split a packet at 2 bytes - before the length fields at offsets 3-4.

    parse_raw_data has a branch for exactly this, added after a real capture
    where a 2-byte fragment was treated as a complete packet and every
    following packet in the next read was misaligned. Until now that branch
    was guarded by a comment; TCP does not split where you ask it to, so the
    only way to test it is to write the two halves separately.
    """
    monkeypatch.setenv("CYNC_CLOUD_PASSTHROUGH", "0")
    server, port, task = await _serve(certs)
    try:
        async with VirtualCyncDevice("127.0.0.1", port) as device:
            await device.send_split(build_23_auth(), at=2)

            ack = await device.read_packet()
            assert ack[0] == 0x28
            session = server.tcp_connections["127.0.0.1"]
            assert session.queue_id == b"\x39\x87\xc8\x57", (
                "the queue id was read from a misaligned buffer"
            )
    finally:
        await _shutdown(server, task)


async def test_passthrough_relays_the_handshake_from_its_first_byte(
    real_sockets, certs, monkeypatch, tmp_path
):
    """The claim enable_passthrough() is built around.

    start_mitm() has to hang up on the device because it is switched on
    mid-session and the cloud would meet the conversation halfway through.
    The option is applied in start_tasks(), before the receive task exists,
    so the cloud should see the 0x23 itself - not whatever followed it.
    """
    monkeypatch.setenv("CYNC_MITM_LOG_DIR", str(tmp_path / "mitm"))
    monkeypatch.setattr(
        "cync_lan.devices.CYNC_MITM_LOG_DIR", str(tmp_path / "mitm"), raising=False
    )
    async with FakeCloud(*certs) as cloud:
        monkeypatch.setenv("CYNC_CLOUD_PASSTHROUGH", "1")
        monkeypatch.setenv("CYNC_CLOUD_IP", "127.0.0.1")
        monkeypatch.setenv("CYNC_CLOUD_PORT", str(cloud.port))
        server, port, task = await _serve(certs)
        try:
            async with VirtualCyncDevice("127.0.0.1", port) as device:
                auth = build_23_auth()
                await device.send(auth)
                assert await cloud.wait_for_bytes(len(auth)), (
                    "the cloud received nothing"
                )
                assert bytes(cloud.received).startswith(auth), (
                    "the cloud did not see the handshake as its first bytes: "
                    f"{bytes(cloud.received[:16]).hex(' ')}"
                )
                assert cloud.connections == 1

                session = server.tcp_connections["127.0.0.1"]
                assert session.mitm_mode is True
                # Relaying must not stop the local parse - the whole point is
                # doing both.
                assert session.queue_id == b"\x39\x87\xc8\x57"
        finally:
            await _shutdown(server, task)


async def test_passthrough_still_reaches_the_device_with_commands(
    real_sockets, certs, monkeypatch, tmp_path
):
    """Over a real socket, end to end: relaying must not silence us.

    The unit test above pins the gate; this pins that a relayed session is
    still a session we can write to. Both exist because the version that
    shipped passed every test in the suite while making the option unusable.
    """
    monkeypatch.setattr(
        "cync_lan.devices.CYNC_MITM_LOG_DIR", str(tmp_path / "mitm"), raising=False
    )
    async with FakeCloud(*certs) as cloud:
        monkeypatch.setenv("CYNC_CLOUD_PASSTHROUGH", "1")
        monkeypatch.setenv("CYNC_CLOUD_IP", "127.0.0.1")
        monkeypatch.setenv("CYNC_CLOUD_PORT", str(cloud.port))
        server, port, task = await _serve(certs)
        try:
            async with VirtualCyncDevice("127.0.0.1", port) as device:
                await device.send(build_23_auth())
                await cloud.wait_for_bytes(1)
                session = server.tcp_connections["127.0.0.1"]

                assert session.mitm_mode is True
                assert session.passthrough is True
                assert session.observe_only is False, (
                    "a relayed session must still be writable"
                )

                # The write path itself, not just the flag.
                await session.write(b"\x73\x00\x00\x00\x01\x00")
                assert await device.read_packet(timeout=3.0) is not None
        finally:
            await _shutdown(server, task)


async def test_passthrough_leaves_the_acks_to_the_cloud(
    real_sockets, certs, monkeypatch, tmp_path
):
    """Every ack in _dispatch_device_request is gated on `not self.mitm_mode`.

    Answering as well as relaying would put two servers in one conversation,
    and the device would see each reply twice.
    """
    monkeypatch.setattr(
        "cync_lan.devices.CYNC_MITM_LOG_DIR", str(tmp_path / "mitm"), raising=False
    )
    async with FakeCloud(*certs) as cloud:
        monkeypatch.setenv("CYNC_CLOUD_PASSTHROUGH", "1")
        monkeypatch.setenv("CYNC_CLOUD_IP", "127.0.0.1")
        monkeypatch.setenv("CYNC_CLOUD_PORT", str(cloud.port))
        server, port, task = await _serve(certs)
        try:
            async with VirtualCyncDevice("127.0.0.1", port) as device:
                await device.send(build_23_auth())
                await cloud.wait_for_bytes(1)
                # The fake cloud never answers, so anything arriving here came
                # from cync-lan itself.
                assert await device.read_until(0x28, timeout=2.0) is None
        finally:
            await _shutdown(server, task)


async def test_unreachable_cloud_leaves_the_session_local_only(
    real_sockets, certs, monkeypatch, tmp_path
):
    """Being unable to phone home is not a reason to stop controlling lights.

    Points passthrough at a port with nothing behind it and asserts the
    session degrades to ordinary local operation - acks and all - rather than
    failing the connection.
    """
    monkeypatch.setattr(
        "cync_lan.devices.CYNC_MITM_LOG_DIR", str(tmp_path / "mitm"), raising=False
    )
    # Bind and immediately release a port to get one nothing is listening on.
    probe = await asyncio.start_server(lambda r, w: None, host="127.0.0.1", port=0)
    dead_port = probe.sockets[0].getsockname()[1]
    probe.close()
    await probe.wait_closed()

    monkeypatch.setenv("CYNC_CLOUD_PASSTHROUGH", "1")
    monkeypatch.setenv("CYNC_CLOUD_IP", "127.0.0.1")
    monkeypatch.setenv("CYNC_CLOUD_PORT", str(dead_port))
    server, port, task = await _serve(certs)
    try:
        async with VirtualCyncDevice("127.0.0.1", port) as device:
            await device.send(build_23_auth())

            ack = await device.read_packet()
            assert ack[0] == 0x28, "the session did not fall back to local acks"

            session = server.tcp_connections["127.0.0.1"]
            assert session.mitm_mode is False
            assert session.cloud_writer is None
            assert session.queue_id == b"\x39\x87\xc8\x57"
    finally:
        await _shutdown(server, task)


async def test_a_cloud_that_dies_mid_session_hands_the_acks_back(
    real_sockets, certs, monkeypatch, tmp_path
):
    """The sibling of test_unreachable_cloud_leaves_the_session_local_only,
    for a cloud that was reachable when the session was accepted and stopped
    being reachable afterwards.

    Only the first case was covered, and the promise in enable_passthrough's
    docstring - that being unable to phone home is not a reason to stop
    controlling lights - held for it alone. Once the relay was up, nothing
    ever took it down: _cloud_proxy_task breaks out of its loop on EOF
    without touching mitm_mode, so every ack gate in _dispatch_device_request
    stayed shut with nobody left to answer. The device would have gone on
    talking to a server that had stopped replying to anything.
    """
    monkeypatch.setattr(
        "cync_lan.devices.CYNC_MITM_LOG_DIR", str(tmp_path / "mitm"), raising=False
    )
    monkeypatch.setenv("CYNC_CLOUD_PASSTHROUGH", "1")
    monkeypatch.setenv("CYNC_CLOUD_IP", "127.0.0.1")

    cloud = FakeCloud(*certs)
    await cloud.__aenter__()
    monkeypatch.setenv("CYNC_CLOUD_PORT", str(cloud.port))
    server, port, task = await _serve(certs)
    try:
        async with VirtualCyncDevice("127.0.0.1", port) as device:
            await device.send(build_23_auth())
            assert await cloud.wait_for_bytes(1), "the relay never came up"

            session = server.tcp_connections["127.0.0.1"]
            assert session.mitm_mode is True
            assert session.passthrough is True

            # The cloud goes away mid-session: peers closed, listener shut.
            await cloud.__aexit__()
            await asyncio.sleep(0.5)  # let the proxy task see EOF

            # 0xC3 is answered with 0xC8 by any session that is not relaying.
            await device.send(build_packet(0xC3))
            ack = await device.read_until(0xC8, timeout=3.0)

            assert ack is not None, (
                "the cloud died and the session kept deferring acks to it - "
                "nothing answers the device now"
            )
            assert session.mitm_mode is False, (
                "the session still believes it is relaying to a cloud that is gone"
            )
            assert session.passthrough is False
            assert session.observe_only is False
    finally:
        with contextlib.suppress(Exception):
            await cloud.__aexit__()
        await _shutdown(server, task)


async def test_passthrough_comes_back_when_the_cloud_does(
    real_sockets, certs, monkeypatch, tmp_path
):
    """Falling back must not be a one-way door.

    Clearing mitm_mode is what lets it recover: start_tasks() re-consults the
    option for every session it adds, and only reaches enable_passthrough()
    when mitm_mode is False. Had the fallback left the flag set to remember
    it was once relaying, the session would have been stuck local-only for
    good - trading a permanent no-ack state for a permanent no-relay one.
    """
    monkeypatch.setattr(
        "cync_lan.devices.CYNC_MITM_LOG_DIR", str(tmp_path / "mitm"), raising=False
    )
    monkeypatch.setenv("CYNC_CLOUD_PASSTHROUGH", "1")
    monkeypatch.setenv("CYNC_CLOUD_IP", "127.0.0.1")

    first_cloud = FakeCloud(*certs)
    await first_cloud.__aenter__()
    monkeypatch.setenv("CYNC_CLOUD_PORT", str(first_cloud.port))
    server, port, task = await _serve(certs)
    try:
        async with VirtualCyncDevice("127.0.0.1", port) as device:
            await device.send(build_23_auth())
            assert await first_cloud.wait_for_bytes(1)
            await first_cloud.__aexit__()
            await asyncio.sleep(0.5)
            assert server.tcp_connections["127.0.0.1"].mitm_mode is False

        # The cloud returns, and the device reconnects to it. A different
        # port only because these are ephemeral - _cloud_endpoint() reads the
        # environment per call, which is what makes this expressible at all.
        async with FakeCloud(*certs) as second_cloud:
            monkeypatch.setenv("CYNC_CLOUD_PORT", str(second_cloud.port))
            async with VirtualCyncDevice("127.0.0.1", port) as device:
                await device.send(build_23_auth())
                assert await second_cloud.wait_for_bytes(1), (
                    "the session never relayed again after falling back"
                )
                session = server.tcp_connections["127.0.0.1"]
                assert session.mitm_mode is True
                assert session.passthrough is True
    finally:
        with contextlib.suppress(Exception):
            await first_cloud.__aexit__()
        await _shutdown(server, task)


async def test_a_capture_session_is_left_alone_when_the_cloud_dies(
    real_sockets, certs, monkeypatch, tmp_path
):
    """The fallback above must not reach the per-device capture switch.

    Capture is an explicit choice to observe one device and stay off its
    wire, and a capture whose cloud has gone is still not an invitation for
    us to start answering in its place - that would put our packets in the
    log the user turned the switch on to collect. `passthrough` is what tells
    the two apart, which is the second thing that flag has now bought.
    """
    monkeypatch.setattr(
        "cync_lan.devices.CYNC_MITM_LOG_DIR", str(tmp_path / "mitm"), raising=False
    )
    # Passthrough off: this session's relay comes from start_mitm(), not the
    # option, so it must keep mitm_mode when the cloud disappears.
    monkeypatch.delenv("CYNC_CLOUD_PASSTHROUGH", raising=False)
    monkeypatch.setenv("CYNC_CLOUD_IP", "127.0.0.1")

    cloud = FakeCloud(*certs)
    await cloud.__aenter__()
    monkeypatch.setenv("CYNC_CLOUD_PORT", str(cloud.port))
    server, port, task = await _serve(certs)
    try:
        async with VirtualCyncDevice("127.0.0.1", port) as device:
            await device.send(build_23_auth())
            await asyncio.sleep(0.3)
            session = server.tcp_connections["127.0.0.1"]

            await session.start_proxy()
            session.mitm_mode = True  # what start_mitm() leaves behind
            assert session.passthrough is False
            assert session.observe_only is True

            await cloud.__aexit__()
            await asyncio.sleep(0.5)

            await session.start_proxy()  # the retry that now fails

            assert session.mitm_mode is True, (
                "a capture session was quietly turned back into an ordinary one"
            )
            assert session.observe_only is True
    finally:
        with contextlib.suppress(Exception):
            await cloud.__aexit__()
        await _shutdown(server, task)


async def test_an_unwritable_capture_log_does_not_cost_the_device(
    real_sockets, certs, monkeypatch, tmp_path
):
    """A diagnostic log must never be why a device stops working.

    CYNC_MITM_LOG_DIR is frozen at import from CYNC_CONFIG_DIR, whose default
    is /root/cync-lan/config - unwritable in a HA container, on a read-only
    root, or for any consumer that sets its config dir after cync_lan.const
    was imported. The mkdir raised, the OSError escaped enable_passthrough and
    start_tasks, and _register_new_connection swallowed it without registering
    the session. With passthrough on that dropped every device, not just the
    relay.
    """
    unwritable = tmp_path / "not-a-dir"
    unwritable.write_text("this is a file, so mkdir underneath it must fail")
    monkeypatch.setattr(
        "cync_lan.devices.CYNC_MITM_LOG_DIR", str(unwritable / "mitm"), raising=False
    )
    async with FakeCloud(*certs) as cloud:
        monkeypatch.setenv("CYNC_CLOUD_PASSTHROUGH", "1")
        monkeypatch.setenv("CYNC_CLOUD_IP", "127.0.0.1")
        monkeypatch.setenv("CYNC_CLOUD_PORT", str(cloud.port))
        server, port, task = await _serve(certs)
        try:
            async with VirtualCyncDevice("127.0.0.1", port) as device:
                await device.send(build_23_auth())
                await asyncio.sleep(0.3)

                assert "127.0.0.1" in server.tcp_connections, (
                    "an unwritable log directory dropped the whole session"
                )
                session = server.tcp_connections["127.0.0.1"]
                assert session.queue_id == b"\x39\x87\xc8\x57"
                # The relay itself is unaffected - only the log is missing.
                assert await cloud.wait_for_bytes(1)
        finally:
            await _shutdown(server, task)
