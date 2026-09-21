"""The standard deck's geometry, and what the instrument's calibration does to it."""

import unittest

from pylabrobot.resources.coordinate import Coordinate
from pylabrobot.resources.resource import Resource
from pylabrobot.veon.iprep2.deck import (
  SIZE_X,
  SIZE_Y,
  ZONE_NAMES,
  ZONE_SIZE_X,
  ZONE_SIZE_Y,
  IPrep2Deck,
  _footprint,
)


def zone_at(deck: IPrep2Deck, zone: str) -> Coordinate:
  """Where a zone holder sits, which it always does.

  Args:
    deck: the deck to look on.
    zone: the zone to find.

  Returns:
    Its location.
  """
  location = deck.get_resource(f"{deck.name}_{zone}").location
  assert location is not None
  return location


def labware(name: str = "plate") -> Resource:
  """Something zone-sized to put in a zone.

  Args:
    name: what to call it.

  Returns:
    The resource.
  """
  return Resource(name=name, size_x=85.48, size_y=127.76, size_z=14.35)


class DeckGeometryTests(unittest.TestCase):
  """Where the zones are."""

  def setUp(self) -> None:
    self.deck = IPrep2Deck()

  def test_the_standard_deck_has_six_zones(self) -> None:
    self.assertEqual(self.deck.zone_names, ZONE_NAMES)
    self.assertEqual(len(ZONE_NAMES), 6)

  def test_a_zone_is_where_the_definition_puts_it(self) -> None:
    """The origin corner a labware component's own coordinates are added to."""
    self.assertEqual(zone_at(self.deck, "Zone1"), Coordinate(74.933, 149.630, 0.0))

  def test_zones_are_laid_out_in_two_rows_of_three(self) -> None:
    """Which is what the instrument's own deck definition describes."""
    locations = {zone: zone_at(self.deck, zone) for zone in self.deck.zone_names}
    back = [z for z, c in locations.items() if c.y > 100]
    front = [z for z, c in locations.items() if c.y < 100]
    self.assertEqual(sorted(back), ["Zone1", "Zone2", "Zone3"])
    self.assertEqual(sorted(front), ["Zone4", "Zone5", "Zone6"])

  def test_the_deck_is_not_perfectly_flat(self) -> None:
    """The instrument reports each zone's surface separately, and they differ by a third of a
    millimetre. Saying so costs nothing and modelling them all level would be a small lie."""
    heights = {zone: zone_at(self.deck, zone).z for zone in self.deck.zone_names}
    self.assertEqual(max(heights.values()), 0.0)
    self.assertAlmostEqual(min(heights.values()), -0.345, places=3)

  def test_every_zone_lies_within_the_decks_far_edges(self) -> None:
    """The footprint has to enclose the zones, or the visualizer draws plates off the deck. The
    front edge is not checked: Zones 4-6 start 4.4 mm before y = 0, which waits on the CAD
    figures rather than on a number here."""
    for zone in self.deck.zone_names:
      at = zone_at(self.deck, zone)
      self.assertLessEqual(at.x + ZONE_SIZE_X, SIZE_X, zone)
      self.assertLessEqual(at.y + ZONE_SIZE_Y, SIZE_Y, zone)
      self.assertGreaterEqual(at.x, 0.0, zone)

  def test_every_zone_takes_the_same_footprint(self) -> None:
    """An SBS plate stood on its long edge, which is how this deck takes one."""
    for zone in self.deck.zone_names:
      holder = self.deck.get_resource(f"deck_{zone}")
      self.assertEqual((holder.get_size_x(), holder.get_size_y()), (ZONE_SIZE_X, ZONE_SIZE_Y))

  def test_a_deck_can_be_built_with_a_subset_of_zones(self) -> None:
    """For an instrument that reports fewer than the standard deck's."""
    self.assertEqual(IPrep2Deck(zones=("Zone1", "Zone4")).zone_names, ("Zone1", "Zone4"))

  def test_an_unknown_zone_is_refused_with_what_is_available(self) -> None:
    """A deck this definition does not describe needs its own definition, not a guess."""
    with self.assertRaises(ValueError) as caught:
      IPrep2Deck(zones=("Zone1", "Zone9"))
    self.assertIn("Zone9", str(caught.exception))
    self.assertIn("Zone1", str(caught.exception))


