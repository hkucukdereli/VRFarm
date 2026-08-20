"""
devices/display.py

Pygame fullscreen renderer for stimulus display.
Runs on the Follower Pi. Receives SHOW commands with trial index,
looks up pre-generated stim params from NPZ, renders rectangle,
auto-blanks after stim duration.
"""

from __future__ import annotations

import os

# SDL installs its own SIGINT/SIGTERM handlers when the video subsystem inits, which makes the
# host process (pi_api) swallow SIGTERM — so graceful restarts (self-SIGTERM via /api/restart,
# Deploy, Restart-API) silently no-op and the process keeps running stale code until a SIGKILL.
# Tell SDL to leave signals to Python so systemd's SIGTERM restarts pi_api cleanly. Must be set
# before pygame.init() (pygame is imported lazily in start_display).
os.environ["SDL_HINT_NO_SIGNAL_HANDLERS"] = "1"

from .base import Device, DeviceInfo, IOType, register_device


def _prewarm_budget_surfaces(per_bytes: int) -> int:
    """Max full-frame surfaces to pre-warm: ~40% of MemAvailable, hard-capped at 512 MB, >= 1.
    Reads /proc/meminfo (Linux/Pi); on other platforms or a read failure, uses a conservative
    256 MB so pre-warming can never over-commit RAM on a memory-tight Pi."""
    HARD_CAP = 512 * 1024 * 1024
    avail = None
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    avail = int(line.split()[1]) * 1024   # kB -> bytes
                    break
    except Exception:
        avail = None
    budget = min(int(avail * 0.4), HARD_CAP) if avail else 256 * 1024 * 1024
    return max(1, budget // max(1, int(per_bytes)))


@register_device
class Display(Device):
    info = DeviceInfo("display", "Stimulus Display", IOType.HDMI, ["pygame"])

    @classmethod
    def task_params_schema(cls):
        return {
            "background_gray": {
                "type": "float", "default": 0.0,
                "label": "Background gray (0..1)", "min": 0.0, "max": 1.0,
            },
        }

    def init(self, rig_config: dict, task_params: dict):
        self.resolution = tuple(rig_config.get("resolution", [1920, 1080]))
        self.refresh_hz = float(rig_config.get("refresh_hz", 60))
        self.bg_gray = task_params.get("background_gray", 0.0)
        self._screen = None
        self._vsync = False      # set True in start_display when vsync-locked flip is granted
        self._warp = None
        self._patch_cache = {}
        self._blank_cache = {}   # corrected uniform-field surfaces, keyed by bg value
        self._corr_map = None    # (H,W) per-pixel luminance correction C(az) in [0,1], 0 off-screen
        self._sync_rect = None   # cached photodiode sync square (corner, dead-band capped)
        # Photodiode sync-square prefs — authored in the setup UI's photodiode card, and
        # injected into this display config by engine/follower.py / setup's init_display payload.
        self.sync_corner = str(rig_config.get("sync_corner", "top-left"))
        self.sync_size_px = int(rig_config.get("sync_size_px", 0) or 0)   # 0 = auto (full dead band)
        self.sync_brightness = max(0.0, min(1.0, float(rig_config.get("sync_brightness", 1.0))))  # red LED level 0..1

    def start_display(self):
        """Initialize pygame and open fullscreen window.
        Retries up to 3 times with backoff if X11 is not ready.
        """
        import time
        import pygame
        max_retries = 3
        for attempt in range(max_retries):
            try:
                pygame.init()
                flags = pygame.FULLSCREEN | pygame.DOUBLEBUF | pygame.HWSURFACE
                # vsync-locked flip so per-frame sync loops pace to the refresh
                try:
                    self._screen = pygame.display.set_mode(
                        self.resolution, flags, vsync=1)
                    self._vsync = True
                    print("Display: vsync-locked flip")
                except (pygame.error, TypeError):
                    self._screen = pygame.display.set_mode(self.resolution, flags)
                    self._vsync = False
                    print("Display: vsync NOT available (unsynced flip)")
                pygame.mouse.set_visible(False)
                self.blank()
                return
            except pygame.error as e:
                if attempt < max_retries - 1:
                    wait = (attempt + 1) * 1.0
                    print(f"Display init failed (attempt {attempt+1}/{max_retries}): {e}")
                    print(f"  Retrying in {wait}s...")
                    try:
                        pygame.quit()
                    except Exception:
                        pass
                    time.sleep(wait)
                else:
                    raise RuntimeError(
                        f"Display init failed after {max_retries} attempts: {e}. "
                        f"Is X11 running? Check start_projector.sh completed."
                    ) from e

    def show_rect(self, px_x: float, px_y: float, px_size: float,
                  corr_contrast: float, bg_gray: float,
                  sync_square: bool = False, shape: str = "square"):
        """Draw a stimulus (square or circle) at pixel coordinates and flip. Flat, pixel-space
        fallback used when no warp is loaded (the spherical path handles the real experiment)."""
        import pygame
        if self._screen is None:
            return
        # Gray = 0..1 luminance (0 = no light, 1 = max). The red LED is off, so fill
        # green+blue only (R=0); value maps to G=B=value*255.
        bg_lin = max(0.0, min(1.0, bg_gray))
        stim_lin = bg_lin + corr_contrast * (1 - bg_lin)
        rgb = max(0, min(255, int(stim_lin * 255)))
        bg_rgb = max(0, min(255, int(bg_lin * 255)))
        # Fill background
        self._screen.fill((0, bg_rgb, bg_rgb))
        # Draw stimulus (centered at px_x, px_y)
        half = px_size / 2
        rect = pygame.Rect(int(px_x - half), int(px_y - half),
                           int(px_size), int(px_size))
        if str(shape).lower() == "circle":
            pygame.draw.ellipse(self._screen, (0, rgb, rgb), rect)
        else:
            self._screen.fill((0, rgb, rgb), rect)
        # Photodiode sync: red flood of the invisible area (no-op if no warp loaded)
        self._draw_sync_border(sync_square)
        pygame.display.flip()

    def blank(self):
        """Blank to the session background (uniform delivered luminance when a warp is loaded)."""
        self.blank_with_gray(self.bg_gray)

    def blank_with_gray(self, gray_value: float):
        """Fill the visible screen with a gray value (0..1). With a warp loaded, the background is
        luminance-corrected per-pixel so its DELIVERED luminance is uniform across azimuth (same
        full-field correction as the stimulus, so the whole session looks flat); without a warp it
        falls back to a plain uniform fill. Green+blue only (red LED off)."""
        import pygame
        if self._screen is None:
            return
        lin = max(0.0, min(1.0, gray_value))
        if self._warp is not None and self._corr_map is not None:
            surf = self._blank_cache.get(round(lin, 4))
            if surf is None:
                surf = self._build_field_surface(lin)
                self._blank_cache[round(lin, 4)] = surf
            self._screen.blit(surf, (0, 0))
        else:
            rgb = max(0, min(255, int(lin * 255)))
            self._screen.fill((0, rgb, rgb))
        pygame.display.flip()

    def _build_field_surface(self, bg_lin):
        """Uniform background field with the full-field per-pixel luminance correction: valid
        pixels driven at bg_lin*C(az) (uniform delivered luminance), invisible area BLACK."""
        import numpy as np
        import pygame
        valid = np.asarray(self._warp["valid_map"], dtype=bool)
        drive = bg_lin * self._corr_map   # _corr_map is 0 outside the visible screen
        code = np.clip(drive * 255.0, 0, 255).astype(np.uint8)
        pixels = np.zeros((self._corr_map.shape[0], self._corr_map.shape[1], 3), dtype=np.uint8)
        pixels[valid, 1] = code[valid]
        pixels[valid, 2] = code[valid]
        return pygame.surfarray.make_surface(pixels.transpose(1, 0, 2)).convert()  # match display format -> fast blits

    def load_warp(self, npz_path: str):
        """Load warp map for warped rendering."""
        import numpy as np
        from pathlib import Path
        p = Path(npz_path)
        if p.exists():
            self._warp = np.load(str(p))
            self._patch_cache = {}
            self._blank_cache = {}
            self._sync_rect = None
            self._corr_map = self._build_corr_map()   # per-pixel luminance correction
            return True
        return False

    def _build_corr_map(self):
        """Per-pixel luminance correction C(az) in [0,1] from the warp's lum data, honoring
        `lum_correction_mode` (none->1, empirical->measured, theoretical->min(gain)/gain). Multiply
        a pixel's drive by this to attenuate the bright center DOWN to the dim edges — full-field
        equalization so delivered luminance is uniform across azimuth. Off-screen (invalid) pixels
        -> 0. Mirrors shared.stim_generator.luminance_correction_curve / gain_to_correction_curve;
        keep the formula in sync. Returns None if the warp has no usable geometry."""
        import numpy as np
        w = self._warp
        if w is None:
            return None
        keys = w.files if hasattr(w, "files") else list(w.keys())
        if "az_map" not in keys or "valid_map" not in keys:
            return None
        mode = str(w["lum_correction_mode"]) if "lum_correction_mode" in keys else None
        az_arr = corr = None
        if mode == "none":
            pass  # az_arr stays None -> all ones
        elif (mode == "empirical" or (mode is None and "lum_az_empirical" in keys)) \
                and "lum_az_empirical" in keys:
            az_arr = np.asarray(w["lum_az_empirical"], dtype=float)
            corr = np.asarray(w["lum_correction_empirical"], dtype=float)
        elif "lum_gain_theoretical" in keys:
            g = np.maximum(np.asarray(w["lum_gain_theoretical"], dtype=float), 0.05)
            az_arr = np.asarray(w["lum_az"], dtype=float)
            corr = np.min(g) / g
        # (no lum data -> az_arr None -> no correction)
        az_map = np.asarray(w["az_map"], dtype=float)
        valid = np.asarray(w["valid_map"], dtype=bool)
        if az_arr is None:
            cmap = np.ones(az_map.shape, dtype=np.float32)
        else:
            cmap = np.interp(np.abs(np.nan_to_num(az_map, nan=0.0)),
                             az_arr, corr).astype(np.float32)
        cmap[~valid] = 0.0
        return cmap

    def show_patch_spherical(self, az_deg: float, alt_deg: float, size_deg: float,
                             frac: float, bg_gray: float,
                             sync_square: bool = False, shape: str = "square",
                             apply_lum: bool = True):
        """Render a stimulus patch in true visual-angle space through the warp map, so it
        subtends `size_deg` at any azimuth/altitude and is shaped to the screen curvature. `frac`
        is the UNcorrected headroom fraction (stimulus luminance = bg + frac*(1-bg) before the
        per-pixel luminance correction). `shape` is "square" or "circle". `apply_lum` applies the
        full-field per-pixel luminance correction (default); set False to render RAW drive — used
        by the intensity-cal measurement, which must read the uncorrected delivered luminance.
        Surfaces are cached; silently no-ops if no warp is loaded (follower falls back to show_rect)."""
        import pygame
        surf = self._get_patch_surface(az_deg, alt_deg, size_deg, frac, bg_gray, shape, apply_lum)
        if surf is None:
            return
        self._screen.blit(surf, (0, 0))
        self._draw_sync_border(sync_square)
        pygame.display.flip()

    def _get_patch_surface(self, az_deg, alt_deg, size_deg, frac, bg_gray, shape, apply_lum):
        """Cached patch surface for these params (build + cache on miss); None if the display/warp
        isn't ready. Shared by show_patch_spherical (which then blits/flips) and prewarm (cache only),
        so both use exactly the same cache key."""
        if self._screen is None or self._warp is None:
            return None
        key = (round(float(az_deg), 2), round(float(alt_deg), 2),
               round(float(size_deg), 2), round(float(frac), 4),
               round(float(bg_gray), 4), str(shape), bool(apply_lum))
        surf = self._patch_cache.get(key)
        if surf is None:
            surf = self._build_patch_surface(az_deg, alt_deg, size_deg, frac, bg_gray, shape, apply_lum)
            self._patch_cache[key] = surf
        return surf

    def prewarm(self, combos, bg_gray, shape, apply_lum=True):
        """Build + cache the patch surface for each (az, alt, size_deg, frac) in `combos` (bg/shape
        held constant), up to a RAM budget, so the FIRST show of each is a warm blit instead of a
        cold surface build on the stimulus-onset path. Returns (built, skipped). Bounded by
        _prewarm_budget_surfaces so it can't over-commit memory on a small Pi."""
        if self._screen is None or self._warp is None:
            return (0, 0)
        w, h = self.resolution
        per = int(w) * int(h) * 4                       # bytes per cached (32-bit post-convert) surface
        max_surfaces = _prewarm_budget_surfaces(per)
        built = skipped = 0
        for (az, alt, size_deg, frac) in combos:
            if len(self._patch_cache) >= max_surfaces:   # budget reached -> leave the rest lazy
                skipped += 1
                continue
            try:
                self._get_patch_surface(az, alt, size_deg, frac, bg_gray, shape, apply_lum)
                built += 1
            except Exception as e:
                print(f"[prewarm] skip ({az},{alt},{size_deg},{frac}): {e}", flush=True)
                skipped += 1
        return (built, skipped)

    def _build_patch_surface(self, az0, alt0, size_deg, frac, bg_gray,
                             shape="square", apply_lum=True):
        """Compose the full (H, W) framebuffer for one patch: a bright stimulus over a background,
        both green+blue only (R=0). `shape` is "square" (within size_deg/2 in BOTH azimuth and
        altitude) or "circle" (radius size_deg/2 in the az/alt plane) — a visual-angle shape,
        warp-shaped to the screen curvature. When apply_lum, EVERY pixel's drive (background AND
        stimulus) is multiplied by the per-pixel correction C(az) so delivered luminance is uniform
        across azimuth (the bright center is darkened to match the dim edges); Bg=1 then means full
        LED at the edges and attenuated at center. The invisible area (~valid_map) stays BLACK —
        off-screen, reserved for the red photodiode sync square (see _draw_sync_border)."""
        import numpy as np
        import pygame
        az_map = self._warp["az_map"]
        alt_map = self._warp["alt_map"]
        valid = np.asarray(self._warp["valid_map"], dtype=bool)
        # Ideal (uncorrected) 0..1 drive: bg everywhere, stimulus = bg + frac*(1-bg) inside the shape.
        bg_lin = max(0.0, min(1.0, bg_gray))
        stim_lin = bg_lin + max(0.0, min(1.0, frac)) * (1 - bg_lin)
        half = size_deg / 2.0
        daz = az_map - az0
        dalt = alt_map - alt0
        if str(shape).lower() == "circle":
            inside = (daz * daz + dalt * dalt) <= half * half
        else:
            inside = (np.abs(daz) <= half) & (np.abs(dalt) <= half)
        lit = valid & inside
        ideal = np.where(lit, stim_lin, bg_lin)
        # Full-field per-pixel luminance correction (or raw drive, masked to the visible screen).
        if apply_lum and self._corr_map is not None:
            drive = ideal * self._corr_map
        else:
            drive = np.where(valid, ideal, 0.0)
        code = np.clip(drive * 255.0, 0, 255).astype(np.uint8)   # green+blue only, R=0
        pixels = np.zeros((az_map.shape[0], az_map.shape[1], 3), dtype=np.uint8)
        pixels[valid, 1] = code[valid]
        pixels[valid, 2] = code[valid]
        return pygame.surfarray.make_surface(pixels.transpose(1, 0, 2)).convert()  # match display format -> fast blits

    def show_checkers(self, n: int = 8, use_warp: bool = True):
        """Draw checkerboard. Uses warp map if loaded and use_warp=True, else simple grid."""
        import pygame
        if self._screen is None:
            return
        if use_warp and self._warp is not None:
            self._show_checkers_warped(n)
        else:
            self._show_checkers_simple(n)

    def _show_checkers_simple(self, n: int = 8):
        """Simple n x n pixel-space checkerboard."""
        import pygame
        w, h = self.resolution
        cw, ch = w // n, h // n
        for row in range(n):
            for col in range(n):
                color = (255, 255, 255) if (row + col) % 2 == 0 else (0, 0, 0)
                rect = pygame.Rect(col * cw, row * ch, cw, ch)
                self._screen.fill(color, rect)
        pygame.display.flip()

    def _show_checkers_warped(self, n: int = 8):
        """Checkerboard in visual angle space, rendered through warp map."""
        import numpy as np
        import pygame
        az_map = self._warp["az_map"]    # (H, W) degrees
        alt_map = self._warp["alt_map"]
        valid = self._warp["valid_map"]

        # Checker size in degrees
        az_range = np.nanmax(az_map[valid]) - np.nanmin(az_map[valid])
        alt_range = np.nanmax(alt_map[valid]) - np.nanmin(alt_map[valid])
        az_min = np.nanmin(az_map[valid])
        alt_min = np.nanmin(alt_map[valid])
        az_step = az_range / n
        alt_step = alt_range / n

        # Build pixel array
        h, w = az_map.shape
        pixels = np.zeros((h, w, 3), dtype=np.uint8)
        # Background mid-gray for invalid pixels
        pixels[:] = 40

        az_idx = ((az_map - az_min) / az_step).astype(np.int32)
        alt_idx = ((alt_map - alt_min) / alt_step).astype(np.int32)
        checker = (az_idx + alt_idx) % 2 == 0
        pixels[valid & checker] = 255
        pixels[valid & ~checker] = 0

        surf = pygame.surfarray.make_surface(pixels.transpose(1, 0, 2)).convert()  # match display format
        self._screen.blit(surf, (0, 0))
        pygame.display.flip()

    def run_sync_test(self, every_n: int, stop_event):
        """Setup-UI diagnostic: flash the red photodiode sync square ON for one frame every
        `every_n` frames (black otherwise), flipping every frame, until `stop_event` is set.
        Simulates the session's sync pulses so the photodiode/scope can be exercised without
        running a real experiment. Runs in the pygame display thread (blocking)."""
        import pygame
        if self._screen is None:
            return
        every_n = max(1, int(every_n))
        rect = pygame.Rect(*self._sync_test_rect())
        clock = pygame.time.Clock()
        frame = 0
        while not stop_event.is_set():
            self._screen.fill((0, 0, 0))
            if frame % every_n == 0:
                self._screen.fill(self._sync_rgb(), rect)   # red sync square (off-screen dead band)
            pygame.display.flip()
            try:
                pygame.event.pump()                    # keep the fullscreen window responsive
            except Exception:
                pass
            clock.tick(120)                            # cap; vsync also paces the flip if available
            frame += 1
        self._screen.fill((0, 0, 0))
        pygame.display.flip()

    def run_sync_burst(self, every_n: int, duration_s: float = 1.0) -> int:
        """Bounded variant of run_sync_test for the photodiode INIT verify: flash the red sync square
        ON for one frame every `every_n` frames (black otherwise) for `duration_s` seconds, then
        blank, and RETURN the number of flashes (red-on frames) actually emitted. That count is the
        ground truth the photodiode compares its detected pulses against (±1). Runs on the pygame
        thread (blocking ~duration_s). `every_n` is floored at 2 so the flashes are always separated
        by a black frame (every_n=1 = continuous red = a single edge, not countable pulses)."""
        import pygame
        import time as _t
        if self._screen is None:
            return 0
        every_n = max(2, int(every_n))
        rect = pygame.Rect(*self._sync_test_rect())
        clock = pygame.time.Clock()
        t_end = _t.monotonic() + max(0.05, float(duration_s))
        frame = 0
        flashes = 0
        while _t.monotonic() < t_end:
            self._screen.fill((0, 0, 0))
            if frame % every_n == 0:
                self._screen.fill(self._sync_rgb(), rect)   # one red frame = one detectable pulse
                flashes += 1
            pygame.display.flip()
            try:
                pygame.event.pump()
            except Exception:
                pass
            clock.tick(120)
            frame += 1
        self._screen.fill((0, 0, 0))
        pygame.display.flip()
        return flashes

    def _sync_rgb(self):
        """Sync-patch colour: (R, 0, 0) with R scaled by sync_brightness (0..1). Green/blue stay 0
        so the pulse is pure red and never enters the mouse's visual field; brightness 0 = off (black)."""
        r = max(0, min(255, int(round(max(0.0, min(1.0, self.sync_brightness)) * 255))))
        return (r, 0, 0)

    def set_sync_layout(self, corner=None, size_px=None, brightness=None):
        """Live-update the sync-square corner/size/brightness (from the setup-UI Test) and drop the
        cached rect so _sync_patch_rect / _sync_test_rect recompute for the new placement — lets the
        controls change the square without a full Save + re-deploy. None leaves a field unchanged."""
        if corner is not None:
            self.sync_corner = str(corner)
        if size_px is not None:
            self.sync_size_px = int(size_px or 0)
        if brightness is not None:
            self.sync_brightness = max(0.0, min(1.0, float(brightness)))
        self._sync_rect = None   # invalidate the cached patch rect

    def _sync_test_rect(self):
        """Rect for the TEST flash: the real warp-derived sync-square rect when a warp is loaded,
        else a corner square of sync_size_px (or a 120px default) at sync_corner — so the test
        still produces a pulse before a warp exists."""
        r = self._sync_patch_rect() if self._warp is not None else None
        if r is not None:
            return r
        w, h = self.resolution
        s = int(self.sync_size_px or 120)
        s = max(8, min(s, min(w, h)))
        x = 0 if "left" in self.sync_corner else w - s
        y = 0 if "top" in self.sync_corner else h - s
        return (x, y, s, s)

    def _draw_sync_border(self, on: bool):
        """Photodiode sync: paint the pure-red sync square in the configured corner on `on` frames.
        WITH a warp the square is capped to the calibrated off-screen dead band (frame_x from the
        warp's valid_map): that band is already BLACK on off-frames, so we only paint red when on —
        the pulse is red-on-black (detectable) and off-screen (the mouse never sees it).
        WITHOUT a warp there is no dark region — the whole panel shows the green+blue stimulus field,
        and red merely REPLACING bright cyan is ~no brightness change, so a broadband photodiode
        won't trigger (this is why the setup flash test, which clears to black, works but a flat run
        didn't). There we force the corner patch red-on-BLACK every frame (a small black square in the
        display corner, where the photodiode is taped) so the pulse is a real brightness spike. That
        patch IS visible to the mouse — generate a warp for a true off-screen, invisible sync patch."""
        import pygame
        if self._screen is None:
            return
        if self._warp is not None:
            if not on:
                return                      # dead band is already black; only flash red
            rect = self._sync_patch_rect()
            if rect is not None:
                self._screen.fill(self._sync_rgb(), pygame.Rect(*rect))
        else:
            rect = self._sync_test_rect()   # pre-warp corner fallback (same rect as the setup test)
            if rect is not None:            # red on flash frames, black otherwise -> detectable edge
                self._screen.fill(self._sync_rgb() if on else (0, 0, 0), pygame.Rect(*rect))

    def _sync_patch_rect(self):
        """Cached (x, y, w, h) of the sync square in the configured corner. The size is
        sync_size_px, capped at frame_x — the dead-band width outside the usable-area
        boundary from geo calibration (derived from the warp's valid_map: first column
        containing any visible pixel; symmetric left/right) — so the square can never
        overlap the visible screen. sync_size_px = 0 means auto (the full dead band).
        Tracks recalibration automatically via load_warp's cache reset."""
        import numpy as np
        if self._sync_rect is not None:
            return self._sync_rect
        if self._warp is None:
            return None
        valid = self._warp["valid_map"]
        H, W = valid.shape
        cols = np.flatnonzero(valid.any(axis=0))
        fx = int(cols[0]) if cols.size else 0
        if fx < 4:
            fx = 40   # boundary at the panel edge -> no dead band; keep a detectable patch
        s = self.sync_size_px if self.sync_size_px > 0 else fx
        s = max(4, min(s, fx))   # hard cap: stay inside the dead band
        corner = self.sync_corner.lower().replace("_", "-")
        x = 0 if "left" in corner else W - s
        y = 0 if "top" in corner else H - s
        self._sync_rect = (x, y, s, s)
        return self._sync_rect

    def check(self) -> dict:
        try:
            import pygame
            pygame.init()
            info = pygame.display.Info()
            pygame.quit()
            return {"ok": True,
                    "message": f"{info.current_w}x{info.current_h}"}
        except Exception as e:
            return {"ok": False, "message": str(e)}

    def shutdown(self):
        import pygame
        pygame.quit()
        self._screen = None

    def close(self):
        self.shutdown()
