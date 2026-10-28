"""Calibration: what the sensors read at rest, how the handlebars turn and how
far the levers move. This describes the hardware; how it feels in a game is set
per profile (see profiles.py).

The procedures are generators: prime them with next(), then send them
(sample, dt) pairs until they return the finished Calibration (as
StopIteration.value). They report through `say(text, step=None)`: instructions
come with a step label such as "Step 1 of 4", results without one."""
import dataclasses
import json
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np

from . import steering

PATH = Path(__file__).resolve().parent.parent / "calibration.json"
SWEEP_PATH = PATH.with_name("calibration_sweep.npz")  # raw sweep data, for tuning
BRAKE_NAMES = ("left", "right")
BRAKE_SQUEEZES = 3
BRAKE_SQUEEZE_MV = 80   # change that counts as the lever being squeezed
BRAKE_RELEASED_MV = 40  # within this of rest counts as released again
STAGE_TIMEOUT = 180.0   # seconds of data


@dataclass
class Calibration:
    steer_axis: list        # unit vector in sensor axes; rotation about it to the right is positive
    steer_right_deg: float  # furthest right turn in the calibration sweep
    steer_left_deg: float   # furthest left turn in the sweep (as a positive number)
    gravity: list           # accelerometer reading (g) with the bars centred
    brake_rest_mv: list     # [left, right] with the levers released
    brake_full_mv: list     # [left, right] with the levers firmly squeezed
    gravity_slope: float = None   # gravity-angle fit (see steering.fit_gravity_angle)
    gravity_offset: float = None
    brake_channels: list = (0, 1)  # which sensor input (A=0, B=1) is on the [left, right] lever

    def save(self, path=PATH):
        path.write_text(json.dumps(asdict(self), indent=2))

    @classmethod
    def load(cls, path=PATH):
        """The saved calibration, or None if there isn't one yet."""
        if not path.exists():
            return None
        fields = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in json.loads(path.read_text()).items() if k in fields})

    def lever_mv(self, brakes_mv):
        """The raw sensor readings in lever order: (left, right)."""
        return tuple(brakes_mv[c] for c in self.brake_channels)

    def levers(self, brakes_mv):
        """Lever positions (left, right), each from 0 (released) to 1 (firmly squeezed)."""
        out = []
        for i, mv in enumerate(self.lever_mv(brakes_mv)):
            rest, full = self.brake_rest_mv[i], self.brake_full_mv[i]
            out.append(min(max((mv - rest) / (full - rest), 0.0), 1.0))
        return tuple(out)

    def steering_tilt_deg(self):
        g = np.asarray(self.gravity) / np.linalg.norm(self.gravity)
        return float(np.degrees(np.arccos(abs(g @ np.asarray(self.steer_axis)))))


class CalibrationError(Exception):
    pass


class _Timeout:
    def __init__(self, what):
        self.what, self.elapsed = what, 0.0

    def add(self, dt):
        self.elapsed += dt
        if self.elapsed > STAGE_TIMEOUT:
            raise CalibrationError(f"Timed out waiting for {self.what}. Try again.")


def _measure_rest(say, step):
    say("Hands off. Leave the handlebars centred and don't touch the brakes.", step)
    timeout, block = _Timeout("everything to be still"), []
    while True:
        s, dt = yield
        timeout.add(dt)
        if not s.mpu_ok:
            block = []
            continue
        block.append(s)
        if len(block) < 400:  # 2 seconds
            continue
        gyro = np.array([b.gyro for b in block])
        brakes = np.array([b.brakes_mv for b in block])
        if steering.is_still(gyro) and np.all(np.ptp(brakes, axis=0) < 40):
            return (gyro.mean(axis=0), np.array([b.accel for b in block]).mean(axis=0),
                    brakes.mean(axis=0))
        block = block[200:]


def _measure_sweep(say, step, bias):
    say("Slowly turn the handlebars RIGHT as far as you'd want a full turn to be, "
        "then LEFT the same amount, then back to the centre. Then let go.", step)
    timeout = _Timeout("the handlebars to turn both ways")
    rates, accels, dts, travel = [], [], [], 0.0
    while True:
        s, dt = yield
        timeout.add(dt)
        if not s.mpu_ok:
            continue
        r = np.asarray(s.gyro) - bias
        rates.append(r)
        accels.append(s.accel)
        dts.append(dt)
        travel += float(np.linalg.norm(r)) * dt
        # Finish once the bars have turned both ways and come to rest.
        if travel > 30 and len(rates) % 50 == 0 and steering.is_still(rates[-200:]):
            axis, _ = steering.fit_axis(rates)
            angles = np.cumsum(np.array(rates) @ axis * np.array(dts))
            if angles.max() > 10 and angles.min() < -10:
                break
    rates, accels, dts = np.array(rates), np.array(accels), np.array(dts)
    np.savez(SWEEP_PATH, rates=rates, accel=accels, dts=dts, bias=bias)
    axis, consistency = steering.fit_axis(rates)
    angles = np.cumsum(rates @ axis * dts)
    first = np.flatnonzero(np.abs(angles) > 10)
    if angles[first[0]] < 0:  # the first turn was to the right, so make right positive
        axis, angles = -axis, -angles
    right, left = float(angles.max()), float(-angles.min())
    say(f"Handlebars turned {right:.0f}° right and {left:.0f}° left "
        f"(turn quality {consistency:.2f}, where 1.00 is perfect).")
    return axis, right, left, rates, accels, dts


