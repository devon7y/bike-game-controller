"""Bluetooth link to the smart trainer (FTMS): speed, cadence and power in,
simulated road slope out.

The Flux 2 sends Indoor Bike Data about 3 times a second while pedalling and
once a second when idle. Power drops to 0 within about a second of stopping;
speed follows the flywheel and coasts down over ~20 seconds. Its FTMS feature
flags (target settings 0xa00c) include indoor bike simulation, which is how the
slope is set."""
import asyncio
import struct
import threading
import time
from dataclasses import dataclass

from bleak import BleakClient, BleakScanner

FTMS_SERVICE = "00001826-0000-1000-8000-00805f9b34fb"
INDOOR_BIKE_DATA = "00002ad2-0000-1000-8000-00805f9b34fb"
CONTROL_POINT = "00002ad9-0000-1000-8000-00805f9b34fb"
STALE_SECONDS = 3.0  # readings older than this count as stopped

# FTMS control point (op code, then parameters); the trainer answers each with
# an indication 0x80, <op code>, <result> where result 0x01 is success.
OP_REQUEST_CONTROL = 0x00
OP_SET_SIMULATION = 0x11       # wind (0.001 m/s), grade (0.01 %), crr (0.0001), cw (0.01 kg/m)
CRR, CW = 40, 51               # rolling resistance 0.0040, drag 0.51 kg/m: typical road bike
GRADE_STEP_PCT = 0.1           # resend when the wanted slope changes by this much
GRADE_REFRESH_S = 15.0         # and at least this often


@dataclass
class TrainerData:
    speed_kmh: float = 0.0
    cadence_rpm: float = 0.0
    power_w: float = 0.0
    received: float = 0.0  # time.monotonic() of the update


def parse_indoor_bike_data(data):
    """Returns (speed km/h, cadence rpm, power W) from an Indoor Bike Data
    notification; fields the trainer leaves out are 0."""
    flags = struct.unpack_from("<H", data, 0)[0]
    i, speed, cadence, power = 2, 0.0, 0.0, 0.0
    if not flags & 0x0001:  # bit 0 clear means instantaneous speed is present
        speed = struct.unpack_from("<H", data, i)[0] * 0.01
        i += 2
    if flags & 0x0002:  # average speed
        i += 2
    if flags & 0x0004:
        cadence = struct.unpack_from("<H", data, i)[0] * 0.5
        i += 2
    if flags & 0x0008:  # average cadence
        i += 2
    if flags & 0x0010:  # total distance
        i += 3
    if flags & 0x0020:  # resistance level
        i += 2
    if flags & 0x0040:
        power = float(struct.unpack_from("<h", data, i)[0])
    return speed, cadence, power


