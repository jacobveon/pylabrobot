"""What an i.prep 2 says it is.

The instrument answers its own shape in one request. `GET /system/capabilities` reports which
channels and axes exist, how far each travels, how the head is spaced, what the deck holds and
which liquid classes are loaded; `GET /system/info` reports what device it is and what it is
running. Nothing here is hard-coded from a document: every figure comes off the instrument at
setup, because two instruments of the same model do not have to agree.

That the shipped examples differ from a real machine is not hypothetical - a simulator answers
eight channels and ten axes where the published example shows four and two - so a driver that
assumed the example would be wrong before it ever reached hardware.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


def _is_number(value: Any) -> bool:
  """Whether a JSON value is a number - and not a bool, which Python counts as one."""
  return isinstance(value, (int, float)) and not isinstance(value, bool)


@dataclass(frozen=True)
class AxisTravel:
  """How far one axis moves, in mm.

  Attributes:
    minimum: the lowest position the axis accepts.
    maximum: the highest.
  """

  minimum: float
  maximum: float

  def contains(self, position: float) -> bool:
    """Whether a position is within this axis's travel.

    Args:
      position: the position to check, in mm.

    Returns:
      Whether the axis reaches it.
    """
    return self.minimum <= position <= self.maximum

  def as_tuple(self) -> Tuple[float, float]:
    """This travel as a `(minimum, maximum)` pair.

    Returns:
      The two bounds, in mm.
    """
    return (self.minimum, self.maximum)


@dataclass(frozen=True)
class PipetteCapabilities:
  """The pipetting head.

  Channel numbers are the instrument's, which start at 1. PyLabRobot indexes channels from 0, so
  the two are converted at the driver's edge rather than carried in both forms - see
  `IPrep2Driver.channel_number`.

  Attributes:
    channels: the channel numbers that exist, as the instrument numbers them.
    channel_pitch: the spacing between adjacent channels, in mm.
    max_volume: the largest volume any channel holds, in uL.
    max_rate: the fastest a channel moves liquid, in uL/s.
    tip_capacities: the tip sizes the instrument takes, in uL.
    tip_capacities_by_channel: those sizes per channel, since a channel need not take all of them.
  """

  channels: Tuple[int, ...]
  channel_pitch: Optional[float]
  max_volume: Optional[float]
  max_rate: Optional[float]
  tip_capacities: Tuple[float, ...]
  tip_capacities_by_channel: Dict[int, Tuple[float, ...]] = field(default_factory=dict)

  @property
  def num_channels(self) -> int:
    """How many channels the instrument has.

    Returns:
      The count.
    """
    return len(self.channels)


@dataclass(frozen=True)
class MotionCapabilities:
  """The axes, and how far each one travels.

  A channel's Z axis is its own: `z1` belongs to channel 1, and an instrument with eight channels
  has eight of them alongside the shared `x` and `y`. The instrument names them rather than
  implying them, so which axis serves which channel is read here and not assumed.

  Attributes:
    axes: every axis the instrument has, in the order it reported them.
    travel: how far each one moves. An axis the instrument did not give a range for is absent.
  """

  axes: Tuple[str, ...]
  travel: Dict[str, AxisTravel] = field(default_factory=dict)

  def z_axis_for_channel(self, channel: int) -> Optional[str]:
    """The Z axis belonging to a channel, as the instrument names it.

    Args:
      channel: the channel number, as the instrument numbers them.

    Returns:
      The axis name, or None on an instrument whose channels do not each have one.
    """
    name = f"z{channel}"
    return name if name in self.axes else None


@dataclass(frozen=True)
class DeckCapabilities:
  """The deck the instrument carries.

  Attributes:
    zones: the zones labware can be placed in, named as the instrument names them.
    size_x: how wide the deck is, in mm.
    size_y: how deep it is, in mm.
    size_z: how tall its working volume is, in mm.
    safe_travel: the height that is clear to travel at, in mm.
  """

  zones: Tuple[str, ...]
  size_x: Optional[float]
  size_y: Optional[float]
  size_z: Optional[float]
  safe_travel: Optional[float]


@dataclass(frozen=True)
class Capabilities:
  """Everything `GET /system/capabilities` reported, in PyLabRobot's terms.

  Static for the life of a boot and read without touching hardware, so it is asked once at setup
  and held.

  Attributes:
    pipette: the head.
    motion: the axes.
    deck: the deck.
    liquid_classes: the liquid classes loaded on the instrument. Names only - the instrument does
      not expose their parameters, so a PyLabRobot liquid class can be matched to one by name and
      no further.
  """

  pipette: PipetteCapabilities
  motion: MotionCapabilities
  deck: DeckCapabilities
  liquid_classes: Tuple[str, ...]

  @classmethod
  def from_response(cls, data: Dict[str, Any]) -> "Capabilities":
    """Build from the `data` object of a capabilities response.

    Tolerant of fields it does not recognise and of ones it does not find: the instrument adds
    fields over time, and a driver that refused an unfamiliar one would stop working on an
    instrument that had merely been updated.

    Args:
      data: the `data` object of `GET /system/capabilities`.

    Returns:
      The capabilities.
    """
    pipette = data.get("pipette") or {}
    motion = data.get("motion") or {}
    deck = data.get("deck") or {}
    dimensions = deck.get("dimensions_mm") or {}

    return cls(
      pipette=PipetteCapabilities(
        channels=tuple(pipette.get("channels") or ()),
        channel_pitch=pipette.get("channel_pitch_mm"),
        max_volume=pipette.get("max_volume_ul"),
        max_rate=pipette.get("max_rate_ul_per_s"),
        tip_capacities=tuple(pipette.get("tip_capacities_ul") or ()),
        # The instrument keys this by channel as a string, JSON having no integer keys. An entry
        # that is not a list of sizes under a channel number - `null` for a channel with no tips
        # configured, say - is left out the way an axis without a range is, not tripped over.
        tip_capacities_by_channel={
          int(channel): tuple(capacities)
          for channel, capacities in (pipette.get("tip_capacities_by_channel") or {}).items()
          if str(channel).isdigit() and isinstance(capacities, (list, tuple))
        },
      ),
      motion=MotionCapabilities(
        axes=tuple(motion.get("axes") or ()),
        # An axis is left out unless both of its bounds are numbers: half a range describes
        # nothing, and a `null` bound would fail the first `contains` rather than this line.
        travel={
          axis: AxisTravel(minimum=bounds["min_mm"], maximum=bounds["max_mm"])
          for axis, bounds in (motion.get("travel") or {}).items()
          if isinstance(bounds, dict)
          and _is_number(bounds.get("min_mm"))
          and _is_number(bounds.get("max_mm"))
        },
      ),
      deck=DeckCapabilities(
        zones=tuple(deck.get("zones") or ()),
        size_x=dimensions.get("x"),
        size_y=dimensions.get("y"),
        size_z=dimensions.get("z"),
        safe_travel=deck.get("safe_travel_mm"),
      ),
      liquid_classes=tuple(data.get("liquid_classes") or ()),
    )


@dataclass(frozen=True)
class DeviceIdentity:
  """Which instrument this is, and what it is running.

  Attributes:
    name: what the instrument calls itself.
    model: the model it reports.
    serial: its serial number.
    versions: every version it reported, as it named them - api, firmware, system, events and so
      on. Held as reported rather than picked apart, since which ones exist is the instrument's
      business and a driver that named them would go stale.
    simulated: whether this is a simulated instrument rather than hardware. Reported by the
      instrument itself, so a caller never has to infer it from an address.
  """

  name: Optional[str]
  model: Optional[str]
  serial: Optional[str]
  versions: Dict[str, str] = field(default_factory=dict)
  simulated: bool = False

  @classmethod
  def from_response(cls, data: Dict[str, Any]) -> "DeviceIdentity":
    """Build from the `data` object of a system info response.

    Args:
      data: the `data` object of `GET /system/info`.

    Returns:
      The identity.
    """
    device = data.get("device") or {}
    simulation = data.get("simulation") or {}
    return cls(
      name=device.get("name"),
      model=device.get("model"),
      serial=device.get("serial"),
      versions={
        name: str(value) for name, value in (data.get("meta") or {}).items() if value is not None
      },
      simulated=bool(simulation.get("simulated", False)),
    )


@dataclass(frozen=True)
class Readiness:
  """Whether the instrument is free, and how it was left.

  Cheap enough to ask before dispatching work, and the way to find out without either moving the
  instrument or provoking a rejection to read the refusal off.

  Attributes:
    busy: whether an operation holds the instrument.
    owner: what holds it, on an instrument that is busy.
    request_id: which request holds it.
    at_home: whether every axis is at its home position. None when the instrument did not say,
      which is not the same as saying no.
    axes_away_from_home: the axes that are not, named as the instrument names them.
    tips_attached: the channels carrying a tip, as the instrument numbers them. None when the
      instrument could not read tip presence, which it reports as `null` and not as an empty
      list - "no tips" is the dangerous wrong answer, and not knowing is not the same as it.
  """

  busy: bool
  owner: Optional[str] = None
  request_id: Optional[str] = None
  at_home: Optional[bool] = None
  axes_away_from_home: Tuple[str, ...] = ()
  tips_attached: Optional[Tuple[int, ...]] = None

  @property
  def left_dirty(self) -> bool:
    """Whether the instrument is free but was not put away.

    A finished run can leave tips on and the head mid-deck. Nothing is wrong, but the next
    operation starts from somewhere no protocol chose, so it is worth knowing before commanding
    one.

    An instrument that did not say whether it is home, or could not read whether tips are on, is
    not called dirty for it: only what it did say - tips on, axes named as away, or `at_home`
    reported false - counts. Whether tip presence is unknown is `tips_unknown`, which a caller
    that needs a clean head has to check as well as this.

    Returns:
      Whether it is free with tips on or an axis away from home.
    """
    return not self.busy and (
      bool(self.tips_attached) or bool(self.axes_away_from_home) or self.at_home is False
    )

  @property
  def tips_unknown(self) -> bool:
    """Whether the instrument could not read which channels carry a tip.

    Distinct from an empty `tips_attached`, which means it looked and found none. A caller that
    needs a clean head cannot take this as one.

    Returns:
      Whether tip presence is unknown.
    """
    return self.tips_attached is None

  def how_left(self) -> List[str]:
    """What was left undone, one phrase per signal the instrument actually reported.

    Built from what is present rather than from a fixed sentence, so an instrument that said only
    `at_home: false` is not described as having tips on an empty list of channels.

    Returns:
      The phrases, empty for an instrument that was put away or did not say.
    """
    signals: List[str] = []
    if self.tips_attached:
      signals.append(f"tips on channels {list(self.tips_attached)}")
    if self.axes_away_from_home:
      signals.append(f"axes away from home: {list(self.axes_away_from_home)}")
    elif self.at_home is False:
      signals.append("not at home")
    return signals

  @classmethod
  def from_response(cls, data: Dict[str, Any]) -> "Readiness":
    """Build from the `data` object of a readiness response.

    Args:
      data: the `data` object of `GET /system/readiness`.

    Returns:
      The readiness.
    """
    return cls(
      busy=bool(data.get("busy", False)),
      owner=data.get("owner"),
      request_id=data.get("request_id"),
      at_home=None if data.get("at_home") is None else bool(data["at_home"]),
      axes_away_from_home=tuple(data.get("axes_away_from_home") or ()),
      tips_attached=None if data.get("tips_attached") is None else tuple(data["tips_attached"]),
    )


def describe(identity: DeviceIdentity, capabilities: Capabilities) -> List[str]:
  """A short account of what was found, for the log line setup writes.

  Args:
    identity: what the instrument said it is.
    capabilities: what it said it has.

  Returns:
    One line per thing worth saying.
  """
  lines = [
    f"{identity.name or 'i.prep 2'} "
    f"(model {identity.model or 'unknown'}, serial {identity.serial or 'unknown'})"
    f"{' [simulated]' if identity.simulated else ''}",
    f"  {capabilities.pipette.num_channels} channels at "
    f"{capabilities.pipette.channel_pitch} mm pitch, up to {capabilities.pipette.max_volume} uL",
    f"  axes: {', '.join(capabilities.motion.axes)}",
    f"  deck: {len(capabilities.deck.zones)} zones, "
    f"{capabilities.deck.size_x} x {capabilities.deck.size_y} mm",
    f"  liquid classes: {', '.join(capabilities.liquid_classes) or 'none'}",
  ]
  versions = ", ".join(f"{name}={value}" for name, value in sorted(identity.versions.items()))
  if versions:
    lines.append(f"  versions: {versions}")
  return lines
