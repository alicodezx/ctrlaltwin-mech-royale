# CtrlAltWin v1 — Tactical Intelligence Strategy

Status: **implemented (v1)**. The design below is realized in `bot.py` and the modules listed in
`ARCHITECTURE.md`; protocol and performance evidence is in `REPORT.md`. Gameplay strength is not
yet measured (no reactive simulator or practice-match results).
Branch: `v1-tactical-intelligence` (local, not pushed). Baseline `main` is untouched.

Sources of truth (in priority order):

1. `protocol.md` — authoritative rules for protocol `mechfight/1`.
2. `mechfight.schema.json` — message shapes.
3. The official kit's recorded `hello.config` — live numeric values (quoted below).
4. `arena/v2/world.json` + `arena/v2/terrain.i16` — static map.
5. `check/check.py` — what the pre-match gate actually enforces.

Where protocol prose and kit comments disagree (e.g. stderr visibility), the protocol wins.

---

## 0. Objective and scoring model

- 16-mech free-for-all, no teams. Placement is **death order**: last alive = 1st, first to die = 16th.
  Same-tick deaths share a placement. At the clock cap, survivors are ranked by HP.
- Points per match: `16 - place` (15 for a win, 0 for dying first). Score = mean over the 48
  practice matches a submission plays.
- Kills do **not** score directly. They matter only through cubes/economy and threat removal.
- Therefore the policy must optimize **expected placement**, not damage or kills. A fight is only
  worth taking if it raises the probability of a high placement (or prevents a larger loss).
- `obs.alive` is the placement we would get if we died now — a continuous survival metric.
- Long fights invite third parties; early aggression is usually negative expected value in a
  16-player field.

---

## 1. Verified mechanics

### 1.1 Match loop and timing

- Server-authoritative, lockstep. Simulation 20 Hz; each bot decides every 2 ticks (10 decisions/s).
- 50 ms wall-clock budget per decision; actions must echo `tick`; stale ticks are dropped.
- Missing an answer: continuous fields (`move`, `boost`, `aim`) coast; one-shots (`fire`, `jump`,
  `reload`, `pick`, `take`, `drop`) are dropped. After 1 s of missed answers the mech idles.
- A crashed process idles for the rest of the match.
- The wrapper answers only the newest observation when observations queue, injects `tick`,
  re-sends the last continuous action after an exception, and exits on `end`.
- Match cap 480 s; the zone closes to radius 0 at about 415 s (6:55).

### 1.2 Frame and body

- x = east, y = north, z = up (meters, seconds, radians). Yaw counter-clockwise from +x; pitch up-positive.
- `pos` is the feet. Mech: cylinder radius 2.5 m, height 9 m; center of mass +4.5, eyes +8,
  gun muzzle +7, mortar tube +9.5. Shots start 3.0 m out along the actual aim.
- NPC drone: radius 1.8 m, height 3.5 m.

### 1.3 Movement and energy

| Parameter | Value |
| --- | --- |
| walk / boost speed | 11.7 / 19.5 m/s |
| ground / air acceleration | 58.5 / 12 m/s² |
| jump speed / gravity | 11 m/s / 22 m/s² |
| jet acceleration / max rise | 34 m/s² / 10 m/s |
| step-up / max slope | 1.2 m / 50° |
| energy / regen / regen delay | 100 / 18 per s / 0.6 s |
| boost / jet drain | 17 / 15 per s |
| torso yaw / pitch slew | 4 / 3 rad/s |
| pitch clamp | −0.7 … 1.3 rad |

- Walls stop the inward component (you slide); ledges ≤ 1.2 m are walked; steeper is a wall unless
  you jump/jet above it; slopes > 50° cannot be climbed; the map edge is a wall; mechs push apart.
- Full energy buys roughly 60 m of climb. Changing direction takes time (acceleration).
- Aiming is slewed: shots leave along the **actual** aim, not the commanded aim. Charging a laser
  halves movement speed but not turn rate.
- Boost on the ground is speed; boost in the air is a jet. Energy stops regenerating for 0.6 s after spending.

### 1.4 Health, regeneration, armor

- 1400 HP. After 5 s without dealing or taking damage: +45 HP/s (not while outside the zone).
- Zone damage resets the regen timer and ignores armor.
- A hit from outside the 180° front arc centered on aim yaw deals ×1.5 (rear armor).
- Own mortar splash deals ×0.5 to self.
- Cube scaling at 10 cubes: +40% max HP and +40% weapon damage.

