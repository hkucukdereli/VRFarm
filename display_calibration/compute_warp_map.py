"""
compute_warp_map.py

Computes the geometric warp map for the parabolic screen.

For every projector pixel (px, py), finds the visual angle (azimuth, altitude)
it corresponds to as seen from the mouse eye. This is the INVERSE map used by
PsychoPy to remap a flat image onto the curved screen correctly.

Also computes the FORWARD map: given a target visual angle (az, alt), which
pixel to illuminate — used for stimulus generation.

Outputs (saved to warp_map.npz):
  - az_map:    (H, W) array, azimuth in degrees for each pixel
  - alt_map:   (H, W) array, altitude in degrees for each pixel
  - valid_map: (H, W) bool, True where pixel hits the screen
  - px_from_az: (N_az, N_alt) pixel-x for each (az, alt) sample
  - py_from_az: (N_az, N_alt) pixel-y for each (az, alt) sample
  - az_samples: azimuth sample points (degrees)
  - alt_samples: altitude sample points (degrees)

Run:
  python compute_warp_map.py              # uses calibration/rig_geometry.yaml
  python compute_warp_map.py --validate  # also shows validation plot
"""

import argparse
import numpy as np
import yaml
from pathlib import Path
from scipy.interpolate import RegularGridInterpolator

# ── Load geometry ──────────────────────────────────────────────────────────────

CAL_DIR = Path(__file__).parent
GEO_FILE = CAL_DIR / "rig_geometry.yaml"

def load_geometry(path=GEO_FILE):
    with open(path) as f:
        return normalize_geo(yaml.safe_load(f))


def normalize_geo(geo):
    """Derive screen.altitude_min/max from azimuth_height (eye/az-line above the screen
    bottom) so this matches the landmark calibrator. The eye sits `h` above the bottom at
    depth `A`, screen height `hc`:
      altitude_min = atan(-h / A)   (bottom, below eye)
      altitude_max = atan((hc-h)/A) (top, above eye)
    In-place; no-op if azimuth_height is absent (older files keep their stored altitudes)."""
    s = (geo or {}).get("screen", {})
    h = s.get("azimuth_height")
    A = s.get("parabola_A")
    hc = s.get("height_cm")
    if h is not None and A and hc is not None:
        s["altitude_min_deg"] = float(np.degrees(np.arctan2(-h, A)))
        s["altitude_max_deg"] = float(np.degrees(np.arctan2(hc - h, A)))
    return geo

# ── Screen intersection ────────────────────────────────────────────────────────

def ray_screen_intersection(az_deg, alt_deg, A, B):
    """
    Find where a ray from the mouse eye (origin) at (azimuth, altitude)
    intersects the parabolic screen y = A - B*x².

    The screen is a parabolic cylinder — same parabola at every height.
    Altitude only affects the z coordinate of the intersection point.

    Returns:
        (x, y, z) in inches, or None if no intersection in front of mouse
    """
    az  = np.radians(az_deg)
    alt = np.radians(alt_deg)

    # Horizontal ray direction components
    dx = np.sin(az)   # lateral
    dy = np.cos(az)   # depth (forward)
    cos_alt = np.cos(alt)

    # Horizontal plane: intersection of ray (r*dx, r*dy) with y = A - B*x²
    # r*dy = A - B*(r*dx)²
    # B*dx²*r² + dy*r - A = 0
    if abs(dx) < 1e-10:
        # Ray is straight ahead
        if dy <= 0:
            return None
        r_horiz = A / dy
    else:
        a_coef = B * dx * dx
        b_coef = dy
        c_coef = -A
        discriminant = b_coef**2 - 4 * a_coef * c_coef
        if discriminant < 0:
            return None
        r_horiz = (-b_coef + np.sqrt(discriminant)) / (2 * a_coef)
        if r_horiz <= 0:
            r_horiz = (-b_coef - np.sqrt(discriminant)) / (2 * a_coef)
        if r_horiz <= 0:
            return None

    # 3D intersection point
    # For a parabolic cylinder, x and y are determined by horizontal ray,
    # z is determined by altitude angle and the actual 3D ray length
    x_s = r_horiz * dx
    y_s = r_horiz * dy
    # 3D ray length: r_3d = r_horiz / cos(alt)
    r_3d = r_horiz / np.cos(alt)
    z_s = r_3d * np.sin(alt)

    return (x_s, y_s, z_s)


