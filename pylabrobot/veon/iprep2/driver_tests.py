"""Driving an i.prep 2, against transports that answer without an instrument."""

import asyncio
import json
import logging
import unittest
from typing import Any, Dict, List, Optional, Union

from pylabrobot.events import EventBus, PLREvent, event_context, use_event_bus
from pylabrobot.io.http import HTTP, HTTPError
from pylabrobot.io.websocket import WebSocket
from pylabrobot.veon.iprep2.configuration_tests import CAPABILITIES, INFO
from pylabrobot.veon.iprep2.driver import IPrep2Driver, _warn_unverified
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


class _WarningCatcher(logging.Handler):
  """Keeps every warning it is given, so a test can read them all or assert there were none.

  `assertLogs` fails when nothing is logged and `assertNoLogs` arrived in Python 3.10, which this
  package does not require; a handler that keeps records answers both questions.
  """

  def __init__(self) -> None:
    super().__init__(level=logging.WARNING)
    self.records: List[logging.LogRecord] = []

  def emit(self, record: logging.LogRecord) -> None:
    self.records.append(record)

  @property
  def messages(self) -> List[str]:
    return [record.getMessage() for record in self.records]


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

  async def test_an_instrument_left_unhomed_is_described_as_that(self) -> None:
    """Not as having tips on an empty list of channels: the warning names what was reported."""
    unhomed = dict(READINESS, at_home=False)
    captured = _WarningCatcher()
    logging.getLogger("pylabrobot.veon.iprep2.driver").addHandler(captured)
    try:
      await self._driver(http=_FakeHTTP({"/system/readiness": unhomed}))
    finally:
      logging.getLogger("pylabrobot.veon.iprep2.driver").removeHandler(captured)
    (message,) = [m for m in captured.messages if "not put away" in m]
    self.assertIn("not at home", message)
    self.assertNotIn("[]", message)

  async def test_unreadable_tip_presence_is_warned_about(self) -> None:
    """`null` for `tips_attached` means the instrument could not read it, and the warning says
    so - alongside, not instead of, whatever else was reported, since a parked head can still be
    one nobody can vouch for."""
    unknown = dict(READINESS, tips_attached=None)
    captured = _WarningCatcher()
    logging.getLogger("pylabrobot.veon.iprep2.driver").addHandler(captured)
    try:
      await self._driver(http=_FakeHTTP({"/system/readiness": unknown}))
    finally:
      logging.getLogger("pylabrobot.veon.iprep2.driver").removeHandler(captured)
    (message,) = [m for m in captured.messages if "carry tips" in m]
    self.assertIn("could not read", message)
    self.assertFalse(any("not put away" in m for m in captured.messages))

  async def test_the_unverified_warning_is_accurate_and_said_once(self) -> None:
    """It says what was checked - read-only, against real instruments - rather than that nothing
    was, and a second setup does not repeat it: a warning on every reconnect gets filtered
    wholesale, taking the ones that matter with it."""
    _warn_unverified.cache_clear()
    captured = _WarningCatcher()
    logging.getLogger("pylabrobot.veon.iprep2.driver").addHandler(captured)
    try:
      driver = await self._driver()
      await driver.setup()
    finally:
      logging.getLogger("pylabrobot.veon.iprep2.driver").removeHandler(captured)
    (message,) = [m for m in captured.messages if "1218" in m]
    self.assertIn("read-only against real instruments", message)
    self.assertNotIn("not been checked", message)

  async def test_a_setup_that_fails_keeps_nothing_it_read(self) -> None:
    """An identity read before capabilities failed describes nothing, and `str` - which a
    notebook and a traceback both call - must still answer rather than raise."""
    http = _FakeHTTP({"/system/capabilities": HTTPError("GET", "/x", 500, "{}")})
    driver = IPrep2Driver(io=http, events_io=_FakeEvents())
    with self.assertRaises(IPrep2Error):
      await driver.setup()
    self.assertIn("not set up", str(driver))
    with self.assertRaises(RuntimeError):
      driver.identity

  async def test_a_failed_second_setup_does_not_mix_two_instruments(self) -> None:
    """A re-setup that reads a different identity and then fails must not leave it beside the
    first instrument's capabilities."""
    http = _FakeHTTP()
    driver = await self._driver(http=http)
    http.answers["/system/info"] = dict(INFO, name="other-unit")
    http.answers["/system/capabilities"] = HTTPError("GET", "/x", 500, "{}")
    with self.assertRaises(IPrep2Error):
      await driver.setup()
    self.assertIn("not set up", str(driver))
    with self.assertRaises(RuntimeError):
      driver.capabilities

  async def test_a_stop_during_setup_leaves_nothing_open(self) -> None:
    """`stop` can land while setup is still opening the event stream, when there is no
    connection yet for it to close. Setup then closes what it went on to open, and says so."""
    gate = asyncio.Event()
    entered = asyncio.Event()

    class _SlowToConnect(_FakeEvents):
      async def setup(self) -> None:
        entered.set()
        await gate.wait()
        await super().setup()

    events = _SlowToConnect()
    http = _FakeHTTP()
    driver = IPrep2Driver(io=http, events_io=events)
    setting_up = asyncio.ensure_future(driver.setup())
    await entered.wait()
    await driver.stop()
    gate.set()
    with self.assertRaises(RuntimeError) as caught:
      await setting_up
    self.assertIn("stop()", str(caught.exception))
    self.assertFalse(events.set_up)
    self.assertFalse(http.set_up)
    self.assertIsNone(driver._events_task)

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

  async def test_a_failure_that_is_json_but_not_an_object_is_still_ours(self) -> None:
    """A gateway answering `"Service Unavailable"` or `null` parses, and is still not an
    envelope. The caller's `except IPrep2Error` has to catch it, with the body in the message."""
    for body in ("null", '"Service Unavailable"', '[{"detail": "x"}]', "503"):
      with self.subTest(body=body):
        driver = await self._driver({"/x": HTTPError("GET", "/x", 503, body)})
        with self.assertRaises(IPrep2Error) as caught:
          await driver.request("GET", "/x")
        self.assertEqual(caught.exception.http_status, 503)
        self.assertIn(body, str(caught.exception))


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
    self.assertEqual(seen.data["device"]["name"], "device.invalid:80")

  async def test_an_event_carries_no_context_from_around_setup(self) -> None:
    """The follower is started inside whatever operation surrounded `setup`, and the instrument's
    events are not part of it: they must not all be stamped with that operation for as long as
    the stream runs."""
    seen: List[PLREvent] = []
    bus = EventBus()
    bus.subscribe(seen.append)
    events = _FakeEvents([event("tip_pickup")])
    driver = IPrep2Driver(io=_FakeHTTP(), events_io=events)
    with use_event_bus(bus):
      with event_context(operation="protocol.setup_phase", step=1):
        await driver.setup()
      await events.drained.wait()
      await driver.stop()
    (tip_pickup,) = [e for e in seen if e.name == "iprep2.tip_pickup"]
    self.assertEqual(tip_pickup.context, {})

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


