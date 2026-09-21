"""The device tier: one tree, and what it says when the instrument disagrees with it."""

import logging
import unittest
from typing import Any, Dict, List, Optional

from pylabrobot.io.http import HTTPError
from pylabrobot.resources.coordinate import Coordinate
from pylabrobot.veon.iprep2.configuration_tests import CAPABILITIES
from pylabrobot.veon.iprep2.deck import IPrep2Deck
from pylabrobot.veon.iprep2.deck_tests import labware, zone_at
from pylabrobot.veon.iprep2.device import IPrep2, IPrep2Device
from pylabrobot.veon.iprep2.driver import IPrep2Driver
from pylabrobot.veon.iprep2.driver_tests import _FakeEvents, _FakeHTTP
from pylabrobot.veon.iprep2.errors import IPrep2Error

EMPTY_DECK: Dict[str, Any] = {zone: {} for zone in CAPABILITIES["deck"]["zones"]}
NO_CALIBRATION: Dict[str, Any] = {
  "zones": {zone: {"x": 0.0, "y": 0.0, "z": 0.0} for zone in EMPTY_DECK}
}


class _WarningCatcher(logging.Handler):
  """Keeps every record it is given, so a test can assert there were none."""

  def __init__(self) -> None:
    super().__init__(level=logging.WARNING)
    self.records: List[logging.LogRecord] = []

  def emit(self, record: logging.LogRecord) -> None:
    self.records.append(record)


class _DeviceTestCase(unittest.IsolatedAsyncioTestCase):
  """A device on transports that answer without an instrument, for every test here to build."""

  def _device(
    self, answers: Optional[Dict[str, Any]] = None, deck: Optional[IPrep2Deck] = None
  ) -> IPrep2Device:
    """A device whose instrument answers as given.

    The transports are kept on the test case as `http` and `events`, so a test can look at what
    they were asked and whether they were left open.

    Args:
      answers: extra or replacement path answers.
      deck: the deck to carry.

    Returns:
      The device, not yet set up.
    """
    self.http = _FakeHTTP(
      {"/deck/state": EMPTY_DECK, "/calibration/zones": NO_CALIBRATION, **(answers or {})}
    )
    self.events = _FakeEvents()
    device = IPrep2Device(deck=deck, driver=IPrep2Driver(io=self.http, events_io=self.events))
    self.addAsyncCleanup(device.stop)
    return device


class DeviceTests(_DeviceTestCase):
  """Bringing the device up."""

  async def test_the_deck_is_a_child_of_the_device(self) -> None:
    """One tree, rooted at the instrument, so everything on the deck is a descendant of the
    machine carrying it."""
    device = self._device()
    await device.setup()
    plate = labware()
    device.deck.assign_child_at_zone(plate, "Zone1")
    self.assertIs(plate.get_root(), device)
    self.assertIs(device.deck.parent, device)

  async def test_setup_reads_the_instrument_through_the_driver(self) -> None:
    device = self._device()
    await device.setup()
    self.assertEqual(device.num_channels, 8)
    self.assertEqual(device.identity.name, "chadley")

  async def test_setup_applies_the_instruments_calibration(self) -> None:
    """The definition says where a zone is drawn; the instrument says where it found it."""
    calibrated = {"zones": dict(NO_CALIBRATION["zones"], Zone1={"x": 2.0, "y": 0.0, "z": 0.0})}
    device = self._device({"/calibration/zones": calibrated})
    await device.setup()
    self.assertAlmostEqual(zone_at(device.deck, "Zone1").x, 76.933, places=6)

  async def test_a_calibration_that_cannot_be_read_closes_what_was_opened(self) -> None:
    """A deck placed against an unknown calibration is a deck placed wrong, so setup fails - and
    leaves nothing open behind it."""
    device = self._device({"/calibration/zones": HTTPError("GET", "/calibration/zones", 404, "{}")})
    with self.assertRaises(IPrep2Error):
      await device.setup()
    self.assertFalse(self.http.set_up)
    self.assertFalse(self.events.set_up)
    self.assertIsNone(device.driver._events_task)

  async def test_the_deck_keeps_the_origin_it_was_built_with(self) -> None:
    """Handing a deck to the device must not move it."""
    deck = IPrep2Deck(origin=Coordinate(10.0, 20.0, 0.0))
    IPrep2Device(deck=deck, host="iprep2.local")
    self.assertEqual(deck.location, Coordinate(10.0, 20.0, 0.0))

  async def test_a_device_round_trips_with_its_deck_and_labware(self) -> None:
    """The serialized deck replaces the default one rather than standing beside it, and the
    address comes back so the device can be set up again - the key does not."""
    device = IPrep2Device(host="iprep2.local", port=4242, api_key="secret-token-xyz", secure=True)
    device.deck.assign_child_at_zone(labware("source"), "Zone2")

    serialized = device.serialize()
    self.assertNotIn("secret-token-xyz", str(serialized))
    loaded = IPrep2Device.deserialize(serialized)

    self.assertEqual([child.name for child in loaded.children], ["deck"])
    self.assertIs(loaded.deck, loaded.children[0])
    held = loaded.deck.zones["Zone2"]
    assert held is not None
    self.assertEqual(held.name, "source")
    self.assertIs(held.get_root(), loaded)
    self.assertEqual((loaded.driver.host, loaded.driver.port), ("iprep2.local", 4242))
    self.assertEqual(loaded.get_absolute_size_x(), device.get_absolute_size_x())

  async def test_a_device_can_be_copied(self) -> None:
    device = IPrep2Device(host="iprep2.local")
    device.deck.assign_child_at_zone(labware(), "Zone1")
    self.assertIsNotNone(device.copy().deck.zones["Zone1"])

  async def test_the_factory_builds_one_on_the_standard_deck(self) -> None:
    device = IPrep2(host="iprep2.local")
    self.assertEqual(len(device.deck.zone_names), 6)
    self.assertEqual(device.model, "IPrep2")

  async def test_the_model_names_the_resource_not_the_instrument(self) -> None:
    """One names a kind of resource and the other names a machine; the instrument's own string
    stays on the identity, where a reader knows which they are getting."""
    device = self._device()
    await device.setup()
    self.assertEqual(device.model, "IPrep2Device")
    self.assertEqual(device.identity.model, "iprep2")