# ── Projector geometry ─────────────────────────────────────────────────────────

def build_projector(geo):
    """
    Build projector geometry from rig parameters.

    Returns a dict with:
      pos:        projector lens position (x, y, z) in cm
      right:      unit vector along projector image width (+x direction)
      up:         unit vector along projector image height (+y direction)
      forward:    unit vector along projector optical axis (toward screen)
      img_w:      projected image width in cm
      img_h:      projected image height in cm
      res:        (width, height) in pixels
    """
    p = geo['projector']
    res_w, res_h = p['resolution']
    throw_dist = p['throw_distance_cm']
    throw_ratio = p['throw_ratio']
    ax_el = np.radians(p['optical_axis_elevation_deg'])
    lat_offset = p.get('lateral_offset_cm', 0.0)

    # horizontal_stretch / vertical_stretch decouple H and V scale from the panel
    # 16:9 aspect (anamorphic): >1 makes that dimension's content span more pixels =
    # bigger on screen. Needed because the screen aspect != projector 16:9.
    base = throw_dist / throw_ratio
    hstretch = p.get('horizontal_stretch', 1.0)
    vstretch = p.get('vertical_stretch', 1.0)
    img_w = base / hstretch
    img_h = base * (res_h / res_w) / vstretch

    # Screen geometry
    A = geo['screen']['parabola_A']
    # Screen center: at 0° azimuth, middle altitude
    alt_min = geo['screen']['altitude_min_deg']
    alt_max = geo['screen']['altitude_max_deg']
    alt_center = np.radians((alt_min + alt_max) / 2)
    az_center = 0.0

    sc = ray_screen_intersection(az_center, np.degrees(alt_center),
                                  A, geo['screen']['parabola_B'])
    if sc is None:
        raise ValueError("Could not find screen center — check geometry params")
    scx, scy, scz = sc

    # optical_axis_elevation_deg is where the IMAGE CENTER hits the screen,
    # not necessarily the true optical axis (which may differ due to lens offset).
    # Forward vector: direction from projector toward screen center.
    fwd = np.array([
        lat_offset,               # x (lateral, small offset if any)
        np.cos(ax_el),            # y (depth component, toward screen)
        -np.sin(ax_el)            # z (downward tilt of beam)
    ])
    fwd = fwd / np.linalg.norm(fwd)

    # Projector axes: right = horizontal, up = vertical (in projector plane)
    world_up = np.array([0, 0, 1])
    right = np.cross(fwd, world_up)
    right = right / np.linalg.norm(right)
    up = np.cross(right, fwd)
    up = up / np.linalg.norm(up)

    # Lens vertical offset: 0 = center, 1.0 = bottom edge (100% upward).
    # A lens-shifted projector is optically equivalent to shifting the
    # projector body in the opposite direction. With 100% upward offset
    # the optical axis is at the image bottom edge, so the projector must
    # sit lower for the image center to still hit the screen center.
    lens_offset_v = p.get('lens_offset_vertical', 0.0)
    # NOTE: lens shift is applied ONCE, as a pixel-origin shift in
    # screen_point_to_pixel / compute_inverse_map (the -ov*0.5 term). Do NOT also
    # move the body here (that double-corrected and sat the projector ~7cm too low,
    # exaggerating the bottom keystone). Body aims straight along fwd at screen center.
    proj_pos = np.array([scx, scy, scz]) - throw_dist * fwd

    return {
        'pos':    proj_pos,
        'right':  right,
        'up':     up,
        'forward': fwd,
        'img_w':  img_w,
        'img_h':  img_h,
        'res':    (res_w, res_h),
        'throw':  throw_dist,
        'lens_offset_v': lens_offset_v,
    }


