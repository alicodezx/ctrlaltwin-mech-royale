"""High-level controller: the tactical state machine.

Arbitration on every decision, highest priority first:
  1. immediate survival    - projectile dodge, emergency disengage
  2. zone safety           - get inside the predicted circle first
  3. emergency threat      - break line of sight and retreat toward cover
  4. tactical positioning  - hold / rotate / endgame cover
  5. combat opportunities  - engage only when the trade is favourable
  6. resource collection   - loot, cubes, crates, drones, supply drops

The strategy never talks to the server.  It turns one observation into a plain
action dict; the bot entry point and the wrapper handle the protocol, and every
action passes through `ActionValidator`.
"""
from __future__ import annotations

import math
import traceback
from typing import Dict, List, Optional, Sequence, Tuple

from action_safety import ActionValidator
from combat import Combat, Solution, Target, aim_to
from navigation import MoveResult, Navigator
from perception import Perception, Track, ZoneInfo, ZoneRisk, angle_between, bearing, wrap
from resource_manager import ResourceGoal, ResourceManager
from threat_assessment import ThreatAssessor, ThreatReport

Vec2 = Tuple[float, float]
Vec3 = Tuple[float, float, float]

_RANGE_BAND = {
    "gun": (65.0, 150.0),
    "laser": (80.0, 240.0),
    "lightning": (0.0, 34.0),
    "mortar": (60.0, 165.0),
}
_REACH = {"gun": 215.0, "laser": 260.0, "lightning": 42.0, "mortar": 190.0}


