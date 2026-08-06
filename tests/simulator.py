"""A Cync device that exists only as a socket, for end-to-end tests.

Everything else in this suite hands bytes straight to a parser or mocks the
transport away - `test_server.py` patches `asyncio.start_server`,
`test_devices.py` patches `open_connection`. That leaves the parts that only
exist on a real connection untested: the TLS handshake, packet framing across
TCP read boundaries, the session lifecycle, and the cloud relay.

So this speaks the wire protocol over an actual socket to an actual
`nCyncServer`. Two pieces:

  VirtualCyncDevice   connects in, plays the device side of the handshake
  FakeCloud           listens, records what a relayed session forwards to it

**What this can and cannot tell you.** It is built from this repository's own
understanding of the protocol, so it can only confirm we are consistent with
ourselves. It is a regression net around behaviour already confirmed on
hardware - it is not evidence about anything unconfirmed, and feeding it our
assumptions will make it agree with our bugs. `docs/hardware_verification.md`
stays the document that decides what is real.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import ssl
from pathlib import Path
from typing import Optional

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

# Header is 5 bytes and is not counted toward the length it declares:
#   [0] type  [1][2] unknown  [3] length multiplier (*256)  [4] length
# See docs/packet_structure.md.
HEADER_LEN = 5


def build_packet(pkt_type: int, body: bytes = b"") -> bytes:
    """Frame a device->server packet the way the real devices do."""
    length = len(body)
    return bytes([pkt_type, 0x00, 0x00, length // 256, length % 256]) + body


def packet_length(header: bytes) -> int:
    """Total on-wire size of the packet this header starts."""
    return (header[3] * 256) + header[4] + HEADER_LEN


# A 0x23 body as captured, with the auth code zeroed. Bytes 1-4 of the body
# are the endpoint/queue id: parse_packet reads packet_header[5:9] and the
# 0x23 branch reads raw_data[6:10], so the queue id a test asserts on is
# whatever lands at those offsets - hence a recognisable value rather than a
# random one.
def build_23_auth(queue_id: bytes = b"\x39\x87\xc8\x57") -> bytes:
    if len(queue_id) != 4:
        raise ValueError("queue_id must be 4 bytes")
    body = b"\x03" + queue_id + b"\x00\x10" + b"1e07" + bytes(15)
    return build_packet(0x23, body)


class VirtualCyncDevice:
    """The device half of a connection, over TLS, as an async context manager.

    Deliberately not driven by `scripts/create_virtual_device.py`: that
    generates device *specifications* from the SDK registry - capabilities,
    product ids, firmware versions - and never opens a socket. The two are
    complementary, and a protocol test needs the socket rather than the
    catalogue.
    """

    def __init__(self, host: str, port: int, queue_id: bytes = b"\x39\x87\xc8\x57"):
        self.host = host
        self.port = port
        self.queue_id = queue_id
        self.reader: Optional[asyncio.StreamReader] = None
        self.writer: Optional[asyncio.StreamWriter] = None
        self.received: list[bytes] = []

    async def __aenter__(self) -> "VirtualCyncDevice":
        context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        self.reader, self.writer = await asyncio.open_connection(
            self.host, self.port, ssl=context
        )
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if self.writer is not None:
            self.writer.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.writer.wait_closed(), timeout=2)

    async def send(self, data: bytes) -> None:
        assert self.writer is not None
        self.writer.write(data)
        await self.writer.drain()

    async def send_split(self, data: bytes, at: int) -> None:
        """Write one packet as two TCP segments, splitting at `at` bytes.

        The interesting case is a split inside the 5-byte header, before the
        length fields at offsets 3-4 are readable. parse_raw_data has a
        specific branch for that, added after a real capture misaligned every
        following packet - this is how it gets exercised.
        """
        await self.send(data[:at])
        await asyncio.sleep(0.05)
        await self.send(data[at:])

    async def read_packet(self, timeout: float = 3.0) -> bytes:
        """Read exactly one framed packet, honouring the declared length."""
        assert self.reader is not None
        header = await asyncio.wait_for(self.reader.readexactly(HEADER_LEN), timeout)
        remaining = packet_length(header) - HEADER_LEN
        body = b""
        if remaining:
            body = await asyncio.wait_for(self.reader.readexactly(remaining), timeout)
        packet = header + body
        self.received.append(packet)
        return packet

    async def read_until(self, pkt_type: int, timeout: float = 5.0) -> Optional[bytes]:
        """Read packets until one of `pkt_type` arrives, or time out.

        Returns None on timeout rather than raising, because "the server
        never sent this" is an ordinary assertion in these tests - the
        passthrough cases turn on the server NOT answering.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            try:
                packet = await self.read_packet(
                    timeout=max(0.1, deadline - loop.time())
                )
            except (asyncio.TimeoutError, asyncio.IncompleteReadError, OSError):
                return None
            if packet[0] == pkt_type:
                return packet
        return None


class FakeCloud:
    """A TLS listener standing in for the vendor's cloud.

    Cloud passthrough is pointed here with CYNC_CLOUD_IP/CYNC_CLOUD_PORT,
    which is what those two being read at use time buys - the relay can be
    aimed somewhere harmless without touching the code under test.
    """

    def __init__(self, certfile: str, keyfile: str):
        self.certfile = certfile
        self.keyfile = keyfile
        self.received = bytearray()
        self.connections = 0
        self._server: Optional[asyncio.AbstractServer] = None
        self._peers: list[asyncio.StreamWriter] = []
        self.port = 0

    async def __aenter__(self) -> "FakeCloud":
        context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        context.load_cert_chain(self.certfile, self.keyfile)
        self._server = await asyncio.start_server(
            self._handle, host="127.0.0.1", port=0, ssl=context
        )
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *_exc: object) -> None:
        # Close the relayed connections ourselves before waiting. Since 3.12
        # Server.wait_closed() waits for every handler to finish, and the
        # handler below sits in read() until EOF - so a proxy connection the
        # server under test has not torn down yet would hang teardown
        # forever rather than failing the test.
        for peer in self._peers:
            with contextlib.suppress(Exception):
                peer.close()
        self._peers.clear()
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception, asyncio.TimeoutError):
                await asyncio.wait_for(self._server.wait_closed(), timeout=5)

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self.connections += 1
        self._peers.append(writer)
        try:
            while True:
                chunk = await reader.read(4096)
                if not chunk:
                    break
                self.received.extend(chunk)
        except Exception:  # noqa: BLE001 - a torn-down test connection is normal
            pass
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def wait_for_bytes(self, count: int = 1, timeout: float = 5.0) -> bool:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            if len(self.received) >= count:
                return True
            await asyncio.sleep(0.05)
        return False


def write_self_signed(directory: Path) -> tuple[str, str]:
    """A throwaway cert/key pair for both listeners.

    Same shape as nCyncServer._ensure_self_signed_cert generates, but written
    here so a test can hand the SAME pair to the fake cloud - the device side
    of every connection in these tests verifies nothing anyway.
    """
    cert_path = directory / "cert.pem"
    key_path = directory / "key.pem"
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "*.xlink.cn")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    key_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return str(cert_path), str(key_path)
