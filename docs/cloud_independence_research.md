# Cloud independence research: could Cync's servers be replaced entirely?

This documents research into a much larger question than the rest of `docs/`: not "how do we
control already-paired devices locally" (solved, see [mesh_opcodes.md](mesh_opcodes.md)), but
**could a self-hosted server ever fully replace Cync's cloud, including account management and
provisioning brand-new devices** — eliminating any dependency on `api.gelighting.com` /
`cm.gelighting.com`, not just at runtime for already-paired devices (already true today), but ever,
including during initial device setup.

Sourcing conventions match [mesh_opcodes.md](mesh_opcodes.md): **confirmed** (cited to an exact
decompiled-app class/line, or a real packet capture), **plausible** (a reasonable inference, not
directly proven), **not found / blocked** (explicitly flagged as absent, not guessed at).

Research credited to two background agents run 2026-07-16 against
`/Users/proxy-alt/Downloads/cync_decompiled/`, prompted by the user asking what it would take to
replace the Cync app (and its servers) with Home Assistant entirely, including registering brand
new devices.

## Two different projects, with very different feasibility

This question actually splits into two things that are easy to conflate:

1. **Redirect the official Cync app's own network traffic** to a self-hosted server (so the app
   itself no longer needs Cync's real servers).
2. **Eliminate the cloud dependency architecturally** - new devices get identity/credentials from a
   self-hosted server, never touching `gelighting.com`, ever, with or without the official app
   involved at all.

(1) is a narrower, more mechanical question (does the app validate TLS strictly enough to block a
DNS+cert redirect, the same trick this project already uses on device firmware). (2) is the real
prize but depends on protocol-level facts about how device identity gets assigned during BLE
pairing, independent of whether the app is ever involved.

## Finding 1: the app's device-control channel is exactly as spoofable as device firmware

The app talks to Cync's device-relay endpoint (`cm.gelighting.com:23779` - the same host/port this
project already DNS-redirects for device firmware, see [DNS.md](DNS.md)) via two paths, and **both
are effectively unauthenticated**:

- **Confirmed**: `io/xlink/wifi/sdk/XlinkTcpService.java:125-224` (`connect()`) - the *primary*
  connection attempt is a **plain unencrypted TCP socket** on port 23778. No TLS at all.
- **Confirmed**: the TLS fallback, `connectInSSL()` (line 227, `sSLContext.init` at line 252), uses
  `new TrustManager[]{new EmptyX509TrustManager()}` -
  `io/xlink/wifi/sdk/EmptyX509TrustManager.java` implements `X509TrustManager` with empty
  `checkServerTrusted`/`checkClientTrusted` bodies (accepts any certificate) and no
  `HostnameVerifier` override either.
- **Confirmed, not a dead/debug-only path**: `XlinkAgentManager.java:307-312` (`XlinkAgent.init`,
  `setCMServer("cm.gelighting.com", 23779)`, `setTcpType(4)`) is referenced from
  `XlinkDeviceManager.java`/`WifiHubProxyManager.java`/`WifiPeerProxyManager.java` - the app's core
  device-control/relay classes, not just first-run onboarding code.

**Practical implication**: redirecting the app's own device-control traffic to a self-hosted server
(the same DNS-override + self-signed-cert trick already used for device firmware) is plausible. The
existing MITM methodology in [debugging_setup.md](debugging_setup.md) already anticipates this - its
example `unbound` config includes a 4th override entry specifically for `App 1: android`, alongside
3 device overrides.

## Finding 2: the account/REST API channel is properly locked down (in release builds)

- **Confirmed, no pinning found**: `HttpClientModule_ProvideHttpClientFactory.java` builds the
  account-API Ktor client ("GeHttpClient") with only a logging plugin - no `CertificatePinner`, no
  custom `TrustManager`/`SSLContext`.
- **Confirmed, this is what actually blocks it**: `resources/res/xml/network_security_config.xml`
  (wired via `AndroidManifest.xml:86`) restricts the `base-config` to `certificates src="system"`
  only. `debug-overrides` (which would add `certificates src="user"`, trusting a manually-installed
  CA) **only applies to debug builds** - a normal installed release APK will not trust a
  self-signed/user CA on this channel without modifying the phone's *system* trust store, i.e. root.
- Two domains (`xlink.cn`, `ota.gelighting.com`) are cleartext-whitelisted, sidestepping TLS
  entirely for OTA downloads specifically - not relevant to account/auth traffic.
