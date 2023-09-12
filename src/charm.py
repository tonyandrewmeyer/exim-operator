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
        self.framework.observe(self.on.force_queue_action, self._on_force_queue_action)

    def _on_pebble_ready(self, event: ops.PebbleReadyEvent):
        """Handle pebble-ready event."""
        container = event.workload
        # Add initial Pebble config layer using the Pebble API
        container.add_layer("exim", self._pebble_layer, combine=True)
        # Make Pebble reevaluate its plan, ensuring any services are started if enabled.
        container.replan()
        self.unit.set_workload_version(self.version)
        self.unit.status = ops.ActiveStatus()

    @property
    def _pebble_layer(self) -> ops.pebble.Layer:
        """Return a dictionary representing a Pebble layer."""
        command = " ".join(
            [
                "/usr/sbin/exim",
                "-bd",
                "-q 30m",
            ]
        )
        pebble_layer: ops.pebble.LayerDict = {
            "summary": "Exim MTA service",
            "description": "pebble config layer for Exim MTA server",
            "services": {
                self.pebble_service_name: {
                    "override": "replace",
                    "summary": "exim MTA",
                    "command": command,
                    "startup": "enabled",
                }
            },
        }
        return ops.pebble.Layer(pebble_layer)

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
        # XXX This could handle errors (e.g. if Exim is missing) more gracefully.
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
        frozen = event.params["frozen"]  # see actions.yaml
        exim = self.container.exec(["/usr/sbin/exim", "-bpc"])
        # XXX There could be some error handling here.
        count = int(exim.wait_output()[0].strip())
        cmd = ["/usr/sbin/exim", "-qff" if frozen else "-qf"]
        # There is no output expected from this - the queue run takes place
        # in the background.
        self.container.exec(cmd)
        # XXX The way this handles the 'item/items' plural is not well suited
        # XXX to i81n.
        event.set_results(
            {"result": f"Queue run initiated, {count} item{'' if count == 1 else 's'} in queue"}
        )


if __name__ == "__main__":  # pragma: nocover
    ops.main(EximCharm)  # type: ignore