### 1.5 Weapons (from live config)

| Weapon | Key numbers | Mechanics |
| --- | --- | --- |
| gun | 45 dmg, 0.24 s cooldown, 252 m/s, 230 m range, 0.6° spread, 12-round mag, 2.4 s reload | Projectile, flat trajectory, must lead moving targets. Starter gun in slot 0; slot 1 empty at spawn. |
| mortar | 200 direct, splash 160 falling to 30% at 7 m, 2.6 s cooldown, 72 m/s, gravity 22, ttl 7 s | Ballistic; pitch sets range (≲235 m at 45°); blast knocks (16 m/s, 9 up), drains 45 energy, blocked by walls; lob over buildings; self-damage ×0.5. |
| laser | 315 full charge, 0.75 s charge, 0.15 s minimum, 35% minimum damage, 1.65 s cooldown, 280 m | Hold to charge, release to fire a hitscan beam. Full charge held 2 s auto-fires. Telegraphs `charging` and sound. Cook-off: +60% vs a mech holding a mortar. |
| lightning | 60 dmg, 0.25 s cooldown, 45 m, 30° cone, 2 chain jumps at 14 m (×0.7), 3 energy/shot | Instant, needs line of sight. "Shorts" a mech holding a laser (cancels charge, 2 s lockout). |

Weapon triangle: laser > mortar (cook-off/range), mortar > lightning (concussion/drain), lightning >
laser (short). Gun is neutral.

Time-to-kill intuition (no armor, no cubes): gun ≈ 32 hits (≈8 s with reloads); full laser ≈ 5
hits (cooldown-bound ≈7 s); lightning ≈ 24 hits (≈6 s and ~72 energy); mortar ≈ 7 direct hits.
Armor, regen and cover stretch all of these; most fights are decided by positioning and aim quality.

### 1.6 Vision and sound

- Eye at `pos.z + 8`; line of sight to center or top.
- See: within 45 m in any direction (proximity), or within 200 m inside a 120° yaw wedge centered
  on torso aim. Bodies block; canopy attenuates sight; shots still pass trees.
- Seen mechs/NPCs stay tracked for 5 s after leaving view; crates, cubes, pickups, projectiles are
  listed only while visible.
- `.charging` on an enemy is a laser telegraph. Beams (`by`, `kind`, `path`, `age`) reveal shooter
  identity and line. `damaged` events give exact bearing and attacker id (null for zone).
- Sounds: bearing with σ≈7°, distance band (near <60, mid <160, far), age. Ranges: gun 320,
  mortar/explosion 420, laser 380, laser_charge 140, lightning 160, jet/boost 90, death/drop 600,
  crate 220, npc_gun 200, step 40, reload 60. Own sounds are not echoed; sounds have no shooter id.
- Where you aim is where you see: scanning costs torso turn time and temporarily narrows awareness behind you.

### 1.7 Zone (live config, cumulative)

| Time (s) | Radius (m) | DPS outside |
| --- | --- | --- |
| 0–60 hold | 560 | 20 |
| 60–110 shrink | 560 → 380 | 20 |
| 110–155 hold | 380 | 35 |
| 155–195 shrink | 380 → 240 | 35 |
| 195–230 hold | 240 | 60 |
| 230–265 shrink | 240 → 140 | 60 |
| 265–295 hold | 140 | 100 |
| 295–325 shrink | 140 → 70 | 100 |
| 325–350 hold | 70 | 160 |
| 350–375 shrink | 70 → 25 | 160 |
| 375–390 hold | 25 | 260 |
| 390–415 shrink | 25 → 0 | 260 |

- Observations always carry the current circle, the next circle, and the shrink window; use those
  fields rather than re-deriving phases from constants.
- Zone damage ignores armor and regeneration. Late-game, being caught outside is fatal within seconds.

### 1.8 Loot, cubes, upgrades, supply drops

- 40 floor weapons (25% on roofs; kinds mortar/laser/lightning equally, never gun). `take` works
  within 4 m horizontally/vertically; a taken weapon is cold for `max(1 s, its cooldown)`.
- 36 crates (25% roofs, 300 HP): 1 cube plus 50% chance of a weapon.
- Dead mechs drop every weapon they held except the gun, plus all cubes +2.
- You can hold max one of each kind; both slots can be full; swap requires specifying the dropped slot.
  A charging laser cannot be dropped/swapped.