- **Not found / unresolved**: `com/thingclips/smart/android/network/http/pin/
  ThingCertificatePinner.java` implements real, refreshable SHA-256 pinning (a bundled Tuya/"Thing"
  SDK dependency, unrelated product line) - no confirmed call site showing `com.gelighting.cbygekit`
  actually invokes it. Looks vestigial, not confirmed dead.

**Practical implication**: spoofing login/account/device-list traffic to the *app itself* would need
root. This is the one piece of "replace the app's own cloud dependency" that's meaningfully harder
than everything else this project has done.

## Finding 3: BLE-provisioned device identity is entirely client-side, not cloud-assigned

This is the load-bearing question for project (2) above - does a *brand-new* device's identity get
assigned by Cync's cloud, or does the phone decide it locally? **Client-side, confirmed:**

- **Confirmed**: `com/gelighting/cbygekit/services/devices/model/DeviceId.java` -
  `DeviceId.Companion.b(macAddress)` returns `new DeviceId(0, mac)`; serializes as
  `"{MAC}.{index}"`. No network call. Called from
  `BaseNonHubDeviceCommissionService.k()` (`.../services/commission/
  BaseNonHubDeviceCommissionService.java:450`) using the MAC already read off the BLE-connected
  device - not from any API response.
- **Confirmed**: `MeshAddress` (`.../product/MeshAddress.java`), the per-device mesh unicast
  address, is likewise just a wrapped `short`/`int`, constructed locally by
  `BaseNonHubDeviceCommissionService.y()` (`setMeshAddressOperation`, step 6 of the pipeline below) -
  no cloud round trip. This corrects an earlier pass's "not found" note on this exact question -
  see [mesh_opcodes.md](mesh_opcodes.md)'s "Provisioning/commissioning" entry.
- **Confirmed**: `SetWifiCommand` (`.../services/devices/command/SetWifiCommand.java:57`), the BLE
  payload actually sent to the device, carries only `ssid`/`key`/`deviceType` - no account ID, auth
  token, or cloud-issued identifier.

**The 20-step BLE commissioning pipeline** (`BleDeviceCommissionService` constructor, `.../
services/commission/BleDeviceCommissionService.java:129`, step names recovered by cross-referencing
each `commissionWorkflow$N` lambda against its continuation class's `@DebugMetadata(m=...)` field,
which JADX preserves even though the outer methods were renamed to single letters during
obfuscation):

1. `setWifiCredentialsIfNeeded`
2. `removeExistingDevicesOperation`
3. `connectAndPairOperation`
4. *(unrecovered - `BleDeviceCommissionService.H`, see Open questions)*
5. `assignDevicesToMeshOperation`
6. `setMeshAddressOperation`
7. *(unrecovered - `.D`)*
8. `setTimeOnDevicesOperation`
9. *(unrecovered - `.T`)*
10. *(unrecovered - `.E`)*
11. `subscribeDevices`
12. **`writeChangesToCloudOperation`** ← the only cloud-write step, ~2/3 through the pipeline
13. `queryFirmwareVersion`
14. `queryMicrophoneSensitivity`
15. `finalizeDeviceCommissioningOperation`
16-20. multi-color/show-related steps, mostly local BLE command sends

The simpler 3-step **standalone-device** path (non-mesh devices, `BleDeviceCommissionService` field
`E`): `createGroupsOperation` → `saveStandaloneDeviceToCloudOperation` → `renameStandaloneDevice` -
same shape, local decisions first, one cloud write after.

In both, WiFi handoff / mesh-address assignment / `deviceID` construction all happen **before** the
single cloud-write step, and that step's own name ("write changes to cloud" / "save ... to cloud")
reads as sync-after-the-fact, not identity issuance.

**Transport**: BLE mesh provisioning uses a Telink Semiconductor BLE-mesh stack
(`com/gelighting/cbygekit/services/devices/telink/TelinkDeviceBleManager.java`, 2100 lines) -
consistent with the Telink-style framing already found on the TCP side in
[mesh_opcodes.md](mesh_opcodes.md).

## Update: both open questions resolved - BLE pairing crypto is local-only

A follow-up session re-ran JADX against the same APK with different flags specifically to recover
what static analysis couldn't reach the first time, then triaged the native `.so` libraries the
Java/Kotlin layer can't see into. Both of the open questions above are now resolved.

