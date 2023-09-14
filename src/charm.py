#!/usr/bin/env python3
# Copyright 2023 Tony Meyer
# See LICENSE file for licensing details.

"""Charm the application."""

import contextlib
import json
import logging
import re
import typing

import MySQLdb
import ops
from charms.data_platform_libs.v0.data_interfaces import (
    DatabaseCreatedEvent,
    DatabaseRequires,
)
from charms.loki_k8s.v0.loki_push_api import LogProxyConsumer
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

logger = logging.getLogger(__name__)

PEER_NAME = "exim-peer"


class EximCharm(ops.CharmBase):
    """Charm the application."""

    def __init__(self, *args):
        super().__init__(*args)
        self.pebble_service_name = "exim-service"
        self.container = self.unit.get_container("exim")
        self.framework.observe(self.on["exim"].pebble_ready, self._on_pebble_ready)
        self.framework.observe(self.on.config_changed, self._on_config_changed)
        self.framework.observe(self.on.force_queue_action, self._on_force_queue_action)
        self.database = DatabaseRequires(self, relation_name="database", database_name="exim")
        self.framework.observe(self.database.on.database_created, self._on_database_created)
        self.framework.observe(self.database.on.endpoints_changed, self._on_database_created)
        self.framework.observe(self.on.database_relation_broken, self._on_database_relation_removed)
        self._logging = LogProxyConsumer(
            self,
            relation_name="log-proxy",
            log_files=[
                "/var/log/exim4/mainlog",
                "/var/log/exim4/rejectlog",
                "/var/log/exim4/paniclog",
            ],
        )

    # XXX To-do: unit.open_port

    def _on_pebble_ready(self, event: ops.PebbleReadyEvent) -> None:
        """Handle pebble-ready event."""
        self._update_layer_and_restart(event)
        self.unit.status = ops.MaintenanceStatus("Getting Exim version")
        self.unit.set_workload_version(self.version)
        self.unit.status = ops.ActiveStatus()

    @property
    def _pebble_layer(self) -> ops.pebble.Layer:
        """Return a dictionary representing a Pebble layer."""
        # A 30-minute queue length is the typical default value. It's
        # not ideal if there's a lot of traffic - but while this could be
        # exposed as a configuration option, you need to have a lot of
        # knowledge of the system in order to choose sensible values
        # (and in particular understand the relationship between the
        # retry configuration and the queue interval) so it's better
        # to just stick with a value we choose.
        command = "/usr/sbin/exim -bd -q 30m"
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
                        "Invalid hostname list. Please provide a comma-separated list of hostnames"
                    )
                    return
        else:
            # It's fine to not have any others.
            other_hostnames = []
        logger.debug("Specified list of additional hostnames: %s", other_hostnames)
        primary_hostname = self.config["primary-hostname"]
        if not hostname_check.match(primary_hostname):
            self.unit.status = ops.BlockedStatus(
                "Invalid primary hostname. This should be a domain name."
            )
            return
        self._check_submission_config([primary_hostname] + other_hostnames)
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
            "MAILDIR_HOME_MAILDIR_LOCATION=/mail\n"
            # We should leave off this line if primary_hostname is not set,
            # so that it's just the default.
            f"MAIN_HARDCODE_PRIMARY_HOSTNAME={primary_hostname}\n",
        )
        # XXX This should actually get integrated into the configuration.
        # XXX It definitely should not be logged raw like this, since it
        # XXX contains the credentials.
        logger.info("Database bits: %s", self._fetch_mysql_relation_data())
        # XXX This is actually failing right at the moment. Seems like maybe
        # XXX it is an issue with the container rather than pebble or anything
        # XXX else (if I run the command manually on the workload container
        # XXX it fails - it's failing to have getpwdnam show that the mail
        # XXX user exists, even though it's in /etc/passwd, which is probably
        # XXX some sort of permission issue?)
        update_conf = self.container.exec(["/usr/sbin/update-exim4.conf"])
        out, err = update_conf.wait_output()
        logger.debug("update-exim4-conf said: {out!r} {err!r}")
        # Restart the service so that it picks up the configuration changes that
        # are not loaded on each connection.
        # XXX Most of the configuration values probably could be changed in the
        # XXX Exim conf to pull from MySQL instead, and then no restart would
        # XXX actually be required.
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
        # XXX to i18n.
        event.set_results(
            {"result": f"Queue run initiated, {count} item{'' if count == 1 else 's'} in queue"}
        )

    @property
    def peers(self) -> ops.Relation | None:
        """Fetch the peer relation."""
        return self.model.get_relation(PEER_NAME)

    def set_peer_data(self, key: str, data: typing.Any) -> None:
        """Put information into the peer data bucket instead of `StoredState`."""
        if not self.peers:
            logger.error("Unable to set peer data.")
            return
        self.peers.data[self.app][key] = json.dumps(data)

    def get_peer_data(self, key: str) -> dict[str, typing.Any]:
        """Retrieve information from the peer data bucket instead of `StoredState`."""
        if not self.peers:
            return {}
        data = self.peers.data[self.app].get(key, "")
        return json.loads(data) if data else {}

    def _check_submission_config(self, domains):
        """Check that Exim is properly configured to handle sending from the specified domains."""
        public_keys = self.get_peer_data("dkim-public-keys")
        for domain in domains:
            if domain not in public_keys:
                logger.info(f"Generating DKIM certificate for {domain}")
                public_keys[domain] = self._generate_dkim_certificate(domain)
        # XXX Is this the best way to provide this? The user needs to get this
        # XXX information to store the public key in DNS (assuming that we are
        # XXX not integrated with the domain's DNS provider, so cannot just do
        # XXX it ourselves). Iiuc, peer data is really more meant for use by
        # XXX other parts of the model, rather than the user.
        # XXX It's nice to be persisted, although as long as the private key is
        # XXX persisted, then we can always just regenerate the public key from
        # XXX that, of course.
        # XXX Maybe it should instead be an action? get-dkim-public-keys?
        # XXX (Obviously, it's not secret data since it's only the public key
        # XXX and it's going to be put in public DNS anyway).
        self.set_peer_data("dkim-public-keys", public_keys)

    def _generate_dkim_certificate(self, domain: str) -> str:
        """Generate an appropriate certificate to use with DKIM.

        Returns the public key in PEM format.
        """
        if not self.peers:
            # XXX Tests don't know about this yet.
            return ""

        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        private_key_pem = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
        public_key = private_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        self._store_dkim_key(domain, private_key_pem.decode())
        # XXX To-do: adjust the Exim configuration to actually use this.
        # XXX (should probably forget about using update-exim4.conf and just
        # XXX have the config as a template in the charm).
        # XXX we also need to make sure that if we no longer require a key
        # XXX we get rid of it (and Exim knows that).
        # XXX Probably we want to be tracking the domains we handle in the DB
        # XXX and link it up that way.
        private_key_pem.decode()
        logger.debug("Generated DKIM key pair for %s: %r", domain, public_key.decode())
        return public_key.decode()

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
                # XXX I should actually check that in the source or asking
                # XXX someone in the team.
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
                c.execute()
                db.commit()

    def _store_dkim_key(self, domain: str, private_key: str) -> None:
        """Store the DKIM private key in the DB for Exim to access."""
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
