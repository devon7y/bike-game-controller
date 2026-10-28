"""Game profiles and app settings.

A profile says what each bike input does on the virtual controller (its
bindings) and how it feels: how far the handlebars turn for a full stick, what
counts as 100% pedalling, brake dead zones and so on."""
import copy
import dataclasses
import json
import math
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .keyboard import KEYS

SETTINGS_PATH = Path(__file__).resolve().parent.parent / "settings.json"

# Bike inputs a binding can read. "steering" runs from -1 (left) to 1 (right);
# everything else from 0 to 1.
SOURCES = {
    "steering": "Handlebars (left / right)",
    "steering_left": "Handlebars turned left",
    "steering_right": "Handlebars turned right",
    "pedalling": "Pedalling",
    "left_brake": "Left brake",
    "right_brake": "Right brake",
    "either_brake": "Either brake",
    "both_brakes": "Both brakes together",
}
AXES = {
    "left_x": "Left stick: left / right",
    "left_y": "Left stick: forward / back",
    "right_x": "Right stick: left / right",
    "right_y": "Right stick: up / down",
}
TRIGGERS = {
    "left_trigger": "Left trigger (LT)",
    "right_trigger": "Right trigger (RT)",
}
BUTTONS = {
    "a": "A button", "b": "B button", "x": "X button", "y": "Y button",
    "lb": "Left bumper (LB)", "rb": "Right bumper (RB)",
    "ls": "Left stick click (LS)", "rs": "Right stick click (RS)",
    "start": "Menu / Start", "back": "View / Back",
    "dpad_up": "D-pad up", "dpad_down": "D-pad down",
    "dpad_left": "D-pad left", "dpad_right": "D-pad right",
}
KEYBOARD = "keyboard"  # target for a keyboard key or mouse button (Binding.key says which)
TARGETS = {**AXES, **TRIGGERS, **BUTTONS, KEYBOARD: "Keyboard / mouse"}
MODES = {"hold": "Hold", "toggle": "Toggle"}

PEDALLING_MEASURES = {  # key: (label, unit, slider max for "100% at", slider max for "starts at")
    "power": ("Power", "W", 600, 100),
    "speed": ("Trainer speed", "km/h", 60, 10),
    "cadence": ("Cadence", "rpm", 150, 60),
}
BUTTON_RELEASE_GAP = 0.05  # a held button lets go this far below its threshold
ZWIFT_FULL_DEG = 40.0      # Zwift treats a +-40 deg Sterzo angle as full steering


@dataclass
class SteeringFeel:
    full_lock_deg: float = 30.0  # handlebar angle that gives a full stick
    deadzone_deg: float = 1.0    # angles nearer the centre than this count as straight
    curve: float = 1.0           # 1 = straight line; higher = gentler near the centre


@dataclass
class PedallingFeel:
    measure: str = "power"       # a PEDALLING_MEASURES key
    full: float = 250.0          # this much gives 100%
    start: float = 10.0          # below this counts as not pedalling
    smoothing_s: float = 0.4


@dataclass
class BrakeFeel:
    deadzone: float = 0.05       # lever travel ignored at the start (0..1)
    curve: float = 1.0


@dataclass
class ResistanceFeel:
    mode: str = "off"            # "off", or "hills": the trainer's slope follows the game's road
    strength: float = 0.5        # share of the game's slope passed to the trainer (0..1)
    brake_pct: float = 0.0       # extra slope (%) at full brake in the game
    max_pct: float = 10.0        # limit on the trainer's slope, uphill and downhill (%)


RESISTANCE_MODES = {"off": "Off", "hills": "Follow the game's hills"}


@dataclass
class Binding:
    source: str
    target: str
    invert: bool = False         # sticks only: reverse the direction
    threshold: float = 0.5       # buttons and keys: pressed at this much (0..1)
    mode: str = "hold"           # buttons and keys: "hold" (while squeezed) or "toggle" (on/off per squeeze)
    key: str = "space"           # keyboard target: a keyboard.KEYS id

    @property
    def pressable(self):
        return self.target in BUTTONS or self.target == KEYBOARD


