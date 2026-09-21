"""Driving an i.prep 2, against transports that answer without an instrument."""

import asyncio
import json
import unittest
from typing import Any, Dict, List, Optional, Union

from pylabrobot.events import EventBus, PLREvent, use_event_bus
from pylabrobot.io.http import HTTP, HTTPError
from pylabrobot.io.websocket import WebSocket
from pylabrobot.veon.iprep2.configuration_tests import CAPABILITIES, INFO
from pylabrobot.veon.iprep2.driver import IPrep2Driver
from pylabrobot.veon.iprep2.errors import (
  IPrep2BusyError,
  IPrep2Error,
  IPrep2MotionError,
  IPrep2ValidationError,
)

READINESS: Dict[str, Any] = {
  "busy": False,
  "owner": None,
  "request_id": None,
  "at_home": True,
  "axes_away_from_home": [],
  "tips_attached": [],
}


class _FakeHTTP(HTTP):
  """Answers the paths it was given, and records what it was asked.

  Subclasses the real transport rather than standing beside it, so what the driver is handed is
  what it declares it takes, and only the one method that would reach an instrument is replaced.
  """

  def __init__(self, answers: Optional[Dict[str, Any]] = None):
    """
    Args:
      answers: the `data` object to answer each path with. A value that is an exception is raised
        instead.
    """
    super().__init__(human_readable_device_name="fake i.prep 2", base_url="http://device.invalid")
    self.answers: Dict[str, Any] = {
      "/system/info": INFO,
      "/system/capabilities": CAPABILITIES,
      "/system/readiness": READINESS,
      **(answers or {}),
    }
    self.asked: List[Any] = []
    self.set_up = False

  async def setup(self) -> None:
    self.set_up = True

  async def stop(self) -> None:
    self.set_up = False

  async def request(
    self, method: str, path: str, data: Optional[Dict[str, Any]] = None
  ) -> Dict[str, Any]:
    self.asked.append((method, path, data))
    answer = self.answers.get(path)
    if isinstance(answer, BaseException):
      raise answer
    return {"status": "success", "data": answer if answer is not None else {}}


class _FakeEvents(WebSocket):
  """Hands out the messages it was given, then waits as a live stream with nothing to say does.

  A message that is an exception is raised from `read` instead, which is how a quiet spell
  (`TimeoutError`) or a dropped link (`ConnectionError`) is staged.
  """

  def __init__(
    self,
    messages: Optional[List[Union[str, bytes, BaseException]]] = None,
    fail_to_stop: bool = False,
  ):
    """
    Args:
      messages: what `read` answers, in order.
      fail_to_stop: whether `stop` raises, as a socket that will not close does.
    """
    super().__init__(
      human_readable_device_name="fake i.prep 2 events", url="ws://device.invalid/ws"
    )
    self._messages = list(messages or [])
    self._fail_to_stop = fail_to_stop
    self.drained = asyncio.Event()
    self.set_up = False

  async def setup(self) -> None:
    self.set_up = True

  async def stop(self) -> None:
    self.set_up = False
    if self._fail_to_stop:
      raise OSError("the socket would not close")

  async def read(self, timeout: Optional[float] = None) -> bytes:
    if self._messages:
      message = self._messages.pop(0)
      if isinstance(message, BaseException):
        raise message
      return message.encode() if isinstance(message, str) else message
    self.drained.set()
    await asyncio.Event().wait()
    raise AssertionError("unreachable")


def event(name: str, **payload) -> str:
  """One event as the instrument sends it.

  Args:
    name: the event's name.
    payload: its payload.

  Returns:
    The message.
  """
  return json.dumps(
    {"event": name, "timestamp": "2026-09-18T13:54:01Z", "source": "iprep2", "payload": payload}
  )


