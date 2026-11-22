"""Static map and spatial queries for CtrlAltWin v1.

Loads arena/v2/world.json and terrain.i16 from hello.map.dir once per match and
precomputes a coarse navigation grid.  All queries are conservative: when the
model is unsure it reports "blocked", because a wasted detour is cheaper than
walking into a building.

The server remains authoritative.  This module never writes to disk.
"""
from __future__ import annotations

import array
import heapq
import json
import math
import os
import sys
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

Point3 = Tuple[float, float, float]
Point2 = Tuple[float, float]


def _cross(ax: float, ay: float, bx: float, by: float) -> float:
    return ax * by - ay * bx


def _point_in_ring(x: float, y: float, ring: Sequence[float]) -> bool:
    """Even-odd point in polygon. ring is flat [x0, y0, x1, y1, ...]."""
    n = len(ring) // 2
    if n < 3:
        return False
    inside = False
    j = n - 1
    for i in range(n):
        xi = ring[2 * i]
        yi = ring[2 * i + 1]
        xj = ring[2 * j]
        yj = ring[2 * j + 1]
        if (yi > y) != (yj > y):
            xin = (xj - xi) * (y - yi) / (yj - yi) + xi
            if x < xin:
                inside = not inside
        j = i
    return inside


def _segment_edge_t(p0: Point2, p1: Point2, q0: Point2, q1: Point2) -> Optional[float]:
    rx = p1[0] - p0[0]
    ry = p1[1] - p0[1]
    sx = q1[0] - q0[0]
    sy = q1[1] - q0[1]
    den = _cross(rx, ry, sx, sy)
    if abs(den) < 1e-12:
        return None
    qpx = q0[0] - p0[0]
    qpy = q0[1] - p0[1]
    t = _cross(qpx, qpy, sx, sy) / den
    u = _cross(qpx, qpy, rx, ry) / den
    if -1e-9 <= t <= 1.0 + 1e-9 and -1e-9 <= u <= 1.0 + 1e-9:
        return min(1.0, max(0.0, t))
    return None


def _ray_edge_t(px: float, py: float, rx: float, ry: float,
                qx0: float, qy0: float, qx1: float, qy1: float) -> Optional[float]:
    """Allocation-free segment/edge intersection used by line of sight."""
    sx = qx1 - qx0
    sy = qy1 - qy0
    den = rx * sy - ry * sx
    if abs(den) < 1e-12:
        return None
    qpx = qx0 - px
    qpy = qy0 - py
    t = (qpx * sy - qpy * sx) / den
    if t < -1e-9 or t > 1.0 + 1e-9:
        return None
    u = (qpx * ry - qpy * rx) / den
    if u < -1e-9 or u > 1.0 + 1e-9:
        return None
    return min(1.0, max(0.0, t))


