"""Settings read when they are used, not when a module was first imported.

`const.py` reads its environment once, at import. That is the shape behind
the 0.10.3 outage, and behind `cync_ble` finding that setting
`CYNC_CONFIG_DIR` had no effect. `CyncConfig` solved it for the cloud and
path settings; these cover the server settings, which had a different and
stranger version of the same problem.

`reload_env()` looked like the answer and was not. It assigned to a list of
names under `global`, but those names are not imported into `structs.py`, so
the statements created fresh module globals there that nothing read - and
left `cync_lan.const` untouched, which is where every consumer imports from.
Anything that had already done `from cync_lan.const import X` held its own
binding either way. It was the appearance of a mechanism rather than one.
"""

from __future__ import annotations

import cync_lan.structs as structs_module
from cync_lan.structs import GlobalObject


def _fresh_env(monkeypatch, **values):
    for name in (
        "CYNC_PORT",
        "CYNC_MAX_TCP_CONN",
        "CYNC_CMD_BROADCASTS",
        "CYNC_TCP_WHITELIST",
        "CYNC_SRV_HOST",
        "CYNC_BASE_DIR",
    ):
        monkeypatch.delenv(name, raising=False)
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    g = GlobalObject()
    g.reload_env()
    return g


def test_reload_env_reads_the_environment_at_call_time(monkeypatch):
    """The whole point: ask twice, get two answers."""
    g = _fresh_env(monkeypatch, CYNC_PORT="24000")
    assert g.env.cync_srv_port == 24000

    monkeypatch.setenv("CYNC_PORT", "25000")
    g.reload_env()
    assert g.env.cync_srv_port == 25000, "reload_env cached instead of re-reading"


def test_the_server_settings_reload_env_never_carried(monkeypatch):
    """Port, connection limit, broadcast count and whitelist were reachable
    only by setting the environment before anything imported cync_lan.const.
    They now travel with everything else."""
    g = _fresh_env(
        monkeypatch,
        CYNC_PORT="24001",
        CYNC_MAX_TCP_CONN="16",
        CYNC_CMD_BROADCASTS="3",
        CYNC_TCP_WHITELIST="10.0.0.1, 10.0.0.2 ,",
    )
    assert g.env.cync_srv_port == 24001
    assert g.env.max_tcp_conn == 16
    assert g.env.cmd_broadcasts == 3
    # Split, stripped, and empty entries dropped - a trailing comma is the
    # obvious way to write one of these by hand.
    assert g.env.tcp_whitelist == ["10.0.0.1", "10.0.0.2"]


def test_defaults_match_what_const_has_always_used(monkeypatch):
    """A consumer that sets nothing must be entirely unaffected."""
    from cync_lan import const

    g = _fresh_env(monkeypatch)
    assert g.env.cync_srv_port == const.CYNC_SRV_PORT
    assert g.env.max_tcp_conn == const.CYNC_MAX_TCP_CONN
    assert g.env.cmd_broadcasts == const.CYNC_CMD_BROADCASTS
    assert g.env.tcp_whitelist is None


def test_a_typo_falls_back_rather_than_taking_the_server_down(monkeypatch):
    """const.py guards each of these individually for the same reason: an
    unparseable value used to be able to raise at import, which is the worst
    possible moment."""
    g = _fresh_env(
        monkeypatch,
        CYNC_PORT="twenty-four-thousand",
        CYNC_MAX_TCP_CONN="",
        CYNC_CMD_BROADCASTS="three",
    )
    assert g.env.cync_srv_port == 23779
    assert g.env.max_tcp_conn == 8
    assert g.env.cmd_broadcasts == 2


def test_reload_env_no_longer_invents_module_globals():
    """The defect itself. These names were assigned under `global` in
    structs.py, which does not import them, so each assignment created a new
    module-level name there - read by nothing, and not the one consumers had
    imported from const."""
    GlobalObject().reload_env()
    for orphan in (
        "CYNC_SRV_HOST",
        "CYNC_MQTT_HOST",
        "CYNC_SSL_CERT",
        "CYNC_BASE_DIR",
        "PERSISTENT_DIR",
    ):
        assert not hasattr(structs_module, orphan), (
            f"structs.{orphan} is back - reload_env is inventing globals again"
        )


def test_the_self_referencing_defaults_still_resolve(monkeypatch):
    """Two settings default to the value of another: the export host to the
    server host, and the certificate paths to the base directory. Those read
    the just-assigned global before, so removing the globals had to route
    them through self.env instead."""
    g = _fresh_env(monkeypatch, CYNC_BASE_DIR="/opt/cync", CYNC_SRV_HOST="10.1.2.3")
    assert g.env.cync_export_host == "10.1.2.3"
    assert g.env.cync_srv_ssl_cert == "/opt/cync/certs/cert.pem"
    assert g.env.cync_srv_ssl_key == "/opt/cync/certs/key.pem"


def test_the_server_reads_its_port_after_refreshing_not_before(monkeypatch):
    """The ordering bug, in the four lines it lived in: cert and key came
    from g.env and were current, host and port came from frozen constants,
    and reload_env() was called after they had been read."""
    from cync_lan.server import nCyncServer

    monkeypatch.setenv("CYNC_PORT", "24500")
    monkeypatch.setenv("CYNC_SRV_HOST", "127.0.0.5")
    nCyncServer._instance = None
    try:
        server = nCyncServer({})
        assert server.port == 24500, (
            "the server took its port from a constant frozen at import"
        )
        assert server.host == "127.0.0.5"
    finally:
        nCyncServer._instance = None