def _measure_brake_rest(say, step):
    say("Hands off both brakes.", step)
    timeout, block = _Timeout("the brakes to be left alone"), []
    while True:
        s, dt = yield
        timeout.add(dt)
        block.append(s.brakes_mv)
        if len(block) < 200:  # 1 second
            continue
        brakes = np.array(block)
        if np.all(np.ptp(brakes, axis=0) < 40):
            return brakes.mean(axis=0)
        block = block[100:]


def _measure_brake(say, step, lever, rest_mv, taken=None):
    """Returns (sensor channel, full-brake reading) for one lever. The channel is
    whichever sensor responds to the squeeze (other than `taken`), so it doesn't
    matter which lever each sensor was wired to. The full reading is the middle
    of three firm squeezes, so an ordinary hard squeeze reaches 100%."""
    name = BRAKE_NAMES[lever]
    say(f"Squeeze the {name.upper()} brake {BRAKE_SQUEEZES} times, firmly, the way you'd brake "
        f"hard in a game. Let go fully between squeezes.", step)
    timeout = _Timeout(f"the {name} brake")
    channel, peaks, extreme, settled = None, [], 0.0, 0
    while True:
        s, dt = yield
        timeout.add(dt)
        if channel is None:
            moved = [c for c in range(2) if c != taken
                     and abs(s.brakes_mv[c] - rest_mv[c]) >= BRAKE_SQUEEZE_MV]
            if not moved:
                continue
            channel = max(moved, key=lambda c: abs(s.brakes_mv[c] - rest_mv[c]))
        i = channel
        dev = s.brakes_mv[i] - rest_mv[i]
        if abs(dev) > abs(extreme):
            extreme = dev
        if abs(extreme) < BRAKE_SQUEEZE_MV:
            continue
        settled = settled + 1 if abs(dev) < BRAKE_RELEASED_MV else 0
        if settled > 60:  # released for 0.3 s: that squeeze is over
            peaks.append(extreme)
            say(f"{name.capitalize()} brake squeeze {len(peaks)} of {BRAKE_SQUEEZES}: "
                f"{rest_mv[i] + extreme:.0f} mV")
            extreme, settled = 0.0, 0
            if len(peaks) == BRAKE_SQUEEZES:
                full = rest_mv[i] + float(np.median(peaks))
                say(f"{name.capitalize()} brake (sensor {'AB'[i]}): {rest_mv[i]:.0f} mV released, "
                    f"{full:.0f} mV squeezed.")
                return i, full


def _measure_brakes(say, first_step, total, rest_mv):
    """Both levers; returns the fields for Calibration in lever order."""
    left, left_full = yield from _measure_brake(say, f"Step {first_step} of {total}", 0, rest_mv)
    right, right_full = yield from _measure_brake(say, f"Step {first_step + 1} of {total}", 1, rest_mv,
                                                  taken=left)
    if left != 0:
        say("The brake sensors are wired to the opposite levers from usual. That's fine: "
            "the app now knows which is which.")
    return dict(brake_channels=[left, right],
                brake_rest_mv=[round(float(rest_mv[left])), round(float(rest_mv[right]))],
                brake_full_mv=[round(left_full), round(right_full)])


def gravity_fit_summary(slope, rms):
    return f"Drift correction from gravity matches the gyro to within {rms:.1f}°."


def full_calibration(say):
    """Steering and both brakes."""
    bias, gravity, rest_mv = yield from _measure_rest(say, "Step 1 of 4")
    axis, right, left, rates, accels, dts = yield from _measure_sweep(say, "Step 2 of 4", bias)
    slope, offset, rms = steering.fit_gravity_angle(rates, accels, dts, axis, gravity)
    say(gravity_fit_summary(slope, rms))
    brakes = yield from _measure_brakes(say, 3, 4, rest_mv)
    return Calibration(
        steer_axis=[float(v) for v in axis],
        steer_right_deg=round(right, 1),
        steer_left_deg=round(left, 1),
        gravity=[float(v) for v in gravity],
        gravity_slope=slope,
        gravity_offset=offset,
        **brakes,
    )


def brake_calibration(say, cal):
    """Both brakes only, keeping the steering part of `cal`."""
    rest_mv = yield from _measure_brake_rest(say, "Step 1 of 3")
    brakes = yield from _measure_brakes(say, 2, 3, rest_mv)
    return dataclasses.replace(cal, **brakes)


def drive(procedure, samples):
    """Runs a calibration procedure over an iterable of (sample, dt) pairs and
    returns the resulting Calibration. Gaps in the data (None) are skipped."""
    next(procedure)
    for s, dt in samples:
        if s is None:
            continue
        try:
            procedure.send((s, dt))
        except StopIteration as done:
            return done.value
