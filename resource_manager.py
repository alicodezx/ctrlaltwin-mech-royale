"""Loot, cubes, crates, drones, supply drops and upgrade choices.

Resource decisions are deliberately conservative: nothing here is worth walking
into a fight or outside the next circle for.  The strategy state machine decides
when the battlefield is quiet enough to spend time on economy.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

from perception import Perception, Track, ZoneInfo

Vec2 = Tuple[float, float]


@dataclass
class ResourceGoal:
    kind: str  # pickup / cube / crate / drone / drop
    id: int
    pos: Vec2
    score: float
    reason: str = ""
    swap_slot: Optional[int] = None


_UPGRADE_PREF = {
    "plating": 100.0,
    "armor": 96.0,
    "nanites": 92.0,
    "capacitor": 88.0,
    "reactor": 85.0,
    "servos": 82.0,
    "jets": 78.0,
    "gyros": 74.0,
    "sensors": 66.0,
    "gun_damage": 70.0,
    "gun_rate": 68.0,
    "gun_mag": 62.0,
    "gun_velocity": 60.0,
    "laser_damage": 72.0,
    "laser_charge": 68.0,
    "laser_pierce": 52.0,
    "lightning_chain": 70.0,
    "lightning_efficiency": 68.0,
    "lightning_range": 64.0,
    "mortar_splash": 58.0,
    "mortar_reload": 60.0,
    "mortar_velocity": 50.0,
}

_WEAPON_VALUE = {"laser": 1.0, "lightning": 0.86, "mortar": 0.58, "gun": 0.12}


class ResourceManager:
    def __init__(self, config: dict) -> None:
        self.config = config
        self.loot_cfg = config.get("loot", {}) or {}
        self.cube_cfg = config.get("cubes", {}) or {}
        self.pickup_radius = float(self.loot_cfg.get("pickup_radius", 4.0))
        self.thresholds = [int(v) for v in (self.cube_cfg.get("upgrade_thresholds") or [])]

    # ------------------------------------------------------------------ upgrades
    def pick_upgrade(self, p: Perception) -> Optional[int]:
        offer = p.offer
        if not offer:
            return None
        choices = offer.get("choices") or []
        if not choices:
            return None
        best_i = 0
        best = -1e9
        for i, c in enumerate(choices):
            cid = str(c.get("id", ""))
            score = _UPGRADE_PREF.get(cid, 40.0)
            if cid in ("plating", "nanites") and p.hp_frac < 0.6:
                score += 12.0
            if cid in ("capacitor", "reactor", "jets") and p.energy_frac < 0.45:
                score += 8.0
            if "laser" in cid and p.has_weapon("laser"):
                score += 8.0
            if "lightning" in cid and p.has_weapon("lightning"):
                score += 8.0
            if "mortar" in cid and p.has_weapon("mortar"):
                score += 5.0
            if "gun" in cid and p.has_weapon("gun"):
                score += 2.0
            if score > best:
                best = score
                best_i = i
        return best_i

    # ------------------------------------------------------------------ helpers
    def _zone_ok(self, x: float, y: float, zone: Optional[ZoneInfo], margin: float = 3.0) -> bool:
        if zone is None:
            return True
        c, r = zone.predicted_circle()
        if math.hypot(x - c[0], y - c[1]) > r - margin:
            return False
        if zone.next_radius < zone.radius and zone.t > zone.shrink_start - 28.0:
            if math.hypot(x - zone.next_center[0], y - zone.next_center[1]) > zone.next_radius - margin:
                return False
        return True

    def _threat_near(self, p: Perception, report, x: float, y: float, radius: float = 65.0) -> float:
        if not report:
            return 0.0
        penalty = 0.0
        for th in report.threats[:3]:
            d = math.hypot(th.track.pos[0] - x, th.track.pos[1] - y)
            if d < radius and th.los:
                penalty += (1.0 - d / radius) * (1.0 if th.los else 0.4)
        return penalty

    def _reachable_ground(self, p: Perception, world, x: float, y: float, z: float) -> bool:
        if abs(z - p.pos[2]) <= 4.0:
            return True
        if world.available:
            return z - world.terrain_height(x, y) <= 2.2
        return True

    # ------------------------------------------------------------------ weapons
    def weapon_goal(self, p: Perception, report, zone: Optional[ZoneInfo], world) -> Optional[ResourceGoal]:
        if not p.pickups:
            return None
        empty = p.empty_slot()
        best = None
        best_score = -1e9
        for pk in p.pickups:
            try:
                kind = str(pk.get("kind", ""))
                pos = pk.get("pos") or (0, 0, 0)
                x, y, z = float(pos[0]), float(pos[1]), float(pos[2])
                pid = int(pk.get("id", -1))
            except (TypeError, ValueError, IndexError):
                continue
            if kind not in _WEAPON_VALUE or kind == "gun":
                continue
            if p.has_weapon(kind):
                continue
            if not self._zone_ok(x, y, zone):
                continue
            if not self._reachable_ground(p, world, x, y, z):
                continue
            d = math.hypot(x - p.pos[0], y - p.pos[1])
            if d > 135.0:
                continue
            swap = None
            value = _WEAPON_VALUE[kind]
            if empty is None:
                # Swap out the least useful non-gun special, and only for a clear gain.
                weakest = None
                weakest_v = 9.0
                for slot in (1, 0):
                    w = p.weapons[slot]
                    if not w:
                        continue
                    k = str(w.get("kind"))
                    if k == "gun":
                        continue
                    if k == "laser" and (w.get("charge") or 0.0) > 0.01:
                        continue
                    v = _WEAPON_VALUE.get(k, 0.2)
                    if v < weakest_v:
                        weakest_v = v
                        weakest = slot
                if weakest is None:
                    continue
                swap = weakest
                if value < weakest_v + 0.25:
                    continue
            score = 1.5 * value - d / 65.0 - 0.9 * self._threat_near(p, report, x, y, 60.0)
            if empty is not None:
                score += 0.25
            if score > best_score:
                best_score = score
                best = ResourceGoal("pickup", pid, (x, y), score, kind, swap)
        return best

    # ------------------------------------------------------------------ cubes / crates / drones
    def cube_goal(self, p: Perception, report, zone: Optional[ZoneInfo], world) -> Optional[ResourceGoal]:
        if not p.cubes_obs:
            return None
        best = None
        best_score = -1e9
        nxt = None
        for th in self.thresholds:
            if th > p.cubes:
                nxt = th
                break
        for c in p.cubes_obs:
            try:
                pos = c.get("pos") or (0, 0, 0)
                x, y, z = float(pos[0]), float(pos[1]), float(pos[2])
                cid = int(c.get("id", -1))
            except (TypeError, ValueError, IndexError):
                continue
            if not self._zone_ok(x, y, zone):
                continue
            if not self._reachable_ground(p, world, x, y, z):
                continue
            d = math.hypot(x - p.pos[0], y - p.pos[1])
            if d > 95.0:
                continue
            score = 1.0 - d / 110.0 - 0.8 * self._threat_near(p, report, x, y, 65.0)
            if nxt is not None and nxt - p.cubes <= 1 and d < 50.0:
                score += 0.35
            if score > best_score:
                best_score = score
                best = ResourceGoal("cube", cid, (x, y), score, "cube")
        return best

    def crate_goal(self, p: Perception, report, zone: Optional[ZoneInfo], world, threat_free: bool) -> Optional[ResourceGoal]:
        if not threat_free or not p.crates:
            return None
        best = None
        best_score = -1e9
        gslot = p.slot_of("gun")
        has_gun = gslot is not None and (p.weapons[gslot].get("ammo") or 0) >= 3
        has_lightning = p.has_weapon("lightning") and p.energy > 18.0
        if not (has_gun or has_lightning):
            return None
        for c in p.crates:
            try:
                pos = c.get("pos") or (0, 0, 0)
                x, y, z = float(pos[0]), float(pos[1]), float(pos[2])
                cid = int(c.get("id", -1))
                hp = float(c.get("hp", 300.0))
            except (TypeError, ValueError, IndexError):
                continue
            if hp <= 0 or not self._zone_ok(x, y, zone):
                continue
            if not self._reachable_ground(p, world, x, y, z):
                continue
            d = math.hypot(x - p.pos[0], y - p.pos[1])
            if d > 115.0:
                continue
            score = 0.5 - d / 150.0
            if score > best_score:
                best_score = score
                best = ResourceGoal("crate", cid, (x, y), score, "crate")
        return best

    def drone_goal(self, p: Perception, report, zone: Optional[ZoneInfo], world) -> Optional[ResourceGoal]:
        if not p.npcs or (report and report.near_pressure > 1.0):
            return None
        best = None
        best_score = -1e9
        for tr in p.npcs:
            d = tr.distance_to(p.pos)
            if d > 58.0 or tr.hp <= 0:
                continue
            if not self._zone_ok(tr.pos[0], tr.pos[1], zone):
                continue
            if not self._reachable_ground(p, world, tr.pos[0], tr.pos[1], tr.pos[2]):
                continue
            score = 0.34 - d / 130.0
            if score > best_score:
                best_score = score
                best = ResourceGoal("drone", tr.id, (tr.pos[0], tr.pos[1]), score, "drone")
        return best

    # ------------------------------------------------------------------ drops
    def drop_goal(self, p: Perception, report, zone: Optional[ZoneInfo], world) -> Optional[ResourceGoal]:
        if not p.drops:
            return None
        best = None
        best_score = -1e9
        for dr in p.drops:
            try:
                pos = dr.get("pos") or (0, 0, 0)
                x, y = float(pos[0]), float(pos[1])
                did = int(dr.get("id", -1))
                lands = float(dr.get("lands_in", 0.0))
                radius = float(dr.get("radius", 8.0))
                holder = dr.get("holder")
            except (TypeError, ValueError, IndexError):
                continue
            d = math.hypot(x - p.pos[0], y - p.pos[1])
            travel = d / max(1.0, p.walk_speed())
            if lands > travel + 8.0:
                continue
            if not self._zone_ok(x, y, zone, margin=2.0):
                # Drops land inside the next circle; allow a little edge slack.
                if zone is not None:
                    if math.hypot(x - zone.next_center[0], y - zone.next_center[1]) > zone.next_radius:
                        continue
            score = 2.0 + 2.0 * (1.0 - p.hp_frac) - d / 90.0 - 1.0 * self._threat_near(p, report, x, y, 70.0)
            if lands <= 0.2:
                score += 0.5
            if holder is not None and holder != p.self_id:
                score -= 0.6
            if score > best_score:
                best_score = score
                best = ResourceGoal("drop", did, (x, y), score, f"lands {lands:.0f}s")
        return best

    def choose(self, p: Perception, report, zone: Optional[ZoneInfo], world,
               allow_crates: bool = True) -> Optional[ResourceGoal]:
        options: List[ResourceGoal] = []
        for g in (self.weapon_goal(p, report, zone, world),
                  self.cube_goal(p, report, zone, world),
                  self.drone_goal(p, report, zone, world)):
            if g is not None:
                options.append(g)
        if allow_crates:
            g = self.crate_goal(p, report, zone, world, threat_free=not report or report.near_pressure < 0.9)
            if g is not None:
                options.append(g)
        if not options:
            return None
        options.sort(key=lambda g: g.score, reverse=True)
        return options[0]
