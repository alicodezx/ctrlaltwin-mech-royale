# CtrlAltWin v1 — Mech Royale 2026 Bot

Entry for the **JoinBuilders 2026 Mech Royale Buildathon**: a bot that pilots one mech in a
16-mech free-for-all using protocol `mechfight/1`.

This branch (`v1-tactical-intelligence`) replaces the starter strategy with a survival-first
tactical bot. The official protocol wrapper `mechfight.py` and the starter `example_bot.py` are
kept byte-identical; the submitted baseline commit is untouched.

## What the bot does

The goal is **expected placement**, not kills: 15 points for 1st, 0 for last, kills score only
through threat removal and loot. The bot therefore avoids fair fights, respects the closing zone,
and spends the mid-game collecting weapons/upgrades where that is safe.

- **World model** — loads `hello.map` (`arena/v2/world.json` + `terrain.i16`), builds a 3 m
  walkability grid, cover-cell list, prism index for exact line-of-sight, and bounded A*.
- **Perception** — tracks mechs/NPCs with short memory, alerts, hits/damage attribution, zone
  phase interpolation, incoming-bullet danger, mortar landing prediction.
- **Threat assessment** — scores every known mech by distance, visibility, facing, weapon state
  (laser charging is treated as an immediate threat) and recent damage history.
- **Navigation** — local probe steering around geometry, committed slide side, stuck escalation
  (repath → jump → jet → random break), cover hold points and retreat points inside the zone.
- **Combat** — target selection, lead/ballistic solvers per weapon, reachability and energy
  gating, laser charge/release discipline, mortar arc checks, gun reload discipline.
- **Resource manager** — weapon pickups, cubes, crates, NPC drones and supply drops filtered by
  zone safety, threat proximity and travel time; upgrade picks by preference order.
- **State machine** — `LOOT / ROTATE / HOLD / ENGAGE / DISENGAGE / CONTEST / ENDGAME` with fixed
  arbitration: survivorship > zone safety > emergency threat > positioning > combat > resources.
- **Action safety** — every action passes through a total validator that clamps/repairs fields
  and returns a minimal valid action on any internal error.

## Repository layout

| File | Purpose |
| --- | --- |
| `bot.py` | Entry point: builds world model + strategy, catches failures and falls back |
| `mechfight.py` | Official protocol wrapper (unmodified) |
| `example_bot.py` | Original starter bot (unmodified, kept for reference) |
| `strategy.py` | State machine, arbitration, action composition |
| `perception.py` | Observation parsing, tracks, zone model, dodging |
| `world_model.py` | Map loading, grid, LOS, pathfinding, cover cells |
| `threat_assessment.py` | Per-mech threat scoring and report |
| `navigation.py` | Local steering, stuck recovery, hold/retreat points |
| `combat.py` | Aim/fire solvers for gun, laser, lightning, mortar |
| `resource_manager.py` | Loot, crates, drops, drones, upgrade picks |
| `action_safety.py` | Action validator and debug-caps enforcement |
| `tests/` | 73 unit tests (stdlib `unittest`, synthetic observations) |
| `Dockerfile` | `python:3.13-slim`, entrypoint runs `bot.py` unbuffered |
| `ARCHITECTURE.md` | Module map, data flow, decision pipeline |
| `STRATEGY.md` | Verified mechanics and design rationale |
| `REPORT.md` | Baseline-vs-v1 protocol/performance report, implemented list |

Dependencies: **Python standard library only** (the image uses 3.13). No `requirements.txt`.

## Build the submission image

```sh
docker build -t mech-royale-v1 .
```

## Run unit tests

From this directory:

```sh
python -m unittest discover -s tests -t .
```

The suite covers the world model, perception, threat assessment, navigation, combat solvers,
resource selection, action validation, strategy decisions on synthetic observations, protocol
round-trip, and a performance budget check.

## Protocol check

The conformance checker ships with the official bot kit (not included here). From the kit root:

```sh
python check/check.py -- python -u /path/to/this/repo/bot.py
```

Run the image the way a match does (the scorer's pre-match gate):

```sh
python check/check.py --map-dir /arena -- docker run --rm -i --network none --cpus 1 \
  --memory 512m --read-only --tmpfs /tmp:rw,size=64m --user 65534:65534 \
  -v "/path/to/bot-kit/arena:/arena:ro" mech-royale-v1
```

The checker replays a recorded `hello`, 300 observations and `end`. It verifies readiness, schema-
valid actions with the correct tick, the 50 ms budget, newest-observation handling, debug caps and
a clean exit. **It is not a simulation**: observations do not react to actions, so it measures
protocol compliance and latency, not skill.

## Status

- Protocol conformance: PASS — 300/300 answered, 0 missed, 0 wrong ticks, 0 bad lines.
- Decision time on the development machine: p50 ≈ 1.6–2.0 ms, p90 ≈ 3.8–5.7 ms,
  p99 ≈ 9–12 ms, typical max < 20 ms against the 50 ms budget.
- World model init: ≈ 1.7–3.2 s of the 20 s readiness window.
- Gameplay strength is **not** claimed here. Scores come from the 48 practice matches run by the
  scorer; `REPORT.md` lists what has and has not been verified.

## Submission checklist

- Repository ≤ 20 MB at the submitted commit; `Dockerfile` at the repository root.
- Image builds on `linux/amd64` with network on, within 10 minutes.
- Only the v1 branch is committed locally; no push without approval.
