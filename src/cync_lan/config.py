"""Per-consumer settings, resolved when asked for rather than at import.

`const.py` reads its environment once, the first time anything imports it,
and derives a tree of paths from `CYNC_CONFIG_DIR` at the same moment. That
is correct for the shape it was written in - one process, one account, the
add-on - and it is exactly what breaks when a second consumer shares the
interpreter.

Home Assistant is that second consumer, twice over: `cync_lan` and
`cync_ble` can both be installed, and whichever sets up first freezes the
paths for the other. Reproduced on a real install, `cync_ble` found that
setting `CYNC_CONFIG_DIR` had no effect (its writes landed in cync-lan's
directory while its reads looked in its own) and that
`CYNC_ACCOUNT_USERNAME`/`_PASSWORD` were ignored outright, so credentials
typed into its wizard were silently replaced by the other account's. That
last one is the dangerous kind, because it looks like it worked. Rather
than fight it, `cync_ble` wrote its own 243-line cloud client to stay away
from this module entirely.

The same freeze caused a live outage from the other direction:
`CYNC_MITM_LOG_DIR` is derived here from `CYNC_CONFIG_DIR`, so a consumer
that set its config directory after import got `/root/cync-lan/config`
anyway, and on a read-only root the unwritable path took down every device
connection (0.10.3).

So: this holds every setting a second consumer must be able to choose for
itself, and `from_env()` reads the environment at the moment it is called.
`const.py` keeps its module-level names - they are still the process
defaults, still what the add-on and the TCP server use, and nothing about
them changes. What changes is that code needing *per-consumer* settings can
now ask for them instead of inheriting whoever imported first.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from cync_lan.const import YES_ANSWER


def _env_flag(name: str, default: str) -> bool:
    return os.environ.get(name, default).casefold() in YES_ANSWER


@dataclass(frozen=True)
class CyncConfig:
    """Settings a consumer may need to differ from the process defaults.

    Frozen because the failure this exists to prevent is one consumer
    mutating settings another is using. Build a new one instead.

    Only the settings that genuinely vary per consumer live here. Vendor
    constants (`CYNC_API_BASE`, `CYNC_CORP_ID`) and settings belonging to
    the TCP server - which only ever has one owner in a process - stay in
    `const.py`.
    """

    config_dir: str
    username: Optional[str] = None
    password: Optional[str] = None
    secret_key: Optional[str] = None
    export_source: Optional[str] = None
    overwrite_config_file: bool = True
    firmware_capture_dir: Optional[str] = None

    # Paths derived from config_dir. Properties rather than fields so a
    # caller who passes only config_dir cannot end up with a mismatched set -
    # which is the precise shape of the cync_ble bug, writes and reads
    # disagreeing about where the directory is.
    @property
    def config_file_path(self) -> str:
        return f"{self.config_dir}/cync_mesh.yaml"

    @property
    def uuid_path(self) -> str:
        return f"{self.config_dir}/uuid.txt"

    @property
    def cloud_auth_path(self) -> str:
        return f"{self.config_dir}/.cloud_auth.enc.json"

    @property
    def raw_mesh_path(self) -> str:
        return f"{self.config_dir}/raw_mesh.cync"

    @classmethod
    def from_env(cls) -> "CyncConfig":
        """Read the environment now.

        Deliberately not cached. A consumer that sets `CYNC_CONFIG_DIR` and
        then constructs a client expects that directory, and the whole point
        of this module is that the answer depends on when you ask.
        """
        base_dir = os.environ.get("CYNC_BASE_DIR", "/root/cync-lan")
        append_dir = os.environ.get("CYNC_CFGAPPEND_DIR", "/config")
        if append_dir and not append_dir.startswith("/"):
            append_dir = f"/{append_dir}"
        config_dir = os.environ.get("CYNC_CONFIG_DIR", f"{base_dir}{append_dir}")

        return cls(
            config_dir=config_dir,
            username=os.environ.get("CYNC_ACCOUNT_USERNAME", None),
            password=os.environ.get("CYNC_ACCOUNT_PASSWORD", None),
            secret_key=os.environ.get("CYNC_SECRET_KEY", None),
            export_source=os.environ.get("CYNC_EXPORT_SOURCE"),
            overwrite_config_file=_env_flag("CYNC_OVERWRITE_CONFIG_FILE", "1"),
            firmware_capture_dir=os.environ.get("CYNC_FIRMWARE_CAPTURE_DIR") or None,
        )

    def with_credentials(self, username: str, password: str) -> "CyncConfig":
        """A copy carrying these credentials.

        For a config flow holding a password it must not put in the
        environment - which it could not do safely anyway, since the
        environment is shared with every other consumer in the process.
        """
        return replace_credentials(self, username, password)


def replace_credentials(config: CyncConfig, username: str, password: str) -> CyncConfig:
    """Module-level so `CyncConfig` stays a plain frozen dataclass."""
    return CyncConfig(
        config_dir=config.config_dir,
        username=username,
        password=password,
        secret_key=config.secret_key,
        export_source=config.export_source,
        overwrite_config_file=config.overwrite_config_file,
        firmware_capture_dir=config.firmware_capture_dir,
    )