- Cubes are auto-collected within 3.5 m horizontally and 4 m vertically: +4% max HP and +4% damage
  (cap 10 cubes), +5% body size uncapped (bigger = easier to hit, higher eyes/muzzle).
- Upgrade offers at 1, 3, 6, 10, 15, 21, 28, 36, 45 cubes: 3 choices, 5 s to pick, default is choice 0.
  Weapon upgrades exist only for weapons held when the offer is drawn; stacks persist on the mech.
- 12 NPC drones (350 HP, 25 dmg/1 s, 45 m aggro with LOS, 60 m chase leash, 2 cubes each, no zone
  damage). Attacking one aggros it even from beyond 45 m.
- 3 supply drops called at 70/140/210 s, land 25 s later: stand alone inside 8 m for 4 s → 3 cubes,
  full HP and energy. Contested rings freeze progress.

Full upgrade list (effects exactly as configured):

| Upgrade | Max | Effect |
| --- | --- | --- |
| plating | 3 | +15% max HP |
| armor | 3 | −8% damage taken |
| servos | 3 | +8% walk and boost speed |
| capacitor | 3 | +30 max energy |
| reactor | 3 | +30% energy regen |
| jets | 2 | −25% boost/jet drain |
| nanites | 2 | +60% out-of-combat regen, −1.5 s delay |
| gyros | 2 | +30% aim turn/pitch rate |
| sensors | 2 | +15 m proximity, +25 m vision, halves sound bearing error |
| gun_damage / gun_rate | 3 / 3 | +15% damage / +15% fire rate |
| gun_velocity / gun_mag | 2 / 2 | +25% speed, +15% range / +8 rounds, −20% reload |
| mortar_splash / mortar_reload / mortar_velocity | 2 / 3 / 2 | +30% splash radius / −18% cooldown / +20% shell speed |
| laser_charge / laser_damage / laser_pierce | 2 / 3 / 1 | −25% charge time / +18% damage / pierce one more target |
| lightning_chain / lightning_range / lightning_efficiency | 2 / 2 / 2 | +1 chain / +20% range+cone / −35% energy, +10% damage |

### 1.9 Map

- Static map, same every match; spawns, loot, NPCs, zone and drops are random.
- Bounds roughly x ∈ [−512, 555], y ∈ [−333, 389]. Terrain: 1070×724 int16 height grid at 1 m, cm units.
- 259 prisms (223 building, 9 bridge, remainder stadium), 1560 trees. Only prisms and terrain block
  movement and shots; trees block vision only; cars, water, barriers, parking are scenery.
- Bridges have a base above ground: walkable/shootable underneath; overhangs and roof edges have
  special rules (see protocol §7).
- The viewer's `world.rules.maxStep` is 1.5 but the **server uses `config.mech.step_up` = 1.2**;
  navigation must use config values.

### 1.10 Action surface and runtime limits

- Action fields: `tick` (required), `move` (world direction, len ≤1), `boost`, `jump`, `aim`
  ([yaw, pitch]), `fire` ([slot0, slot1]), `reload`, `pick` (offer index), `take`, `drop`, `debug`.
- Line limit 64 KiB; debug caps 64 lines/points, 16 texts, 80 chars, 8 KiB/decision.
- Runtime: no network, read-only FS except 64 MB `/tmp`, 1 CPU, 512 MB, 128 processes/threads, uid
  65534, map at `/arena`. Standard library only unless vendored (none needed).

### 1.11 Information model (asymmetries to exploit)

- Vision is a 120° wedge: threats behind you are invisible unless within 45 m.
- Enemy HP, weapons and laser charging are visible — target selection can use them.
- Sound gives early warning and weapon type at long range but no identity; beams add identity and
  exact origin; `damaged.by` gives identity and bearing.
- 5 s memory means a target that breaks line of sight is still trackable for a short window.
- Zone and supply drops are globally known from call time; supply drops broadcast their landing.

---

## 2. Strategic opportunities (ranked by leverage)

1. **Survive the zone and the map.** The starter walks straight at the next center with no collision
   avoidance. A map-aware rotation that never gets stuck, uses cover, and times shrinks is the
   highest-confidence improvement, because it removes the most common non-combat deaths.
2. **Stop fighting by default.** The starter holds both triggers whenever any mech is visible. Firing
   gating (range, line of sight, aim readiness, cooldown, ammo) plus engagement thresholds should
   cut wasted damage, exposure and third-party deaths.
