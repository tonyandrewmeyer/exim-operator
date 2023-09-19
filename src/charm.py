#!/usr/bin/env python3
# Copyright 2023 Tony Meyer
# See LICENSE file for licensing details.

"""Charm the application."""

import contextlib
import logging
import re
import subprocess
import os

import ops
from charms.data_platform_libs.v0.data_interfaces import (
    DatabaseCreatedEvent,
    DatabaseRequires,
)
from charms.loki_k8s.v0.loki_push_api import LogProxyConsumer
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

logger = logging.getLogger(__name__)


class EximCharm(ops.CharmBase):
    """Charm the application."""

    def __init__(self, *args):
        super().__init__(*args)
        self.pebble_exim_service_name = "exim-service"
        self.pebble_cron_service_name = "cron-service"
        self.container = self.unit.get_container("exim")
        self.framework.observe(self.on["exim"].pebble_ready, self._on_pebble_ready)
        self.framework.observe(self.on.install, self._on_install)
        self.framework.observe(self.on.config_changed, self._on_config_changed)
        self.framework.observe(self.on.force_queue_action, self._on_force_queue_action)
        self.framework.observe(self.on.get_dkim_keys_action, self._on_get_dkim_keys_action)
        self.database = DatabaseRequires(self, relation_name="database", database_name="exim")
        self.framework.observe(self.database.on.database_created, self._on_database_created)
        self.framework.observe(self.database.on.endpoints_changed, self._on_database_created)
        self.framework.observe(
            self.on.database_relation_broken, self._on_database_relation_removed
        )
        self._logging = LogProxyConsumer(
            self,
            relation_name="log-proxy",
            log_files=[
                "/var/log/exim4/mainlog",
                "/var/log/exim4/rejectlog",
                "/var/log/exim4/paniclog",
            ],
        )

    def _on_install(self, event) -> None:
        """Handle the install event."""
        logger.info("Opening smtp and submission ports for listening.")
        # Provide the SMTP interface (after 'expose' is run).
        self.unit.open_port("tcp", 22)
        # Also provide the submission interface, for outgoing mail.
        self.unit.open_port("tcp", 587)
        logger.info("Installing libmysqlclient with apt")
        subprocess.call("/usr/bin/apt update")
        subprocess.call("/usr/bin/apt install libmysqlclient21")

    def _on_pebble_ready(self, event: ops.PebbleReadyEvent) -> None:
        """Handle pebble-ready event."""
        # Ensure that we have a crontab for cron to run.
        # If we made the queue interval configurable we could put this in
        # the config changed handler, but since we're being opinionated,
        # we can just ensure that it exists here.
        # A 30-minute queue length is the typical default value. It's
        # not ideal if there's a lot of traffic - but while this could be
        # exposed as a configuration option, you need to have a lot of
        # knowledge of the system in order to choose sensible values
        # (and in particular understand the relationship between the
        # retry configuration and the queue interval) so it's better
        # to just stick with a value we choose.
        self.container.push("/etc/crontab", "*/30 * * * * /usr/sbin/exim -q")
        self._update_layer_and_restart(event)
        logger.debug("Setting Exim version")
        self.unit.set_workload_version(self.version)

    @property
    def _pebble_layer(self) -> ops.pebble.Layer:
        """Return a dictionary representing a Pebble layer for Exim."""
        return ops.pebble.Layer(
            {
                "summary": "Exim MTA service",
                "description": "Pebble config layer for Exim MTA server",
                "services": {
                    self.pebble_exim_service_name: {
                        "override": "replace",
                        "summary": "Exim MTA",
                        "command": "/usr/sbin/exim -bdf",
                        "startup": "enabled",
                    },
                    # Use cron to periodically do an Exim queue run.
                    # The "-q" option will do this when given a time (e.g. "-q 30m")
                    # but will then daemonise, which we do not want, because we want
                    # to have Pebble managing what services are running.
                    # We use the standard cron service to handle this, since it's
                    # already available in the container image.
                    self.pebble_cron_service_name: {
                        "override": "replace",
                        "summary": "cron",
                        "command": "/usr/sbin/cron -f",
                        "startup": "enabled",
                    },
                },
            }
        )

    def _on_config_changed(self, event) -> None:
        """Handle configuration changes."""
        other_hostnames = self.config["extra-hostnames"]
        hostname_check = re.compile(r"^[a-z0-9\.-]+$", re.IGNORECASE)
        # We don't strictly validate that these are valid hostnames
        # but we do a basic check that it's something similar, and that
        # there isn't a blank hostname or one that would break the Exim list.
        if other_hostnames:
            other_hostnames = other_hostnames.split(",")
            for hostname in other_hostnames:
                if not hostname_check.match(hostname):
                    self.unit.status = ops.BlockedStatus(
                        "extra-hostnames not comma-separated hostname list"
                    )
                    return
        else:
            # It's fine to not have any others.
            other_hostnames = []
        logger.debug("Specified list of additional hostnames: %s", other_hostnames)
        primary_hostname = self.config["primary-hostname"]
        if primary_hostname and not hostname_check.match(primary_hostname):
            self.unit.status = ops.BlockedStatus("primary-hostname must be a host name")
            return
        self._check_submission_config(
            ([primary_hostname] + other_hostnames) if primary_hostname else other_hostnames
        )
        with open(os.path.join("src", "exim.config")) as conff:
            # options = {}
            # conf = conff.read().format(options)
            conf = conff.read()
            # Put the config in the same place as Debian's tools do for convenience.
            self.container.push("/var/lib/exim4/config.autogenerated", conf)
        # XXX This should actually get integrated into the configuration.
        # XXX It definitely should not be logged raw like this, since it
        # XXX contains the credentials.
        logger.info("Database bits: %s", self._fetch_mysql_relation_data())
        # Restart the service so that it picks up the configuration changes that
        # are not loaded on each connection.
        self._update_layer_and_restart(None)

    def _update_layer_and_restart(self, event) -> None:
        """Define and start a workload using the Pebble API."""
        self.unit.status = ops.MaintenanceStatus("Assembling pod spec")
        if not self.container.can_connect():
            self.unit.status = ops.WaitingStatus("Waiting for Pebble in workload container")
            return
        new_layer = self._pebble_layer.to_dict()
        services = self.container.get_plan().to_dict().get("services", {})
        if services != new_layer.get("services"):
            self.container.add_layer("exim", self._pebble_layer, combine=True)
            logger.info("Added updated layer 'exim' to Pebble plan")
            for service_name in services:
                self.container.restart(service_name)
                logger.info(f"Restarted '{service_name}' service")
        self.unit.status = ops.ActiveStatus()

    @property
    def version(self) -> str:
        """Reports the current workload (Exim) version."""
        if self.container.can_connect() and self.container.get_services(
            self.pebble_exim_service_name
        ):
            try:
                return self._request_version()
                # Catching Exception is not ideal, but we don't care much for the error here.
            except Exception as e:
                logger.warning("unable to get version from Exim: %s", str(e), exc_info=True)
        return "unknown"

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
        # so adjust it slightly. (`--format=yaml`` will show all of the version).
        # See https://github.com/juju/juju/blob/13afc56eb85cdfc7953610dd900dd4d62f193841/cmd/juju/status/output_tabular.go#L158  # noqa: E501,W505
        return mo.groups()[0].replace(" #", "-debian-") if mo else "unknown"

    def _on_force_queue_action(self, event) -> None:
        """Force an Exim queue run.

        If the 'frozen' parameter is set, then tell Exim to also retry
        delivery for all of the frozen messages.
        """
        frozen = event.params["frozen"]
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
        # XXX The way this handles the 'item/items' plural is not well suited to i18n.
        event.set_results(
            {"result": f"Queue run initiated, {count} item{'' if count == 1 else 's'} in queue"}
        )

    def _on_get_dkim_keys_action(self, event):
        """Provide the user with the DKIM public keys that should be
        added to the domain's DNS as TXT records.

        By default, returns keys for all domains, but can be optionally
        limited to a specific domain."""
        # This has to be a late import because importing will fail until after
        # the `install` hook has run (which installs the dynamic libraries that
        # are required).
        import MySQLdb

        domain = event.params["domain"]
        keys = {}
        query = "SELECT domain, key FROM dkim"
        args = []
        if domain:
            query += " WHERE domain=%s"
            args.append(domain)
        connection_args = self._fetch_mysql_relation_data()
        db = MySQLdb.connect(
            host=connection_args["db_host"],
            port=connection_args["db_port"],
            user=connection_args["db_username"],
            passwd=connection_args["db_password"],
        )
        with contextlib.closing(db):
            with db.cursor() as c:
                c.execute(query, args)
                for domain, private_key in c.fetchall():
                    public_key = private_key.public_key().public_bytes(
                        encoding=serialization.Encoding.PEM,
                        format=serialization.PublicFormat.SubjectPublicKeyInfo,
                    )
                    keys[domain] = public_key
        event.set_results(keys)

    def _check_submission_config(self, domains):
        """Check that Exim is properly configured to handle sending from the specified domains."""
        # This has to be a late import because importing will fail until after
        # the `install` hook has run (which installs the dynamic libraries that
        # are required).
        import MySQLdb

        connection_args = self._fetch_mysql_relation_data()
        db = MySQLdb.connect(
            host=connection_args["db_host"],
            port=connection_args["db_port"],
            user=connection_args["db_username"],
            passwd=connection_args["db_password"],
        )
        with contextlib.closing(db):
            with db.cursor() as c:
                for domain in domains:
                    c.execute("SELECT 1 FROM dkim WHERE domain=%s", (domain,))
                    if not c.fetchone():
                        logger.info(f"Generating DKIM certificate for {domain}")
                        self._generate_dkim_certificate(domain)

    def _generate_dkim_certificate(self, domain: str) -> None:
        """Generate an appropriate certificate to use with DKIM.

        Returns the public key in PEM format.
        """
        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        private_key_pem = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
        # XXX To-do: adjust the Exim configuration to actually use this.
        # XXX (should probably forget about using update-exim4.conf and just
        # XXX have the config as a template in the charm).
        # XXX we also need to make sure that if we no longer require a key
        # XXX we get rid of it (and Exim knows that).
        # XXX Probably we want to be tracking the domains we handle in the DB
        # XXX and link it up that way.
        private_key_pem.decode()
        logger.debug("Generated DKIM key pair for %s", domain)

    def _fetch_mysql_relation_data(self) -> dict:
        """Fetch MySQL relation data.

        This method retrieves relation data from a MySQL database.
        Any non-empty retrieved data is processed to extract endpoint
        information, username, and password. This processed data is then
        returned as a dictionary. If no data is retrieved, the unit is
        set to waiting status and the program exits with a zero status code.
        """
        relations = self.database.fetch_relation_data()
        for data in relations.values():
            if not data:
                continue
            logger.info("New MySQL database endpoint is %s", data["endpoints"])
            host, port = data["endpoints"].split(":")
            db_data = {
                "db_host": host,
                "db_port": port,
                "db_username": data["username"],
                # Ideally, this would be a Juju secret, but it seems like
                # the charms.data_platform_libs.v0.data_interfaces integration
                # does not yet offer this functionality.
                # XXX There's an open PR for adjusting this.
                # XXX https://github.com/canonical/data-platform-libs/pull/94
                "db_password": data["password"],
            }
            return db_data
        self.unit.status = ops.WaitingStatus("Waiting for database relation")

    def _on_database_created(self, event: DatabaseCreatedEvent) -> None:
        """Event is fired when MySQL database is created."""
        self._ensure_tables()
        self._update_layer_and_restart(None)

    def _on_database_relation_removed(self, event) -> None:
        """Event is fired when relation with MySQL is broken."""
        self.unit.status = ops.WaitingStatus("Waiting for database relation")

    def _ensure_tables(self) -> None:
        """Make sure that the required MySQL tables exist."""
        # This has to be a late import because importing will fail until after
        # the `install` hook has run (which installs the dynamic libraries that
        # are required).
        import MySQLdb

        connection_args = self._fetch_mysql_relation_data()
        db = MySQLdb.connect(
            host=connection_args["db_host"],
            port=connection_args["db_port"],
            user=connection_args["db_username"],
            passwd=connection_args["db_password"],
        )
        with contextlib.closing(db):
            with db.cursor() as c:
                with open(os.path.join("src", "schema", "dkim.sql")) as schemaf:
                    c.execute(schemaf.read())
                db.commit()

    def _store_dkim_key(self, domain: str, private_key: str) -> None:
        """Store the DKIM private key in the DB for Exim to access."""
        # This has to be a late import because importing will fail until after
        # the `install` hook has run (which installs the dynamic libraries that
        # are required).
        import MySQLdb

        # XXX There's a lot of boilerplate here - should probably have
        # XXX some sort of 'get DB cursor' method.
        connection_args = self._fetch_mysql_relation_data()
        db = MySQLdb.connect(
            host=connection_args["db_host"],
            port=connection_args["db_port"],
            user=connection_args["db_username"],
            passwd=connection_args["db_password"],
        )
        with contextlib.closing(db):
            with db.cursor() as c:
                # For simplicity, just run the create table with
                # 'if not exists', rather than checking first.
                c.execute("INSERT INTO dkim (domain, key) VALUES (%s, %s)", (domain, private_key))
                db.commit()


if __name__ == "__main__":  # pragma: nocover
    ops.main(EximCharm)  # type: ignore
