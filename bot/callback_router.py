"""Declarative callback query dispatch for Telegram inline buttons.

Each route declares a prefix, part constraints, type conversions, and the
handler method name.  The dispatcher matches routes in registration order
and calls the first match.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True, slots=True)
class CallbackRoute:
    """A single callback dispatch rule.

    Attributes:
        prefix:      First element after splitting on ':' (e.g. "approved_page").
        handler:     Method name on the bot instance (e.g. "_cb_approved_page").
        min_parts:   Minimum number of colon-separated parts (including prefix).
        max_parts:   Maximum parts (None = no upper bound).  Use None together
                     with rejoin_from for routes whose last arg may contain colons.
        answer:      Auto-answer text for the callback query (None = don't auto-answer).
        constraints: Dict mapping a part index to a set of allowed values.
                     Example: {2: {"allowed", "blocked"}} means parts[2] must be
                     one of those strings.
        int_parts:   Set of part indices that should be converted to int.
        rejoin_from: If set, parts[rejoin_from:] are re-joined with ':' into a
                     single string argument (for values that legitimately contain
                     colons, like times or channel names).
        pass_update: If True, pass the Update and context objects as extra args
                     after query (for handlers that need them).
    """
    prefix: str
    handler: str
    min_parts: int = 2
    max_parts: Optional[int] = None
    answer: Optional[str] = ""
    constraints: dict[int, frozenset[str]] = field(default_factory=dict)
    int_parts: frozenset[int] = field(default_factory=frozenset)
    rejoin_from: Optional[int] = None
    pass_update: bool = False

    def __post_init__(self) -> None:
        # max_parts defaults to min_parts when not set (exact match)
        if self.max_parts is None:
            object.__setattr__(self, "max_parts", self.min_parts)


def match_route(routes: list[CallbackRoute], parts: list[str]) -> Optional[tuple[CallbackRoute, list]]:
    """Find the first matching route and return (route, parsed_args).

    Returns None if no route matches.  parsed_args is the list of arguments
    to pass to the handler method (after the query object).
    """
    prefix = parts[0]
    n = len(parts)

    for route in routes:
        if route.prefix != prefix:
            continue
        if n < route.min_parts:
            continue
        if route.max_parts is not None and n > route.max_parts:
            # rejoin_from relaxes max_parts — allow extra parts if they'll be joined
            if route.rejoin_from is None:
                continue

        # Check value constraints
        ok = True
        for idx, allowed in route.constraints.items():
            if idx >= n or parts[idx] not in allowed:
                ok = False
                break
        if not ok:
            continue

        # Build args from parts[1:]
        args = _build_args(route, parts)
        if args is None:
            continue
        return route, args

    return None


def _build_args(route: CallbackRoute, parts: list[str]) -> Optional[list]:
    """Parse parts into handler arguments, applying int conversion and rejoin."""
    raw = parts[1:]  # drop prefix

    if route.rejoin_from is not None:
        # Rejoin from this index onward (index relative to full parts list)
        rj = route.rejoin_from
        # parts[:rj] stay separate, parts[rj:] become one joined string
        head = parts[1:rj]
        tail = ":".join(parts[rj:])
        raw = head + [tail]

    # Convert int parts (indices relative to full parts list)
    args = []
    for i, val in enumerate(raw):
        full_idx = i + 1  # index in original parts list
        if full_idx in route.int_parts:
            try:
                args.append(int(val))
            except (ValueError, TypeError):
                return None  # conversion failed → no match
        else:
            args.append(val)

    return args
