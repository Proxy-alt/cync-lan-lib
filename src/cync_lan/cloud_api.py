import asyncio
import base64
import datetime
import hashlib
import json
import logging
import os
import random
import signal
import string
from pathlib import Path
from typing import Any, Optional, Union

import aiohttp
import yaml
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from pydantic import ValidationError

from cync_lan.const import (
    CYNC_ACCOUNT_LANGUAGE,
    CYNC_ACCOUNT_PASSWORD,
    CYNC_ACCOUNT_USERNAME,
    CYNC_API_BASE,
    CYNC_CLOUD_AUTH_PATH,
    CYNC_CONFIG_DIR,
    CYNC_CONFIG_FILE_PATH,
    CYNC_CORP_ID,
    CYNC_EXPORT_SOURCE,
    CYNC_FIRMWARE_CAPTURE_DIR,
    CYNC_LOG_NAME,
    CYNC_OVERWRITE_CONFIG_FILE,
    CYNC_SECRET_KEY,
)
from cync_lan.structs import ComputedTokenStruct, EntityState, GlobalObject

logger = logging.getLogger(CYNC_LOG_NAME)
g = GlobalObject()


def _write_yaml_and_chmod(path: Path, data: dict) -> None:
    """Write a dict as YAML and chmod it - meant to run inside an
    executor, not called directly from the event loop."""
    with open(path, "w") as f:
        f.write(yaml.dump(data))
    os.chmod(path, 0o777)


# Cync's own 4-slot motion-sensor schedule model (confirmed via the decompiled
# Android app's ScheduleTimeSlot.java - see docs/cync_automations.md for the
# full research). Each device GROUP (not individual device) can carry up to
# one of these per slot.
SENSOR_SCHEDULE_SLOT_NAMES = {0: "morning", 1: "daytime", 2: "evening", 3: "sleep"}


def _decode_sensor_schedule_slot(raw_slot: dict) -> Optional[dict]:
    """Decode one raw groupsArray[].sensorSchedules[] entry.

    Returns None (caller skips it) for anything malformed - id outside
    0-3, missing start/end time - rather than raising. Real accounts have
    been observed with malformed data (e.g. a duplicate slot id and a
    missing one), see docs/cync_automations.md's data-quality caveat.
    """
    slot_id = raw_slot.get("id")
    if slot_id not in SENSOR_SCHEDULE_SLOT_NAMES:
        return None
    start, end = raw_slot.get("startTime"), raw_slot.get("endTime")
    if not start or not end:
        return None
    is_enabled = bool(raw_slot.get("isEnabled", False))
    simple_mode = bool(raw_slot.get("simpleMode", True))
    # MotionSensorResponseMode, confirmed via SensorSchedule2Mapper.java -
    # see docs/cync_automations.md.
    mode = "disabled" if not is_enabled else ("simple" if simple_mode else "occupancy")
    return {
        "slot_id": slot_id,
        "enabled": is_enabled,
        "mode": mode,
        # startTime/endTime are "YYYY-MM-DD HH:MM" with a placeholder date
        # (confirmed, docs/cync_automations.md) - keep only HH:MM.
        "start_time": start.split(" ")[-1],
        "end_time": end.split(" ")[-1],
        "brightness": raw_slot.get("brightness"),  # 0-100, passthrough
        # Raw 0-100 warm(0)->cool(100) percentage (confirmed, CctColor.java)
        # - NOT an index into anything, despite the name's resemblance to
        # a color-temp lookup.
        "cct": raw_slot.get("cct"),
        "display_name": raw_slot.get("displayName") or "",
    }


def _decode_sensor_schedules(raw_schedules) -> dict:
    """{slot_name: slot_dict} for every valid slot in a group's raw
    sensorSchedules list.

    Duplicate slot ids: last-write-wins, matching the real Cync app's own
    SensorSchedule2Mapper.b() (confirmed) - so what this shows matches
    what the Cync app itself would show for the same account. Tolerant by
    design: never raises on malformed input, worst case returns fewer
    than 4 slots (or {}).
    """
    decoded: dict = {}
    for raw_slot in raw_schedules or []:
        if not isinstance(raw_slot, dict):
            continue
        slot = _decode_sensor_schedule_slot(raw_slot)
        if slot is None:
            continue
        decoded[SENSOR_SCHEDULE_SLOT_NAMES[slot["slot_id"]]] = slot
    return decoded


# See the comment at its use site in send_otp. Named rather than inlined so
# the number is findable from a bug report that only quotes error 4001007.
PASSWORD_MAX_LENGTH = 16


