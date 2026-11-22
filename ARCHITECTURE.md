# CtrlAltWin v1 Architecture

Protocol: `mechfight/1`. Runtime: 1 CPU, 512 MB, read-only root (writable `/tmp`), static map at
`/arena`, stdlib-only Python 3.13. Decisions arrive every 2 ticks (10 Hz) with a 50 ms budget.

## Data flow

```
stdin ──> mechfight.py (official wrapper, unmodified)
              │  hello
              ▼
          bot.Bot ──> world_model.WorldModel(hello)      map load before `ready`
              │  obs
              ▼
          strategy.Strategy.decide(obs)
              │
              ├─ perception.Perception.update(obs)        tracks, zone, danger, events
              ├─ threat_assessment.assess(p, world)       ThreatReport
              ├─ zone risk + projectile dodge
              ├─ target selection (combat/choose_target)
              ├─ resource selection (resource_manager)
              ├─ state machine (LOOT/ROTATE/HOLD/ENGAGE/DISENGAGE/CONTEST/ENDGAME)
              ├─ combat solvers (gun/laser/lightning/mortar)
              ├─ navigation (steer / A* / hold / retreat)
              └─ action_safety.ActionValidator.validate(p, action)
              │  action
              ▼
          stdout one JSON line, tick echoed
```

On any exception inside the strategy, `Bot.act` re-sends the previous action with `fire` cleared and
the new tick; if that too fails it emits `{"tick": ...}`. The wrapper still owns stdout framing.

## Modules

### `bot.py`
Builds the world model (logs validation to stderr) and the strategy, then returns actions. Total
failure containment: no exception escapes to the wrapper.

### `world_model.py`
- Parses `hello.map` lazily; on any error `available=False` and every query degrades gracefully
  (open space, direct paths) so the bot still runs without a map.
- Prism index over the map: analytic `point_solid`, `terrain_height`, exact slab-clipped
  `los_blocked` and a cheaper `los_blocked_fast` for threat scoring.
- 3 m walkability grid (`cell_blocked`, `walkable`), nearest-walkable repair, segment clearance.
- `find_path`: A* with danger penalties, zone-edge penalties, expansion cap (900) and path
  smoothing; returns `[goal]` when unavailable.
- `cover_cells` precomputed cells adjacent to solid geometry (used for hold/retreat points).
- `danger_field` rasterises threat positions for path cost.

### `perception.py`
- `Track`: id, kind (mech/NPC), position, velocity, aim, weapon slot states, seen flags.
  Tracks survive up to `memory_s` without an observation and are marked `seen_now=False`, so stale
  targets are never treated as visible.
- `ZoneInfo`: parses the zone phase table, interpolates the current circle, predicts the next
  circle, scores risk (`outside`, `urgency`, `dist_edge_next`) and produces `safe_point` /
  `next_entry` positions.
- `projectile_danger`: perpendicular dodge for gun bullets, landing-point dodge for mortar.
- Event handling: damage attribution per attacker, `take_failed`, drops/kills, NPC separation.

### `threat_assessment.py`
Scores visible/remembered mechs by distance, LOS, facing toward us, weapon type (charging laser is
an emergency), recent damage dealt to us, and energy/HP state. Produces a sorted report with
`engaging_count`, `near_pressure`, `primary` and the raw `Track` objects.

### `navigation.py`
- Local steering: probe candidate headings against `point_free`/grid with an 8 m lookahead,
  prefer the goal direction, an open side, and a committed slide side.
- Stuck detection by displacement over 1 s; escalation levels trigger A* (`find_path`),
  jump, jet, then a random break-off with a temporary avoid cell.
- `zone_goal`: move inside/into the next circle before it closes, otherwise a cover hold point.
- `choose_hold_point`: samples cover cells and ring positions inside the current/next circle,
  scores distance, ring proximity and threat distance, and runs up to three exact LOS checks
  against the primary threat.
- `choose_retreat_point`: cover point inside the zone that breaks the primary threat's sightline.

