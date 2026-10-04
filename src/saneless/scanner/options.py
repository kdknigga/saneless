"""
How a SANE option's constraint is read from the device's option list.

The single place a SANE option constraint is parsed, shared by the scan child
and the parent's capability read.  It imports no python-sane.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _OptionConstraint:
    """
    What a device reports for one option, in whichever shape it used.

    ``present`` is not implied by the other two: a device may expose an option
    whose constraint saneless cannot read, and it must still be recognised as
    having that option, or the source would silently go unassigned.

    Attributes:
        present: Whether the device reports the option at all.
        values: The word list the device gave, or None if it gave another shape.
        span: The ``(min, max, step)`` the device gave, or None if it gave
            another shape.

    """

    present: bool
    values: list | None
    span: tuple[float, float, float] | None


def _is_number(value: object) -> bool:
    """
    Report whether a constraint member is a real number.

    ``bool`` is excluded deliberately: it is a subclass of ``int``, so a
    device reporting ``True`` would otherwise convert to ``1.0`` and be
    accepted as a legitimate bound.
    """
    return isinstance(value, int | float) and not isinstance(value, bool)


def _as_span(constraint: object) -> tuple[float, float, float] | None:
    """
    Read a ``(min, max, step)`` range, or None if that is not what this is.

    The constraint is device-supplied, so a malformed one yields None rather
    than a guess, and this never raises: it must not take down a capability
    query.
    """
    if not isinstance(constraint, tuple):
        # None, an unconstrained option, is a documented shape: no warning.
        return None
    if len(constraint) != 3 or not all(_is_number(member) for member in constraint):
        logger.warning(
            "Scanner reports %r for a range-constrained option, which is not a "
            "(minimum, maximum, step) triple; ignoring it rather than guessing",
            constraint,
        )
        return None
    low, high, step = constraint
    return (float(low), float(high), float(step))


def _constraint(raw_options: list[tuple], name: str) -> _OptionConstraint:
    """
    Read what a device reports for one option, in whichever shape it used.

    This is the single place any option constraint is parsed; a second copy
    tends to handle only the word-list shape.
    """
    opt = _option_tuple(raw_options, name)
    if opt is None:
        return _OptionConstraint(present=False, values=None, span=None)
    constraint = opt[8]
    if isinstance(constraint, list):
        return _OptionConstraint(present=True, values=constraint, span=None)
    return _OptionConstraint(present=True, values=None, span=_as_span(constraint))


def _option_tuple(raw_options: list[tuple], name: str) -> tuple | None:
    """
    Find one option's tuple in the device's option list.

    A SANE option tuple is ``(index, name, title, desc, type, unit, size, cap,
    constraint)``; one too short to carry all nine is skipped rather than being
    an error.
    """
    for opt in raw_options:
        if len(opt) >= 9 and opt[1] == name:
            return opt
    return None


def _option_type(raw_options: list[tuple], name: str) -> int | None:
    """
    Read an option's SANE value type, index 4 of its tuple.

    Args:
        raw_options: The device's option tuples.
        name: The hyphenated option name.

    Returns:
        The value-type code, or None if the device does not report the option.

    """
    opt = _option_tuple(raw_options, name)
    return None if opt is None else opt[4]
