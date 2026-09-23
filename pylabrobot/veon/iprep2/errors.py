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

Alongside the code the instrument sends `inherits`: the code's ancestors, most specific first, so
that a whole family can be handled without matching every leaf. The reference says to prefer
matching one of those to matching every leaf, and the class chosen here follows the same lineage -
a leaf this module has never heard of that inherits `INSTRUMENT.BUSY` is still a busy error.

There is deliberately no "retriable" flag, because the instrument does not have one: whether
retrying is safe depends on what else is on the deck, which is PyLabRobot's question rather than
the instrument's. A tip lost during pickup is the clearest case - the instrument knows the tip is
not on the channel, and cannot know where it fell.
"""

import logging
from typing import Any, Dict, Optional, Tuple, Type

logger = logging.getLogger(__name__)


class IPrep2Error(Exception):
  """A failure the instrument reported.

  Attributes:
    error_code: the instrument's dotted code, e.g. `"MOTION.STALLED"`. Empty when the instrument
      answered an error without one, which an older firmware may.
    domain: the family the code belongs to, as the instrument named it.
    inherits: the code's ancestors, most specific first, as the instrument listed them. Match one
      of these with `is_a` to handle a whole family without naming every leaf.
    http_status: the status the request was answered with.
    message: what the instrument said, for a person. Do not branch on it.
    payload: the whole `data` object as it arrived, including whatever this code carries beyond
      the fields above. The instrument documents `field` and `detail` on a validation failure,
      `operation` and `channels` on a per-channel one, and `uncertain` for what it can no longer
      vouch for after a failure; a code may carry more. Read it for anything this class does not
      name, and tolerate fields you do not recognise. `channels` arrives keyed by strings, as
      JSON keys are; the `channels` property keys it by number.
  """

  def __init__(
    self,
    error_code: str,
    domain: str,
    http_status: int,
    message: str,
    payload: Dict[str, Any],
    inherits: Tuple[str, ...] = (),
  ):
    """
    Args:
      error_code: the instrument's dotted code.
      domain: the family the code belongs to.
      http_status: the status the request was answered with.
      message: what the instrument said.
      payload: the whole `data` object from the envelope.
      inherits: the code's ancestors, most specific first.
    """
    self.error_code = error_code
    self.domain = domain
    self.http_status = http_status
    self.message = message
    self.payload = payload
    self.inherits = inherits
    super().__init__(f"{error_code or f'HTTP {http_status}'}: {message}")

  def __reduce__(self) -> Tuple[Any, ...]:
    """Rebuild from every field rather than from the message alone.

    `Exception` pickles and copies itself by calling the class with `args`, which holds only the
    formatted message and so cannot satisfy this constructor.

    Returns:
      The class and the arguments that rebuild this error.
    """
    return (
      self.__class__,
      (self.error_code, self.domain, self.http_status, self.message, self.payload, self.inherits),
    )

  def is_a(self, code: str) -> bool:
    """Whether this error is the given code, or descends from it.

    Args:
      code: an error code, e.g. `"INSTRUMENT.BUSY"`.

    Returns:
      Whether the code is this error's own or one of its ancestors.
    """
    return self.error_code == code or code in self.inherits

  @property
  def channel(self) -> Optional[int]:
    """Which channel this is about, on a code that names one as `channel`.

    The instrument numbers channels from 1. PyLabRobot indexes them from 0, so this is the
    instrument's number and a caller working in PyLabRobot's terms wants one less.

    A failure across several channels does not name one: see `channels`.

    Returns:
      The channel, or None when the code does not name one.
    """
    channel = self.payload.get("channel")
    # `bool` is an `int` to Python, and a JSON `true` is not a channel.
    return channel if isinstance(channel, int) and not isinstance(channel, bool) else None

  @property
  def channels(self) -> Dict[int, Any]:
    """Every selected channel's outcome, on a per-channel failure.

    The instrument reports every channel it was asked to use, not only the ones that failed, so a
    partial failure is visible rather than inferred from absence. Each outcome carries an
    `outcome` of `ok`, `failed` or `not_attempted`, and whatever the code adds.

    Keyed by the instrument's channel number, from 1, as an `int`: JSON carries the keys as
    strings, and a lookup by number would otherwise miss every one.

    Returns:
      The outcome per channel, empty when the code reports none.
    """
    reported = self.payload.get("channels")
    if not isinstance(reported, dict):
      return {}
    return {int(key): outcome for key, outcome in reported.items() if str(key).isdigit()}


class IPrep2ValidationError(IPrep2Error):
  """The request did not satisfy the instrument's schema.

  Always the caller's fault, and always reproducible: the same request is rejected the same way
  every time, so there is nothing to retry. `payload["field"]` names the first problem.
  """


class IPrep2InstrumentError(IPrep2Error):
  """The instrument as a whole is not in a state to take the request - not homed, stopped, or
  otherwise unready - as distinct from something wrong with what was asked of it."""


class IPrep2BusyError(IPrep2InstrumentError):
  """Something else holds the instrument.

  Temporal rather than a fault, which is why it is its own class within the instrument family:
  a caller that waits this one out must not wait out an instrument that is stopped or unhomed.
  `payload["owner"]` says what holds it and `payload["request_id"]` which request, so a caller
  can say what it is waiting for rather than only that it waited.
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

  What the code carries beyond the message is not fixed by the instrument's error reference;
  whatever it sent is in `payload`.
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
  "INSTRUMENT": IPrep2InstrumentError,
  "LLD": IPrep2LiquidLevelError,
  "MOTION": IPrep2MotionError,
  "RESOURCE": IPrep2ResourceError,
  "SERIAL": IPrep2SerialError,
  "TIP": IPrep2TipError,
  "VALIDATION": IPrep2ValidationError,
}


