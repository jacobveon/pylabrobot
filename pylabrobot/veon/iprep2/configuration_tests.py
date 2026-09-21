"""Reading what an i.prep 2 says it is, from what one actually answered."""

import unittest
from typing import Any, Dict

from pylabrobot.veon.iprep2.configuration import (
  Capabilities,
  DeviceIdentity,
  Readiness,
  describe,
)

# What an i.prep 2 answered `GET /system/capabilities` with. Eight channels and ten axes, against
# the four and two the published example shows - which is the reason nothing here is hard-coded.
CAPABILITIES: Dict[str, Any] = {
  "pipette": {
    "channels": [1, 2, 3, 4, 5, 6, 7, 8],
    "channel_pitch_mm": 9.0,
    "max_volume_ul": 1100.0,
    "max_rate_ul_per_s": 1500.0,
    "tip_capacities_ul": [50.0, 200.0, 1000.0],
    "tip_capacities_by_channel": {str(c): [50.0, 200.0, 1000.0] for c in range(1, 9)},
  },
  "motion": {
    "axes": ["x", "y"] + [f"z{c}" for c in range(1, 9)],
    "travel": dict(
      {"x": {"min_mm": -1.0, "max_mm": 300.0}, "y": {"min_mm": -1.0, "max_mm": 300.0}},
      **{f"z{c}": {"min_mm": -1.0, "max_mm": 220.0} for c in range(1, 9)},
    ),
  },
  "deck": {
    "zones": ["Zone1", "Zone2", "Zone3", "Zone4", "Zone5", "Zone6"],
    "dimensions_mm": {"x": 350, "y": 350, "z": 217.172},
    "safe_travel_mm": 10.0,
  },
  "liquid_classes": ["water_1000_transfer", "water_200_transfer", "water_50_transfer"],
}

INFO: Dict[str, Any] = {
  "device": {"name": "chadley", "model": "iprep2", "serial": "00000"},
  "meta": {"api_version": "0.10.0", "events_version": "1.6.0", "firmware_version": "0.8.0-rc5"},
  "simulation": {"simulated": True, "motors": True, "pipette_module": True},
}


class CapabilitiesTests(unittest.TestCase):
  """What the instrument reported it has."""

  def setUp(self) -> None:
    self.capabilities = Capabilities.from_response(CAPABILITIES)

  def test_reads_the_head(self) -> None:
    """Channels are the instrument's own numbers, from 1."""
    pipette = self.capabilities.pipette
    self.assertEqual(pipette.channels, (1, 2, 3, 4, 5, 6, 7, 8))
    self.assertEqual(pipette.num_channels, 8)
    self.assertEqual(pipette.channel_pitch, 9.0)
    self.assertEqual(pipette.tip_capacities, (50.0, 200.0, 1000.0))

  def test_tip_capacities_are_keyed_by_channel_number(self) -> None:
    """JSON has no integer keys, so the instrument sends strings and this converts them."""
    self.assertEqual(self.capabilities.pipette.tip_capacities_by_channel[3], (50.0, 200.0, 1000.0))

  def test_a_channels_z_axis_is_read_not_assumed(self) -> None:
    """Which axis serves which channel is the instrument's to say."""
    self.assertEqual(self.capabilities.motion.z_axis_for_channel(3), "z3")
    self.assertIsNone(self.capabilities.motion.z_axis_for_channel(99))

  def test_travel_bounds_a_position(self) -> None:
    """An axis reaches what it reaches."""
    x = self.capabilities.motion.travel["x"]
    self.assertEqual(x.as_tuple(), (-1.0, 300.0))
    self.assertTrue(x.contains(150.0))
    self.assertFalse(x.contains(400.0))

  def test_reads_the_deck(self) -> None:
    """Zone names come off the instrument; where they are does not."""
    deck = self.capabilities.deck
    self.assertEqual(len(deck.zones), 6)
    self.assertEqual((deck.size_x, deck.size_y), (350, 350))
    self.assertEqual(deck.safe_travel, 10.0)

  def test_an_empty_response_does_not_raise(self) -> None:
    """An instrument that answered with less than expected leaves a driver with less, not with an
    exception: what it did say is still worth having."""
    empty = Capabilities.from_response({})
    self.assertEqual(empty.pipette.num_channels, 0)
    self.assertEqual(empty.motion.axes, ())
    self.assertIsNone(empty.deck.size_x)

  def test_unfamiliar_fields_are_ignored(self) -> None:
    """The instrument adds fields; a driver that refused one would stop working on an instrument
    that had merely been updated."""
    grown = dict(CAPABILITIES, something_new={"added": "later"})
    grown["pipette"] = dict(CAPABILITIES["pipette"], future_field=1)
    self.assertEqual(Capabilities.from_response(grown).pipette.num_channels, 8)

  def test_an_axis_without_a_range_is_left_out(self) -> None:
    """Half a range describes nothing, and `null` describes less; neither is a reason to fail."""
    partial = dict(CAPABILITIES)
    partial["motion"] = {"axes": ["x", "y"], "travel": {"x": {"min_mm": 0.0}, "y": None}}
    self.assertEqual(Capabilities.from_response(partial).motion.travel, {})


