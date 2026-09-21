"""Programmatic control of an i.prep 2.

The instrument runs its own service and answers over HTTP, so this drives it by request rather
than by firmware command: there is no framing to assemble, no reply to route, and no unit to
convert - the API already speaks millimetres, microlitres and microlitres per second, which are
PyLabRobot's own units.

What it does instead is ask the instrument what it is. `GET /system/capabilities` reports every
channel, axis, travel range, zone and liquid class in one request, so discovery is a read rather
than a survey, and nothing here carries a figure that a document rather than the instrument
supplied.

Alongside the request surface the instrument pushes a stream of events - a move starting and
finishing, a tip picked up or ejected, a channel's volume changing. This opens that stream and
puts each event on PyLabRobot's event bus, so the model can follow the machine without polling
it.
"""

import asyncio
import json
import logging
from typing import Any, Dict, Mapping, Optional

from pylabrobot.events import device_reference, emit_event
from pylabrobot.io.http import HTTP, HTTPError
from pylabrobot.io.websocket import WebSocket
from pylabrobot.veon.iprep2.configuration import (
  Capabilities,
  DeviceIdentity,
  Readiness,
  describe,
)
from pylabrobot.veon.iprep2.errors import error_from_envelope

logger = logging.getLogger(__name__)

# Where the instrument's API and its event stream live under whatever host it is reached at.
API_PATH = "/api/v1"
EVENTS_PATH = "/api/v1/ws/events"

# The default port an i.prep 2 serves on.
DEFAULT_PORT = 11011

# What every event this emits is called on PyLabRobot's bus, before the instrument's own name for
# it. Prefixed rather than passed through so a subscriber can tell an event the instrument
# reported from one PyLabRobot raised itself.
EVENT_PREFIX = "iprep2"

# How long to wait for one event before checking the stream is still there. Not a fault when it
# passes: an instrument with nothing to say is not broken, so the follower goes round again. The
# instrument does send an `uptime_tick` every 10 s, so on a live link this never actually elapses,
# but nothing here depends on that.
EVENT_READ_TIMEOUT = 60.0


