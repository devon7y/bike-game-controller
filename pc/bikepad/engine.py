"""The engine: a background thread that reads the bike, keeps the steering
filter, pedalling and virtual controller up to date, runs calibrations, and
publishes a Snapshot for the window (or the command line) to show."""
import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path

from .calibration import Calibration, CalibrationError, brake_calibration, full_calibration
from .game import GameData, GameTelemetry
from .link import Link, stream
from .profiles import ZWIFT_FULL_DEG, Mapper, Outputs
from .steering import Steering
from .trainer import Trainer, TrainerData

LOG_PATH = Path(__file__).resolve().parent.parent / "app.log"
LOG_MAX_BYTES = 5_000_000
STATUS_LOG_SECONDS = 1.0
ZWIFT_SEND_SECONDS = 0.05   # send the Zwift angle to the ESP32 20 times a second
GRADE_SMOOTH_S = 1.0        # evens out the truck's pitch bouncing on its suspension
ZWIFT_STATES = ("off", "waiting for Zwift", "connecting to Zwift", "connected to Zwift")


@dataclass
class Snapshot:
    bike_connected: bool = False
    mpu_ok: bool = False
    calibrated: bool = False
    steering_deg: float = None     # handlebar angle, positive to the right
    brakes_mv: tuple = (0, 0)      # raw Hall sensor readings, in lever order (left, right)
    levers: tuple = (0.0, 0.0)     # calibrated lever positions (0..1)
    values: dict = field(default_factory=dict)  # profiles.SOURCES values for the profile
    outputs: Outputs = field(default_factory=Outputs)
    trainer_status: str = "searching"
    trainer_name: str = None
    trainer: TrainerData = field(default_factory=TrainerData)
    controller_on: bool = False    # the virtual controller exists
    controls_on: bool = False      # game controls (controller and keys) are being sent
    zwift_state: int = 0           # see ZWIFT_STATES
    zwift_angle: float = None      # angle being sent to Zwift, or None when not sending
    game: GameData = field(default_factory=GameData)
    grade_wanted: float = None     # trainer slope (%) the profile asks for, or None when not following a game
    grade_set: float = None        # slope the trainer last accepted


class CalibrationRun:
    """A calibration in progress: what to show, and how it ended."""

    def __init__(self, kind, procedure):
        self.kind = kind
        self.step = ""          # e.g. "Step 2 of 4"
        self.instruction = ""
        self.results = []       # result lines so far
        self.done = False
        self.error = None
        self.calibration = None
        self._procedure = procedure(self._say)
        next(self._procedure)

    def _say(self, text, step=None):
        if step:
            self.step, self.instruction = step, text
        else:
            self.results.append(text)

    def feed(self, sample, dt):
        try:
            self._procedure.send((sample, dt))
        except StopIteration as finished:
            self.calibration, self.done = finished.value, True
        except CalibrationError as e:
            self.error, self.done = str(e), True


