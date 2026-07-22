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
        self._warp = None
        self._patch_cache = {}
        self._sync_surf = None   # red photodiode flood for the invisible (off-screen) area

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
                    print("Display: vsync-locked flip")
                except (pygame.error, TypeError):
                    self._screen = pygame.display.set_mode(self.resolution, flags)
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
        """Fill screen with background color."""
        import pygame
        if self._screen is None:
            return
        bg_lin = max(0.0, min(1.0, self.bg_gray))
        bg_rgb = max(0, min(255, int(bg_lin * 255)))
        self._screen.fill((0, bg_rgb, bg_rgb))
        pygame.display.flip()

    def blank_with_gray(self, gray_value: float):
        """Fill screen with an arbitrary gray value (0..1: 0 = darkest, 1 = brightest)."""
        import pygame
        if self._screen is None:
            return
        lin = max(0.0, min(1.0, gray_value))
        rgb = max(0, min(255, int(lin * 255)))
        self._screen.fill((0, rgb, rgb))   # red LED off -> green+blue only
        pygame.display.flip()

    def load_warp(self, npz_path: str):
        """Load warp map for warped rendering."""
        import numpy as np
        from pathlib import Path
        p = Path(npz_path)
        if p.exists():
            self._warp = np.load(str(p))
            self._patch_cache = {}
            self._sync_surf = None
            return True
        return False

    def show_patch_spherical(self, az_deg: float, alt_deg: float, size_deg: float,
                             corr_contrast: float, bg_gray: float,
                             sync_square: bool = False, shape: str = "square"):
        """Render a stimulus patch in true visual-angle space through the warp map, so it
        subtends `size_deg` at any azimuth/altitude and is shaped to the screen curvature.
        `shape` is "square" or "circle". Surfaces are cached per (az, alt, size, contrast, bg,
        shape) — positions recur every block, so a trial re-blits a prebuilt surface. Silently
        no-ops if no warp is loaded (the follower falls back to show_rect)."""
        import pygame
        if self._screen is None or self._warp is None:
            return
        key = (round(float(az_deg), 2), round(float(alt_deg), 2),
               round(float(size_deg), 2), round(float(corr_contrast), 4),
               round(float(bg_gray), 4), str(shape))
        surf = self._patch_cache.get(key)
        if surf is None:
            surf = self._build_patch_surface(az_deg, alt_deg, size_deg,
                                             corr_contrast, bg_gray, shape)
            self._patch_cache[key] = surf
        self._screen.blit(surf, (0, 0))
        self._draw_sync_border(sync_square)
        pygame.display.flip()

    def _build_patch_surface(self, az0, alt0, size_deg, corr_contrast, bg_gray,
                             shape="square"):
        """Compose the full (H, W) framebuffer for one patch: a bright stimulus over a darker
        background, both green+blue only (R=0). `shape` is "square" (within size_deg/2 in BOTH
        azimuth and altitude) or "circle" (within radius size_deg/2 in the az/alt plane) — a
        visual-angle shape, warp-shaped to the screen curvature. The invisible area (outside the
        marked visible screen, ~valid_map) is left BLACK — it's off-screen and reserved for the
        red photodiode flood (see _draw_sync_border)."""
        import numpy as np
        import pygame
        az_map = self._warp["az_map"]
        alt_map = self._warp["alt_map"]
        valid = self._warp["valid_map"]
        # Gray 0..1 -> 8-bit (same mapping as show_rect)
        bg_lin = max(0.0, min(1.0, bg_gray))
        stim_lin = bg_lin + corr_contrast * (1 - bg_lin)
        rgb = max(0, min(255, int(stim_lin * 255)))
        bg_rgb = max(0, min(255, int(bg_lin * 255)))
        # Stimulus shape in visual-angle space, radius/half-side = size_deg/2.
        half = size_deg / 2.0
        daz = az_map - az0
        dalt = alt_map - alt0
        if str(shape).lower() == "circle":
            inside = (daz * daz + dalt * dalt) <= half * half
        else:
            inside = (np.abs(daz) <= half) & (np.abs(dalt) <= half)
        lit = valid & inside
        # Green+blue only: G=B=value, R=0. Visible area gets background/stimulus; the invisible
        # off-screen area stays (0,0,0) so the red sync flood has a dark baseline.
        gb = np.where(lit, rgb, bg_rgb).astype(np.uint8)
        pixels = np.zeros((az_map.shape[0], az_map.shape[1], 3), dtype=np.uint8)
        pixels[valid, 1] = gb[valid]
        pixels[valid, 2] = gb[valid]
        return pygame.surfarray.make_surface(pixels.transpose(1, 0, 2))

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

        surf = pygame.surfarray.make_surface(pixels.transpose(1, 0, 2))
        self._screen.blit(surf, (0, 0))
        pygame.display.flip()

    def _draw_sync_border(self, on: bool):
        """Photodiode sync: when `on`, flood the INVISIBLE area (projector pixels outside the
        marked visible screen, ~valid_map) with max red. The visible stimulus/background is
        green+blue only (R=0), so red is reserved for the off-screen photodiode zone and never
        reaches the mouse's visual field. When off, nothing is drawn — the patch surface already
        left the invisible area black, so the photodiode sees a clean red pulse (see
        _show_synced: ON every Nth frame). No-op on the flat fallback (no warp/valid_map)."""
        import pygame
        if self._screen is None or self._warp is None or not on:
            return
        surf = self._sync_border_surface()
        if surf is not None:
            self._screen.blit(surf, (0, 0))

    def _sync_border_surface(self):
        """Cached full-screen surface: max red where the pixel is off the visible screen
        (~valid_map), transparent (black colorkey) elsewhere, so one blit reddens only the
        invisible area and leaves the visible green+blue patch untouched."""
        import numpy as np
        import pygame
        if self._sync_surf is not None:
            return self._sync_surf
        if self._warp is None:
            return None
        valid = self._warp["valid_map"]
        px = np.zeros((valid.shape[0], valid.shape[1], 3), dtype=np.uint8)
        px[~valid, 0] = 255   # max red in the invisible (off-screen) area
        surf = pygame.surfarray.make_surface(px.transpose(1, 0, 2))
        surf.set_colorkey((0, 0, 0))   # black = transparent -> only the red border paints
        self._sync_surf = surf
        return self._sync_surf

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
