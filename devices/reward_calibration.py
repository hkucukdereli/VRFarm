"""
devices/reward_calibration.py

DEPRECATED — Automated calibration routines. Kept for potential future use.
Manual calibration (editable table in setup UI) is now the default workflow.

Original purpose: deliver N pulses at each test duration, user weighs
collected water and enters volume per duration.
"""

from __future__ import annotations
import time

import numpy as np


def run_calibration(reward, pin_name: str = "main",
                    durations_ms: list[int] | None = None,
                    pulses_per_duration: int = 50,
                    inter_pulse_s: float = 0.2) -> list[dict]:
    """Run calibration sequence. Returns list of {duration_ms, pulses} dicts.
    The UI fills in volume_ul after each batch and calls save_calibration().
    """
    if durations_ms is None:
        durations_ms = [10, 20, 30, 50]

    results = []
    for dur in durations_ms:
        for _ in range(pulses_per_duration):
            reward.pulse(dur, pin_name)
            time.sleep(inter_pulse_s)
        results.append({"duration_ms": dur, "pulses": pulses_per_duration,
                        "volume_ul": None})
    return results


def build_calibration_table(results: list[dict]) -> list[list[float]]:
    """Convert filled-in results to calibration table [[ms, ul], ...].
    volume_ul should be total volume for all pulses at that duration.
    Per-pulse volume = total / pulses.
    """
    table = []
    for r in results:
        if r["volume_ul"] is None:
            continue
        per_pulse_ul = r["volume_ul"] / r["pulses"]
        table.append([r["duration_ms"], per_pulse_ul])
    return table


def compute_pulse_ms(calibration: dict, volume_ul: float,
                     pin_name: str = "main") -> float:
    """Interpolate pulse duration for desired volume from calibration data."""
    from scipy.interpolate import interp1d
    cal = np.array(calibration[pin_name])
    fn = interp1d(cal[:, 1], cal[:, 0], kind="linear",
                  fill_value="extrapolate")
    return float(fn(volume_ul))