3. **Lead and aim correctly.** Gun bullets need a 0.5–0.6 s lead at typical ranges; the starter aims
   at the current position. Modeling slew time and firing only when the actual aim is on target
   converts trigger time into damage.
4. **Disengage and regenerate.** Regen is 45 HP/s after 5 s clean; breaking line of sight and
   resetting the fight is often worth more than a kill.
5. **Energy discipline.** Keep a jet/boost reserve to escape or rotate; never boost constantly toward
   the center. Energy also gates lightning DPS.
6. **Economy routing.** Cubes are +4% HP and damage each (cap 10) and unlock upgrades. Routing
   through safe cubes, crates, drone kills and uncontested drops compounds strength without fights.
7. **Late-game positioning and HP preservation.** Final circles are 25 m and 0 m at 260 DPS; cover,
   facing approach lanes, and entering the last phases at high HP decide many placements.
8. **Unpredictability and dodging.** Visible projectiles and laser telegraphs can be strafed; varied
   waypoints and strafe timing reduce free hits. Lowest confidence offline, but cheap to add.

---

## 3. Proposed architecture

Target: Python 3.13, standard library only, single process, one reader thread, bounded per-decision work.

```
bot.py                 entry point: mf.run(Bot); wiring only
  strategy.py          high-level policy: state machine + arbitration + action composition
  perception.py        observation normalization, entity tracks, event/sound/beam memory
  world_model.py       map load, terrain sampler, prism index, LOS, zone math, nav grid (built on hello)
  threat_assessment.py threat/opportunity scoring per enemy, damage-source reasoning
  navigation.py        cost fields, bounded A*, path following, stuck recovery, combat movement
  combat.py            target selection, lead/ballistic solvers, fire gating, weapon/inventory logic
  resource_manager.py  pickups, cubes, crates, drones, supply drops, upgrade policy
  mechfight.py         unchanged protocol wrapper
```

Flow per decision (≤ 0.1 s, target p99 < 10 ms on the host, hard guard ~35 ms):

```
obs → perception.tracks (merge visible + 5 s memory + sounds/beams/events)
    → world_model.zone_state / position queries
    → threat_assessment.enemies[] (threat, opportunity)
    → strategy.state (LOOT / ROTATE / HOLD / ENGAGE / DISENGAGE / CONTEST / ENDGAME)
    → navigation.intent + combat.intent + resource.intent
    → strategy arbitrates (survival overrides combat; zone overrides loot)
    → action dict (move, boost, jump, aim, fire, pick, take, drop, reload, debug)
    → safety validator (ranges, IDs, energy reserve, self-splash, schema sanity)
```

Design rules:

- Build the world model and navigation grid at `hello` (20 s budget), not per tick.
- Every component returns a conservative fallback; any exception or timeout inside `act()` falls
  back to "hold position/current aim, no fire" rather than blindly advancing.
- Read all numbers from `hello.config` and `obs`; avoid hard-coded game constants except known
  protocol semantics. Deterministic tie-breaking via `random.Random(hello.seed)`.
- Keep `mechfight.py` byte-identical; later switch the Dockerfile entrypoint to `bot.py` on this
  branch only (protocol compatibility unchanged).

---

## 4. Tactical logic (design intent; thresholds to be tuned)

### 4.1 Situational awareness

- Maintain per-mech tracks: last seen time/position/velocity, HP/max, weapons, charging, last
  bearing/beam, whether the mech is currently visible. Decay by 5 s memory.
- Classify threats: distance, weapon matchup vs our kit, whether their aim points near us, laser
  charge, recent damage events attributed to them, projectiles in flight.
- Classify opportunities: low HP, rear exposure, holding a countered weapon, isolated, no cover.
- Track zone geometry and predict the circle at the next shrink end; compute time-to-safety.
- Track our own HP%, energy, ammo/reload/charge, cube count and next upgrade threshold.

### 4.2 Intelligent navigation

- Navigation grid (1–2 m cells) over terrain + prisms, using `step_up`/`max_slope`; mark cover cells
  adjacent to blocking geometry; bridges handled as walk-under volumes (conservative where unsure).
- Cost field = zone urgency (outside current circle = prohibitive) + exposure to known threats +
  slope/step penalties + detour length. Bounded A* (capped expansions, cached results) or a
  flow field toward the current hold point.
