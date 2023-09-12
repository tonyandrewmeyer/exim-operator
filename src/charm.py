#!/usr/bin/env python3
# Copyright 2023 Tony Meyer
# See LICENSE file for licensing details.

"""Charm the application."""

import logging
import re

import ops

logger = logging.getLogger(__name__)


class EximCharm(ops.CharmBase):
    """Charm the application."""

    def __init__(self, *args):
        super().__init__(*args)
        self.pebble_service_name = "exim-service"
        self.container = self.unit.get_container("exim")
        self.framework.observe(self.on["exim"].pebble_ready, self._on_pebble_ready)
        self.framework.observe(self.on.config_changed, self._on_config_changed)
        self.framework.observe(self.on.force_queue_action, self._on_force_queue_action)

    def _on_pebble_ready(self, event: ops.PebbleReadyEvent) -> None:
        """Handle pebble-ready event."""
        self._update_layer_and_restart(event)
        self.unit.status = ops.MaintenanceStatus("Getting Exim version")
        self.unit.set_workload_version(self.version)
        self.unit.status = ops.ActiveStatus()

    @property
    def _pebble_layer(self) -> ops.pebble.Layer:
        """Return a dictionary representing a Pebble layer."""
        command = " ".join(
            [
                "/usr/sbin/exim",
                "-bd",
                # A 30-minute queue length is the typical default value. It's
                # not ideal if there's a lot of traffic - but while this could be
                # exposed as a configuration option, you need to have a lot of
                # knowledge of the system in order to choose sensible values
                # (and in particular understand the relationship between the
                # retry configuration and the queue interval) so it's better
                # to just stick with a value we choose.
                "-q 30m",
            ]
        )
        pebble_layer: ops.pebble.LayerDict = {
            "summary": "Exim MTA service",
            "description": "Pebble config layer for Exim MTA server",
            "services": {
                self.pebble_service_name: {
                    "override": "replace",
                    "summary": "Exim MTA",
                    "command": command,
                    "startup": "enabled",
                }
            },
        }
        return ops.pebble.Layer(pebble_layer)

    def _on_config_changed(self, event) -> None:
        """Handle configuration changes."""
        config_type = self.config["config-type"]
        # There are more types than this, but for simplicity only handle
        # these at the moment.
        if config_type not in ("internet", "smarthost", "local"):
            self.unit.status = ops.BlockedStatus(
                "Invalid config type. Please use 'internet', 'smarthost', or 'local'"
            )
            return
        logger.debug("Set Debian config type to %s", config_type)
        other_hostnames = self.config["hostnames"]
        # We don't strictly validate that these are valid hostnames
        # but we do a basic check that it's something similar, and that
        # there isn't a blank hostname or one that would break the Exim list.
        if other_hostnames:
            other_hostnames = other_hostnames.split(",")
            for hostname in other_hostnames:
                if not re.match(r"[\w\.-]+", hostname):
                    self.unit.status = ops.BlockedStatus(
                        "Invalid hostname list. Please provide a comma-separated list of hostnames"
                    )
                    return
        else:
            # It's fine to not have any others.
            other_hostnames = []
        logger.debug("Specified list of additional hostnames: %s", other_hostnames)
        # We use Debian's update-exim4.conf utility to make the changes rather
        # than writing the configuration file ourselves (or changing the configuration
        # files to load these all from a database, which isn't always possible anyway).
        self.container.push(
            "/etc/exim4/update-exim4.conf.conf",
            source="dc_localdelivery='maildir_home'\n"
            f"dc_eximconfig_configtype='{config_type}'\n"
            f"dc_other_hostnames='{':'.join(other_hostnames)}'\n"
            # We don't change these, but need to provide them.
            "dc_local_interfaces='0.0.0.0 ; ::0'\n"
            "dc_readhost=''\n"
            "dc_relay_domains=''\n"
            "dc_minimaldns='false'\n"
            "dc_relay_nets='0.0.0.0/0'\n"
            "dc_smarthost=''\n"
            "CFILEMODE='644'\n"
            "dc_use_split_config='false'\n"
            "dc_hide_mailname='true'\n"
            "dc_mailname_in_oh='true'\n"
            "MAILDIR_HOME_MAILDIR_LOCATION=/mail\n",
        )
        update_conf = self.container.exec(["/usr/sbin/update-exim4.conf"])
        out, err = update_conf.wait_output()
        logger.debug("update-exim4-conf said: {out!r} {err!r}")
        # Restart the service so that it picks up the configuration changes that
        # are not loaded on each connection.
        self._update_layer_and_restart(None)

    def _update_layer_and_restart(self, event) -> None:
        """Define and start a workload using the Pebble API."""
        self.unit.status = ops.MaintenanceStatus("Assembling pod spec")
        if self.container.can_connect():
            new_layer = self._pebble_layer.to_dict()
            services = self.container.get_plan().to_dict().get("services", {})
            if services != new_layer.get("services"):
                self.container.add_layer("exim", self._pebble_layer, combine=True)
                logger.info("Added updated layer 'exim' to Pebble plan")
                self.container.restart(self.pebble_service_name)
                logger.info(f"Restarted '{self.pebble_service_name}' service")
            self.unit.status = ops.ActiveStatus()
        else:
            self.unit.status = ops.WaitingStatus("Waiting for Pebble in workload container")

    @property
    def version(self) -> str:
        """Reports the current workload (Exim) version."""
        if self.container.can_connect() and self.container.get_services(self.pebble_service_name):
            try:
                return self._request_version()
            # Catching Exception is not ideal, but we don't care much for the error here, and just
            # default to setting a blank version since there isn't much the admin can do!
            except Exception as e:
                logger.warning("unable to get version from Exim: %s", str(e))
                logger.exception(e)
        return ""

    def _request_version(self) -> str:
        """Fetch the version from the running workload using a subprocess."""
        try:
            exim = self.container.exec(["/usr/sbin/exim", "-bV"])
            out = exim.wait_output()[0]
        except ops.pebble.ExecError as e:
            logger.warning("Unable to get Exim version: %s", e, exc_info=True)
            return "unknown"
        mo = re.match(r".*?(\d+\.\d+ #\d+).+", out)
        # `juju status` won't show a version that has a space or a '#' (or '+'),
        # so adjust it slightly.
        return mo.groups()[0].replace(" #", "-debian-") if mo else "unknown"

    def _on_force_queue_action(self, event) -> None:
        """Force an Exim queue run.

        If the 'frozen' parameter is set, then tell Exim to also retry
        delivery for all of the frozen messages.
        """
        frozen = event.params["frozen"]  # see actions.yaml
        try:
            exim = self.container.exec(["/usr/sbin/exim", "-bpc"])
            count = int(exim.wait_output()[0].strip())
        except ops.pebble.ExecError as e:
            logger.warning("Unable to get current queue size: %s", e, exc_info=True)
            count = "unknown"
        cmd = ["/usr/sbin/exim", "-qff" if frozen else "-qf"]
        # There is no output expected from this - the queue run takes place
        # in the background.
        try:
            self.container.exec(cmd)
        except ops.pebble.ExecError as e:
            logger.exception("Unable to trigger forced queue run: %s", e)
            event.set_results(
                {
                    "result": "Queue run could not start, "
                    f"{count} item{'' if count == 1 else 's'} in queue"
                }
            )
        # XXX The way this handles the 'item/items' plural is not well suited
        # XXX to i81n.
        event.set_results(
            {"result": f"Queue run initiated, {count} item{'' if count == 1 else 's'} in queue"}
        )


if __name__ == "__main__":  # pragma: nocover
    ops.main(EximCharm)  # type: ignore