**Re-decompile methodology** (for reproducing or extending this): the original decompile lost 4 of
`BleDeviceCommissionService`'s 20 pipeline steps to JADX's aggressive default inlining, which
collapses Kotlin coroutine state-machine methods into their callers and can push already-complex
generated code past JADX's internal region-complexity guard (`JadxOverflowException`). Re-running
with `--no-inline-anonymous --no-inline-methods --no-inline-kotlin-lambda --deobf --cfg
--show-bad-code` against the same APK, repacked from its extracted form back into a zip container
(`zip -r -X -0 out.apk .` from the extracted APK directory, since JADX's resource decoder expects a
proper zip, not a loose directory) recovered real names for all 4 previously-unnamed steps by
keeping their lambda/continuation classes as separately-named files instead of inlining them away.
Note: disabling inlining is a **targeted trade**, not a strict improvement - it fixed the 4 target
methods but pushed ~850 *other*, unrelated methods elsewhere in the app (mostly Kotlin
stdlib/coroutines/Ktor internals, not anything in this project's area of interest) into the same
kind of decompilation failure. Worth doing for a specific investigation, not as a default.

**The 4 previously-unresolved pipeline steps (positions 4, 7, 9, 10) are: `createGroupsAndSubgroupsOperation`,
`assignToGroupsOperation`, `setLoadTypeOperation`, `checkMotionSensorOperation`** - all four confirmed
local-only (group/subgroup model building and motion-sensor capability checks against already-in-memory
`CommissionBuilder` state via local `GroupService`/`MotionSensorService` classes, no HTTP client
reference anywhere in their bodies). This closes out the entire 20-step pipeline: **exactly 1 of 20
steps (`writeChangesToCloudOperation`) touches the network at all.**

**BLE pairing/session crypto - confirmed local, not server-gated:**

- **`libBleLib.so` turned out to be a red herring.** Its JNI package is
  `com.thingclips.ble.jni.BLEJniLib` (Tuya/ThingClips branding, the same bundled-but-likely-unused
  SDK pattern already flagged for `ThingCertificatePinner.java`) - `greadelf -d libBleLib.so` shows
  no crypto library linkage at all, and its one `made_session_key` function (disassembled via
  `r2 -e bin.relocs.apply=true -A -c 'pdf @ sym.made_session_key' libBleLib.so`) is a 16-byte CRC8
  table-whitening loop over two already-local byte arrays - not AES/ECDH/HMAC, no capacity for a
  network round trip at all. This is very likely unused code from a different product line, not
  Cync's real Telink pairing engine.
- **The real logic is in Kotlin, not native**: `TelinkDeviceBleManager.getSessionKey()`
  (`.../telink/TelinkDeviceBleManager.java:912-932`, backing class
  `TelinkDeviceBleManager$getSessionKey$2.java`) just awaits a local `Flow` already populated by
  data arriving over the established BLE connection (`FlowKt.filterNotNull` on a manager-internal
  field) - no network call anywhere in it.
- **`MeshCredentials`** (the actual mesh-wide network name+password - the real shared secret, not
  the per-session key above) comes from exactly one of two local sources
  (`DeviceServiceDefault.java`'s `MeshDataProviderImpl.getMeshCredentials`, ~line 960-1010): if a
  mesh already exists at this location, it's read via a `HubMeshNameAndPasswordNotification` BLE
  characteristic **directly off an already-paired hub device on the same mesh** (peer-to-peer, not
  cloud); if this is a brand-new mesh, it's synthesized from a locally-held `LocationModel`'s own
  name/ID fields (`locationModel.f40108c.toUpperCase()` + `locationModel.f40109d`). Neither path
  makes an HTTP call.
- **Independent corroboration from a different angle**: `libxlinkdtsl.so` (the native library
  actually securing the separate `cm.gelighting.com` TCP relay channel, not BLE - confirmed via its
  exported JNI symbols `io.xlink.wifi.sdk.util.XlinkDTSLUtils.{initDTSL,encryptSendData,...}`) is
  Eclipse tinyDTLS using a **PSK** ciphersuite (`TLS_PSK_WITH_AES_128_CCM_8`), with strings
  (`secretPSK`, `default identity`, `Xlink_Identify`) suggesting a shared/default PSK identity
  rather than a real per-device server-issued secret - reinforcing, at the native/wire-protocol
  level, Finding 1's conclusion that this channel isn't meaningfully authenticated per-device.
- **`libnetwork-android.so` re-checked for native pinning** (the one thing Java-only analysis
  couldn't rule out) - `strings` found no pinning/TrustManager-related text, and its only JNI
  exports (`ThingNetworkApi.sendBroadcast`/`stopBroadcast`) are LAN-discovery utilities, not the
  account-API HTTPS path at all - so this doesn't so much confirm "no pinning" on that channel as
  show it isn't the right place to look; Finding 2's conclusion stands unchanged.

**Net conclusion**: no cloud round-trip or server-issued secret was found anywhere in device
identity assignment, mesh-address assignment, group placement, or the BLE pairing/session-key
crypto itself. A live BLE sniffer capture (the originally-suggested next step) would still be the
only way to get 100% certainty, but static analysis has now converged on the same answer from three
independent angles (app-level flow tracing, native library inspection, and the Kotlin session-key
logic itself) - the practical case for a hard cloud dependency in BLE pairing is now weak.

## Update: the actual BLE GATT wire protocol is now documented

See [ble_provisioning_protocol.md](ble_provisioning_protocol.md) - service/characteristic UUIDs,
the pairing/session-key handshake, command encryption (MIC + CTR-style keystream), and the
`SetWifiCommand` chunking format, all traced from the decompiled app and cross-validated against
independent open-source Telink-mesh prior art (`google/python-dimond`, `vpaeder/telinkpp`,
`google/python-laurel` - the last one specifically cited elsewhere as used for Cync/GE devices).
This is the missing byte-level layer under everything documented above.

## Suggested next step: MITM the app's own traffic during a real pairing session

Given Finding 1 (the app's `cm.gelighting.com` channel is unauthenticated, same as firmware), the
cheapest next real-world step is exactly what [debugging_setup.md](debugging_setup.md) already
anticipates but hasn't been done for a *pairing* session specifically: MITM the app's traffic while
pairing one brand-new device, using the existing `socat` + `unbound`-view methodology already
documented there (its example config already includes an `App 1: android` override entry).

**What this would and wouldn't show:**

- **Would show**: any traffic the app or the newly-provisioned device sends over the
  `cm.gelighting.com:23779` relay channel - e.g. once the device gets WiFi credentials over BLE and
  joins the network, its first connection to the relay; possibly the app performing its own
  post-pairing verification/sync over the same channel.
- **Would NOT show**: the actual BLE handoff itself (a different transport, needs a BLE sniffer, not
  a TCP MITM) or the `writeChangesToCloudOperation` REST call (Finding 2 - that's the
  properly-TLS-validated account API channel, invisible to this specific MITM technique without
  root).

**How to run it** (following [debugging_setup.md](debugging_setup.md)'s existing pattern exactly,
scoped to one device + the phone):

1. Set up `unbound` view-based DNS override for exactly 2 clients: the phone's IP and the new
   (still-unpaired, so not yet on your WiFi at all - it'll only appear once BLE handoff finishes)
   device's IP, each pointed at its own `socat` listener, per the existing example config.
2. Per the existing warning, decide deliberately whether to leave Bluetooth on or off during the
   test - it needs to be **on** for pairing to work at all (BLE is mandatory for WiFi handoff to an
   unprovisioned device), unlike the existing warning's normal-operation scenario (where turning
   Bluetooth off forces ordinary commands through the TCP relay instead of local BLE proximity).
3. Start `socat` on both listeners, then pair one new device through the app as normal.
4. Save both `dump.txt` files, renamed descriptively (e.g. `new_device_pairing_app.txt`,
   `new_device_pairing_device.txt`) per the existing "one session, clearly labeled" convention.

This is real-hardware-dependent work only the user can do (a spare/factory-reset device and the
phone app) - documenting the plan here so it's not lost, not attempting it from static analysis.

## Update: firmware OTA delivery - REST shape confirmed and live-tested, blocked on a TCP-level "subscription"

Prompted by asking whether a device could be made to report a stale firmware version and elicit
an OTA push from Cync's cloud (for capture/study - not for flashing anything, see
[mesh_opcodes.md](mesh_opcodes.md)'s note on why the firmware-*applying* opcodes stay
unimplemented). Two background-agent passes plus one live test against a real account, 2026-08-30.

### Finding 4: the account-write step is a whole-location PUT, not a per-device create

`writeChangesToCloudOperation` (the one cloud-touching step of the 20-step BLE commissioning
pipeline, Finding 3 above) resolves to **`PUT {base}/product/{productId}/device/{locationId}/property`**
(`Cloud.java:90-96`, base URL confirmed `https://api.gelighting.com/v2` production,
`Environment.java:19`). Auth via `Access-Token`/`Xlink-User-Id` headers
(`HeaderTypeKt.java:44-56`). Body is a full `LocationProperties` DTO - the *entire* location's
state (admin, groups, all devices' `bulbsArray` entries, scenes, schedules), not a single-device
record. Response (`LocationPropertiesResponse.java:29-32`) just echoes the same structure back -
confirms this doc's existing "sync-after-the-fact, not identity issuance" reading, it does not
hand back anything new. **Caveat**: the two non-hub placement paths
(`GroupService.mo14569w`/`SubgroupService.mo14568v`) were confirmed to funnel into the same
`OperationManager` transaction by class/method naming convention, not read byte-for-byte -
plausible, not confirmed.

### Finding 5: firmware-check is a separate, already-implemented REST endpoint

**Confirmed**: `POST {base}/upgrade/firmware/check/{deviceId}/geapp?useHttps=true`
(`XlinkApiClientDefault.java:49-90`, `Cloud.java:47-50` - **`api.gelighting.com`, not
`ota.gelighting.com`**). Request body (`FirmwareUpgradeTaskRequest`, confirmed JSON keys):
`type, identify, product_id, current_version` - all four pulled from a local, per-model
`DeviceType` catalog object, not from any prior cloud response or device-reported state. Response
(`FirmwareUpgradeTaskResponse`) includes `target_version_url`, `target_version_md5`,
`target_version_size` directly - a plain, unauthenticated `GET` to that URL returns the raw
firmware image (`FirmwareServiceDefault.fetchFirmwareImage`). `ota.gelighting.com` (the
cleartext-whitelisted host from Finding 2) never appears as a literal anywhere in `sources/` -
plausible only that `target_version_url` happens to resolve there for the binary transfer itself.

This exact endpoint is **already implemented, read-only, in this repo**:
`cloud_api.py`'s `check_firmware_update()` (asks the question, sends no device traffic) and
`capture_firmware()` (downloads/checksums to disk, has no path to a device or opcode - cannot
flash anything by construction). `server.py`'s `_capture_available_firmware()` was meant to run
this periodically per distinct device model. **Bug found in passing**: it reads
`device.product_id`/`device.metadata.ota_type` via `getattr(..., None)`, but nothing in
`devices.py`/`structs.py`/`classify.py` ever populates either field on a real `CyncDevice` - so
this periodic task silently no-ops for every device, every time. Confirmed by grepping the whole
`src/`/`tests/` tree for both names outside `cloud_api.py`/`server.py` themselves: no hits. Not
yet fixed.

### Finding 6: per-model catalog values, for reference

`DeviceType.java` (17,968 lines, one `super(id, otaType, ...)` subclass per model) is the source
of the four `check_firmware_update()` fields. `productId` is always a 32-hex-char string, differs
per model/generation even within one product family. `getOtaIdentifier()` is defined once on the
base class, **not** per-subclass - `id` (WIFI) returns `0` unless the model overrides it (a few
legacy MCU classes do); MCU-type models return their own `id`. `OTAType.java` confirms the wire
values: `WIFI(1)`, `MCU(2)`. Sample entries (id = Cync's numeric `deviceType` byte, same one
`cync-lan` already parses): a WIFI-type wired dimmer switch has `otaIdentifier=0`,
`otaBaseFirmwareVersion=2200`; a WIFI-type motion-sensing wired dimmer switch, same product line,
shares the identical `productId` string as the plain dimmer above - confirms product line, not
individual model checksum, drives `productId`; a standalone MCU-type motion sensor has
`otaIdentifier` equal to its own device-type id and a different `productId`.
`otaBaseFirmwareVersion`'s only three literals seen anywhere in the catalog are `2200` (default),
`2300`, and `1` - no comment documents units/scale, so "2.2.00"-style interpretation is plausible
inference only.

### Finding 7: live test against a real account - confirmed real device, real 404

2026-08-30: refreshed a stale cloud session (see "Update: refreshing a stale token cache without
reconfiguring the integration" below), then called `check_firmware_update()` for real against one
real WIFI-type wired-switch device on the account, with `current_version` deliberately set far
below anything real. Result: **HTTP 404**, `{"error": {"msg": "user have not subscribe device",
"code": 4001034}}` - not a malformed-request rejection, a distinct "this device isn't in the right
state" error. This is real signal a static-analysis-only pass couldn't have produced: the endpoint
does enforce *some* account/device relationship beyond the client-asserted `product_id` the
request body carries, contradicting this doc's own working assumption up to this point that
device identity checks on this specific channel were effectively absent.

### Finding 8: "subscription" is a live TCP/XLink artifact, not a REST toggle - blocks this path

A follow-up agent pass traced what "subscribed" means. **Confirmed**: it is not a REST action at
all. `XlinkAgent.subscribeDevice()` (`io/xlink/wifi/sdk/XlinkAgent.java:1043-1168`) sends a raw
packet (`TcpSendPacket.getInstance().subscriptionDevice(...)`) over an already-open TCP session to
`cm.gelighting.com:23779` - the same relay channel Finding 1 already established is
unauthenticated. The payload needs the device's `subKey`/`accessKey`, set during WiFi
commissioning (`WifiDeviceService.java:2325-2347`). There is no `POST .../subscribe` REST
endpoint anywhere in the app - the `GET /user/{userId}/subscribe/devices` endpoint
`cloud_api.py`'s `request_device_data()` already calls is a same-named but unrelated whole-account
device-list read, confirmed called from `LocationServiceDefault`/`WifiDeviceService.fetchWifiLocations()`,
not a per-device subscribe action.

**Confirmed**: the app only calls per-device `subscribeDevice` from the WiFi commissioning flow
(`BaseNonHubDeviceCommissionService.m13677B()`, "subscribeDevices start/complete" log strings at
lines 1302/1509) for devices newly being added. No call site re-subscribes already-owned devices
on ordinary login/app-startup was found. Error code `4001034` itself doesn't appear anywhere in
decompiled sources (including resource strings) - the closest semantic match is a named-but-not-
value-recoverable constant, `XlinkErrorResponse.UNABLE_TO_OPERATE_UNABLE_TO_FIND_SUBSCRIBED_DEVICES`
(`XlinkErrorResponse.java:14`) - plausible match by name, its literal int value is inlined by the
Kotlin compiler and not recoverable from bytecode.

**Net conclusion**: eliciting a real OTA offer this way needs a live XLink TCP session performing
the subscribe handshake first - REST calls alone cannot satisfy it, and a device whose real WiFi
traffic has been redirected away from Cync's actual relay (as this whole project does) can
plausibly have its subscription lapse over time with nothing to restore it automatically. This is
a materially bigger undertaking than the REST path looked like at the outset - closer to a second,
narrower mitm-adjacent protocol client than one more API call. Stopped here; `subKey`/`accessKey`
recovery and the subscribe packet's exact wire format are the next things needed if this gets
picked back up.

## Update: refreshing a stale cloud token cache without reconfiguring the integration

The HA custom_component already ships a proper reauth flow (`config_flow.py`'s
`async_step_reauth`/`async_step_reauth_confirm`/`async_step_otp` - updates the *existing* config
entry, never creates a second one). **Confirmed gap**: nothing in the integration's running code
ever calls `entry.async_start_reauth()` or raises `ConfigEntryAuthFailed`, so Home Assistant never
surfaces the "Reauthenticate" card that would normally trigger it - `util.py`'s
`refresh_cloud_export()` already has a comment acknowledging this ("recovering ... requires the
user to go through the config flow's reauth step themselves") but nothing actually offers that
step. The flow exists in code and is currently unreachable through the UI.

Worked around live, 2026-08-30, without touching the config entry: `CyncConfig.secret_key` is
derived from Home Assistant's own stable instance UUID (`util.py`'s `stable_secret()`, backed by
`homeassistant.helpers.instance_id`, persisted at `.storage/core.uuid`) - reading that file plus
the config entry's already-stored `account_username`/`account_password`
(`.storage/core.config_entries`) is enough to construct a standalone `CyncCloudAPI` instance (not
`.shared()`, to avoid touching the live singleton) and drive `request_otp()`/`send_otp()` exactly
as the reauth UI would, writing a fresh token straight to the same `.cloud_auth.enc.json` the live
integration already reads from disk on demand. No entry update, no reload, no interruption to
local device control (verified via live logs showing normal mesh traffic throughout).

**Worth fixing properly**: wiring an actual `entry.async_start_reauth()` call into
`refresh_cloud_export()`'s failure branch so this becomes a normal "Reauthenticate" button instead
of something that needs a manual script.
