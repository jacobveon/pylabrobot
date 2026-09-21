"""Turning what the instrument reported into something a caller can catch."""

import unittest

from pylabrobot.veon.iprep2.errors import (
  IPrep2BusyError,
  IPrep2CapacityError,
  IPrep2Error,
  IPrep2InstrumentError,
  IPrep2MotionError,
  IPrep2TipError,
  IPrep2ValidationError,
  error_from_envelope,
)


def envelope(**data) -> dict:
  """An error envelope carrying the given data.

  Args:
    data: the fields of the `data` object.

  Returns:
    The envelope.
  """
  return {"status": "error", "data": data, "message": data.get("message", "")}


class ErrorFromEnvelopeTests(unittest.TestCase):
  """Which exception a reported failure becomes."""

  def test_a_family_picks_the_exception(self) -> None:
    """The first part of the code names the family, and the family names the class."""
    for code, expected in (
      ("VALIDATION.MISSING_FIELD", IPrep2ValidationError),
      ("INSTRUMENT.BUSY", IPrep2BusyError),
      ("MOTION.STALLED", IPrep2MotionError),
      ("TIP.PICKUP_FAILED", IPrep2TipError),
      ("CAPACITY.EXCEEDED", IPrep2CapacityError),
    ):
      with self.subTest(code=code):
        raised = error_from_envelope(400, envelope(error_code=code, message="x"))
        self.assertIsInstance(raised, expected)
        self.assertEqual(raised.error_code, code)

  def test_only_busy_is_busy(self) -> None:
    """The instrument family holds more than `BUSY`, and a caller that waits out a busy instrument
    must not wait out one that is stopped or unhomed. Busy is still an instrument error, so one
    `except IPrep2InstrumentError` still catches the whole family."""
    busy = error_from_envelope(409, envelope(error_code="INSTRUMENT.BUSY"))
    self.assertIsInstance(busy, IPrep2BusyError)
    self.assertIsInstance(busy, IPrep2InstrumentError)
    for code in ("INSTRUMENT.NOT_HOMED", "INSTRUMENT.ESTOP", "INSTRUMENT.SOMETHING_NEW"):
      with self.subTest(code=code):
        raised = error_from_envelope(409, envelope(error_code=code))
        self.assertIsInstance(raised, IPrep2InstrumentError)
        self.assertNotIsInstance(raised, IPrep2BusyError)

  def test_an_unknown_family_is_still_raised(self) -> None:
    """New codes are added, and one this does not know is still a failure the caller has to see."""
    raised = error_from_envelope(500, envelope(error_code="GRIPPER.JAMMED", message="stuck"))
    self.assertIsInstance(raised, IPrep2Error)
    self.assertEqual(raised.error_code, "GRIPPER.JAMMED")

  def test_a_body_with_no_code_is_still_raised(self) -> None:
    """A proxy or a crash rather than the instrument answering. The caller needs the failure."""
    raised = error_from_envelope(502, {"message": "Bad Gateway"})
    self.assertIsInstance(raised, IPrep2Error)
    self.assertEqual(raised.http_status, 502)
    self.assertIn("Bad Gateway", str(raised))

  def test_the_instruments_own_404_keeps_its_text(self) -> None:
    """An unknown path is answered `{"error": "Not Found"}` - not an envelope, but it says
    something, and that beats a stock phrase. Read off a real instrument."""
    raised = error_from_envelope(404, {"error": "Not Found"})
    self.assertIn("Not Found", str(raised))

  def test_a_body_that_is_not_an_envelope_is_still_raised(self) -> None:
    """Rather than failing to parse, which would replace the failure with a second one."""
    raised = error_from_envelope(500, {"data": "not an object"})
    self.assertIsInstance(raised, IPrep2Error)
    self.assertEqual(raised.payload, {})

  def test_the_code_specific_fields_are_kept(self) -> None:
    """Which fields a code carries depends on the code, so the whole payload is kept rather than
    the few this happens to name."""
    raised = error_from_envelope(
      422,
      envelope(
        error_code="CAPACITY.EXCEEDED",
        domain="capacity",
        message="too much",
        quantity="volume_ul",
        requested=206.0,
        available=200.0,
        channel=3,
      ),
    )
    self.assertEqual(raised.payload["requested"], 206.0)
    self.assertEqual(raised.payload["available"], 200.0)
    self.assertEqual(raised.domain, "capacity")

  def test_the_channel_is_the_instruments_number(self) -> None:
    """From 1, as the instrument counts. A caller working in PyLabRobot's indexing wants one less,
    and this says so rather than converting silently."""
    raised = error_from_envelope(422, envelope(error_code="TIP.PICKUP_FAILED", channel=3))
    self.assertEqual(raised.channel, 3)

  def test_a_code_that_names_no_channel_has_none(self) -> None:
    self.assertIsNone(error_from_envelope(400, envelope(error_code="DECK.NOT_CONFIGURED")).channel)

  def test_every_exception_is_catchable_as_the_base(self) -> None:
    """One `except IPrep2Error` catches whatever the instrument reports."""
    for code in ("VALIDATION.X", "MOTION.STALLED", "SOMETHING.NEW", ""):
      with self.subTest(code=code):
        with self.assertRaises(IPrep2Error):
          raise error_from_envelope(400, envelope(error_code=code))

  def test_the_message_is_carried_but_not_branched_on(self) -> None:
    """It is written for a person and changes; it belongs in the string, not in a comparison."""
    raised = error_from_envelope(400, envelope(error_code="MOTION.STALLED", message="y stalled"))
    self.assertIn("MOTION.STALLED", str(raised))
    self.assertIn("y stalled", str(raised))