class DriverSetupTests(unittest.IsolatedAsyncioTestCase):
  """What setup reads, and what it leaves alone."""

  async def _driver(self, http=None, events=None) -> IPrep2Driver:
    driver = IPrep2Driver(io=http or _FakeHTTP(), events_io=events or _FakeEvents())
    self.addAsyncCleanup(driver.stop)
    await driver.setup()
    return driver

  async def test_setup_reads_what_the_instrument_is(self) -> None:
    driver = await self._driver()
    self.assertEqual(driver.identity.name, "chadley")
    self.assertEqual(driver.num_channels, 8)
    self.assertEqual(driver.capabilities.deck.zones, tuple(CAPABILITIES["deck"]["zones"]))

  async def test_setup_moves_nothing(self) -> None:
    """Discovery is a read. Whatever the instrument was left holding, it is still holding."""
    http = _FakeHTTP()
    await self._driver(http=http)
    self.assertEqual([method for method, _, _ in http.asked], ["GET", "GET", "GET"])

  async def test_reading_before_setup_says_to_set_up(self) -> None:
    driver = IPrep2Driver(io=_FakeHTTP(), events_io=_FakeEvents())
    for read in (lambda: driver.identity, lambda: driver.capabilities):
      with self.assertRaises(RuntimeError) as caught:
        read()
      self.assertIn("setup()", str(caught.exception))

  async def test_a_setup_that_fails_closes_what_it_opened(self) -> None:
    """Rather than leaving a connection open with nothing holding it."""
    http = _FakeHTTP({"/system/capabilities": HTTPError("GET", "/x", 500, "{}")})
    driver = IPrep2Driver(io=http, events_io=_FakeEvents())
    with self.assertRaises(IPrep2Error):
      await driver.setup()
    self.assertFalse(http.set_up)

  async def test_a_second_setup_leaves_one_follower_not_two(self) -> None:
    """Setup is repeatable. The task following the old stream has to go when the new one starts,
    or it outlives `stop` and reports a stream as ended that nobody is reading any more."""
    driver = await self._driver()
    first = driver._events_task
    assert first is not None
    await driver.setup()
    second = driver._events_task
    self.assertIsNot(first, second)
    self.assertTrue(first.cancelled() or first.done())
    await driver.stop()
    assert second is not None
    self.assertTrue(second.done())

  async def test_a_failure_to_close_does_not_hide_why_setup_failed(self) -> None:
    """The caller needs the refusal, not the footnote about a socket on the way out."""
    http = _FakeHTTP({"/system/capabilities": HTTPError("GET", "/x", 500, "{}")})
    driver = IPrep2Driver(io=http, events_io=_FakeEvents(fail_to_stop=True))
    with self.assertLogs("pylabrobot.veon.iprep2.driver", level="WARNING"):
      with self.assertRaises(IPrep2Error):
        await driver.setup()

  async def test_the_event_stream_can_be_left_closed(self) -> None:
    """For a caller that only wants to command the instrument."""
    events = _FakeEvents()
    driver = IPrep2Driver(io=_FakeHTTP(), events_io=events, follow_events=False)
    self.addAsyncCleanup(driver.stop)
    await driver.setup()
    self.assertFalse(events.set_up)


class ChannelNumberingTests(unittest.IsolatedAsyncioTestCase):
  """The instrument counts channels from 1 and PyLabRobot indexes them from 0."""

  async def asyncSetUp(self) -> None:
    self.driver = IPrep2Driver(io=_FakeHTTP(), events_io=_FakeEvents())
    self.addAsyncCleanup(self.driver.stop)
    await self.driver.setup()

  async def test_the_two_numberings_convert_both_ways(self) -> None:
    self.assertEqual(self.driver.channel_number(0), 1)
    self.assertEqual(self.driver.channel_number(7), 8)
    self.assertEqual(self.driver.channel_index(1), 0)
    self.assertEqual(self.driver.channel_index(8), 7)

  async def test_every_channel_round_trips(self) -> None:
    """An off-by-one here is an aspirate from the wrong well, and no single-channel test finds
    one."""
    for index in range(self.driver.num_channels):
      self.assertEqual(self.driver.channel_index(self.driver.channel_number(index)), index)

  async def test_a_channel_the_instrument_does_not_have_is_refused(self) -> None:
    with self.assertRaises(ValueError):
      self.driver.channel_number(8)
    with self.assertRaises(ValueError) as caught:
      self.driver.channel_index(0)
    self.assertIn("no channel 0", str(caught.exception))


class RequestTests(unittest.IsolatedAsyncioTestCase):
  """What comes back, and what a failure becomes."""

  async def _driver(self, answers) -> IPrep2Driver:
    driver = IPrep2Driver(io=_FakeHTTP(answers), events_io=_FakeEvents())
    self.addAsyncCleanup(driver.stop)
    await driver.setup()
    return driver

  async def test_a_request_returns_the_data_inside_the_envelope(self) -> None:
    """The envelope's other fields say only that it worked, which not raising already says."""
    driver = await self._driver({"/motors/position": {"x": 1.0, "y": 2.0}})
    self.assertEqual(await driver.request("GET", "/motors/position"), {"x": 1.0, "y": 2.0})

  async def test_a_reported_failure_becomes_its_family(self) -> None:
    for status, code, expected in (
      (400, "VALIDATION.MISSING_FIELD", IPrep2ValidationError),
      (409, "INSTRUMENT.BUSY", IPrep2BusyError),
      (500, "MOTION.STALLED", IPrep2MotionError),
    ):
      with self.subTest(code=code):
        body = json.dumps({"status": "error", "data": {"error_code": code, "message": "no"}})
        driver = await self._driver({"/x": HTTPError("POST", "/x", status, body)})
        with self.assertRaises(expected):
          await driver.request("POST", "/x")

  async def test_a_failure_that_is_not_an_envelope_is_still_ours(self) -> None:
    """A proxy or a crash. The caller needs the failure, not a JSON parse error."""
    driver = await self._driver({"/x": HTTPError("GET", "/x", 502, "<html>Bad Gateway</html>")})
    with self.assertRaises(IPrep2Error) as caught:
      await driver.request("GET", "/x")
    self.assertEqual(caught.exception.http_status, 502)