class FitTests(unittest.TestCase):
  """Labware has to fit the zone in the orientation it is given in."""

  def setUp(self) -> None:
    self.deck = IPrep2Deck()

  def test_labware_that_fits_goes_in(self) -> None:
    """The deck takes an SBS footprint on its long edge, which is what `labware` is."""
    self.deck.assign_child_at_zone(labware(), "Zone1")
    self.assertIsNotNone(self.deck.zones["Zone1"])

  def test_a_plate_the_usual_way_round_is_refused(self) -> None:
    """PyLabRobot draws a plate on its short edge and this deck holds it on its long one, so the
    usual plate is a quarter turn out - and it would otherwise overhang the zone silently."""
    landscape = Resource(name="plate", size_x=127.76, size_y=85.48, size_z=14.35)
    with self.assertRaises(ValueError) as caught:
      self.deck.assign_child_at_zone(landscape, "Zone1")
    self.assertIn("rotated(z=90)", str(caught.exception))
    self.assertIn("127.76", str(caught.exception))

  def test_the_same_plate_turned_goes_in(self) -> None:
    """And the holder works out where the turned labware sits, so its wells land right."""
    landscape = Resource(name="plate", size_x=127.76, size_y=85.48, size_z=14.35)
    self.deck.assign_child_at_zone(landscape.rotated(z=90), "Zone1")
    self.assertIsNotNone(self.deck.zones["Zone1"])

  def test_labware_too_big_either_way_is_not_told_to_turn_it(self) -> None:
    """Advice that would not work is worse than none."""
    huge = Resource(name="huge", size_x=200.0, size_y=200.0, size_z=10.0)
    with self.assertRaises(ValueError) as caught:
      self.deck.assign_child_at_zone(huge, "Zone1")
    self.assertIn("would not help", str(caught.exception))
    self.assertNotIn("rotated", str(caught.exception))

  def test_small_labware_fits_either_way_round(self) -> None:
    """Only what overhangs is refused."""
    for resource in (
      Resource(name="a", size_x=50.0, size_y=60.0, size_z=10.0),
      Resource(name="b", size_x=60.0, size_y=50.0, size_z=10.0),
    ):
      with self.subTest(resource=resource.name):
        IPrep2Deck().assign_child_at_zone(resource, "Zone1")

  def test_labware_is_measured_in_its_own_frame_not_its_old_parents(self) -> None:
    """A plate coming off a turned carrier is judged as it will sit in the zone, not as it sat on
    the carrier: the holder it is going into is not rotated, whatever it is leaving was."""
    carrier = Resource(name="carrier", size_x=300.0, size_y=300.0, size_z=1.0)
    carrier.rotate(z=90)
    upright = labware()
    carrier.assign_child_resource(upright, location=Coordinate(0, 0, 0))
    self.deck.assign_child_at_zone(upright, "Zone1")
    self.assertIs(self.deck.zones["Zone1"], upright)

    landscape = Resource(name="plate", size_x=127.76, size_y=85.48, size_z=14.35)
    carrier.assign_child_resource(landscape, location=Coordinate(0, 0, 0))
    with self.assertRaises(ValueError):
      IPrep2Deck().assign_child_at_zone(landscape, "Zone2")

  def test_the_footprint_is_the_resources_own_rotation_and_nothing_above_it(self) -> None:
    """Turned by the resource's own rotation only: under a turned carrier the absolute size
    swaps, the footprint does not."""
    landscape = Resource(name="plate", size_x=127.76, size_y=85.48, size_z=14.35)
    width, depth = _footprint(landscape.rotated(z=90))
    self.assertAlmostEqual(width, 85.48, places=6)
    self.assertAlmostEqual(depth, 127.76, places=6)

    carrier = Resource(name="carrier", size_x=300.0, size_y=300.0, size_z=1.0)
    carrier.rotate(z=90)
    carrier.assign_child_resource(landscape, location=Coordinate(0, 0, 0))
    self.assertAlmostEqual(landscape.get_absolute_size_x(), 85.48, places=6)
    self.assertEqual(_footprint(landscape), (127.76, 85.48))

  def test_a_refused_plate_leaves_the_zone_empty(self) -> None:
    """Rather than half-assigned, which nothing downstream would describe."""
    landscape = Resource(name="plate", size_x=127.76, size_y=85.48, size_z=14.35)
    with self.assertRaises(ValueError):
      self.deck.assign_child_at_zone(landscape, "Zone1")
    self.assertIsNone(self.deck.zones["Zone1"])