class DivergenceTests(_DeviceTestCase):
  """What it says when the instrument's idea of the deck is not this model's."""

  async def test_a_matching_deck_says_nothing(self) -> None:
    # `assertNoLogs` arrived in Python 3.10 and this package supports 3.9, so the absence of a
    # warning is checked by capturing and finding nothing.
    captured = _WarningCatcher()
    logging.getLogger("pylabrobot.veon.iprep2.device").addHandler(captured)
    try:
      await self._device().setup()
    finally:
      logging.getLogger("pylabrobot.veon.iprep2.device").removeHandler(captured)
    self.assertEqual(captured.records, [])

  async def test_labware_on_the_instrument_that_this_model_lacks_is_reported(self) -> None:
    """A zone the instrument believes is loaded and this model believes is empty will behave in
    ways neither explains."""
    loaded = dict(EMPTY_DECK, Zone2={"labwareId": "rack-96"})
    with self.assertLogs("pylabrobot.veon.iprep2.device", level="WARNING") as logs:
      await self._device({"/deck/state": loaded}).setup()
    self.assertIn("Zone2", "\n".join(logs.output))

  async def test_labware_in_this_model_that_the_instrument_lacks_is_reported(self) -> None:
    deck = IPrep2Deck()
    deck.assign_child_at_zone(labware("source"), "Zone3")
    with self.assertLogs("pylabrobot.veon.iprep2.device", level="WARNING") as logs:
      await self._device(deck=deck).setup()
    self.assertIn("source", "\n".join(logs.output))

  async def test_divergence_is_reported_rather_than_corrected(self) -> None:
    """PyLabRobot's tree is the source of truth, and a deck left loaded by a previous run is a
    fact about the bench that somebody should look at."""
    loaded = dict(EMPTY_DECK, Zone2={"labwareId": "rack-96"})
    device = self._device({"/deck/state": loaded})
    with self.assertLogs("pylabrobot.veon.iprep2.device", level="WARNING"):
      await device.setup()
    self.assertIsNone(device.deck.zones["Zone2"])

  async def test_a_zone_the_instrument_does_not_report_is_called_out(self) -> None:
    """Commands about it would be refused, so it is said out loud rather than found out later."""
    fewer = dict(CAPABILITIES, deck=dict(CAPABILITIES["deck"], zones=["Zone1", "Zone2"]))
    with self.assertLogs("pylabrobot.veon.iprep2.device", level="WARNING") as logs:
      await self._device({"/system/capabilities": fewer}).setup()
    self.assertIn("Zone3", "\n".join(logs.output))

  async def test_a_deck_state_that_cannot_be_read_does_not_fail_setup(self) -> None:
    """Not knowing what is on the deck is worth saying; it is not worth refusing to connect."""
    device = self._device({"/deck/state": RuntimeError("no")})
    with self.assertLogs("pylabrobot.veon.iprep2.device", level="WARNING"):
      await device.setup()
    self.assertEqual(device.num_channels, 8)