class WorldModel:
    """Map, terrain, line-of-sight and a bounded A* navigation grid."""

    CELL = 3.0
    BUCKET = 24.0

    def __init__(self, hello: dict, log=None) -> None:
        cfg = hello.get("config", {}) or {}
        mech = cfg.get("mech", {}) or {}
        self.config = cfg
        self.step_up = float(mech.get("step_up", 1.2))
        self.max_slope = math.radians(float(mech.get("max_slope_deg", 50.0)))
        self.mech_height = float(mech.get("height", 9.0))
        self.mech_radius = float(mech.get("radius", 2.5))
        self.eye_height = float(mech.get("eye_height", 8.0))
        self.log = log or (lambda *a: None)

        self.available = False
        self.bounds = (-512.0, 555.0, -333.0, 389.0)
        self.prisms: List[dict] = []
        self.trees: List[dict] = []
        self._buckets: Dict[Tuple[int, int], List[dict]] = {}
        self._terrain = None
        self._tnx = self._tny = 0
        self._tminx = self._tminy = 0.0
        self._tstep = 1.0
        self.cell = self.CELL
        self.nx = self.ny = 0
        self.minx = self.miny = 0.0
        self.cell_h: List[float] = []
        self.cell_x: List[float] = []
        self.cell_y: List[float] = []
        self.cell_blocked = bytearray()
        self.cover_cells: List[int] = []
        self.explore_cells: List[int] = []
        self.validation = (True, "not loaded")

        try:
            self._load(hello)
            self.available = True
            self.validation = (True, "ok")
        except Exception as exc:  # a missing map must not crash the mech
            self.available = False
            self.validation = (False, f"{type(exc).__name__}: {exc}")
            self.log(f"world model unavailable: {self.validation[1]}")

    @staticmethod
    def _row_intervals(y: float, ring: Sequence[float]) -> List[Tuple[float, float]]:
        xs: List[float] = []
        n = len(ring) // 2
        j = n - 1
        for i in range(n):
            xi = ring[2 * i]
            yi = ring[2 * i + 1]
            xj = ring[2 * j]
            yj = ring[2 * j + 1]
            if (yi > y) != (yj > y):
                xs.append((xj - xi) * (y - yi) / (yj - yi) + xi)
            j = i
        xs.sort()
        return [(xs[k], xs[k + 1]) for k in range(0, len(xs) - 1, 2)]

    @staticmethod
    def _in_intervals(x: float, iv: List[Tuple[float, float]]) -> bool:
        for a, b in iv:
            if a <= x <= b:
                return True
        return False

    # ------------------------------------------------------------------ loading
    def _load(self, hello: dict) -> None:
        m = hello.get("map", {}) or {}
        base = str(m.get("dir", "") or "")
        files = list(m.get("files") or [])
        candidates: List[str] = []
        for f in files:
            candidates.append(os.path.join(base, str(f)))
        candidates += [
            os.path.join(base, "world.json"),
            os.path.join(base, "v2", "world.json"),
        ]
        world_path = next((p for p in candidates if os.path.isfile(p)), None)
        if world_path is None:
            raise FileNotFoundError(f"world.json not found under {base!r}")
        with open(world_path, "r", encoding="utf-8") as f:
            world = json.load(f)

        b = world.get("bounds") or {}
        self.bounds = (
            float(b.get("minX", self.bounds[0])),
            float(b.get("maxX", self.bounds[1])),
            float(b.get("minY", self.bounds[2])),
            float(b.get("maxY", self.bounds[3])),
        )

        for i, p in enumerate(world.get("prisms") or []):
            ring = [float(v) for v in p.get("ring") or []]
            if len(ring) < 6:
                continue
            holes = [[float(v) for v in h] for h in (p.get("holes") or []) if len(h) >= 6]
            xs = ring[0::2]
            ys = ring[1::2]

            def edges_of(r):
                n = len(r) // 2
                return [(r[2 * k], r[2 * k + 1], r[2 * ((k + 1) % n)], r[2 * ((k + 1) % n) + 1])
                        for k in range(n)]

            self.prisms.append({
                "idx": i,
                "kind": str(p.get("kind", "building")),
                "base": float(p.get("base", 0.0)),
                "top": float(p.get("top", 0.0)),
                "ring": ring,
                "holes": holes,
                "edges": edges_of(ring),
                "hole_edges": [edges_of(h) for h in holes],
                "minx": min(xs), "maxx": max(xs),
                "miny": min(ys), "maxy": max(ys),
            })

        trees = world.get("trees") or {}
        fields = list(trees.get("fields") or [])
        data = trees.get("data") or []
        if fields and data:
            idx = {name: i for i, name in enumerate(fields)}
            stride = len(fields)
            for off in range(0, len(data), stride):
                row = data[off:off + stride]
                if len(row) < stride:
                    break
                try:
                    self.trees.append({
                        "x": float(row[idx["x"]]),
                        "y": float(row[idx["y"]]),
                        "z": float(row[idx["z"]]),
                        "height": float(row[idx["height"]]),
                        "crownR": float(row[idx["crownR"]]) if "crownR" in idx else 0.0,
                        "trunkR": float(row[idx["trunkR"]]) if "trunkR" in idx else 0.0,
                    })
                except (KeyError, TypeError, ValueError):
                    break

        terrain = world.get("terrain") or {}
        tfile = str(terrain.get("file", "terrain.i16"))
        tpath = os.path.join(os.path.dirname(world_path), tfile)
        if not os.path.isfile(tpath):
            tpath = os.path.join(base, "v2", tfile)
        self._tnx = int(terrain.get("nx", 0))
        self._tny = int(terrain.get("ny", 0))
        self._tminx = float(terrain.get("minX", 0.0))
        self._tminy = float(terrain.get("minY", 0.0))
        self._tstep = float(terrain.get("step", 1.0))
        with open(tpath, "rb") as f:
            raw = f.read()
        heights = array.array("h")
        heights.frombytes(raw[: self._tnx * self._tny * 2])
        if sys.byteorder != "little":
            heights.byteswap()
        if len(heights) != self._tnx * self._tny:
            raise ValueError(f"terrain has {len(heights)} samples, expected {self._tnx * self._tny}")
        self._terrain = heights

        self._build_buckets()
        self._build_grid()
        self._build_cover_cells()

    def _build_buckets(self) -> None:
        self._buckets = {}
        for p in self.prisms:
            i0 = int(math.floor((p["minx"] - self.bounds[0]) / self.BUCKET))
            i1 = int(math.floor((p["maxx"] - self.bounds[0]) / self.BUCKET))
            j0 = int(math.floor((p["miny"] - self.bounds[2]) / self.BUCKET))
            j1 = int(math.floor((p["maxy"] - self.bounds[2]) / self.BUCKET))
            for i in range(i0, i1 + 1):
                for j in range(j0, j1 + 1):
                    self._buckets.setdefault((i, j), []).append(p)

    def _build_grid(self) -> None:
        minx, maxx, miny, maxy = self.bounds
        self.minx, self.miny = minx, miny
        self.cell = self.CELL
        self.nx = max(1, int((maxx - minx) / self.cell) + 1)
        self.ny = max(1, int((maxy - miny) / self.cell) + 1)
        n = self.nx * self.ny
        self.cell_h = [0.0] * n
        self.cell_x = [minx + (i + 0.5) * self.cell for _j in range(self.ny) for i in range(self.nx)]
        self.cell_y = [miny + (j + 0.5) * self.cell for j in range(self.ny) for _i in range(self.nx)]
        self.cell_blocked = bytearray(n)

        spans: Dict[int, List[Tuple[float, float]]] = {}
        cell = self.cell
        nx = self.nx
        for p in self.prisms:
            i0 = max(0, int((p["minx"] - minx) / cell))
            i1 = min(nx - 1, int((p["maxx"] - minx) / cell) + 1)
            j0 = max(0, int((p["miny"] - miny) / cell))
            j1 = min(self.ny - 1, int((p["maxy"] - miny) / cell) + 1)
            ring = p["ring"]
            holes = p["holes"]
            for j in range(j0, j1 + 1):
                y = miny + (j + 0.5) * cell
                row = self._row_intervals(y, ring)
                if not row:
                    continue
                hole_rows = [self._row_intervals(y, h) for h in holes]
                base_k = j * nx
                for i in range(i0, i1 + 1):
                    x = minx + (i + 0.5) * cell
                    if not self._in_intervals(x, row):
                        continue
                    if any(self._in_intervals(x, hr) for hr in hole_rows):
                        continue
                    spans.setdefault(base_k + i, []).append((p["base"], p["top"]))

        height = self.cell_h
        blocked = self.cell_blocked
        margin = 2.0
        step = self.step_up
        head = self.mech_height
        for j in range(self.ny):
            y = miny + (j + 0.5) * cell
            for i in range(self.nx):
                k = j * self.nx + i
                x = minx + (i + 0.5) * cell
                h = self.terrain_height(x, y)
                surface = h
                is_blocked = False
                sp = spans.get(k)
                if sp:
                    for base, top in sorted(sp):
                        if top <= surface + step + 0.05:
                            if top > surface:
                                surface = top
                            continue
                        if base >= surface + head - 1e-6:
                            continue  # bridge / overhang clears the head
                        is_blocked = True
                        break
                if x < minx + margin or x > maxx - margin or y < miny + margin or y > maxy - margin:
                    is_blocked = True
                height[k] = surface
                blocked[k] = 1 if is_blocked else 0

        # Terrain steeper than the server's slope limit is not walkable.
        slope_limit = math.tan(self.max_slope) * cell * 1.08
        limit = max(step * 1.25, slope_limit)
        for j in range(1, self.ny - 1):
            for i in range(1, self.nx - 1):
                k = j * self.nx + i
                if blocked[k]:
                    continue
                h = height[k]
                if (abs(height[k + 1] - h) > limit or abs(height[k - 1] - h) > limit or
                        abs(height[k + self.nx] - h) > limit or abs(height[k - self.nx] - h) > limit):
                    blocked[k] = 1

    def _build_cover_cells(self) -> None:
        nx, ny = self.nx, self.ny
        blocked = self.cell_blocked
        # 2-D prefix sums give the blocked count in any window in O(1).
        pref = [0] * ((nx + 1) * (ny + 1))
        w = nx + 1
        for j in range(ny):
            row_acc = 0
            src = j * nx
            dst = (j + 1) * w
            up = j * w
            for i in range(nx):
                row_acc += blocked[src + i]
                pref[dst + i + 1] = pref[up + i + 1] + row_acc
        cover: List[int] = []
        open_cells: List[int] = []
        for j in range(3, ny - 3):
            base = j * nx
            for i in range(3, nx - 3):
                k = base + i
                if blocked[k]:
                    continue
                if (k + 1) % 4 == 0:
                    open_cells.append(k)
                j0, j1 = j - 2, j + 3
                i0, i1 = i - 2, i + 3
                cnt = (pref[j1 * w + i1] - pref[j0 * w + i1]
                       - pref[j1 * w + i0] + pref[j0 * w + i0])
                if cnt > 0:
                    cover.append(k)
        stride = max(1, len(cover) // 5000)
        self.cover_cells = cover[::stride]
        stride = max(1, len(open_cells) // 2500)
        self.explore_cells = open_cells[::stride]

    # ------------------------------------------------------------------ terrain
    def terrain_height(self, x: float, y: float) -> float:
        t = self._terrain
        if t is None:
            return 0.0
        fx = (x - self._tminx) / self._tstep
        fy = (y - self._tminy) / self._tstep
        i = int(fx)
        j = int(fy)
        nx = self._tnx
        ny = self._tny
        if i < 0:
            i = 0
        elif i > nx - 2:
            i = nx - 2
        if j < 0:
            j = 0
        elif j > ny - 2:
            j = ny - 2
        tx = fx - i
        ty = fy - j
        if tx < 0.0:
            tx = 0.0
        elif tx > 1.0:
            tx = 1.0
        if ty < 0.0:
            ty = 0.0
        elif ty > 1.0:
            ty = 1.0
        k = j * nx + i
        k2 = k + nx
        h00 = t[k] * 0.01
        h10 = t[k + 1] * 0.01
        h01 = t[k2] * 0.01
        h11 = t[k2 + 1] * 0.01
        return (h00 + (h10 - h00) * tx) * (1.0 - ty) + (h01 + (h11 - h01) * tx) * ty

    # ------------------------------------------------------------------ prisms
    @staticmethod
    def _in_prism_footprint(p: dict, x: float, y: float) -> bool:
        if x < p["minx"] or x > p["maxx"] or y < p["miny"] or y > p["maxy"]:
            return False
        if not _point_in_ring(x, y, p["ring"]):
            return False
        for h in p["holes"]:
            if _point_in_ring(x, y, h):
                return False
        return True

    def _prisms_at(self, x: float, y: float) -> Iterable[dict]:
        key = (int(math.floor((x - self.bounds[0]) / self.BUCKET)),
               int(math.floor((y - self.bounds[2]) / self.BUCKET)))
        return self._buckets.get(key, ())

    def point_solid(self, x: float, y: float, z: float) -> bool:
        if not self.available:
            return False
        if z < self.terrain_height(x, y):
            return True
        for p in self._prisms_at(x, y):
            if z < p["base"] or z > p["top"]:
                continue
            if self._in_prism_footprint(p, x, y):
                return True
        return False

    def point_free(self, x: float, y: float) -> bool:
        """Ground-level walking check at an arbitrary point (exact geometry)."""
        if not self.available:
            return True
        minx, maxx, miny, maxy = self.bounds
        if not (minx + 1.0 <= x <= maxx - 1.0 and miny + 1.0 <= y <= maxy - 1.0):
            return False
        ground = self.terrain_height(x, y)
        for p in self._prisms_at(x, y):
            if p["top"] <= ground + self.step_up + 0.05:
                continue  # a low slab you step onto
            if p["base"] >= ground + self.mech_height - 1e-6:
                continue  # walk under it
            if self._in_prism_footprint(p, x, y):
                return False
        return True

    def cell_index(self, x: float, y: float) -> int:
        i = int((x - self.minx) / self.cell)
        j = int((y - self.miny) / self.cell)
        if i < 0:
            i = 0
        elif i >= self.nx:
            i = self.nx - 1
        if j < 0:
            j = 0
        elif j >= self.ny:
            j = self.ny - 1
        return j * self.nx + i

    def cell_center(self, k: int) -> Point2:
        j, i = divmod(k, self.nx)
        return (self.minx + (i + 0.5) * self.cell, self.miny + (j + 0.5) * self.cell)

    def walkable(self, k: int) -> bool:
        if not self.available:
            return True
        return not self.cell_blocked[k]

    def nearest_walkable(self, k: int, radius: int = 10) -> Optional[int]:
        if not self.available:
            return k
        if self.walkable(k):
            return k
        j0, i0 = divmod(k, self.nx)
        best = None
        best_d = 1e9
        for r in range(1, radius + 1):
            for dj in range(-r, r + 1):
                j = j0 + dj
                if j < 0 or j >= self.ny:
                    continue
                edge = abs(dj) == r
                for di in range(-r, r + 1):
                    if not edge and abs(di) != r:
                        continue
                    i = i0 + di
                    if i < 0 or i >= self.nx:
                        continue
                    kk = j * self.nx + i
                    if not self.cell_blocked[kk]:
                        d = di * di + dj * dj
                        if d < best_d:
                            best_d = d
                            best = kk
            if best is not None:
                return best
        return best

    def nearest_walkable_point(self, x: float, y: float) -> Point2:
        k = self.nearest_walkable(self.cell_index(x, y))
        if k is None:
            return (x, y)
        return self.cell_center(k)

    # ------------------------------------------------------------------ walking lines
    def segment_walkable(self, a: Point2, b: Point2) -> bool:
        if not self.available:
            return True
        dx = b[0] - a[0]
        dy = b[1] - a[1]
        d = math.hypot(dx, dy)
        if d < 1e-6:
            return self.point_free(a[0], a[1])
        steps = max(1, int(d / 0.75))
        for s in range(steps + 1):
            t = s / steps
            x = a[0] + dx * t
            y = a[1] + dy * t
            if self.cell_blocked[self.cell_index(x, y)]:
                return False
            if not self.point_free(x, y):
                return False
        return True

    # ------------------------------------------------------------------ line of sight
    def los_blocked(self, a: Point3, b: Point3) -> bool:
        """True if terrain or a prism blocks the 3-D segment a->b."""
        if not self.available:
            return False
        ax, ay, az = a
        bx, by, bz = b
        zmin, zmax = (az, bz) if az <= bz else (bz, az)
        for p in self._segment_candidates(a, b):
            if zmax < p["base"] or zmin > p["top"]:
                continue
            if self._segment_prism_blocked(ax, ay, az, bx, by, bz, p):
                return True
        # Terrain.  Near shots get dense samples; long sightlines can take
        # larger steps because the height field is smooth at that scale.
        d = math.hypot(bx - ax, by - ay)
        steps = max(1, int(d / (2.0 if d <= 50.0 else 3.0)))
        for s in range(1, steps):
            t = s / steps
            x = ax + (bx - ax) * t
            y = ay + (by - ay) * t
            z = az + (bz - az) * t
            if z < self.terrain_height(x, y) + 0.04:
                return True
        return False

    def los_blocked_fast(self, a: Point3, b: Point3) -> bool:
        """Approximate sightline for threat scoring: walks the 3 m raster.

        Uses the precomputed walkability/height grid, so it is far cheaper than
        the exact prism test.  It is conservative (a cell the mech cannot stand
        on counts as blocking), which is fine for scoring; firing and cover
        selection still use the exact `los_blocked`.
        """
        if not self.available:
            return False
        ax, ay, az = a
        bx, by, bz = b
        dx = bx - ax
        dy = by - ay
        dz = bz - az
        d = math.hypot(dx, dy)
        steps = max(1, int(d / 3.0))
        cells = self.cell_blocked
        heights = self.cell_h
        nx = self.nx
        ny = self.ny
        minx = self.minx
        miny = self.miny
        cell = self.cell
        for s in range(1, steps):
            t = s / steps
            i = int((ax + dx * t - minx) / cell)
            j = int((ay + dy * t - miny) / cell)
            if i < 0:
                i = 0
            elif i >= nx:
                i = nx - 1
            if j < 0:
                j = 0
            elif j >= ny:
                j = ny - 1
            k = j * nx + i
            if cells[k]:
                return True
            if az + dz * t < heights[k] + 0.25:
                return True
        return False

    def _segment_candidates(self, a: Point3, b: Point3) -> List[dict]:
        ax, ay = a[0], a[1]
        bx, by = b[0], b[1]
        dx = bx - ax
        dy = by - ay
        d = math.hypot(dx, dy)
        keys: Set[Tuple[int, int]] = set()
        steps = max(1, int(d / self.BUCKET) + 1)
        for s in range(steps + 1):
            t = s / steps
            x = ax + dx * t
            y = ay + dy * t
            bi = int(math.floor((x - self.bounds[0]) / self.BUCKET))
            bj = int(math.floor((y - self.bounds[2]) / self.BUCKET))
            for di in (-1, 0, 1):
                for dj in (-1, 0, 1):
                    keys.add((bi + di, bj + dj))
        out: List[dict] = []
        seen: Set[int] = set()
        lo_x, hi_x = min(ax, bx), max(ax, bx)
        lo_y, hi_y = min(ay, by), max(ay, by)
        for key in keys:
            for p in self._buckets.get(key, ()):
                pid = p["idx"]
                if pid in seen:
                    continue
                if p["maxx"] < lo_x or p["minx"] > hi_x or p["maxy"] < lo_y or p["miny"] > hi_y:
                    continue
                seen.add(pid)
                out.append(p)
        return out

    def _segment_prism_blocked(self, ax: float, ay: float, az: float,
                               bx: float, by: float, bz: float, p: dict) -> bool:
        dx = bx - ax
        dy = by - ay
        dz = bz - az
        t0 = 0.0
        t1 = 1.0
        if dx == 0.0:
            if ax < p["minx"] or ax > p["maxx"]:
                return False
        else:
            ta = (p["minx"] - ax) / dx
            tb = (p["maxx"] - ax) / dx
            if ta > tb:
                ta, tb = tb, ta
            if ta > t0:
                t0 = ta
            if tb < t1:
                t1 = tb
            if t0 > t1:
                return False
        if dy == 0.0:
            if ay < p["miny"] or ay > p["maxy"]:
                return False
        else:
            ta = (p["miny"] - ay) / dy
            tb = (p["maxy"] - ay) / dy
            if ta > tb:
                ta, tb = tb, ta
            if ta > t0:
                t0 = ta
            if tb < t1:
                t1 = tb
            if t0 > t1:
                return False
        base = p["base"]
        top = p["top"]
        if dz == 0.0:
            if az < base or az > top:
                return False
        else:
            ta = (base - az) / dz
            tb = (top - az) / dz
            if ta > tb:
                ta, tb = tb, ta
            if ta > t0:
                t0 = ta
            if tb < t1:
                t1 = tb
            if t0 > t1:
                return False
        ts: List[float] = [t0, t1]
        for e in p["edges"]:
            t = _ray_edge_t(ax, ay, dx, dy, e[0], e[1], e[2], e[3])
            if t is not None and t0 < t < t1:
                ts.append(t)
        for edges in p["hole_edges"]:
            for e in edges:
                t = _ray_edge_t(ax, ay, dx, dy, e[0], e[1], e[2], e[3])
                if t is not None and t0 < t < t1:
                    ts.append(t)
        ts.sort()
        ring = p["ring"]
        holes = p["holes"]
        for i in range(len(ts) - 1):
            tm = (ts[i] + ts[i + 1]) * 0.5
            x = ax + dx * tm
            y = ay + dy * tm
            if not _point_in_ring(x, y, ring):
                continue
            if any(_point_in_ring(x, y, h) for h in holes):
                continue
            return True
        return False

    # ------------------------------------------------------------------ path finding
    def find_path(self, start: Point2, goal: Point2, danger: Optional[Dict[int, float]] = None,
                  zone: Optional[Tuple[float, float, float]] = None,
                  max_expand: int = 3000, avoid: Optional[Dict[int, float]] = None,
                  now: float = 0.0) -> List[Point2]:
        """Bounded A* over the 3 m grid. Returns world points, or [] when unreachable."""
        if not self.available:
            return [goal]
        sk = self.nearest_walkable(self.cell_index(*start))
        gk = self.nearest_walkable(self.cell_index(*goal), radius=12)
        if sk is None or gk is None:
            return []
        if sk == gk:
            return [goal]
        nx = self.nx
        ny = self.ny
        n = nx * ny
        gx, gy = divmod(gk, nx)
        blocked = self.cell_blocked
        height = self.cell_h
        cell_x = self.cell_x
        cell_y = self.cell_y
        slope = max(self.step_up * 1.25, math.tan(self.max_slope) * self.cell * 1.08)
        inf = 1e18
        gscore = [inf] * n
        came = [-1] * n
        closed = bytearray(n)
        gscore[sk] = 0.0

        open_heap: List[Tuple[float, int]] = [(0.0, sk)]
        best = sk
        best_h = inf
        expanded = 0
        sqrt2 = 1.41421356
        if zone is not None:
            zcx, zcy, zr = zone
            zr2 = max(0.0, zr - 3.0) ** 2
        while open_heap and expanded < max_expand:
            _f, k = heapq.heappop(open_heap)
            if closed[k]:
                continue
            closed[k] = 1
            expanded += 1
            j = k // nx
            i = k - j * nx
            ax = abs(i - gx)
            ay = abs(j - gy)
            hh = ax + ay + (sqrt2 - 2.0) * (ax if ax < ay else ay)
            if hh < best_h:
                best_h = hh
                best = k
            if k == gk:
                best = k
                break
            base_g = gscore[k]
            hk = height[k]
            for dj in (-1, 0, 1):
                nj = j + dj
                if nj < 0 or nj >= ny:
                    continue
                row = nj * nx
                for di in (-1, 0, 1):
                    if di == 0 and dj == 0:
                        continue
                    ni = i + di
                    if ni < 0 or ni >= nx:
                        continue
                    nk = row + ni
                    if blocked[nk] or closed[nk]:
                        continue
                    if abs(height[nk] - hk) > slope:
                        continue
                    if di and dj:
                        if blocked[j * nx + ni] or blocked[nj * nx + i]:
                            continue
                        step = sqrt2
                    else:
                        step = 1.0
                    if danger:
                        dval = danger.get(nk)
                        if dval:
                            step *= 1.0 + dval
                    if avoid:
                        av = avoid.get(nk)
                        if av is not None and av > now:
                            step *= 4.0
                    if zone is not None:
                        px = cell_x[nk] - zcx
                        py = cell_y[nk] - zcy
                        if px * px + py * py > zr2:
                            step += 6.0
                    tentative = base_g + step
                    if tentative < gscore[nk]:
                        gscore[nk] = tentative
                        came[nk] = k
                        # Weighted A*: a slightly longer route is fine, and the
                        # reduced expansion count keeps the 50 ms budget safe.
                        heapq.heappush(open_heap, (tentative + hh * 1.8, nk))

        if best != sk and came[best] < 0:
            best = sk
        cells: List[int] = [best]
        cur = best
        guard = 0
        while cur != sk and guard < 5000:
            cur = came[cur]
            if cur < 0:
                break
            cells.append(cur)
            guard += 1
        cells.reverse()
        pts = [(cell_x[k], cell_y[k]) for k in cells]
        if pts and math.hypot(pts[-1][0] - goal[0], pts[-1][1] - goal[1]) > self.cell:
            if self.point_free(goal[0], goal[1]):
                pts.append(goal)
        return self._smooth(pts, start)

    def _grid_line_clear(self, a: Point2, b: Point2) -> bool:
        dx = b[0] - a[0]
        dy = b[1] - a[1]
        d = math.hypot(dx, dy)
        if d < 1e-6:
            return True
        steps = max(1, int(d / self.cell))
        for s in range(steps + 1):
            t = s / steps
            if self.cell_blocked[self.cell_index(a[0] + dx * t, a[1] + dy * t)]:
                return False
        return True

    def _smooth(self, pts: List[Point2], start: Point2) -> List[Point2]:
        if len(pts) <= 2:
            return pts
        out: List[Point2] = []
        cur = start
        i = 0
        while i < len(pts):
            far = min(len(pts) - 1, i + 12)
            j = far
            while j > i:
                if self._grid_line_clear(cur, pts[j]):
                    break
                j -= 1
            out.append(pts[j])
            cur = pts[j]
            i = j + 1
        return out

    # ------------------------------------------------------------------ danger fields
    def danger_field(self, points: Iterable[Point2], radius: float = 40.0,
                     weight: float = 3.0) -> Dict[int, float]:
        """Inflate A* costs near threatened points (last known enemy positions)."""
        if not self.available:
            return {}
        danger: Dict[int, float] = {}
        cell = self.cell
        reach = int(radius / cell) + 1
        for x, y in points:
            ci = int((x - self.minx) / cell)
            cj = int((y - self.miny) / cell)
            for dj in range(-reach, reach + 1):
                j = cj + dj
                if j < 0 or j >= self.ny:
                    continue
                for di in range(-reach, reach + 1):
                    i = ci + di
                    if i < 0 or i >= self.nx:
                        continue
                    px = self.minx + (i + 0.5) * cell
                    py = self.miny + (j + 0.5) * cell
                    d = math.hypot(px - x, py - y)
                    if d >= radius:
                        continue
                    val = weight * (1.0 - d / radius)
                    k = j * self.nx + i
                    if val > danger.get(k, 0.0):
                        danger[k] = val
        return danger

    # ------------------------------------------------------------------ misc
    def random_free_point(self, x: float, y: float, radius: float) -> Point2:
        k = self.nearest_walkable(self.cell_index(x, y))
        if k is None:
            return (x, y)
        return self.cell_center(k)