# Whole codes that pick a class of their own. Busy is the one a caller handles differently from
# everything else in its family - by waiting - so it is named. The others have no dot, and so no
# family to be found by: `VALIDATION_ERROR` is what the instrument's own provisioning endpoints
# send, and `SERVICE_UNAVAILABLE` is the generic code it fills in for a 503, which it answers when
# the instrument cannot yet take a request at all.
_CODES: Dict[str, Type[IPrep2Error]] = {
  "INSTRUMENT.BUSY": IPrep2BusyError,
  "VALIDATION_ERROR": IPrep2ValidationError,
  "SERVICE_UNAVAILABLE": IPrep2InstrumentError,
}


def _is_known(code: str) -> bool:
  """Whether a code picks a class here, as a whole code or by its family.

  Args:
    code: an error code.

  Returns:
    Whether it is named in `_CODES` or its family is in `_FAMILIES`.
  """
  return code in _CODES or code.split(".", 1)[0] in _FAMILIES


def _class_for(lineage: Tuple[str, ...]) -> Type[IPrep2Error]:
  """The most specific class any code in a lineage names.

  A named code anywhere in the lineage beats a family anywhere in it, since the named codes are
  the ones a caller handles differently from the rest of their family - a leaf that inherits
  `INSTRUMENT.BUSY` is something to wait out, whatever its own family is called.

  Args:
    lineage: the error's own code first, then its ancestors, most specific first.

  Returns:
    The class, `IPrep2Error` when nothing in the lineage is known.
  """
  for code in lineage:
    if code in _CODES:
      return _CODES[code]
  for code in lineage:
    family = code.split(".", 1)[0]
    if family in _FAMILIES:
      return _FAMILIES[family]
  return IPrep2Error


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
  reported_inherits = payload.get("inherits")
  inherits = tuple(
    code
    for code in (reported_inherits if isinstance(reported_inherits, list) else ())
    if isinstance(code, str)
  )
  # A body that is not an envelope may still say something: the instrument's own 404 for an
  # unknown path is `{"error": "Not Found"}`, which is worth more than a stock phrase.
  message = str(
    payload.get("message")
    or envelope.get("message")
    or envelope.get("error")
    or "the instrument reported an error"
  )

  lineage = (error_code, *inherits) if error_code else inherits
  cls = _class_for(lineage)
  if error_code and not any(_is_known(code) for code in lineage):
    # Worth saying: a lineage with nothing known in it is handled, but less precisely than it
    # could be, and the fix is to add the family. Judged by the families rather than by the class
    # chosen, since a known family may deliberately map to the base class.
    logger.debug(
      "no known i.prep 2 error family in %r (inherits %r); raising it as IPrep2Error",
      error_code,
      list(inherits),
    )

  return cls(
    error_code=error_code,
    domain=domain,
    http_status=http_status,
    message=message,
    payload=payload,
    inherits=inherits,
  )
