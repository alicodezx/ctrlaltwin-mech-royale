"""Threat and opportunity scoring.

A threat score is intentionally simple and explainable: proximity, weapon
danger, whether the enemy is looking at us, line of sight, health and cubes,
plus recent damage attribution.  The report also exposes the information the
strategy needs to decide whether fighting is favourable at all.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import List, Optional

from perception import Perception, Track, angle_between, bearing


@dataclass
class Threat:
    id: int
    track: Track
    distance: float
    los: bool
    aiming_at_us: bool
    charging: bool
    weapons: tuple
    score: float


@dataclass
class ThreatReport:
    threats: List[Threat] = field(default_factory=list)
    primary: Optional[Threat] = None
    pressure: float = 0.0
    near_pressure: float = 0.0
    close_count: int = 0
    engaging_count: int = 0
    recent_attacker: Optional[int] = None
    recent_damage: float = 0.0

    @property
    def fighting(self) -> bool:
        return self.pressure > 0.0


_WEAPON_DANGER = {
    "gun": 0.85,
    "mortar": 0.95,
    "laser": 1.15,
    "lightning": 1.2,
}


class ThreatAssessor:
    def __init__(self, config: dict) -> None:
        self.config = config
        self.fov_half = math.radians(float(config.get("vision", {}).get("fov_deg", 120.0)) * 0.5)
        self.near = float(config.get("vision", {}).get("near_range", 45.0))
        self.memory_s = float(config.get("vision", {}).get("memory_s", 5.0))

    def assess(self, p: Perception, world) -> ThreatReport:
        report = ThreatReport()
        eye = p.eye()
        now = p.t
        scored: List[Threat] = []
        for tr in p.enemies:
            if now - tr.seen_t > self.memory_s + 0.2:
                continue
            d = tr.distance_to(p.pos)
            target = (tr.pos[0], tr.pos[1], tr.pos[2] + 4.5)
            los = not world.los_blocked_fast(eye, target)
            to_us = bearing(tr.pos, p.pos)
            aiming = angle_between(tr.aim[0], to_us) <= self.fov_half
            charging = tr.charging and "laser" in tr.weapons

            wd = 0.55
            for w in tr.weapons:
                base = _WEAPON_DANGER.get(w, 0.7)
                if w == "laser" and charging:
                    base += 0.55
                if w == "lightning" and d <= float(self.config.get("weapons", {}).get("lightning", {}).get("range", 45.0)) + 5:
                    base += 0.25
                if w == "mortar":
                    base += 0.1
                if base > wd:
                    wd = base
            prox = max(0.0, 1.0 - d / 165.0)
            hp_factor = 1.3 - 0.4 * tr.hp_frac
            cube_factor = 1.0 + 0.03 * min(tr.cubes, 10)
            score = wd * (0.45 + 0.75 * prox) * hp_factor * cube_factor
            if not los:
                score *= 0.42
            if aiming:
                score *= 1.25
            score += 0.85 * p.recent_damage(tr.id)
            if tr.id == p.last_attacker and now - p.last_damage_t < 6.0:
                score += 0.6
            if charging and aiming:
                score += 0.55
            if d < self.near and los:
                score += 0.35
            t = Threat(tr.id, tr, d, los, aiming, charging, tr.weapons, score)
            scored.append(t)
        scored.sort(key=lambda t: t.score, reverse=True)
        report.threats = scored
        report.primary = scored[0] if scored else None
        report.pressure = sum(t.score for t in scored[:3])
        report.near_pressure = sum(t.score for t in scored if t.distance < 70.0 and t.los)
        report.close_count = sum(1 for t in scored if t.distance < 60.0 and t.los)
        report.engaging_count = sum(1 for t in scored if t.aiming_at_us and t.los and t.distance < 150.0)
        if p.last_attacker is not None and now - p.last_damage_t < 8.0:
            report.recent_attacker = p.last_attacker
            report.recent_damage = p.recent_damage(p.last_attacker)
        return report
