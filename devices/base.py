"""
devices/base.py

Base class for all hardware devices. Each device declares:
  - I/O type (I2C, GPIO, CSI, etc.)
  - Task-editable parameters (saved to task YAML, editable in experiment UI)
  - Livestream interface (for real-time monitoring)
  - HDF5 interface (per-trial data saving)
  - Calibration interface (for devices that need it)

Adding a new device = subclassing Device + one new file.
"""

from __future__ import annotations
from dataclasses import dataclass
from enum import Enum


class IOType(Enum):
    I2C = "i2c"
    GPIO_OUT = "gpio_out"
    GPIO_IN = "gpio_in"
    SPI = "spi"
    CSI = "csi"
    HDMI = "hdmi"
    USB = "usb"


@dataclass
class DeviceInfo:
    name: str                       # e.g. "lick_sensor"
    label: str                      # e.g. "Lick Sensor"
    io_type: IOType                 # e.g. IOType.I2C
    required_packages: list[str]    # e.g. ["smbus2"]


class Device:
    """Base class for all hardware devices."""

    info: DeviceInfo  # subclass sets as class variable

    # ── Lifecycle ──

    def init(self, rig_config: dict, task_params: dict):
        """Initialize hardware.
        rig_config: from rig JSON (pins, I2C addr, calibration — fixed per rig)
        task_params: from task YAML devices: section (tunable per experiment)
        """
        raise NotImplementedError

    def check(self) -> dict:
        """Health check. Returns {"ok": bool, "message": str}."""
        raise NotImplementedError

    def close(self):
        """Clean shutdown."""
        raise NotImplementedError

    # ── Livestream ──

    def start_stream(self, callback):
        """Start sending live data. callback(event_dict) pushes to UDP/SSE."""
        pass

    def stop_stream(self):
        pass

    # ── HDF5 ──

    def hdf5_datasets(self) -> dict:
        """Return {name: dtype_or_spec} for per-trial datasets this device writes.

        dtype_or_spec can be:
          - A simple dtype string: "f8", "i4", "f4"
          - A dict with h5py kwargs: {"dtype": h5py.vlen_dtype(np.float64)}

        Every dataset declared here will be created once at session start
        and extended by one row per trial.
        """
        return {}

    def hdf5_trial_data(self, trial_context: dict) -> dict:
        """Return {dataset_name: value} for this trial.

        Called after each trial completes. The trial_context contains
        all timing fields (iti_start_t, stim_onset_t, etc.) plus
        any fields the device itself populated during the trial.
        """
        return {}

    def hdf5_session_data(self) -> dict:
        """Return {name: array} for session-level datasets.

        Called once at session end. For continuous data that doesn't
        fit per-trial structure (e.g., full running speed trace).
        Written as complete arrays, not extended per trial.
        """
        return {}

    # ── Task-editable parameters ──

    @classmethod
    def task_params_schema(cls) -> dict:
        """Declare parameters editable in the experiment UI.
        Returns {param_name: {"type": str, "default": Any, "label": str, ...}}.
        These are saved to the task YAML devices: section.
        Hardware-fixed values (pins, calibrations) stay in rig config only.
        """
        return {}

    # ── Calibration ──

    @property
    def needs_calibration(self) -> bool:
        return False

    def calibrate(self, **kwargs) -> dict:
        """Run calibration routine. Returns data to save in rig config."""
        raise NotImplementedError

    def load_calibration(self, cal_data: dict):
        """Load saved calibration from rig config."""
        pass


# ── Device registry ──

DEVICE_REGISTRY: dict[str, type[Device]] = {}


def register_device(cls: type[Device]) -> type[Device]:
    """Decorator to register a device class."""
    DEVICE_REGISTRY[cls.info.name] = cls
    return cls
