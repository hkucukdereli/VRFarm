#!/usr/bin/env python3
"""
Parabolic Screen Azimuth Warping Module
========================================
For the Harvey Lab mouseVR parabolic screen with DLPDLCR230NPEVM projector.

Screen geometry (all dimensions in inches):
  - Parabola (top view): y = -(0.125 * x^2 - 5)  →  y = -0.125x² + 5
  - Mouse eyes at origin (0, 0)
  - Screen vertex at (0, 5.0) — closest point to mouse
  - Horizontal coverage: ~105° per side (210° total)
  - Projector projects along parabola axis of symmetry (via 45° mirror)

Key insight:
  Since the projector is aligned with the parabola's axis, projector x-pixel
  maps directly to screen x-coordinate. The curvature only affects depth.
  So the warp is 1D: azimuth angle ↔ pixel column.

Framebuffer format:
  With vc4-fkms-v3d driver: 16-bit RGB565 (2 bytes per pixel)
  Without vc4 driver: 32-bit BGRA (4 bytes per pixel)
  This module auto-detects the format from /dev/fb0.

Usage:
  from screen_warp import ScreenWarp
  warp = ScreenWarp()

  # Get pixel column for a visual angle
  col = warp.angle_to_pixel(15.0)   # 15° to the right

  # Get visual angle for a pixel column
  ang = warp.pixel_to_angle(960)    # center pixel

  # Place a rectangle at a specific azimuth angle
  frame = warp.draw_rect_at_angle(azimuth=30, width_deg=10, height_px=200,
                                   color=(0, 255, 0))
"""

import numpy as np
import time
import json
import mmap
import struct
from typing import Tuple, Optional, List, Dict


def rgb_to_rgb565(r: int, g: int, b: int) -> int:
    """Convert 8-bit RGB to 16-bit RGB565."""
    return ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)


