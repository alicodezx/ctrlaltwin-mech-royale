# CtrlAltWin v1 — Build and Test Report

Scope: protocol compliance and latency of the v1 bot versus the submitted starter baseline.
**No gameplay-superiority claim is made.** The supplied replay is non-reactive (observations do not
depend on actions), and no reactive match simulator or practice-match results were available here.
See "Evidence limits" for exactly what is and is not verified.

## 1. Baseline vs v1 — protocol and performance only

Both bots were run through the official `check.py` (recorded `hello`, 300 observations, `end`).
The checker measures conformance and per-decision answer time on this development machine; the
match container is 1 CPU and may differ.

| Metric | Starter baseline (`example_bot.py`) | v1 (`bot.py`) |
| --- | --- | --- |
| Checker verdict | PASS (5/5 runs earlier) | PASS (all runs incl. 3 after final edits) |
| Observations answered in time | 300 / 300 | 300 / 300 |
| Missed 50 ms budget | 0 | 0 |
| Late answers | 0 | 0 |
| Wrong tick / bad lines | 0 / 0 | 0 / 0 |
| Dropped older queued observations | 0 | 0 |
| Ready time | ≈ 0.15 s | ≈ 1.7–3.2 s (world model build) |
| Answer p50 | 1.4 ms | 1.6–2.0 ms |
| Answer p90 | 2.4 ms | 3.8–5.7 ms |
| Answer p99 | 3.7 ms | 8.7–11.8 ms (one noisy run: 18.9) |
| Answer max | 4.4 ms | 9.2–17.3 ms (same noisy run: 70.4) |
| Debug drawings | 308 (under caps) | 0 (debug drawing not used) |
| Protocol wrapper | unmodified | same file, byte-identical |
| Entry point | `example_bot.py` | `bot.py` |

The single noisy run (p99 18.9, max 70.4 ms) still reported 0 missed and 0 late answers, so the
spike is host scheduling, not a missed deadline. Two back-to-back reruns produced p99 8.7 / 11.8 ms
and max 9.2 / 17.3 ms. No code path performs work proportional to match length.

Unit-test performance case (synthetic 100-decision sequence with mech, projectiles, loot, crate,
drop and shrinking zone):

- world init: ≈ 170–215 ms (synthetic flat/walled arenas)
- p50 ≈ 0.9–1.4 ms, p90 ≈ 4.7–7.5 ms, p99 ≈ 7.8–11.3 ms, max ≈ 9.5–11.3 ms
- validator latency < 5 ms

Recorded-stream profiling (300 non-reactive decisions): world init ≈ 1.73 s; p50 ≈ 0.4–0.6 ms,
p90 ≈ 2.5–3.4 ms, p99 ≈ 7.5–8.3 ms. State distribution: HOLD 202, ENGAGE 73, LOOT 21,
DISENGAGE 4; 49 firing decisions. No pickups were taken because the recorded stream contains no
reachable loot, so loot/upgrade paths are covered only by synthetic unit tests.

## 2. Unit tests

Command (from `submission-baseline/`):

```sh
python -m unittest discover -s tests -t .
```

Result: **73 tests, 0 failures, 0 errors** (also passes with `-W error::ResourceWarning`).

Coverage by file:

- `test_world_model.py` — map load, LOS through/over/around walls, path around wall, unreachable
  goal, degraded mode without a map.
- `test_perception.py` — track memory and `seen_now`, refresh, NPC/mech separation, damage
  attribution, zone interpolation/risk/safe points, bullet and mortar dodges, offers/pickups.
- `test_combat.py` `ThreatTests` — charging-laser priority, LOS effect on threat score,
  recent-damage effect.
- `test_navigation.py` — straight steering, arrival threshold, wall steering, stuck escalation to
  jump/jet, hold/retreat points remain inside the zone.
- `test_combat.py` — gun fire/lead/range, laser cancel/hold, laser with lightning track, lightning
  energy/range/cone gates, mortar arc and too-close refusal.
- `test_strategy.py` — zone rotation, disengage, dodge, engage + gun, laser hold/release timing,
  lightning energy gate, gun+laser regression, NPC self-defense, mortar first/second decision,
  upgrade pick, weapon take/swap, far-pickup approach, crate LOOT, drop CONTEST, low-HP skip,
  empty/garbage observations, determinism.
- `test_action_safety.py` — move clamp, aim wrap/clamp, fire slots, pick bounds, take radius,
  drop slot, reload gun requirement, debug caps and byte trimming, total validator.
