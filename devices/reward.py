"""
devices/reward.py

Solenoid reward valve. GPIO output via pigpio.
Supports multiple pins (e.g., main reward, secondary).
Calibration maps pulse duration (ms) to volume (uL).
"""

from __future__ import annotations
import threading
import time

from .base import Device, DeviceInfo, IOType, register_device


@register_device
class Reward(Device):
    info = DeviceInfo("reward", "Reward Valve", IOType.GPIO_OUT, ["pigpio"])

    @classmethod
    def task_params_schema(cls):
        return {
            "amount_ul": {
                "type": "float", "default": 4.0,
                "label": "Reward amount (uL)", "min": 0.5, "max": 20.0,
            },
        }

    # Valve off-time between pulses in count mode, so it fully cycles each time
    PULSE_GAP_MS = 150.0

    def init(self, rig_config: dict, task_params: dict):
        import pigpio
        self.amount_ul = task_params.get("amount_ul", 4.0)
        # "volume": one pulse, duration interpolated from the calibration for amount_ul.
        # "count": amount_count repeats of the BASE pulse (first calibration row, as
        # set up in the setup UI's reward card).
        self.amount_mode = str(task_params.get("amount_mode", "volume"))
        self.amount_count = max(1, int(task_params.get("amount_count", 1)))
        self.pulse_gap_ms = float(task_params.get("pulse_gap_ms", self.PULSE_GAP_MS))
        self.reward_type = rig_config.get("reward_type", "water")
        # Serialize valve access: only one pulse/train may drive the GPIO at a time, so a
        # concurrent trigger (e.g. a manual REWARD during an auto-reward train) is dropped
        # rather than racing the output and garbling the waveform. _stopping lets close()
        # end an in-flight train with the valve shut instead of leaving it energized.
        self._valve_lock = threading.Lock()
        self._stopping = False
        self._pi = pigpio.pi()
        if not self._pi.connected:
            raise RuntimeError("pigpiod not running — sudo pigpiod")

        # Init pins
        self._pins = {}  # name -> gpio
        pins_raw = rig_config.get("pins", {})
        for name, pin_cfg in pins_raw.items():
            gpio = pin_cfg["gpio"]
            self._pi.set_mode(gpio, pigpio.OUTPUT)
            self._pi.write(gpio, 0)
            self._pins[name] = gpio

        # Calibration (loaded separately via load_calibration)
        self._interp = {}  # name -> interp1d function
        self._base = {}    # name -> (ms, ul) of the FIRST calibration row = base pulse

    @property
    def needs_calibration(self) -> bool:
        return True

    def load_calibration(self, cal_data: dict):
        """cal_data: {"main": [[10,2.1],[20,4.3],...]}
        Single point: proportional scaling (e.g., [100,4] → 25 ms/uL).
        Two+ points: scipy linear interpolation with extrapolation.
        """
        import numpy as np
        for name, points in cal_data.items():
            arr = np.array(points)
            self._base[name] = (float(arr[0, 0]), float(arr[0, 1]))   # base pulse (ms, ul)
            if arr.shape[0] == 1:
                # Single point: proportional scaling
                ms_per_ul = arr[0, 0] / arr[0, 1]
                self._interp[name] = lambda vol, r=ms_per_ul: vol * r
            else:
                from scipy.interpolate import interp1d
                self._interp[name] = interp1d(
                    arr[:, 1], arr[:, 0],
                    kind="linear", fill_value="extrapolate",
                )

    def pulse_ms_for_volume(self, volume_ul: float, pin_name: str = "main") -> float:
        if pin_name not in self._interp:
            raise ValueError(f"No calibration for pin '{pin_name}'")
        return float(self._interp[pin_name](volume_ul))

    def pulse(self, duration_ms: float, pin_name: str = "main"):
        gpio = self._pins[pin_name]
        t = threading.Thread(target=self._pulse_thread,
                             args=(gpio, duration_ms), daemon=True)
        t.start()

    def pulse_train(self, duration_ms: float, n: int, pin_name: str = "main"):
        """Fire n identical pulses with PULSE_GAP_MS off-time between them."""
        gpio = self._pins[pin_name]
        t = threading.Thread(target=self._pulse_train_thread,
                             args=(gpio, duration_ms, max(1, int(n))), daemon=True)
        t.start()

    def deliver(self, pin_name: str = "main"):
        """Deliver a reward per the configured amount mode.
        volume: one pulse, duration interpolated from the calibration for amount_ul.
        count:  amount_count repeats of the base pulse (first calibration row)."""
        if self.amount_mode == "count":
            if pin_name not in self._base:
                raise ValueError(f"No calibration for pin '{pin_name}'")
            ms = self._base[pin_name][0]
            self.pulse_train(ms, self.amount_count, pin_name)
            return ms
        ms = self.pulse_ms_for_volume(self.amount_ul, pin_name)
        self.pulse(ms, pin_name)
        return ms

    @property
    def effective_amount_ul(self) -> float:
        """µL commanded per reward: amount_ul in volume mode; amount_count × the base
        pulse's calibrated volume in count mode (for HDF5 logging)."""
        if self.amount_mode == "count":
            base = self._base.get("main")
            return float(self.amount_count * base[1]) if base else float("nan")
        return float(self.amount_ul)

    def _pulse_thread(self, gpio: int, duration_ms: float):
        # Drop if a delivery is already in flight (don't race the GPIO).
        if not self._valve_lock.acquire(blocking=False):
            return
        try:
            if self._stopping:
                return
            self._pi.write(gpio, 1)
            time.sleep(duration_ms / 1000.0)
        finally:
            self._pi.write(gpio, 0)   # always leave the valve closed
            self._valve_lock.release()

    def _pulse_train_thread(self, gpio: int, duration_ms: float, n: int):
        # Drop if a delivery is already in flight (don't race the GPIO).
        if not self._valve_lock.acquire(blocking=False):
            return
        try:
            for i in range(n):
                if self._stopping:
                    break
                if i:
                    time.sleep(self.pulse_gap_ms / 1000.0)
                    if self._stopping:
                        break
                self._pi.write(gpio, 1)
                time.sleep(duration_ms / 1000.0)
                self._pi.write(gpio, 0)
        finally:
            self._pi.write(gpio, 0)   # always leave the valve closed
            self._valve_lock.release()

    def check(self) -> dict:
        if not self._pi.connected:
            return {"ok": False, "message": "pigpiod not connected"}
        for name, gpio in self._pins.items():
            try:
                self._pi.read(gpio)
            except Exception as e:
                return {"ok": False, "message": f"GPIO{gpio} read failed: {e}"}
        return {"ok": True, "message": f"pins: {list(self._pins.keys())}"}

    def hdf5_datasets(self) -> dict:
        return {
            "reward_t": "f8",
            "reward_amount_ul": "f4",
            # 1 if this trial's reward came from the Pav-delay rescue (no lick on a
            # conditional trial), else 0.
            "reward_pavlovian": "i1",
        }

    def hdf5_trial_data(self, ctx: dict) -> dict:
        return {
            "reward_t": ctx.get("reward_t", float("nan")),
            "reward_amount_ul": ctx.get("reward_amount_ul", 0.0),
            "reward_pavlovian": 1 if ctx.get("pavlovian_rescue") else 0,
        }

    def close(self):
        # Signal any in-flight pulse/train to stop, then wait briefly for it to finish so
        # we never disconnect pigpio mid-pulse and leave the valve energized.
        self._stopping = True
        got = self._valve_lock.acquire(timeout=1.0)
        try:
            for gpio in self._pins.values():
                self._pi.write(gpio, 0)
            self._pi.stop()
        finally:
            if got:
                self._valve_lock.release()