class ZoneAssignmentTests(unittest.TestCase):
  """Putting labware in a zone."""

  def setUp(self) -> None:
    self.deck = IPrep2Deck()

  def test_labware_lands_at_the_zones_origin(self) -> None:
    plate = labware()
    self.deck.assign_child_at_zone(plate, "Zone2")
    self.assertEqual(plate.get_absolute_location(), Coordinate(176.303, 149.318, -0.009))

  def test_a_zone_holds_one_thing(self) -> None:
    self.deck.assign_child_at_zone(labware("first"), "Zone1")
    with self.assertRaises(ValueError) as caught:
      self.deck.assign_child_at_zone(labware("second"), "Zone1")
    self.assertIn("first", str(caught.exception))

  def test_an_unknown_zone_names_the_ones_there_are(self) -> None:
    with self.assertRaises(ValueError) as caught:
      self.deck.assign_child_at_zone(labware(), "Zone9")
    self.assertIn("Zone1", str(caught.exception))

  def test_what_is_in_each_zone_can_be_read_back(self) -> None:
    plate = labware()
    self.deck.assign_child_at_zone(plate, "Zone3")
    self.assertIs(self.deck.zones["Zone3"], plate)
    self.assertIsNone(self.deck.zones["Zone1"])
    self.assertEqual(self.deck.get_zone(plate), "Zone3")

  def test_labware_can_be_taken_out_again(self) -> None:
    plate = labware()
    self.deck.assign_child_at_zone(plate, "Zone3")
    self.deck.unassign_child_resource(plate)
    self.assertIsNone(self.deck.zones["Zone3"])
    self.assertIsNone(self.deck.get_zone(plate))

  def test_clearing_the_deck_empties_the_zones_and_keeps_them(self) -> None:
    """The zones are the deck. What goes is the labware, and a plate put down afterwards is still
    in the tree rooted at the deck."""
    self.deck.assign_child_at_zone(labware("first"), "Zone1")
    self.deck.clear()
    self.assertIsNone(self.deck.zones["Zone1"])
    self.assertEqual(self.deck.zone_names, ZONE_NAMES)
    plate = labware("second")
    self.deck.assign_child_at_zone(plate, "Zone1")
    self.assertIs(plate.get_root(), self.deck)

  def test_a_zone_cannot_be_taken_off_the_deck(self) -> None:
    """A deck that had lost one would still describe it."""
    holder = self.deck.get_resource("deck_Zone1")
    with self.assertRaises(ValueError) as caught:
      self.deck.unassign_child_resource(holder)
    self.assertIn("Zone1", str(caught.exception))
    with self.assertRaises(ValueError):
      holder.unassign()
    self.assertIn(holder, self.deck.children)

  def test_labware_cannot_be_put_straight_on_the_deck(self) -> None:
    """It would land somewhere no zone describes, and the instrument addresses zones."""
    with self.assertRaises(ValueError) as caught:
      self.deck.assign_child_resource(labware(), location=Coordinate(0, 0, 0))
    self.assertIn("assign_child_at_zone", str(caught.exception))

  def test_labware_wearing_a_zones_name_cannot_replace_the_zone(self) -> None:
    """A plate named like a zone holder would otherwise take the holder's place in the tree and
    in the deck's bookkeeping, and the zone would fail the first time it was asked what it held."""
    impostor = labware("deck_Zone1")
    with self.assertRaises(ValueError) as caught:
      self.deck.assign_child_resource(impostor, location=Coordinate(0, 0, 0))
    self.assertIn("assign_child_at_zone", str(caught.exception))
    holder = self.deck.get_resource("deck_Zone1")
    self.assertIn(holder, self.deck.children)
    self.assertIsNone(self.deck.zones["Zone1"])
    self.assertIsNone(impostor.parent)

  def test_the_summary_says_what_is_where(self) -> None:
    self.deck.assign_child_at_zone(labware("source"), "Zone2")
    summary = self.deck.summary()
    self.assertIn("Zone2: source", summary)
    self.assertIn("Zone1: -", summary)