class ScreenWarp:
    """Azimuth warp for parabolic screen projection."""

    def __init__(
        self,
        proj_width: int = 1920,
        proj_height: int = 1080,
        # Parabola: y = -(a * x^2 - b)  →  y = -a*x² + b
        parabola_a: float = 0.125,  # curvature coefficient
        parabola_b: float = 5.0,    # vertex distance from mouse (inches)
        # Screen horizontal extent (inches)
        # At 105° azimuth on each side, the ray from the mouse hits the
        # parabola at x ≈ ±7.49". This is the full illuminated extent.
        screen_x_max: float = 7.49,
        log_path: Optional[str] = None,
        use_mmap: bool = True,
    ):
        self.proj_width = proj_width
        self.proj_height = proj_height
        self.parabola_a = parabola_a
        self.parabola_b = parabola_b
        self.screen_x_max = screen_x_max

        # Timing log
        self.log: List[Dict] = []
        self._frame_idx = 0
        self._log_path = log_path
        self._t0: Optional[int] = None

        # Framebuffer setup
        self._use_mmap = use_mmap
        self._fb_file = None
        self._fb_map = None

        # Auto-detect framebuffer format
        self._detect_fb_format()

        # Build the lookup tables
        self._build_tables()

    def _detect_fb_format(self):
        """Detect framebuffer pixel format from /dev/fb0."""
        try:
            with open("/sys/class/graphics/fb0/bits_per_pixel", "r") as f:
                self.fb_bpp = int(f.read().strip())
        except (FileNotFoundError, ValueError):
            self.fb_bpp = 16  # default to RGB565

        self.fb_bytes_per_pixel = self.fb_bpp // 8
        self.fb_size = self.proj_width * self.proj_height * self.fb_bytes_per_pixel

        if self.fb_bpp == 16:
            self.fb_format = "rgb565"
        elif self.fb_bpp == 32:
            self.fb_format = "bgra"
        else:
            self.fb_format = f"unknown_{self.fb_bpp}bpp"

    def _color_to_bytes(self, r: int, g: int, b: int, a: int = 255) -> bytes:
        """Convert RGB color to framebuffer pixel bytes."""
        if self.fb_format == "rgb565":
            return struct.pack("<H", rgb_to_rgb565(r, g, b))
        else:  # bgra
            return bytes([b, g, r, a])

    def _open_fb(self):
        """Open framebuffer (with mmap for faster writes)."""
        if self._fb_map is not None:
            return
        self._fb_file = open("/dev/fb0", "r+b")
        if self._use_mmap:
            self._fb_map = mmap.mmap(self._fb_file.fileno(), self.fb_size)
        else:
            self._fb_map = None

    def _log_event(self, event: str, **kwargs):
        """Record a timestamped event."""
        now = time.perf_counter_ns()
        if self._t0 is None:
            self._t0 = now
        entry = {
            "event": event,
            "timestamp_ns": now,
            "time_since_start_s": (now - self._t0) / 1e9,
            "frame_idx": self._frame_idx,
            **kwargs,
        }
        self.log.append(entry)
        return entry

    def save_log(self, path: Optional[str] = None):
        """Save timing log to JSON file."""
        if path is None:
            path = self._log_path
        if path is None:
            path = f"stim_log_{int(time.time())}.json"

        with open(path, "w") as f:
            json.dump({
                "session_start_ns": self._t0,
                "n_frames": self._frame_idx,
                "fb_format": self.fb_format,
                "fb_bpp": self.fb_bpp,
                "resolution": f"{self.proj_width}x{self.proj_height}",
                "events": self.log,
            }, f, indent=2)
        print(f"Timing log saved: {path} ({len(self.log)} events)")
        return path

    def close(self):
        """Clean up framebuffer and save log."""
        if self._fb_map is not None and self._use_mmap:
            self._fb_map.close()
        if self._fb_file is not None:
            self._fb_file.close()
        self._fb_map = None
        self._fb_file = None
        if self.log:
            self.save_log()

    def _screen_y(self, x: np.ndarray) -> np.ndarray:
        """Parabola y-coordinate for given x (top view)."""
        return -self.parabola_a * x**2 + self.parabola_b

    def _x_to_angle(self, x: float) -> float:
        """Convert screen x-position (inches) to azimuth angle (degrees)."""
        y = self._screen_y(np.array(x))
        return np.degrees(np.arctan2(x, y))

    def _angle_to_x(self, angle_deg: float) -> float:
        """Convert azimuth angle (degrees) to screen x-position (inches).

        Solves: tan(θ) = x / (-a*x² + b)
        Rearranged: a*tan(θ)*x² + x - b*tan(θ) = 0
        """
        theta = np.radians(angle_deg)

        if abs(angle_deg) < 0.001:
            return 0.0

        tan_t = np.tan(theta)
        A = self.parabola_a * tan_t
        B = 1.0
        C = -self.parabola_b * tan_t

        discriminant = B**2 - 4 * A * C
        if discriminant < 0:
            return np.sign(angle_deg) * self.screen_x_max

        x1 = (-B + np.sqrt(discriminant)) / (2 * A)
        x2 = (-B - np.sqrt(discriminant)) / (2 * A)

        if angle_deg > 0:
            x = max(x1, x2)
        else:
            x = min(x1, x2)

        return np.clip(x, -self.screen_x_max, self.screen_x_max)

    def _build_tables(self):
        """Pre-compute pixel ↔ angle lookup tables."""
        self._pixel_to_x = np.linspace(
            -self.screen_x_max, self.screen_x_max, self.proj_width
        )

        self._pixel_angles = np.array([
            self._x_to_angle(x) for x in self._pixel_to_x
        ])

        self.angle_min = self._pixel_angles[0]
        self.angle_max = self._pixel_angles[-1]

        self._angle_range = np.linspace(self.angle_min, self.angle_max, 10000)
        self._angle_to_pixel_interp = np.interp(
            self._angle_range,
            self._pixel_angles,
            np.arange(self.proj_width),
        )

    # ---- Public API ----

    def angle_to_pixel(self, angle_deg: float) -> int:
        """Convert azimuth angle (degrees) to pixel column.

        Args:
            angle_deg: Azimuth angle. 0 = straight ahead,
                       positive = right, negative = left.

        Returns:
            Pixel column index (0 to proj_width-1).
        """
        idx = np.interp(angle_deg, self._angle_range, self._angle_to_pixel_interp)
        return int(np.clip(np.round(idx), 0, self.proj_width - 1))

    def pixel_to_angle(self, pixel_col: int) -> float:
        """Convert pixel column to azimuth angle (degrees)."""
        pixel_col = np.clip(pixel_col, 0, self.proj_width - 1)
        return self._pixel_angles[pixel_col]

    def angle_range_to_pixels(self, center_deg: float, width_deg: float) -> Tuple[int, int]:
        """Convert an angular range to pixel column range."""
        left_angle = center_deg - width_deg / 2
        right_angle = center_deg + width_deg / 2
        return self.angle_to_pixel(left_angle), self.angle_to_pixel(right_angle)

    def degrees_per_pixel(self) -> np.ndarray:
        """Get the angular size of each pixel column."""
        dang = np.diff(self._pixel_angles)
        return np.append(dang, dang[-1])

    def make_warp_map_x(self) -> np.ndarray:
        """Generate an OpenCV-compatible remap table for horizontal warping."""
        uniform_angles = np.linspace(self.angle_min, self.angle_max, self.proj_width)
        map_x_row = np.array([
            self.angle_to_pixel(a) for a in uniform_angles
        ], dtype=np.float32)
        map_x = np.tile(map_x_row, (self.proj_height, 1))
        return map_x

    def make_warp_map(self) -> Tuple[np.ndarray, np.ndarray]:
        """Generate full OpenCV remap tables (horizontal warp only)."""
        map_x = self.make_warp_map_x()
        map_y = np.tile(
            np.arange(self.proj_height, dtype=np.float32).reshape(-1, 1),
            (1, self.proj_width),
        )
        return map_x, map_y

    def warp_frame(self, frame: np.ndarray) -> np.ndarray:
        """Warp a frame from visual-angle-linear space to projector pixel space."""
        import cv2
        map_x, map_y = self.make_warp_map()
        return cv2.remap(frame, map_x, map_y, cv2.INTER_LINEAR)

    # ---- Stimulus helpers ----

    def make_grey_frame(self, grey_value: int = 128) -> np.ndarray:
        """Create a uniform grey frame in the correct framebuffer format."""
        if self.fb_format == "rgb565":
            pixel = rgb_to_rgb565(grey_value, grey_value, grey_value)
            frame = np.full((self.proj_height, self.proj_width), pixel, dtype=np.uint16)
        else:
            frame = np.full((self.proj_height, self.proj_width, 4), grey_value, dtype=np.uint8)
            frame[:, :, 3] = 255
        return frame

    def draw_rect_at_angle(
        self,
        azimuth_deg: float,
        width_deg: float = 10.0,
        height_px: int = 200,
        color: Tuple[int, int, int] = (0, 255, 0),  # RGB green
        background: Optional[np.ndarray] = None,
    ) -> np.ndarray:
        """Draw a rectangle centered at a given azimuth angle.

        The rectangle's horizontal extent is defined in visual degrees,
        so it will subtend the correct angle at the mouse's eyes.

        Args:
            azimuth_deg: Center azimuth angle (degrees). 0 = straight ahead.
            width_deg: Angular width (degrees).
            height_px: Height in pixels (vertical, no warp applied).
            color: RGB color tuple (0-255 each).
            background: Base frame to draw on. If None, uses grey.

        Returns:
            Frame array in the correct framebuffer format.
        """
        if background is None:
            frame = self.make_grey_frame()
        else:
            frame = background.copy()

        left_px, right_px = self.angle_range_to_pixels(azimuth_deg, width_deg)
        if left_px > right_px:
            left_px, right_px = right_px, left_px

        cy = self.proj_height // 2
        top = cy - height_px // 2
        bottom = cy + height_px // 2

        r, g, b = color
        if self.fb_format == "rgb565":
            pixel = rgb_to_rgb565(r, g, b)
            frame[top:bottom, left_px:right_px] = pixel
        else:
            frame[top:bottom, left_px:right_px] = [b, g, r, 255]

        return frame

    def show_frame(self, frame: np.ndarray, label: str = ""):
        """Write a frame to /dev/fb0 and log timing.

        Args:
            frame: Frame array (RGB565 uint16 or BGRA uint8).
            label: Optional label for the log entry.

        Returns:
            Dict with timing info for this frame.
        """
        self._open_fb()
        data = frame.tobytes()

        t_before = time.perf_counter_ns()

        if self._use_mmap and self._fb_map is not None:
            self._fb_map.seek(0)
            self._fb_map.write(data)
            self._fb_map.flush()
        else:
            self._fb_file.seek(0)
            self._fb_file.write(data)
            self._fb_file.flush()

        t_after = time.perf_counter_ns()

        entry = self._log_event(
            "frame",
            label=label,
            write_us=(t_after - t_before) / 1e3,
        )
        self._frame_idx += 1
        return entry

    # ---- Diagnostics ----

    def print_info(self):
        """Print screen geometry and angular mapping info."""
        print(f"Projector: {self.proj_width}×{self.proj_height}")
        print(f"Framebuffer: {self.fb_format} ({self.fb_bpp}bpp)")
        print(f"Parabola: y = -{self.parabola_a}x² + {self.parabola_b}")
        print(f"Screen x range: ±{self.screen_x_max}\"")
        print(f"Angle range: {self.angle_min:.1f}° to {self.angle_max:.1f}°")
        print(f"Total coverage: {self.angle_max - self.angle_min:.1f}°")
        print()

        print("Pixel → Angle mapping (sampled):")
        for px in np.linspace(0, self.proj_width - 1, 11, dtype=int):
            angle = self.pixel_to_angle(px)
            print(f"  pixel {px:5d}  →  {angle:+6.1f}°")
        print()

        print("Angle → Pixel mapping (sampled):")
        for angle in range(-100, 110, 10):
            if self.angle_min <= angle <= self.angle_max:
                px = self.angle_to_pixel(angle)
                print(f"  {angle:+4.0f}°  →  pixel {px:5d}")
        print()

        dpp = self.degrees_per_pixel()
        print("Degrees per pixel (angular resolution):")
        print(f"  Center:     {dpp[self.proj_width // 2]:.4f}°/px")
        print(f"  Left edge:  {dpp[0]:.4f}°/px")
        print(f"  Right edge: {dpp[-1]:.4f}°/px")


