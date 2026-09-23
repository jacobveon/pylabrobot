"""The i.prep 2's deck: six zones, and where each one is.

The geometry is a definition rather than a discovery. The instrument reports which zones it has
and how they have been calibrated, but not where they are, and PyLabRobot needs to know that
without an instrument to ask - a protocol is laid out, a deck is drawn in the visualizer, and a
run is planned long before anything is plugged in. So the standard deck is described here, as
every other PyLabRobot deck is, and what the instrument knows is applied on top of it.

What the instrument contributes is calibration: the per-zone offset an operator measured on the
bench, which says where this instrument found a zone rather than where the drawing puts it.
`apply_calibration` moves the zones by it.

The instrument's x and y agree with PyLabRobot's. The gantry carries the channels left to right
along x, and the deck itself moves along y: increasing y brings the deck forward, so in the deck's
own frame - the one everything here is in - the channels travel toward the back as y grows, which
is PyLabRobot's y. Home is x = 0, y = 0, with channel 1 over the deck's front-left; a zone's
position is where channel 1 sits over the zone's front-left corner, and labware in the zone has
its origin there too. The instrument's own plate definitions confirm the handedness: their A1,
H1 and A12 fall where PyLabRobot puts them on a plate turned a quarter turn anticlockwise.

A note on Z, because the two frames disagree. PyLabRobot measures Z upward from the deck surface,
so a taller plate reaches a higher z. The instrument measures it downward from the head's home
position, so its deck surface reads about 217 mm and a taller plate reaches a *smaller* number.
Nothing here carries the instrument's frame: zone heights below are PyLabRobot's, and converting
between the two is the driver's business at the point a move is commanded.
"""

from typing import Any, Dict, List, Mapping, Optional, Tuple

from pylabrobot.resources.coordinate import Coordinate
from pylabrobot.resources.deck import Deck
from pylabrobot.resources.resource import Resource
from pylabrobot.resources.resource_holder import ResourceHolder
from pylabrobot.utils.linalg import matrix_vector_multiply_3x3

# The standard deck, as the instrument's own deck definition describes it (`iprep2-standard-deck`
# 1.0.0). One entry per zone: where the labware's origin corner sits on the deck, and how much
# room the zone gives it.
#
# The origin corner is what the instrument adds a labware component's own coordinates to - a well
# at (11.24, 14.38) in its plate is at the zone's x + 11.24, y + 14.38 - so it is the same point
# PyLabRobot locates a resource by, and a zone becomes a holder at exactly this position.
DECK_ID = "iprep2-standard-deck"
DECK_DEFINITION_VERSION = "1.0.0"

# The footprint, as the deck definition's `dimensions` give it. These are the instrument's
# outside dimensions, 532 mm wide by 520 mm deep, not the moving deck plate's, so the zones sit
# in the front-left of the rectangle: the gantry's frame takes the sides and the deck's travel
# takes the back. Zones 4-6 start 4.4 mm in front of y = 0, which is where channel 1 sits at
# home, so the plate's front edge reaches a little past the head's reach.
SIZE_X = 532.0
SIZE_Y = 520.0
# The instrument's Z reading with a channel touching the deck surface. Its Z frame starts at the
# channel's home position and grows as the channel comes down, so this is the head's full travel
# to the deck - the height of the working volume above the surface - and not the thickness of any
# part. The deck resource spans that volume: its origin plane is the surface the zones sit on,
# and its top is where the channels rest at home.
SIZE_Z = 217.172

# The height that is clear to travel at, in mm above the deck surface.
SAFE_TRAVEL_Z = 10.0

# Every zone gives its labware the same room: an SBS footprint stood on its long edge, which is
# how this deck takes a plate.
ZONE_SIZE_X = 86.0
ZONE_SIZE_Y = 128.25

# Where each zone's origin corner sits, in mm on the deck surface, and how far that zone's
# surface sits below the highest one. Six zones in two rows of three.
#
# The z figures come from the instrument reporting each zone's surface in its own downward frame
# (217.172 to 217.517 mm from home). Subtracting each from the shallowest gives how much lower
# that zone sits than the highest, which is what PyLabRobot's upward frame wants: a zone reading
# further from home is a zone whose surface is further down. The spread is a third of a
# millimetre - the deck is not perfectly flat, and saying so costs nothing.
_HIGHEST_SURFACE_FROM_HOME = 217.172
_ZONE_POSITIONS: Dict[str, Tuple[float, float, float]] = {
  "Zone1": (74.933, 149.630, 217.172),
  "Zone2": (176.303, 149.318, 217.181),
  "Zone3": (276.901, 149.311, 217.255),
  "Zone4": (74.699, -4.437, 217.517),
  "Zone5": (176.183, -4.487, 217.422),
  "Zone6": (277.028, -4.486, 217.419),
}

