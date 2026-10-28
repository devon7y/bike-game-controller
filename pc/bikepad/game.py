"""The truck games (American Truck Simulator, Euro Truck Simulator 2): finding
them, installing the telemetry plugin (see game_plugin/ in the source), and
receiving what it sends."""
import filecmp
import math
import re
import shutil
import socket
import threading
import time
from dataclasses import dataclass
from pathlib import Path

PORT = 47270
STALE_SECONDS = 1.5
PLUGIN_NAME = "bike_controller_telemetry.dll"
PLUGIN_SOURCE = Path(__file__).resolve().parent.parent / "game_plugin" / PLUGIN_NAME
GAMES = {"ats": "American Truck Simulator", "eut2": "Euro Truck Simulator 2"}


@dataclass
class GameData:
    game: str = None         # a GAMES key, or None while no game is sending
    paused: bool = True
    speed_kmh: float = 0.0
    grade_pct: float = 0.0   # road slope in the direction of travel; positive = uphill
    brake: float = 0.0       # 0..1
    received: float = 0.0    # time.monotonic()


def parse(line):
    """Returns GameData for a plugin packet, or None if it isn't one."""
    parts = line.strip().split(",")
    if len(parts) != 6 or parts[0] != "BCT1":
        return None
    try:
        speed, pitch, brake = float(parts[3]), float(parts[4]), float(parts[5])
    except ValueError:
        return None
    grade = math.tan(pitch * 2 * math.pi) * 100  # pitch is in turns: 0.25 = 90 degrees, nose up
    if speed < -0.5:  # reversing, so uphill is behind the truck
        grade = -grade
    return GameData(parts[1], parts[2] == "1", speed * 3.6, grade, brake, time.monotonic())


class GameTelemetry:
    """Listens for the plugin's packets in a background thread."""

    def __init__(self):
        self._data = GameData()
        self.error = None
        threading.Thread(target=self._listen, daemon=True).start()

    @property
    def data(self):
        d = self._data
        return d if time.monotonic() - d.received < STALE_SECONDS else GameData()

    def _listen(self):
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.bind(("127.0.0.1", PORT))
        except OSError as e:
            self.error = f"Can't listen for the game on port {PORT} ({e})."
            return
        while True:
            packet, _ = sock.recvfrom(256)
            d = parse(packet.decode("ascii", "replace"))
            if d:
                self._data = d


def _steam_libraries():
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam")
        steam = Path(winreg.QueryValueEx(key, "SteamPath")[0])
    except (ImportError, OSError):
        return []
    libraries = [steam]
    vdf = steam / "steamapps" / "libraryfolders.vdf"
    if vdf.exists():
        for path in re.findall(r'"path"\s+"([^"]+)"', vdf.read_text(errors="replace")):
            libraries.append(Path(path.replace("\\\\", "\\")))
    return libraries


def find_games():
    """{GAMES key: install folder} for the truck games in the Steam libraries."""
    found = {}
    for library in _steam_libraries():
        for key, name in GAMES.items():
            folder = library / "steamapps" / "common" / name
            if key not in found and (folder / "bin" / "win_x64").is_dir():
                found[key] = folder
    return found


def plugin_path(folder):
    return folder / "bin" / "win_x64" / "plugins" / PLUGIN_NAME


def plugin_status(folder):
    """'installed', 'outdated' or 'missing'."""
    path = plugin_path(folder)
    if not path.exists():
        return "missing"
    return "installed" if filecmp.cmp(path, PLUGIN_SOURCE, shallow=False) else "outdated"


def install_plugin(folder):
    """Copies the plugin into the game. Raises OSError if that fails (e.g. the
    game is running and has the old one loaded)."""
    path = plugin_path(folder)
    path.parent.mkdir(exist_ok=True)
    shutil.copyfile(PLUGIN_SOURCE, path)
