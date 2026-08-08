# Changelog

Version history for the `cync-lan` core protocol library
(this package's `pyproject.toml` `version` field). Independent of the
`cync-lan-mqtt` Docker/MQTT add-on's own version scheme and the Home
Assistant `cync_lan` custom_component's own version scheme - all three are
versioned and released separately. See the root `README.md`/`RELEASING.md`
on `feature/ha-custom-component` for how the three artifacts relate.

### 0.14.1

`TcpTransport` omits `sub_id` rather than passing it as `None` when there is
no sub-device. `set_power(1, None)` and `set_power(1)` reach the same wire
but are not the same call to anything mocking the device, and three of the
integration's own tests failed on exactly that difference when it was first
put behind the facade. A facade that silently rewrites its consumers' call
signatures makes work for them for no behavioural gain.

Found by porting the Home Assistant light platform onto it, which is the
only way this kind of thing shows up.

### 0.14.0

**`transport.py`: one command surface, whichever wire it goes out on.**
Both transports already lived here and already had the same four commands.
What they lacked was the same shape - a device object on one side, a session
plus an explicit target on the other; `int` against `bool`; `set_temperature`
against `set_colour_temp`; and `is_sol_lamp` a caller's problem on BLE while
TCP works it out itself.

`protocol("tcp", device=...)` and `protocol("ble", session=..., target=...,
dev_type=...)` both return something satisfying `CyncTransport`, so a caller
can be handed the name of a transport without knowing which class implements
it.

**Units are the contract**, stated once because getting them wrong is silent:
brightness and colour temperature are both 0-100, as the wire speaks them.
`classify.kelvin_to_cync` is there for callers who have kelvin. Writing that
down is what found the bug in 0.13.0 - one caller had been sending kelvin
into a function that refuses anything above 100, for as long as it had
existed.

Deliberately small. Anything one transport can do and the other cannot stays
on the concrete class, where it is absent, rather than here, where it would
be a method that raises on half its implementations. That is also what makes
this the natural landing site for the BLE command work: a command arriving
on both sides can move up, and one that only works on the mesh does not have
to pretend.

### 0.13.0

**Kelvin conversion, because one consumer had it and the other did not.**
Cync speaks 0-100 on the wire whatever a bulb's real range is, and
`CyncDevice.set_temperature` refuses anything above 100 outright. The MQTT
add-on has always converted, with its own `kelvin2cync`/`cync2kelvin`. The
Home Assistant integration never did - it passed Home Assistant's kelvin
straight through, so every colour-temperature change was rejected with
"Invalid temperature! must be 0-100" and no packet was sent. Fixed in
cync_lan 2.13.0; `kelvin_to_cync` and `cync_to_kelvin` live here so there is
one implementation for both.

`DEFAULT_MIN_KELVIN`/`DEFAULT_MAX_KELVIN` (2000/7000) come with them, the
same defaults `const.py` has carried since the beginning for the same
reason.

Found while unifying the two transports' command surfaces, which is what a
unit contract is for: writing down that colour temperature is 0-100 on the
wire immediately showed that one of the two callers disagreed.

### 0.12.0

**`classify.py`: one answer to "what is this device", for both integrations.**
The Home Assistant integrations cannot import each other, so `cync_ble`
carried a copy of `CyncDevice`'s classification logic - its docstring said so
- and the copies drifted where nothing could see it.

Checked afterwards against all 157 known types: `is_light` agreed on every
one, so the carve-out that keeps dimmable switches on the light platform had
been copied faithfully. `is_dimmable` disagreed on 13. `CyncDevice` required
the type be classified LIGHT; `cync_ble` went by the capability alone.

The capability reading wins, because the narrow one had quietly made its own
callers impossible. `is_dimmable and not is_light` appears four times in the
integration to mean "a dimmer switch" - and since every dimmable switch has
`is_light` True by that same carve-out, while `is_dimmable` demanded LIGHT,
**no device type could satisfy it**. Two entity classes behind that condition
had never been created for anybody. `is_dimmer_switch` is what those callers
meant, and it matches 11 real types.

`CyncDevice`'s properties keep their per-instance setters and now defer to
`classify` for the computation, so there is one implementation rather than
three. Also here: `light_features()`, a transport-free descriptor of what a
light can do, and `to_ha_brightness`/`from_ha_brightness` for the 0-100 to
0-255 arithmetic both integrations were doing inline.

**Deliberately no Home Assistant import**, enforced by a test that parses the
module rather than grepping it. The CLI and the MQTT add-on depend on this
package too, and importing `homeassistant` here would put HA's release
cadence in front of all three to save each integration about five lines.
Each side maps `LightFeatures` to its own vocabulary - which is also what
lets `cync_ble` advertise less than the hardware claims while colour temp and
RGB are unconfirmed on that transport.

**Upgrading:** `CyncDevice.is_dimmable` is now True for dimmable switches
(and for types 96 and 112, which are SENSOR-classified with a stray
`dimmable=True` in the capability data). If you were using
`is_dimmable and not is_light` as a dimmer-switch test, it never worked;
use `is_dimmer_switch`.

### 0.11.1

**A cloud that died mid-session left the device unacknowledged.** Suspected
by reading while fixing 0.10.4, then confirmed over a real socket before
anything was changed - the test failed first, which is the only reason this
entry is written as fact.

`enable_passthrough()` promises that being unable to phone home is not a
reason to stop controlling lights. That held for a cloud already down when
the session was accepted, and only for that. Once a relay was up, nothing
took it down: `_cloud_proxy_task` breaks out of its loop on EOF without
touching `mitm_mode`, and every ack in `_dispatch_device_request` is gated on
`not self.mitm_mode`. Those gates are right while relaying - the cloud
answers the device's handshake and a second ack from us would be a duplicate
- and wrong the instant there is no cloud to answer. The session went on
deferring to something that had gone.

Milder than the outages either side of it, and harder to attribute for that.
Commands still went out, because `passthrough` had already separated
"relay" from "stay silent" in 0.10.2; what stopped was every acknowledgement.
A device that is controllable but never acknowledged is the kind of
half-working that gets blamed on a weak mesh.

A passthrough session now returns to ordinary local operation when its relay
ends on its own, from either direction it can end: the proxy task reaching
EOF, and a failed restart on the reconnect or idle-watcher path. A deliberate
teardown cancels the task instead, and cancellation re-raises before it can
reach the fallback, so shutting a session down cannot trigger this.

Two things it deliberately does not do. A per-device capture session is left
alone - there `mitm_mode` is set by `start_mitm()` with `passthrough` False,
silence is the entire point, and answering in the cloud's place would put our
own packets in the log the switch was turned on to collect. And the fallback
clears `mitm_mode` rather than remembering the session was once relaying,
which is what lets it recover: `start_tasks()` re-consults the option for
every session it adds and only reaches `enable_passthrough()` when the flag
is clear. Remembering would have swapped a permanent no-ack state for a
permanent no-relay one.

All three properties have their own end-to-end test against a real socket and
a fake cloud, and the first two are mutation-verified in opposite directions:
drop the fallback and the mid-session test fails; drop the capture guard and
the capture test fails.

### 0.11.0

**`CyncCloudAPI` is no longer a singleton, and settings can be passed in
rather than read from the environment at import.** Both existed to serve one
process with one account - the add-on - and both break as soon as something
else shares the interpreter.

Home Assistant is that something else. `cync_lan` and `cync_ble` can be
installed side by side, and on a real install with both, three things went
wrong silently:

- `CyncCloudAPI()` returned the *other* integration's live client, token
  cache and all. `cync_ble` picked up cync-lan's expired token, tried to
  refresh it, and reported the failure to its user as "could not reach the
  Cync cloud API" - a fault in neither the cloud nor the caller.
- `CYNC_CONFIG_DIR` had no effect once the first integration had imported
  `const`, so writes landed in one integration's directory while reads
  looked in the other's.
- `CYNC_ACCOUNT_USERNAME`/`_PASSWORD` had no effect either, so credentials
  typed into the second wizard were ignored and the first account's used.
  The worst of the three, because a login that succeeds as the wrong user
  looks like it worked.

`cync_ble` worked around all of it by writing its own 243-line cloud client
purely to avoid this module. That client can now go.

Two changes. `CyncCloudAPI` is an ordinary class: constructing one gets you
one. Process-wide sharing still exists and is still wanted - the firmware
sensor reads what the server's periodic check wrote - but it is now
`CyncCloudAPI.shared()`, asked for by name at the call site. And a new
`cync_lan.config.CyncConfig` carries the settings that must differ per
consumer, with `from_env()` reading the environment *when called* instead of
whenever `const` was first imported.

**`const.py` is unchanged.** Every module-level name is still there, still
means the same thing, and is still what the add-on and the TCP server use.
Nothing about the single-consumer path moves.

**Upgrading:** `CyncCloudAPI()` now returns a new instance. If you relied on
it returning the shared one, call `CyncCloudAPI.shared()`. The add-on
constructs it once and holds the reference, so it is unaffected; both call
sites in the Home Assistant integration are updated in 2.11.0.

Worth recording that the tests had been paying for this quietly: they were
littered with `CyncCloudAPI._instance = None` resets, and the OTP tests
routed every stub through monkeypatch because a plain assignment on a
singleton outlived its test. Those workarounds are gone.

### 0.10.4

**Cloud passthrough went mute again, on reconnect.** Found on a live install
rather than by a test, which is the point of the entry below it.

0.10.2 split `mitm_mode` into two flags so passthrough could relay *and* keep
controlling devices, and pinned the result with a truth table: `observe_only`
is `mitm_mode and not passthrough`. The table was right. What no test asked
was how a session gets into a combination, and one of the routes was
`stop_proxy()`, which cleared `passthrough` and left `mitm_mode` set - the
per-device capture-switch row. A passthrough session that stopped its proxy
came back indistinguishable from a capture and wrote nothing of its own again.

Nothing has to go wrong to get there. Both the reconnect path and the
idle-cloud-connection watcher call `stop_proxy()` and then `start_proxy()`
directly, never `enable_passthrough()`, so the flag was cleared and never
restored. One cloud reconnect muted a session until Home Assistant restarted.

Measured on a live 46-session install before the fix: 21 sessions silently
mute, 85 dropped writes in 17 hours. It read as flaky hardware rather than a
bug because commands survive it - `send_command` broadcasts over
`CYNC_CMD_BROADCASTS` (2) randomly sampled bridges, so a command is lost only
when both draws are mute. At 21 of 46 that is about one command in five.

`passthrough` is not connection state like the streams and byte counters
`stop_proxy()` resets; it is the reason the session is relaying, and it now
lives exactly as long as `mitm_mode` does, cleared beside it in `stop_mitm()`.
The new tests cover the lifecycle rather than the truth table: stopping a
proxy must not turn a passthrough session into a capture, and stopping capture
must still leave an ordinary session behind.

### 0.10.3

**An unwritable capture-log directory dropped every device.** Found by the
integration's option matrix, and a genuine fault rather than a test artifact.

`CYNC_MITM_LOG_DIR` is frozen at import from `CYNC_CONFIG_DIR`, which defaults
to `/root/cync-lan/config` - not writable in a Home Assistant container, on a
read-only root, or for any consumer that sets its config directory after
`cync_lan.const` has already been imported. `_setup_mitm_logger()` called
`mkdir` on it unguarded, so with cloud passthrough on:

```
FileNotFoundError: '/root/cync-lan/config/mitm_logs'
OSError: [Errno 30] Read-only file system: '/root'
ERROR cync_lan:server.py Error creating new Cync Wi-Fi device
```

The `OSError` escaped `enable_passthrough()`, escaped `start_tasks()`, and was
swallowed by `_register_new_connection`'s broad handler - which meant the
session was never registered at all. Not a missing log: **every device
connection refused, for as long as the option was on**. `enable_passthrough`'s
own docstring promised the opposite ("the cloud being unreachable is not a
reason to stop controlling lights"); it guarded `start_proxy()` failing and
never considered the logger.

A capture log is a diagnostic convenience and can never be why a device stops
working. The directory creation, the chmod and the handler open are each
guarded now, each degrading to a warning, and `enable_passthrough` will not let
anything from logging setup out.

Verified by mutation: revert both guards and the new test fails; revert only
the inner one and it still passes, because the outer guard catches it. Worth
knowing the test pins the behaviour rather than either particular guard.

### 0.10.2

**Fixes a bug in 0.9.0 that made cloud passthrough disable every device.**
Reported from a live install where nothing could be switched on while the
option was enabled, with this in the log once per command per session:

```
set_power: MITM mode active for this device: 192.168.86.27 (ID: unidentified) not writing data >>>
```

`mitm_mode` meant two things at once - "relay to the cloud" and "send nothing
of our own". That is right for the per-device MITM switch it was written for:
the cloud drives the device there, and anything injected pollutes the capture.
Cloud passthrough set the same flag and inherited the silence, so commands were
built, logged and dropped. The 0.9.0 notes claimed it "goes on parsing the same
traffic and controlling devices locally" - the parsing half was true and the
controlling half was not.

The two meanings are now separate. `observe_only` (`mitm_mode and not
passthrough`) gates our own outbound traffic - commands, mesh-info requests,
the control-callback sweep. Acks stay gated on `mitm_mode` in both modes,
because the cloud answers the device's handshake and a second ack from us is a
duplicate.

Three tests, since the version that shipped passed the whole suite while being
unusable: the truth table, the broadcast gate with a relayed session, and a
socket-level check that a passthrough session is still writable.

### 0.10.1

**OTP codes are strings now, and the account check happens before the password
is touched.** Both come out of @baudneo's [#1](https://github.com/Proxy-alt/cync-lan-lib/pull/1),
merged here - the password truncation it fixes plus the second bug he flagged
in the description and one the truncation exposed.

- `send_otp` took `otp_code: int` and coerced with `int(otp_code)`, which
  destroys a leading zero: `int("012345")` is `12345`, six digits become five,
  and the vendor rejects it with nothing to explain why. `"000000"` was worse -
  it coerced to `0`, tripped the falsy check, and was reported back as "OTP code
  must be provided" for a code the user had entered correctly. The code is now
  carried as a string end to end. Integers are still accepted and re-padded,
  though one that already lost its leading zero cannot be recovered.
- `CYNC_ACCOUNT_PASSWORD` defaults to `None`, and the new `[:16]` subscripts it,
  so an unconfigured account raised `TypeError: 'NoneType' object is not
  subscriptable` where it used to send `None` and get a normal API error.
  `request_otp` has guarded this since it was written; `send_otp` never did and
  only started needing to.


**New: `cync_lan.testing`** - a virtual Cync device and a fake Cync cloud, both
real sockets. `VirtualCyncDevice` connects to an `nCyncServer` and plays the
device side of the handshake; `FakeCloud` stands in for the vendor and records
what a relayed session forwards to it.

Shipped rather than kept in this package's own tests, because the Home
Assistant integration needs the same thing to test its own layer, and the
protocol lives here. A copy in the other repository would drift from the parser
it exists to exercise, which is the one failure a protocol simulator cannot
afford. Import costs nothing at runtime - no test framework is involved, and
`cryptography` is already required by the server for its own certificate.


**Tests that scale past hand-written cases.** No source changes - two additions
to the suite, both aimed at the same problem: `devices.py` is 2138 statements
at 60% coverage, and closing that by writing one check per path is not a thing
anyone finishes.

`test_framing.py` cuts a two-packet stream at **every** offset rather than at a
chosen one. The e2e suite splits at 2 bytes because that is the case the
comment in `parse_raw_data` describes, but TCP does not split where you ask it
to and the offset that bites in production is the one nobody thought of. The
input space is enumerable, so this is exhaustive rather than sampled: 74 split
points, plus byte-at-a-time delivery through the header, leading junk, and a
header promising more than arrived.

`test_structural.py` reads the package's own AST and checks the two bug
*classes* that actually recurred, everywhere, including code not yet written:

- `await`ing a just-cancelled task under `except Exception` - the 0.9.2
  `stop_proxy()` bug, which was written twice in one function.
- dereferencing `self.node` on a path that runs before the device has
  identified itself - which bit three times, in `_setup_mitm_logger`,
  `existing_init` and `start_proxy`, each fixed alone without asking where else
  it was true.

The second walks the call graph from six accept-time roots and reaches 28
methods, so adding a helper to that path brings it under the check
automatically rather than when someone remembers. Both were verified by
mutation - reintroduce either bug and the matching test fails, naming file,
line and method - because a structural test that passes trivially is worse than
none.

Neither moves the coverage number. Structural tests execute no product code at
all, which is the point: they cover a shape rather than a line.

### 0.9.2

**End-to-end tests: a real server, a real socket, a device on the other end.**
`tests/simulator.py` plays the device half of a connection over TLS and
`FakeCloud` stands in for the vendor, so `nCyncServer` can be exercised
through asyncio's own transport instead of around it. Everything else in the
suite either hands bytes straight to a parser or mocks the transport away -
`test_server.py` patches `asyncio.start_server`, `test_devices.py` patches
`open_connection` - which left the TLS handshake, packet framing across TCP
read boundaries, the session lifecycle and the cloud relay with no coverage at
all.

It found two bugs on its first run, both in code shipped in 0.9.0:

- **`stop_proxy()` abandoned its own teardown.** Both awaits caught
  `Exception`, which has not included `asyncio.CancelledError` since Python
  3.8 - and awaiting a task you just cancelled is exactly how you get one. So
  the first await raised straight back out: the cloud writer was never closed,
  the connection watcher kept running, and the caller (`stop_mitm`, and
  `close()` through it) saw its own shutdown cancelled. Invisible to the
  existing tests because a mocked `open_connection` leaves the proxy task with
  nothing to be cancelled out of.
- **`start_proxy()` built its task name from `self.node.id`.** The third place
  this bit, after `_setup_mitm_logger` and `existing_init` in 0.9.0. Being only
  a task *name* made it worse, not better: the `AttributeError` surfaced as
  "failed to start MITM" and the session silently fell back to local-only.

The five tests assert the handshake over TLS, reassembly of a packet split
inside its own header (the case `parse_raw_data`'s comment describes from a
real capture, until now untested because TCP will not split where you ask it
to), and the three claims cloud passthrough is built on: the cloud sees the
handshake from its first byte, cync-lan stops answering for itself while
relaying, and an unreachable cloud degrades to local-only rather than failing
the connection.

**What it is not.** The simulator is built from this repository's own
understanding of the protocol, so it can only confirm we are consistent with
ourselves. It is a regression net around what hardware already confirmed, and
it is not evidence about colour temperature, RGB, the hub family, or any of
the unconfirmed experimental commands - feed it our assumptions and it will
agree with our bugs. `docs/hardware_verification.md` stays the document that
decides what is real.

### 0.9.1

**Type annotations on the public entry points**, so consumers type-checking
their own code stop being penalised for calling into this one. `mypy` reports
`no-untyped-call` at every call site of an unannotated function, which put six
errors in the Home Assistant integration that were entirely about this
package's missing `-> None`s: `CyncCloudAPI.__init__`, `nCyncServer.start` /
`stop`, and `CyncTCPSession.start_mitm` / `stop_mitm` / `start_proxy` /
`stop_proxy` / `is_proxy_good`.

No behaviour change - annotations only.

**`docs/` re-synced from the integration repository.** The two copies are
compared by CI on both sides and had drifted 376 lines apart, all of it work
that landed in `Proxy-alt/cync-lan` and never came back here: four new
documents, plus `hardware_verification.md` recording `identify` as the second
command confirmed against real hardware. The check names this repository
canonical, so the drift was pointing the wrong way.

One content note: the checkmarks in `mesh_opcodes.md` are gone. The
integration repository bans them (`U+2713` falls inside the dingbats range its
`test_no_emojis` covers), which made the two checks unsatisfiable at once -
matching the canonical copy required text that the other test rejected.

### 0.9.0

**New: cloud passthrough (`CYNC_CLOUD_PASSTHROUGH`).** Every accepted session
is relayed to the real Cync cloud from its first byte, while cync-lan goes on
parsing the same traffic and controlling devices locally. Devices stay
cloud-connected, so the vendor's app, schedules and firmware delivery keep
working through a server that is also doing its own thing with everything it
sees.

**This is the existing MITM machinery made into a setting, not a second
implementation.** The relay, the per-connection rotating logs and the local
parse-while-relaying behaviour have all been in `CyncTCPSession` for a while,
reachable only through a per-device switch entity that is disabled by default.
What was missing was a way to say "do this for everything".

One thing genuinely differs, and it is why `enable_passthrough()` exists rather
than reusing `start_mitm()`. That method has to hang up on the device after
enabling: it is flicked on mid-session, the device is already several packets
into a handshake with *us*, and the cloud would receive a conversation starting
from the middle. The option is consulted in `start_tasks()` while a freshly
accepted session is being wired up — after construction, before the receive
task exists, so before a single byte has been read. The handshake reaches the
cloud intact and a forced reconnect would only produce a loop.

Failure is not fatal. A cloud that cannot be reached logs and leaves the
session in ordinary local-only mode; being unable to phone home is not a reason
to stop controlling lights.

**Be clear about what it does:** turning this on sends your device traffic —
and, if the app connects through this server too, your app traffic — to the
vendor. That is the point of it, and it is the opposite of what cync-lan is
normally for. Off by default.

Two smaller things came with it, both previously unreachable rather than
newly broken:

- `_setup_mitm_logger()` dereferenced `self.node` in four places. Its only
  caller was reachable from a per-node entity, so a node always existed;
  passthrough calls it before the device has identified itself, where
  `identifier` was never assigned at all. Now falls back to the address.
- `existing_init()` called `g.mqtt_client.add_mitm_button()` without checking
  for a client. Reached only after someone pressed a switch before; now every
  reconnect goes through it, including on the CLI, where there is no client.

`CYNC_CLOUD_PORT` joins `CYNC_CLOUD_IP` — the port was hardcoded at the one
call site. Both are re-read where they are used, so a bad port degrades to the
default instead of raising on every accepted connection.

### 0.8.0

**New: firmware capture.** Set `CYNC_FIRMWARE_CAPTURE_DIR` and the server
periodically asks the cloud whether a release exists for each model on the
account, downloads any image it is offered, and writes it there as a `.bin`
with a sidecar `.json` recording the URL, versions, size and MD5.

**It never installs anything.** Nothing in the capture path touches an OTA
opcode, opens a device session, or takes a device argument — `capture_firmware`
accepts an upgrade task and a destination directory and that is the whole
surface. A test asserts that structurally, on the signature and the function
body, rather than trusting that today's implementation happens not to; a
mutation adding a send call fails it.

Endpoint and payload are the app's own: `Cloud$Firmware.a()` builds
`/upgrade/firmware/check/{id}/geapp?useHttps=true`, and
`FirmwareUpgradeTaskResponse` declares the reply — including
`target_version_url`, `target_version_md5` and `target_version_size`, i.e. the
vendor publishes a direct download link. `scripts/cync_ota_fetch.py` already
reached the point of confirming that URL resolves with a HEAD request; this
completes it by fetching and verifying the image.

Off unless the directory is set — it must not add background traffic to the
vendor's API for people who never asked for it. Default interval six hours,
`CYNC_FIRMWARE_CHECK_INTERVAL`. One query per distinct model rather than per
device, since a release is published against a model and asking per bulb would
multiply identical requests by the size of the account.

An image that fails MD5 or size verification is **kept**, with the mismatch
recorded. A truncated or re-signed image is itself a finding and deleting it
would destroy the evidence.

### 0.7.0

**Fixed: the `0xDC` status slot was being parsed wrongly, and on `main` it was
inverted.** The field meanings are now taken from the shipping app's own parser
(`MeshStatusNotification$TelinkParser` → `product/MeshState.java`) rather than
inferred from captures:

```
slot[0]  address    a slot holds a device only when this is > 0
slot[1]  online     the flag behind MeshStateWithOnlineInfo
slot[2]  bits 0-6 brightness (coerced 0..100), bit 7 full-colour
slot[3]  run mode / packed RGB; 0xFF and 0x7F are sentinels
```

`slot[1]` is not, and never was, a presence byte. acync skips a slot whose
second byte is zero; a previous revision here inverted that on the strength of
one capture and skipped every slot whose second byte was *non*-zero. On a
46-node mesh that keeps 6 slots and drops 38 — every reachable device. This
never reached a release, but it was on `main` and would have shipped into
`cync-ble`.

Two independent confirmations, without the decompile. Device 21 decodes to
`[21, 183, 100, 0]` while the TCP transport — a wholly separate protocol —
reports `pow=1 bri=100` for it at the same moment; the inverted rule skips it.
And the "unexplained record shape" that made up 25 of 34 captured slots is
simply reachable devices that are switched off: devices 22 and 26 decode to
brightness 0, with TCP concurrently reporting `pow=0 bri=0` for both.

Alongside that:

- **`DeviceStatus.online` is new.** An unreachable device still reports the
  last level the mesh knew, which is real information but not a fresh reading.
- **`colour_temp` is no longer reported as 255.** `0xFF` is a sentinel the app
  routes away from its colour decode, and a CCT device cannot be at 255, so
  surfacing it invented a reading.
- Power derived as `brightness != 0` is now **confirmed against the vendor
  implementation**, not only against observed behaviour.

**New: `BleMeshSession.set_wifi_credentials()`** — the Wi-Fi credential handoff
(`SetWifiCommand`), the last unbuilt piece of BLE provisioning and the one part
of this protocol with no prior art anywhere. The payload
(`[chunks][len(ssid)][ssid][len(pass)][pass][0x01][type]`) is cut into 8-byte
pieces, each sent as its own fully-encrypted mesh command — no cleartext
credentials go on air. The chunk index occupies packet byte 2, which every
ordinary command leaves zero; that is load-bearing rather than cosmetic,
because byte 2 feeds both the authentication nonce and the keystream IV.
`build_command`/`BleMeshSession.send` grew a keyword-only `chunk_index` for it.

**Not verified against hardware** — that needs a factory-fresh unit. This
transport has no acknowledgement, so a silent success here proves nothing.

**Fixed: the mesh address is 16-bit, and only the low byte was being read.**
`MeshAddress` is `base_address | (element_id << 8)`, little-endian (confirmed
against `DataBytes.m15001d`). Reading one byte is correct for every device seen
so far because they all carry element 0 — by luck, not construction. A
multi-gang device using the high byte would have had every gang collapse onto
the parent id silently. Per-element addressing is still not implemented (it
needs hardware that uses it); what changed is that the case now logs the full
address and asks for the device model instead of being invisible.

Also recorded, in `set_light_effect`'s docstring: the second Reveal code path
(`SetComboCommand`, `0xF0`, with a two-byte `[0xFF, 0xF0]` sentinel) is
deliberately not wired in. It refuses `RevealColor` for hub-relayed devices, so
it is the strictly less capable of two routes to an identical result.

### 0.6.0

**New: a BLE mesh transport, confirmed on real hardware.** `cync_lan.ble_mesh`
controls already-provisioned devices over Bluetooth instead of over the TCP
relay. It is the sibling of `ble_provision`, which gets a factory-default device
*onto* a mesh — same Telink protocol, same opcode table, different framing.

This matters beyond adding a second way to send a command. **The BLE path needs
neither DNS redirection nor the hub command family.** Those are the two things
that constrain the TCP path: DNS redirection is the setup requirement behind most
support traffic, and hub commands currently get no reply at all (see
`docs/hub_envelope_ab_test.md`). Neither is on the critical path here.

What was verified against a wired Cync switch, not reasoned about:

- the session handshake, with `verify_pairing_response` reporting mutual auth
  **verified** — the device proved it derived the same key material;
- `set_power` (`0xD0`) **changed the switch's state**, with cync-lan reporting
  that change over its own TCP connection. Command out over one transport,
  confirmation back over an independent one, so the result cannot be a false
  positive;
- brightness, in both the `0xF0` and `0xD2` forms;
- **mesh relay** — a command addressed to one device and sent over a connection
  to a *different* device is relayed and acted on. One session therefore reaches
  the whole mesh, which is what makes this usable at forty-odd nodes rather than
  needing a link per device;
- the mesh credentials come from the cloud export this library already writes:
  the home's `mac` is the Telink mesh name and its `access_key` is the mesh
  password. `mesh_credentials_from_home()` pulls both out. Nothing about BLE
  needs the hub, despite what the `query_mesh_credentials` button implies.

Not confirmed, and marked as such in the code: colour temperature and RGB. They
ride the same `0xF0` family whose brightness member works, so they are better
founded than a guess, but nobody has moved either over this transport. Status
notifications are worse than unconfirmed — at least one firmware declares
`notify` on its characteristic, rejects the subscription with GATT `Unlikely
Error`, and drops the connection. `subscribe()` reports that rather than raising,
and sending never depends on it.

`ble_mesh` deliberately never imports `bleak`. It takes a `GattClient`
(a `typing.Protocol`) rather than constructing one, so a Home Assistant
integration can hand in a connection from HA's own Bluetooth stack — which is
what makes ESPHome Bluetooth proxies work. Building a client internally would
have limited the transport to devices in radio range of the host and quietly
ruled proxies out.

Two smaller things:

- **The `0x11, 0x02` prefix on every documented payload is identified**: it is
  the Telink vendor ID `0x0211`, little-endian. The TCP transport embeds it at
  the head of the payload; BLE gives it a field of its own. That is why the
  opcode table is shared between the two rather than duplicated, and it is now
  confirmed on hardware for two separate opcode families.
- **Device type 54 added**, as `supported=False` and marked plausible rather
  than confirmed. It was a gap in an otherwise dense run, and
  juanboro/cync2mqtt lists it beside 37 and 49 in the built-in-occupancy switch
  family. That is agreement between two reverse-engineering efforts, not
  hardware truth — and this project's own capture-confirmed third member of that
  family is type 56, which keeps its supported status.

### 0.5.3

**Fixed: the version this library reports was wrong, and had been for a
while.** `__version__` was a hand-maintained string that nothing in the release
process touched, so it still read `0.1.2` at release 0.5.2 - a version that was
never published at all.

That number is not decorative. `CYNC_VERSION` is built from it and reaches
users in three places:

- the startup line (`CyncLAN (version: ...) stack initializing`), which is
  exactly what the bug report template asks people to paste;
- the output of `cync-lan -V`;
- the `sw_version` shown on every device page in Home Assistant.

All three have been reporting a fiction, which quietly undermines any bug
report that depends on knowing what someone is actually running. **If you have
reported an issue before, the version you gave was almost certainly wrong** -
not your fault.

`__version__` now comes from the installed package metadata, so
`pyproject.toml` is the only place a version is declared and this cannot drift
again. A source tree that was never installed reports `0.0.0+unknown` rather
than inventing a plausible-looking number.

Also stops tracking `build/lib/`, fifteen files of stale setuptools output that
had diverged from `src/` - it still carried the old hand-written version. No
effect on the published package.

### 0.5.2

Docstring only - no behaviour change.

`set_indicator_led`'s docstring opened with "EXPERIMENTAL:" while its own body
said "CONFIRMED WORKING on real hardware with the corrected op/payload" and
explained that it is deliberately not flagged by `_warn_experimental_cmd_code`,
because both its op_code and cmd_code are confirmed. The first word contradicted
the rest and was the one people read.

### 0.5.1

Packaging metadata only - no code change.

Adds the `keywords` and the `Topic ::` / `Framework ::` / `Development Status`
classifiers that PyPI weights in its own search ranking. `cync-lan` previously
declared none of them, so it was effectively unfindable on PyPI except by
exact name.

### 0.5.0

**New: an opt-in alternate envelope for the hub command family**, selected with
the `CYNC_HUB_ENVELOPE` environment variable. Default is `routed`, which is
byte-for-byte what has shipped since 0.3.0; `bare` is the alternative.

This exists to settle a question, not because anything is known to be broken.
Hub commands - scenes, schedules, automations, groups, and the hub queries -
currently go out as `header(8) + routing(7) + op(1) + payload`, with the
7-byte block that addresses a mesh device. A pass over the decompiled phone
app found that all fifteen of its hub command classes bypass the method that
prepends that block entirely, which makes structural sense: a hub command is
not addressed to a mesh device, so there is nothing to route to.

That is suggestive, not decisive. The app's path is phone-to-device, while
this library sits on the device-to-cloud side, so the evidence does not
transfer automatically. Setting `CYNC_HUB_ENVELOPE=bare` sends
`header(8) + op(1) + payload` with the length field 7 shorter, so the two
shapes can be compared against real hardware instead of argued about. The
exact bytes each produces, for all six shipped hub commands, are tabulated in
`docs/hub_envelope_ab_test.md`.

Unlike every other `CYNC_*` setting, this one is re-read on each command
rather than frozen when `cync_lan.const` is imported. Flipping between the two
arms has to be cheap, or the second arm never gets run.

Internally, the length field and the routing flag now come from a single
helper instead of eleven hardcoded `8 + len(payload)` expressions. They have
to move together - the length counts the bytes after the header, so dropping
routing drops exactly 7 from it - and a packet whose declared length disagrees
with its body is considerably harder to diagnose than either envelope simply
being the wrong choice.

No behaviour changes unless you set the variable.

### 0.1.2

- Fix `protocols.py`'s `MqttSink.pub_online` being declared as a plain
  `def` instead of `async def` - every real implementation (the MQTT
  add-on's `MQTTClient.pub_online`, the HA integration's
  `CyncLanBridge.pub_online`) is async, since `devices.py` wraps this call
  in `asyncio.create_task()`, which requires a coroutine. Caught by running
  the HA integration's own strict-mypy pass against this package - the
  mismatch was invisible at runtime (Python doesn't enforce `Protocol`
  conformance dynamically) but broke static type-checking for anything
  assigning a real implementation to `GlobalObject.mqtt_client`.

### 0.1.1

- No functional change - verifies the CI publish workflow's PyPI Trusted
  Publishing step end-to-end now that the `cync-lan` project exists on
  PyPI (0.1.0 was published manually after the pending publisher wasn't
  yet recognized on the first automated attempt).

### 0.4.0

**New: `CyncDevice.identify()`** - makes a device announce itself physically,
so you can tell which bulb or switch an entity actually is. Payload is
`{0xF7,0x11,0x02,0x03}` plus 1 to start or 2 to stop, on the same dispatch
path as `set_indicator_led` - the one command in this family confirmed
working on real hardware, which makes this the most likely of the new
commands to work.

**New: dimmer level-bar LEDs.** `set_dimmer_led_mode()` (0xF7/0x62) and
`set_dimmer_led_brightness()` (0xF7/0x63). These are the row of level LEDs on
a dimmer switch, not the small status LED `set_indicator_led` controls.

`DimmingLedsIndicatorMode` has exactly two values, `BRIEFLY_DISPLAY` and
`ALWAYS_ON` - there is no "off", so the bar cannot be disabled, only made
momentary. Brightness deliberately sends **two** packets, Preview then Save,
because only Preview carries a level; a lone Save would commit whatever the
device happened to be previewing.

**New: `set_time()`** - sets the clock a hub runs its native Schedules from,
the counterpart to 0.3.0's `query_device_time()`. A drifted hub runs its
Schedules at the wrong moment and nothing on the Home Assistant side can
compensate.

Its DST byte reproduces a quirk rather than improving on it: the app decides
by testing whether the timezone id starts with `"America"`, and writes
minutes=0/flag=1 in that case. That is a string prefix test, not a DST
calculation, so `us_style_dst` is exposed to override it.

All three carry the usual caveat for this family: the `cmd_code` is predicted
from the length formula rather than confirmed against a capture.

### 0.3.0

**New: five more hub commands**, all with op_codes and request payloads read
from the decompiled app rather than guessed:

| Function | op_code | What it does |
|---|---|---|
| `query_hub_info()` | `0x4B` | Firmware version, MAC and setup code |
| `query_device_time()` | `0x46` | The clock the hub believes it is running on |
| `query_sol_config()` | `0xAD` | A Sol lamp's clock/timer/mic-light display flags |
| `delete_automation()` | `0x97` | Removes a Schedule's trigger binding |
| `delete_group()` | `0x32` | Deletes a device group from the mesh |

`delete_automation` closes a real gap: `create_schedule`, `toggle_automation`
and `delete_schedule` all existed, but nothing removed the binding
`add_automation` creates.

`query_device_time` is worth calling out - native Cync Schedules fire off the
hub's own clock, not Home Assistant's, so a hub whose time has drifted runs
its automations at the wrong moment and nothing else exposed that.

As with the rest of this family, each `cmd_code` is PREDICTED from the length
formula and the reply channel is unconfirmed, so a query returning `None` on
timeout is an expected outcome rather than an error.

**Deliberately not implemented**, and documented in `docs/mesh_opcodes.md`
instead:

- `0x49` `QueryHubFirmwareUpdates` - its reply is a variable-length list of
  per-device records rather than a fixed layout, and a wrong record stride
  produces plausible-looking garbage rather than an obvious failure. There is
  no capture to validate a decoder against.
- `0x4F` `StartHubFirmwareUpdates`, `StartWifiOtaUpdate` and
  `SetWifiOtaUpdateMode` - these flash firmware. Everywhere else in this
  family a wrong predicted `cmd_code` means the device ignores the packet;
  here the same mistake has a far worse floor, so no code path capable of
  sending them exists.

### 0.2.1

**Fixed: this package could not be installed alongside Home Assistant.**
`pyyaml` was pinned to exactly `==6.0.2`, while Home Assistant requires
`PyYAML==6.0.3`. The two are mutually exclusive, so `pip install cync-lan
homeassistant` failed outright with a resolution error - which also meant the
Home Assistant integration that depends on this package could not have its
requirements installed. Relaxed to `>=6.0.2`.

Nothing in this package needs an exact PyYAML version; it only calls
`safe_load`/`dump`.

### 0.2.0

**Minimum Python is now 3.12.** The package previously declared `>=3.9`, but
that was never true: `structs.py` imports `enum.StrEnum` (3.11+) and
`devices.py` uses a PEP 701 nested-quote f-string (3.12+). Installing on
3.9-3.11 resolved and then failed on the first import. The declared floor now
matches what the code actually needs, and CI runs against it.

**Fixed: six hub commands sent a malformed length field.** `cmd_code` is the
byte length of everything after the packet header, and `create_scene`,
`create_schedule`, `delete_scene`, `delete_schedule`, `toggle_automation` and
`add_automation` all computed it one byte short. A short length field makes
device firmware read a truncated body, which presents as the command silently
doing nothing. If you tried these and nothing happened, this is why. They are
still EXPERIMENTAL - the fix makes the framing correct, it does not confirm
the commands work on real hardware.

`scripts/cmd_code.py` computes the field for a new command and audits every
existing one against it; the audit runs in CI. It is what found this.

**Fixed: three crashes on error and shutdown paths.**

- A network failure during token refresh or OTP submission raised
  `NameError: name 'lp' is not defined` instead of reporting a clean auth
  failure. `aiohttp`'s connection and timeout errors are not
  `ClientResponseError`, so they hit the generic handler, which referenced a
  variable that was never defined there.
- Stopping MITM mode raised `NameError: name 'name' is not defined` from the
  cancellation handler, so the proxy task never shut down cleanly.
- Closing a connection with mismatched state raised `NameError` from a
  malformed f-string.

**Fixed: MITM mode spun a CPU core after the cloud disconnected.** The proxy
loop treated an empty read as "nothing to do" and looped. A stream returns
empty forever once the peer closes, so this ran flat out for as long as MITM
stayed enabled. It now stops on EOF.

**Fixed: the packet parser could freeze for 3 seconds per device.** Checking a
device's retained MITM state opened a blocking broker connection from inside
the inbound packet parser, stalling all other devices' traffic for the
duration. Moved off the event loop.

**Added: `query_hub_mesh_credentials()`** - reads the BTLE mesh name and
password from a connected hub (op_code `0x8A`). These are the two values
`ble_provision`'s key derivation needs, so this is what allows provisioning a
new device onto an *existing* mesh rather than only a factory-default one.
EXPERIMENTAL: the response channel is unconfirmed and may time out.

**Removed** `parse_packet_OLD`, 769 lines of superseded dead code.

**Removed** `nCyncServer.loop`. It was assigned in `__init__` and never read
anywhere, and `asyncio.get_event_loop()` raises when no loop is current - so
constructing the server outside a running loop crashed. Anything needing the
loop should call `get_running_loop()` at the point of use.

Housekeeping: ruff now runs in CI (it was configured but had never been run -
444 violations, including the three undefined names above). Tests run on every
push and pull request, not only on a version bump. `server.py` went from no
test coverage to 80%; the suite is 123 -> 157 tests.

### 0.1.0

- First published release. Extracted from what was previously vendored
  directly into the Home Assistant custom_component
  (`custom_components/cync_lan/vendor/cync_lan/`) and duplicated in the
  `cync-lan-mqtt` add-on's own source tree - both now depend on this
  package from PyPI instead. Contains the device/session TCP state machine
  (`devices.py`, `server.py`), the packet codec (`packet/`), Cync cloud
  auth (`cloud_api.py`), BLE GATT provisioning (`ble_provision.py`), and
  shared config/constants (`const.py`, `structs.py`, `utils.py`,
  `metadata/`). Ships a `py.typed` marker. `GlobalObject.mqtt_client`/
  `.export_server` are now typed against `Protocol` classes in the new
  `protocols.py` instead of importing the add-on's concrete types directly
  (this package doesn't depend on either consumer package).