class Strategy:
    def __init__(self, config: dict, world, seed: int = 1, log=None) -> None:
        self.config = config or {}
        self.world = world
        self.log = log or (lambda *a: None)
        self.seed = seed
        self.p = Perception(self.config)
        self.nav = Navigator(self.config, world, seed)
        self.combat = Combat(self.config)
        self.threat_assess = ThreatAssessor(self.config)
        self.resources = ResourceManager(self.config)
        self.validator = ActionValidator(self.config)

        self.state = "ROTATE"
        self.state_t = -1e9
        self.target_id: Optional[int] = None
        self._target_pos: Optional[Vec3] = None
        self._target_t = -1e9
        self._prev_hp: Optional[float] = None
        self._prev_t: Optional[float] = None
        self._lost_hp = 0.0
        self.strafe_side = 1.0
        self.strafe_t = -1e9
        self._skip_pickups: Dict[int, float] = {}

    # ------------------------------------------------------------------ entry
    def decide(self, obs: dict) -> dict:
        self.p.update(obs)
        try:
            action = self._decide(self.p)
        except Exception:
            self.log("strategy error:\n" + traceback.format_exc())
            action = self._fallback(self.p)
        return self.validator.validate(self.p, action)

    def fallback(self, obs: Optional[dict] = None) -> dict:
        if obs is not None:
            try:
                self.p.tick = int(obs.get("tick", self.p.tick))
            except (TypeError, ValueError):
                pass
        return self.validator.validate(self.p, self._fallback(self.p))

    def _fallback(self, p: Perception) -> dict:
        action = {"move": [0.0, 0.0], "aim": [p.aim[0], p.aim[1]], "fire": [False, False]}
        for slot in (0, 1):
            w = p.weapons[slot]
            if w and w.get("kind") == "laser" and float(w.get("charge") or 0.0) > 0.02:
                action["fire"][slot] = True
        return action

    # ------------------------------------------------------------------ pipeline
    def _decide(self, p: Perception) -> dict:
        report = self.threat_assess.assess(p, self.world)
        zone = p.zone
        risk = zone.risk(p.pos, p.walk_speed(), p.boost_speed()) if zone is not None else None
        dodge = p.projectile_danger(self.world.terrain_height)

        if self._prev_t is not None and self._prev_hp is not None and p.t >= self._prev_t:
            self._lost_hp = max(0.0, self._prev_hp - p.hp)
        self._prev_hp = p.hp
        self._prev_t = p.t
        for ev in p.events:
            if ev.get("type") == "take_failed":
                try:
                    self._skip_pickups[int(ev.get("id", -1))] = p.t + 3.0
                except (TypeError, ValueError):
                    pass

        target = self._pick_target(p, report, zone)
        res = self._pick_resource(p, report, zone)
        state = self._choose_state(p, report, risk, target, res)
        self._enter_state(p, state)

        action: Dict[str, object] = {
            "move": [0.0, 0.0],
            "aim": [p.aim[0], p.aim[1]],
            "fire": [False, False],
        }
        pick = self.resources.pick_upgrade(p)
        if pick is not None:
            action["pick"] = pick

        aim_target = target
        if state in ("LOOT", "CONTEST") and res is not None:
            special = self._resource_target(p, res)
            if special is not None:
                aim_target = special

        reserve = self._energy_reserve(p, report)
        sol: Optional[Solution] = None
        if aim_target is not None and self._want_combat(p, state, aim_target):
            sol = self._solve(p, aim_target, report, state, risk, reserve)

        goal = self._goal(p, state, target, report, risk, res)
        threats = [th.track for th in report.threats[:4]]
        if goal is not None:
            move = self.nav.move_to(p, goal, risk, threats)
        else:
            self.nav.update_stuck(p, False, None)
            move = MoveResult()
        if (goal is not None and state in ("HOLD", "ENDGAME")
                and self._dist(p.pos, goal) < 3.0 and report.engaging_count > 0):
            move = self._strafe(p, report)

        boost = self._want_boost(p, state, risk)
        if dodge.active and dodge.urgent >= 0.55:
            move = MoveResult(dodge.direction, False, False, False, False, False)
            boost = p.grounded and p.energy > 8.0
        action["move"] = [float(move.move[0]), float(move.move[1])]
        if (boost and p.grounded) or move.jet:
            action["boost"] = True
        if move.jump and p.grounded:
            action["jump"] = True

        if sol is not None:
            self._apply_solution(p, action, sol, state, aim_target, report)
        else:
            action["aim"] = list(self._idle_aim(p, move.move))
        self._laser_trigger(p, action, state, sol)

        if res is not None and state in ("LOOT", "CONTEST") and report.near_pressure < 0.8:
            self._apply_take(p, action, res)

        self._maybe_reload(p, action)
        return action

    # ------------------------------------------------------------------ state
    def _choose_state(self, p: Perception, report: ThreatReport, risk: Optional[ZoneRisk],
                      target: Optional[Target], res: Optional[ResourceGoal]) -> str:
        if self._want_disengage(p, report):
            return "DISENGAGE"
        if risk is not None and risk.outside:
            return "ROTATE"
        if risk is not None and risk.urgency >= 1.45:
            return "ROTATE"

        endgame = self._is_endgame(p)
        if target is not None and self._should_engage(p, report, target, risk, endgame):
            return "ENGAGE"
        if res is not None and self._resource_ok(p, report, res, endgame):
            return "CONTEST" if res.kind == "drop" else "LOOT"
        return "ENDGAME" if endgame else "HOLD"

    def _enter_state(self, p: Perception, state: str) -> None:
        if state == self.state:
            return
        soft = {"HOLD", "LOOT", "CONTEST"}
        if self.state in soft and state in soft and p.t - self.state_t < 0.35:
            return
        if state == "DISENGAGE":
            self.nav.reset()
        self.state = state
        self.state_t = p.t

    def _want_disengage(self, p: Perception, report: ThreatReport) -> bool:
        if not report.threats:
            return False
        hp = p.hp_frac
        near = report.near_pressure
        close = report.close_count
        if hp < 0.30 and (close >= 1 or near > 0.5):
            return True
        if hp < 0.45 and near > 1.35 and close >= 1:
            return True
        if near > 1.6 + 1.4 * hp and close >= 2:
            return True
        if p.t - p.last_damage_t < 1.0 and self._lost_hp > 260.0 and near > 0.5:
            return True
        return False

    def _should_engage(self, p: Perception, report: ThreatReport, target: Target,
                       risk: Optional[ZoneRisk], endgame: bool) -> bool:
        if risk is not None and risk.outside:
            return False
        if p.hp_frac < 0.34:
            return False
        d = self._dist(p.pos, target.pos)
        if d > self._usable_reach(p):
            return False
        if target.kind == "npc":
            return True
        if report.engaging_count >= 3 and p.hp_frac < 0.62:
            return False
        if report.near_pressure > 2.0 + 1.6 * p.hp_frac:
            return False
        if not target.seen_now and d > 120.0:
            return False
        if endgame and p.hp_frac < 0.50 and target.hp_frac > 0.60 and d > 35.0:
            return False
        if (p.hp_frac < 0.50 and target.hp_frac > 0.85 and d > 90.0
                and not self._target_aiming(report, target.id)):
            return False
        return True

    def _target_aiming(self, report: ThreatReport, tid: int) -> bool:
        for th in report.threats:
            if th.id == tid:
                return th.aiming_at_us
        return False

    def _is_endgame(self, p: Perception) -> bool:
        if p.zone is not None and p.zone.radius <= 90.0:
            return True
        if p.alive <= 4:
            return True
        return p.t >= 400.0

    def _resource_ok(self, p: Perception, report: ThreatReport,
                     res: ResourceGoal, endgame: bool) -> bool:
        d = self._dist(p.pos, res.pos)
        near = report.near_pressure
        if p.hp_frac < 0.34:
            return res.kind == "cube" and d < 22.0 and near <= 0.0
        if near > 0.9 and d > 30.0:
            return False
        if endgame:
            if res.kind in ("crate", "drone"):
                return False
            if res.kind == "drop":
                return p.hp_frac < 0.70 and d < 60.0 and near < 0.6
            return d < 45.0 and near <= 0.2
        if res.kind == "drop":
            return d < 160.0 and (p.hp_frac < 0.85 or near < 0.5)
        if res.kind == "crate":
            return near <= 0.0 and d < 100.0
        if res.kind == "drone":
            return near <= 0.4 and d < 55.0
        return True

    # ------------------------------------------------------------------ targeting
    def _pick_target(self, p: Perception, report: ThreatReport,
                     zone: Optional[ZoneInfo]) -> Optional[Target]:
        target = self.combat.choose_target(p, report, zone, preferred=self.target_id)
        if target is not None:
            self.target_id = target.id
            self._target_pos = target.pos
            if target.seen_now:
                self._target_t = p.t
            return target
        for tr in p.npcs:
            if p.recent_damage(tr.id) > 0.2 or tr.distance_to(p.pos) < 22.0:
                self.target_id = tr.id
                self._target_pos = tr.pos
                self._target_t = p.t
                return self.combat.make_target(tr, "npc")
        if self.target_id is not None and p.t - self._target_t > 1.0:
            self.target_id = None
        return None

    def _pick_resource(self, p: Perception, report: ThreatReport,
                       zone: Optional[ZoneInfo]) -> Optional[ResourceGoal]:
        goal = self.resources.choose(p, report, zone, self.world, allow_crates=p.t < 300.0)
        if p.drops:
            dg = self.resources.drop_goal(p, report, zone, self.world)
            if dg is not None and (goal is None or dg.score >= goal.score):
                goal = dg
        if goal is not None and goal.kind == "pickup" and self._skip_pickups.get(goal.id, 0.0) > p.t:
            return None
        return goal

    def _resource_target(self, p: Perception, res: ResourceGoal) -> Optional[Target]:
        if res.kind == "crate":
            for c in p.crates:
                try:
                    if int(c.get("id", -1)) == res.id:
                        return self.combat.crate_target(c)
                except (TypeError, ValueError):
                    continue
        if res.kind == "drone":
            for tr in p.npcs:
                if tr.id == res.id:
                    return self.combat.make_target(tr, "npc")
        return None

    def _usable_reach(self, p: Perception) -> float:
        best = 0.0
        for slot in (0, 1):
            w = p.weapons[slot]
            if not w:
                continue
            kind = str(w.get("kind"))
            if kind == "gun" and (w.get("ammo") or 0) <= 0:
                continue
            if kind == "lightning" and p.energy < 5.0:
                continue
            stats = p.weapon_stats(slot)
            best = max(best, float(stats.get("range", _REACH.get(kind, 0.0))) * 0.95)
        return best

    def _best_weapon_kind(self, p: Perception, target: Target) -> str:
        d = self._dist(p.pos, target.pos)
        best_kind = ""
        best = -1.0
        for slot in (0, 1):
            w = p.weapons[slot]
            if not w:
                continue
            kind = str(w.get("kind"))
            stats = p.weapon_stats(slot)
            rng = float(stats.get("range", _REACH.get(kind, 100.0)))
            if kind == "gun":
                if (w.get("ammo") or 0) <= 0:
                    continue
                score = 1.0 if d <= rng * 0.95 else 0.0
            elif kind == "laser":
                score = 1.2 if d <= rng * 0.95 else 0.1
            elif kind == "lightning":
                score = 1.5 if d <= rng else 0.2
            elif kind == "mortar":
                score = 0.8 if 40.0 <= d <= min(rng, 200.0) else 0.1
            else:
                score = 0.0
            if score > best:
                best = score
                best_kind = kind
        return best_kind or "gun"

    # ------------------------------------------------------------------ combat
    def _want_combat(self, p: Perception, state: str, target: Target) -> bool:
        if state in ("ENGAGE", "DISENGAGE", "ENDGAME"):
            return True
        if target.kind in ("crate", "npc"):
            return True
        if state in ("HOLD", "ROTATE") and self._dist(p.pos, target.pos) < 120.0:
            return True
        return False

    def _solve(self, p: Perception, target: Target, report: ThreatReport, state: str,
               risk: Optional[ZoneRisk], reserve: float) -> Solution:
        allow_mortar = (state in ("ENGAGE", "ENDGAME") and p.grounded
                        and report.near_pressure < 2.0)
        aggressive = state in ("ENGAGE", "ENDGAME")
        tracks = [th.track for th in report.threats[:4]]
        sol = self.combat.solve(p, target, self.world, reserve, tracks,
                                allow_mortar=allow_mortar, aggressive=aggressive)
        w = p.weapons[sol.slot] if 0 <= sol.slot < 2 else None
        charging = bool(w and w.get("kind") == "laser" and float(w.get("charge") or 0.0) > 0.02)

        if not target.seen_now:
            sol.fire = bool(sol.kind == "laser" and charging)
            return sol

        if sol.kind == "laser":
            urgent = state in ("ROTATE", "LOOT", "CONTEST", "DISENGAGE") or (
                risk is not None and risk.urgency >= 1.0)
            if urgent:
                sol.fire = False
        if sol.kind == "mortar" and state not in ("ENGAGE", "ENDGAME"):
            sol.fire = False
        return sol

    def _apply_solution(self, p: Perception, action: Dict[str, object], sol: Solution,
                        state: str, target: Target, report: ThreatReport) -> None:
        if sol.slot < 0:
            action["aim"] = list(self._idle_aim(p, action["move"]))
            return
        action["aim"] = [math.atan2(math.sin(sol.aim[0]), math.cos(sol.aim[0])), sol.aim[1]]
        fire = action["fire"]
        if isinstance(fire, list) and sol.slot < len(fire):
            fire[sol.slot] = bool(sol.fire)
        # Keep the gun working while the laser charges in the other slot.
        if sol.kind == "laser" and sol.fire and target.seen_now and state in ("ENGAGE", "ENDGAME"):
            g = p.slot_of("gun")
            if g is not None and g != sol.slot:
                tracks = [th.track for th in report.threats[:4]]
                gs = self.combat.solve_slot(p, target, self.world, g, tracks)
                if gs is not None and gs.fire and isinstance(fire, list):
                    fire[g] = True

    def _laser_trigger(self, p: Perception, action: Dict[str, object], state: str,
                       sol: Optional[Solution]) -> None:
        fire = action["fire"]
        if not isinstance(fire, list):
            return
        for slot in (0, 1):
            w = p.weapons[slot]
            if not w or w.get("kind") != "laser" or slot >= len(fire):
                continue
            if float(w.get("charge") or 0.0) <= 0.02:
                continue
            if sol is not None and sol.slot == slot:
                continue
            if (state != "DISENGAGE" and self.target_id is not None
                    and p.t - self._target_t <= 2.5 and self._target_pos is not None):
                fire[slot] = True
                tp = self._target_pos
                action["aim"] = list(aim_to(p.muzzle(), (tp[0], tp[1], tp[2] + 4.5)))
            else:
                fire[slot] = False  # let go: the beam fires now and full speed returns

    def _idle_aim(self, p: Perception, move: Sequence[float]) -> Vec2:
        if self.target_id is not None and self._target_pos is not None and p.t - self._target_t < 2.6:
            tp = self._target_pos
            return aim_to(p.muzzle(), (tp[0], tp[1], tp[2] + 4.5))
        if math.hypot(move[0], move[1]) > 0.15:
            return (math.atan2(move[1], move[0]), 0.0)
        base = 0.0
        if p.zone is not None:
            base = bearing(p.pos, p.zone.center)
        if p.sound_bearing is not None and p.t - p.sound_t < 2.0:
            base = p.sound_bearing
        sweep = math.sin(p.t * 0.55) * math.radians(55.0)
        return (wrap(base + sweep), 0.0)

    # ------------------------------------------------------------------ movement
    def _goal(self, p: Perception, state: str, target: Optional[Target],
              report: ThreatReport, risk: Optional[ZoneRisk],
              res: Optional[ResourceGoal]) -> Optional[Vec2]:
        zone = p.zone
        if state == "DISENGAGE":
            trs = [th.track for th in report.threats[:3]]
            if zone is not None:
                return self.nav.choose_retreat_point(p, zone, trs)
            return self._flee_point(p, report)
        if state == "ROTATE":
            if zone is not None and risk is not None:
                return self.nav.zone_goal(p, zone, risk)
            return None
        if state == "ENGAGE" and target is not None:
            return self._engage_goal(p, target, risk)
        if state in ("LOOT", "CONTEST") and res is not None:
            return self._resource_goal(p, res)
        if zone is not None:
            trs = [th.track for th in report.threats[:3]]
            return self.nav.choose_hold_point(p, zone, trs, state)
        return None

    def _engage_goal(self, p: Perception, target: Target, risk: Optional[ZoneRisk]) -> Optional[Vec2]:
        if not target.seen_now and p.t - self._target_t > 2.0:
            return None
        d = self._dist(p.pos, target.pos)
        if d < 1e-3:
            return None
        kind = self._best_weapon_kind(p, target)
        lo, hi = _RANGE_BAND.get(kind, (65.0, 150.0))
        ux = (p.pos[0] - target.pos[0]) / d
        uy = (p.pos[1] - target.pos[1]) / d
        if d > hi:
            goal = (target.pos[0] + ux * hi * 0.92, target.pos[1] + uy * hi * 0.92)
        elif d < lo:
            goal = (target.pos[0] + ux * lo * 1.05, target.pos[1] + uy * lo * 1.05)
        else:
            if p.t - self.strafe_t > 1.5:
                self.strafe_side = -self.strafe_side
                self.strafe_t = p.t
            sx = -uy * self.strafe_side
            sy = ux * self.strafe_side
            goal = (p.pos[0] + sx * 7.0, p.pos[1] + sy * 7.0)
        if risk is not None and risk.outside and p.zone is not None:
            goal = p.zone.safe_point(goal, margin=6.0)
        return goal

    def _resource_goal(self, p: Perception, res: ResourceGoal) -> Optional[Vec2]:
        d = self._dist(p.pos, res.pos)
        if res.kind == "crate":
            if d <= 55.0:
                return None
            ux = (p.pos[0] - res.pos[0]) / d
            uy = (p.pos[1] - res.pos[1]) / d
            return (res.pos[0] + ux * 55.0, res.pos[1] + uy * 55.0)
        if res.kind == "drone":
            if d <= 40.0:
                return None
            ux = (p.pos[0] - res.pos[0]) / d
            uy = (p.pos[1] - res.pos[1]) / d
            return (res.pos[0] + ux * 40.0, res.pos[1] + uy * 40.0)
        return res.pos

    def _flee_point(self, p: Perception, report: ThreatReport) -> Optional[Vec2]:
        if not report.threats:
            return None
        th = report.threats[0].track
        dx = p.pos[0] - th.pos[0]
        dy = p.pos[1] - th.pos[1]
        n = math.hypot(dx, dy)
        if n < 1e-3:
            dx, dy, n = 1.0, 0.0, 1.0
        return (p.pos[0] + dx / n * 30.0, p.pos[1] + dy / n * 30.0)

    def _strafe(self, p: Perception, report: ThreatReport) -> MoveResult:
        if p.t - self.strafe_t > 1.3:
            self.strafe_side = -self.strafe_side
            self.strafe_t = p.t
        th = report.threats[0].track
        ux = p.pos[0] - th.pos[0]
        uy = p.pos[1] - th.pos[1]
        n = math.hypot(ux, uy)
        if n < 1e-3:
            return MoveResult((self.strafe_side, 0.0))
        return MoveResult((-uy / n * self.strafe_side, ux / n * self.strafe_side))

    def _want_boost(self, p: Perception, state: str, risk: Optional[ZoneRisk]) -> bool:
        if not p.grounded:
            return False
        if state == "DISENGAGE":
            return p.energy > 14.0
        if risk is not None and risk.boost and risk.urgency >= 1.5:
            return p.energy > 8.0
        if state == "ROTATE" and risk is not None and risk.boost:
            return p.energy > 22.0
        return False

    def _energy_reserve(self, p: Perception, report: Optional[ThreatReport]) -> float:
        reserve = 16.0
        if report is not None and report.close_count > 0:
            reserve += 8.0
        if p.hp_frac < 0.5:
            reserve += 8.0
        return min(45.0, reserve)

    # ------------------------------------------------------------------ item use
    def _apply_take(self, p: Perception, action: Dict[str, object], res: ResourceGoal) -> None:
        if res.kind != "pickup":
            return
        for pk in p.pickups:
            try:
                if int(pk.get("id", -1)) != res.id:
                    continue
                pos = pk.get("pos") or (0, 0, 0)
                dh = math.hypot(float(pos[0]) - p.pos[0], float(pos[1]) - p.pos[1])
                dz = abs(float(pos[2]) - p.pos[2])
            except (TypeError, ValueError, IndexError):
                continue
            if dh <= 3.4 and dz <= 3.8:
                action["take"] = int(res.id)
                if res.swap_slot is not None:
                    action["drop"] = int(res.swap_slot)
            return

    def _maybe_reload(self, p: Perception, action: Dict[str, object]) -> None:
        fire = action.get("fire")
        if isinstance(fire, list) and any(fire):
            return
        g = p.slot_of("gun")
        if g is None:
            return
        w = p.weapons[g]
        if not w:
            return
        if float(w.get("reload") or 0.0) > 0.0:
            return
        ammo = float(w.get("ammo") or 0.0)
        mag = float(p.weapon_stats(g).get("mag", 12.0))
        if ammo <= mag * 0.34:
            action["reload"] = True

    @staticmethod
    def _dist(a: Sequence[float], b: Sequence[float]) -> float:
        return math.hypot(a[0] - b[0], a[1] - b[1])