- `test_bot_protocol.py` — real subprocess handshake, `ready` name `ctrlaltwin-v1`, one action per
  observation, tick echo, unit move vectors, clean exit.
- `test_performance.py` — init < 20 s, p99 < 25 ms, max < 50 ms, validator < 5 ms.

## 3. Changed files (relative to the baseline commit)

New: `bot.py`, `strategy.py`, `perception.py`, `world_model.py`, `threat_assessment.py`,
`navigation.py`, `combat.py`, `resource_manager.py`, `action_safety.py`, `tests/`,
`ARCHITECTURE.md`, `REPORT.md`, `LICENSE`, `.gitignore`.

Modified: `README.md` (v1 documentation), `Dockerfile` (entrypoint `python3 -u /bot/bot.py`),
`.dockerignore` (exclude `tests/` and bytecode).

Unchanged: `mechfight.py`, `example_bot.py` (byte-identical to the kit/baseline); `STRATEGY.md`
edited only to record implementation status.

## 4. Implemented

- Deterministic world model from `hello.map`: prism index, terrain height, solidity, fast/exact
  LOS, 3 m nav grid, danger-aware bounded A*, path smoothing, cover-cell list.
- Perception: tracks with short memory, seen/stale distinction, NPC separation, damage
  attribution, event handling, zone phase prediction and risk, bullet/mortar dodging.
- Threat assessment: distance/LOS/facing/weapon-state/damage scoring, charging-laser emergency,
  pressure and engaging counts.
- Navigation: probe steering with committed slide side; stuck detection with A* → jump → jet →
  random break escalation; zone rotation and next-circle entry; cover hold and retreat points that
  break the primary threat's sightline.
- Combat: target selection; per-weapon solvers for gun (lead, range, mag/reload), laser
  (charge/release/cancel/cookoff), lightning (range/cone/energy), mortar (ballistic arc, splash
  self-safety, clearance); fire gated on actual aim and energy reserve.
- Resources: weapon picks and swaps, cubes, crates, NPC drones, supply drops with zone, threat,
  reachability and travel-time filters; upgrade preference list; `take_failed` cooldown.
- Strategy: seven-state machine with the required priority order, anti-flap guard, dodge override,
  boost/jump/jet energy rules, reload discipline, safe fallback.
- Action safety: total validator for every protocol field and debug caps.
- Engineering: stdlib only, no writes outside `/tmp`, no network, map-missing degradation,
  strategy exception containment, deterministic seeding.
- Tests: 73 unit tests including synthetic observations for mechanics absent from the replay, a
  real protocol round-trip, and a performance budget test.

## 5. Not implemented / not verified

- **No gameplay-strength evidence.** No reactive simulator and no practice-match results were
  available; placement improvement is unmeasured. The bot could still play worse than the starter
  in real matches; thresholds in `STRATEGY.md` are design intent, not tuned against matches.
- No Docker image could be built in this environment (Docker unavailable); the Dockerfile change is
  limited to the entrypoint and was not container-tested.
- Pathfinding is a 2D 3 m grid: vertical routes (bridges/ramps under overhangs) are approximated.
  True collision sliding and jump/boost trajectories are not simulated.
- Mortar indirect fire relies on the analytic arc/clearance checks; it does not model moving
  targets beyond the lead solution.
- No learned/adaptive tuning; constants are fixed. No opponent modelling beyond observed state.
- Debug drawing is implemented in the validator but not used by the strategy (0 drawings).
- No multi-observation planning or explicit third-party prediction; threat handling is reactive.
- Upgrade preference is a fixed list; it does not evaluate the current loadout or offer rarity.

## 6. Exact commands

```sh
# unit tests
cd submission-baseline
python -m unittest discover -s tests -t .

# official protocol checker (kit root)
python mechfight-kit/mechfight-kit/check/check.py -- python -u submission-baseline/bot.py

# baseline for comparison
python mechfight-kit/mechfight-kit/check/check.py -- python -u submission-baseline/example_bot.py

# image (requires Docker, not run here)
docker build -t mech-royale-v1 .
```

## 7. Limitations and next steps

1. Run the 48 official practice matches and record mean placement; compare with the baseline.
   Only then tune engagement/disengage, loot and hold-point thresholds.
2. Build a small reactive simulator from the protocol (or use an organizer harness) to test
   survival scenarios and regressions, especially zone-edge deaths and third-party fights.
3. Benchmark on a 1-CPU container; verify map load and p99 under the real limits.
4. Add vertical/link-aware pathing only if match data shows bridge/ramp deaths.
5. If practice results show the bot is too passive or too aggressive, adjust the state priority
   thresholds rather than adding features.