- Hold points: a point inside the **next** circle with cover and at least two exit directions,
  not the exact center, offset along the side away from known threats.
- Combat movement: strafe perpendicular to the target's bearing, hop between cover cells, never
  reverse direction on a fixed cadence; if stuck (small displacement over ~0.5 s), try jump/jet or
  a lateral detour before re-planning.
- Movement and aiming are independent: keep the torso near the threat bearing while the legs take
  a different path.

### 4.3 Tactical combat

- Target score ≈ opportunity − risk: choose targets that are low HP, rear-exposed, holding a weapon
  we counter, within our effective band, with clear LOS, and not screened by a third mech.
- Aiming:
  - gun: iterate time-of-flight `t = distance / 252` with target velocity to aim ahead; fire only
    when the **actual** aim error is within a tolerance and cooldown/ammo permit.
  - laser: charge early, release when actual aim crosses the predicted target point; do not start a
    charge when a lightning carrier is within 45 m; abort charge by releasing when the shot is bad.
  - lightning: instant, use as a close finisher; respect the 30° cone and 45 m range; mind energy.
  - mortar: only at 40–200 m against slowed/zoned targets, with a verified clear muzzle and blast
    away from our own position; skip if the pitch solution is uncertain.
- Fire discipline: no default-held trigger. Gate on range, LOS, aim-ready, cooldown, ammo and a
  minimum expected damage; reload during cover instead of mid-duel.
- Engagement gating (heuristics, to tune): engage if expected damage trade is favorable and HP is
  healthy; disengage on HP < ~40%, energy < reserve, multiple threats, being outside the zone, or
  loss of cover. Disengage = break LOS, move with zone, reset the 5 s regen timer.
- Rear armor: face the most dangerous known threat when not shooting elsewhere; treat `damaged.rear`
  as a signal to re-face immediately.

### 4.4 Energy management

- Keep a reserve (≈30 energy) for an escape jet/boost; spend below it only for a decisive fight or
  a forced zone rotation.
- Boost only when late to the zone, crossing open ground under no fire, or escaping; otherwise walk.
- Do not spam lightning below the reserve; its DPS is energy-gated.
- Plan roof climbs (up to ~60 m) only when safe; a climb costs most of the bar and leaves no jet reserve.

### 4.5 Resource collection

- Score candidate resources by (value / (travel + exposure + opportunity cost)), with hard gates:
  never divert while outside the zone; never contest under fire; never cross an open sightline
  toward a known threat.
- Weapons: prefer laser or lightning as the second slot; treat mortar as situational; swap when the
  current kit is redundant (two same-range weapons), keeping the gun unless ammo is exhausted.
- Cubes: route through safe cubes; the first 10 are the strongest stat investment. Crates only when
  isolated (they are noisy and slow); drones only early or when a 2-cube drop is safe.
- Supply drops: contest if close, healthy, and the ring is uncontested or we arrive first; skip if it
  forces a fight outside the next circle. Full HP + 3 cubes is a large swing.
- Upgrades: prioritize survival stats (armor, plating, nanites, capacitor, gyros) unless a weapon
  upgrade matches a weapon we intend to keep; never pick weapon upgrades for weapons we do not hold.
  Be aware offers use the weapon set at draw time.

### 4.6 Endgame

- Circle ≤ 70 m: hold cover at the ring edge, face the likely approach lanes, and avoid being the
  mech in the open center. Preserve HP for the timeout-HP ranking and for the 25 m/0 m phases.
- Final circles: minimize damage taken, use zone pressure to force opponents, and only take fights
  that improve placement (e.g. finishing a low-HP mech that blocks our exit).

---

## 5. Testing plan and evidence limits

| Area | Offline replay (`check.py`) | Unit tests (new, synthetic obs) | Requires real matches |
| --- | --- | --- | --- |
| Protocol, ticks, latency, queueing, schema, debug caps, clean exit | Yes (authoritative) | — | Confirmation under 1 CPU container |
| Zone math and rotation timing | Partial (recorded zone only) | Yes (synthetic circles/timelines) | Zone death rate, rotations |
| Nav grid, walkability, LOS, cover | — | Yes (static map) | Physics fidelity, roofs/bridges |
| Lead/ballistic solvers | — | Yes (math invariants) | Hit rate, damage per engagement |
| Threat scoring, state transitions | — | Yes (scenario fixtures) | Placement effect |
| Economy (take/pick/drop/upgrades/drops) | No: the recorded stream has no offers or eligible pickups | Yes (crafted observations) | Resource efficiency, upgrade value |
| Placement/score | No | — | Only the 48-match practice runs |