@dataclass
class Profile:
    name: str
    steering: SteeringFeel = field(default_factory=SteeringFeel)
    pedalling: PedallingFeel = field(default_factory=PedallingFeel)
    brakes: BrakeFeel = field(default_factory=BrakeFeel)
    bindings: list = field(default_factory=list)
    resistance: ResistanceFeel = field(default_factory=ResistanceFeel)
    zwift_steering: bool = False  # also send the handlebar angle to Zwift (as a Sterzo)
    preset: str = None           # the built-in preset this started from, for "restore defaults"

    def uses_pedalling(self):
        return any(b.source == "pedalling" for b in self.bindings)

    def uses_trainer(self):
        return self.uses_pedalling() or self.resistance.mode != "off"

    def uses_controller(self):
        return any(b.target != KEYBOARD for b in self.bindings)

    @classmethod
    def from_dict(cls, d):
        def build(kind, data):
            names = {f.name for f in dataclasses.fields(kind)}
            return kind(**{k: v for k, v in (data or {}).items() if k in names})
        bindings = [build(Binding, b) for b in d.get("bindings", [])
                    if b.get("source") in SOURCES and b.get("target") in TARGETS]
        for b in bindings:
            b.mode = b.mode if b.mode in MODES else "hold"
            b.key = b.key if b.key in KEYS else "space"
        resistance = build(ResistanceFeel, d.get("resistance"))
        if resistance.mode not in RESISTANCE_MODES:
            resistance.mode = "off"
        return cls(name=d["name"], steering=build(SteeringFeel, d.get("steering")),
                   pedalling=build(PedallingFeel, d.get("pedalling")),
                   brakes=build(BrakeFeel, d.get("brakes")),
                   bindings=bindings,
                   resistance=resistance,
                   zwift_steering=bool(d.get("zwift_steering", False)),
                   preset=d.get("preset"))


PRESETS = {
    "driving": "Driving games (trucks, cars)",
    "minecraft": "Minecraft",
    "zwift": "Zwift (steering through Bluetooth)",
    "test": "Test (each input on its own control)",
    "blank": "Blank",
}


def make_preset(key, name=None):
    if key == "driving":
        p = Profile("Driving games", resistance=ResistanceFeel(mode="hills"), bindings=[
            Binding("steering", "left_x"),
            Binding("pedalling", "right_trigger"),
            Binding("either_brake", "left_trigger"),
        ])
    elif key == "minecraft":
        p = Profile("Minecraft",
                    steering=SteeringFeel(full_lock_deg=25, deadzone_deg=3, curve=1.5),
                    pedalling=PedallingFeel(full=200, start=15),
                    bindings=[
                        Binding("steering", "right_x"),               # turn to look around
                        Binding("pedalling", "left_y"),               # pedal to walk forward
                        Binding("left_brake", "left_y", invert=True),  # left brake walks back
                        Binding("pedalling", "ls", threshold=0.8),    # pedal hard to sprint
                        Binding("right_brake", "rs", threshold=0.5),  # right brake to sneak
                    ])
    elif key == "zwift":
        # No controller and no pedalling: Zwift reads the trainer itself.
        p = Profile("Zwift", zwift_steering=True)
    elif key == "test":
        p = Profile("Test", bindings=[
            Binding("steering", "left_x"),
            Binding("pedalling", "right_y"),
            Binding("left_brake", "left_trigger"),
            Binding("right_brake", "right_trigger"),
        ])
    else:
        p = Profile("New profile")
    p.preset = key
    if name:
        p.name = name
    return p


STARTER_PRESETS = ("driving", "minecraft", "zwift", "test")


def default_profiles():
    return [make_preset(k) for k in STARTER_PRESETS]


class Settings:
    """Everything the app remembers between runs, saved to settings.json."""

    def __init__(self, path=SETTINGS_PATH):
        self.path = path
        self._lock = threading.Lock()
        data = json.loads(path.read_text()) if path.exists() else {}
        self.profiles = [Profile.from_dict(p) for p in data.get("profiles", [])] or default_profiles()
        self.current = data.get("current", self.profiles[0].name)
        self.controller_on = data.get("controller_on", True)
        self.trainer_address = data.get("trainer_address")
        # Starter profiles added in later versions appear once, even for existing settings.
        self.offered = set(data.get("offered_presets", [p.preset for p in self.profiles]))
        for key in STARTER_PRESETS:
            if key not in self.offered:
                self.offered.add(key)
                self.profiles.append(make_preset(key, self.unique_name(make_preset(key).name)))

    def profile(self):
        for p in self.profiles:
            if p.name == self.current:
                return p
        return self.profiles[0]

    def names(self):
        return [p.name for p in self.profiles]

    def unique_name(self, wanted):
        name, n = wanted, 2
        while name in self.names():
            name, n = f"{wanted} {n}", n + 1
        return name

    def save(self):
        with self._lock:
            data = dict(profiles=[asdict(p) for p in self.profiles], current=self.current,
                        controller_on=self.controller_on, trainer_address=self.trainer_address,
                        offered_presets=sorted(self.offered))
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2))
            tmp.replace(self.path)