def screen_point_to_pixel(pt, proj):
    """
    Project a 3D screen point to projector pixel coordinates.

    Uses perspective projection from projector lens.

    Returns (px, py) as floats, or None if point is behind projector.
    """
    # Vector from projector to screen point
    v = np.array(pt) - proj['pos']

    # Distance along optical axis
    depth = np.dot(v, proj['forward'])
    if depth <= 0:
        return None

    # Project onto image plane (normalize by depth to get image-plane coords)
    x_img = np.dot(v, proj['right'])  / depth * proj['throw']
    y_img = np.dot(v, proj['up'])     / depth * proj['throw']

    # Convert from inches to pixels
    # Lens offset shifts the image origin vertically.
    # offset=0: optical axis at image center, y_img in [-h/2, +h/2]
    # offset=1: optical axis at bottom edge, y_img in [0, h]
    res_w, res_h = proj['res']
    ov = proj.get('lens_offset_v', 0.0)
    px = (x_img / proj['img_w'] + 0.5) * res_w
    py = (1.0 - (y_img / proj['img_h'] + 0.5 - ov * 0.5)) * res_h

    return (px, py)


# ── Compute maps ───────────────────────────────────────────────────────────────

def compute_inverse_map(geo, proj):
    """
    Inverse map: for each projector pixel, what visual angle does it correspond to?
    Computed by casting rays from projector through each pixel to the screen,
    then computing the angle from the mouse eye to the intersection point.

    Returns az_map, alt_map, valid_map — all shape (res_h, res_w).
    """
    A = geo['screen']['parabola_A']
    B = geo['screen']['parabola_B']
    res_w, res_h = proj['res']
    alt_min = geo['screen']['altitude_min_deg']
    alt_max = geo['screen']['altitude_max_deg']
    # Usable-pixel rectangle from the landmark calibrator (where projector light actually
    # falls on the physical screen). Replaces the old azimuth_max_deg gating — horizontal
    # extent is now defined by the frame + ±90° landmarks, not a stored max azimuth.
    cal = geo.get('calibration', {})
    fx = int(cal.get('frame_x', 0))
    fy = int(cal.get('frame_y', 0))

    az_map  = np.full((res_h, res_w), np.nan)
    alt_map = np.full((res_h, res_w), np.nan)
    valid   = np.zeros((res_h, res_w), dtype=bool)

    # Pixel grid
    px_arr = np.arange(res_w)
    py_arr = np.arange(res_h)
    PX, PY = np.meshgrid(px_arr, py_arr)

    # Pixel → image plane position (inches relative to optical axis)
    # Lens offset shifts the origin: offset=1 means axis at bottom edge
    ov = proj.get('lens_offset_v', 0.0)
    x_img = (PX / res_w - 0.5) * proj['img_w']
    y_img = (0.5 - PY / res_h + ov * 0.5) * proj['img_h']

    # Ray direction from projector (in world coords)
    # ray = forward * throw + right * x_img + up * y_img, then normalize
    ray_x = proj['forward'][0] * proj['throw'] + proj['right'][0] * x_img + proj['up'][0] * y_img
    ray_y = proj['forward'][1] * proj['throw'] + proj['right'][1] * x_img + proj['up'][1] * y_img
    ray_z = proj['forward'][2] * proj['throw'] + proj['right'][2] * x_img + proj['up'][2] * y_img

    # Intersect each projector ray with parabolic screen
    # y_s = A - B*x_s²
    # Parametric ray from projector: (x,y,z) = proj_pos + t*(ray_x, ray_y, ray_z)
    # x_s = proj['pos'][0] + t*ray_x
    # y_s = proj['pos'][1] + t*ray_y  = A - B*x_s²

    px0, py0, pz0 = proj['pos']

    # Quadratic in t: B*(px0 + t*ray_x)² + (py0 + t*ray_y) - A = 0
    # B*(px0² + 2*px0*t*ray_x + t²*ray_x²) + py0 + t*ray_y - A = 0
    # t²*(B*ray_x²) + t*(2*B*px0*ray_x + ray_y) + (B*px0² + py0 - A) = 0
    a_q = B * ray_x**2
    b_q = 2*B*px0*ray_x + ray_y
    c_q = B*px0**2 + py0 - A

    disc = b_q**2 - 4*a_q*c_q

    # Only consider pixels where discriminant > 0 and t > 0
    with np.errstate(invalid='ignore'):
        disc_ok = disc >= 0
        sqrt_disc = np.where(disc_ok, np.sqrt(np.maximum(disc, 0)), 0)

        # Two solutions, take the smaller positive t (closer intersection)
        t1 = np.where(np.abs(a_q) > 1e-10,
                      (-b_q + sqrt_disc) / (2*a_q),
                      np.where(np.abs(b_q) > 1e-10, -c_q/b_q, np.inf))
        t2 = np.where(np.abs(a_q) > 1e-10,
                      (-b_q - sqrt_disc) / (2*a_q),
                      np.inf)

        # Choose smallest positive t
        t1_ok = t1 > 0.01
        t2_ok = t2 > 0.01
        t = np.where(
            t1_ok & t2_ok, np.minimum(t1, t2),
            np.where(t1_ok, t1,
            np.where(t2_ok, t2, np.inf))
        )

    # 3D intersection points
    xs = px0 + t * ray_x
    ys = py0 + t * ray_y
    zs = pz0 + t * ray_z

    # Visual angles from mouse eye (origin)
    r_horiz = np.sqrt(xs**2 + ys**2)
    az_rad  = np.arctan2(xs, ys)     # azimuth: 0=forward, +right
    alt_rad = np.arctan2(zs, r_horiz)

    az_deg_arr  = np.degrees(az_rad)
    alt_deg_arr = np.degrees(alt_rad)

    # Validity: t finite, in front of mouse, within the altitude band, and inside the
    # usable-pixel rectangle (the frame marks pixels that land on the physical screen).
    in_frame = (PX >= fx) & (PX < res_w - fx) & (PY >= fy) & (PY < res_h - fy)
    hit = (t < 1e9) & disc_ok & in_frame & \
          (alt_deg_arr >= alt_min) & (alt_deg_arr <= alt_max) & \
          (ys > 0)   # must be in front of mouse

    az_map[hit]  = az_deg_arr[hit]
    alt_map[hit] = alt_deg_arr[hit]
    valid[hit]   = True

    return az_map, alt_map, valid


