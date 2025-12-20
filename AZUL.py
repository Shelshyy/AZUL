

class PID:
    def __init__(self, kp=0.01, ki=0.001, kd=0.005):
        self.kp = kp
        self.ki = ki
        self.kd = kd
        self.integral = 0
        self.prev_error = 0

    def update(self, error, dt):
        self.integral += error * dt
        derivative = (error - self.prev_error) / dt if dt > 0 else 0
        output = self.kp * error + self.ki * self.integral + self.kd * derivative
        self.prev_error = error
        return output


import threading
import time
import os, json
import urllib.request
import shutil
AZUL_ICON_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "AZUL.png")
AZUL_ICON_BGR = None

from typing import Optional

try:
    import vgamepad as vg
except Exception:
    vg = None

try:
    import hid  # pip install hidapi
except Exception:
    hid = None


def _log(*args):
    print("[controller]", *args)


class VirtualController:
    """
    Thin wrapper around vgamepad.VX360Gamepad (or any future backend),
    with helpers for normalized -1..1 stick input and 0..1 triggers.
    """

    LAST = None

    def __init__(self):
        self._lock = threading.Lock()
        self._backend = None
        self._update_rate_hz = 1000

        self._rx = 0.0
        self._ry = 0.0
        self._lx = 0.0
        self._ly = 0.0
        self._lt = 0.0
        self._rt = 0.0

        self._micro_dither = (False, 0.0, 0.0)
        self._adaptive_bite = (False, 0.0, 0.0, 0.0, 0.0)

        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

        VirtualController.LAST = self

    def _attach_backend(self, backend):
        with self._lock:
            self._backend = backend

    def stop(self):
        self._running = False
        try:
            self._thread.join(timeout=1.0)
        except Exception:
            pass

    @staticmethod
    def _clamp16(v) -> int:
        if isinstance(v, int):
            if v < -32768:
                return -32768
            if v > 32767:
                return 32767
            return v
        try:
            v = float(v)
        except Exception:
            return 0
        if v <= -1.0:
            return -32768
        if v >= 1.0:
            return 32767
        return int(v * 32767.0)

    @staticmethod
    def _trig_to_int(v) -> int:
        if isinstance(v, int):
            if v < 0:
                return 0
            if v > 255:
                return 255
            return int(v)
        try:
            v = float(v)
        except Exception:
            return 0
        if v <= 0.0:
            return 0
        if v >= 1.0:
            return 255
        return int(v * 255.0)

    def set_update_rate_hz(self, hz: int):
        if hz and hz > 0:
            self._update_rate_hz = int(hz)

    def set_micro_dither(self, enabled: bool, amp: float, threshold: float):
        self._micro_dither = (bool(enabled), float(amp), float(threshold))

    def set_adaptive_bite(self, enabled: bool, base: float, maxv: float, step: float, decay: float):
        self._adaptive_bite = (
            bool(enabled),
            float(base),
            float(maxv),
            float(step),
            float(decay),
        )

    def set_right_stick(self, x: float, y: float):
        with self._lock:
            self._rx = max(-1.0, min(1.0, float(x)))
            self._ry = max(-1.0, min(1.0, float(y)))

    def set_left_stick(self, x: float, y: float):
        with self._lock:
            self._lx = max(-1.0, min(1.0, float(x)))
            self._ly = max(-1.0, min(1.0, float(y)))

    def set_triggers(self, lt: float = None, rt: float = None):
        with self._lock:
            if lt is not None:
                self._lt = max(0.0, min(1.0, float(lt)))
            if rt is not None:
                self._rt = max(0.0, min(1.0, float(rt)))

    def get_right_injection(self):
        """Return the current normalized right-stick injection (x, y)."""
        with self._lock:
            return self._rx, self._ry


    def get_left_injection(self):
        """Return the current normalized left-stick injection (x, y)."""
        with self._lock:
            return self._lx, self._ly

    def get_trigger_injection(self):
        """Return the current normalized trigger injection (lt, rt)."""
        with self._lock:
            return self._lt, self._rt

    def _apply_curves_and_dither(self, v):
        enabled, amp, threshold = self._micro_dither
        if not enabled:
            return v
        if abs(v) < threshold:
            return 0.0
        return v

    def _loop(self):
        period = 1.0 / float(self._update_rate_hz or 1000)
        while self._running:
            time.sleep(period)



_PS4_PIDS = {0x05C4, 0x09CC}
_PS5_PIDS = {0x0CE6, 0x0DF2}