class Engine:
    def __init__(self, settings, port=None, log_path=LOG_PATH, controller=None):
        """`controller` overrides the saved virtual-controller setting."""
        self.settings = settings
        self.cal = Calibration.load()
        self.steering = Steering(self.cal) if self.cal else None
        self.mapper = Mapper()
        self.want_controller = settings.controller_on if controller is None else controller
        self.pad = None
        self.keyboard = None
        self.calibration = None  # the CalibrationRun in progress, or the last one
        self.snapshot = Snapshot()
        if log_path.exists() and log_path.stat().st_size > LOG_MAX_BYTES:
            log_path.unlink()
        self._log_file = open(log_path, "a", buffering=1, encoding="utf-8")
        self.log("Engine starting")
        self.link = Link(port)
        self.game = GameTelemetry()
        self.trainer = Trainer(settings.trainer_address, on_connect=self._trainer_connected,
                               enabled=settings.profile().uses_trainer(), log=self.log)
        self._grade = 0.0
        threading.Thread(target=self._run, daemon=True).start()

    # --- called from the window ---

    def set_controller(self, on):
        self.want_controller = on
        self.settings.controller_on = on
        self.settings.save()

    def start_calibration(self, kind):
        if kind == "brakes" and self.cal:
            run = CalibrationRun(kind, lambda say: brake_calibration(say, self.cal))
        else:
            run = CalibrationRun("full", full_calibration)
        self.log(f"Calibration started ({run.kind})")
        self.calibration = run

    def cancel_calibration(self):
        run = self.calibration
        if run and not run.done:
            run.error, run.done = "Cancelled.", True
            self.log("Calibration cancelled")

    @property
    def calibrating(self):
        return self.calibration is not None and not self.calibration.done

    def shutdown(self, timeout=1.5):
        """Removes the virtual controller (without changing the saved setting) and
        lets go of the trainer."""
        self.want_controller = False
        end = time.monotonic() + timeout
        while (self.pad is not None or (self.keyboard and self.keyboard.down)) and time.monotonic() < end:
            time.sleep(0.02)
        self.trainer.stop()
        self.log("Engine stopped")

    # --- engine thread ---

    def log(self, text):
        self._log_file.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {text}\n")

    def _trainer_connected(self, address, name):
        self.log(f"Trainer connected: {name} ({address})")
        if self.settings.trainer_address != address:
            self.settings.trainer_address = address
            self.settings.save()

    def _sync_outputs(self, profile):
        """The virtual controller exists while game controls are on and the
        profile has controller rows; keys are released while controls are off;
        the trainer is connected while the profile uses pedalling."""
        self.trainer.set_enabled(profile.uses_trainer())
        if self.keyboard is None:
            from .keyboard import Keyboard
            self.keyboard = Keyboard()
        if not self.want_controller:
            self.keyboard.release_all()
        wanted = self.want_controller and profile.uses_controller()
        if wanted and self.pad is None:
            from .gamepad import Pad
            try:
                self.pad = Pad()
                self.log("Virtual controller on")
            except Exception as e:
                self.log(f"Could not create the virtual controller: {e!r}")
                self.want_controller = False
        elif not wanted and self.pad is not None:
            self.pad.close()
            self.pad = None
            self.log("Virtual controller off")

    def _finish_calibration(self, run):
        if run.calibration:
            run.calibration.save()
            self.cal = run.calibration
            self.steering = Steering(self.cal)
            self.log(f"Calibration saved ({run.kind}): " + " | ".join(run.results))
        else:
            self.log(f"Calibration stopped: {run.error}")

    def _run(self):
        while True:
            try:
                self._loop()
            except Exception:
                self.log("Engine error:\n" + traceback.format_exc())
                time.sleep(1.0)

    def _trainer_grade(self, profile, game, dt):
        """The slope (%) the trainer should simulate for this profile, or None
        when the profile doesn't follow the game (the trainer is then left flat)."""
        res = profile.resistance
        if res.mode != "hills":
            self._grade = 0.0
            return None
        target = 0.0  # flat while no game is sending or it's paused
        if game.game and not game.paused:
            target = res.strength * game.grade_pct + res.brake_pct * game.brake
        target = min(max(target, -res.max_pct), res.max_pct)
        self._grade += (target - self._grade) * min(dt / GRADE_SMOOTH_S, 1.0)
        return self._grade

    def _send_zwift(self, profile, steer, now):
        """Keeps the ESP32's Zwift angle up to date. The first angle switches on
        its Bluetooth; leaving a Zwift profile sends one last 0 (straight)."""
        if profile.zwift_steering and steer is not None:
            angle = steer * ZWIFT_FULL_DEG
        elif self._zwift_sending:
            angle = 0.0
        else:
            return None
        if now - self._zwift_sent_at >= ZWIFT_SEND_SECONDS:
            self.link.send(f"Z,{angle:.1f}\n")
            self._zwift_sent_at = now
            self._zwift_sending = profile.zwift_steering
        return angle if profile.zwift_steering else None

    def _loop(self):
        last_log, last_mpu, last_trainer, last_zwift = 0.0, None, None, None
        self._zwift_sent_at, self._zwift_sending = 0.0, False
        for s, dt in stream(self.link):
            profile = self.settings.profile()
            self._sync_outputs(profile)
            now = time.monotonic()
            trainer = self.trainer.data
            if self.trainer.status != last_trainer:
                last_trainer = self.trainer.status
                self.log(f"Trainer: {last_trainer}")
            if s is None:
                if self.pad:
                    self.pad.neutral()
                self.keyboard.release_all()
                self.snapshot = Snapshot(calibrated=self.cal is not None,
                                         zwift_state=self.snapshot.zwift_state,
                                         trainer_status=self.trainer.status,
                                         trainer_name=self.trainer.name, trainer=trainer,
                                         controller_on=self.pad is not None)
                continue
            if s.mpu_ok != last_mpu:
                last_mpu = s.mpu_ok
                self.log("Steering sensor " + ("ok" if s.mpu_ok else "NOT RESPONDING"))
            if s.zwift != last_zwift:
                last_zwift = s.zwift
                self.log(f"Zwift steering: {ZWIFT_STATES[s.zwift] if s.zwift < 4 else s.zwift}")

            run = self.calibration
            if run and not run.done:
                run.feed(s, dt)
                if run.done:
                    self._finish_calibration(run)

            if self.steering and s.mpu_ok:
                self.steering.update(s.gyro, s.accel, dt)
            angle = self.steering.angle if self.steering and self.steering.ready else None
            levers = self.cal.levers(s.brakes_mv) if self.cal else (0.0, 0.0)
            lever_mv = self.cal.lever_mv(s.brakes_mv) if self.cal else s.brakes_mv
            values = self.mapper.sources(profile, angle, levers, trainer, dt)
            outputs = self.mapper.outputs(profile, values)
            live = self.want_controller and self.cal and not self.calibrating
            if self.pad and live:
                self.pad.set(outputs)
            elif self.pad:
                self.pad.neutral()
            self.keyboard.set(outputs.keys if live else set())
            usable = self.cal and not self.calibrating and angle is not None
            zwift_angle = self._send_zwift(profile, values["steering"] if usable else None, now)
            game = self.game.data
            grade = self._trainer_grade(profile, game, dt)
            self.trainer.set_grade(grade)

            self.snapshot = Snapshot(
                bike_connected=True, mpu_ok=s.mpu_ok, calibrated=self.cal is not None,
                steering_deg=angle, brakes_mv=lever_mv, levers=levers, values=values,
                outputs=outputs, trainer_status=self.trainer.status, trainer_name=self.trainer.name,
                trainer=trainer, controller_on=self.pad is not None, controls_on=bool(live),
                game=game, grade_wanted=grade, grade_set=self.trainer.grade_set, zwift_state=s.zwift,
                zwift_angle=zwift_angle)

            if now - last_log >= STATUS_LOG_SECONDS:
                last_log = now
                ang = f"{angle:+.1f}" if angle is not None else "-"
                self.log(f"status: profile={profile.name} steer={ang}deg brakes_mv={s.brakes_mv} "
                          f"levers=({levers[0]:.2f},{levers[1]:.2f}) trainer={self.trainer.describe()} "
                          f"controller={'on' if self.pad else 'off'} keys={sorted(outputs.keys) if live else []} "
                          f"mpu={'ok' if s.mpu_ok else 'NO'} "
                          f"zwift={ZWIFT_STATES[s.zwift] if s.zwift < 4 else s.zwift}"
                          f"{'' if zwift_angle is None else f' {zwift_angle:+.0f}deg'} "
                          f"game={game.game or '-'}{' paused' if game.paused and game.game else ''}"
                          f"{f' slope={game.grade_pct:+.1f}%' if game.game else ''} "
                          f"trainer_slope={'-' if grade is None else f'{grade:+.1f}%'}"
                          f"(set {'-' if self.trainer.grade_set is None else f'{self.trainer.grade_set:+.1f}%'}) "
                          f"bad_lines={self.link.bad_lines}")