- The replay is non-reactive: it validates compliance, timing and crash-freedom, never skill.
- The recorded stream came from a match that stayed in-zone with no offers and no eligible pickups,
  so several new code paths need synthetic observations or a purpose-built simulator.
- No strategic improvement will be claimed from replay results. Claims require server practice
  scores, and even those are noisy: change one family of behavior per submission and compare means.

---

## 6. Risks and mitigations

| Risk | Mitigation |
| --- | --- |
| Offline feedback cannot measure skill | Small, staged submissions; one behavior family per version; keep the best-scoring version tagged |
| 50 ms × 10 Hz on 1 CPU | Precompute at hello; bounded A*; cache; strict per-decision time guard with fallback |
| Map/physics fidelity (bridges, roofs, slopes) | Use `config.mech` values; validate grid against known prisms; conservative "unknown = avoid" near complex geometry |
| Aim slew and 0.1 s action latency | Command aim ahead of the shot; gate fire on actual `self.aim`; solve lead at the predicted fire time |
| Zone timing off-by-one | Prefer observation fields; interpolate only forward from current circle to next |
| Mortar self-damage / walls | No close-range mortar; verify muzzle clearance; skip when pitch solution is uncertain |
| Weapon swap cooldowns and laser-charging lock | Track equip timers and charging state before issuing `take`/`drop` |
| Upgrade offers for weapons not held | Preference rules keyed to current inventory; never rely on future pickups |
| Third parties during fights | Short engagements, cover, disengage thresholds, avoid open-ground duels |
| Read-only FS / no network / stdlib only | No file writes, no network, no third-party packages |
| Stderr is not shown for matches | Plan validation around scores and offline tests; use debug drawing only as a team artifact, not as a measurement channel |
| Overfitting to the static map | Keep policies geometric (cover, distance, zone) rather than hard-coded coordinates |

---

## 7. Prioritized development plan (after approval)

0. **Branch + plan** — `v1-tactical-intelligence`, this document. No push. *(done)*
1. **Foundation** — `world_model` (terrain/prism/LOS/nav grid), `perception` tracks, safe action
   composition and fallback, config parsing, RNG seeding. Behavior not yet changed. Unit tests on
   the static map; official checker must stay PASS with unchanged output timing. *(done)*
2. **Survival v1a** — zone-aware navigation, cover hold points, stuck recovery, energy reserve.
   Expected to remove the largest class of avoidable deaths. Validate locally, then submit and
   record the mean placement. *(implemented; match results pending)*
3. **Combat v1b** — target selection, lead/ballistic solvers, actual-aim fire gating, engagement and
   disengage thresholds, reload discipline. Submit and compare. *(implemented; match results pending)*
4. **Economy v1c** — safe pickup routing, cube/crate/drone/drop policy, inventory swaps, upgrade
   preferences. Submit and compare. *(implemented; match results pending)*
5. **Endgame + threats v1d** — final-circle positioning, rear-facing, laser/projectile dodging,
   third-party avoidance, anti-predictability. Validate locally, then submit and compare.
   *(partially implemented: positioning, dodging, zone prediction; third-party modelling is reactive)*
6. **Tuning** — adjust thresholds only after the structure is stable; keep a rollback tag per
   submission; never mix unrelated changes. *(pending real match data)*

Acceptance gates for every stage: official `check.py` PASS; decision latency p99 well under budget
on the host; no writes outside `/tmp`; unchanged protocol surface; `main` and the submitted baseline
commit untouched; branch commits only, no push without approval.

---

## 8. Open questions to verify during implementation

- Exact collision/sliding behavior at bridge undersides and roof edges versus a 2D grid approximation.
- Whether the config `loot.death_drop` value modifies the documented "drop every weapon except gun".
- How upgrade offers behave when both slots are empty or mid-swap (weapon-dependent choices).
- Zone interpolation rounding and how `shrink_start`/`shrink_end` appear during hold phases.
- Real cost of A* at 10 Hz under the 1-CPU container limit (benchmark early, cap expansions).
- Whether any team-only debug artifacts are downloaded after practice matches; the protocol says
  stderr is not shown, so do not design measurement around it.
