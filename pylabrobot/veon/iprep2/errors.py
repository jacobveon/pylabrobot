"""What the i.prep 2 reports when something goes wrong, as Python exceptions.

The instrument answers a failure with an envelope carrying an `error_code` - a dotted name whose
first part names the family - alongside the HTTP status, a human-readable message, and whatever
fields that particular code carries.

Two rules come from the instrument's own error reference and shape everything here:

1. **Branch on the code, never on the message.** The message is written for a person reading a
   log and changes without notice. Nothing in this module reads one.
2. **An unfamiliar code is not a reason to fail.** New ones are added, so a code this does not
   know becomes its family's exception, and a family it does not know becomes `IPrep2Error`. What
   arrives is always raised as something a caller can catch.

There is deliberately no "retriable" flag, because the instrument does not have one: whether
retrying is safe depends on what else is on the deck, which is PyLabRobot's question rather than
the instrument's. A tip lost during pickup is the clearest case - the instrument knows the tip is
not on the channel, and cannot know where it fell.
"""

import logging
from typing import Any, Dict, Optional, Type

logger = logging.getLogger(__name__)


class IPrep2Error(Exception):
  """A failure the instrument reported.

  Attributes:
    error_code: the instrument's dotted code, e.g. `"MOTION.STALLED"`. Empty when the instrument
      answered an error without one, which an older firmware may.
    domain: the family the code belongs to, as the instrument named it.
    http_status: the status the request was answered with.
    message: what the instrument said, for a person. Do not branch on it.
    payload: the whole `data` object, including fields specific to this code - `channel`,
      `requested` and `available` on a capacity error, and so on. Read it for anything this class
      does not name, and tolerate fields you do not recognise.
  """

  def __init__(
    self,
    error_code: str,
    domain: str,
    http_status: int,
    message: str,
    payload: Dict[str, Any],
  ):
    """
    Args:
      error_code: the instrument's dotted code.
      domain: the family the code belongs to.
      http_status: the status the request was answered with.
      message: what the instrument said.
      payload: the whole `data` object from the envelope.
    """
    self.error_code = error_code
    self.domain = domain
    self.http_status = http_status
    self.message = message
    self.payload = payload
    super().__init__(f"{error_code or f'HTTP {http_status}'}: {message}")

  @property
  def channel(self) -> Optional[int]:
    """Which channel this is about, on a code that names one.

    The instrument numbers channels from 1. PyLabRobot indexes them from 0, so this is the
    instrument's number and a caller working in PyLabRobot's terms wants one less.

    Returns:
      The channel, or None when the code does not name one.
    """
    channel = self.payload.get("channel")
    return channel if isinstance(channel, int) else None


class IPrep2ValidationError(IPrep2Error):
  """The request did not satisfy the instrument's schema.

  Always the caller's fault, and always reproducible: the same request is rejected the same way
  every time, so there is nothing to retry. `payload["field"]` names the first problem.
  """


class IPrep2BusyError(IPrep2Error):
  """Something else holds the instrument.

  Temporal rather than a fault. `payload["owner"]` says what holds it and `payload["request_id"]`
  which request, so a caller can say what it is waiting for rather than only that it waited.
  """


class IPrep2MotionError(IPrep2Error):
  """A move did not arrive where it was sent.

  The instrument stops where it stopped, which no target describes, so what is on the deck and
  where the head is are both in doubt until something re-establishes them.
  """


class IPrep2TipError(IPrep2Error):
  """Something about a tip: absent where one was expected, present where none should be, or lost
  on the way to or from a channel."""


class IPrep2CapacityError(IPrep2Error):
  """More volume was asked for than the tip or channel holds.

  `payload["requested"]` and `payload["available"]` carry the two figures in the units of
  `payload["quantity"]`.
  """


class IPrep2DeckError(IPrep2Error):
  """The deck is not in a state the request can be carried out against - most often labware that
  the instrument has not been told about."""


class IPrep2LiquidLevelError(IPrep2Error):
  """Liquid level detection did not find a surface, or was asked for in a mode this instrument
  does not implement."""


class IPrep2ResourceError(IPrep2Error):
  """A protocol, run, or other stored object was not found, or is not in a state this request can
  be made against."""


class IPrep2SerialError(IPrep2Error):
  """A failure on the instrument's serial probe port, rather than in the instrument itself."""


class IPrep2BatchError(IPrep2Error):
  """Steps that were given together cannot be carried out together."""


# Which exception each family raises. The instrument's families as of events 1.6.0 / api 0.10.0;
# a family not here raises `IPrep2Error`, because a code this does not know is still a failure the
# caller has to see, and refusing to represent it would be worse than representing it plainly.
_FAMILIES: Dict[str, Type[IPrep2Error]] = {
  "BATCH": IPrep2BatchError,
  "CAPACITY": IPrep2CapacityError,
  "CHANNEL": IPrep2Error,
  "DECK": IPrep2DeckError,
  "INSTRUMENT": IPrep2BusyError,
  "LLD": IPrep2LiquidLevelError,
  "MOTION": IPrep2MotionError,
  "RESOURCE": IPrep2ResourceError,
  "SERIAL": IPrep2SerialError,
  "TIP": IPrep2TipError,
  "VALIDATION": IPrep2ValidationError,
}


def error_from_envelope(http_status: int, envelope: Dict[str, Any]) -> IPrep2Error:
  """Build the exception for an error the instrument answered with.

  Args:
    http_status: the status the request was answered with, used when the body carries no code of
      its own.
    envelope: the decoded response body. Its `data` object is where the error lives; a body that
      is not shaped like one still produces an error rather than a parsing failure, since a
      caller with a broken instrument needs the failure, not a second one.

  Returns:
    The exception to raise, of the most specific class the code's family has.
  """
  payload = envelope.get("data")
  if not isinstance(payload, dict):
    payload = {}

  error_code = str(payload.get("error_code") or "")
  domain = str(payload.get("domain") or "")
  message = str(
    payload.get("message") or envelope.get("message") or "the instrument reported an error"
  )

  family = error_code.split(".", 1)[0]
  if family and family not in _FAMILIES:
    # Worth saying once: a family that is not here is handled, but less precisely than it could
    # be, and the fix is to add it.
    logger.debug(
      "unknown i.prep 2 error family %r in %r; raising it as IPrep2Error", family, error_code
    )

  return _FAMILIES.get(family, IPrep2Error)(
    error_code=error_code,
    domain=domain,
    http_status=http_status,
    message=message,
    payload=payload,
  )
