"""Synthetic arena, config and observation helpers for the unit tests.

The official checker ships a recorded stream of a real match, but that stream is
non-reactive and contains no nearby loot.  These helpers build a tiny flat arena
plus protocol-shaped observations (same field names as protocol.md) so every
rule the strategy uses can be exercised on demand.
"""
from __future__ import annotations

import array
import json
import os
import shutil
import tempfile

CONFIG = {
    "match": {"tick_hz": 20, "decide_every": 2, "duration_s": 480, "mechs": 16,
              "spawn_min_separation": 130, "spawn_zone_fraction": 0.8, "arena_margin": 30},
    "bot": {"tick_budget_ms": 50, "hello_budget_ms": 20000, "coast_s": 1,
            "offer_timeout_s": 5, "max_line_bytes": 65536},
    "mech": {"radius": 2.5, "height": 9, "eye_height": 8, "muzzle_height": 7, "hp": 1400,
             "walk_speed": 11.7, "boost_speed": 19.5, "ground_accel": 58.5, "air_accel": 12,
             "gravity": 22, "jump_speed": 11, "jet_accel": 34, "max_rise_speed": 10,
             "step_up": 1.2, "max_slope_deg": 50, "energy": 100, "energy_regen": 18,
             "energy_regen_delay": 0.6, "boost_drain": 17, "jet_drain": 15, "turn_rate": 4,
             "pitch_rate": 3, "min_pitch": -0.7, "max_pitch": 1.3, "regen_delay": 5,
             "regen_per_s": 45, "charge_move_mult": 0.5, "self_damage_mult": 0.5, "armor": 0,
             "rear_mult": 1.5, "front_arc_deg": 180, "near_range_bonus": 0, "vision_bonus": 0,
             "hearing": 1},
    "vision": {"range": 200, "fov_deg": 120, "near_range": 45, "canopy_blocks": True,
               "show_enemy_hp": True, "memory_s": 5},
    "sound": {"bearing_noise_deg": 7, "near": 60, "mid": 160, "step_speed": 5, "step_every": 0.6,
              "range": {"step": 40, "boost": 90, "reload": 60, "gun": 320, "mortar": 420,
                        "explosion": 420, "laser_charge": 140, "laser": 380, "lightning": 160,
                        "jet": 90, "death": 600, "crate": 220, "npc_gun": 200, "drop": 600}},
    "weapons": {
        "gun": {"damage": 45, "cooldown": 0.24, "speed": 252, "range": 230, "spread_deg": 0.6,
                "mag": 12, "reload": 2.4},
        "mortar": {"damage": 200, "splash": 160, "splash_radius": 7, "splash_edge": 0.3,
                   "cooldown": 2.6, "speed": 72, "gravity": 22, "ttl": 7, "knock": 16,
                   "knock_up": 9, "drain": 45},
        "laser": {"damage": 315, "charge": 0.75, "min_charge": 0.15, "min_frac": 0.35,
                  "hold_s": 2, "cooldown": 1.65, "range": 280, "pierce": 0,
                  "charge_turn_mult": 1, "cookoff": 0.6},
        "lightning": {"damage": 60, "cooldown": 0.25, "range": 45, "cone_deg": 30, "chain": 2,
                      "chain_range": 14, "chain_falloff": 0.7, "energy": 3, "short_s": 2},
    },
    "projectile": {"gun_radius": 0.3, "npc_radius": 0.3},
    "cubes": {"pickup_radius": 3.5, "hp_per_cube": 0.04, "damage_per_cube": 0.04,
              "stat_cap": 10, "death_drop_extra": 2, "size_per_cube": 0.05,
              "upgrade_thresholds": [1, 3, 6, 10, 15, 21, 28, 36, 45]},
    "crates": {"count": 36, "roof_fraction": 0.25, "hp": 300, "radius": 1.4, "height": 2.4,
               "cubes": 1},
    "loot": {"pickup_radius": 4, "equip_s": 1, "floor_count": 40, "floor_roof_fraction": 0.25,
             "crate_weapon_chance": 0.5, "death_drop": 1,
             "weights": {"gun": 0, "mortar": 1, "laser": 1, "lightning": 1}},
    "npc": {"count": 12, "hp": 350, "radius": 1.8, "height": 3.5, "speed": 5, "leash": 30,
            "chase_leash": 60, "aggro_range": 45, "deaggro_range": 90, "spawn_clear": 0,
            "damage": 25, "cooldown": 1, "projectile_speed": 90, "spread_deg": 2.5, "cubes": 2},
    "drops": {"times": [70, 140, 210], "fall_s": 25, "radius": 8, "capture_s": 4, "cubes": 3,
              "repair": 1, "spread": 0.7},
    "zone": {"start_center": [41.3, -72.8], "start_radius": 560,
             "phases": [{"hold_s": 60, "shrink_s": 50, "radius": 380, "dps": 20}]},
    "debug": {"max_lines": 64, "max_points": 64, "max_text": 16, "max_text_chars": 80,
              "max_bytes": 8192, "max_match_bytes": 8000000, "hold_s": 0.5},
    "replay": {"frame_every": 2},
    "upgrades": [],
}

HALF = 150.0
TERRAIN_N = int(2 * HALF) + 1


