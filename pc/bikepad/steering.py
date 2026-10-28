"""Steering angle from the MPU6050.

The gyro gives fast, smooth changes but drifts. Gravity gives an absolute angle
with no drift: the steering axis is tilted back (the head tube angle, ~17 deg),
so turning the bars swings gravity around that axis as the sensor sees it. A
complementary filter combines the two."""
import math

import numpy as np

STILL_SPREAD_DPS = 3.0  # max spread of each gyro axis within a block that counts as still
BLOCK_SECONDS = 1.0
BIAS_BLEND = 0.3        # weight of each new still block in the running bias
GRAVITY_TAU = 1.0       # seconds for the angle to settle onto the gravity angle
STARTUP_TAU = 0.3       # same, before the gyro bias is known
SMOOTH_SAMPLES = 20     # accelerometer smoothing when fitting the gravity angle


def is_still(gyro_block):
    return bool(np.all(np.ptp(gyro_block, axis=0) < STILL_SPREAD_DPS))


class GyroBias:
    """Estimates the gyro's zero offset from one-second blocks in which the
    sensor is still. `value` is None until the first still block."""

    def __init__(self, value=None):
        self.value = None if value is None else np.array(value, float)
        self._block = []
        self._block_time = 0.0

    def update(self, gyro, dt):
        self._block.append(gyro)
        self._block_time += dt
        if self._block_time < BLOCK_SECONDS:
            return
        block = np.array(self._block)
        self._block, self._block_time = [], 0.0
        if is_still(block):
            mean = block.mean(axis=0)
            self.value = mean if self.value is None else self.value + BIAS_BLEND * (mean - self.value)


class GravityAngle:
    """Absolute steering angle (degrees) from the accelerometer: the direction
    of gravity around the steering axis, relative to where it points with the
    bars centred, scaled by a fit against the gyro from the calibration sweep."""

    def __init__(self, axis, gravity_centred, slope=1.0, offset=0.0):
        self.axis = np.asarray(axis, float)
        g = np.asarray(gravity_centred, float)
        u = g - (g @ self.axis) * self.axis
        self.u = u / np.linalg.norm(u)
        self.v = np.cross(self.axis, self.u)
        self.slope, self.offset = slope, offset

    def raw(self, accel):
        g = np.asarray(accel, float)
        p = g - (g @ self.axis) * self.axis
        return math.degrees(math.atan2(p @ self.v, p @ self.u))

    def angle(self, accel):
        """The angle, or None while the reading is too disturbed to trust."""
        g = np.asarray(accel, float)
        p = g - (g @ self.axis) * self.axis
        if not 0.8 < np.linalg.norm(g) < 1.2 or np.linalg.norm(p) < 0.1:
            return None
        return self.slope * self.raw(g) + self.offset


def fit_gravity_angle(rates, accels, dts, axis, gravity_centred):
    """Fits the sweep's gyro-integrated angle against its raw gravity angle.
    Returns (slope, offset, rms_error_deg)."""
    theta = np.cumsum(np.asarray(rates) @ np.asarray(axis) * np.asarray(dts))
    ga = GravityAngle(axis, gravity_centred)
    phi = np.array([ga.raw(a) for a in accels])
    k = SMOOTH_SAMPLES
    phi = np.convolve(phi, np.ones(k) / k, mode="same")[k:-k]
    theta = theta[k:-k]
    slope, offset = np.polyfit(phi, theta, 1)
    rms = float(np.std(theta - (slope * phi + offset)))
    return float(slope), float(offset), rms


class Steering:
    """Steering angle in degrees, positive to the right: the gyro integrated
    about the steering axis, pulled steadily towards the gravity angle so it
    can't drift. Without a gravity fit in the calibration it is gyro-only and
    starts at 0, so the bars must then be centred at startup."""

    def __init__(self, cal):
        self.axis = np.asarray(cal.steer_axis, float)
        self.bias = GyroBias()
        self.gravity = None
        if cal.gravity_slope is not None:
            self.gravity = GravityAngle(self.axis, cal.gravity, cal.gravity_slope, cal.gravity_offset)
        self.angle = None if self.gravity else 0.0

    @property
    def ready(self):
        return self.angle is not None and (self.gravity or self.bias.value is not None)

    def update(self, gyro, accel, dt):
        gyro = np.asarray(gyro, float)
        self.bias.update(gyro, dt)
        target = self.gravity.angle(accel) if self.gravity else None
        if self.angle is None:
            self.angle = target
            return
        tau = STARTUP_TAU
        if self.bias.value is not None:
            self.angle += float(np.dot(gyro - self.bias.value, self.axis)) * dt
            tau = GRAVITY_TAU
        if target is not None:
            self.angle += (target - self.angle) * min(dt / tau, 1.0)


def fit_axis(rates):
    """Returns (axis, consistency) for a sweep's bias-corrected gyro samples
    (N x 3): the unit axis the sensor mostly rotated about, and the share of all
    rotation that was about it (1.0 means a clean single-axis turn)."""
    rates = np.asarray(rates, float)
    vals, vecs = np.linalg.eigh(rates.T @ rates)
    return vecs[:, -1], float(vals[-1] / vals.sum())
