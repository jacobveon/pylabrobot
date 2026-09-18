"""The device tier: one tree, and what it says when the instrument disagrees with it."""

import unittest
from typing import Any, Dict, Optional

from pylabrobot.resources.resource import Resource
from pylabrobot.veon.iprep2.deck_tests import zone_at
from pylabrobot.veon.iprep2.configuration_tests import CAPABILITIES
from pylabrobot.veon.iprep2.deck import IPrep2Deck
from pylabrobot.veon.iprep2.device import IPrep2, IPrep2Device
from pylabrobot.veon.iprep2.driver import IPrep2Driver
from pylabrobot.veon.iprep2.driver_tests import _FakeEvents, _FakeHTTP

EMPTY_DECK: Dict[str, Any] = {zone: {} for zone in CAPABILITIES["deck"]["zones"]}
NO_CALIBRATION: Dict[str, Any] = {
  "zones": {zone: {"x": 0.0, "y": 0.0, "z": 0.0} for zone in EMPTY_DECK}
}


def labware(name: str = "plate") -> Resource:
  """Something to put in a zone.

  Args:
    name: what to call it.

  Returns:
    The resource.
  """
  return Resource(name=name, size_x=85.48, size_y=127.76, size_z=14.35)


class DeviceTests(unittest.IsolatedAsyncioTestCase):
  """Bringing the device up."""

  def _device(
    self, answers: Optional[Dict[str, Any]] = None, deck: Optional[IPrep2Deck] = None
  ) -> IPrep2Device:
    """A device on transports that answer without an instrument.

    Args:
      answers: extra or replacement path answers.
      deck: the deck to carry.

    Returns:
      The device, not yet set up.
    """
    http = _FakeHTTP(
      {"/deck/state": EMPTY_DECK, "/calibration/zones": NO_CALIBRATION, **(answers or {})}
    )
    device = IPrep2Device(deck=deck, driver=IPrep2Driver(io=http, events_io=_FakeEvents()))
    self.addAsyncCleanup(device.stop)
    return device

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


class DivergenceTests(unittest.IsolatedAsyncioTestCase):
  """What it says when the instrument's idea of the deck is not this model's."""

  def _device(
    self, answers: Optional[Dict[str, Any]] = None, deck: Optional[IPrep2Deck] = None
  ) -> IPrep2Device:
    """A device whose instrument answers as given.

    Args:
      answers: extra or replacement path answers.
      deck: the deck to carry.

    Returns:
      The device.
    """
    http = _FakeHTTP(
      {"/deck/state": EMPTY_DECK, "/calibration/zones": NO_CALIBRATION, **(answers or {})}
    )
    device = IPrep2Device(deck=deck, driver=IPrep2Driver(io=http, events_io=_FakeEvents()))
    self.addAsyncCleanup(device.stop)
    return device

  async def test_a_matching_deck_says_nothing(self) -> None:
    with self.assertNoLogs("pylabrobot.veon.iprep2.device", level="WARNING"):
      await self._device().setup()

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