class CalibrationTests(unittest.TestCase):
  """What the instrument measured, applied to what the definition drew."""

  def setUp(self) -> None:
    self.deck = IPrep2Deck()

  def test_an_offset_moves_the_zone(self) -> None:
    self.deck.apply_calibration({"Zone1": {"x": 1.5, "y": -0.5, "z": 0.0}})
    self.assertEqual(zone_at(self.deck, "Zone1"), Coordinate(76.433, 149.130, 0.0))

  def test_a_positive_z_offset_lowers_the_zone(self) -> None:
    """The instrument measures Z downward from the head, so further from the head is lower, and
    PyLabRobot measures it upward. The sign flips."""
    self.deck.apply_calibration({"Zone1": {"x": 0.0, "y": 0.0, "z": 0.25}})
    self.assertAlmostEqual(zone_at(self.deck, "Zone1").z, -0.25, places=6)

  def test_calibration_replaces_rather_than_accumulates(self) -> None:
    """Applying what the instrument currently reports must not drift by how many times it was
    read."""
    for _ in range(3):
      self.deck.apply_calibration({"Zone1": {"x": 1.0, "y": 0.0, "z": 0.0}})
    self.assertAlmostEqual(zone_at(self.deck, "Zone1").x, 75.933, places=6)

  def test_an_offset_for_a_zone_that_is_not_here_is_ignored(self) -> None:
    """The instrument may describe a deck this definition does not; a zone that is not here
    cannot be moved, and refusing would fail a setup over something that changes nothing."""
    self.deck.apply_calibration({"Zone9": {"x": 99.0, "y": 0.0, "z": 0.0}})
    self.assertEqual(zone_at(self.deck, "Zone1"), Coordinate(74.933, 149.630, 0.0))

  def test_a_zone_with_no_calibration_sits_where_the_definition_draws_it(self) -> None:
    """`null` rather than zeros: the zone has not been calibrated, which is not a reason to fail
    setup, and not a reason to leave it wherever an earlier calibration put it either."""
    self.deck.apply_calibration({"Zone1": {"x": 3.0, "y": 0.0, "z": 0.0}})
    self.deck.apply_calibration({"Zone1": None})
    self.assertEqual(zone_at(self.deck, "Zone1"), Coordinate(74.933, 149.630, 0.0))

  def test_a_missing_component_of_an_offset_is_zero(self) -> None:
    self.deck.apply_calibration({"Zone1": {"x": 2.0}})
    self.assertEqual(zone_at(self.deck, "Zone1"), Coordinate(76.933, 149.630, 0.0))

  def test_labware_moves_with_its_zone(self) -> None:
    """Which is the point of the zone being the holder rather than a coordinate looked up."""
    plate = labware()
    self.deck.assign_child_at_zone(plate, "Zone1")
    self.deck.apply_calibration({"Zone1": {"x": 3.0, "y": 0.0, "z": 0.0}})
    self.assertEqual(plate.get_absolute_location().x, 77.933)


class SerializationTests(unittest.TestCase):
  """A deck that has been laid out comes back as it was laid out."""

  def test_a_loaded_deck_round_trips(self) -> None:
    """With its labware in the zone it was in, and its calibration applied."""
    deck = IPrep2Deck()
    deck.apply_calibration({"Zone1": {"x": 1.5, "y": 0.0, "z": 0.0}})
    deck.assign_child_at_zone(labware("source"), "Zone1")

    loaded = IPrep2Deck.deserialize(deck.serialize())

    self.assertEqual(loaded.zone_names, ZONE_NAMES)
    held = loaded.zones["Zone1"]
    assert held is not None
    self.assertEqual(held.name, "source")
    self.assertIs(held.get_root(), loaded)
    self.assertEqual(zone_at(loaded, "Zone1"), Coordinate(76.433, 149.630, 0.0))
    self.assertEqual(len(loaded.children), 6)

  def test_a_renamed_deck_round_trips(self) -> None:
    """`named()` renames the deck and not its holders, so a loaded holder's name need not start
    with its deck's. It still lands in its zone - matched by the zone it carries, not by its
    name - rather than beside a placeholder, with the labware in the tree but in no zone."""
    deck = IPrep2Deck()
    deck.assign_child_at_zone(labware("source"), "Zone1")
    renamed = deck.named("bench")

    for loaded in (IPrep2Deck.deserialize(renamed.serialize()), renamed.copy()):
      with self.subTest(via=type(loaded).__name__):
        self.assertEqual(loaded.name, "bench")
        self.assertEqual(len(loaded.children), 6)
        self.assertEqual(loaded.zone_names, ZONE_NAMES)
        held = loaded.zones["Zone1"]
        assert held is not None
        self.assertEqual(held.name, "source")
        self.assertEqual(loaded.get_zone(held), "Zone1")
        self.assertIs(held.get_root(), loaded)

  def test_a_holder_that_does_not_say_its_zone_is_matched_by_name(self) -> None:
    """A deck serialized before holders carried their zone still loads."""
    serialized = IPrep2Deck().serialize()
    for child in serialized["children"]:
      child.pop("metadata", None)
    loaded = IPrep2Deck.deserialize(serialized)
    self.assertEqual(len(loaded.children), 6)
    self.assertEqual(loaded.zone_names, ZONE_NAMES)

  def test_a_deck_built_with_fewer_zones_comes_back_with_the_same_few(self) -> None:
    deck = IPrep2Deck(zones=("Zone1", "Zone4"))
    loaded = IPrep2Deck.deserialize(deck.serialize())
    self.assertEqual(loaded.zone_names, ("Zone1", "Zone4"))

  def test_a_deck_can_be_copied(self) -> None:
    """Which is what `rotated` and `at` do under the hood."""
    deck = IPrep2Deck()
    deck.assign_child_at_zone(labware(), "Zone2")
    copied = deck.copy()
    self.assertIsNotNone(copied.zones["Zone2"])
    self.assertIsNot(copied.zones["Zone2"], deck.zones["Zone2"])
