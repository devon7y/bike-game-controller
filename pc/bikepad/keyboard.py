"""Keyboard keys and mouse buttons sent to the game with Windows SendInput.
Keys go as scan codes, which games that read the keyboard directly
(DirectInput) also see."""
import ctypes
import ctypes.wintypes as W

# key id -> (label, scan code, extended key)
_KEY_TABLE = [
    ("space", "Space", 0x39, False), ("enter", "Enter", 0x1C, False),
    ("lshift", "Left Shift", 0x2A, False), ("rshift", "Right Shift", 0x36, False),
    ("lctrl", "Left Ctrl", 0x1D, False), ("rctrl", "Right Ctrl", 0x1D, True),
    ("lalt", "Left Alt", 0x38, False), ("ralt", "Right Alt", 0x38, True),
    ("tab", "Tab", 0x0F, False), ("esc", "Esc", 0x01, False), ("backspace", "Backspace", 0x0E, False),
    ("capslock", "Caps Lock", 0x3A, False),
    ("up", "Up arrow", 0x48, True), ("down", "Down arrow", 0x50, True),
    ("left", "Left arrow", 0x4B, True), ("right", "Right arrow", 0x4D, True),
    ("insert", "Insert", 0x52, True), ("delete", "Delete", 0x53, True),
    ("home", "Home", 0x47, True), ("end", "End", 0x4F, True),
    ("pageup", "Page Up", 0x49, True), ("pagedown", "Page Down", 0x51, True),
]
_KEY_TABLE += [(c.lower(), c, code, False) for c, code in zip("QWERTYUIOP", range(0x10, 0x1A))]
_KEY_TABLE += [(c.lower(), c, code, False) for c, code in zip("ASDFGHJKL", range(0x1E, 0x27))]
_KEY_TABLE += [(c.lower(), c, code, False) for c, code in zip("ZXCVBNM", range(0x2C, 0x33))]
_KEY_TABLE += [(d, d, code, False) for d, code in zip("1234567890", range(0x02, 0x0C))]
_KEY_TABLE += [(f"f{n}", f"F{n}", code, False) for n, code in zip(range(1, 11), range(0x3B, 0x45))]
_KEY_TABLE += [("f11", "F11", 0x57, False), ("f12", "F12", 0x58, False)]
_KEY_TABLE += [("minus", "- (minus)", 0x0C, False), ("equals", "= (equals)", 0x0D, False),
               ("lbracket", "[", 0x1A, False), ("rbracket", "]", 0x1B, False),
               ("semicolon", ";", 0x27, False), ("quote", "' (quote)", 0x28, False),
               ("grave", "` (backtick)", 0x29, False), ("backslash", "\\", 0x2B, False),
               ("comma", ", (comma)", 0x33, False), ("period", ". (full stop)", 0x34, False),
               ("slash", "/", 0x35, False)]
# mouse button id -> (label, down flag, up flag)
_MOUSE_TABLE = [("mouse_left", "Mouse left button", 0x0002, 0x0004),
                ("mouse_right", "Mouse right button", 0x0008, 0x0010),
                ("mouse_middle", "Mouse middle button", 0x0020, 0x0040)]

KEYS = {k: label for k, label, *_ in _MOUSE_TABLE + _KEY_TABLE}  # id -> label, in menu order
_SCAN = {k: (code, ext) for k, _, code, ext in _KEY_TABLE}
_MOUSE = {k: (down, up) for k, _, down, up in _MOUSE_TABLE}

KEYEVENTF_EXTENDEDKEY, KEYEVENTF_KEYUP, KEYEVENTF_SCANCODE = 0x0001, 0x0002, 0x0008
INPUT_MOUSE, INPUT_KEYBOARD = 0, 1


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", W.LONG), ("dy", W.LONG), ("mouseData", W.DWORD), ("dwFlags", W.DWORD),
                ("time", W.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", W.WORD), ("wScan", W.WORD), ("dwFlags", W.DWORD), ("time", W.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


class _INPUT(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT)]
    _anonymous_ = ("u",)
    _fields_ = [("type", W.DWORD), ("u", _U)]


def _send(key, down):
    item = _INPUT()
    if key in _MOUSE:
        item.type = INPUT_MOUSE
        item.mi.dwFlags = _MOUSE[key][0 if down else 1]
    else:
        code, ext = _SCAN[key]
        item.type = INPUT_KEYBOARD
        item.ki.wScan = code
        item.ki.dwFlags = (KEYEVENTF_SCANCODE | (KEYEVENTF_EXTENDEDKEY if ext else 0)
                           | (0 if down else KEYEVENTF_KEYUP))
    ctypes.windll.user32.SendInput(1, ctypes.byref(item), ctypes.sizeof(item))


class Keyboard:
    """Holds down exactly the keys and mouse buttons it was last given."""

    def __init__(self):
        self.down = set()

    def set(self, keys):
        keys = {k for k in keys if k in KEYS}
        for k in self.down - keys:
            _send(k, down=False)
        for k in keys - self.down:
            _send(k, down=True)
        self.down = keys

    def release_all(self):
        self.set(set())