class DeviceIdentityTests(unittest.TestCase):
  """Which instrument answered."""

  def test_reads_the_device_and_its_versions(self) -> None:
    identity = DeviceIdentity.from_response(INFO)
    self.assertEqual(
      (identity.name, identity.model, identity.serial), ("chadley", "iprep2", "00000")
    )
    self.assertEqual(identity.versions["api_version"], "0.10.0")

  def test_the_instrument_says_whether_it_is_simulated(self) -> None:
    """Rather than a caller inferring it from the address it used."""
    self.assertTrue(DeviceIdentity.from_response(INFO).simulated)
    self.assertFalse(DeviceIdentity.from_response({"device": {"name": "a"}}).simulated)


class ReadinessTests(unittest.TestCase):
  """Whether the instrument is free, and how it was left."""

  def test_a_parked_instrument_is_not_dirty(self) -> None:
    ready = Readiness.from_response(
      {"busy": False, "at_home": True, "axes_away_from_home": [], "tips_attached": []}
    )
    self.assertFalse(ready.busy)
    self.assertFalse(ready.left_dirty)

  def test_a_finished_run_can_leave_it_dirty(self) -> None:
    """Free, but not put away: the next operation starts from somewhere nobody chose."""
    ready = Readiness.from_response(
      {"busy": False, "at_home": False, "axes_away_from_home": ["x", "y"], "tips_attached": [1, 2]}
    )
    self.assertTrue(ready.left_dirty)
    self.assertEqual(ready.tips_attached, (1, 2))

  def test_an_instrument_that_did_not_say_whether_it_is_home_is_not_called_dirty(self) -> None:
    """Not saying is not the same as saying no; only what was reported counts."""
    ready = Readiness.from_response({"busy": False, "tips_attached": []})
    self.assertIsNone(ready.at_home)
    self.assertFalse(ready.left_dirty)
    self.assertTrue(Readiness.from_response({"busy": False, "at_home": False}).left_dirty)
    self.assertTrue(
      Readiness.from_response({"busy": False, "axes_away_from_home": ["x"]}).left_dirty
    )

  def test_a_busy_instrument_is_not_reported_dirty(self) -> None:
    """Something holds it, which is the more useful thing to say."""
    ready = Readiness.from_response(
      {"busy": True, "owner": "transfer", "request_id": "abc", "at_home": False}
    )
    self.assertTrue(ready.busy)
    self.assertFalse(ready.left_dirty)
    self.assertEqual(ready.owner, "transfer")


class DescribeTests(unittest.TestCase):
  """The account setup writes to the log."""

  def test_says_what_was_found(self) -> None:
    lines = "\n".join(
      describe(DeviceIdentity.from_response(INFO), Capabilities.from_response(CAPABILITIES))
    )
    self.assertIn("chadley", lines)
    self.assertIn("8 channels", lines)
    self.assertIn("[simulated]", lines)
    self.assertIn("6 zones", lines)