class Trainer:
    """Connects to the trainer at `address` (or else the first FTMS trainer it
    finds) in a background thread, keeps `data` current and reconnects if it
    drops out. `on_connect(address, name)` is called on each connection."""

    def __init__(self, address=None, on_connect=None, enabled=True, log=None):
        self.address = address
        self.on_connect = on_connect
        self.log = log or (lambda text: None)
        self.enabled = enabled
        self.grade_wanted = None  # simulated slope (%) to set, or None to leave the trainer alone
        self.grade_set = None     # slope the trainer last accepted
        self.name = None
        self.status = "searching" if enabled else "off"
        self._data = TrainerData()
        self._loop = self._stopping = self._changed = None
        self._thread = threading.Thread(target=lambda: asyncio.run(self._run()), daemon=True)
        self._thread.start()

    def set_grade(self, grade_pct):
        """Asks for a simulated road slope in %, or None to stop setting it (the
        trainer is then put back to flat once)."""
        self.grade_wanted = None if grade_pct is None else round(grade_pct, 1)

    def set_enabled(self, on):
        """Connects (True) or lets go of the trainer (False), e.g. so Zwift can use it."""
        if on != self.enabled:
            self.enabled = on
            if self._loop:
                self._loop.call_soon_threadsafe(self._changed.set)

    def stop(self, timeout=2.0):
        """Disconnects cleanly, so the trainer is free for the next program straight away."""
        if self._loop and self._stopping:
            self._loop.call_soon_threadsafe(self._stopping.set)
            self._thread.join(timeout)

    @property
    def connected(self):
        return self.status == "connected"

    @property
    def data(self):
        d = self._data
        if time.monotonic() - d.received > STALE_SECONDS:
            return TrainerData(received=d.received)
        return d

    def _on_data(self, _, raw):
        speed, cadence, power = parse_indoor_bike_data(bytes(raw))
        self._data = TrainerData(speed, cadence, power, time.monotonic())

    async def _find(self):
        if self.address:
            device = await BleakScanner.find_device_by_address(self.address, timeout=8.0)
            if device:
                return device
        return await BleakScanner.find_device_by_filter(
            lambda d, adv: FTMS_SERVICE in adv.service_uuids, timeout=8.0)

    async def _pause(self, seconds):
        """Sleeps, but returns as soon as stop() is called."""
        try:
            await asyncio.wait_for(self._stopping.wait(), seconds)
        except asyncio.TimeoutError:
            pass

    async def _until_changed(self):
        """Waits until set_enabled() or stop() is called."""
        waits = [asyncio.create_task(e.wait()) for e in (self._changed, self._stopping)]
        await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
        for w in waits:
            w.cancel()
        self._changed.clear()

    async def _run(self):
        self._loop, self._stopping = asyncio.get_running_loop(), asyncio.Event()
        self._changed = asyncio.Event()
        while not self._stopping.is_set():
            if not self.enabled:
                self.status = "off"
                self._data = TrainerData()
                await self._until_changed()
                continue
            try:
                self.status = "searching"
                device = await self._find()
                if device is None:
                    self.status = "not found (is it plugged in? pedal to wake it)"
                    await self._pause(2.0)
                    continue
                lost = asyncio.Event()
                async with BleakClient(device, disconnected_callback=lambda _: lost.set()) as client:
                    self.name = device.name
                    await client.start_notify(INDOOR_BIKE_DATA, self._on_data)
                    self.status = "connected"
                    if self.on_connect:
                        self.on_connect(device.address, device.name)
                    control = asyncio.create_task(self._control(client))
                    while self.enabled and not lost.is_set() and not self._stopping.is_set():
                        waits = [asyncio.create_task(e.wait())
                                 for e in (lost, self._stopping, self._changed)]
                        await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
                        for w in waits:
                            w.cancel()
                        self._changed.clear()
                    control.cancel()
            except Exception as e:  # Bluetooth errors are varied; keep retrying
                self.status = f"error ({e.__class__.__name__}), retrying"
            else:
                if not self.enabled:
                    continue  # let go on purpose; the loop top shows "off"
                self.status = "connection lost, retrying"
            await self._pause(2.0)
        self.status = "stopped"

    async def _command(self, client, replies, payload):
        """Writes one control point command; True if the trainer accepted it."""
        while not replies.empty():
            replies.get_nowait()
        await client.write_gatt_char(CONTROL_POINT, payload, response=True)
        try:
            reply = await asyncio.wait_for(replies.get(), 3.0)
        except asyncio.TimeoutError:
            self.log(f"Trainer didn't answer command 0x{payload[0]:02x}")
            return False
        if len(reply) >= 3 and reply[0] == 0x80 and reply[1] == payload[0] and reply[2] == 0x01:
            return True
        self.log(f"Trainer refused command 0x{payload[0]:02x}: reply {reply.hex()}")
        return False

    async def _control(self, client):
        """Keeps the trainer's simulated slope at grade_wanted while connected."""
        self.grade_set = None
        replies = asyncio.Queue()
        try:
            await client.start_notify(CONTROL_POINT, lambda _, d: replies.put_nowait(bytes(d)))
            have_control, sent_at = False, 0.0
            while True:
                await asyncio.sleep(0.25)
                wanted = self.grade_wanted
                if wanted is None:
                    if self.grade_set in (None, 0.0):
                        continue
                    wanted = 0.0  # stopped following the game: leave the trainer flat
                elif (self.grade_set is not None and abs(wanted - self.grade_set) < GRADE_STEP_PCT
                      and time.monotonic() - sent_at < GRADE_REFRESH_S):
                    continue
                if not have_control:
                    have_control = await self._command(client, replies, bytes([OP_REQUEST_CONTROL]))
                    if not have_control:
                        await asyncio.sleep(5.0)
                        continue
                    self.log("Trainer control granted")
                payload = struct.pack("<BhhBB", OP_SET_SIMULATION, 0, round(wanted * 100), CRR, CW)
                if await self._command(client, replies, payload):
                    if self.grade_set is None or abs(wanted - self.grade_set) >= 1.0:
                        self.log(f"Trainer slope {wanted:+.1f}%")
                    self.grade_set, sent_at = wanted, time.monotonic()
                else:
                    have_control = False
        except asyncio.CancelledError:
            raise
        except Exception as e:  # e.g. the trainer disconnected mid-command
            self.log(f"Trainer control stopped: {e!r}")

    def describe(self):
        if self.status == "off":
            return "trainer off (not used by this profile)"
        if self.status != "connected":
            return f"trainer {self.status}"
        d = self.data
        return f"trainer {d.speed_kmh:4.1f} km/h {d.cadence_rpm:3.0f} rpm {d.power_w:4.0f} W"