class CyncCloudAPI:
    api_timeout: int = 8
    lp: str = "CyncCloudAPI"
    auth_cache_file = CYNC_CLOUD_AUTH_PATH
    token_cache: ComputedTokenStruct
    http_session: aiohttp.ClientSession = None
    # inject-websession: True once a caller-owned session has been injected
    # via the session= kwarg (see __init__) - close() must not close a
    # session it doesn't own.
    _session_injected: bool = False
    # The most recent firmware image captured, as the sidecar metadata dict
    # plus `path` and `captured_at`. None until one lands - which may be
    # months, since it depends on the vendor publishing a release. Read by the
    # Home Assistant integration's "Last Firmware Released" sensor; kept here
    # rather than only on disk so a consumer does not have to scrape a
    # directory to notice something arrived.
    last_firmware_capture: Optional[dict] = None
    _instance: "CyncCloudAPI" = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self, **kwargs: Any) -> None:
        self.api_timeout = kwargs.get("api_timeout", 8)
        self.lp = kwargs.get("lp", self.lp)
        session = kwargs.get("session")
        if session is not None:
            # Accept a caller-owned aiohttp session (e.g. Home Assistant's
            # shared session via
            # homeassistant.helpers.aiohttp_client.async_get_clientsession)
            # instead of always creating our own. This is a singleton
            # (__new__ above), so __init__ re-runs on every CyncCloudAPI()
            # call - only overwrite http_session when a session is actually
            # passed, so bare CyncCloudAPI() calls elsewhere in the codebase
            # keep using whatever session (injected or self-created) is
            # already set, rather than clobbering it back to None.
            self.http_session = session
            self._session_injected = True

    async def close(self):
        """
        Close the aiohttp session if it exists, is not closed, and is one
        we created ourselves - never close a caller-injected session, its
        lifecycle belongs to whoever passed it in.
        """
        lp = f"{self.lp}:close:"
        if self._session_injected:
            logger.debug(f"{lp} Session was injected by caller, not closing it here")
            return
        if self.http_session and not self.http_session.closed:
            logger.debug(f"{lp} Closing aiohttp ClientSession")
            await self.http_session.close()
            self.http_session = None

    async def _check_session(self):
        """
        Check if the aiohttp session is initialized.
        If not, create a new session. No-op if a session was injected -
        that session's readiness is the injecting caller's responsibility.
        """
        if self._session_injected:
            return
        if not self.http_session or self.http_session.closed:
            logger.debug(
                f"{self.lp}:_check_session: Creating new aiohttp ClientSession"
            )
            self.http_session = aiohttp.ClientSession()
            await self.http_session.__aenter__()

    async def read_token_cache(self) -> Optional[ComputedTokenStruct]:
        """
        Read the encrypted token cache from a file.
        Returns:
            CloudTokenData: The cached token data if available, otherwise None.
        """
        lp = f"{self.lp}:read_token_cache:"
        try:
            # async-dependency: plain open() blocks the event loop - flagged by
            # HA's own blocking-call detector when this class runs inside a HA
            # custom_component. run_in_executor works identically for the
            # standalone add-on's asyncio loop too, unlike a HA-specific helper.
            encrypted_data = await asyncio.get_running_loop().run_in_executor(
                None, Path(self.auth_cache_file).read_bytes
            )
            cipher = self._get_fernet_cipher()
            decrypted_json = cipher.decrypt(encrypted_data)
            token_dict = json.loads(decrypted_json.decode("utf-8"))
            logger.debug(f"{lp} Cached token data read and decrypted successfully")
            return ComputedTokenStruct(**token_dict)
        except FileNotFoundError:
            logger.debug(f"{lp} Token cache file not found: {self.auth_cache_file}")
            return None
        except Exception as e:
            logger.error(
                f"{lp} Failed to decrypt or parse token cache. It may be corrupt or the secret key changed: {e}"
            )
            return None

    async def check_token(self) -> bool:
        """Check if we need to request a new OTP code for 2FA authentication."""
        lp = f"{self.lp}:check_tkn:"
        self.token_cache = await self.read_token_cache()
        if not self.token_cache:
            logger.debug(f"{lp} No cached token found, requesting OTP...")
            return False
        if self.token_cache.expires_at < datetime.datetime.now(datetime.UTC):
            # try refreshing first
            succ = await self.refresh_access_token()
            if not succ:
                logger.debug(f"{lp} Token expired, requesting OTP...")
            return succ

        else:
            logger.debug(f"{lp} Token is valid, using cached token")
        return True

    async def request_otp(self) -> bool:
        """
        Request an OTP code for 2FA authentication.
        """
        lp = f"{self.lp}:request_otp:"
        await self._check_session()
        req_otp_url = f"{CYNC_API_BASE}two_factor/email/verifycode"
        if CYNC_EXPORT_SOURCE is None:
            if not CYNC_ACCOUNT_USERNAME or not CYNC_ACCOUNT_PASSWORD:
                logger.error(
                    f"{lp} Cync account username or password not set, cannot request OTP!"
                )
                return False
            auth_data = {
                "corp_id": CYNC_CORP_ID,
                "email": CYNC_ACCOUNT_USERNAME,
                "local_lang": CYNC_ACCOUNT_LANGUAGE,
            }
            sesh = self.http_session
            try:
                otp_r = await sesh.post(
                    req_otp_url,
                    json=auth_data,
                    timeout=aiohttp.ClientTimeout(total=self.api_timeout),
                )
                otp_r.raise_for_status()
            except aiohttp.ClientResponseError as e:
                logger.error(f"{lp} Failed to request OTP code: {e}")
                return False
        return True

    async def refresh_access_token(self) -> bool:
        return await self._send_tkn_post(
            f"{CYNC_API_BASE}user/token/refresh",
            {"refresh_token": self.token_cache.refresh_token},
            lp=f"{self.lp}:refresh token:",
        )

    async def send_otp(self, otp_code: Union[str, int]) -> bool:
        """Complete the two-factor exchange.

        The code is carried as a **string** all the way to the wire. It was
        typed `int` and coerced with `int(otp_code)`, which silently destroys
        any code the vendor issues with a leading zero - `int("012345")` is
        `12345`, six digits become five, and the API rejects it with no clue
        why. `"000000"` was worse still: it coerced to `0`, tripped the
        falsy check below, and was reported as "OTP code must be provided"
        for a code the user had entered correctly.

        Reported by @baudneo in the description of Proxy-alt/cync-lan-lib#1,
        alongside the password truncation that PR fixes.

        Integers are still accepted, since callers have been passing them,
        but they are formatted back to six digits rather than trusted - an
        int that has already lost its leading zero cannot be recovered here,
        so this only helps callers that never lost it.
        """
        lp = f"{self.lp}:send_otp:"
        await self._check_session()
        if otp_code is None or (isinstance(otp_code, str) and not otp_code.strip()):
            logger.error("OTP code must be provided")
            return False
        if isinstance(otp_code, int):
            otp_code = f"{otp_code:06d}"
        else:
            otp_code = otp_code.strip()
        if not otp_code.isdigit():
            logger.error(f"{lp} OTP code must be digits, got {otp_code!r}")
            return False
        # Both default to None when unset, and the truncation below
        # subscripts the password - so an unconfigured account turned a
        # clear "credentials not set" into TypeError: 'NoneType' object is
        # not subscriptable. request_otp has guarded this since it was
        # written; send_otp never did, and only started needing to when the
        # slice arrived.
        if not CYNC_ACCOUNT_USERNAME or not CYNC_ACCOUNT_PASSWORD:
            logger.error(
                f"{lp} Cync account username or password not set, cannot send OTP!"
            )
            return False

        api_auth_url = f"{CYNC_API_BASE}user_auth/two_factor"
        auth_data = {
            "corp_id": CYNC_CORP_ID,
            "email": CYNC_ACCOUNT_USERNAME,
            # Cync's signup form caps passwords at 16 characters, so an
            # account created with a longer one only ever had the first 16
            # stored. Sending the whole thing compares against something the
            # server never saved and returns 400 `{"error": {"msg":
            # "password error", "code": 4001007}}` - a credential mismatch
            # rather than a malformed request, which is why it reads as a
            # wrong password and invites the user to retype it and fail
            # again. Truncation reconstructs what the account actually has.
            #
            # Truncate only, never strip. A space is a legal password
            # character, and an account whose stored password genuinely ends
            # in one cannot be told apart from a stray keystroke - so nothing
            # changes except the length. There is a test on this, because
            # "tidy the input" is the obvious-looking change that breaks it.
            #
            # A login shim, not validation: if this package ever grows a
            # registration path, the limit belongs at the input instead.
            "password": CYNC_ACCOUNT_PASSWORD[:PASSWORD_MAX_LENGTH],
            "two_factor": otp_code,
            "resource": "".join(random.choices(string.ascii_lowercase, k=16)),
        }
        logger.debug(
            f"{lp} Sending OTP code: {otp_code} to Cync Cloud API for authentication"
        )
        return await self._send_tkn_post(api_auth_url, auth_data, lp=lp)

    async def _send_tkn_post(
        self, url: str, data: dict, lp: Optional[str] = None
    ) -> bool:
        """POST to a token-issuing endpoint and cache the resulting token.

        `lp` is the caller's own log prefix. It used to be read as a bare
        name here without ever being defined or passed in, so the generic
        handler below raised NameError instead of returning False - and
        that handler is what catches a plain network failure
        (ClientConnectorError/TimeoutError are not ClientResponseError).
        A dropped connection during token refresh or OTP submission
        therefore blew up with "name 'lp' is not defined" instead of
        reporting a clean auth failure to the caller.

        Building the token model is deliberately INSIDE the try as well.
        It used to sit in an `else:` block, outside every handler above, so
        a response that parsed as JSON but did not match the model raised
        straight through a method whose whole contract is to return a bool -
        see the identity-field note below for the case that actually hit.
        """
        lp = lp or f"{self.lp}:token post:"
        try:
            self.http_session: aiohttp.ClientSession
            resp = await self.http_session.post(
                url, json=data, timeout=aiohttp.ClientTimeout(total=self.api_timeout)
            )
            resp.raise_for_status()
            iat = datetime.datetime.now(datetime.UTC)
            token_data = await resp.json()
            # add issued_at to the token data for computing the expiration datetime
            token_data["issued_at"] = iat
            # The refresh endpoint returns a new access/refresh token pair and
            # nothing else: user_id and authorize do not change on a refresh, so
            # it does not resend them. RawTokenStruct requires both, which made
            # EVERY refresh fail validation - confirmed on a real account, where
            # this raised on every scheduled export refresh:
            #
            #   ValidationError: 2 validation errors for ComputedTokenStruct
            #   user_id    Field required
            #   authorize  Field required
            #
            # Carry them over from the token being refreshed. Only fills fields
            # the response genuinely omitted, so a full login (which does return
            # them) is unaffected.
            cached = getattr(self, "token_cache", None)
            if cached is not None:
                for identity_field in ("user_id", "authorize"):
                    token_data.setdefault(
                        identity_field, getattr(cached, identity_field)
                    )
            token = ComputedTokenStruct(**token_data)
        except aiohttp.ClientResponseError as e:
            logger.error(f"{lp} Failed to authenticate: {e}")
            return False
        except json.JSONDecodeError as je:
            logger.error(f"{lp} Failed to decode JSON: {je}")
            return False
        except ValidationError as ve:
            logger.error(f"{lp} Token response did not match the expected shape: {ve}")
            return False
        except KeyError as ke:
            logger.error(f"{lp} Failed to get key from JSON: {ke}")
            return False
        except Exception as e:
            logger.warning(f"{lp} Failed to refresh credentials: {e}")
            return False
        else:
            return await self.write_token_cache(token)

    async def write_token_cache(self, tkn: ComputedTokenStruct) -> bool:
        """
        Write the encrypted token cache to file.
        Args:
            tkn (ComputedTokenStruct): The token data to write to the cache.
        Returns:
            bool: True if the write was successful, False otherwise.
        """
        lp = f"{self.lp}:write_token_cache:"
        try:
            json_data = tkn.model_dump_json().encode("utf-8")
            cipher = self._get_fernet_cipher()
            encrypted_data = cipher.encrypt(json_data)

            def _write() -> None:
                Path(self.auth_cache_file).write_bytes(encrypted_data)
                os.chmod(self.auth_cache_file, 0o777)

            # async-dependency: same blocking-call fix as read_token_cache above.
            await asyncio.get_running_loop().run_in_executor(None, _write)
            logger.debug(
                f"{lp} Token cache encrypted and written successfully to: {self.auth_cache_file}"
            )
            self.token_cache = tkn
            return True
        except Exception as e:
            logger.error(f"{lp} Failed to write encrypted token cache: {e}")
            return False

    async def request_device_data(self):
        """Get a list of Cync homes that have their own devices for a particular account."""
        lp = f"{self.lp}:get_devices:"
        await self._check_session()
        user_id = self.token_cache.user_id
        access_token = self.token_cache.access_token
        api_devices_url = f"{CYNC_API_BASE}user/{user_id}/subscribe/devices"
        headers = {"Access-Token": access_token}
        sesh = self.http_session
        try:
            r = await sesh.get(
                api_devices_url,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=self.api_timeout),
            )
        except aiohttp.ClientResponseError as e:
            logger.error(f"{lp} Failed to get devices: {e}")
            raise e
        except json.JSONDecodeError as je:
            logger.error(f"{lp} Failed to decode JSON: {je}")
            raise je
        except KeyError as ke:
            logger.error(f"{lp} Failed to get key from JSON: {ke}")
            raise ke
        else:
            ret = await r.json()

        # {'error': {'msg': 'Access-Token Expired', 'code': 4031021}}
        if "error" in ret:
            error_data = ret["error"]
            if (
                "msg" in error_data
                and error_data["msg"]
                and error_data["msg"].lower() == "access-token expired"
            ):
                logger.error(f"{lp} Access-Token expired, you need to re-authenticate!")
                # logger.error(f"{lp} Access-Token expired, re-authenticating...")
                # return self.get_devices(*self.authenticate_2fa())
        return ret

    async def get_cync_home_properties(self, product_id: str, device_id: str):
        """
        Get properties for a Cync home. Properties contain a device list (bulbsArray),
        groups (groupsArray), and saved light effects (lightShows).
        """
        lp = f"{self.lp}:get_properties:"
        await self._check_session()
        access_token = self.token_cache.access_token
        api_device_prop_url = (
            f"{CYNC_API_BASE}product/{product_id}/device/{device_id}/property"
        )
        headers = {"Access-Token": access_token}
        sesh = self.http_session
        try:
            r = await sesh.get(
                api_device_prop_url,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=self.api_timeout),
            )
            ret = await r.json()
        except aiohttp.ClientResponseError as e:
            logger.error(f"{lp} Failed to get device properties: {e}")
        except json.JSONDecodeError as je:
            logger.error(f"{lp} Failed to decode JSON: {je}")
            raise je
        except KeyError as ke:
            logger.error(f"{lp} Failed to get key from JSON: {ke}")
            raise ke

        # {'error': {'msg': 'Access-Token Expired', 'code': 4031021}}
        logit = False
        if "error" in ret:
            error_data = ret["error"]
            if "msg" in error_data and error_data["msg"]:
                if error_data["msg"].lower() == "access-token expired":
                    raise Exception(
                        f"{lp} Access-Token expired, you need to re-authenticate!"
                    )
                    # logger.error("Access-Token expired, re-authenticating...")
                    # return self.get_devices(*self.authenticate_2fa())
                else:
                    logit = True

                if "code" in error_data:
                    cync_err_code = error_data["code"]
                    if cync_err_code == 4041009:
                        # no properties for this home ID
                        # I've noticed lots of empty homes in the returned data,
                        # we only parse homes with an assigned name and a 'bulbsArray'
                        logit = False
                    else:
                        logger.debug(
                            f"{lp} DBG>>> error code != 4041009 (int) ---> {type(cync_err_code) = } -- {cync_err_code =} /// setting logit = True"
                        )
                        logit = True
                else:
                    logger.debug(
                        f"{lp} DBG>>> no 'code' in error data, setting logit = True"
                    )
                    logit = True
            if logit is True:
                logger.warning(f"{lp} Cync Cloud API Error: {error_data}")
        return ret

    async def check_firmware_update(
        self,
        device_id: int,
        product_id: str,
        ota_type: int,
        identify: int,
        current_version: int,
    ) -> Optional[dict]:
        """Ask the cloud whether a firmware update exists for one device.

        Endpoint and payload confirmed from the app: `Cloud$Firmware.a()` builds
        `{base}/upgrade/firmware/check/{id}/geapp?useHttps=true`, and
        `FirmwareUpgradeTaskResponse` declares the reply fields - notably
        `target_version_url`, `target_version_md5` and `target_version_size`,
        i.e. the vendor hands out a direct, plain download link.

        Returns the parsed upgrade task, or `{"up_to_date": True, ...}` when the
        cloud says there is nothing to install (HTTP 404 with code 4041013),
        or None on any other failure.

        **This asks a question and nothing more.** It sends no device traffic.
        """
        lp = f"{self.lp}:firmware check:"
        url = f"{CYNC_API_BASE}upgrade/firmware/check/{device_id}/geapp?useHttps=true"
        payload = {
            "type": ota_type,
            "identify": identify,
            "product_id": product_id,
            "current_version": current_version,
        }
        headers = {"Content-Type": "application/json"}
        if self.token_cache is not None:
            headers["Access-Token"] = self.token_cache.access_token
        try:
            async with self.http_session.post(
                url,
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=self.api_timeout),
            ) as resp:
                body = await resp.json(content_type=None)
                if resp.status == 200:
                    return body
                # The cloud's way of saying "already current".
                code = (body or {}).get("error", {}).get("code")
                if code == 4041013:
                    return {"up_to_date": True, "code": code}
                logger.debug(f"{lp} HTTP {resp.status} for device {device_id}: {body}")
                return None
        except Exception as exc:
            logger.debug(f"{lp} device {device_id}: {exc}")
            return None

    async def capture_firmware(
        self, task: dict, dest_dir: Optional[str] = None
    ) -> Optional[Path]:
        """Download a firmware image to disk for inspection. Never installs it.

        Deliberately has no path to a device. It takes an upgrade task, fetches
        the URL the cloud published, writes it to a file and verifies it - there
        is no session, no opcode and no device argument anywhere in this
        function, so it cannot flash something by accident or by a later edit
        that "adds the install step while we're here".

        Verifies against the cloud's own `target_version_md5` and
        `target_version_size` and records the outcome in a sidecar `.json`. A
        mismatch is written down rather than raised: a truncated or re-signed
        image is itself a finding, and deleting the evidence would be the wrong
        response to it.

        Returns the path written, or None if there was nothing to fetch.
        """
        lp = f"{self.lp}:firmware capture:"
        url = task.get("target_version_url")
        if not url:
            return None
        directory = Path(dest_dir or CYNC_FIRMWARE_CAPTURE_DIR or ".")
        directory.mkdir(parents=True, exist_ok=True)

        version = str(task.get("target_version", "unknown"))
        product = str(task.get("product_id", "unknown"))
        stem = f"cync_fw_{product}_{version}".replace("/", "_")
        target = directory / f"{stem}.bin"
        if target.exists():
            # Already captured. Re-publish it as the last known capture anyway:
            # the device never actually updates (nothing here installs), so the
            # cloud keeps offering the same release forever, and this is what
            # repopulates the sensor after a restart without re-downloading.
            logger.debug(f"{lp} already have {target.name}, skipping download")
            sidecar = directory / f"{stem}.json"
            try:
                known = json.loads(sidecar.read_text()) if sidecar.exists() else {}
            except Exception:
                known = {}
            if self.last_firmware_capture is None:
                self.last_firmware_capture = {
                    **known,
                    "path": str(target),
                    "captured_at": datetime.datetime.fromtimestamp(
                        target.stat().st_mtime, datetime.timezone.utc
                    ).isoformat(),
                }
            return target

        try:
            async with self.http_session.get(
                url, timeout=aiohttp.ClientTimeout(total=300)
            ) as resp:
                if resp.status != 200:
                    logger.warning(f"{lp} HTTP {resp.status} fetching {url}")
                    return None
                blob = await resp.read()
        except Exception as exc:
            logger.warning(f"{lp} could not fetch {url}: {exc}")
            return None

        digest = hashlib.md5(blob).hexdigest()  # noqa: S324 - matching the vendor
        expected_md5 = (task.get("target_version_md5") or "").lower()
        expected_size = task.get("target_version_size")
        meta = {
            "url": url,
            "product_id": product,
            "target_version": version,
            "from_version": task.get("from_version"),
            "bytes_written": len(blob),
            "md5": digest,
            "expected_md5": expected_md5 or None,
            "expected_size": expected_size,
            "md5_matches": (digest == expected_md5) if expected_md5 else None,
            "size_matches": (
                len(blob) == expected_size if expected_size is not None else None
            ),
        }
        target.write_bytes(blob)
        (directory / f"{stem}.json").write_text(json.dumps(meta, indent=2))
        self.last_firmware_capture = {
            **meta,
            "path": str(target),
            "captured_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }

        if meta["md5_matches"] is False or meta["size_matches"] is False:
            logger.warning(
                f"{lp} {target.name} does not match what the cloud advertised "
                f"(md5 {digest} vs {expected_md5}, {len(blob)} vs {expected_size} "
                "bytes). Kept anyway - the mismatch is itself worth looking at."
            )
        else:
            logger.info(
                f"{lp} captured {target.name} ({len(blob)} bytes) for inspection. "
                "Not installed - nothing was sent to any device."
            )
        return target

    async def export_config_file(self) -> bool:
        """Get Cync devices from the cloud"""
        if CYNC_EXPORT_SOURCE is not None:
            logger.warning(
                f"{self.lp} The source for export has been configured as a file: {CYNC_EXPORT_SOURCE} "
                f"skipping cloud export and using the provided file instead..."
            )
            src_file = Path(CYNC_EXPORT_SOURCE)
            if not src_file.exists():
                logger.error(
                    f"{self.lp} The provided export source file does not exist: {CYNC_EXPORT_SOURCE}"
                )
                return False
            elif not src_file.is_file():
                logger.error(
                    f"{self.lp} The provided export source path is not a file: {CYNC_EXPORT_SOURCE}"
                )
                return False
            else:
                try:
                    with src_file.open("r") as f:
                        exported_data = yaml.safe_load(f)
                except Exception as file_exc:
                    logger.error(
                        f"{self.lp} Failed to read export source file: {CYNC_EXPORT_SOURCE} -> {file_exc}"
                    )
                    return False
                else:
                    logger.debug(
                        f"{self.lp} Successfully read export source file: {CYNC_EXPORT_SOURCE}"
                    )
        else:
            # use the cloud
            exported_data = await self.request_device_data()
        cync_lan_cfg = await self._parse_raw_export(exported_data)
        # write config to file in YAML format
        base_cfg_path = Path(CYNC_CONFIG_FILE_PATH)
        raw_cfg_file_out = base_cfg_path
        if CYNC_OVERWRITE_CONFIG_FILE is False:
            counter = 1
            while raw_cfg_file_out.exists():
                raw_cfg_file_out = base_cfg_path.with_name(
                    f"{base_cfg_path.stem}_{counter}{base_cfg_path.suffix}"
                )
                counter += 1
        try:
            # async-dependency: same blocking-call fix as read_token_cache -
            # write_yaml_and_chmod runs the open()/write()/chmod() sequence
            # inside the executor rather than on the event loop. Confirmed
            # via a real HA install flagging this exact line.
            await asyncio.get_running_loop().run_in_executor(
                None, _write_yaml_and_chmod, raw_cfg_file_out, cync_lan_cfg
            )
        except Exception as file_exc:
            logger.error(
                f"{self.lp} Failed to write cync-lan config to file: {CYNC_CONFIG_FILE_PATH} -> {file_exc}"
            )
            return False
        else:
            return True

    async def _parse_raw_export(self, exported_home_data: dict):
        """Take exported cloud data and format it into a working config dict to be dumped in YAML format."""
        lp = f"{self.lp}:parse export:"
        new_cfg = {}
        # What we get from the Cync cloud API
        base_file_path = Path(CYNC_CONFIG_DIR) / "raw_mesh.cync"
        raw_file_out = base_file_path
        # strip out empty configs (IDK why, I have a bunch with access_code 77777 that are empty)
        for raw_home in exported_home_data:
            if "name" not in raw_home or len(raw_home["name"]) < 1:
                logger.debug(
                    f"{lp} No name found for Cync home (safely ignore), skipping..."
                )
                # I see several empty 'home' configs in the returned data, they don't have a name,
                # any properties/devices, so we can safely ignore them
                continue
            if "properties" not in raw_home:
                # only pull device list for valid Cync homes
                raw_home["properties"] = await self.get_cync_home_properties(
                    raw_home["product_id"], raw_home["id"]
                )
            if "bulbsArray" not in raw_home["properties"]:
                logger.debug(
                    f"{lp} No 'bulbsArray' in Cync home: '{raw_home['name']}' properties (safely ignore), skipping..."
                )
                continue
            logger.debug(
                f"{lp} 'properties' and 'bulbsArray' found in exported config, proceeding..."
            )
            new_home: dict = {
                kv: raw_home[kv] for kv in ("access_key", "id", "mac") if kv in raw_home
            }
            new_cfg[raw_home["name"]] = new_home
            new_home["devices"] = {}
            entity_reg = {}
            for raw_device in raw_home["properties"]["bulbsArray"]:
                if any(
                    checkattr not in raw_device
                    for checkattr in (
                        "deviceID",
                        "displayName",
                        "mac",
                        "deviceType",
                        "firmwareVersion",
                    )
                ):
                    logger.warning(
                        f"{lp} Missing required attribute (deviceID, displayName, deviceType, mac, firmwareVersion) in Cync device, skipping: {raw_device}"
                    )
                    continue
                new_device: dict = {}
                # wifiMac is legitimately absent for standalone BTLE-only accessories
                # (e.g. motion sensors, deviceType 96) - they have no WiFi radio at all.
                # Requiring it here silently dropped every such device from the
                # exported config (confirmed via a real export: a motion sensor's raw
                # cloud entry has no wifiMac key whatsoever). Downstream code already
                # handles this gracefully via CyncDevice.bt_only, which skips using
                # wifi_mac entirely for BTLE-only protocol devices.
                wifi_mac = (
                    str(raw_device["wifiMac"]) if "wifiMac" in raw_device else None
                )
                bt_mac = str(raw_device["mac"])
                dev_name = str(raw_device["displayName"])
                dev_type = int(raw_device["deviceType"])
                fw_ver = str(raw_device["firmwareVersion"])
                # switchID: a foreign key to the WiFi hub/parent device's own entry in
                # exported_home_data - confirmed by cross-referencing a real export,
                # every non-zero switchID value matched exactly one other entry's "id"
                # field (49/49 match rate). Each physical WiFi hub gets its own
                # synthetic top-level "home" entry in the cloud API response (only
                # entries with a populated bulbsArray are real homes; the rest exist
                # solely to be pointed at by switchID), matching the "no name found /
                # no bulbsArray" skip logic already just above in this loop. Not
                # currently used by cync-lan - devices are already keyed by their own
                # dev_id, not by which hub they're wired through.
                raw_id = str(raw_device["deviceID"])
                home_id = raw_id[:9]
                raw_dev = raw_id.split(home_id)[1]
                dev_id = int(raw_dev[-3:])
                sub_id = 0
                # Seems the thermostat has a multi-endpoint deviceID but is a single entry
                # 169573386
                # 7 (multi endpoint is 3 digit, this is only 1)
                # 009
                # { "hvacSystem": { "changeoverMode": 0, "auxHeatStages": 1, "auxFurnaceType": 1, "stages": 1, "furnaceType": 1, "type": 2, "powerLines": 1 },
                # "thermostatSensors": [ { "pin": "025572", "name": "Living Room", "type": "savant" }, { "pin": "044604", "name": "Bedroom Sensor", "type": "savant" }, { "pin": "022724", "name": "Thermostat sensor 3", "type": "savant" } ] } ]
                hvac_cfg = None
                if "hvacSystem" in raw_device:
                    hvac_cfg = raw_device["hvacSystem"]
                    if "thermostatSensors" in raw_device:
                        hvac_cfg["thermostatSensors"] = raw_device["thermostatSensors"]
                    logger.debug(
                        f"{lp} Found HVAC device '{dev_name}' (ID: {dev_id}): {hvac_cfg}"
                    )
                    logger.info(
                        f"{lp} HVAC devices are currently unsupported, work is in progress to "
                        f"get the thermostat and temp sensors added"
                    )
                    continue
                    new_device["hvac"] = hvac_cfg
                if len(raw_dev) > 4:
                    # firmwareVersion = Unknown is also an identifier for sub-devices
                    # sub-device wifiMac will always be 01:02:03:04:05:06 even if parent has WiFi, BT MACs match
                    sub_id = int(raw_dev[:3])
                    if dev_id in entity_reg:
                        if sub_id in entity_reg[dev_id]:
                            logger.error(
                                f"{lp} Duplicate sub-device ID {sub_id} found for parent device ID {dev_id} in home "
                                f"'{raw_home['name']}' (device name: '{dev_name}'), Please open an issue with debug "
                                f"logs enabled..."
                            )
                            continue
                    logger.info(
                        f"{lp} Staging sub-device ({sub_id}) named: '{dev_name}' with parent device ID {dev_id} in "
                        f"home '{raw_home['name']}' devices registry"
                    )
                    state = EntityState(dev_id=dev_id, sub_id=sub_id, name=dev_name)
                    if dev_id in entity_reg:
                        entity_reg[dev_id][sub_id] = state.name
                    else:
                        entity_reg[dev_id] = {sub_id: state.name}
                    continue
                elif len(raw_dev) == 4:
                    logger.debug(
                        f"{lp} FOUND: '{dev_name}' has a thermostat related quirk with a value of: {raw_dev[3]}"
                    )
                # END OF SUB DEVICE PARSING

                # cync_device = CyncNode(
                #     name=dev_name,
                #     node_id=dev_id,
                #     dev_type=dev_type,
                #     mac=bt_mac,
                #     wifi_mac=wifi_mac,
                #     fw_version=fw_ver,
                #     hvac=hvac_cfg,
                # )
                new_device["name"] = dev_name
                new_device["type"] = dev_type
                # new_device["is_plug"] = cync_device.is_plug
                # new_device["supports_temperature"] = cync_device.supports_temperature
                # new_device["supports_rgb"] = cync_device.supports_rgb
                new_device["fw"] = fw_ver
                new_device["mac"] = bt_mac
                new_device["wifi_mac"] = wifi_mac
                # give it the default 0, if it has children, we will overwrite the 0
                new_device["endpoints"] = {0: dev_name}
                # del cync_device
                new_home["devices"][dev_id] = new_device

            # END OF DEVICE PARSING LOOP
            # check sub dev reg
            if entity_reg:
                for node_id, endpoint_data in entity_reg.items():
                    if node_id in new_home["devices"]:
                        # overwrite the default 0 endpoint with the children
                        new_home["devices"][node_id]["endpoints"] = endpoint_data

            # Cync device groups (e.g. "Living Room" containing several
            # individually-addressable switches/bulbs) - confirmed real via a
            # real account export: groupID is a synthetic pseudo-address
            # (32768 + sequential index, matching the app's own
            # MeshAddress.GROUP_ADDRESS_RANGE), deviceIDArray entries are the
            # same short dev_id keys used in new_home["devices"] above, no ID
            # translation needed. Purely additive - nothing currently reads
            # this key, existing consumers of the exported config are
            # unaffected.
            new_home["groups"] = {}
            for raw_group in raw_home["properties"].get("groupsArray", []):
                if "groupID" not in raw_group or "deviceIDArray" not in raw_group:
                    continue
                group_id = raw_group["groupID"]
                new_home["groups"][group_id] = {
                    "name": raw_group.get("displayName") or f"Group {group_id}",
                    "device_ids": list(raw_group["deviceIDArray"]),
                    "is_subgroup": bool(raw_group.get("isSubgroup", False)),
                    # {} when the group has no native motion-sensor schedule
                    # data (most groups) - see docs/cync_automations.md.
                    "sensor_schedules": _decode_sensor_schedules(
                        raw_group.get("sensorSchedules")
                    ),
                }

            # Cync Scenes (named multi-device state snapshots) and
            # Schedules (time/day triggers that fire a scene) - the
            # "Routines" tab, see docs/cync_automations.md. Field names
            # (sceneArray/sceneID/displayName, schedules/id-or-scheduleID/
            # trigger.action.sceneID/state) confirmed via decompiled
            # kotlinx.serialization descriptors, but UNVALIDATED against a
            # real populated export - the one real account sampled for
            # this research had zero scenes/schedules configured. Purely
            # additive, same as groups above.
            new_home["scenes"] = {}
            for raw_scene in raw_home["properties"].get("sceneArray", []):
                if "sceneID" not in raw_scene:
                    continue
                scene_id = raw_scene["sceneID"]
                new_home["scenes"][scene_id] = {
                    "name": raw_scene.get("displayName") or f"Scene {scene_id}",
                }

            new_home["schedules"] = {}
            for raw_schedule in raw_home["properties"].get("schedules", []):
                # scheduleID preferred over the sibling `id` field - both
                # are present on the DTO with no confirmed distinction,
                # see parse_schedules()'s docstring.
                schedule_id = raw_schedule.get("scheduleID", raw_schedule.get("id"))
                trigger = raw_schedule.get("trigger") or {}
                action = trigger.get("action") or {}
                if schedule_id is None or "sceneID" not in action:
                    continue
                new_home["schedules"][schedule_id] = {
                    "name": raw_schedule.get("displayName")
                    or f"Schedule {schedule_id}",
                    "scene_id": action["sceneID"],
                    # `state` is the closest boolean field on the DTO to
                    # an enabled/disabled flag - inferred, not confirmed,
                    # see parse_schedules()'s docstring. Defaults to
                    # enabled=True (matches every other "assume normal
                    # unless told otherwise" default in this codebase)
                    # when absent.
                    "enabled": bool(raw_schedule.get("state", True)),
                }

        # END OF HOME PARSING LOOP
        # write raw exported config to file for debugging, only if export source is None
        if CYNC_EXPORT_SOURCE is None:
            if CYNC_OVERWRITE_CONFIG_FILE is False:
                # basic numbered suffix logic to prevent overwriting existing files
                counter = 1
                while raw_file_out.exists():
                    raw_file_out = base_file_path.with_name(
                        f"{base_file_path.stem}_{counter}{base_file_path.suffix}"
                    )
                    counter += 1
            try:
                await asyncio.get_running_loop().run_in_executor(
                    None, _write_yaml_and_chmod, raw_file_out, exported_home_data
                )
            except Exception as file_exc:
                logger.error(
                    f"{lp} Failed to write RAW config to '{raw_file_out}': {file_exc}"
                )
            else:
                logger.debug(f"{lp} Dumped RAW cloud export data to: {raw_file_out}")

        config_dict = {"exported_homes": new_cfg}
        return config_dict

    def _get_fernet_cipher(self) -> Fernet:
        """
        Derives a secure, 32-byte url-safe base64-encoded key from the CYNC_SECRET_KEY
        passphrase using PBKDF2 and initializes a fernet cipher suite.
        """
        if not CYNC_SECRET_KEY:
            logger.critical("CYNC_SECRET_KEY is not set! You must configure this!")
            signal.raise_signal(signal.SIGINT)
        else:
            passphrase = CYNC_SECRET_KEY.encode()
        # A static salt is used because the target file is local and we need to derive
        # the exact same key across restarts without storing it on disk.
        salt = b"cync_lan_static_salt_for_local_storage"
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt,
            iterations=480000,
        )
        key = base64.urlsafe_b64encode(kdf.derive(passphrase))
        return Fernet(key)