class PS4PS5ToDS4Bridge:
    EMA = 1.0
    POLL_HZ_MIN = 1000

    def __init__(self, poll_hz: int = 1000, vc: Optional[VirtualController] = None):
        if vg is None or not hasattr(vg, "VX360Gamepad") or hid is None:
            raise RuntimeError("Bridge requires vgamepad.VX360Gamepad + hidapi")

        self._dt = 1.0 / float(max(self.POLL_HZ_MIN, poll_hz))
        self._stop = threading.Event()
        self._dev: Optional["hid.device"] = None
        self._vx360: Optional["vg.VX360Gamepad"] = None

        self._vc = vc or VirtualController.LAST or VirtualController()
        self._left_inj_scale = 1.0
        self._right_inj_scale = 0.5
        self._trig_inj_scale = 1.0

        self._ema = {"lx": 0.0, "ly": 0.0, "rx": 0.0, "ry": 0.0}
        self._last_axes = {"lx": 0, "ly": 0, "rx": 0, "ry": 0}
        self._last_trig = {"l2": 0, "r2": 0}
        self._btn_down = set()

        self._l1_held = False
        self._l2_held = False

        self._is_dualsense = False

        t = threading.Thread(target=self._loop, name="PS4PS5ToX360Bridge", daemon=True)
        t.start()
        self._thread = t

        _log(f"[Bridge] Thread started at ~{1.0/self._dt:.0f} Hz")

    def stop(self):
        self._stop.set()

    
    def set_injection_scales(self, left: float=None, right: float=None, trig: float=None):
        """
        Set scaling for additive injections (normalized 0..1).
        Typical: left=1.0 for strong micro-strafe, right=0.5 to keep aim subtle.
        """
        if left is not None:
            try: self._left_inj_scale = float(left)
            except Exception: pass
        if right is not None:
            try: self._right_inj_scale = float(right)
            except Exception: pass
        if trig is not None:
            try: self._trig_inj_scale = float(trig)
            except Exception: pass
    def _open_physical(self) -> bool:
        if self._dev is not None:
            return True

        if hid is None:
            _log("[Bridge] hidapi not available; cannot attach to controller.")
            return False

        found = None
        try:
            devices = list(hid.enumerate())
            for d in devices:
                vid = d.get("vendor_id", 0)
                pid = d.get("product_id", 0)
                path = d.get("path")
                mfg = (d.get("manufacturer_string") or "").lower()
                prod = (d.get("product_string") or "").lower()
                if vid == 0x054C and (
                    pid in _PS5_PIDS
                    or pid in _PS4_PIDS
                    or "dualsense" in prod
                    or "dualshock" in prod
                    or "wireless controller" in prod
                ):
                    found = (vid, pid, path)
                    break

            if not found:
                for d in devices:
                    vid = d.get("vendor_id", 0)
                    pid = d.get("product_id", 0)
                    path = d.get("path")
                    if pid in _PS5_PIDS or pid in _PS4_PIDS:
                        found = (vid, pid, path)
                        break

            if not found:
                _log("[Bridge] No PS4/PS5 controller HID device found.  "
                     "If you use DS4Windows/HidHide, make sure this process is allowed.")
                try:
                    for d in devices:
                        _log(
                            "[Bridge] HID device:",
                            hex(d.get("vendor_id", 0)),
                            hex(d.get("product_id", 0)),
                            (d.get("product_string") or "").strip()
                        )
                except Exception:
                    pass
                return False

            vid, pid, path = found
            d = hid.device()
            if path:
                d.open_path(path)
            else:
                d.open(vid, pid)
            d.set_nonblocking(True)
            self._dev = d
            self._is_dualsense = pid in _PS5_PIDS
            _log(f"[Bridge] Attached to {vid:04X}:{pid:04X} (DualSense={self._is_dualsense})")
            return True
        except Exception as e:
            _log("[Bridge] open() failed:", e)
            try:
                d.close()
            except Exception:
                pass
            self._dev = None
            return False


    def _drop_physical(self):
        if self._dev is not None:
            try:
                self._dev.close()
            except Exception:
                pass
            self._dev = None

    def _ensure_vx360(self) -> bool:
        if self._vx360 is not None:
            return True
        if vg is None or not hasattr(vg, "VX360Gamepad"):
            return False
        try:
            self._vx360 = vg.VX360Gamepad()
            self._vc._attach_backend(self._vx360)
            _log("[Bridge] Created vX360 backend")
            return True
        except Exception as e:
            _log("[Bridge] Failed to create vX360:", e)
            self._vx360 = None
            return False

    def _drop_vx360(self):
        if self._vx360 is not None:
            try:
                self._vx360.reset()
                self._vx360.update()
            except Exception:
                pass
            _log("[Bridge] Dropped virtual Xbox 360 pad.")
            self._vx360 = None
            self._vc._attach_backend(None)
        self._btn_down.clear()
        self._last_axes = {"lx": 0, "ly": 0, "rx": 0, "ry": 0}
        self._last_trig = {"l2": 0, "r2": 0}
        self._l1_held = False
        self._l2_held = False

    def is_l1_held(self) -> bool:
        return bool(self._l1_held)

    def is_l2_held(self) -> bool:
        return bool(self._l2_held)

    @staticmethod
    def _to_unit(b: int) -> float:
        if b <= 0:
            return -1.0
        if b >= 255:
            return 1.0
        return (b - 128) / 127.0

    def _ema_update(self, key: str, target: float) -> float:
        self._ema[key] = target
        return target

    def _decode_hat(self, data, rid):
        if len(data) < 6:
            return 8
        if self._is_dualsense and rid == 0x01 and len(data) > 8:
            hat = data[8] & 0x0F
        else:
            hat = data[5] & 0x0F
        if hat > 8:
            return 8
        return hat

    def _decode_buttons(self, data, rid):
        if vg is None or len(data) < 7:
            return set()
        new = set()

        hat = self._decode_hat(data, rid)
        if hat == 0:
            new.add(vg.XUSB_BUTTON.XUSB_GAMEPAD_DPAD_UP)
        elif hat == 2:
            new.add(vg.XUSB_BUTTON.XUSB_GAMEPAD_DPAD_RIGHT)
        elif hat == 4:
            new.add(vg.XUSB_BUTTON.XUSB_GAMEPAD_DPAD_DOWN)
        elif hat == 6:
            new.add(vg.XUSB_BUTTON.XUSB_GAMEPAD_DPAD_LEFT)

        b5 = data[5]
        b6 = data[6]
        b8 = data[8] if len(data) > 8 else 0
        b9 = data[9] if len(data) > 9 else 0
        b7 = data[7] if len(data) > 7 else 0
        b10 = data[10] if len(data) > 10 else 0

        if self._is_dualsense and rid == 0x01 and len(data) > 10:
            if b8 & 0x10:
                new.add(vg.XUSB_BUTTON.XUSB_GAMEPAD_X)  # Square
            if b8 & 0x20:
                new.add(vg.XUSB_BUTTON.XUSB_GAMEPAD_A)  # Cross
            if b8 & 0x40:
                new.add(vg.XUSB_BUTTON.XUSB_GAMEPAD_B)  # Circle
            if b8 & 0x80:
                new.add(vg.XUSB_BUTTON.XUSB_GAMEPAD_Y)  # Triangle

            if b9 & 0x01:
                new.add(vg.XUSB_BUTTON.XUSB_GAMEPAD_LEFT_SHOULDER)
            if b9 & 0x02:
                new.add(vg.XUSB_BUTTON.XUSB_GAMEPAD_RIGHT_SHOULDER)
            if b9 & 0x40:
                new.add(vg.XUSB_BUTTON.XUSB_GAMEPAD_LEFT_THUMB)
            if b9 & 0x80:
                new.add(vg.XUSB_BUTTON.XUSB_GAMEPAD_RIGHT_THUMB)

            if b9 & 0x10:
                new.add(vg.XUSB_BUTTON.XUSB_GAMEPAD_BACK)
            if b9 & 0x20:
                new.add(vg.XUSB_BUTTON.XUSB_GAMEPAD_START)

        else:
            if b5 & 0x10:
                new.add(vg.XUSB_BUTTON.XUSB_GAMEPAD_X)  # Square
            if b5 & 0x20:
                new.add(vg.XUSB_BUTTON.XUSB_GAMEPAD_A)  # Cross
            if b5 & 0x40:
                new.add(vg.XUSB_BUTTON.XUSB_GAMEPAD_B)  # Circle
            if b5 & 0x80:
                new.add(vg.XUSB_BUTTON.XUSB_GAMEPAD_Y)  # Triangle

            if b6 & 0x01:
                new.add(vg.XUSB_BUTTON.XUSB_GAMEPAD_LEFT_SHOULDER)
            if b6 & 0x02:
                new.add(vg.XUSB_BUTTON.XUSB_GAMEPAD_RIGHT_SHOULDER)
            if b6 & 0x40:
                new.add(vg.XUSB_BUTTON.XUSB_GAMEPAD_LEFT_THUMB)
            if b6 & 0x80:
                new.add(vg.XUSB_BUTTON.XUSB_GAMEPAD_RIGHT_THUMB)

            if b6 & 0x10:
                new.add(vg.XUSB_BUTTON.XUSB_GAMEPAD_BACK)
            if b6 & 0x20:
                new.add(vg.XUSB_BUTTON.XUSB_GAMEPAD_START)


        return new


    def _apply_buttons(self, new_buttons: set):
        if self._vx360 is None:
            return False
        changed = False
        for btn in list(self._btn_down):
            if btn not in new_buttons:
                try:
                    self._vx360.release_button(button=btn)
                except Exception:
                    pass
                changed = True
        for btn in new_buttons:
            if btn not in self._btn_down:
                try:
                    self._vx360.press_button(button=btn)
                except Exception:
                    pass
                changed = True
        self._btn_down = set(new_buttons)
        return changed

    def _handle_report(self, data: bytes):
        if self._vx360 is None and not self._ensure_vx360():
            return
        if isinstance(data, bytes):
            data = list(data)
        if not data:
            return
        rid = data[0]

        off_lx = off_ly = off_rx = off_ry = None
        off_l2 = off_r2 = None

        if rid == 0x01 and len(data) >= 11:
            off_lx, off_ly, off_rx, off_ry = 1, 2, 3, 4
            off_l2, off_r2 = 5, 6
        elif rid in (0x30, 0x31, 0x32) and len(data) >= 10:
            off_lx, off_ly, off_rx, off_ry = 1, 2, 3, 4
            cand_l2 = data[5]
            cand_r2 = data[6]
            cand2_l2 = data[8] if len(data) > 8 else 0
            cand2_r2 = data[9] if len(data) > 9 else 0
            if cand_l2 or cand_r2 or not (cand2_l2 or cand2_r2):
                off_l2, off_r2 = 5, 6
            else:
                off_l2, off_r2 = 8, 9
        else:
            return

        max_idx = len(data) - 1
        for idx in (off_lx, off_ly, off_rx, off_ry, off_l2, off_r2):
            if idx is None or idx > max_idx:
                return

        l2_raw = data[off_l2]
        r2_raw = data[off_r2]
        if l2_raw < 3:
            l2_raw = 0
        if r2_raw < 3:
            r2_raw = 0

        lx = self._ema_update("lx", self._to_unit(data[off_lx]))
        ly = self._ema_update("ly", -self._to_unit(data[off_ly]))
        rx = self._ema_update("rx", self._to_unit(data[off_rx]))
        ry = self._ema_update("ry", -self._to_unit(data[off_ry]))

        lx_i = int(max(-32768, min(32767, lx * 32767.0)))
        ly_i = int(max(-32768, min(32767, ly * 32767.0)))
        rx_i = int(max(-32768, min(32767, rx * 32767.0)))
        ry_i = int(max(-32768, min(32767, ry * 32767.0)))

        try:
            lshape = getattr(self._vc, "lshape", None)
            rshape = getattr(self._vc, "rshape", None)
            if lshape is not None and getattr(lshape, "enabled", False):
                lx_i, ly_i = lshape.shape_ints(lx_i, ly_i)
            if rshape is not None and getattr(rshape, "enabled", False):
                rx_i, ry_i = rshape.shape_ints(rx_i, ry_i)
        except Exception:
            pass

        try:
            if self._vc is not None:
                inj_lx, inj_ly = self._vc.get_left_injection()
                inj_rx, inj_ry = self._vc.get_right_injection()
                inj_lt, inj_rt = self._vc.get_trigger_injection()
            else:
                inj_lx = inj_ly = inj_rx = inj_ry = inj_lt = inj_rt = 0.0
        except Exception:
            inj_lx = inj_ly = inj_rx = inj_ry = inj_lt = inj_rt = 0.0

        if inj_lx or inj_ly:
            inj_lx_i = int(max(-32768, min(32767, inj_lx * 32767.0 * self._left_inj_scale)))
            inj_ly_i = int(max(-32768, min(32767, inj_ly * 32767.0 * self._left_inj_scale)))
            lx_i = int(max(-32768, min(32767, lx_i + inj_lx_i)))
            ly_i = int(max(-32768, min(32767, ly_i + inj_ly_i)))

        if inj_rx or inj_ry:
            inj_rx_i = int(max(-32768, min(32767, inj_rx * 32767.0 * self._right_inj_scale)))
            inj_ry_i = int(max(-32768, min(32767, inj_ry * 32767.0 * self._right_inj_scale)))
            rx_i = int(max(-32768, min(32767, rx_i + inj_rx_i)))
            ry_i = int(max(-32768, min(32767, ry_i + inj_ry_i)))

        l2 = int(l2_raw)
        r2 = int(r2_raw)

        if inj_lt > 0.0:
            inj_l2 = int(max(0, min(255, inj_lt * 255.0 * self._trig_inj_scale)))
            if inj_l2 > l2:
                l2 = inj_l2
        if inj_rt > 0.0:
            inj_r2 = int(max(0, min(255, inj_rt * 255.0 * self._trig_inj_scale)))
            if inj_r2 > r2:
                r2 = inj_r2

        new_buttons = self._decode_buttons(data, rid)

        try:
            if vg is not None:
                self._l1_held = vg.XUSB_BUTTON.XUSB_GAMEPAD_LEFT_SHOULDER in new_buttons
            else:
                self._l1_held = False
        except Exception:
            self._l1_held = False

        self._l2_held = l2 > 30

        changed = False

        if (lx_i != self._last_axes["lx"]) or (ly_i != self._last_axes["ly"]):
            try:
                self._vx360.left_joystick(x_value=lx_i, y_value=ly_i)
                self._last_axes["lx"] = lx_i
                self._last_axes["ly"] = ly_i
                changed = True
            except Exception:
                pass

        if (rx_i != self._last_axes["rx"]) or (ry_i != self._last_axes["ry"]):
            try:
                self._vx360.right_joystick(x_value=rx_i, y_value=ry_i)
                self._last_axes["rx"] = rx_i
                self._last_axes["ry"] = ry_i
                changed = True
            except Exception:
                pass

        if l2 != self._last_trig["l2"]:
            try:
                self._vx360.left_trigger(value=l2)
                self._last_trig["l2"] = l2
                changed = True
            except Exception:
                pass

        if r2 != self._last_trig["r2"]:
            try:
                self._vx360.right_trigger(value=r2)
                self._last_trig["r2"] = r2
                changed = True
            except Exception:
                pass

        if self._apply_buttons(new_buttons):
            changed = True

        if changed:
            try:
                self._vx360.update()
            except Exception as e:
                _log("[Bridge] vX360 update failed:", e)

    def _loop(self):
        while not self._stop.is_set():
            if not self._open_physical():
                self._drop_vx360()
                time.sleep(0.5)
                continue

            if not self._ensure_vx360():
                time.sleep(0.5)
                continue

            try:
                data = self._dev.read(64)
            except Exception as e:
                _log("[Bridge] read() failed:", e)
                self._drop_physical()
                self._drop_vx360()
                time.sleep(0.5)
                continue

            if data:
                try:
                    self._handle_report(bytes(data))
                except Exception as e:
                    _log("[Bridge] handle_report() error:", e)

            time.sleep(self._dt)

        self._drop_physical()
        self._drop_vx360()



