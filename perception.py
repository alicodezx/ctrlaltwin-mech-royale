"""Observation parsing and short-term situational awareness.

Everything here is derived from the documented observation schema.  The server
already maintains a five-second memory for mechs; we keep our own tracks as a
secondary view and use events, sounds, beams and projectile paths to raise
awareness without inventing enemy identities.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

Vec2 = Tuple[float, float]
Vec3 = Tuple[float, float, float]


def wrap(a: float) -> float:
    while a > math.pi:
        a -= 2.0 * math.pi
    while a < -math.pi:
        a += 2.0 * math.pi
    return a


def angle_between(a: float, b: float) -> float:
    return abs(wrap(a - b))


def bearing(a: Sequence[float], b: Sequence[float]) -> float:
    return math.atan2(b[1] - a[1], b[0] - a[0])


@dataclass
class Track:
    id: int
    is_npc: bool
    pos: Vec3
    vel: Vec3
    aim: Vec2
    hp: float
    max_hp: float
    weapons: Tuple[str, ...]
    charging: bool
    cubes: int
    seen_t: float
    seen_now: bool

    @property
    def hp_frac(self) -> float:
        return max(0.0, min(1.5, self.hp / self.max_hp)) if self.max_hp > 0 else 0.0

    def distance_to(self, p: Sequence[float]) -> float:
        return math.hypot(self.pos[0] - p[0], self.pos[1] - p[1])

    def dist3_to(self, p: Sequence[float]) -> float:
        return math.sqrt((self.pos[0] - p[0]) ** 2 + (self.pos[1] - p[1]) ** 2 + (self.pos[2] - p[2]) ** 2)


@dataclass
class Dodge:
    active: bool = False
    direction: Vec2 = (0.0, 0.0)
    threat: str = ""
    urgent: float = 0.0


@dataclass
class ZoneRisk:
    outside: bool
    dist_edge_current: float
    dist_edge_next: float
    time_left: float
    urgency: float
    boost: bool
    predicted_radius: float


@dataclass
class ZoneInfo:
    center: Vec2
    radius: float
    next_center: Vec2
    next_radius: float
    shrink_start: float
    shrink_end: float
    dps: float
    t: float

    @classmethod
    def from_obs(cls, z: dict, t: float) -> "ZoneInfo":
        c = z.get("center") or [0.0, 0.0]
        n = z.get("next_center") or c
        return cls(
            center=(float(c[0]), float(c[1])),
            radius=float(z.get("radius", 0.0)),
            next_center=(float(n[0]), float(n[1])),
            next_radius=float(z.get("next_radius", 0.0)),
            shrink_start=float(z.get("shrink_start", t)),
            shrink_end=float(z.get("shrink_end", t)),
            dps=float(z.get("dps", 0.0)),
            t=float(t),
        )

    def shrink_progress(self) -> float:
        span = self.shrink_end - self.shrink_start
        if span <= 1e-6:
            return 1.0
        return max(0.0, min(1.0, (self.t - self.shrink_start) / span))

    def predicted_circle(self) -> Tuple[Vec2, float]:
        if self.t <= self.shrink_start:
            return self.center, self.radius
        if self.t >= self.shrink_end:
            return self.next_center, self.next_radius
        f = self.shrink_progress()
        return (
            (self.center[0] + (self.next_center[0] - self.center[0]) * f,
             self.center[1] + (self.next_center[1] - self.center[1]) * f),
            self.radius + (self.next_radius - self.radius) * f,
        )

    def risk(self, pos: Sequence[float], walk_speed: float, boost_speed: float) -> ZoneRisk:
        c, r = self.predicted_circle()
        dc = math.hypot(pos[0] - c[0], pos[1] - c[1]) - r
        dn = math.hypot(pos[0] - self.next_center[0], pos[1] - self.next_center[1]) - self.next_radius
        if self.t < self.shrink_start:
            time_left = self.shrink_start - self.t
        else:
            time_left = max(0.1, self.shrink_end - self.t)
        urgency = 0.0
        boost = False
        if dc > 0.0:
            urgency = 3.0
            boost = True
        elif dc > -8.0:
            urgency = max(urgency, 1.8)
            boost = True
        if dn > 0.0:
            need_speed = (dn + 8.0) / max(0.35, time_left)
            if need_speed > walk_speed * 0.95:
                urgency = max(urgency, 2.6)
                boost = need_speed > walk_speed * 1.02
            elif need_speed > walk_speed * 0.55:
                urgency = max(urgency, 1.4)
            elif self.t < self.shrink_start and (dn + 8.0) > (self.shrink_start - self.t) * walk_speed * 0.55:
                urgency = max(urgency, 0.9)
        if r <= 75.0 and dc > -12.0:
            urgency = max(urgency, 1.6)
        return ZoneRisk(dc > 0.0, dc, dn, time_left, urgency, boost, r)

    def safe_point(self, pos: Sequence[float], margin: float = 8.0) -> Vec2:
        """Nearest point comfortably inside the current (predicted) circle."""
        c, r = self.predicted_circle()
        dx = pos[0] - c[0]
        dy = pos[1] - c[1]
        d = math.hypot(dx, dy)
        if d <= max(1.0, r - margin):
            return (pos[0], pos[1])
        k = max(1.0, r - margin) / max(1e-6, d)
        return (c[0] + dx * k, c[1] + dy * k)

    def next_entry(self, pos: Sequence[float], margin: float = 8.0) -> Vec2:
        """Closest point inside the next circle, from the current position."""
        c, r = self.next_center, self.next_radius
        dx = pos[0] - c[0]
        dy = pos[1] - c[1]
        d = math.hypot(dx, dy)
        if d <= max(1.0, r - margin):
            return (pos[0], pos[1])
        k = max(1.0, r - margin) / max(1e-6, d)
        return (c[0] + dx * k, c[1] + dy * k)


class Perception:
    """One decision's view of the world plus short-lived memory."""

    def __init__(self, config: dict) -> None:
        self.config = config
        self.mech_cfg = config.get("mech", {}) or {}
        self.vision_cfg = config.get("vision", {}) or {}
        self.weapons_cfg = config.get("weapons", {}) or {}
        self.memory_s = float(self.vision_cfg.get("memory_s", 5.0))
        self.tracks: Dict[int, Track] = {}
        self.prev_t: Optional[float] = None
        self.t = 0.0
        self.tick = 0
        self.self_id = -1
        self.alive = 16
        self.pos: Vec3 = (0.0, 0.0, 0.0)
        self.vel: Vec3 = (0.0, 0.0, 0.0)
        self.aim: Vec2 = (0.0, 0.0)
        self.hp = self.max_hp = 1400.0
        self.energy = self.max_energy = 100.0
        self.grounded = True
        self.cubes = 0
        self.in_zone = True
        self.stats: Dict[str, float] = {}
        self.weapons: List[Optional[dict]] = [None, None]
        self.upgrades: List[str] = []
        self.enemies: List[Track] = []
        self.npcs: List[Track] = []
        self.crates: List[dict] = []
        self.cubes_obs: List[dict] = []
        self.pickups: List[dict] = []
        self.drops: List[dict] = []
        self.projectiles: List[dict] = []
        self.sounds: List[dict] = []
        self.beams: List[dict] = []
        self.events: List[dict] = []
        self.offer: Optional[dict] = None
        self.zone: Optional[ZoneInfo] = None
        self.damage_by: Dict[int, List[float]] = {}
        self.last_damage_t = -1e9
        self.last_damage_bearing: Optional[float] = None
        self.last_attacker: Optional[int] = None
        self.sound_bearing: Optional[float] = None
        self.sound_kind: str = ""
        self.sound_t = -1e9
        self.beam_bearing: Optional[float] = None
        self.beam_kind: str = ""
        self.self_prev_pos: Optional[Vec3] = None

    # ------------------------------------------------------------------ update
    def update(self, obs: dict) -> None:
        self.tick = int(obs.get("tick", 0))
        t = float(obs.get("t", 0.0))
        dt = 0.0 if self.prev_t is None else max(0.0, t - self.prev_t)
        self.prev_t = t
        self.t = t
        self.alive = int(obs.get("alive", 16))

        me = obs.get("self") or {}
        self.self_id = int(me.get("id", self.self_id))
        self.self_prev_pos = self.pos
        self.pos = tuple(float(v) for v in (me.get("pos") or (0.0, 0.0, 0.0)))[:3]  # type: ignore[assignment]
        self.vel = tuple(float(v) for v in (me.get("vel") or (0.0, 0.0, 0.0)))[:3]  # type: ignore[assignment]
        aim = me.get("aim") or (0.0, 0.0)
        self.aim = (float(aim[0]), float(aim[1]))
        self.hp = float(me.get("hp", 0.0))
        self.max_hp = float(me.get("max_hp", 1400.0))
        self.energy = float(me.get("energy", 0.0))
        self.max_energy = float(me.get("max_energy", 100.0))
        self.grounded = bool(me.get("grounded", True))
        self.cubes = int(me.get("cubes", 0))
        self.in_zone = bool(me.get("in_zone", True))
        self.stats = {str(k): float(v) for k, v in (me.get("stats") or {}).items()}
        self.weapons = list(me.get("weapons") or [None, None])[:2]
        while len(self.weapons) < 2:
            self.weapons.append(None)
        self.upgrades = [str(v) for v in (me.get("upgrades") or [])]

        for by, val in list(self.damage_by.items()):
            val[0] *= math.exp(-dt / 7.0)
            if val[0] < 0.02:
                del self.damage_by[by]

        seen: Dict[int, Track] = {}
        enemies: List[Track] = []
        npcs: List[Track] = []
        for m in obs.get("mechs") or []:
            try:
                mid = int(m.get("id", -1))
                if mid < 0:
                    continue
                is_npc = m.get("kind") == "npc"
                pos = tuple(float(v) for v in m.get("pos", (0.0, 0.0, 0.0)))[:3]
                vel = tuple(float(v) for v in m.get("vel", (0.0, 0.0, 0.0)))[:3]
                a = m.get("aim") or (0.0, 0.0)
                tr = Track(
                    id=mid,
                    is_npc=is_npc,
                    pos=pos,  # type: ignore[arg-type]
                    vel=vel,  # type: ignore[arg-type]
                    aim=(float(a[0]), float(a[1])),
                    hp=float(m.get("hp", 0.0)),
                    max_hp=float(m.get("max_hp", 1.0)),
                    weapons=tuple(str(w) for w in (m.get("weapons") or [])),
                    charging=bool(m.get("charging", False)),
                    cubes=int(m.get("cubes", 0)),
                    seen_t=t,
                    seen_now=True,
                )
            except (TypeError, ValueError):
                continue
            seen[mid] = tr
            (npcs if is_npc else enemies).append(tr)
        for mid, tr in self.tracks.items():
            if mid in seen:
                continue
            if t - tr.seen_t <= self.memory_s:
                tr.seen_now = False
                seen[mid] = tr
                (npcs if tr.is_npc else enemies).append(tr)
        self.tracks = seen
        self.enemies = enemies
        self.npcs = npcs

        self.crates = [c for c in (obs.get("crates") or []) if c.get("pos")]
        self.cubes_obs = [c for c in (obs.get("cubes") or []) if c.get("pos")]
        self.pickups = [p for p in (obs.get("pickups") or []) if p.get("pos")]
        self.drops = list(obs.get("drops") or [])
        self.projectiles = list(obs.get("projectiles") or [])
        self.sounds = list(obs.get("sounds") or [])
        self.beams = list(obs.get("beams") or [])
        self.events = list(obs.get("events") or [])
        self.offer = obs.get("offer")
        z = obs.get("zone")
        self.zone = ZoneInfo.from_obs(z, t) if z else None

        for s in self.sounds:
            try:
                age = float(s.get("age", 0.0))
                kind = str(s.get("kind", ""))
                if kind in ("gun", "mortar", "explosion", "laser", "lightning", "laser_charge", "jet", "boost"):
                    st = t - age
                    if st >= self.sound_t:
                        self.sound_t = st
                        self.sound_bearing = float(s.get("bearing", 0.0))
                        self.sound_kind = kind
            except (TypeError, ValueError):
                continue
        for b in self.beams:
            try:
                path = b.get("path") or []
                if path:
                    self.beam_bearing = math.atan2(float(path[0][1]) - self.pos[1], float(path[0][0]) - self.pos[0])
                    self.beam_kind = str(b.get("kind", ""))
            except (TypeError, ValueError, IndexError):
                continue

        for e in self.events:
            et = e.get("type")
            if et == "damaged":
                by = e.get("by")
                amount = float(e.get("amount", 0.0))
                self.last_damage_t = t
                self.last_damage_bearing = e.get("bearing")
                if by is not None:
                    by = int(by)
                    self.last_attacker = by
                    val = self.damage_by.setdefault(by, [0.0, t])
                    val[0] += 0.6 + min(2.0, amount / 80.0)
                    val[1] = t
                elif self.last_damage_bearing is not None:
                    self.sound_bearing = float(self.last_damage_bearing)
                    self.sound_t = t
            elif et == "effect":
                by = e.get("by")
                if by is not None:
                    self.last_attacker = int(by)
                    val = self.damage_by.setdefault(int(by), [0.0, t])
                    val[0] += 1.0
                    val[1] = t
            elif et == "hit":
                # Landing hits keeps our own regen timer down; nothing to store.
                pass

    # ------------------------------------------------------------------ helpers
    @property
    def hp_frac(self) -> float:
        return max(0.0, min(1.0, self.hp / self.max_hp)) if self.max_hp > 0 else 0.0

    @property
    def energy_frac(self) -> float:
        return max(0.0, min(1.0, self.energy / self.max_energy)) if self.max_energy > 0 else 0.0

    def walk_speed(self) -> float:
        return float(self.stats.get("walk_speed", self.mech_cfg.get("walk_speed", 11.7)))

    def boost_speed(self) -> float:
        return float(self.stats.get("boost_speed", self.mech_cfg.get("boost_speed", 19.5)))

    def eye_height(self) -> float:
        return float(self.stats.get("eye_height", self.mech_cfg.get("eye_height", 8.0)))

    def muzzle_height(self) -> float:
        return float(self.stats.get("muzzle_height", self.mech_cfg.get("muzzle_height", 7.0)))

    def eye(self) -> Vec3:
        return (self.pos[0], self.pos[1], self.pos[2] + self.eye_height())

    def center(self) -> Vec3:
        return (self.pos[0], self.pos[1], self.pos[2] + self.mech_cfg.get("height", 9.0) * 0.5)

    def muzzle(self) -> Vec3:
        return (self.pos[0], self.pos[1], self.pos[2] + self.muzzle_height())

    def slot_kind(self, slot: int) -> Optional[str]:
        if 0 <= slot < len(self.weapons) and self.weapons[slot]:
            return str(self.weapons[slot].get("kind"))
        return None

    def slot_of(self, kind: str) -> Optional[int]:
        for i, w in enumerate(self.weapons):
            if w and w.get("kind") == kind:
                return i
        return None

    def has_weapon(self, kind: str) -> bool:
        return self.slot_of(kind) is not None

    def weapon_stats(self, slot: int) -> dict:
        w = self.weapons[slot] if 0 <= slot < 2 else None
        if not w:
            return {}
        return dict(w.get("stats") or self.weapons_cfg.get(w.get("kind"), {}))

    def empty_slot(self) -> Optional[int]:
        for i, w in enumerate(self.weapons):
            if not w:
                return i
        return None

    def recent_damage(self, mid: int) -> float:
        v = self.damage_by.get(mid)
        return v[0] if v else 0.0

    # ------------------------------------------------------------------ danger
    def projectile_danger(self, terrain_height) -> Dodge:
        """Closest incoming projectile threat over the next second."""
        if not self.projectiles:
            return Dodge()
        px, py, pz = self.pos
        height = float(self.mech_cfg.get("height", 9.0))
        radius = float(self.mech_cfg.get("radius", 2.5)) + 0.9
        best = Dodge()
        best_t = 1e9
        for pr in self.projectiles:
            try:
                kind = pr.get("kind")
                p = pr.get("pos") or (0, 0, 0)
                v = pr.get("vel") or (0, 0, 0)
                x, y, z = float(p[0]), float(p[1]), float(p[2])
                vx, vy, vz = float(v[0]), float(v[1]), float(v[2])
            except (TypeError, ValueError, IndexError):
                continue
            if kind == "mortar":
                g = float(self.weapons_cfg.get("mortar", {}).get("gravity", 22.0))
                t = 0.0
                ix = iy = iz = 0.0
                while t < 6.0:
                    t += 0.08
                    ix = x + vx * t
                    iy = y + vy * t
                    iz = z + vz * t - 0.5 * g * t * t
                    if iz <= terrain_height(ix, iy) + 0.2:
                        break
                splash = float(self.weapons_cfg.get("mortar", {}).get("splash_radius", 7.0))
                d = math.hypot(ix - px, iy - py)
                if t < 6.0 and d < splash + 2.5 and t < 3.5 and t < best_t:
                    away = (px - ix, py - iy)
                    n = math.hypot(*away)
                    if n < 0.1:
                        away = (-vy, vx)
                        n = math.hypot(*away)
                    if n < 0.1:
                        away = (1.0, 0.0)
                        n = 1.0
                    best = Dodge(True, (away[0] / n, away[1] / n), "mortar", max(0.5, 1.0 - d / (splash + 3.0)))
                    best_t = t
                continue
            # bullets fly straight over the short horizon that matters
            rx = x - px
            ry = y - py
            rz = z - (pz + height * 0.5)
            rvx = vx - self.vel[0]
            rvy = vy - self.vel[1]
            rvz = vz - self.vel[2]
            vv = rvx * rvx + rvy * rvy + rvz * rvz
            if vv < 1e-6:
                continue
            tc = -(rx * rvx + ry * rvy + rz * rvz) / vv
            if tc < 0.0 or tc > 1.1 or tc > best_t:
                continue
            mx = rx + rvx * tc
            my = ry + rvy * tc
            mz = rz + rvz * tc
            if math.hypot(mx, my) > radius or abs(mz) > height * 0.5 + 1.5:
                continue
            away = (-vx, -vy) if kind == "gun" else (vx, vy)
            # Sidestep perpendicular to the bullet, away from its line of travel.
            mag = math.hypot(vx, vy)
            if mag < 0.5:
                side = (-my, mx)
            else:
                side = (-vy / mag, vx / mag)
                if side[0] * (px - x) + side[1] * (py - y) < 0:
                    side = (-side[0], -side[1])
            best = Dodge(True, (side[0], side[1]), str(kind), max(0.4, 1.0 - tc))
            best_t = tc
        return best
