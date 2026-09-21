"""The i.prep 2: the device, its driver, and the deck it carries."""

import logging
from typing import Any, List, Mapping, Optional

from pylabrobot.resources.coordinate import Coordinate
from pylabrobot.resources.resource import Resource
from pylabrobot.veon.iprep2.configuration import Capabilities, DeviceIdentity, Readiness
from pylabrobot.veon.iprep2.deck import IPrep2Deck
from pylabrobot.veon.iprep2.driver import DEFAULT_PORT, IPrep2Driver

logger = logging.getLogger(__name__)

# The device takes the size of the deck it carries. The chassis stands around that deck and is
# larger, but nobody has measured one, and an invented envelope is worse than an honest
# understatement: something would be laid out against it.


class IPrep2Device(Resource):
  """A Veon Scientific i.prep 2 liquid handler.

  The device is itself a resource and its deck is its child, so everything on the deck is a
  descendant of the instrument carrying it: one tree, rooted here.

  Two tiers over one driver. `iprep2.driver` speaks to the instrument in its own terms - zones,
  channel numbers, millimetres - and stays reachable whatever is built on top of it. The device
  adds what the driver cannot know: where things are, and so which of them a command is about.
  """

  def __init__(
    self,
    deck: Optional[IPrep2Deck] = None,
    driver: Optional[IPrep2Driver] = None,
    host: str = "localhost",
    port: int = DEFAULT_PORT,
    api_key: Optional[str] = None,
    secure: bool = False,
    follow_events: bool = True,
    name: str = "i.prep 2",
    model: Optional[str] = None,
    size_x: Optional[float] = None,
    size_y: Optional[float] = None,
    size_z: Optional[float] = None,
    category: str = "device",
    metadata: Optional[Mapping[str, Any]] = None,
  ):
    """
    Args:
      deck: the deck this instrument carries. Becomes a child of the device, so everything
        assigned to it is a descendant of this device. Defaults to the standard deck. A
        serialized device carries its deck as a child, and that one replaces this on
        deserialization.
      driver: the driver to drive it through. Defaults to one built from the arguments below.
      host: the instrument's hostname or address, when building a driver.
      port: the port it serves on.
      api_key: a bearer token, for an instrument that requires one. Requires `secure`.
      secure: whether to reach the instrument over TLS.
      follow_events: whether to follow the instrument's event stream.
      name: what to call this device in the resource tree.
      model: which kind of resource this is, in PyLabRobot's terms. Not the instrument's own
        model string, which setup reads into `identity.model` and leaves there: one names a
        resource and the other names a machine, and a reader of either wants to know which.
      size_x: how wide the device is, in mm. Defaults to the deck's own width.
      size_y: how deep it is, in mm. Defaults to the deck's own depth.
      size_z: how tall it is, in mm. Defaults to the deck's own height.
      category: which kind of resource this is. Written by `serialize` and handed back by
        `deserialize`, which is why it is taken here.
      metadata: likewise, anything a caller attached to the device.
    """
    deck = deck if deck is not None else IPrep2Deck()
    super().__init__(
      name=name,
      size_x=deck.get_absolute_size_x() if size_x is None else size_x,
      size_y=deck.get_absolute_size_y() if size_y is None else size_y,
      size_z=deck.get_absolute_size_z() if size_z is None else size_z,
      category=category,
      model=model if model is not None else self.__class__.__name__,
      metadata=metadata,
    )
    self.deck = deck
    self.driver = (
      driver
      if driver is not None
      else IPrep2Driver(
        host=host, port=port, api_key=api_key, secure=secure, follow_events=follow_events
      )
    )
    self.assign_child_resource(deck, location=deck.location or Coordinate(0, 0, 0))

  # ----------------------------------------
  # What this instrument turned out to be
  # ----------------------------------------

  @property
  def identity(self) -> DeviceIdentity:
    """Which instrument this is, and what it is running.

    Returns:
      The identity, once setup has read it.
    """
    return self.driver.identity

  @property
  def capabilities(self) -> Capabilities:
    """What the instrument reported it has.

    Returns:
      The capabilities, once setup has read them.
    """
    return self.driver.capabilities

  @property
  def num_channels(self) -> int:
    """How many pipetting channels this instrument has.

    Returns:
      The count, as the instrument reported it.
    """
    return self.driver.num_channels

  async def readiness(self) -> Readiness:
    """Ask whether the instrument is free, and how it was left.

    Returns:
      What it reported.
    """
    return await self.driver.request_readiness()

  # ----------------------------------------
  # Session
  # ----------------------------------------

  async def setup(self) -> None:
    """Bring the device up.

    Reads only: nothing moves. The instrument is asked what it is, its calibration is applied to
    the deck, and its event stream is followed so the model keeps up with what it does.

    What the instrument was left holding is reported rather than tidied. Whether it is safe to
    home an instrument or eject a tip depends on what is underneath, and this has not looked yet.

    Raises:
      IPrep2Error: If the instrument would not say what it is, or how its deck is calibrated. A
        deck placed against an unknown calibration is a deck placed wrong, so this is not tolerated
        the way an unreadable deck state is. Whatever was opened is closed again first.
    """
    await self.driver.setup()
    try:
      self._check_zones_match()
      self.deck.apply_calibration(await self.driver.request_zone_calibration())
    except BaseException:
      await self.driver._stop_quietly()
      raise
    await self._report_deck_divergence()

  async def stop(self) -> None:
    """Put the device down. The instrument is left exactly as it is."""
    await self.driver.stop()

  # ----------------------------------------
  # The deck, against what the instrument believes
  # ----------------------------------------

  def _check_zones_match(self) -> None:
    """Warn if the deck this device carries is not the deck the instrument reports.

    A mismatch is not fatal - a deck is a definition and an instrument can be fitted with one this
    PyLabRobot does not describe - but a zone in one and not the other is a zone that commands
    will be sent about and nothing will be modelled for, so it is said out loud.
    """
    described = set(self.deck.zone_names)
    reported = set(self.capabilities.deck.zones)
    if described == reported:
      return
    if reported - described:
      logger.warning(
        "the instrument reports zones this deck does not describe: %s. Labware there will not be "
        "modelled.",
        ", ".join(sorted(reported - described)),
      )
    if described - reported:
      logger.warning(
        "this deck describes zones the instrument does not report: %s. Commands about them will "
        "be refused.",
        ", ".join(sorted(described - reported)),
      )

  async def _report_deck_divergence(self) -> None:
    """Say where the instrument's idea of what is on the deck differs from this model's.

    PyLabRobot's resource tree is the source of truth for what is where - it is the only one of
    the two that knows what stands next to the instrument - so nothing here is changed to match.
    What is reported is the difference, since a zone the instrument believes is loaded and this
    model believes is empty will behave in ways neither explains.

    Bringing the two into line is a separate matter, and deliberately not done at setup: a deck
    left loaded by a previous run is a fact about the bench that somebody should look at.
    """
    try:
      state = await self.driver.request_deck_state()
    except Exception as exc:
      logger.warning("could not read what the instrument has on its deck: %r", exc)
      return

    differences: List[str] = []
    for zone, held in self.deck.zones.items():
      on_instrument = bool(state.get(zone))
      if on_instrument and held is None:
        differences.append(f"{zone}: the instrument holds labware, this model does not")
      elif held is not None and not on_instrument:
        differences.append(f"{zone}: this model holds {held.name}, the instrument does not")
    if differences:
      logger.warning(
        "the instrument's deck and this model disagree:\n  %s", "\n  ".join(differences)
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
    """Assign a child to the device.

    The device's one child is its deck, assigned in `__init__`. Deserialization then assigns the
    serialized deck, which carries the labware; it replaces the one built by default rather than
    standing beside it, and becomes `self.deck`.

    Args:
      resource: the child.
      location: where it sits.
      reassign: whether to replace a child of the same name.

    Raises:
      ValueError: If a child of the same name is already here and `reassign` is False, or if
        what would replace the deck is not an `IPrep2Deck`. The device carries one deck and
        answers for it, so it cannot be swapped for something that does not behave as one.
    """
    existing = next((child for child in self.children if child.name == resource.name), None)
    if existing is not None:
      if not reassign:
        raise ValueError(f"{resource.name!r} is already assigned to this device")
      if existing is self.deck:
        if not isinstance(resource, IPrep2Deck):
          raise ValueError(
            f"{resource.name!r} is named like this device's deck but is not an IPrep2Deck, and "
            f"the device carries one deck and answers for it"
          )
        super().unassign_child_resource(existing)
        self.deck = resource
      else:
        super().unassign_child_resource(existing)
    super().assign_child_resource(resource, location=location, reassign=reassign)

  def serialize(self) -> dict:
    """This device, with how to reach the instrument again - but not the key to it.

    The address is the driver's, not what this constructor was given: a caller that handed over
    a ready-built driver never gave the constructor an address at all, and what has to come back
    is the instrument the device actually talks to. The key does not come back: it is a
    credential, and serialized state is written to disk and passed around.

    Returns:
      The serialized device.
    """
    return {
      **super().serialize(),
      "host": self.driver.host,
      "port": self.driver.port,
      "secure": self.driver.secure,
      "follow_events": self.driver.follow_events,
    }

  def __str__(self) -> str:
    identity = self.driver._identity
    if identity is None:
      return f"IPrep2Device({self.name}, not set up)"
    return (
      f"IPrep2Device({identity.name or self.name}, {self.driver.num_channels} channels, "
      f"{len(self.deck.zone_names)} zones)"
    )


def IPrep2(
  host: str = "localhost",
  port: int = DEFAULT_PORT,
  api_key: Optional[str] = None,
  secure: bool = False,
  follow_events: bool = True,
  deck: Optional[IPrep2Deck] = None,
  name: str = "i.prep 2",
) -> IPrep2Device:
  """An i.prep 2 on the standard deck.

  Args:
    host: the instrument's hostname or address.
    port: the port it serves on.
    api_key: a bearer token, for an instrument that requires one. Requires `secure`.
    secure: whether to reach the instrument over TLS.
    follow_events: whether to follow the instrument's event stream.
    deck: the deck it carries. Defaults to the standard deck.
    name: what to call it in the resource tree.

  Returns:
    The device, ready to be set up.
  """
  return IPrep2Device(
    deck=deck if deck is not None else IPrep2Deck(),
    host=host,
    port=port,
    api_key=api_key,
    secure=secure,
    follow_events=follow_events,
    name=name,
    model=IPrep2.__name__,
  )