ZONE_NAMES: Tuple[str, ...] = tuple(_ZONE_POSITIONS)


def _zone_location(zone: str) -> Coordinate:
  """Where a zone sits on the deck, in PyLabRobot's frame.

  Args:
    zone: the zone's name.

  Returns:
    Its origin corner, with z measured upward from the highest zone's surface.
  """
  x, y, from_home = _ZONE_POSITIONS[zone]
  return Coordinate(x, y, _HIGHEST_SURFACE_FROM_HOME - from_home)


def _footprint(resource: Resource) -> Tuple[float, float]:
  """How much of a zone a resource takes, in the frame it will be held in.

  A zone holder is not rotated, so what matters is the resource's own rotation and nothing above
  it: `get_absolute_size_x` would include whatever the resource currently sits under, and labware
  being moved off a turned carrier would be measured in the carrier's frame rather than the
  zone's.

  Args:
    resource: the labware.

  Returns:
    Its width and depth on the deck, in mm.
  """
  matrix = resource.rotation.get_rotation_matrix()
  corners = [
    Coordinate(*matrix_vector_multiply_3x3(matrix, corner.vector()))
    for corner in (
      Coordinate(0, 0, 0),
      Coordinate(resource.get_size_x(), 0, 0),
      Coordinate(0, resource.get_size_y(), 0),
      Coordinate(resource.get_size_x(), resource.get_size_y(), 0),
    )
  ]
  xs = [corner.x for corner in corners]
  ys = [corner.y for corner in corners]
  return max(xs) - min(xs), max(ys) - min(ys)