def compute_forward_map(geo, proj,
                        az_range=(-105, 105), n_az=421,
                        alt_range=None, n_alt=100):
    """
    Forward map: given (azimuth, altitude), which pixel to illuminate?

    Returns:
      az_samples:  (n_az,) array of azimuth values
      alt_samples: (n_alt,) array of altitude values
      px_map:      (n_alt, n_az) pixel x
      py_map:      (n_alt, n_az) pixel y
      valid_map:   (n_alt, n_az) bool
    """
    A = geo['screen']['parabola_A']
    B = geo['screen']['parabola_B']

    if alt_range is None:
        alt_range = (geo['screen']['altitude_min_deg'],
                     geo['screen']['altitude_max_deg'])

    az_samples  = np.linspace(az_range[0],  az_range[1],  n_az)
    alt_samples = np.linspace(alt_range[0], alt_range[1], n_alt)

    px_map    = np.full((n_alt, n_az), np.nan)
    py_map    = np.full((n_alt, n_az), np.nan)
    valid_map = np.zeros((n_alt, n_az), dtype=bool)

    for j, alt in enumerate(alt_samples):
        for i, az in enumerate(az_samples):
            pt = ray_screen_intersection(az, alt, A, B)
            if pt is None:
                continue
            pix = screen_point_to_pixel(pt, proj)
            if pix is None:
                continue
            px, py = pix
            res_w, res_h = proj['res']
            if 0 <= px < res_w and 0 <= py < res_h:
                px_map[j, i]    = px
                py_map[j, i]    = py
                valid_map[j, i] = True

    return az_samples, alt_samples, px_map, py_map, valid_map


