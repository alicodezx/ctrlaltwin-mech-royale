"""Zone-aware movement.

Normal movement is cheap local steering: probe a handful of headings, prefer the
one closest to the goal that is open far enough ahead, and keep a consistent
side while sliding around a wall.  Bounded A* is a fallback for dead ends and
stuck states, so most decisions never allocate a search.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from perception import Perception, Track, ZoneInfo, ZoneRisk

Vec2 = Tuple[float, float]


@dataclass
class MoveResult:
    move: Vec2 = (0.0, 0.0)
    jump: bool = False
    jet: bool = False
    blocked: bool = False
    reached: bool = False
    used_path: bool = False


class Navigator:
    PROBE = 8.0
    STOP = 2.2

    def __init__(self, config: dict, world, seed: int = 1) -> None:
        self.config = config
        self.world = world
        self.rng = random.Random(seed)
        self.goal: Optional[Vec2] = None
        self.path: List[Vec2] = []
        self.path_idx = 0
        self.path_goal: Optional[Vec2] = None
        self.path_t = -1e9
        self.side = 1.0
        self.last_goal: Optional[Vec2] = None
        self.history: List[Tuple[float, Vec2]] = []
        self.stuck = 0.0
        self.stuck_level = 0
        self.avoid: Dict[int, float] = {}
        self.jet_until = -1.0
        self._hold: Optional[Vec2] = None
        self._hold_t = -1e9
        self._hold_key = None
        self._retreat: Optional[Vec2] = None
        self._retreat_t = -1e9

    # ------------------------------------------------------------------ probes
    def _ray_free(self, pos: Sequence[float], d: Vec2, maxd: float = PROBE) -> float:
        w = self.world
        if not w.available:
            return maxd
        x, y = pos[0], pos[1]
        d0 = 0.0
        exact_next = 0.0
        while d0 < maxd:
            step = 1.5
            d0 = min(maxd, d0 + step)
            px = x + d[0] * d0
            py = y + d[1] * d0
            if not (w.bounds[0] + 1.0 <= px <= w.bounds[1] - 1.0 and
                    w.bounds[2] + 1.0 <= py <= w.bounds[3] - 1.0):
                return d0 - step
            if w.cell_blocked[w.cell_index(px, py)]:
                return d0 - step
            if d0 >= exact_next:
                if not w.point_free(px, py):
                    return d0 - step
                exact_next = d0 + 3.0
        return maxd

    def _best_heading(self, p: Perception, goal: Vec2, maxd: float = PROBE) -> Tuple[Vec2, float, bool]:
        dx = goal[0] - p.pos[0]
        dy = goal[1] - p.pos[1]
        dist = math.hypot(dx, dy)
        if dist < 1e-6:
            return (0.0, 0.0), dist, True
        base = math.atan2(dy, dx)
        direct_free = self._ray_free(p.pos, (dx / dist, dy / dist), maxd)
        if direct_free >= min(maxd, max(3.0, dist - self.STOP)):
            self.side = 0.0
            return (dx / dist, dy / dist), dist, True
        best = None
        best_score = -1e9
        angles = [0, 18, -18, 36, -36, 55, -55, 75, -75, 95, -95, 120, -120, 145, -145, 180]
        for a_deg in angles:
            if a_deg == 0:
                free = direct_free
                ang = 0.0
            else:
                sign = 1.0 if a_deg > 0 else -1.0
                ang = math.radians(a_deg)
                hx = math.cos(base + ang)
                hy = math.sin(base + ang)
                free = self._ray_free(p.pos, (hx, hy), maxd)
            if a_deg == 180:
                free = max(free, 2.0)
            if free < 2.2:
                continue
            # Prefer open space, the straight line, and the side we committed to.
            score = free - 0.9 * abs(a_deg) / 90.0
            if a_deg != 0 and self.side != 0.0 and (a_deg > 0) == (self.side > 0):
                score += 0.55
            if score > best_score:
                best_score = score
                best = (a_deg, free)
        if best is None:
            return (dx / dist, dy / dist), dist, False
        a_deg, free = best
        if a_deg != 0:
            self.side = 1.0 if a_deg > 0 else -1.0
        ang = math.radians(a_deg)
        return (math.cos(base + ang), math.sin(base + ang)), dist, a_deg == 0

    # ------------------------------------------------------------------ path fallback
    def _ensure_path(self, p: Perception, goal: Vec2, zone: Optional[ZoneRisk] = None,
                     threats: Optional[List[Track]] = None) -> None:
        w = self.world
        now = p.t
        if (self.path and self.path_goal and self.path_t > now - 2.5 and
                math.hypot(self.path_goal[0] - goal[0], self.path_goal[1] - goal[1]) < 10.0):
            return
        if not w.available:
            self.path = [goal]
            self.path_idx = 0
            self.path_goal = goal
            self.path_t = now
            return
        danger = None
        if threats:
            danger = w.danger_field([(t.pos[0], t.pos[1]) for t in threats[:4]], radius=28.0, weight=2.2)
        zc = None
        if p.zone is not None:
            c, r = p.zone.predicted_circle()
            zc = (c[0], c[1], r)
        self.path = w.find_path((p.pos[0], p.pos[1]), goal, danger=danger, zone=zc,
                                max_expand=650, avoid=self.avoid, now=now,
                                time_budget=0.008)
        self.path_idx = 0
        self.path_goal = goal
        self.path_t = now

    def _follow_path(self, p: Perception, goal: Vec2) -> Optional[MoveResult]:
        if not self.path:
            return None
        while self.path_idx < len(self.path):
            wp = self.path[self.path_idx]
            if math.hypot(wp[0] - p.pos[0], wp[1] - p.pos[1]) <= 2.4:
                self.path_idx += 1
            else:
                break
        if self.path_idx >= len(self.path):
            self.path = []
            return None
        wp = self.path[self.path_idx]
        d = self._ray_free(p.pos, (wp[0] - p.pos[0], wp[1] - p.pos[1]), 6.0)
        # If the next leg is blocked, replan once rather than grinding a wall.
        if d < 2.0 and self.path_t < p.t - 1.5:
            self._ensure_path(p, goal)
            if not self.path:
                return None
            wp = self.path[self.path_idx]
        dx = wp[0] - p.pos[0]
        dy = wp[1] - p.pos[1]
        n = math.hypot(dx, dy)
        if n < 1e-6:
            return None
        mv = (dx / n, dy / n)
        reached = math.hypot(goal[0] - p.pos[0], goal[1] - p.pos[1]) <= self.STOP
        return MoveResult(mv, False, False, False, reached, True)

    # ------------------------------------------------------------------ main
    def update_stuck(self, p: Perception, intended: bool, goal: Optional[Vec2]) -> None:
        if goal is None:
            self._set_history(p)
            self.stuck = 0.0
            self.stuck_level = 0
            return
        self._set_history(p)
        if not intended:
            self.stuck = max(0.0, self.stuck - 0.15)
            return
        oldest = None
        for t, pos in self.history:
            if p.t - t <= 1.0:
                break
            oldest = (t, pos)
        if oldest is None:
            return
        moved = math.hypot(p.pos[0] - oldest[1][0], p.pos[1] - oldest[1][1])
        if moved < 0.85:
            self.stuck = min(2.2, self.stuck + 0.18)
        else:
            self.stuck = max(0.0, self.stuck - 0.2)
            if self.stuck <= 0.0:
                self.stuck_level = 0
        if self.stuck > 0.5:
            self.stuck_level = max(self.stuck_level, 1)
        if self.stuck > 1.05:
            self.stuck_level = max(self.stuck_level, 2)
        if self.stuck > 1.5:
            self.stuck_level = max(self.stuck_level, 3)
        if self.stuck > 1.95:
            self.stuck_level = max(self.stuck_level, 4)

    def _set_history(self, p: Perception) -> None:
        if not self.history or self.history[-1][0] < p.t - 0.1:
            self.history.append((p.t, (p.pos[0], p.pos[1])))
        while self.history and p.t - self.history[0][0] > 1.5:
            self.history.pop(0)

    def move_to(self, p: Perception, goal: Optional[Vec2], zone: Optional[ZoneRisk] = None,
                threats: Optional[List[Track]] = None) -> MoveResult:
        self.goal = goal
        if goal is None:
            self.update_stuck(p, False, None)
            return MoveResult(reached=True)
        dxg = goal[0] - p.pos[0]
        dyg = goal[1] - p.pos[1]
        gdist = math.hypot(dxg, dyg)
        if gdist <= self.STOP:
            self.update_stuck(p, False, goal)
            self.path = []
            return MoveResult(reached=True)
        mv, _, direct = self._best_heading(p, goal)
        self.update_stuck(p, True, goal)
        result = MoveResult(mv, blocked=not direct)
        if self.stuck_level >= 1 or self.stuck > 0.55:
            # Try a real path before jumping around.
            self._ensure_path(p, goal, zone, threats)
            fol = self._follow_path(p, goal)
            if fol is not None:
                result = fol
        result.jump = self.stuck_level >= 2 and p.grounded
        if self.stuck_level >= 3 and p.energy > 12.0:
            result.jet = True
            self.jet_until = p.t + 0.35
        elif p.t < self.jet_until:
            result.jet = True
        if self.stuck_level >= 4:
            # Last resort: move off the wall so the next decision sees new geometry.
            ang = self.rng.uniform(-math.pi, math.pi)
            result.move = (math.cos(ang), math.sin(ang))
            if self.world.available:
                self.avoid[self.world.cell_index(p.pos[0], p.pos[1])] = p.t + 2.0
            self.stuck_level = 0
            self.stuck = 0.0
            self.path = []
        return result

    def register_stuck(self, p: Perception) -> None:
        self.stuck_level = min(5, self.stuck_level + 1)
        self.stuck = 1.0
        self.path = []

    # ------------------------------------------------------------------ goals
    def zone_goal(self, p: Perception, zone: ZoneInfo, risk: ZoneRisk) -> Vec2:
        c, r = zone.predicted_circle()
        pos = (p.pos[0], p.pos[1])
        if risk.outside or r - math.hypot(pos[0] - c[0], pos[1] - c[1]) < 10.0:
            return zone.safe_point(pos, margin=12.0)
        # Move toward the next circle before it finishes closing, not just the centre.
        if risk.dist_edge_next > -2.0 and (risk.urgency > 0.8 or zone.shrink_start - p.t < 35.0):
            return zone.next_entry(pos, margin=12.0)
        # Otherwise take a cover position in the circle rather than the exact centre.
        hold = self.choose_hold_point(p, zone, [], "HOLD")
        return hold or zone.safe_point(pos, margin=12.0)

    def choose_hold_point(self, p: Perception, zone: ZoneInfo, threats: List[Track],
                          mode: str = "HOLD") -> Optional[Vec2]:
        now = p.t
        c, r = zone.predicted_circle()
        next_key = (round(zone.next_center[0] / 20.0), round(zone.next_center[1] / 20.0),
                    round(zone.next_radius / 20.0), mode)
        if self._hold is not None and now - self._hold_t < 2.0 and self._hold_key == next_key:
            return self._hold
        w = self.world
        if not w.available:
            return zone.safe_point((p.pos[0], p.pos[1]), margin=12.0)
        use_next = zone.next_radius > 0 and zone.next_radius < r and (p.t > zone.shrink_start - 35.0 or mode == "ROTATE")
        cc, rr = (zone.next_center, zone.next_radius) if use_next else (c, r)
        margin = 8.0 if rr > 90 else 5.0
        # In a big early circle, walk to the nearest good cover; only in the
        # late small circles is hugging the outer ring worth the extra travel.
        ring_w = 0.55 if rr <= 150.0 else (0.30 if rr <= 260.0 else 0.10)
        cands: List[Tuple[float, float, bool]] = []
        cover = w.cover_cells
        stride = max(1, len(cover) // 700)
        for k in cover[::stride]:
            x = w.cell_x[k]
            y = w.cell_y[k]
            if math.hypot(x - cc[0], y - cc[1]) > rr - margin:
                continue
            d = math.hypot(x - p.pos[0], y - p.pos[1])
            if d > 230.0:
                continue
            cands.append((x, y, True))
        for a in range(0, 360, 30):
            ang = math.radians(a)
            x = cc[0] + math.cos(ang) * max(0.0, rr - margin)
            y = cc[1] + math.sin(ang) * max(0.0, rr - margin)
            if w.point_free(x, y):
                cands.append((x, y, False))
        if w.point_free(cc[0], cc[1]):
            cands.append((cc[0], cc[1], False))
        if not cands:
            return zone.safe_point((p.pos[0], p.pos[1]), margin=10.0)
        scored: List[Tuple[float, float, float, bool]] = []
        cd = math.hypot(*cc)
        for x, y, is_cover in cands:
            d = math.hypot(x - p.pos[0], y - p.pos[1])
            inner = math.hypot(x - cc[0], y - cc[1])
            score = 9.0 - 0.055 * d - ring_w * abs(inner - rr * 0.58)
            if is_cover:
                score += 1.35
            if use_next and math.hypot(x - zone.next_center[0], y - zone.next_center[1]) > zone.next_radius - margin:
                score -= 4.0
            for tr in threats[:3]:
                dt = math.hypot(x - tr.pos[0], y - tr.pos[1])
                if dt < 55.0:
                    score -= 0.9 * (1.0 - dt / 55.0)
            scored.append((score, x, y, is_cover))
        scored.sort(key=lambda v: v[0], reverse=True)
        best = scored[0]
        # Check actual line-of-sight cover for a short shortlist; this is the
        # most expensive query in a decision, so keep it to a few rays.
        if threats and threats[0].distance_to(p.pos) <= 160.0:
            tr = threats[0]
            best_score = -1e9
            for score, x, y, is_cover in scored[:3]:
                z = w.terrain_height(x, y) + 4.5
                if w.los_blocked_fast((tr.pos[0], tr.pos[1], tr.pos[2] + 7.0), (x, y, z)):
                    val = score + 1.6
                elif math.hypot(tr.pos[0] - x, tr.pos[1] - y) < 70.0:
                    val = score - 0.8
                else:
                    val = score
                if val > best_score:
                    best_score = val
                    best = (score, x, y, is_cover)
        self._hold = (best[1], best[2])
        self._hold_t = now
        self._hold_key = next_key
        return self._hold

    def choose_retreat_point(self, p: Perception, zone: ZoneInfo, threats: List[Track]) -> Optional[Vec2]:
        now = p.t
        if self._retreat is not None and now - self._retreat_t < 1.0:
            return self._retreat
        w = self.world
        if not w.available:
            return zone.safe_point((p.pos[0], p.pos[1]), margin=10.0)
        c, r = zone.predicted_circle()
        cands: List[Tuple[float, float]] = []
        stride = max(1, len(w.cover_cells) // 700)
        for k in w.cover_cells[::stride]:
            x, y = w.cell_x[k], w.cell_y[k]
            if math.hypot(x - c[0], y - c[1]) > r - 8.0:
                continue
            d = math.hypot(x - p.pos[0], y - p.pos[1])
            if d < 12.0 or d > 130.0:
                continue
            cands.append((x, y))
        if not cands:
            self._retreat = zone.safe_point((p.pos[0], p.pos[1]), margin=12.0)
            self._retreat_t = now
            return self._retreat
        best = None
        best_score = -1e9
        for x, y in cands:
            score = 0.0
            for tr in threats[:3]:
                dt = math.hypot(x - tr.pos[0], y - tr.pos[1])
                score += min(1.0, dt / 90.0) * (1.4 if tr is threats[0] else 0.6)
            d = math.hypot(x - p.pos[0], y - p.pos[1])
            score -= 0.035 * d
            score -= 0.02 * math.hypot(x - c[0], y - c[1])
            if score > best_score:
                best_score = score
                best = (x, y)
        if best is not None and threats:
            # Prefer a point that actually breaks the primary threat's sightline.
            z = w.terrain_height(best[0], best[1]) + 4.5
            tr = threats[0]
            if not w.los_blocked((tr.pos[0], tr.pos[1], tr.pos[2] + 7.0), (best[0], best[1], z)):
                best = zone.safe_point((p.pos[0], p.pos[1]), margin=12.0)
        self._retreat = best
        self._retreat_t = now
        return best

    def reset(self) -> None:
        self.path = []
        self.path_idx = 0
        self.path_goal = None
        self._hold = None
        self._retreat = None