class TransportAddressTests(unittest.TestCase):
  """Where the event stream goes when the HTTP transport was handed over ready-built."""

  def _io(self, base_url: str, key: Optional[str] = None) -> HTTP:
    """A transport to the instrument at the given address.

    Args:
      base_url: where its API is served.
      key: a bearer token to send, if any.

    Returns:
      The transport.
    """
    return HTTP(
      human_readable_device_name="sim",
      base_url=base_url,
      headers={} if key is None else {"Authorization": f"Bearer {key}"},
    )

  def test_the_event_stream_follows_the_transport_it_was_handed(self) -> None:
    """Not `localhost`: both connections reach the instrument the requests go to, with the same
    headers, and `host` and `port` name that instrument."""
    driver = IPrep2Driver(io=self._io("https://sim.example.com/api/v1", key="secret-token"))
    self.assertEqual(driver.events_io._url, "wss://sim.example.com/api/v1/ws/events")
    self.assertEqual(driver.events_io._headers, {"Authorization": "Bearer secret-token"})
    self.assertEqual((driver.host, driver.port, driver.secure), ("sim.example.com", 443, True))

  def test_the_event_stream_is_served_under_the_same_root(self) -> None:
    driver = IPrep2Driver(io=self._io("http://10.0.0.5:8080/sims/7/api/v1"))
    self.assertEqual(driver.events_io._url, "ws://10.0.0.5:8080/sims/7/api/v1/ws/events")
    self.assertEqual((driver.host, driver.port, driver.secure), ("10.0.0.5", 8080, False))


class CredentialTests(unittest.TestCase):
  """What the driver refuses to do with a key."""

  def test_a_key_without_tls_is_refused(self) -> None:
    """It would go on the wire in clear text, which is what a key is meant to avoid."""
    with self.assertRaises(ValueError) as caught:
      IPrep2Driver(host="iprep2.local", api_key="secret-token")
    self.assertIn("clear text", str(caught.exception))
    self.assertNotIn("secret-token", str(caught.exception))

  def test_a_key_over_tls_is_allowed(self) -> None:
    IPrep2Driver(host="iprep2.example.com", api_key="secret-token", secure=True)

  def test_no_key_needs_no_tls(self) -> None:
    """An instrument on a bench is unauthenticated, which is the common case."""
    IPrep2Driver(host="iprep2.local")