# ── Luminance correction ───────────────────────────────────────────────────────

def compute_theoretical_luminance_correction(geo, az_samples):
    """
    Theoretical luminance correction based on screen geometry.

    Two effects:
    1. Cosine falloff: luminance ∝ cos(angle of incidence on screen)
    2. Foreshortening: pixel covers more screen area at oblique angles

    Returns normalized gain (1.0 at az=0) for each azimuth sample.
    Apply as: corrected_contrast = desired_contrast / gain
    """
    A = geo['screen']['parabola_A']
    B = geo['screen']['parabola_B']

    gains = np.zeros_like(az_samples)
    for i, az_deg in enumerate(az_samples):
        az = np.radians(az_deg)
        # Screen intersection at this azimuth (mid altitude)
        alt_mid = np.radians((geo['screen']['altitude_min_deg'] +
                               geo['screen']['altitude_max_deg']) / 2)
        pt = ray_screen_intersection(az_deg, np.degrees(alt_mid), A, B)
        if pt is None:
            continue
        x_s, y_s, z_s = pt

        # Screen surface normal at (x_s, y_s) on parabola y = A - B*x²
        # dy/dx = -2*B*x, so normal (unnormalized) in horiz plane = (2Bx, 1)
        nx = 2 * B * x_s
        ny = 1.0
        nz = 0.0
        n = np.array([nx, ny, nz])
        n = n / np.linalg.norm(n)

        # Projector direction to screen point
        proj_vec = np.array(pt) - np.array([0, 0, 0])  # approx: from mouse eye
        proj_vec = proj_vec / np.linalg.norm(proj_vec)

        # Cosine of incidence angle
        cos_inc = abs(np.dot(n, proj_vec))
        gains[i] = cos_inc

    # Normalize so gain = 1.0 at az = 0
    center_idx = np.argmin(np.abs(az_samples))
    if gains[center_idx] > 0:
        gains = gains / gains[center_idx]

    return gains


# ── Main ───────────────────────────────────────────────────────────────────────

def main(validate=False, geo_path=None):
    print(f"Loading rig geometry from {geo_path or GEO_FILE}...")
    geo = load_geometry(geo_path or GEO_FILE)

    print("Building projector model...")
    proj = build_projector(geo)
    print(f"  Projector position: ({proj['pos'][0]:.2f}, {proj['pos'][1]:.2f}, {proj['pos'][2]:.2f}) cm")
    print(f"  Image size: {proj['img_w']:.1f} × {proj['img_h']:.1f} cm")

    print("Computing inverse map (pixel → visual angle)...")
    az_map, alt_map, valid_map = compute_inverse_map(geo, proj)
    coverage = valid_map.sum() / valid_map.size * 100
    print(f"  Screen coverage: {coverage:.1f}% of pixels hit the screen")

    print("Computing forward map (visual angle → pixel)...")
    az_samples, alt_samples, px_map, py_map, fwd_valid = compute_forward_map(geo, proj)
    fwd_coverage = fwd_valid.sum() / fwd_valid.size * 100
    print(f"  Forward map coverage: {fwd_coverage:.1f}%")

    print("Computing theoretical luminance correction...")
    az_sym = np.linspace(0, 105, 53)
    lum_gain = compute_theoretical_luminance_correction(geo, az_sym)

    # Save. Carry the calibration orientation (flip/offset/frame) so the runtime renderer
    # reproduces exactly what was tuned in calib_geo (the maps themselves stay model-space).
    cal = geo.get('calibration', {})
    out_path = CAL_DIR / "warp_map.npz"
    np.savez(out_path,
             az_map=az_map,
             alt_map=alt_map,
             valid_map=valid_map,
             px_from_az=px_map,
             py_from_az=py_map,
             az_samples=az_samples,
             alt_samples=alt_samples,
             lum_az=az_sym,
             lum_gain_theoretical=lum_gain,
             flip_h=bool(cal.get('flip_h', False)),
             flip_v=bool(cal.get('flip_v', False)),
             offset_x=int(cal.get('offset_x', 0)),
             offset_y=int(cal.get('offset_y', 0)),
             frame_x=int(cal.get('frame_x', 0)),
             frame_y=int(cal.get('frame_y', 0)))
    print(f"\nSaved: {out_path}")

    if validate:
        _plot_validation(az_map, alt_map, valid_map,
                         az_samples, alt_samples, px_map, py_map,
                         az_sym, lum_gain, geo, proj)