def _write_arena(path, prisms):
    os.makedirs(os.path.join(path, "v2"), exist_ok=True)
    world = {
        "bounds": {"minX": -HALF, "maxX": HALF, "minY": -HALF, "maxY": HALF},
        "prisms": prisms,
        "trees": {"fields": ["x", "y", "z", "height", "crownR", "trunkR"], "data": []},
        "terrain": {"file": "terrain.i16", "nx": TERRAIN_N, "ny": TERRAIN_N,
                    "minX": -HALF, "minY": -HALF, "step": 1.0},
    }
    with open(os.path.join(path, "v2", "world.json"), "w", encoding="utf-8") as f:
        json.dump(world, f)
    heights = array.array("h", [0]) * (TERRAIN_N * TERRAIN_N)
    with open(os.path.join(path, "v2", "terrain.i16"), "wb") as f:
        heights.tofile(f)
    return world


def make_hello(prisms=()):
    """A hello message whose map dir is a fresh flat arena. Returns (hello, tmpdir)."""
    tmp = tempfile.mkdtemp(prefix="mecharena_")
    _write_arena(tmp, list(prisms))
    hello = {
        "type": "hello",
        "protocol": "mechfight/1",
        "slot": 3,
        "seed": 1213,
        "map": {"dir": tmp, "files": ["v2/world.json", "v2/terrain.i16"]},
        "config": json.loads(json.dumps(CONFIG)),
    }
    return hello, tmp


def remove_arena(path):
    shutil.rmtree(path, ignore_errors=True)


def wall(ax, ay, bx, by, base=-5.0, top=12.0, kind="building"):
    return {"kind": kind, "base": base, "top": top,
            "ring": [ax, ay, bx, ay, bx, by, ax, by], "holes": []}


def gun(ammo=12, cooldown=0.0, reload=0.0):
    return {"kind": "gun", "cooldown": cooldown, "ammo": ammo, "reload": reload,
            "stats": dict(CONFIG["weapons"]["gun"])}


def _special(kind, **extra):
    w = {"kind": kind, "cooldown": 0.0, "stats": dict(CONFIG["weapons"][kind])}
    if kind == "laser":
        w["charge"] = 0.0
    w.update(extra)
    return w


def laser(charge=0.0, cooldown=0.0):
    return _special("laser", charge=charge, cooldown=cooldown)


def mortar(drain=45.0, cooldown=0.0):
    return _special("mortar", cooldown=cooldown)


def lightning(energy=3.0, cooldown=0.0):
    return _special("lightning", cooldown=cooldown)


def zone(center=(0.0, 0.0), radius=120.0, next_center=None, next_radius=None,
         shrink_start=300.0, shrink_end=350.0, dps=20.0):
    nc = center if next_center is None else next_center
    nr = radius if next_radius is None else next_radius
    return {"center": [center[0], center[1]], "radius": radius,
            "next_center": [nc[0], nc[1]], "next_radius": nr,
            "shrink_start": shrink_start, "shrink_end": shrink_end, "dps": dps}


def mech(mid, pos, aim=(0.0, 0.0), vel=(0.0, 0.0, 0.0), hp=1400.0, max_hp=1400.0,
         weapons=("gun",), charging=False, cubes=0, kind="mech"):
    m = {"id": mid, "kind": kind, "pos": [pos[0], pos[1], pos[2]],
         "vel": [vel[0], vel[1], vel[2]], "aim": [aim[0], aim[1]],
         "hp": hp, "max_hp": max_hp, "cubes": cubes,
         "weapons": list(weapons), "charging": charging}
    if kind == "npc":
        m.pop("cubes")
        m.pop("weapons")
        m.pop("charging")
    return m


def obs(t=10.0, tick=None, pos=(0.0, 0.0, 0.0), aim=(0.0, 0.0), hp=1400.0, max_hp=1400.0,
        energy=100.0, max_energy=100.0, grounded=True, cubes=0, weapons=None,
        mechs=(), crates=(), pickups=(), cubes_obs=(), drops=(), projectiles=(),
        events=(), zone_obs=None, offer=None, alive=16, in_zone=True, upgrades=(),
        sounds=(), beams=()):
    if tick is None:
        tick = int(round(t * 20))
    if weapons is None:
        weapons = [gun(), None]
    return {
        "type": "obs", "tick": tick, "t": t, "alive": alive,
        "self": {
            "id": 3, "pos": [pos[0], pos[1], pos[2]], "vel": [0.0, 0.0, 0.0],
            "aim": [aim[0], aim[1]], "hp": hp, "max_hp": max_hp,
            "energy": energy, "max_energy": max_energy, "grounded": grounded,
            "cubes": cubes, "upgrades": list(upgrades), "in_zone": in_zone,
            "weapons": list(weapons),
            "stats": {"walk_speed": 11.7, "boost_speed": 19.5, "turn_rate": 4.0,
                      "pitch_rate": 3.0, "muzzle_height": 7.0, "eye_height": 8.0},
        },
        "mechs": list(mechs),
        "projectiles": list(projectiles),
        "crates": list(crates),
        "cubes": list(cubes_obs),
        "pickups": list(pickups),
        "sounds": list(sounds),
        "beams": list(beams),
        "events": list(events),
        "drops": list(drops),
        "zone": zone() if zone_obs is None else zone_obs,
        "offer": offer,
    }


def bullet(pos, vel, kind="gun"):
    return {"id": 280, "kind": kind, "pos": [pos[0], pos[1], pos[2]],
            "vel": [vel[0], vel[1], vel[2]]}
