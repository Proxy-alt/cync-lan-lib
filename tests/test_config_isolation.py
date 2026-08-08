"""Two consumers in one interpreter must not become one consumer.

These pin the three failures reproduced on a real Home Assistant install
running `cync_lan` and `cync_ble` side by side, and written up in
`cync_ble`'s cloud.py as the reason it reimplemented a cloud client rather
than reuse this one. Each was silent: no exception, no log, just the second
integration quietly operating as the first.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from cync_lan import cloud_api
from cync_lan.cloud_api import CyncCloudAPI
from cync_lan.config import CyncConfig


def _reset_shared():
    CyncCloudAPI._instance = None


def test_two_clients_do_not_share_credentials():
    """The worst of the three, because it looked like it worked.

    Credentials typed into the second integration's wizard were ignored and
    the first account's used instead - a login that succeeds as the wrong
    user is far harder to notice than one that fails.
    """
    first = CyncCloudAPI(
        CyncConfig(config_dir="/one", username="alice@example.com", password="a")
    )
    second = CyncCloudAPI(
        CyncConfig(config_dir="/two", username="bob@example.com", password="b")
    )

    assert first is not second
    assert first.config.username == "alice@example.com"
    assert second.config.username == "bob@example.com"


def test_two_clients_do_not_share_a_config_directory():
    """Writes landed in cync-lan's directory while reads looked in cync_ble's,
    because every path is derived from a config dir frozen at import."""
    first = CyncCloudAPI(CyncConfig(config_dir="/config/cync_lan"))
    second = CyncCloudAPI(CyncConfig(config_dir="/config/cync_ble"))

    assert first.auth_cache_file != second.auth_cache_file
    assert second.auth_cache_file.startswith("/config/cync_ble/")
    assert second.config.config_file_path == "/config/cync_ble/cync_mesh.yaml"


def test_a_second_client_does_not_inherit_the_first_token_cache():
    """`CyncCloudAPI()` used to hand back the live instance, so the second
    integration picked up the first's *expired* token, tried to refresh it,
    and reported the failure to its user as "could not reach the Cync cloud
    API" - a fault in neither the cloud nor the caller."""
    first = CyncCloudAPI(CyncConfig(config_dir="/one"))
    first.token_cache = MagicMock(name="alice's expired token")

    second = CyncCloudAPI(CyncConfig(config_dir="/two"))

    assert not hasattr(second, "token_cache"), (
        "the second client started life holding the first client's token"
    )


def test_stubbing_one_client_does_not_leak_into_the_next():
    """A consequence rather than a headline, but it is why the OTP tests had
    to route every stub through monkeypatch: a plain assignment on what was
    secretly a singleton outlived its test."""
    first = CyncCloudAPI(CyncConfig(config_dir="/one"))
    first._send_tkn_post = "stub"

    assert not isinstance(
        getattr(CyncCloudAPI(CyncConfig(config_dir="/two")), "_send_tkn_post", None),
        str,
    )


def test_shared_is_still_available_for_the_things_that_want_it():
    """Removing the singleton must not remove process-wide sharing, only
    make asking for it explicit. The firmware sensor has no other route to
    what the server's periodic check captured."""
    _reset_shared()
    try:
        one = CyncCloudAPI.shared(CyncConfig(config_dir="/one"))
        two = CyncCloudAPI.shared()
        assert one is two

        one.last_firmware_capture = {"version": "1.2.3"}
        assert CyncCloudAPI.shared().last_firmware_capture == {"version": "1.2.3"}
    finally:
        _reset_shared()


def test_shared_applies_a_late_session_to_the_existing_instance():
    """Home Assistant's shared aiohttp session may not exist yet when the
    first caller builds the client."""
    _reset_shared()
    try:
        first = CyncCloudAPI.shared(CyncConfig(config_dir="/one"))
        session = MagicMock(name="hass session")

        again = CyncCloudAPI.shared(session=session)

        assert again is first
        assert first.http_session is session
        assert first._session_injected is True
    finally:
        _reset_shared()


def test_from_env_reads_the_environment_when_asked_not_when_imported(monkeypatch):
    """The whole point. `const.py` answers with whatever was set the first
    time anything imported it; this answers with what is set now.

    This is the same freeze that took down every device connection in
    0.10.3 - CYNC_MITM_LOG_DIR derived from a config dir a consumer set too
    late to matter, landing on an unwritable /root.
    """
    monkeypatch.setenv("CYNC_CONFIG_DIR", "/first")
    assert CyncConfig.from_env().config_dir == "/first"

    monkeypatch.setenv("CYNC_CONFIG_DIR", "/second")
    assert CyncConfig.from_env().config_dir == "/second", (
        "from_env cached, which is the defect it exists to remove"
    )


def test_from_env_derives_the_default_config_dir_the_way_const_does(monkeypatch):
    """Consumers that set nothing must land exactly where they always did."""
    for var in ("CYNC_CONFIG_DIR", "CYNC_BASE_DIR", "CYNC_CFGAPPEND_DIR"):
        monkeypatch.delenv(var, raising=False)

    from cync_lan import const

    assert CyncConfig.from_env().config_dir == const.CYNC_CONFIG_DIR


def test_a_relative_cfgappend_is_still_rooted(monkeypatch):
    """const.py prepends the slash; from_env has to agree or the two
    disagree about where a default install keeps its files."""
    monkeypatch.delenv("CYNC_CONFIG_DIR", raising=False)
    monkeypatch.setenv("CYNC_BASE_DIR", "/opt/cync")
    monkeypatch.setenv("CYNC_CFGAPPEND_DIR", "conf")

    assert CyncConfig.from_env().config_dir == "/opt/cync/conf"


def test_credentials_can_be_supplied_without_touching_the_environment():
    """A config flow holds a password it must not export - the environment is
    shared with every other consumer in the process, which is how this class
    of bug started."""
    base = CyncConfig(config_dir="/cfg")
    with_creds = base.with_credentials("carol@example.com", "hunter2")

    assert with_creds.username == "carol@example.com"
    assert with_creds.config_dir == "/cfg"
    assert base.username is None, "the original must be untouched"


def test_the_module_constants_are_still_there():
    """The add-on and the TCP server import these by name. Nothing about the
    single-consumer path changes."""
    from cync_lan import const

    assert isinstance(const.CYNC_CONFIG_DIR, str)
    assert const.CYNC_CONFIG_FILE_PATH.endswith("/cync_mesh.yaml")
    assert hasattr(cloud_api, "CYNC_API_BASE")