def _plot_validation(az_map, alt_map, valid_map,
                     az_samples, alt_samples, px_map, py_map,
                     az_sym, lum_gain, geo, proj):
    import matplotlib.pyplot as plt
    fig, axes = plt.subplots(2, 2, figsize=(14, 10))
    fig.suptitle("Warp Map Validation", fontsize=14, fontweight='bold')

    res_w, res_h = proj['res']

    # 1. Azimuth map
    ax = axes[0, 0]
    im = ax.imshow(az_map, origin='upper', cmap='RdBu',
                   vmin=-105, vmax=105, aspect='auto')
    ax.set_title("Azimuth map (degrees)")
    plt.colorbar(im, ax=ax)
    ax.set_xlabel("Pixel X"); ax.set_ylabel("Pixel Y")

    # 2. Altitude map
    ax = axes[0, 1]
    im = ax.imshow(alt_map, origin='upper', cmap='viridis', aspect='auto')
    ax.set_title("Altitude map (degrees)")
    plt.colorbar(im, ax=ax)
    ax.set_xlabel("Pixel X"); ax.set_ylabel("Pixel Y")

    # 3. Forward map: where do azimuth isolines land?
    ax = axes[1, 0]
    ax.set_xlim(0, res_w); ax.set_ylim(res_h, 0)
    ax.set_title("Forward map — azimuth/altitude grid")
    ax.set_facecolor('black')
    target_azs = [-80, -40, 0, 40, 80]
    colors = ['red', 'orange', 'white', 'orange', 'red']
    for target_az, col in zip(target_azs, colors):
        idx = np.argmin(np.abs(az_samples - target_az))
        valid_row = fwd_valid[:, idx]
        if valid_row.any():
            ax.plot(px_map[valid_row, idx], py_map[valid_row, idx],
                    '-', color=col, linewidth=1.5, label=f"{target_az}°")
    ax.legend(fontsize=8, loc='upper right')
    ax.set_xlabel("Pixel X"); ax.set_ylabel("Pixel Y")

    # 4. Luminance correction
    ax = axes[1, 1]
    ax.plot(az_sym, lum_gain, 'b-o', label='Theoretical', linewidth=2)
    ax.plot(-az_sym, lum_gain, 'b-o')
    ax.axhline(1.0, color='gray', linestyle='--', alpha=0.5)
    ax.set_xlabel("Azimuth (degrees)")
    ax.set_ylabel("Relative luminance")
    ax.set_title("Luminance correction (theoretical)")
    ax.legend()
    ax.grid(True, alpha=0.3)

    plt.tight_layout()
    out = CAL_DIR / "warp_map_validation.png"
    plt.savefig(out, dpi=120, bbox_inches='tight')
    print(f"Validation plot saved: {out}")
    plt.show()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Compute parabolic screen warp map")
    parser.add_argument('--geo', default=None,
                        help='geometry YAML to build from (default: ./rig_geometry.yaml)')
    parser.add_argument('--validate', action='store_true',
                        help='Show validation plots after computing')
    args = parser.parse_args()
    main(validate=args.validate, geo_path=args.geo)