def steering_value(feel, angle_deg):
    """Stick position from -1 (full left) to 1 (full right)."""
    if angle_deg is None:
        return 0.0
    span = max(feel.full_lock_deg - feel.deadzone_deg, 0.1)
    x = min(max(abs(angle_deg) - feel.deadzone_deg, 0.0) / span, 1.0) ** feel.curve
    return math.copysign(x, angle_deg)


def brake_value(feel, lever):
    """Brake from 0 to 1, given the calibrated lever position (0..1)."""
    x = min(max((lever - feel.deadzone) / max(1.0 - feel.deadzone, 0.01), 0.0), 1.0)
    return x ** feel.curve


@dataclass
class Outputs:
    axes: dict = field(default_factory=dict)      # AXES key -> -1..1
    triggers: dict = field(default_factory=dict)  # TRIGGERS key -> 0..1
    buttons: set = field(default_factory=set)     # BUTTONS keys held down
    keys: set = field(default_factory=set)        # keyboard.KEYS ids held down


class Mapper:
    """Turns the bike's inputs into controller outputs for a profile, keeping
    the pedalling smoothing and which buttons are held or toggled on between
    samples."""

    def __init__(self):
        self.effort = 0.0
        self.held = set()     # rows whose input is past its threshold
        self.toggled = set()  # toggle rows currently switched on

    def sources(self, profile, steering_deg, levers, trainer, dt):
        """Every SOURCES value, tuned by the profile's feel settings."""
        steer = steering_value(profile.steering, steering_deg)
        left, right = (brake_value(profile.brakes, x) for x in levers)
        ped = profile.pedalling
        raw = {"power": trainer.power_w, "speed": trainer.speed_kmh,
               "cadence": trainer.cadence_rpm}.get(ped.measure, 0.0)
        target = 0.0 if raw < ped.start else min(raw / max(ped.full, 1e-3), 1.0)
        self.effort += (target - self.effort) * min(dt / max(ped.smoothing_s, 1e-3), 1.0)
        return dict(steering=steer, steering_left=max(-steer, 0.0), steering_right=max(steer, 0.0),
                    pedalling=self.effort, left_brake=left, right_brake=right,
                    either_brake=max(left, right), both_brakes=min(left, right))

    def outputs(self, profile, values):
        out = Outputs()
        for b in profile.bindings:
            v = values.get(b.source, 0.0)
            if b.target in AXES:
                out.axes[b.target] = out.axes.get(b.target, 0.0) + (-v if b.invert else v)
            elif b.target in TRIGGERS:
                out.triggers[b.target] = max(out.triggers.get(b.target, 0.0), max(v, 0.0))
            elif b.pressable and self._pressed(b, v):
                if b.target == KEYBOARD:
                    out.keys.add(b.key)
                else:
                    out.buttons.add(b.target)
        out.axes = {k: min(max(v, -1.0), 1.0) for k, v in out.axes.items()}
        return out

    def _pressed(self, b, v):
        row = (b.source, b.target, b.key, b.threshold, b.mode)
        was = row in self.held
        if v >= b.threshold:
            self.held.add(row)
        elif v < b.threshold - BUTTON_RELEASE_GAP:
            self.held.discard(row)
        now = row in self.held
        if b.mode != "toggle":
            return now
        if now and not was:  # each new squeeze flips it on or off
            self.toggled ^= {row}
        return row in self.toggled


def swap_triggers(profile):
    """Swaps left- and right-trigger rows (some games use them the other way round)."""
    swap = {"left_trigger": "right_trigger", "right_trigger": "left_trigger"}
    for b in profile.bindings:
        b.target = swap.get(b.target, b.target)


def copy_profile(profile, name):
    p = copy.deepcopy(profile)
    p.name = name
    return p