class LeftStickMicroRotator:
    """
    While L1 or L2 is held, adds tiny, continuous micro-rotational corrections
    on the LEFT stick via the VirtualController injection path. Designed to be subtle.
    """
    def __init__(
        self,
        vc: VirtualController,
        bridge: PS4PS5ToDS4Bridge,
        *,
        amp_x: float = 0.18,
        amp_y: float = 0.07,
        base_freq_hz: float = 2.4,
        jitter: float = 0.12,
        mode: str = "circle",  # "circle" | "figure8" | "horizontal"
        update_hz: int = 500,
        engage=None,
    ):
        import math, random, threading, time
        self._math = math
        self._random = random
        self._time = time
        self._threading = threading

        self.vc = vc
        self.bridge = bridge
        self.amp_x = float(amp_x)
        self.amp_y = float(amp_y)
        self.base_freq_hz = float(base_freq_hz)
        self.jitter = float(jitter)
        self.mode = mode
        self.dt = 1.0 / float(max(1, int(update_hz)))
        self._phase = random.random() * math.tau
        self._t0 = time.perf_counter()
        self._run = False
        self._thr = None
        self.engage = (lambda b: b.is_l1_held() or b.is_l2_held()) if engage is None else engage

    def start(self):
        if self._run:
            return
        self._run = True
        self._thr = self._threading.Thread(target=self._loop, daemon=True)
        self._thr.start()

    def stop(self):
        self._run = False
        if self._thr:
            try:
                self._thr.join(timeout=0.2)
            except Exception:
                pass
            self._thr = None
        try:
            self.vc.set_left_stick(0.0, 0.0)
        except Exception:
            pass

    def _loop(self):
        last_jitter = self._time.perf_counter()
        while self._run:
            t = self._time.perf_counter()
            active = False
            try:
                active = bool(self.engage(self.bridge))
            except Exception:
                active = False

            if active:
                if (t - last_jitter) > 0.6:
                    try:
                        self._phase = self._random.random() * self._math.tau
                    except Exception:
                        pass
                    last_jitter = t

                freq = self.base_freq_hz * (1.0 + self.jitter * (self._random.random() * 2.0 - 1.0))
                w = self._math.tau * freq * (t - self._t0) + self._phase

                if self.mode == "horizontal":
                    lx = self.amp_x * self._math.sin(w)
                    ly = 0.0
                elif self.mode == "figure8":
                    lx = self.amp_x * self._math.sin(w)
                    ly = self.amp_y * self._math.sin(2.0 * w)
                else:
                    lx = self.amp_x * self._math.sin(w)
                    ly = self.amp_y * self._math.cos(w)

                if lx > 1.0: lx = 1.0
                if lx < -1.0: lx = -1.0
                if ly > 1.0: ly = 1.0
                if ly < -1.0: ly = -1.0
                try:
                    self.vc.set_left_stick(lx, ly)
                except Exception:
                    pass
            else:
                try:
                    self.vc.set_left_stick(0.0, 0.0)
                except Exception:
                    pass

            self._time.sleep(self.dt)



class RightStickPIDInjector:
    """
    Runs two PIDs (x,y) at high rate and writes into the VirtualController right stick.
    Error must be normalized to [-1..1] where +X = right, +Y = up.
    Defaults to engaging while L1 or L2 is held (via the HID bridge).
    """
    def __init__(self, vc, bridge, *, kp=0.20, ki=0.0, kd=0.05, update_hz=500, engage=None, max_out=1.0):
        import threading, time
        self.vc = vc
        self.bridge = bridge
        try:
            _PID = PID  # type: ignore[name-defined]
        except Exception:
            class _PID:
                def __init__(self, kp=0.2, ki=0.0, kd=0.05):
                    self.kp, self.ki, self.kd = kp, ki, kd
                    self.i = 0.0
                    self.prev = 0.0
                def update(self, e, dt):
                    self.i += e * dt
                    d = (e - self.prev) / dt if dt > 0 else 0.0
                    self.prev = e
                    return self.kp*e + self.ki*self.i + self.kd*d

        self.pid_x = _PID(kp, ki, kd)
        self.pid_y = _PID(kp, ki, kd)
        self.dt = 1.0 / float(max(1, int(update_hz)))
        self.max_out = float(max_out)
        self._err = (0.0, 0.0)
        self._lock = threading.Lock()
        self._run = False
        self._thr = None
        self.engage = (lambda b: getattr(b, "is_l1_held", lambda: False)() or getattr(b, "is_l2_held", lambda: False)()) if engage is None else engage
        self._time = time
        self._threading = threading

    def set_error(self, ex: float, ey: float):
        ex = -1.0 if ex < -1.0 else (1.0 if ex > 1.0 else float(ex))
        ey = -1.0 if ey < -1.0 else (1.0 if ey > 1.0 else float(ey))
        with self._lock:
            self._err = (ex, ey)

    def set_gains(self, kp=None, ki=None, kd=None):
        if kp is not None: self.pid_x.kp = self.pid_y.kp = float(kp)
        if ki is not None: self.pid_x.ki = self.pid_y.ki = float(ki)
        if kd is not None: self.pid_x.kd = self.pid_y.kd = float(kd)

    def start(self):
        if self._run: return
        self._run = True
        self._thr = self._threading.Thread(target=self._loop, daemon=True)
        self._thr.start()

    def stop(self):
        self._run = False
        if self._thr:
            try: self._thr.join(timeout=0.2)
            except Exception: pass
            self._thr = None
        try: self.vc.set_right_stick(0.0, 0.0)
        except Exception: pass

    def _loop(self):
        last = self._time.perf_counter()
        while self._run:
            now = self._time.perf_counter()
            dt = max(1e-3, now - last); last = now

            try:
                active = bool(self.engage(self.bridge)) if callable(self.engage) else bool(self.engage)
            except Exception:
                active = True

            with self._lock:
                ex, ey = self._err

            out_x = self.pid_x.update(ex, dt) if active else 0.0
            out_y = self.pid_y.update(ey, dt) if active else 0.0

            m = self.max_out
            if out_x < -m: out_x = -m
            if out_x >  m: out_x =  m
            if out_y < -m: out_y = -m
            if out_y >  m: out_y =  m

            try:
                self.vc.set_right_stick(out_x, out_y)
            except Exception:
                pass

            self._time.sleep(self.dt)


def attach_right_stick_pid(injector, *, kp=0.20, ki=0.0, kd=0.05, update_hz=500, max_out=1.0):
    """
    Utility: attach and start a RightStickPIDInjector to a BlendedControllerInjector.
    Returns the created injector. Also monkey-patches helper methods onto the instance.
    """
    try:
        vc = injector.vc
        bridge = injector.bridge
    except Exception as e:
        try:
            _log("attach_right_stick_pid: injector missing vc/bridge:", e)
        except Exception:
            pass
        raise

    pid = RightStickPIDInjector(vc, bridge, kp=kp, ki=ki, kd=kd, update_hz=update_hz, max_out=max_out)
    try:
        if hasattr(bridge, "set_injection_scales"):
            bridge.set_injection_scales(right=1.0)
    except Exception:
        pass

    setattr(injector, "_right_pid", pid)
    pid.start()

    def _set_error(ex, ey):
        return pid.set_error(ex, ey)
    def _set_gains(kp=None, ki=None, kd=None):
        return pid.set_gains(kp, ki, kd)
    def _stop_pid():
        return pid.stop()
    def _start_pid():
        return pid.start()

    try:
        injector.set_right_pid_error = _set_error
        injector.set_right_pid_gains = _set_gains
        injector.stop_right_pid = _stop_pid
        injector.start_right_pid = _start_pid
    except Exception:
        pass
    return pid


