"""The virtual Xbox 360 controller, through the ViGEmBus driver."""
import vgamepad as vg

from .profiles import Outputs

B = vg.XUSB_BUTTON
BUTTON_CODES = {
    "a": B.XUSB_GAMEPAD_A, "b": B.XUSB_GAMEPAD_B, "x": B.XUSB_GAMEPAD_X, "y": B.XUSB_GAMEPAD_Y,
    "lb": B.XUSB_GAMEPAD_LEFT_SHOULDER, "rb": B.XUSB_GAMEPAD_RIGHT_SHOULDER,
    "ls": B.XUSB_GAMEPAD_LEFT_THUMB, "rs": B.XUSB_GAMEPAD_RIGHT_THUMB,
    "start": B.XUSB_GAMEPAD_START, "back": B.XUSB_GAMEPAD_BACK,
    "dpad_up": B.XUSB_GAMEPAD_DPAD_UP, "dpad_down": B.XUSB_GAMEPAD_DPAD_DOWN,
    "dpad_left": B.XUSB_GAMEPAD_DPAD_LEFT, "dpad_right": B.XUSB_GAMEPAD_DPAD_RIGHT,
}


class Pad:
    """One virtual controller. It exists from creation until close()."""

    def __init__(self):
        self._pad = vg.VX360Gamepad()
        self._last = None

    def set(self, out):
        a, t = out.axes, out.triggers
        state = (round(a.get("left_x", 0.0), 3), round(a.get("left_y", 0.0), 3),
                 round(a.get("right_x", 0.0), 3), round(a.get("right_y", 0.0), 3),
                 round(t.get("left_trigger", 0.0), 3), round(t.get("right_trigger", 0.0), 3),
                 frozenset(out.buttons))
        if state == self._last:
            return
        self._last = state
        lx, ly, rx, ry, lt, rt, buttons = state
        p = self._pad
        p.left_joystick_float(x_value_float=lx, y_value_float=ly)
        p.right_joystick_float(x_value_float=rx, y_value_float=ry)
        p.left_trigger_float(value_float=lt)
        p.right_trigger_float(value_float=rt)
        for key, code in BUTTON_CODES.items():
            if key in buttons:
                p.press_button(button=code)
            else:
                p.release_button(button=code)
        p.update()

    def neutral(self):
        self.set(Outputs())

    def close(self):
        self._pad.reset()
        self._pad.update()
        del self._pad
