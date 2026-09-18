"""The i.prep 2: the device, its driver, and the deck it carries."""

import logging
from typing import List, Optional

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
  ):
    """
    Args:
      deck: the deck this instrument carries. Becomes a child of the device, so everything
        assigned to it is a descendant of this device. Defaults to the standard deck.
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
    """
    deck = deck if deck is not None else IPrep2Deck()
    super().__init__(
      name=name,
      size_x=deck.get_absolute_size_x(),
      size_y=deck.get_absolute_size_y(),
      size_z=deck.get_absolute_size_z(),
      category="device",
      model=model if model is not None else self.__class__.__name__,
    )

    self.deck = deck
    self.driver = (
      driver
      if driver is not None
      else IPrep2Driver(
        host=host, port=port, api_key=api_key, secure=secure, follow_events=follow_events
      )
    )
    self.assign_child_resource(deck, location=Coordinate(0, 0, 0))

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
    """
    await self.driver.setup()
    self._check_zones_match()
    self.deck.apply_calibration(await self.driver.request_zone_calibration())
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
    for zone in self.deck.zone_names:
      on_instrument = bool(state.get(zone))
      in_model = self.deck.zones.get(zone) is not None
      if on_instrument and not in_model:
        differences.append(f"{zone}: the instrument holds labware, this model does not")
      elif in_model and not on_instrument:
        held = self.deck.zones[zone]
        differences.append(
          f"{zone}: this model holds {held.name if held else 'labware'}, the instrument does not"
        )
    if differences:
      logger.warning(
        "the instrument's deck and this model disagree:\n  %s", "\n  ".join(differences)
      )

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