class BlendedControllerInjector:
    """Adapter used by Razorcore overlay."""

    def __init__(self):
        self.vc = VirtualController()
        self.bridge = None
        try:
            self.bridge = PS4PS5ToDS4Bridge(vc=self.vc)
            try:
                self.bridge.set_injection_scales(left=1.0, right=0.5)
            except Exception:
                pass
            try:
                self._left_micro = LeftStickMicroRotator(self.vc, self.bridge)
                self._left_micro.start()
            except Exception as _e:
                self._left_micro = None
        except Exception as e:
            _log("[Blended] Bridge not started:", e)
            self.bridge = None

    @staticmethod
    def _clamp_unit(v: float) -> float:
        try:
            v = float(v)
        except Exception:
            return 0.0
        if v < -1.0:
            return -1.0
        if v > 1.0:
            return 1.0
        return v

    def move_right_stick(self, x: float, y: float):
        nx = self._clamp_unit(x)
        ny = self._clamp_unit(y)
        self.vc.set_right_stick(nx, ny)

    def move_left_stick(self, x: float, y: float):
        nx = self._clamp_unit(x)
        ny = self._clamp_unit(y)
        self.vc.set_left_stick(nx, ny)


    def set_r2(self, value: float):
        """Helper used by automation module to drive right trigger (R2/RT)
        via the VirtualController trigger injection path.
        This does NOT change any stick or button mapping."""
        try:
            self.vc.set_triggers(rt=float(value))
        except Exception:
            return


    def update(self, blending: bool = True):
        return

    def is_l1_held(self):
        if self.bridge is not None and hasattr(self.bridge, "is_l1_held"):
            return bool(self.bridge.is_l1_held())
        return None

    def is_l2_held(self):
        if self.bridge is not None and hasattr(self.bridge, "is_l2_held"):
            return bool(self.bridge.is_l2_held())
        return None

    
    def configure_left_micro(self, **kwargs):
        """
        Update micro-rotation parameters at runtime. Supported kwargs:
        amp_x, amp_y, base_freq_hz, jitter, mode, engage, update_hze, update_hz.
        """
        try:
            if getattr(self, "_left_micro", None) is not None:
                for k, v in kwargs.items():
                    if hasattr(self._left_micro, k):
                        setattr(self._left_micro, k, v)
        except Exception:
            pass
def stop(self):
        try:
            if self.bridge is not None:
                self.bridge.stop()
        except Exception:
            pass
        try:
            self.vc.stop()
        except Exception:
            pass



from dataclasses import dataclass
from typing import Any, Optional, Tuple
import hashlib
import importlib
import os
import sys
import types

KEYAUTH_NAME = "AZUL"
KEYAUTH_OWNER_ID = "Wft7DiaZvK"
KEYAUTH_VERSION = "1.0"
KEYAUTH_SECRET = "632c30acceedcb3f64ed93a7c73df3fdd3bee5ca1fd9871a64cae44ab103f42e"


@dataclass
class KAResult:
    ok: bool
    message: str = ""


def getchecksum() -> str:
    """
    SHA256 checksum of the currently running script (best-effort).
    """
    try:
        path = sys.argv[0] if sys.argv and sys.argv[0] else __file__
        path = os.path.abspath(path)
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return ""


def _looks_like_module(x: Any) -> bool:
    return isinstance(x, types.ModuleType)


def _resolve_keyauth_constructor() -> Any:
    """
    Locate a callable KeyAuth API constructor across common python wrappers.
    Returns a callable/class that can be instantiated to create a client object.
    """
    candidates: list[Any] = []

    try:
        from keyauth import api as ka_api  # type: ignore
        candidates.append(ka_api)
    except Exception:
        pass

    try:
        ka_api_mod = importlib.import_module("keyauth.api")  # type: ignore
        candidates.append(ka_api_mod)
    except Exception:
        pass

    try:
        ka_mod = importlib.import_module("keyauth")  # type: ignore
        candidates.append(ka_mod)
    except Exception:
        pass

    for cand in candidates:
        if _looks_like_module(cand):
            for attr in ("KeyAuthApp", "Keyauth", "KeyAuth", "Application", "App"):
                ctor = getattr(cand, attr, None)
                if callable(ctor):
                    return ctor

    raise ImportError("Could not locate a valid KeyAuth constructor. Make sure the keyauth library is installed.")


def _create_keyauth_client() -> Any:
    """
    Create KeyAuth client using a few common parameter layouts.
    """
    ctor = _resolve_keyauth_constructor()
    checksum = getchecksum()

    kw_attempts = [
        dict(name=KEYAUTH_NAME, ownerid=KEYAUTH_OWNER_ID, secret=KEYAUTH_SECRET, version=KEYAUTH_VERSION, hash_to_check=checksum),
        dict(name=KEYAUTH_NAME, owner_id=KEYAUTH_OWNER_ID, secret=KEYAUTH_SECRET, version=KEYAUTH_VERSION, hash_to_check=checksum),
        dict(appname=KEYAUTH_NAME, ownerid=KEYAUTH_OWNER_ID, secret=KEYAUTH_SECRET, version=KEYAUTH_VERSION, hash_to_check=checksum),
        dict(name=KEYAUTH_NAME, owner=KEYAUTH_OWNER_ID, secret=KEYAUTH_SECRET, version=KEYAUTH_VERSION, hash_to_check=checksum),
        dict(name=KEYAUTH_NAME, ownerid=KEYAUTH_OWNER_ID, secret=KEYAUTH_SECRET, version=KEYAUTH_VERSION),
        dict(name=KEYAUTH_NAME, owner_id=KEYAUTH_OWNER_ID, secret=KEYAUTH_SECRET, version=KEYAUTH_VERSION),
    ]

    last_err: Optional[Exception] = None

    for kwargs in kw_attempts:
        try:
            return ctor(**kwargs)
        except TypeError as e:
            last_err = e
        except Exception as e:
            last_err = e

    try:
        return ctor(KEYAUTH_NAME, KEYAUTH_OWNER_ID, KEYAUTH_SECRET, KEYAUTH_VERSION, checksum)
    except TypeError:
        pass
    except Exception as e:
        last_err = e

    try:
        return ctor(KEYAUTH_NAME, KEYAUTH_OWNER_ID, KEYAUTH_SECRET, KEYAUTH_VERSION)
    except Exception as e:
        last_err = e

    raise RuntimeError(f"Failed to construct KeyAuth client with any known signature: {last_err}")


def _interpret_response(client: Any, ret: Any) -> Tuple[bool, str]:
    """
    Normalize success + message from various wrappers.
    """
    if isinstance(ret, bool):
        return ret, "" if ret else "Activation failed."

    if isinstance(ret, dict):
        ok = bool(ret.get("success", ret.get("status", False)))
        msg = str(ret.get("message", ret.get("msg", "")) or "")
        return ok, msg

    for container in [ret, getattr(client, "response", None)]:
        if container is None:
            continue
        for ok_attr in ("success", "status", "ok"):
            if hasattr(container, ok_attr):
                ok = bool(getattr(container, ok_attr))
                msg = ""
                for msg_attr in ("message", "msg", "error", "reason"):
                    if hasattr(container, msg_attr):
                        msg = str(getattr(container, msg_attr) or "")
                        break
                return ok, msg

    if isinstance(ret, str):
        low = ret.lower()
        if "success" in low or "valid" in low:
            return True, ret
        if "invalid" in low or "fail" in low or "error" in low:
            return False, ret
        return False, ret

    resp = getattr(client, "response", None)
    if resp is not None:
        try:
            ok = bool(getattr(resp, "success", getattr(resp, "status", False)))
            msg = str(getattr(resp, "message", getattr(resp, "msg", "")) or "")
            return ok, msg
        except Exception:
            pass

    return True, ""  # assume ok if nothing indicates failure


def _safe_init(client: Any) -> KAResult:
    """
    Calls client.init() if present; some wrappers auto-init or forbid double init.
    """
    if not hasattr(client, "init"):
        return KAResult(True, "")

    try:
        ret = client.init()
        if isinstance(ret, bool):
            return KAResult(ret, "" if ret else "init() returned False")
        return KAResult(True, "")
    except Exception as e:
        msg = str(e).lower()
        if "already initialized" in msg or "already initialized" in msg:
            return KAResult(True, "")
        return KAResult(False, str(e))


def _call_license(client: Any, key: str) -> KAResult:
    """
    Attempts to activate a key using common method names and response formats.
    """
    methods = ["license", "licence", "license_key", "key"]
    last_err: Optional[Exception] = None

    for m in methods:
        fn = getattr(client, m, None)
        if callable(fn):
            try:
                r = fn(key)
                ok, msg = _interpret_response(client, r)
                return KAResult(ok, msg)
            except Exception as e:
                last_err = e

    for m in ["login", "activate", "upgrade"]:
        fn = getattr(client, m, None)
        if callable(fn):
            try:
                r = fn(key)
                ok, msg = _interpret_response(client, r)
                return KAResult(ok, msg)
            except Exception as e:
                last_err = e

    if last_err is not None:
        return KAResult(False, f"KeyAuth error: {last_err}")
    return KAResult(False, "No suitable license/activation method found on client.")


def _azul_cli_keyauth_gate():
    """
    Simple console-based KeyAuth activation:
    - Prints an AZUL banner in the console
    - Prompts for license key
    - Validates key using the helper functions above
    - Exits process if activation fails
    """
    try:
        if os.name == "nt":
            import ctypes
            ctypes.windll.kernel32.SetConsoleTitleW("AZUL Activation")
    except Exception:
        pass

    
    
    
    
    
    banner = r"""
====================================================
        Thank you for purchasing AZUL
====================================================

AZUL is a precision aiming assistant with:

  • Clean, modern GUI built with CustomTkinter
  • Tweakable sliders for:
      - Aim speed
      - Smoothing strength
      - Target switch delay
      - Hit chance and more
  • Visual checkboxes to toggle:
      - Show / hide boxes on targets
      - FOV circle overlay
      - Minimal HUD / performance mode
      - Auto-center behavior
  • Real-time FPS readout so you can tune performance
  • Fast detection pipeline optimized for high refresh rates

Please enter your license key below to activate AZUL.
"""





    os.system("cls" if os.name == "nt" else "clear")
    print(banner)

    try:
        client = _create_keyauth_client()
    except Exception as e:
        print("[AZUL] Failed to construct KeyAuth client:")
        print("       ", e)
        os._exit(0)

    init_res = _safe_init(client)
    if not init_res.ok:
        print("[AZUL] KeyAuth init failed:", init_res.message)
        os._exit(0)

    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        try:
            key = input(f"[AZUL] Enter license key (attempt {attempt}/{max_attempts}): ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\n[AZUL] Activation cancelled.")
            os._exit(0)

        if not key:
            print("[AZUL] Empty key; please paste your license key.")
            continue

        res = _call_license(client, key)
        if res.ok:
            print("[AZUL] License activated successfully!")
            try:
                if os.name == "nt":
                    import ctypes
                    hwnd = ctypes.windll.kernel32.GetConsoleWindow()
                    if hwnd:
                        ctypes.windll.user32.ShowWindow(hwnd, 0)  # SW_HIDE
            except Exception:
                pass
            break
        else:
            print(f"[AZUL] Invalid key: {res.message or 'activation failed.'}")
            if attempt < max_attempts:
                print("        Please try again.\n")
            else:
                print("[AZUL] Too many failed attempts. Exiting.")
                os._exit(0)

