#!/usr/bin/env python3
"""Time-control validation + the FIDE-mode clock-model input scaling.

Terminology (per the product owner's definitions, kept strict):
  * TIME CONTROL = two numbers: base_time (seconds) and increment (seconds).
  * TIME MANAGER = how the engine allocates its move time (only used in FIDE
    mode; it calls the external clock model). This module supplies the SCALING
    that wraps the clock model's input/output.

Validation:
  T = base_seconds + 60 * increment
  Allowed only if T < 86400 (1 day). If T >= 86400, the caller shows:
    "Chess Amateur is too impatient to appreciate the depth of correspondence
     chess."
  base_seconds and increment are nonnegative integers. `unlimited` is a
  separate mode (no clock).

Rating time-class (OUR definition; see ratings.time_class):
  T <= 600 -> blitz; 600 < T < 3600 -> rapid; else classical.

Clock-model scaling (LICHESS definition of blitz/rapid/classical):
  The model is trained on Lichess BLITZ. Lichess classes by base + 40*inc:
     base + 40*inc  < 8 min   -> (bullet/)blitz    (no scaling needed)
     8 min <= ...   < 25 min  -> rapid             (scale down to blitz)
     25 min <= ...            -> classical         (scale down to blitz)
  When scaling is needed, we scale the WHOLE time control so that
  (base + 40*inc) maps to the Lichess 5-minute blitz reference (300 s):
     scale = (base + 40*inc) / 300
     model_base = base / scale ; model_inc = inc / scale
  The scale factor is FIXED after move 1 (computed once from the INITIAL time
  control) and reused for the rest of the game. The model's predicted think
  time is then multiplied back up by `scale` to get the real think time.

  If the initial control is already Lichess-blitz (base + 40*inc < 8 min),
  scale = 1.0 (no scaling), confirmed with the product owner.
"""

MAX_T = 86400            # 1 day, exclusive upper bound
LICHESS_BLITZ_MAX = 8 * 60      # base + 40*inc < 480s => Lichess blitz
BLITZ_REFERENCE = 300.0         # scale so (base + 40*inc) maps to 5 min
CORRESPONDENCE_MESSAGE = ("Chess Amateur is too impatient to appreciate the "
                          "depth of correspondence chess.")


class InvalidTimeControl(ValueError):
    """Raised for an out-of-range or malformed custom time control."""


def parse_base_seconds(hours, minutes, seconds):
    """Combine h/m/s into total base seconds. Each must be a nonnegative int."""
    vals = []
    for v in (hours, minutes, seconds):
        if isinstance(v, bool):
            raise InvalidTimeControl("time fields must be integers")
        try:
            iv = int(v)
        except (TypeError, ValueError):
            raise InvalidTimeControl("time fields must be integers")
        if iv < 0:
            raise InvalidTimeControl("time fields must be nonnegative")
        vals.append(iv)
    h, m, s = vals
    return h * 3600 + m * 60 + s


def validate_custom(base_seconds, increment):
    """Validate a custom time control. Returns T = base + 60*inc.

    Raises InvalidTimeControl (with CORRESPONDENCE_MESSAGE) if T >= 1 day, or a
    generic message for negatives/non-integers.
    """
    for v in (base_seconds, increment):
        if isinstance(v, bool):
            raise InvalidTimeControl("time fields must be integers")
        if not isinstance(v, int):
            raise InvalidTimeControl("time fields must be integers")
        if v < 0:
            raise InvalidTimeControl("time fields must be nonnegative")
    t = base_seconds + 60 * increment
    # A control of 0 base + 0 inc is degenerate (no time at all); disallow.
    if t <= 0:
        raise InvalidTimeControl("time control must be greater than zero")
    if t >= MAX_T:
        raise InvalidTimeControl(CORRESPONDENCE_MESSAGE)
    return t


def lichess_class(base_seconds, increment):
    """Lichess class by base + 40*inc (used only for clock-model scaling)."""
    est = base_seconds + 40 * increment
    if est < LICHESS_BLITZ_MAX:
        return "blitz"
    if est < 25 * 60:
        return "rapid"
    return "classical"


def clock_scale_factor(base_seconds, increment):
    """The FIXED scaling factor for the clock model, from the INITIAL control.

    scale = max(1.0, (base + 40*inc) / 300). If the control is already
    Lichess-blitz, (base + 40*inc) < 480 so this could be < 1.6; per the spec
    we only scale DOWN oversized controls, and leave Lichess-blitz unscaled
    (scale = 1.0).
    """
    est = base_seconds + 40 * increment
    if est < LICHESS_BLITZ_MAX:
        return 1.0
    return est / BLITZ_REFERENCE


def model_input_clocks(scale, player_clock, opponent_clock, increment):
    """Scale the LIVE clock state down into the model's blitz-sized input.

    `scale` is the fixed factor from clock_scale_factor(initial control).
    Returns (model_player_clock, model_opponent_clock, model_increment).
    """
    if scale <= 0:
        scale = 1.0
    return (player_clock / scale, opponent_clock / scale, increment / scale)


def scale_thinking_time_up(scale, model_thinking_time):
    """Scale the model's predicted (blitz-sized) think time back up to real
    seconds for this game's time control."""
    if scale <= 0:
        scale = 1.0
    return model_thinking_time * scale