# ---- Demo / test ----

if __name__ == "__main__":
    import sys

    warp = ScreenWarp(log_path="stim_log.json")
    warp.print_info()

    if "--demo" in sys.argv:
        print("\n--- Running left/right stimulus demo ---")
        print("10 trials: green square at -30° (left) then +30° (right)")
        print("Square: 10° wide, 200px tall, 2s each, 5s ITI\n")

        grey = warp.make_grey_frame()

        warp._log_event("session_start")

        for trial in range(10):
            warp._log_event("trial_start", trial=trial + 1)

            print(f"Trial {trial + 1}/10 — LEFT (-30°)")
            frame = warp.draw_rect_at_angle(-30, width_deg=10, height_px=200)
            entry = warp.show_frame(frame, label=f"trial{trial+1}_left")
            print(f"  fb write: {entry['write_us']:.0f} µs")
            time.sleep(2)

            print(f"Trial {trial + 1}/10 — RIGHT (+30°)")
            frame = warp.draw_rect_at_angle(+30, width_deg=10, height_px=200)
            entry = warp.show_frame(frame, label=f"trial{trial+1}_right")
            print(f"  fb write: {entry['write_us']:.0f} µs")
            time.sleep(2)

            if trial < 9:
                print("  ITI (5s)")
                warp.show_frame(grey, label=f"trial{trial+1}_iti")
                time.sleep(5)

            warp._log_event("trial_end", trial=trial + 1)

        warp.show_frame(grey, label="end")
        warp._log_event("session_end")

        # Print timing summary
        frame_events = [e for e in warp.log if e["event"] == "frame"]
        write_times = [e["write_us"] for e in frame_events]
        print(f"\n--- Timing Summary ---")
        print(f"Total frames: {len(frame_events)}")
        print(f"FB write time: {np.mean(write_times):.0f} ± {np.std(write_times):.0f} µs")
        print(f"  min: {np.min(write_times):.0f} µs, max: {np.max(write_times):.0f} µs")

        warp.close()
        print("Done!")

    elif "--test-angles" in sys.argv:
        print("\n--- Angle test: showing squares at known angles ---")
        angles = [-90, -75, -60, -45, -30, -15, 0, 15, 30, 45, 60, 75, 90]

        for angle in angles:
            print(f"  {angle:+d}° ...", end=" ", flush=True)
            frame = warp.draw_rect_at_angle(angle, width_deg=5, height_px=150)
            entry = warp.show_frame(frame, label=f"angle_{angle}")
            print(f"({entry['write_us']:.0f} µs)")
            time.sleep(1.5)

        # Show all at once
        print("  All angles simultaneously...")
        frame = warp.make_grey_frame()
        for angle in angles:
            frame = warp.draw_rect_at_angle(
                angle, width_deg=5, height_px=150,
                color=(0, 255, 0),
                background=frame,
            )
        warp.show_frame(frame, label="all_angles")
        time.sleep(3)

        warp.show_frame(warp.make_grey_frame(), label="end")
        warp.close()
        print("Done!")
