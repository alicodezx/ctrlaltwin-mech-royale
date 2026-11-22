"""Targeting, aiming, and fire gating.

The server shoots along the torso's actual aim, not the commanded aim.  Every
weapon solution here therefore reports a desired aim *and* checks the angular
error after the torso has had one decision to slew.  Shots that cannot plausibly
connect are not fired.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

from perception import Perception, Track, angle_between, bearing

Vec2 = Tuple[float, float]
Vec3 = Tuple[float, float, float]


def clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else hi if v > hi else v


def unit_from_aim(aim: Vec2) -> Vec3:
    cp = math.cos(aim[1])
    return (cp * math.cos(aim[0]), cp * math.sin(aim[0]), math.sin(aim[1]))


def angular_error(a: Vec2, b: Vec2) -> float:
    ua = unit_from_aim(a)
    ub = unit_from_aim(b)
    dot = clamp(ua[0] * ub[0] + ua[1] * ub[1] + ua[2] * ub[2], -1.0, 1.0)
    return math.acos(dot)


def aim_to(src: Sequence[float], dst: Sequence[float]) -> Vec2:
    dx = dst[0] - src[0]
    dy = dst[1] - src[1]
    dz = dst[2] - src[2]
    h = math.hypot(dx, dy)
    return (math.atan2(dy, dx), math.atan2(dz, max(1e-6, h)))


def lead_point(src: Sequence[float], pos: Sequence[float], vel: Sequence[float], speed: float,
               iterative: int = 3) -> Vec3:
    if speed <= 0.0:
        return (pos[0], pos[1], pos[2])
    px, py, pz = pos
    t = 0.0
    for _ in range(iterative):
        nx = px + vel[0] * t
        ny = py + vel[1] * t
        nz = pz + vel[2] * t
        d = math.sqrt((nx - src[0]) ** 2 + (ny - src[1]) ** 2 + (nz - src[2]) ** 2)
        t = d / speed
    return (px + vel[0] * t, py + vel[1] * t, pz + vel[2] * t)


def ballistic_solutions(d: float, dz: float, v: float, g: float) -> List[float]:
    """Return low and high launch pitches for a ballistic shot, or []."""
    if d < 0.5 or v <= 0.0 or g <= 0.0:
        return []
    a = g * d * d / (2.0 * v * v)
    c = dz + a
    disc = d * d - 4.0 * a * c
    if disc < 0.0:
        return []
    root = math.sqrt(disc)
    if a < 1e-9:
        return []
    out = []
    for tan_t in ((d - root) / (2.0 * a), (d + root) / (2.0 * a)):
        out.append(math.atan(tan_t))
    return out


@dataclass
class Target:
    id: int
    pos: Vec3
    vel: Vec3
    kind: str  # mech / npc / crate
    hp: float
    max_hp: float
    center_z: float
    radius: float = 2.2
    weapons: Tuple[str, ...] = ()
    charging: bool = False
    cubes: int = 0
    seen_now: bool = True

    @property
    def hp_frac(self) -> float:
        return max(0.0, min(1.5, self.hp / self.max_hp)) if self.max_hp > 0 else 0.0


@dataclass
class Solution:
    slot: int = -1
    kind: str = ""
    aim: Vec2 = (0.0, 0.0)
    fire: bool = False
    reload: bool = False
    target_id: Optional[int] = None
    reason: str = ""
    confidence: float = 0.0
    fire_other: bool = False
    other_slot: int = -1


class Combat:
    def __init__(self, config: dict) -> None:
        self.config = config
        self.wcfg = config.get("weapons", {}) or {}
        self.mech_cfg = config.get("mech", {}) or {}
        self.turn_rate = float(self.mech_cfg.get("turn_rate", 4.0))
        self.pitch_rate = float(self.mech_cfg.get("pitch_rate", 3.0))
        self.min_pitch = float(self.mech_cfg.get("min_pitch", -0.7))
        self.max_pitch = float(self.mech_cfg.get("max_pitch", 1.3))
        self._laser_full_since: List[Optional[float]] = [None, None]
        self._laser_hold_until = [0.0, 0.0]

    # ------------------------------------------------------------------ targeting
    def make_target(self, tr: Track, kind: Optional[str] = None) -> Target:
        size = 1.0 + 0.05 * max(0, tr.cubes)
        if tr.is_npc or kind == "npc":
            center = tr.pos[2] + 1.75
            radius = 1.6
            k = "npc"
        else:
            center = tr.pos[2] + 4.5 * size
            radius = 2.15
            k = kind or "mech"
        return Target(tr.id, tr.pos, tr.vel, k, tr.hp, tr.max_hp, center, radius,
                      tr.weapons, tr.charging, tr.cubes, tr.seen_now)

    def crate_target(self, crate: dict) -> Target:
        p = crate.get("pos") or (0, 0, 0)
        return Target(int(crate.get("id", -1)), (float(p[0]), float(p[1]), float(p[2])), (0, 0, 0),
                      "crate", float(crate.get("hp", 300.0)), 300.0, float(p[2]) + 1.2, 1.3)

    def choose_target(self, p: Perception, report, zone=None, preferred: Optional[int] = None) -> Optional[Target]:
        best = None
        best_score = -1e9
        for tr in p.enemies:
            if p.t - tr.seen_t > 5.2:
                continue
            d = tr.distance_to(p.pos)
            if d > 260.0:
                continue
            hp = max(0.0, tr.hp_frac)
            score = 1.2 * (1.0 - hp)
            if preferred is not None and tr.id == preferred:
                score += 0.6
            score += 0.55 * (1.0 - min(1.0, d / 230.0))
            if tr.id == p.last_attacker and p.t - p.last_damage_t < 6.0:
                score += 0.8
            if p.recent_damage(tr.id) > 0.2:
                score += 0.45
            # Rear or distracted targets are cheaper to hit.
            to_us = bearing(tr.pos, p.pos)
            if angle_between(tr.aim[0], to_us) > math.radians(100.0):
                score += 0.35
            # Isolated targets are much safer to engage.
            nearby = sum(1 for o in p.enemies if o.id != tr.id and o.distance_to(tr.pos) < 55.0)
            score -= 0.28 * nearby
            score += 0.022 * min(tr.cubes, 10)
            if "laser" in tr.weapons and tr.charging:
                score += 0.25
            if zone is not None:
                c, r = zone.predicted_circle()
                if math.hypot(tr.pos[0] - c[0], tr.pos[1] - c[1]) > r:
                    score += 0.5  # taking zone damage and likely distracted
            if score > best_score:
                best_score = score
                best = self.make_target(tr)
        return best

    # ------------------------------------------------------------------ selection
    def best_slot(self, p: Perception, target: Target, world, energy_reserve: float,
                  threats: Optional[list] = None, allow_mortar: bool = True) -> int:
        d = math.sqrt((target.pos[0] - p.pos[0]) ** 2 + (target.pos[1] - p.pos[1]) ** 2 +
                      (target.center_z - p.muzzle()[2]) ** 2)
        # Keep a charging laser selected; dropping it wastes the shot.
        for slot in (0, 1):
            w = p.weapons[slot]
            if w and w.get("kind") == "laser" and (w.get("charge") or 0.0) > 0.02:
                return slot
        scores: List[Tuple[float, int]] = []
        for slot in (0, 1):
            w = p.weapons[slot]
            if not w:
                continue
            kind = str(w.get("kind"))
            stats = p.weapon_stats(slot)
            base = -1e9
            if kind == "gun":
                if d <= float(stats.get("range", 230.0)) * 1.02 and (w.get("ammo") or 0) > 0:
                    base = 1.0 - 0.25 * d / 230.0
                    if target.kind == "crate":
                        base += 0.4
            elif kind == "laser":
                if 3.0 <= d <= float(stats.get("range", 280.0)) * 0.98:
                    base = 1.28 - 0.12 * d / 280.0
                    if target.kind == "crate":
                        base -= 0.5
                    if threats:
                        for tr in threats[:3]:
                            if ("lightning" in tr.weapons and tr.distance_to(p.pos) < 52.0):
                                base -= 0.5
            elif kind == "lightning":
                rng = float(stats.get("range", 45.0))
                cost = float(stats.get("energy", 3.0))
                if d <= rng and p.energy - cost >= min(energy_reserve, 10.0):
                    base = 1.45 + (0.35 if "laser" in target.weapons and d < 48.0 else 0.0)
                    if target.kind != "mech":
                        base -= 0.55
            elif kind == "mortar":
                if not allow_mortar:
                    continue
                if 40.0 <= d <= 210.0 and target.kind == "mech":
                    base = 0.9
                    for tr in (threats or [])[:3]:
                        if "lightning" in tr.weapons and tr.distance_to(p.pos) < 50.0:
                            base -= 0.6
                    if target.vel[0] ** 2 + target.vel[1] ** 2 > 55.0:
                        base -= 0.35
            scores.append((base, slot))
        if not scores:
            return -1
        scores.sort(reverse=True)
        return scores[0][1] if scores[0][0] > -1e8 else -1

    # ------------------------------------------------------------------ solving
    def solve(self, p: Perception, target: Target, world, energy_reserve: float = 18.0,
              threats: Optional[list] = None, allow_mortar: bool = True,
              aggressive: bool = True) -> Solution:
        slot = self.best_slot(p, target, world, energy_reserve, threats, allow_mortar)
        if slot < 0:
            # Still face the target even with no usable weapon.
            aim = aim_to(p.muzzle(), (target.pos[0], target.pos[1], target.center_z))
            return Solution(-1, "", aim, False, False, target.id, "no-weapon")
        w = p.weapons[slot] or {}
        kind = str(w.get("kind", ""))
        stats = p.weapon_stats(slot)

        if kind == "mortar":
            sol = self._solve_mortar(p, target, world, stats)
            if sol is None:
                # Fall back to another weapon rather than firing a bad shell.
                if aggressive:
                    return self._solve_direct(p, target, world, exclude=slot, threats=threats,
                                              energy_reserve=energy_reserve)
                aim = aim_to(p.muzzle(), (target.pos[0], target.pos[1], target.center_z))
                return Solution(slot, kind, aim, False, False, target.id, "no-arc")
            aim, fire, conf, reason = sol
            return Solution(slot, kind, aim, fire, False, target.id, reason, conf)

        return self._solve_direct(p, target, world, prefer=slot, threats=threats,
                                  energy_reserve=energy_reserve)

    def solve_slot(self, p: Perception, target: Target, world, slot: int,
                   threats: Optional[list] = None) -> Optional[Solution]:
        """One specific slot: used to fire a gun while a laser charges."""
        if slot < 0 or slot > 1:
            return None
        w = p.weapons[slot]
        if not w:
            return None
        kind = str(w.get("kind", ""))
        if kind == "mortar":
            return None
        return self._solve_one(p, target, world, slot, kind, w, threats)

    def _solve_direct(self, p: Perception, target: Target, world, prefer: Optional[int] = None,
                      exclude: int = -1, threats: Optional[list] = None,
                      energy_reserve: float = 18.0) -> Solution:
        order = [prefer] if prefer is not None else []
        order += [s for s in (0, 1) if s not in order]
        first: Optional[Solution] = None
        for slot in order:
            if slot == exclude or slot < 0 or slot > 1:
                continue
            w = p.weapons[slot]
            if not w:
                continue
            kind = str(w.get("kind"))
            if kind == "mortar":
                continue
            sol = self._solve_one(p, target, world, slot, kind, w, threats, energy_reserve)
            if sol is None:
                continue
            if sol.fire:
                return sol
            if first is None and sol.slot == prefer:
                first = sol
            elif first is None:
                first = sol
        if first is not None:
            return first
        aim = aim_to(p.muzzle(), (target.pos[0], target.pos[1], target.center_z))
        return Solution(-1, "", aim, False, False, target.id, "no-solution")

    def _solve_one(self, p: Perception, target: Target, world, slot: int, kind: str,
                   w: dict, threats: Optional[list], energy_reserve: float = 18.0) -> Optional[Solution]:
        stats = p.weapon_stats(slot)
        cooldown = float(w.get("cooldown", 0.0))
        src = p.muzzle()
        target_pt = (target.pos[0], target.pos[1], target.center_z)
        d3 = math.sqrt((target_pt[0] - src[0]) ** 2 + (target_pt[1] - src[1]) ** 2 +
                       (target_pt[2] - src[2]) ** 2)
        is_laser = kind == "laser"
        is_lightning = kind == "lightning"
        is_gun = kind == "gun"

        if is_gun:
            if (w.get("ammo") or 0) <= 0 or (w.get("reload") or 0) > 0:
                # Auto-reload happens anyway; start it early only when idle.
                return Solution(slot, kind, aim_to(src, target_pt), False,
                                (w.get("ammo") or 0) <= 0, target.id, "reload")
            speed = float(stats.get("speed", 252.0))
            rng = float(stats.get("range", 230.0))
            pred = lead_point(src, target_pt, target.vel, speed)
            aim = aim_to(src, pred)
            if d3 > rng * 0.985:
                return Solution(slot, kind, aim, False, False, target.id, "out-of-range")
            if cooldown > 0.06:
                return Solution(slot, kind, aim, False, False, target.id, "cooldown")
            if world.los_blocked(src, pred):
                return Solution(slot, kind, aim, False, False, target.id, "no-los")
            err = self._projected_error(p, aim)
            hit_angle = math.atan2(target.radius, max(1.0, d3))
            spread = math.radians(float(stats.get("spread_deg", 0.6)))
            allow = max(0.0025, hit_angle - spread * 1.3)
            fire = err <= allow
            return Solution(slot, kind, aim, fire, False, target.id,
                            "gun-fire" if fire else "slew", max(0.0, 1.0 - err / max(allow, 1e-4)))

        if is_laser:
            charge = float(w.get("charge", 0.0))
            charge_time = max(0.1, float(stats.get("charge", 0.75)))
            rng = float(stats.get("range", 280.0))
            pred = (target_pt[0] + target.vel[0] * 0.06,
                    target_pt[1] + target.vel[1] * 0.06,
                    target_pt[2] + target.vel[2] * 0.06)
            aim = aim_to(src, pred)
            in_range = d3 <= rng * 0.98
            los = not world.los_blocked(src, pred)
            if not in_range or not los:
                if charge > 0.02:
                    return Solution(slot, kind, aim, False, False, target.id, "cancel", 0.0)
                return Solution(slot, kind, aim, False, False, target.id,
                                "out-of-range" if not in_range else "no-los", 0.0)
            err = self._projected_error(p, aim)
            hit_angle = math.atan2(target.radius, max(1.0, d3))
            allow = max(0.002, hit_angle * 0.75)
            if charge <= 0.02:
                if cooldown > 0.08:
                    return Solution(slot, kind, aim, False, False, target.id, "cooldown")
                risky = False
                if threats:
                    for tr in threats[:3]:
                        if "lightning" in tr.weapons and tr.distance_to(p.pos) < 50.0:
                            risky = True
                if risky and p.hp_frac < 0.55:
                    return Solution(slot, kind, aim, False, False, target.id, "risky-charge")
                return Solution(slot, kind, aim, True, False, target.id, "charge")
            # Charging: hold until enough damage, then release on a good line.
            enough = charge >= min(charge_time * 0.97, max(0.30, charge_time * 0.62))
            if charge >= charge_time * 0.97:
                self._laser_full_since[slot] = self._laser_full_since[slot] or p.t
            held_full = p.t - (self._laser_full_since[slot] or p.t)
            release = enough and err <= allow
            if held_full > 1.4:
                release = True
            if release:
                self._laser_full_since[slot] = None
            return Solution(slot, kind, aim, not release, False, target.id,
                            "release" if release else "charge", max(0.0, 1.0 - err / max(allow, 1e-4)))

        if is_lightning:
            rng = float(stats.get("range", 45.0))
            cone = math.radians(float(stats.get("cone_deg", 30.0)) * 0.5)
            cost = float(stats.get("energy", 3.0))
            aim = aim_to(src, target_pt)
            if d3 > rng * 0.97:
                return Solution(slot, kind, aim, False, False, target.id, "out-of-range")
            if p.energy - cost < min(energy_reserve, 10.0):
                return Solution(slot, kind, aim, False, False, target.id, "energy")
            if cooldown > 0.03:
                return Solution(slot, kind, aim, False, False, target.id, "cooldown")
            if angle_between(p.aim[0], bearing(p.pos, target.pos)) > max(0.02, cone - 0.02):
                return Solution(slot, kind, aim, False, False, target.id, "cone")
            if world.los_blocked(src, target_pt):
                return Solution(slot, kind, aim, False, False, target.id, "no-los")
            # The arc hits the nearest target in the cone; avoid feeding a crate.
            nearest = None
            best_d = 1e9
            for tr in list(p.enemies) + list(p.npcs):
                dd = tr.distance_to(p.pos)
                if dd < best_d and dd <= rng and angle_between(p.aim[0], bearing(p.pos, tr.pos)) <= cone:
                    best_d = dd
                    nearest = tr.id
            for c in p.crates:
                cp = c.get("pos") or (0, 0, 0)
                dd = math.hypot(cp[0] - p.pos[0], cp[1] - p.pos[1])
                if dd < best_d and dd <= rng and angle_between(p.aim[0], bearing(p.pos, cp)) <= cone:
                    best_d = dd
                    nearest = int(c.get("id", -1))
            if nearest is not None and nearest != target.id and best_d < d3 - 1.0:
                return Solution(slot, kind, aim, False, False, target.id, "blocked-by-nearer")
            return Solution(slot, kind, aim, True, False, target.id, "lightning-fire", 1.0)

        return None

    def _projected_error(self, p: Perception, aim: Vec2) -> float:
        err = angular_error(p.aim, aim)
        cap = max(self.turn_rate, self.pitch_rate) * 0.1
        return max(0.0, err - cap)

    # ------------------------------------------------------------------ mortar
    def _solve_mortar(self, p: Perception, target: Target, world, stats: dict):
        if target.kind != "mech":
            return None
        speed = float(stats.get("speed", 72.0))
        g = float(stats.get("gravity", 22.0))
        ttl = float(stats.get("ttl", 7.0))
        splash = float(stats.get("splash_radius", 7.0))
        muzzle = (p.pos[0], p.pos[1], p.pos[2] + p.stats.get("height", 9.0) + 0.5)
        pred = (target.pos[0], target.pos[1], target.center_z)
        if math.hypot(pred[0] - p.pos[0], pred[1] - p.pos[1]) < 18.0:
            return None
        best = None
        for _ in range(3):
            dx = pred[0] - muzzle[0]
            dy = pred[1] - muzzle[1]
            dz = pred[2] - muzzle[2]
            d = math.hypot(dx, dy)
            sols = ballistic_solutions(d, dz, speed, g)
            if not sols:
                return None
            chosen = None
            for pitch in sols:
                if pitch < self.min_pitch or pitch > self.max_pitch:
                    continue
                t = d / max(1e-6, speed * math.cos(pitch))
                if t > ttl * 0.96:
                    continue
                impact = (pred[0], pred[1], pred[2])
                if math.hypot(impact[0] - p.pos[0], impact[1] - p.pos[1]) < splash + 2.5:
                    continue
                if not self._arc_clear(world, muzzle, pitch, math.atan2(dy, dx), speed, g, t):
                    continue
                chosen = (pitch, t)
                break
            if chosen is None:
                return None
            pitch, tflight = chosen
            # Lead the target by the shell's flight time.
            pred = (target.pos[0] + target.vel[0] * tflight,
                    target.pos[1] + target.vel[1] * tflight,
                    target.center_z + target.vel[2] * tflight * 0.3)
            best = (pitch, math.atan2(pred[1] - muzzle[1], pred[0] - muzzle[0]), tflight)
            if target.vel[0] ** 2 + target.vel[1] ** 2 < 1.0:
                break
        if best is None:
            return None
        pitch, yaw, _ = best
        aim = (yaw, clamp(pitch, self.min_pitch, self.max_pitch))
        err = angular_error(p.aim, aim)
        allow = math.radians(1.1)
        fire = err <= allow and self._predicted_safe(p, target, splash)
        return aim, fire, max(0.0, 1.0 - err / allow), "mortar-fire" if fire else "mortar-slew"

    def _predicted_safe(self, p: Perception, target: Target, splash: float) -> bool:
        return math.hypot(target.pos[0] - p.pos[0], target.pos[1] - p.pos[1]) > splash + 3.0

    def _arc_clear(self, world, start: Sequence[float], pitch: float, yaw: float,
                   speed: float, g: float, tflight: float) -> bool:
        if not world.available:
            return True
        vx = speed * math.cos(pitch) * math.cos(yaw)
        vy = speed * math.cos(pitch) * math.sin(yaw)
        vz = speed * math.sin(pitch)
        steps = max(4, min(90, int(tflight / 0.09)))
        for s in range(1, steps + 1):
            t = tflight * s / steps
            x = start[0] + vx * t
            y = start[1] + vy * t
            z = start[2] + vz * t - 0.5 * g * t * t
            if s == steps:
                return True
            if world.point_solid(x, y, z):
                return False
        return True

    # ------------------------------------------------------------------ misc
    def cancel_charge(self, p: Perception) -> None:
        self._laser_full_since = [None, None]