class EventTests(unittest.IsolatedAsyncioTestCase):
  """Putting what the instrument pushes onto PyLabRobot's event bus."""

  async def _events(self, messages) -> List[PLREvent]:
    """Run a driver over the given messages and return the events that reached the bus.

    Args:
      messages: what the stream carries.

    Returns:
      The events, in order.
    """
    seen: List[PLREvent] = []
    bus = EventBus()
    bus.subscribe(seen.append)
    events = _FakeEvents(messages)
    with use_event_bus(bus):
      driver = IPrep2Driver(io=_FakeHTTP(), events_io=events)
      await driver.setup()
      await events.drained.wait()
      await driver.stop()
    return seen

  async def _seen(self, messages) -> List[str]:
    """The names of the events that reached the bus, in order.

    Args:
      messages: what the stream carries.

    Returns:
      The names.
    """
    return [e.name for e in await self._events(messages)]

  async def test_an_event_says_which_device_it_came_from_the_way_others_do(self) -> None:
    """`device` is a reference like every other emitter's, so a subscriber written against those
    reads this one the same way."""
    (seen,) = await self._events([event("tip_pickup")])
    self.assertEqual(seen.data["device"]["type"], "IPrep2Driver")
    self.assertEqual(seen.data["device"]["name"], "localhost:11011")

  async def test_an_event_reaches_the_bus_under_the_instruments_name(self) -> None:
    names = await self._seen([event("motor_move_started"), event("motor_move_completed")])
    self.assertEqual(names, ["iprep2.motor_move_started", "iprep2.motor_move_completed"])

  async def test_events_are_prefixed_so_their_source_is_clear(self) -> None:
    """A subscriber can tell one the instrument reported from one PyLabRobot raised itself."""
    self.assertTrue(all(n.startswith("iprep2.") for n in await self._seen([event("tip_pickup")])))

  async def test_a_malformed_event_does_not_end_the_stream(self) -> None:
    """One bad message is not a reason to stop following an instrument that is still working."""
    names = await self._seen(["{not json", event("tip_pickup"), "[]", event("tip_eject")])
    self.assertEqual(names, ["iprep2.tip_pickup", "iprep2.tip_eject"])

  async def test_an_event_without_a_name_is_dropped(self) -> None:
    names = await self._seen([json.dumps({"payload": {}}), event("uptime_tick")])
    self.assertEqual(names, ["iprep2.uptime_tick"])

  async def test_a_quiet_spell_does_not_end_the_stream(self) -> None:
    """The read is bounded so a dead link is noticed, but the bound passing is not the link
    dying: the follower goes round again and the next event still arrives. `None` would have
    meant the transport's own 30 s default, which ended the follower on a quiet half-minute."""
    names = await self._seen([TimeoutError("nothing in 60 s"), event("tip_pickup")])
    self.assertEqual(names, ["iprep2.tip_pickup"])

  async def test_the_read_is_bounded_not_unbounded(self) -> None:
    """A bound the follower resumes after, rather than `None`."""
    asked: List[Optional[float]] = []

    class _Recording(_FakeEvents):
      async def read(self, timeout: Optional[float] = None) -> bytes:
        asked.append(timeout)
        return await super().read(timeout)

    events = _Recording([event("tip_pickup")])
    driver = IPrep2Driver(io=_FakeHTTP(), events_io=events)
    await driver.setup()
    await events.drained.wait()
    await driver.stop()
    self.assertTrue(asked)
    self.assertTrue(all(t is not None and t > 0 for t in asked))

  async def test_an_unfamiliar_event_is_passed_on(self) -> None:
    """The instrument adds events; one this does not know is still worth telling a listener."""
    self.assertEqual(await self._seen([event("something_new")]), ["iprep2.something_new"])


class CredentialTests(unittest.IsolatedAsyncioTestCase):
  """What the driver refuses to do with a key.

  Asynchronous although nothing here awaits: building a driver builds a `pylabrobot.io.HTTP`,
  which constructs an asyncio primitive in its own constructor, and on Python 3.9 that needs a
  loop to exist. Running these inside one keeps the test about credentials rather than about that.
  """

  async def test_a_key_without_tls_is_refused(self) -> None:
    """It would go on the wire in clear text, which is what a key is meant to avoid."""
    with self.assertRaises(ValueError) as caught:
      IPrep2Driver(host="iprep2.local", api_key="secret-token")
    self.assertIn("clear text", str(caught.exception))
    self.assertNotIn("secret-token", str(caught.exception))

  async def test_a_key_over_tls_is_allowed(self) -> None:
    IPrep2Driver(host="iprep2.example.com", api_key="secret-token", secure=True)

  async def test_no_key_needs_no_tls(self) -> None:
    """An instrument on a bench is unauthenticated, which is the common case."""
    IPrep2Driver(host="iprep2.local")