class IPrep2Deck(Deck):
  """The deck of an i.prep 2.

  Each zone is a :class:`~pylabrobot.resources.resource_holder.ResourceHolder` child of the deck,
  so labware placed in a zone is a descendant of the deck carrying it and its wells locate
  against the instrument's own coordinates without anything further being worked out.

  Labware goes in with :meth:`assign_child_at_zone`, named as the instrument names the zone.
  """

  def __init__(
    self,
    name: str = "deck",
    size_x: float = SIZE_X,
    size_y: float = SIZE_Y,
    size_z: float = SIZE_Z,
    origin: Coordinate = Coordinate(0, 0, 0),
    zones: Optional[Tuple[str, ...]] = None,
    category: str = "deck",
    metadata: Optional[Mapping[str, Any]] = None,
  ):
    """
    Args:
      name: what to call this deck in the resource tree.
      size_x: how wide it is, in mm.
      size_y: how deep it is, in mm.
      size_z: how far its surface stands above its origin, in mm.
      origin: where the deck sits in whatever carries it.
      zones: which zones to build, for an instrument that reports a subset of the standard deck's.
        Defaults to all six.
      category: which kind of resource this is. `serialize` writes it and `deserialize` hands it
        back, so it is taken here even though a deck is always a deck.
      metadata: likewise, anything a caller attached to the deck.

    Raises:
      ValueError: If a zone is named that this deck definition does not describe.
    """
    super().__init__(
      name=name,
      size_x=size_x,
      size_y=size_y,
      size_z=size_z,
      origin=origin,
      category=category,
      metadata=metadata,
    )

    wanted = ZONE_NAMES if zones is None else tuple(zones)
    unknown = [zone for zone in wanted if zone not in _ZONE_POSITIONS]
    if unknown:
      raise ValueError(
        f"the {DECK_ID} definition does not describe {', '.join(unknown)}. It has: "
        f"{', '.join(ZONE_NAMES)}. An instrument with a different deck needs its own definition."
      )

    self._zone_holders: Dict[str, ResourceHolder] = {}
    for zone in wanted:
      holder = ResourceHolder(
        name=f"{name}_{zone}",
        size_x=ZONE_SIZE_X,
        size_y=ZONE_SIZE_Y,
        size_z=0,
        category="iprep2_zone",
        model=f"{DECK_ID}_zone",
        # Which zone this holder stands for, carried on the holder itself rather than read off its
        # name: `named()` renames a deck without renaming its children, so a holder's name need
        # not start with its deck's, and `assign_child_resource` needs to know the zone anyway.
        metadata={"zone": zone},
      )
      self._zone_holders[zone] = holder
      super().assign_child_resource(holder, location=_zone_location(zone))

  # ----------------------------------------
  # Zones
  # ----------------------------------------

  @property
  def zone_names(self) -> Tuple[str, ...]:
    """The zones this deck has, in the order the definition lists them.

    Returns:
      The zone names.
    """
    return tuple(self._zone_holders)

  @property
  def zones(self) -> Dict[str, Optional[Resource]]:
    """What is in each zone, None for a zone holding nothing.

    Returns:
      One entry per zone.
    """
    return {zone: holder.resource for zone, holder in self._zone_holders.items()}

  def _check_fits(self, resource: Resource, zone: str) -> None:
    """Refuse labware that does not fit the zone it is being put in.

    This deck holds an SBS footprint on its long edge, and PyLabRobot's plate definitions are
    drawn on their short edge, so the usual plate is a quarter turn from the orientation this deck
    takes. Turning it is the caller's to do rather than this method's: rotating labware moves
    every well in it, and doing that silently is how a protocol comes to aspirate from the wrong
    one. Refusing says what happened at the point the mistake was made.

    Args:
      resource: the labware.
      zone: the zone it is going in.

    Raises:
      ValueError: If it does not fit as it stands.
    """
    width, depth = _footprint(resource)
    if width <= ZONE_SIZE_X and depth <= ZONE_SIZE_Y:
      return

    complaint = (
      f"{resource.name} is {width:.2f} x {depth:.2f} mm and {zone} takes "
      f"{ZONE_SIZE_X} x {ZONE_SIZE_Y} mm"
    )
    if depth <= ZONE_SIZE_X and width <= ZONE_SIZE_Y:
      raise ValueError(
        f"{complaint}. This deck holds labware on its long edge, so it needs a quarter turn: "
        f"assign_child_at_zone(resource.rotated(z=90), {zone!r}). Rotating it here instead would "
        f"move every well without saying so."
      )
    raise ValueError(f"{complaint}, and turning it would not help.")

  def assign_child_at_zone(self, resource: Resource, zone: str) -> None:
    """Put labware in a zone.

    The labware has to fit the zone as it stands. This deck takes an SBS footprint on its long
    edge, which is a quarter turn from how PyLabRobot draws a plate, so the usual plate goes in as
    `resource.rotated(z=90)` - and the zone holder works out where the turned labware sits, so its
    wells land where the instrument expects them.

    Args:
      resource: the labware.
      zone: which zone, named as the instrument names it.

    Raises:
      ValueError: If there is no such zone, something is already in it, or the labware does not
        fit it in the orientation given.
    """
    holder = self._zone_holders.get(zone)
    if holder is None:
      raise ValueError(f"no zone {zone!r} on this deck; it has {', '.join(self.zone_names)}")
    self._check_fits(resource, zone)
    if holder.resource is not None:
      raise ValueError(f"{zone} already holds {holder.resource.name}")
    holder.assign_child_resource(resource)

  def unassign_child_resource(self, resource: Resource) -> None:
    """Take labware out of whichever zone holds it.

    Args:
      resource: the labware to remove.

    Raises:
      ValueError: If the resource is one of the zones themselves. A zone is part of the deck, and
        a deck that had lost one would still answer for it - `zone_names` and
        `assign_child_at_zone` would go on describing a holder that was no longer in the tree.
    """
    for zone, holder in self._zone_holders.items():
      if holder is resource:
        raise ValueError(
          f"{zone} is part of this deck and cannot be taken off it; take out what it holds instead"
        )
      if holder.resource is resource:
        holder.unassign_child_resource(resource)
        return
    super().unassign_child_resource(resource)

  def clear(self, include_trash: bool = False) -> None:
    """Take every piece of labware off the deck. The zones stay.

    Args:
      include_trash: accepted for compatibility with other decks; this one has no trash area.
    """
    for holder in self._zone_holders.values():
      held = holder.resource
      if held is not None:
        holder.unassign_child_resource(held)

  def get_zone(self, resource: Resource) -> Optional[str]:
    """Which zone holds a resource.

    Args:
      resource: the labware to find.

    Returns:
      The zone's name, or None if this deck is not holding it.
    """
    for zone, holder in self._zone_holders.items():
      if holder.resource is resource:
        return zone
    return None

  # ----------------------------------------
  # Calibration
  # ----------------------------------------

  def apply_calibration(self, offsets: Mapping[str, Optional[Mapping[str, float]]]) -> None:
    """Move the zones by what the instrument measured.

    The definition says where a zone is drawn; this says where this instrument found it. An offset
    for a zone this deck does not have is ignored rather than refused: the instrument may describe
    a deck this definition does not, and a zone that is not here cannot be moved.

    The instrument's z offset is in its own downward frame, so a positive one means the zone
    turned out to be *further* from the head - lower - and it is subtracted rather than added.

    A zone reported with no offset at all - `null`, rather than zeros - has not been calibrated,
    and sits where the definition draws it.

    Args:
      offsets: the per-zone offset, as `GET /calibration/zones` reports it.
    """
    for zone, offset in offsets.items():
      holder = self._zone_holders.get(zone)
      if holder is None:
        continue
      if not isinstance(offset, Mapping):
        offset = {}
      base = _zone_location(zone)
      holder.location = Coordinate(
        base.x + float(offset.get("x", 0.0) or 0.0),
        base.y + float(offset.get("y", 0.0) or 0.0),
        base.z - float(offset.get("z", 0.0) or 0.0),
      )

  # ----------------------------------------
  # Resource tree
  # ----------------------------------------

  def assign_child_resource(
    self,
    resource: Resource,
    location: Optional[Coordinate] = None,
    reassign: bool = True,
  ) -> None:
    """Assign a zone holder to the deck.

    The deck's own children are the zone holders built in `__init__`. Labware goes into a zone
    with `assign_child_at_zone` rather than onto the deck, so that it lands where the instrument
    believes that zone is. Deserialization re-assigns the holders, replacing a placeholder with
    the loaded one and whatever labware it carries.

    A loaded holder replaces the one standing for its zone, found by the zone it carries in its
    metadata rather than by its name: a deck renamed with `named()` keeps its holders' old names,
    and matching on those would leave the loaded holders standing beside six empty ones, with the
    labware in the tree but in no zone. A holder that does not say which zone it is is matched by
    name, as one serialized before the zone was carried is.

    Args:
      resource: the holder to assign.
      location: where it sits.
      reassign: whether to replace the holder standing for the same zone or wearing the same
        name.

    Raises:
      ValueError: If something other than a zone holder is assigned directly to the deck, whether
        or not it wears a zone holder's name. A zone that had been swapped for a plate would
        still be answered for as a zone, and fail the first time it was asked what it holds.
    """
    if not isinstance(resource, ResourceHolder):
      raise ValueError(
        f"cannot assign {resource.name!r} straight to the deck: labware goes in a zone, with "
        f"assign_child_at_zone(resource, 'Zone1')"
      )
    zone = resource.metadata.get("zone")
    existing: Optional[Resource] = self._zone_holders.get(zone) if isinstance(zone, str) else None
    if existing is None:
      existing = next((child for child in self.children if child.name == resource.name), None)
    if existing is not None:
      if not reassign:
        raise ValueError(f"{resource.name!r} is already assigned to this deck")
      super().unassign_child_resource(existing)
      for zone_name, holder in self._zone_holders.items():
        if holder is existing:
          self._zone_holders[zone_name] = resource
          break
    super().assign_child_resource(resource, location=location, reassign=reassign)

  def serialize(self) -> dict:
    """This deck, with which zones it was built with.

    The zones are children and go round with the rest of the tree, but which ones to *build* is a
    constructor argument, and a deck built with fewer than the standard six has to come back with
    the same few rather than with all of them plus the loaded ones replacing some.

    Returns:
      The serialized deck.
    """
    return {**super().serialize(), "zones": list(self.zone_names)}

  def summary(self) -> str:
    """What is on the deck, zone by zone.

    Returns:
      One line per zone.
    """
    lines: List[str] = [f"{self.name} ({DECK_ID} {DECK_DEFINITION_VERSION})"]
    for zone, holder in self._zone_holders.items():
      held = holder.resource
      lines.append(f"  {zone}: {held.name if held is not None else '-'}")
    return "\n".join(lines)