_azul_cli_keyauth_gate()

from ultralytics import YOLO
from pathlib import Path

MODEL_TRT = Path("weights.engine")
MODEL_ONNX = Path("weights.onnx")
MODEL_PT   = Path("weights.pt")

def _ensure_converted():
    if not MODEL_PT.exists():
        print("[Azul] ERROR: weights.pt not found, cannot export ONNX/engine.")
        return
    try:
        base_model = YOLO(str(MODEL_PT))
    except Exception as e:
        print("[Azul] ERROR loading weights.pt for export:", e)
        return
    if not MODEL_ONNX.exists():
        try:
            print("[Azul] Exporting ONNX from weights.pt ->", MODEL_ONNX)
            base_model.export(format="onnx", imgsz=640)
        except Exception as e:
            print("[Azul] ONNX export failed:", e)
    if not MODEL_TRT.exists():
        try:
            print("[Azul] Exporting TensorRT engine from weights.pt ->", MODEL_TRT)
            base_model.export(format="engine", device=0, half=True)
        except Exception as e:
            print("[Azul] TensorRT export failed:", e)

if not MODEL_TRT.exists() and not MODEL_ONNX.exists() and MODEL_PT.exists():
    _ensure_converted()

if MODEL_TRT.exists():
    print("[Azul] Loading TensorRT engine:", MODEL_TRT)
    model = YOLO(str(MODEL_TRT))
elif MODEL_ONNX.exists():
    print("[Azul] Loading ONNX model:", MODEL_ONNX)
    model = YOLO(str(MODEL_ONNX))
else:
    print("[Azul] Falling back to PyTorch .pt model:", MODEL_PT)
    model = YOLO(str(MODEL_PT))

model.conf = 0.65

import customtkinter as ctk
from tkinter import filedialog
from PIL import Image
import cv2
import threading
import queue
from pygrabber.dshow_graph import FilterGraph

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("dark-blue")

app = ctk.CTk()
app.title("Azul Control Panel")
app.geometry("720x800")

header = ctk.CTkFrame(app, fg_color="#101626")
header.pack(fill="x")

header_left = ctk.CTkFrame(header, fg_color="transparent")
header_left.pack(side="left", padx=10, pady=10)

header_center = ctk.CTkFrame(header, fg_color="transparent")
header_center.pack(side="left", expand=True)

header_right = ctk.CTkFrame(header, fg_color="transparent")
header_right.pack(side="right", padx=10, pady=10)

try:
    import os
    from PIL import Image
    logo_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "AZUL.png")
    if os.path.isfile(logo_path):
        azul_logo_image = ctk.CTkImage(Image.open(logo_path), size=(180, 180))
        logo_label = ctk.CTkLabel(header_left, image=azul_logo_image, text="")
        logo_label.pack()
    else:
        spacer = ctk.CTkFrame(header_left, fg_color="transparent")
        spacer.pack()
except Exception:
    spacer = ctk.CTkFrame(header_left, fg_color="transparent")
    spacer.pack()

beta_label = ctk.CTkLabel(header_center, text="AZUL BETA", font=("Segoe UI", 18, "bold"))
beta_label.pack(pady=10, padx=40)

christmas_colors = ["#ff4d4d", "#ffffff", "#4dff4d"]
def _cycle_beta_color(i=0):
    try:
        beta_label.configure(text_color=christmas_colors[i % len(christmas_colors)])
    except Exception:
        return
    app.after(500, _cycle_beta_color, (i + 1) % len(christmas_colors))

_cycle_beta_color()
tabs = ctk.CTkTabview(app, width=700, height=650)
tabs.pack(pady=20)
target_tab = tabs.add("Targeting")
visuals_tab = tabs.add("Visuals")
config_tab = tabs.add("Config")

target_scroll = ctk.CTkScrollableFrame(target_tab, width=650, height=600)
target_scroll.pack(pady=10)

ctk.CTkLabel(target_scroll, text="Targeting Settings", font=("Segoe UI", 20, "bold")).pack(pady=10)

enable_switch = ctk.CTkSwitch(target_scroll, text="Enable Target Assist")
enable_switch.pack(pady=10)

def slider_with_label(tab, label, from_, to_, default):
    """Create a labeled slider that shows its current value next to the label."""
    frame = ctk.CTkFrame(tab)
    frame.pack(pady=5, fill="x")

    value_label = ctk.CTkLabel(frame, text="")
    value_label.pack(pady=2)

    slider = ctk.CTkSlider(frame, from_=from_, to=to_)
    slider.set(default)
    slider.pack(pady=2, fill="x")

    def _format_value(val: float) -> str:
        text = label
        if "%" in label or "Speed" in label or "Smoothness" in label:
            return f"{text}: {val:.0f}"
        if "Delay" in label or "Radius" in label:
            return f"{text}: {val:.0f}"
        if "Zoom" in label:
            return f"{text}: {val:.2f}"
        return f"{text}: {val:.2f}"

    def _on_change(val: float):
        try:
            value_label.configure(text=_format_value(float(val)))
        except Exception:
            value_label.configure(text=f"{label}: {val}")

    _on_change(default)
    slider.configure(command=_on_change)

    return slider

smooth_slider = slider_with_label(target_scroll, "Tracking Smoothness", 1, 10, 5)
delay_slider = slider_with_label(target_scroll, "Target Switch Delay (ms)", 0, 500, 50)
hit_chance_slider = slider_with_label(target_scroll, "Aim Speed (%)", 0, 100, 80)
det_zoom_slider = slider_with_label(target_scroll, "Detection Zoom", 1.0, 3.0, 1.0)

ctk.CTkLabel(target_scroll, text="Target Priority:").pack(pady=5)
target_priority_var = ctk.StringVar(value="Closest")
ctk.CTkComboBox(
    target_scroll,
    values=["Closest", "Highest Confidence", "Closest + Confidence"],
    variable=target_priority_var,
).pack(pady=5)

ctk.CTkLabel(target_scroll, text="Target Area:").pack(pady=5)
target_bone = ctk.CTkComboBox(target_scroll, values=["Head", "Chest", "Stomach", "Neck", "Pelvis"])
target_bone.set("Head")
target_bone.pack(pady=5)

pred_var = ctk.BooleanVar()
ctk.CTkCheckBox(target_scroll, text="Enable Prediction", variable=pred_var).pack(pady=5)

auto_center_var = ctk.BooleanVar()
ctk.CTkCheckBox(target_scroll, text="Auto Center", variable=auto_center_var).pack(pady=5)

fov_overlay_var = ctk.BooleanVar()
fov_overlay_toggle = ctk.CTkCheckBox(target_scroll, text="Draw FOV Overlay", variable=fov_overlay_var)
fov_overlay_toggle.pack(pady=5)
fov_circle_slider = slider_with_label(target_scroll, "FOV Circle Radius", 10, 500, 150)

ctk.CTkLabel(target_scroll, text="Capture Device:").pack(pady=10)

graph = FilterGraph()
capture_devices = graph.get_input_devices()
capture_selector = ctk.CTkComboBox(target_scroll, values=capture_devices, width=300)
if capture_devices:
    capture_selector.set(capture_devices[0])
else:
    capture_selector.set("No Devices Found")
capture_selector.pack(pady=5)