### `combat.py`
- `Target` with `seen_now` and predicted position; `choose_target` prefers seen, close, weak,
  recently-damaging mechs and can prefer a resource carrier.
- `best_slot` / `solve`: chooses the best usable slot; `solve_slot` re-solves a specific slot.
- Gun: lead by bullet speed, range check, cooldown/mag/reload discipline.
- Laser: hold to charge, release when charged and aligned; cancel when the target is lost;
  release before auto-fire; cookoff risk for the mortar holder.
- Lightning: range/cone/energy gate with a reserve.
- Mortar: ballistic low/high-arc solve, splash self-safety and arc clearance.
- `_projected_error` gates fire on actual aim alignment, not just solver output.

### `resource_manager.py`
- Candidates: weapon pickups (prefer useful kinds, avoid duplicates), cubes, crates, NPC drones,
  supply drops. Each is rejected when outside the zone, unreachable on the grid, too far, near a
  threat, or when travel time exceeds the value (drops also require arrival before fall).
- `pick_upgrade`: fixed preference list (plating/armor/nanites > servos/jets > capacitor/reactor >
  weapon-specific), returns the offer index.
- Returns `ResourceGoal(kind, pos, id, crate/slot metadata)` consumed by the strategy.

### `strategy.py`
Decision pipeline (per observation):

1. Update perception, track HP loss, skip `take_failed` pickups for 3 s.
2. Threat report; zone risk; projectile dodge.
3. Pick target and resource goal.
4. Choose state with fixed priority:
   `DISENGAGE` (low HP + close pressure) → `ROTATE` (outside zone or urgent) → `ENGAGE`
   (worthwhile target) → `LOOT`/`CONTEST` (safe resource) → `ENDGAME`/`HOLD`.
   Soft states have a 0.35 s anti-flap guard.
5. Solve combat if the state wants it; otherwise idle aim.
6. Pick a navigation goal per state; move (or strafe at a hold point); apply dodge override.
7. Boost/jump/jet rules with an energy reserve; laser trigger; take; reload.
8. Validate.

### `action_safety.py`
Single choke point for action shape: unit-clamps `move`, wraps yaw, clamps pitch, drops fire on
empty slots, bounds `pick` by offer length, bounds `take` by pickup radius/height and `drop` by
slot occupancy, requires a gun for `reload`, and trims debug payloads to the `hello.config.debug`
caps (lines/points/text/chars/bytes). Any internal error returns a minimal valid action.

## State machine and arbitration

| Priority | Concern | Mechanism |
| --- | --- | --- |
| 1 | Survivorship | `DISENGAGE` on HP loss + pressure; retreat cover point; energy reserve |
| 2 | Zone safety | `ROTATE` when outside/urgent; goals clamped inside current/next circle |
| 3 | Emergency threat | charging laser / close mech overrides loot; dodge beats goal movement |
| 4 | Positioning | cover hold points, LOS breaking, strafing, stuck recovery |
| 5 | Combat opportunity | `ENGAGE` only when solvable and not suicidal; short engagements |
| 6 | Resources | `LOOT`/`CONTEST` only when zone-safe and low pressure |

## Performance model

- Map load once before `ready` (≈ 0.17 s synthetic grid build; ≈ 1.7–3.2 s wall clock in the
  subprocess incl. Python start and map parse).
- Steady-state decision p50 ≈ 1.6–2.0 ms, p90 ≈ 3.8–5.7 ms, p99 ≈ 9–12 ms on the development
  machine; A* is capped and only runs after the local steering reports stuck.
- Hold/retreat point selection is cached briefly; threat LOS is limited to short candidate lists.
- No allocation-heavy loops per decision; the heaviest repeated work is cover-cell sampling,
  capped by stride.

## Safety properties

- The map is treated as read-only; nothing is written anywhere.
- No network access, no subprocesses, no third-party packages.
- Unknown/missing fields degrade to defaults rather than raising; the validator is total.
- The official wrapper and starter bot are byte-identical to the submitted baseline.
