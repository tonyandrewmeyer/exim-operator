# Copyright 2023 Tony Meyer
# See LICENSE file for licensing details.
#
# Learn more about testing at: https://juju.is/docs/sdk/testing

import unittest

import ops
import ops.testing
from charm import EximCharm


class TestCharm(unittest.TestCase):
    def setUp(self):
        self.harness = ops.testing.Harness(EximCharm)
        self.addCleanup(self.harness.cleanup)
        self.harness.begin()

    def test_pebble_ready(self):
        # Simulate the container coming up and emission of pebble-ready event
        self.harness.container_pebble_ready("exim")
        # Ensure we set an ActiveStatus with no message
        self.assertEqual(self.harness.model.unit.status, ops.ActiveStatus())
