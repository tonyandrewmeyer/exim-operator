# Copyright 2023 Tony Meyer
# See LICENSE file for licensing details.
#
# Learn more about testing at: https://juju.is/docs/sdk/testing

import ops
import ops.testing
import pytest
from charm import EximCharm


@pytest.fixture
def harness() -> ops.testing.Harness:
    """Create a generic ops.testing.Harness, with begun() called."""
    harness = ops.testing.Harness(EximCharm)
    harness.begin()
    yield harness
    harness.cleanup()


def test_pebble_ready(harness: ops.testing.Harness) -> None:
    # Simulate the container coming up and emission of pebble-ready event
    harness.container_pebble_ready("exim")
    # Ensure we set an ActiveStatus with no message
    assert harness.model.unit.status == ops.ActiveStatus()


def test_get_version(harness: ops.testing.Harness) -> None:
    """Verify that getting the version works correctly.

    Assumes that Pebble will be able to successfully execute the exim
    command to get the version.
    """
    raw_version = "28.8 #2006"
    mock_version = raw_version.replace(" #", "-debian-")
    exim_output = f"""Exim version {raw_version} built 24-Aug-2022 16:23:44
Copyright (c) University of Cambridge, 1995 - 2018
(c) The Exim Maintainers and contributors in ACKNOWLEDGMENTS file, 2007 - 2018
Berkeley DB: Berkeley DB 5.3.28: (September  9, 2013)
Support for: crypteq iconv() IPv6 GnuTLS move_frozen_messages Event TCP_Fast_Open
Lookups (built-in): lsearch wildlsearch nwildlsearch iplsearch nis nis0 passwd
Authenticators: cram_md5 plaintext
Routers: accept dnslookup ipliteral manualroute queryprogram redirect
Transports: appendfile/maildir/mailstore autoreply lmtp pipe smtp
Fixed never_users: 0
Configure owner: 0:0
Size of off_t: 8
Configuration file search path is /etc/exim4/exim4.conf:/var/lib/exim4/config.autogenerated
Configuration file is /var/lib/exim4/config.autogenerated
"""
    harness.set_can_connect("exim", True)
    harness.handle_exec("exim", ["/usr/sbin/exim"], result=exim_output)
    assert harness.charm._request_version() == mock_version


def test_get_version_not_found(harness: ops.testing.Harness) -> None:
    """Verify that a version is set when it can't be taken from Exim."""
    exim_output = "No version string to be found."
    harness.set_can_connect("exim", True)
    harness.handle_exec("exim", ["/usr/sbin/exim"], result=exim_output)
    assert harness.charm._request_version() == "unknown"


def test_get_version_exim_failed(harness: ops.testing.Harness) -> None:
    """Verify that a version is set when running Exim fails."""
    harness.set_can_connect("exim", True)
    harness.handle_exec(
        "exim",
        ["/usr/sbin/exim"],
        result=ops.testing.ExecResult(exit_code=1, stderr="No such file or directory"),
    )
    assert harness.charm._request_version() == "unknown"


def test_workload_version(harness: ops.testing.Harness) -> None:
    """Verify that the workload version is set correctly."""
    raw_version = "28.8 #2006"
    mock_version = raw_version.replace(" #", "-debian-")
    exim_output = f"""Exim version {raw_version} built 24-Aug-2022 16:23:44
Copyright (c) University of Cambridge, 1995 - 2018
(c) The Exim Maintainers and contributors in ACKNOWLEDGMENTS file, 2007 - 2018
Berkeley DB: Berkeley DB 5.3.28: (September  9, 2013)
Support for: crypteq iconv() IPv6 GnuTLS move_frozen_messages Event TCP_Fast_Open
Lookups (built-in): lsearch wildlsearch nwildlsearch iplsearch nis nis0 passwd
Authenticators: cram_md5 plaintext
Routers: accept dnslookup ipliteral manualroute queryprogram redirect
Transports: appendfile/maildir/mailstore autoreply lmtp pipe smtp
Fixed never_users: 0
Configure owner: 0:0
Size of off_t: 8
Configuration file search path is /etc/exim4/exim4.conf:/var/lib/exim4/config.autogenerated
Configuration file is /var/lib/exim4/config.autogenerated
"""
    harness.handle_exec("exim", ["/usr/sbin/exim"], result=exim_output)
    harness.container_pebble_ready("exim")
    assert harness.get_workload_version() == mock_version


def test_pebble_layer(harness: ops.testing.Harness) -> None:
    """Validate the Pebble layer used for Exim."""
    # From a functional point of view, it's not really important what the
    # summary or description are, but there should be one.
    layer = harness.charm._pebble_layer.to_dict()
    assert layer.get("summary"), "The layer must have a summary"
    assert layer.get("description"), "The layer should have a description"
    # It is important that the service is correctly defined.
    expected_services = {
        "exim-service": {
            "override": "replace",
            "summary": "Exim MTA",
            "command": "/usr/sbin/exim -bd -q 30m",
            "startup": "enabled",
        },
    }
    assert layer["services"] == expected_services


def test_configuration_change(harness: ops.testing.Harness) -> None:
    """Check that changing a configuration value works correctly."""
    # Set an initial primary hostname.
    harness.disable_hooks()
    harness.update_config({"primary-hostname": "test.invalid"})
    harness.enable_hooks()
    harness.set_can_connect("exim", True)
    fs = harness.get_filesystem_root("exim")
    (fs / "etc" / "exim4").mkdir(parents=True)
    harness.handle_exec("exim", ["/usr/sbin/update-exim4.conf"], result="")
    other_hostnames = "example.com", "example.org"
    harness.update_config({"extra-hostnames": ",".join(other_hostnames)})
    expected = f"""dc_localdelivery='maildir_home'
dc_eximconfig_configtype='internet'
dc_other_hostnames='{":".join(other_hostnames)}'
dc_local_interfaces='0.0.0.0 ; ::0'
dc_readhost=''
dc_relay_domains=''
dc_minimaldns='false'
dc_relay_nets='0.0.0.0/0'
dc_smarthost=''
CFILEMODE='644'
dc_use_split_config='false'
dc_hide_mailname='true'
dc_mailname_in_oh='true'
MAILDIR_HOME_MAILDIR_LOCATION=/mail
MAIN_HARDCODE_PRIMARY_HOSTNAME={harness.charm.config["primary-hostname"]}
"""
    assert (fs / "etc" / "exim4" / "update-exim4.conf.conf").read_text("ascii") == expected


def test_invalid_configuration(harness: ops.testing.Harness) -> None:
    """Check that we are prevented from setting an invalid hostname configuration value."""
    harness.set_can_connect("exim", True)
    harness.update_config({"extra-hostnames": "example.com,,example.org"})
    assert isinstance(harness.model.unit.status, ops.BlockedStatus)
    harness.update_config({"extra-hostnames": "not_a_domain_name.com"})
    assert isinstance(harness.model.unit.status, ops.BlockedStatus)