class IPrep2Driver:
  """Drives an i.prep 2 over its HTTP API and event stream.

  Everything the instrument can be asked or told goes through here. What is where, and which of
  it a command is about, is the device tier's business - see `IPrep2Device`.
  """

  def __init__(
    self,
    host: str = "localhost",
    port: int = DEFAULT_PORT,
    api_key: Optional[str] = None,
    secure: bool = False,
    timeout: float = 300.0,
    follow_events: bool = True,
    io: Optional[HTTP] = None,
    events_io: Optional[WebSocket] = None,
  ):
    """
    Args:
      host: the instrument's hostname or address.
      port: the port it serves on.
      api_key: a bearer token, for an instrument that requires one. An instrument on a bench is
        unauthenticated and wants None; one reachable over a public address is not. Sent as an
        `Authorization` header, which `pylabrobot.io` keeps out of its logs and captures, and
        refused over an unencrypted connection - so a key requires `secure`.
      secure: whether to reach the instrument over TLS, i.e. `https://` and `wss://`.
      timeout: how long to wait for a request, in seconds. Long by default because the instrument
        answers an operation when it has finished it, not when it has accepted it, so a transfer's
        request is open for as long as the transfer takes.
      follow_events: whether to open the instrument's event stream and put what arrives on
        PyLabRobot's event bus. Off means the model only knows what this driver did itself.
      io: an already-built HTTP transport to use instead of one built from the arguments above.
      events_io: likewise for the event stream.

    Raises:
      ValueError: If an api_key is given without `secure`, since it would go in clear text.
    """
    if api_key is not None and not secure:
      raise ValueError(
        "an api_key over an unencrypted connection would go on the wire in clear text; "
        "pass secure=True, or reach the instrument without a key"
      )

    self.host = host
    self.port = port
    self._secure = secure
    self._follow_events = follow_events
    headers: Mapping[str, str] = {} if api_key is None else {"Authorization": f"Bearer {api_key}"}

    scheme = "https" if secure else "http"
    self.io = io or HTTP(
      human_readable_device_name=f"i.prep 2 at {host}:{port}",
      base_url=f"{scheme}://{host}:{port}{API_PATH}",
      headers=headers,
      timeout=timeout,
    )
    self.events_io = events_io or WebSocket(
      human_readable_device_name=f"i.prep 2 events at {host}:{port}",
      url=f"{'wss' if secure else 'ws'}://{host}:{port}{EVENTS_PATH}",
      headers=headers,
    )

    # Read at setup and held: the instrument's own account of what it is. None before that, and a
    # property below raises rather than letting a caller work from nothing.
    self._identity: Optional[DeviceIdentity] = None
    self._capabilities: Optional[Capabilities] = None
    self._events_task: Optional[asyncio.Task[None]] = None
    self._connected = False

  # ----------------------------------------
  # What the instrument turned out to be
  # ----------------------------------------

  @property
  def identity(self) -> DeviceIdentity:
    """Which instrument this is, and what it is running.

    Returns:
      The identity read at setup.

    Raises:
      RuntimeError: If setup has not run.
    """
    if self._identity is None:
      raise RuntimeError("nothing has been read off this instrument; call `setup()` first")
    return self._identity

  @property
  def capabilities(self) -> Capabilities:
    """What the instrument reported it has.

    Returns:
      The capabilities read at setup.

    Raises:
      RuntimeError: If setup has not run.
    """
    if self._capabilities is None:
      raise RuntimeError("nothing has been read off this instrument; call `setup()` first")
    return self._capabilities

  @property
  def num_channels(self) -> int:
    """How many pipetting channels the instrument has.

    Returns:
      The count, as the instrument reported it.
    """
    return self.capabilities.pipette.num_channels

  # ----------------------------------------
  # Channel numbering
  # ----------------------------------------
  # The instrument numbers channels from 1 and PyLabRobot indexes them from 0. The conversion
  # lives here and nowhere else: an off-by-one between the two is an aspirate from the wrong well,
  # which no test of a single channel would catch.

  def channel_number(self, index: int) -> int:
    """The instrument's number for a channel PyLabRobot indexes.

    Args:
      index: the channel's PyLabRobot index, from 0.

    Returns:
      The instrument's channel number, from 1.

    Raises:
      ValueError: If the instrument has no such channel.
    """
    channels = self.capabilities.pipette.channels
    if not 0 <= index < len(channels):
      raise ValueError(
        f"channel index {index} is outside this instrument's {len(channels)} channels"
      )
    return channels[index]

  def channel_index(self, number: int) -> int:
    """PyLabRobot's index for a channel the instrument numbered.

    Args:
      number: the instrument's channel number, from 1.

    Returns:
      The PyLabRobot index, from 0.

    Raises:
      ValueError: If the instrument has no such channel.
    """
    channels = self.capabilities.pipette.channels
    if number not in channels:
      raise ValueError(f"this instrument has no channel {number}; it has {list(channels)}")
    return channels.index(number)

  # ----------------------------------------
  # Requests
  # ----------------------------------------

  async def request(
    self, method: str, path: str, body: Optional[Dict[str, Any]] = None
  ) -> Dict[str, Any]:
    """Make one request, and return what the instrument answered with.

    The instrument wraps every answer in an envelope; this returns the `data` inside it, since the
    envelope's other fields say only that the request succeeded, which is what not raising says.

    A request is answered when the operation it asked for has finished, not when it was accepted,
    so a long operation holds its request open for as long as it runs.

    Args:
      method: the HTTP method.
      path: the path under the API root, e.g. `"/system/info"`.
      body: the JSON body, for a method that takes one.

    Returns:
      The `data` object of the response, or an empty dict where the instrument answered without
      one.

    Raises:
      IPrep2Error: If the instrument reported a failure, of the class its error family names.
    """
    try:
      envelope = await self.io.request(method, path, body)
    except HTTPError as exc:
      try:
        reported = json.loads(exc.body)
      except ValueError:
        # Not an envelope at all - a proxy or a crash rather than the instrument answering. Still
        # raised as one of ours, because what a caller needs is the failure, not a parse error.
        reported = {"message": exc.body}
      raise error_from_envelope(exc.status, reported) from exc

    data = envelope.get("data")
    return data if isinstance(data, dict) else {}

  # ----------------------------------------
  # Reads
  # ----------------------------------------

  async def request_identity(self) -> DeviceIdentity:
    """Ask the instrument which one it is, and what it is running.

    Returns:
      The identity.
    """
    return DeviceIdentity.from_response(await self.request("GET", "/system/info"))

  async def request_capabilities(self) -> Capabilities:
    """Ask the instrument what it has.

    Static for the life of the instrument's boot and touching no hardware, so setup asks once and
    holds the answer.

    Returns:
      The capabilities.
    """
    return Capabilities.from_response(await self.request("GET", "/system/capabilities"))

  async def request_readiness(self) -> Readiness:
    """Ask whether the instrument is free, and how it was left.

    Read-only and cheap enough to ask before dispatching work, which is what it is for: the
    alternative is to send a command and read the refusal, which means either moving the
    instrument or provoking an error to tell the two apart.

    Returns:
      The readiness.
    """
    return Readiness.from_response(await self.request("GET", "/system/readiness"))

  async def request_deck_state(self) -> Dict[str, Any]:
    """Ask what labware the instrument believes is on its deck.

    Returns:
      One entry per zone, empty for a zone holding nothing.
    """
    return await self.request("GET", "/deck/state")

  async def request_zone_calibration(self) -> Dict[str, Dict[str, float]]:
    """Ask how each zone has been calibrated.

    The offsets an operator measured on the bench, per zone, in mm. They are the instrument's and
    not PyLabRobot's: the deck definition says where a zone nominally is, and this says where this
    instrument found it.

    Returns:
      The offset per zone, as `{"Zone1": {"x": .., "y": .., "z": ..}}`.
    """
    zones = (await self.request("GET", "/calibration/zones")).get("zones")
    return zones if isinstance(zones, dict) else {}

  # ----------------------------------------
  # Session
  # ----------------------------------------

  async def setup(self) -> None:
    """Connect, find out what the instrument is, and start following what it does.

    Reads only. Nothing moves, and nothing about the instrument's state is changed - what it was
    left holding, and where, is reported rather than tidied, since putting an instrument away is a
    decision about what is on the deck and PyLabRobot has not seen the deck yet.

    Repeatable: a second call re-reads the instrument and reopens the stream.

    Raises:
      IPrep2Error: If the instrument would not say what it is. Whatever was opened is closed
        again first.
    """
    logger.warning(
      "The PyLabRobot i.prep 2 integration has not been checked against physical hardware. "
      "Treat what it does as unverified, and please report back once it has been - "
      "https://github.com/PyLabRobot/pylabrobot/issues/1218"
    )

    if not self._connected:
      await self.io.setup()
      self._connected = True

    try:
      self._identity = await self.request_identity()
      self._capabilities = await self.request_capabilities()
      readiness = await self.request_readiness()

      if self._follow_events:
        await self._start_following_events()
    except BaseException:
      await self._stop_quietly()
      raise

    for line in describe(self._identity, self._capabilities):
      logger.info("%s", line)
    if readiness.busy:
      logger.warning(
        "the instrument is busy: %s holds it (request %s)", readiness.owner, readiness.request_id
      )
    elif readiness.left_dirty:
      logger.warning(
        "the instrument was left with tips on channels %s and these axes away from home: %s",
        list(readiness.tips_attached),
        list(readiness.axes_away_from_home),
      )

  async def stop(self) -> None:
    """Stop following the instrument and close the connections.

    Leaves the instrument exactly as it is. Nothing is homed and no tip is ejected: what is safe
    to do with a tip depends on what is under it.
    """
    await self._stop_following_events()
    if self._connected:
      await self.io.stop()
      self._connected = False

  async def _stop_quietly(self) -> None:
    """Stop, without letting a failure in stopping hide what went wrong before it.

    For the error paths of `setup`: what the caller needs is why setup failed, and a socket that
    would not close on the way out is a footnote to that, not a replacement for it.
    """
    try:
      await self.stop()
    except Exception as exc:
      logger.warning("could not close the connections after a failed setup: %r", exc)

  # ----------------------------------------
  # The event stream
  # ----------------------------------------

  async def _start_following_events(self) -> None:
    """Open the instrument's event stream and start putting what arrives on the event bus.

    A task already following it is stopped first, so a repeated `setup` leaves one follower and
    not two: the old one would otherwise outlive `stop`, and report the stream it was reading as
    ended when the new one is following fine.
    """
    await self._cancel_follower()
    await self.events_io.setup()
    self._events_task = asyncio.create_task(self._follow())

  async def _stop_following_events(self) -> None:
    """Stop following the stream and close it."""
    await self._cancel_follower()
    await self.events_io.stop()

  async def _cancel_follower(self) -> None:
    """End the task reading the stream, if there is one, and wait for it to go."""
    if self._events_task is not None:
      self._events_task.cancel()
      await asyncio.gather(self._events_task, return_exceptions=True)
      self._events_task = None

  async def _follow(self) -> None:
    """Read the instrument's events until the stream ends, emitting each one.

    Runs for as long as the stream does. Nothing awaits this task, so what ends it is logged here
    rather than raised: the stream ending means the model stops following the instrument, which a
    caller needs to know about but which is not a failure of whatever they were doing.
    """
    try:
      while True:
        # A quiet spell is not a fault, so the wait is bounded and then simply resumed. It is not
        # unbounded: `None` would mean the transport's own default, which is 30 s and would end
        # the follower on the first quiet half-minute - and a stream that has actually ended
        # raises ConnectionError, which is what the clause below is for.
        try:
          raw = await self.events_io.read(timeout=EVENT_READ_TIMEOUT)
        except TimeoutError:
          logger.debug("no event from the i.prep 2 in %.0f s; still listening", EVENT_READ_TIMEOUT)
          continue
        self._emit(raw)
    except asyncio.CancelledError:
      raise
    except Exception as exc:
      logger.warning(
        "stopped following the i.prep 2's events: %r. The resource model will no longer follow "
        "what the instrument does until `setup()` runs again.",
        exc,
      )

  def _emit(self, raw: bytes) -> None:
    """Put one event from the instrument on PyLabRobot's event bus.

    An event that cannot be read is logged and dropped rather than ending the stream: one
    malformed message is not a reason to stop following an instrument that is still working, and
    an event this does not recognise is still worth passing on to whoever is listening.

    Args:
      raw: the message as it arrived.
    """
    try:
      event = json.loads(raw)
    except ValueError:
      logger.warning("could not read an event from the i.prep 2: %r", raw[:200])
      return
    if not isinstance(event, dict):
      logger.warning("an i.prep 2 event was not an object: %r", raw[:200])
      return

    name = event.get("event")
    if not isinstance(name, str):
      logger.warning("an i.prep 2 event did not say what it was: %r", raw[:200])
      return

    emit_event(
      f"{EVENT_PREFIX}.{name}",
      device=device_reference(self, name=f"{self.host}:{self.port}"),
      timestamp=event.get("timestamp"),
      source=event.get("source"),
      payload=event.get("payload"),
    )

  def __str__(self) -> str:
    identity = self._identity
    where = f"{self.host}:{self.port}"
    if identity is None:
      return f"IPrep2Driver({where}, not set up)"
    return f"IPrep2Driver({identity.name or 'i.prep 2'} at {where}, {self.num_channels} channels)"