def launch_capture():
    selected_name = capture_selector.get()
    try:
        index = capture_devices.index(selected_name)
    except ValueError:
        print("Device not found")
        return

    detection_queue = queue.Queue(maxsize=1)
    detections_lock = threading.Lock()
    latest_detections = []

    def detection_loop():
        nonlocal latest_detections
        while True:
            item = detection_queue.get()
            if item is None:
                break
            det_frame, dz, x_off, y_off = item
            try:
                results = model.predict(det_frame, verbose=False, device=0)
                dets = results[0].boxes.data.cpu().numpy()

                try:
                    if dz > 1.01 and dets is not None and len(dets) > 0:
                        import numpy as _np
                        det_arr_map = _np.asarray(dets)
                        if det_arr_map.ndim == 2 and det_arr_map.shape[1] >= 4:
                            det_arr_map[:, 0] = x_off + det_arr_map[:, 0] / dz
                            det_arr_map[:, 2] = x_off + det_arr_map[:, 2] / dz
                            det_arr_map[:, 1] = y_off + det_arr_map[:, 1] / dz
                            det_arr_map[:, 3] = y_off + det_arr_map[:, 3] / dz
                            dets = det_arr_map
                except Exception:
                    pass

                try:
                    if dets is not None and len(dets) > 0:
                        import numpy as _np
                        det_arr = _np.asarray(dets)
                        if det_arr.ndim == 2 and det_arr.shape[1] >= 6:
                            det_arr = det_arr[det_arr[:, 5] == 0]
                            dets = det_arr
                except Exception:
                    pass

                with detections_lock:
                    latest_detections = dets
            except Exception:
                pass

    threading.Thread(target=detection_loop, daemon=True).start()

    def capture_loop():
        cap = cv2.VideoCapture(index, cv2.CAP_DSHOW)
        if not cap.isOpened():
            print("Failed to open capture device")
            return
        prev_time = time.time()
        fps = 0.0
        while True:
            ret, frame = cap.read()
            if not ret:
                break

            now = time.time()
            dt = now - prev_time
            if dt > 0:
                fps = 1.0 / dt
            prev_time = now

            try:
                fps_label_var.set(f"FPS: {fps:.1f}")
            except Exception:
                pass

            det_frame = frame
            dz = 1.0
            x_off = 0.0
            y_off = 0.0
            try:
                dz = float(det_zoom_slider.get())
            except Exception:
                dz = 1.0
            if dz < 1.0:
                dz = 1.0
            if dz > 1.01:
                h, w = frame.shape[:2]
                crop_w = int(w / dz)
                crop_h = int(h / dz)
                cx = w // 2
                cy = h // 2
                x1 = max(0, cx - crop_w // 2)
                y1 = max(0, cy - crop_h // 2)
                x2 = min(w, x1 + crop_w)
                y2 = min(h, y1 + crop_h)
                det_frame = frame[y1:y2, x1:x2].copy()
                det_frame = cv2.resize(det_frame, (w, h))
                x_off = float(x1)
                y_off = float(y1)

            try:
                if enable_switch.get():
                    if detection_queue.full():
                        _ = detection_queue.get_nowait()
                    detection_queue.put_nowait((det_frame.copy(), dz, x_off, y_off))
            except Exception:
                pass

            with detections_lock:
                dets = latest_detections
            if dets is None:
                detections = []
            else:
                detections = dets

            try:
                if detections is not None and len(detections) > 0:
                    height, width = frame.shape[:2]
                    cx_screen, cy_screen = width // 2, height // 2
                    radius = float(fov_circle_slider.get())
                    r2 = radius * radius
                    dets_in_fov = []
                    for det in detections:
                        x1, y1, x2, y2, conf, cls = det
                        tx = (x1 + x2) / 2.0
                        ty = (y1 + y2) / 2.0
                        dx = tx - cx_screen
                        dy = ty - cy_screen
                        if dx * dx + dy * dy <= r2:
                            dets_in_fov.append(det)
                    detections = dets_in_fov
            except Exception:
                pass

            if enable_switch.get() and len(detections) > 0:
                height, width = frame.shape[:2]
                cx_screen, cy_screen = width // 2, height // 2

                dets_for_aim = detections
                try:
                    radius = float(fov_circle_slider.get())
                except Exception:
                    radius = min(width, height) / 3.0
                r2 = radius * radius
                filtered = []
                for det in detections:
                    x1, y1, x2, y2, conf, cls = det
                    tx = (x1 + x2) / 2.0
                    ty = (y1 + y2) / 2.0
                    dx = tx - cx_screen
                    dy = ty - cy_screen
                    if dx * dx + dy * dy <= r2:
                        filtered.append(det)
                if filtered:
                    dets_for_aim = filtered
                else:
                    pid.set_error(0, 0)
                    dets_for_aim = []

                if len(dets_for_aim) > 0:
                    def _center_xy(det):
                        x1, y1, x2, y2, conf, cls = det
                        return ( (x1 + x2) / 2.0, (y1 + y2) / 2.0 )

                    try:
                        mode = target_priority_var.get()
                    except Exception:
                        mode = "Closest"

                    def _dist2_to_center(det):
                        cx_d, cy_d = _center_xy(det)
                        dx = cx_d - cx_screen
                        dy = cy_d - cy_screen
                        return dx * dx + dy * dy

                    if mode == "Highest Confidence":
                        candidate = max(dets_for_aim, key=lambda det: det[4])
                    elif mode == "Closest + Confidence":
                        try:
                            radius = float(fov_circle_slider.get())
                        except Exception:
                            radius = min(width, height) / 3.0
                        max_d2 = radius * radius if radius > 0 else max(width, height) ** 2 or 1.0

                        def _score(det):
                            d2 = _dist2_to_center(det)
                            conf = det[4]
                            norm_d = d2 / max_d2
                            return conf - 0.5 * norm_d  # tune 0.5 if needed

                        candidate = max(dets_for_aim, key=_score)
                    else:
                        candidate = min(
                            dets_for_aim,
                            key=lambda det: _dist2_to_center(det),
                        )

                    cand_cx, cand_cy = _center_xy(candidate)

                    import time as _time_azul
                    try:
                        delay_ms = float(delay_slider.get())
                    except Exception:
                        delay_ms = 0.0
                    if delay_ms < 0.0:
                        delay_ms = 0.0

                    now_t = _time_azul.time()
                    try:
                        prev_cx, prev_cy = pid._target_center
                        prev_since = pid._target_since
                    except Exception:
                        prev_cx, prev_cy = None, None
                        prev_since = now_t

                    try:
                        fov_px_for_switch = float(fov_circle_slider.get())
                    except Exception:
                        fov_px_for_switch = min(width, height) / 3.0
                    if fov_px_for_switch < 1.0:
                        fov_px_for_switch = min(width, height) / 3.0
                    same_thresh_px = max(40.0, 0.55 * fov_px_for_switch)
                    same_thresh2 = same_thresh_px * same_thresh_px

                    use_candidate = True
                    chosen = candidate

                    if prev_cx is not None and prev_cy is not None:
                        try:
                            prev_box = pid._target_box  # (x1, y1, x2, y2) from prior frame
                        except Exception:
                            prev_box = None

                        def _iou_box(det, box_prev):
                            x1, y1, x2, y2, *_ = det
                            px1, py1, px2, py2 = box_prev
                            ix1 = max(x1, px1)
                            iy1 = max(y1, py1)
                            ix2 = min(x2, px2)
                            iy2 = min(y2, py2)
                            iw = max(0.0, ix2 - ix1)
                            ih = max(0.0, iy2 - iy1)
                            inter = iw * ih
                            if inter <= 0.0:
                                return 0.0
                            area_det = max(0.0, (x2 - x1)) * max(0.0, (y2 - y1))
                            area_prev = max(0.0, (px2 - px1)) * max(0.0, (py2 - py1))
                            union = area_det + area_prev - inter
                            if union <= 0.0:
                                return 0.0
                            return inter / union

                        best_for_prev = None
                        best_score = None
                        for det in dets_for_aim:
                            cx_d, cy_d = _center_xy(det)
                            d2 = (cx_d - prev_cx) ** 2 + (cy_d - prev_cy) ** 2
                            iou = 0.0
                            if prev_box is not None:
                                iou = _iou_box(det, prev_box)
                            score = iou - 1e-4 * d2
                            if best_for_prev is None or score > best_score:
                                best_for_prev = (det, cx_d, cy_d, iou, d2)
                                best_score = score

                        if best_for_prev is not None:
                            det_prev, prev_frame_cx, prev_frame_cy, best_iou, best_prev_d2 = best_for_prev
                            same_box = False
                            if best_iou >= 0.25:
                                same_box = True
                            elif best_prev_d2 <= same_thresh2:
                                same_box = True

                            if same_box:
                                if delay_ms > 0.0 and (now_t - prev_since) < (delay_ms / 1000.0):
                                    use_candidate = False
                                    chosen = det_prev
                                else:
                                    use_candidate = True
                                    chosen = candidate
                                    prev_since = now_t
                            else:
                                use_candidate = True
                                chosen = candidate
                                prev_since = now_t
                        else:
                            use_candidate = True
                            chosen = candidate
                            prev_since = now_t
                    else:
                        use_candidate = True
                        chosen = candidate
                        prev_since = now_t

                    ch_x, ch_y = _center_xy(chosen)
                    pid._target_center = (ch_x, ch_y)
                    pid._target_since = prev_since
                    pid._target_box = (x1, y1, x2, y2)
                    pid._reticle_on_target = True

                    ch_x, ch_y = _center_xy(chosen)
                    pid._target_center = (ch_x, ch_y)
                    pid._target_since = prev_since

                    x1, y1, x2, y2, conf, cls = chosen
                    box_h = (y2 - y1)
                    try:
                        bone = target_bone.get()
                    except Exception:
                        bone = 'Head'
                    if bone == 'Head':
                        frac_y = 0.20  # near top of box
                    elif bone == 'Neck':
                        frac_y = 0.28
                    elif bone == 'Chest':
                        frac_y = 0.40
                    elif bone == 'Stomach':
                        frac_y = 0.52
                    elif bone == 'Pelvis':
                        frac_y = 0.65
                    else:
                        frac_y = 0.50
                    tx = (x1 + x2) / 2.0
                    ty = y1 + box_h * frac_y

                    try:
                        use_pred = bool(pred_var.get())
                    except Exception:
                        use_pred = False

                    tx_eff, ty_eff = tx, ty
                    now_t = time.time()
                    try:
                        prev_tx, prev_ty, prev_t = pid._pred_state
                    except Exception:
                        prev_tx, prev_ty, prev_t = tx, ty, now_t

                    dt = now_t - prev_t
                    if dt <= 0.0 or dt > 0.5:
                        dt = 0.0

                    if use_pred and dt > 0.0:
                        vx = (tx - prev_tx) / dt
                        vy = (ty - prev_ty) / dt

                        try:
                            sm_s = float(smooth_slider.get())
                        except Exception:
                            sm_s = 5.0
                        sm_s = 1.0 if sm_s < 1.0 else (10.0 if sm_s > 10.0 else sm_s)
                        alpha_pos = 0.15 + (sm_s - 1.0) / 9.0 * (0.50 - 0.15)

                        sm_x = (1.0 - alpha_pos) * tx + alpha_pos * prev_tx
                        sm_y = (1.0 - alpha_pos) * ty + alpha_pos * prev_ty

                        lead_time = 0.08
                        tx_eff = sm_x + vx * lead_time
                        ty_eff = sm_y + vy * lead_time

                        if width > 0 and height > 0:
                            if tx_eff < 0.0:
                                tx_eff = 0.0
                            if ty_eff < 0.0:
                                ty_eff = 0.0
                            if tx_eff > float(width - 1):
                                tx_eff = float(width - 1)
                            if ty_eff > float(height - 1):
                                ty_eff = float(height - 1)

                    pid._pred_state = (tx, ty, now_t)

                    ex_px = tx_eff - cx_screen
                    ey_px = ty_eff - cy_screen

                    try:
                        fov_px = float(fov_circle_slider.get())
                    except Exception:
                        fov_px = min(width, height) / 3.0
                    if fov_px < 1.0:
                        fov_px = min(width, height) / 3.0

                    deadzone_px = 4.0
                    if abs(ex_px) < deadzone_px:
                        ex_px = 0.0
                    if abs(ey_px) < deadzone_px:
                        ey_px = 0.0

                    ex_n = ex_px / fov_px
                    ey_n = ey_px / fov_px

                    baseline_yank = 0.14
                    gain = 1.20
                    alpha = 0.6

                    def _yank(e: float) -> float:
                        if e == 0.0:
                            return 0.0
                        s = -1.0 if e < 0.0 else 1.0
                        mag = baseline_yank + gain * (abs(e) ** alpha)
                        out = s * mag
                        if out < -1.0:
                            out = -1.0
                        if out > 1.0:
                            out = 1.0
                        return out

                    err_x = _yank(ex_n)
                    err_y = _yank(-ey_n)  # invert so positive = up

                    try:
                        hc = float(hit_chance_slider.get())
                    except Exception:
                        hc = 100.0
                    strength = max(0.0, min(100.0, hc)) / 100.0
                    err_x *= strength
                    err_y *= strength

                    force_no_smooth = False
                    try:
                        auto_center_on = bool(auto_center_var.get())
                    except Exception:
                        auto_center_on = False
                    if auto_center_on:
                        dist2_px = ex_px * ex_px + ey_px * ey_px
                        dead_center_px = 10.0  # pixels radius around center to stop forcing
                        if dist2_px > (dead_center_px * dead_center_px):
                            force_no_smooth = True

                    try:
                        sm = float(smooth_slider.get())
                    except Exception:
                        sm = 5.0
                    sm = 1.0 if sm < 1.0 else (10.0 if sm > 10.0 else sm)

                    base = (sm - 1.0) / 9.0  # 0..1
                    smooth_strength = (base * base) * 0.7

                    err_mag = max(abs(err_x), abs(err_y))
                    adapt = min(1.0, err_mag / 0.35)
                    smooth_strength = smooth_strength * (1.0 - 0.85 * adapt)

                    try:
                        prev_x, prev_y = pid._smooth_err
                    except Exception:
                        prev_x, prev_y = 0.0, 0.0

                    center_band = 0.22  # ~small stick deflection
                    if abs(err_x) < center_band and abs(prev_x) < center_band and (err_x * prev_x) < 0.0:
                        prev_x = 0.0
                    if abs(err_y) < center_band and abs(prev_y) < center_band and (err_y * prev_y) < 0.0:
                        prev_y = 0.0

                    if force_no_smooth:
                        smooth_strength = 0.0

                    if smooth_strength < 0.0:
                        smooth_strength = 0.0
                    if smooth_strength > 0.75:
                        smooth_strength = 0.75

                    smooth_x = (1.0 - smooth_strength) * err_x + smooth_strength * prev_x
                    smooth_y = (1.0 - smooth_strength) * err_y + smooth_strength * prev_y

                    pid._smooth_err = (smooth_x, smooth_y)
                    pid.set_error(smooth_x, smooth_y)
            else:
                pid.set_error(0, 0)
                pid._reticle_on_target = False

            
            if show_boxes_var.get():
                for x1, y1, x2, y2, conf, cls in detections:
                    center_x = int((x1 + x2) / 2)
                    center_y = int((y1 + y2) / 2)
                    cv2.circle(frame, (center_x, center_y), 5, (255, 0, 0), -1)  # filled blue circle

            if not ret:
                break

            if fov_overlay_var.get() and not minimal_hud_var.get():
                height, width = frame.shape[:2]
                center = (width // 2, height // 2)
                radius = int(fov_circle_slider.get())
                cv2.circle(frame, center, radius, (0, 0, 0), 2, lineType=cv2.LINE_AA)
                cv2.circle(frame, center, radius - 1, (255, 255, 0), 1, lineType=cv2.LINE_AA)
                cx, cy = center
                cross_len = 6  # fixed small size
                try:
                    on_target = bool(getattr(pid, "_reticle_on_target", False))
                except Exception:
                    on_target = False
                color = (0, 0, 255) if on_target else (255, 255, 0)
                cv2.line(frame, (cx - cross_len, cy - cross_len), (cx + cross_len, cy + cross_len), color, 1, lineType=cv2.LINE_AA)
                cv2.line(frame, (cx - cross_len, cy + cross_len), (cx + cross_len, cy - cross_len), color, 1, lineType=cv2.LINE_AA)

            if not minimal_hud_var.get():
                try:
                    h, w = frame.shape[:2]
                    header_h = min(40, h)
                    header_color = (38, 22, 16)
                    frame[0:header_h, 0:w] = header_color
                    global AZUL_ICON_BGR
                    if AZUL_ICON_BGR is None and os.path.isfile(AZUL_ICON_PATH):
                        import cv2 as _cv2_azul
                        icon = _cv2_azul.imread(AZUL_ICON_PATH, _cv2_azul.IMREAD_UNCHANGED)
                        if icon is not None:
                            AZUL_ICON_BGR = icon
                    if AZUL_ICON_BGR is not None:
                        import cv2 as _cv2_azul2
                        icon = AZUL_ICON_BGR
                        ih, iw = icon.shape[:2]
                        target_h = header_h - 8 if header_h > 8 else header_h
                        if target_h > 0 and ih > 0:
                            scale = target_h / float(ih)
                            new_w = max(1, int(iw * scale))
                            resized = _cv2_azul2.resize(icon, (new_w, target_h), interpolation=_cv2_azul2.INTER_AREA)
                            y0 = (header_h - target_h) // 2
                            x0 = 8
                            y1 = y0 + target_h
                            x1 = min(w, x0 + new_w)
                            roi = frame[y0:y1, x0:x1]
                            if resized.shape[2] == 4:
                                rgb = resized[:, :, :3].astype(roi.dtype)
                                alpha = resized[:, :, 3:4].astype('float32') / 255.0
                                inv_alpha = 1.0 - alpha
                                roi[:] = (alpha * rgb + inv_alpha * roi).astype(roi.dtype)
                            else:
                                roi[:] = resized[:, : roi.shape[1], : roi.shape[2]]
                    try:
                        fps_text = f"FPS: {fps:.1f}"
                        cv2.putText(
                            frame,
                            fps_text,
                            (max(10, w - 180), int(header_h * 0.75)),
                            cv2.FONT_HERSHEY_SIMPLEX,
                            0.6,
                            (255, 255, 255),
                            1,
                            cv2.LINE_AA,
                        )
                    except Exception:
                        pass
                except Exception:
                    pass
            if hide_preview_var.get():
                try:
                    cv2.destroyWindow("AZUL Capture Feed")
                except Exception:
                    pass
            else:
                cv2.imshow("AZUL Capture Feed", frame)
            if cv2.waitKey(1) & 0xFF == ord('q'):
                break
        try:
            detection_queue.put_nowait(None)
        except Exception:
            pass

        cap.release()
        cv2.destroyAllWindows()

    threading.Thread(target=capture_loop, daemon=True).start()

ctk.CTkButton(target_scroll, text="Launch Feed", command=launch_capture).pack(pady=10)

ctk.CTkLabel(visuals_tab, text="Visual Options", font=("Segoe UI", 20, "bold")).pack(pady=10)

show_boxes_var = ctk.BooleanVar()

ctk.CTkCheckBox(visuals_tab, text="Show Boxes", variable=show_boxes_var).pack(pady=5)

fast_mode_var = ctk.BooleanVar(value=False)
minimal_hud_var = ctk.BooleanVar(value=False)
hide_preview_var = ctk.BooleanVar(value=False)

def _on_fast_mode_toggle():
    val = fast_mode_var.get()
    minimal_hud_var.set(val)
    hide_preview_var.set(val)

ctk.CTkCheckBox(
    visuals_tab,
    text="Performance Mode (Minimal HUD + Hide Preview)",
    variable=fast_mode_var,
    command=_on_fast_mode_toggle,
).pack(pady=5)

fps_label_var = ctk.StringVar(value="FPS: 0.0")
ctk.CTkLabel(visuals_tab, textvariable=fps_label_var, font=("Segoe UI", 14)).pack(pady=5)

ctk.CTkLabel(config_tab, text="Configuration", font=("Segoe UI", 20, "bold")).pack(pady=10)

SETTINGS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "azul_settings.json")
APP_UPDATE_URL = "https://example.com/your_scriptforge_update.py"  # TODO: set this to your real update URL

def update_application():
    """Download and apply the latest version of this app from APP_UPDATE_URL."""
    global APP_UPDATE_URL
    try:
        if not APP_UPDATE_URL or APP_UPDATE_URL.startswith('https://example.com'):
            print('[Azul] APP_UPDATE_URL is not configured. Edit the script to set a real update URL.')
            return
    except NameError:
        print('[Azul] APP_UPDATE_URL is not defined.')
        return
    try:
        print('[Azul] Checking for updates from', APP_UPDATE_URL)
        with urllib.request.urlopen(APP_UPDATE_URL, timeout=10) as resp:
            new_code = resp.read()
    except Exception as e:
        print('[Azul] Failed to download update:', e)
        return
    try:
        current_path = os.path.abspath(__file__)
        backup_path = current_path + '.bak'
        shutil.copy2(current_path, backup_path)
        with open(current_path, 'wb') as f:
            f.write(new_code)
        print('[Azul] Update downloaded successfully.')
        print('[Azul] Backup saved as', backup_path)
        print('[Azul] Please restart the application to finish updating.')
    except Exception as e:
        print('[Azul] Failed to apply update:', e)



def save_settings():
    """Save current sliders / toggles / combos to a JSON file chosen by the user."""
    data = {}
    try:
        data["enable_target_assist"] = bool(enable_switch.get())
    except Exception:
        pass
    try:
        data["tracking_smoothness"] = float(smooth_slider.get())
        data["target_switch_delay_ms"] = float(delay_slider.get())
        data["aim_speed"] = float(hit_chance_slider.get())
        data["detection_zoom"] = float(det_zoom_slider.get())
        data["fov_radius"] = float(fov_circle_slider.get())
    except Exception:
        pass
    try:
        data["prediction_enabled"] = bool(pred_var.get())
    except Exception:
        pass
    try:
        data["auto_center"] = bool(auto_center_var.get())
    except Exception:
        pass
    try:
        data["fov_overlay"] = bool(fov_overlay_var.get())
    except Exception:
        pass
    try:
        data["target_bone"] = str(target_bone.get())
    except Exception:
        pass
    try:
        data["capture_device"] = str(capture_selector.get())
    except Exception:
        pass
    try:
        data["show_boxes"] = bool(show_boxes_var.get())
    except Exception:
        pass
    try:
        file_path = filedialog.asksaveasfilename(
            title="Save Azul Settings",
            defaultextension=".json",
            filetypes=[("JSON Files", "*.json"), ("All Files", "*.*")],
            initialfile="azul_settings.json",
        )
        if not file_path:
            print("[Azul] Save cancelled.")
            return
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        print(f"[Azul] Settings saved to {file_path}")
    except Exception as e:
        print("[Azul] Failed to save settings:", e)


def load_settings(file_path=None):
    """Load settings from a JSON file path. If no path is given, let the user pick one."""
    global SETTINGS_PATH
    try:
        if file_path is None:
            file_path = filedialog.askopenfilename(
                title="Load Azul Settings",
                filetypes=[("JSON Files", "*.json"), ("All Files", "*.*")],
            )
    except Exception as e:
        print("[Azul] Failed to open file dialog:", e)
        return
    if not file_path:
        print("[Azul] Load cancelled.")
        return
    SETTINGS_PATH = file_path
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        print("[Azul] Failed to load settings:", e)
        return
    try:
        if data.get("enable_target_assist", False):
            enable_switch.select()
        else:
            enable_switch.deselect()
    except Exception:
        pass
    try:
        smooth_slider.set(float(data.get("tracking_smoothness", smooth_slider.get())))
        delay_slider.set(float(data.get("target_switch_delay_ms", delay_slider.get())))
        hit_chance_slider.set(float(data.get("aim_speed", hit_chance_slider.get())))
        det_zoom_slider.set(float(data.get("detection_zoom", det_zoom_slider.get())))
        fov_circle_slider.set(float(data.get("fov_radius", fov_circle_slider.get())))
    except Exception:
        pass
    try:
        if data.get("prediction_enabled", False):
            pred_var.set(True)
        else:
            pred_var.set(False)
    except Exception:
        pass
    try:
        if data.get("auto_center", False):
            auto_center_var.set(True)
        else:
            auto_center_var.set(False)
    except Exception:
        pass
    try:
        if data.get("fov_overlay", False):
            fov_overlay_var.set(True)
        else:
            fov_overlay_var.set(False)
    except Exception:
        pass
    try:
        bone = data.get("target_bone")
        if bone:
            target_bone.set(bone)
    except Exception:
        pass
    try:
        dev = data.get("capture_device")
        if dev:
            capture_selector.set(dev)
    except Exception:
        pass
    try:
        if data.get("show_boxes", False):
            show_boxes_var.set(True)
        else:
            show_boxes_var.set(False)
    except Exception:
        pass
        pass
    print("[" + "Azul" + "] Settings loaded from", file_path)

def update_settings():
    """Reload settings from the last used JSON file path, if available."""
    global SETTINGS_PATH
    try:
        if not SETTINGS_PATH or not os.path.isfile(SETTINGS_PATH):
            print("[Azul] No saved settings path found. Use Save or Load once first.")
            return
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        print("[Azul] Failed to reload settings:", e)
        return
    try:
        if data.get("enable_target_assist", False):
            enable_switch.select()
        else:
            enable_switch.deselect()
    except Exception:
        pass
    try:
        smooth_slider.set(float(data.get("tracking_smoothness", smooth_slider.get())))
        delay_slider.set(float(data.get("target_switch_delay_ms", delay_slider.get())))
        hit_chance_slider.set(float(data.get("aim_speed", hit_chance_slider.get())))
        det_zoom_slider.set(float(data.get("detection_zoom", det_zoom_slider.get())))
        fov_circle_slider.set(float(data.get("fov_radius", fov_circle_slider.get())))
    except Exception:
        pass
    try:
        if data.get("prediction_enabled", False):
            pred_var.set(True)
        else:
            pred_var.set(False)
    except Exception:
        pass
    try:
        if data.get("auto_center", False):
            auto_center_var.set(True)
        else:
            auto_center_var.set(False)
    except Exception:
        pass
    try:
        if data.get("fov_overlay", False):
            fov_overlay_var.set(True)
        else:
            fov_overlay_var.set(False)
    except Exception:
        pass
    try:
        bone = data.get("target_bone")
        if bone:
            target_bone.set(bone)
    except Exception:
        pass
    try:
        dev = data.get("capture_device")
        if dev:
            capture_selector.set(dev)
    except Exception:
        pass
    try:
        if data.get("show_boxes", False):
            show_boxes_var.set(True)
        else:
            show_boxes_var.set(False)
    except Exception:
        pass
    print("[Azul] Settings updated from", SETTINGS_PATH)

def reset_settings():
    """Reset UI controls back to their default values."""
    try:
        enable_switch.deselect()
    except Exception:
        pass
    try:
        smooth_slider.set(5)
        delay_slider.set(50)
        hit_chance_slider.set(80)
        det_zoom_slider.set(1.0)
        fov_circle_slider.set(150)
    except Exception:
        pass
    try:
        pred_var.set(False)
    except Exception:
        pass
    try:
        auto_center_var.set(False)
    except Exception:
        pass
    try:
        fov_overlay_var.set(False)
    except Exception:
        pass
    try:
        target_bone.set("Head")
    except Exception:
        pass
    try:
        if capture_devices:
            capture_selector.set(capture_devices[0])
    except Exception:
        pass
    try:
        show_boxes_var.set(False)
    except Exception:
        pass
    print("[Azul] Settings reset to defaults.")

def save_settings_dialog():
    """Open a Save dialog, choose a JSON path, then call save_settings()."""
    global SETTINGS_PATH
    try:
        file_path = filedialog.asksaveasfilename(
            title="Save Azul Settings",
            defaultextension=".json",
            filetypes=[("JSON Files", "*.json"), ("All Files", "*.*")],
            initialfile="azul_settings.json",
        )
    except Exception as e:
        print("[Azul] Failed to open save dialog:", e)
        return
    if not file_path:
        print("[Azul] Save cancelled.")
        return
    SETTINGS_PATH = file_path
    save_settings()

def load_settings_dialog():
    """Show a black AZUL popup with a Browse button to load settings."""
    global SETTINGS_PATH, app

    popup = ctk.CTkToplevel(app)
    popup.title("Load AZUL Settings")
    popup.geometry("520x320")
    try:
        popup.configure(fg_color="black")
    except Exception:
        pass

    try:
        popup.lift()
        popup.focus_force()
        popup.grab_set()
    except Exception:
        pass

    message = (
        "Load AZUL settings\n\n"
        "Use 'Browse...' to select a JSON settings file.\n\n"
        "You can keep different configs for different games or profiles,\n"
        "and load them here whenever you need."
    )

    label = ctk.CTkLabel(
        popup,
        text=message,
        text_color="white",
        fg_color="transparent",
        justify="left",
        font=("Segoe UI", 14),
    )
    label.pack(padx=20, pady=(20, 10), anchor="w")

    btn_frame = ctk.CTkFrame(popup, fg_color="black")
    btn_frame.pack(pady=20)

    def on_browse():
        """Let the user pick a file, then load it and close the popup."""
        global SETTINGS_PATH
        try:
            file_path = filedialog.askopenfilename(
                title="Load Azul Settings",
                filetypes=[("JSON Files", "*.json"), ("All Files", "*.*")],
            )
        except Exception as e:
            print("[Azul] Failed to open load dialog:", e)
            return
        if not file_path:
            print("[Azul] Load cancelled.")
            return
        SETTINGS_PATH = file_path
        load_settings(file_path)
        try:
            popup.destroy()
        except Exception:
            pass

    def on_cancel():
        try:
            popup.destroy()
        except Exception:
            pass

    browse_btn = ctk.CTkButton(btn_frame, text="Browse...", width=140, command=on_browse)
    browse_btn.pack(side="left", padx=10)

    cancel_btn = ctk.CTkButton(btn_frame, text="Cancel", width=140, fg_color="#333333", command=on_cancel)
    cancel_btn.pack(side="left", padx=10)

save_btn = ctk.CTkButton(config_tab, text="Save Settings", width=200, command=save_settings_dialog)
save_btn.pack(pady=10)

load_btn = ctk.CTkButton(config_tab, text="Load Settings", width=200, command=load_settings_dialog)
load_btn.pack(pady=10)
update_btn = ctk.CTkButton(config_tab, text="Update Application", width=200, command=update_application)
update_btn.pack(pady=10)

reset_btn = ctk.CTkButton(config_tab, text="Reset to Default", width=200, command=reset_settings)
reset_btn.pack(pady=10)


injector = BlendedControllerInjector()
pid = attach_right_stick_pid(injector, kp=1.0, ki=0.0, kd=0.25, update_hz=1000, max_out=1.0)
injector.bridge.set_injection_scales(right=1.0)

app.mainloop()
