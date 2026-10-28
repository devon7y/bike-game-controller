"""Serial link to the ESP32: reads, checks and queues its sensor lines."""
import queue
import threading
import time
from dataclasses import dataclass

import serial
from serial.tools import list_ports

BAUD = 921600
CP2102_VID_PID = (0x10C4, 0xEA60)
ACCEL_COUNTS_PER_G = 16384.0
GYRO_COUNTS_PER_DPS = 65.5
REPLAY_WINDOW = 1000  # samples; see Link._read_forever


@dataclass
class Sample:
    seq: int
    t_us: int         # ESP32 clock in microseconds; wraps every ~71 minutes
    mpu_ok: bool
    accel: tuple      # g, in sensor axes
    gyro: tuple       # deg/s, in sensor axes
    brakes_mv: tuple  # (brake A, brake B)
    zwift: int = 0    # Zwift steering link: 0 off, 1 advertising, 2 connected, 3 ready


def parse(line):
    """Returns a Sample, or None if the line is incomplete or fails its checksum."""
    body, star, check = line.strip().partition("*")
    if not star or not body.startswith("D,"):
        return None
    x = 0
    for ch in body:
        x ^= ord(ch)
    try:
        if x != int(check, 16):
            return None
        fields = [int(v) for v in body[2:].split(",")]
    except ValueError:
        return None
    if len(fields) == 11:  # firmware from before Zwift steering
        fields.append(0)
    if len(fields) != 12:
        return None
    seq, t_us, ok, ax, ay, az, gx, gy, gz, brake_a, brake_b, zwift = fields
    a, g = ACCEL_COUNTS_PER_G, GYRO_COUNTS_PER_DPS
    return Sample(seq, t_us, bool(ok), (ax / a, ay / a, az / a),
                  (gx / g, gy / g, gz / g), (brake_a, brake_b), zwift)


def find_port():
    for p in list_ports.comports():
        if (p.vid, p.pid) == CP2102_VID_PID:
            return p.device
    return None


class Link:
    """Reads the ESP32's serial port in a background thread and queues every
    valid sample once, in order. Reopens the port if the cable is unplugged."""

    def __init__(self, port=None):
        self.port = port
        self.samples = queue.Queue(maxsize=1000)
        self.bad_lines = 0
        self.connected = False
        self._port = None
        self._write_lock = threading.Lock()
        threading.Thread(target=self._read_forever, daemon=True).start()

    def send(self, text):
        """Sends a command line to the ESP32; dropped if it isn't connected."""
        with self._write_lock:
            port = self._port
            if port is None:
                return
            try:
                port.write(text.encode("ascii"))
            except serial.SerialException:
                pass

    def _open(self):
        s = serial.Serial()
        s.port = self.port or find_port()
        if s.port is None:
            raise serial.SerialException("ESP32 not found")
        s.baudrate = BAUD
        s.timeout = 0.5
        s.dtr = False  # leaving DTR and RTS low keeps the ESP32 from resetting
        s.rts = False
        s.open()
        s.reset_input_buffer()
        return s

    def _read_forever(self):
        last_seq = None
        while True:
            try:
                port = self._open()
            except serial.SerialException:
                time.sleep(1.0)
                continue
            self._port, self.connected = port, True
            try:
                while True:
                    raw = port.readline()
                    if not raw:
                        continue
                    s = parse(raw.decode("ascii", "replace"))
                    if s is None:
                        self.bad_lines += 1
                        continue
                    # After a power glitch the USB link can replay recent data:
                    # drop anything that isn't newer than the last sample.
                    if last_seq is not None:
                        d = (s.seq - last_seq) % 65536
                        if d == 0 or d > 65536 - REPLAY_WINDOW:
                            continue
                    last_seq = s.seq
                    try:
                        self.samples.put_nowait(s)
                    except queue.Full:
                        pass
            except serial.SerialException:
                with self._write_lock:
                    self._port, self.connected = None, False
                port.close()
                time.sleep(1.0)


def stream(link, max_dt=0.1):
    """Yields (sample, dt) for every sample, with dt in seconds from the ESP32's
    clock. Yields (None, 0) once a second while no data arrives."""
    prev = None
    while True:
        try:
            s = link.samples.get(timeout=1.0)
        except queue.Empty:
            prev = None
            yield None, 0.0
            continue
        dt = 0.0 if prev is None else ((s.t_us - prev) % 2**32) / 1e6
        prev = s.t_us
        yield s, min(dt, max_dt)
