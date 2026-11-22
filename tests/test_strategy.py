import json
import math
import unittest

from strategy import Strategy
from world_model import WorldModel

from . import support


class StrategyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.hello, cls.tmp = support.make_hello()
        cls.world = WorldModel(cls.hello)
        assert cls.world.validation[0]

    @classmethod
    def tearDownClass(cls):
        support.remove_arena(cls.tmp)

    def setUp(self):
        self.errors = []
        self.s = Strategy(support.CONFIG, self.world, seed=7, log=self.errors.append)

    def decide(self, o):
        a = self.s.decide(o)
        self.assertFalse([e for e in self.errors if "strategy error" in e],
                         "strategy raised: %r" % (self.errors,))
        self.assertEqual(a["tick"], o["tick"])
        return a

    # ------------------------------------------------------------ survivorship
    def test_zone_rotation_when_outside(self):
        o = support.obs(t=30.0, pos=(130.0, 0.0, 0.0),
                        zone_obs=support.zone(radius=60.0))
        a = self.decide(o)
        self.assertEqual(self.s.state, "ROTATE")
        self.assertLess(a["move"][0], -0.5)
        self.assertTrue(a.get("boost"))

    def test_disengage_when_hurt_and_pressed(self):
        o = support.obs(t=30.0, hp=275.0, aim=(0.0, 0.0),
                        mechs=[support.mech(5, (25.0, 0.0, 0.0), aim=(math.pi, 0.0),
                                            weapons=("gun", "laser"))])
        a = self.decide(o)
        self.assertEqual(self.s.state, "DISENGAGE")
        self.assertTrue(a.get("boost"), "a hurt mech should burn energy to leave")
        self.assertEqual(self.s.target_id, 5)

    def test_bullet_dodge_overrides_movement(self):
        o = support.obs(t=12.0, projectiles=[
            support.bullet((-15.0, 2.0, 4.5), (250.0, 0.0, 0.0))])
        a = self.decide(o)
        self.assertLess(abs(a["move"][0]), 0.3, "should sidestep, not run into the bullet")
        self.assertGreater(abs(a["move"][1]), 0.8)
        self.assertTrue(a.get("boost"))

    # ------------------------------------------------------------ combat
    def test_engage_and_fire_gun(self):
        o = support.obs(t=20.0, aim=(0.0, 0.0),
                        mechs=[support.mech(5, (100.0, 0.0, 0.0), aim=(0.0, 0.0))])
        a = self.decide(o)
        self.assertEqual(self.s.state, "ENGAGE")
        self.assertEqual(self.s.target_id, 5)
        self.assertTrue(a["fire"][0])
        self.assertAlmostEqual(a["aim"][0], 0.0, delta=0.15)

    def test_laser_charge_hold_then_release(self):
        o = support.obs(t=10.0, aim=(0.0, 0.0),
                        weapons=[support.gun(), support.laser(charge=0.40)],
                        mechs=[support.mech(5, (150.0, 0.0, 0.0), aim=(0.0, 0.0))])
        a = self.decide(o)
        self.assertEqual(self.s.state, "ENGAGE")
        self.assertTrue(a["fire"][1], "a nearly charged laser should keep holding")

        a = self.decide(support.obs(t=12.4, aim=(0.0, 0.0),
                                    weapons=[support.gun(), support.laser(charge=0.75)]))
        self.assertTrue(a["fire"][1], "keep the charge while the target was seen 2.4 s ago")

        a = self.decide(support.obs(t=12.8, aim=(0.0, 0.0),
                                    weapons=[support.gun(), support.laser(charge=0.75)]))
        self.assertFalse(a["fire"][1], "release the beam when the target has been gone")

    def test_lightning_needs_energy(self):
        o = support.obs(t=8.0, aim=(0.0, 0.0), energy=2.0,
                        weapons=[None, support.lightning()],
                        mechs=[support.mech(5, (30.0, 0.0, 0.0), aim=(0.0, 0.0))])
        a = self.decide(o)
        self.assertFalse(a["fire"][0] or a["fire"][1], "no energy, no arc")

        self.setUp()
        o["self"]["energy"] = 100.0
        a = self.decide(o)
        self.assertTrue(a["fire"][1], "with energy the arc should fire")

    def test_gun_and_laser_with_close_lightning_threat(self):
        # Regression: threats reach combat as tracks (distance_to), never as
        # report threats; a close lightning carrier must not crash the laser path.
        o = support.obs(t=15.0, aim=(0.0, 0.0),
                        weapons=[support.laser(), None],
                        mechs=[support.mech(5, (40.0, 5.0, 0.0), aim=(0.0, 0.0)),
                               support.mech(6, (30.0, -8.0, 0.0), aim=(math.pi, 0.0),
                                            weapons=("lightning",))])
        a = self.decide(o)
        self.assertIn(self.s.state, ("ENGAGE", "DISENGAGE", "ROTATE", "HOLD"))
        self.assertTrue(isinstance(a["fire"], list))

    def test_npc_self_defense(self):
        o = support.obs(t=9.0, aim=(0.0, 0.0),
                        mechs=[support.mech(104, (15.0, 0.0, 0.0), kind="npc")])
        a = self.decide(o)
        self.assertEqual(self.s.state, "ENGAGE")
        self.assertEqual(self.s.target_id, 104)
        self.assertTrue(a["fire"][0])

    def test_mortar_fires_after_aiming(self):
        o = support.obs(t=20.0, aim=(0.0, 0.0),
                        weapons=[support.mortar(), support.gun()],
                        mechs=[support.mech(5, (100.0, 0.0, 0.0), aim=(0.0, 0.0))])
        a = self.decide(o)
        self.assertEqual(self.s.state, "ENGAGE")
        self.assertGreater(a["aim"][1], 0.05, "ballistic solution needs loft")
        self.assertFalse(a["fire"][0], "the tube must slew before firing")
        o2 = support.obs(t=20.2, aim=tuple(a["aim"]),
                         weapons=[support.mortar(), support.gun()],
                         mechs=[support.mech(5, (100.0, 0.0, 0.0), aim=(0.0, 0.0))])
        a2 = self.decide(o2)
        self.assertTrue(a2["fire"][0], "aligned, in range, no self-damage")
        self.assertAlmostEqual(a2["aim"][1], a["aim"][1], delta=0.02)

    # ------------------------------------------------------------ economy
    def test_upgrade_offer_is_picked(self):
        offer = {"choices": [{"id": "plating"}, {"id": "servos"}, {"id": "jets"}]}
        a = self.decide(support.obs(t=15.0, offer=offer))
        self.assertEqual(a.get("pick"), 0)

    def test_take_weapon_into_empty_slot(self):
        pickups = [{"id": 8013, "kind": "lightning", "pos": [2.0, 0.0, 0.0]}]
        a = self.decide(support.obs(t=10.0, pickups=pickups))
        self.assertEqual(self.s.state, "LOOT")
        self.assertEqual(a.get("take"), 8013)
        self.assertNotIn("drop", a)

    def test_take_weapon_swaps_weakest_special(self):
        pickups = [{"id": 8014, "kind": "laser", "pos": [2.5, 0.0, 0.0]}]
        a = self.decide(support.obs(t=10.0, weapons=[support.gun(), support.mortar()],
                                    pickups=pickups))
        self.assertEqual(self.s.state, "LOOT")
        self.assertEqual(a.get("take"), 8014)
        self.assertEqual(a.get("drop"), 1)

    def test_far_pickup_is_approached_then_taken(self):
        pickups = [{"id": 8015, "kind": "laser", "pos": [60.0, 0.0, 0.0]}]
        a = self.decide(support.obs(t=10.0, pickups=pickups))
        self.assertEqual(self.s.state, "LOOT")
        self.assertNotIn("take", a, "pickup is 60 m away")
        self.assertGreater(a["move"][0], 0.5)
        took = None
        for k in range(1, 20):
            pos = (3.0 * k, 0.0, 0.0)
            a = self.decide(support.obs(t=10.0 + k * 0.2, pos=pos, pickups=pickups))
            if "take" in a:
                took = a["take"]
                break
        self.assertEqual(took, 8015, "walking to the pickup must end in a take")

    def test_crate_is_shot_from_standoff(self):
        crates = [{"id": 1007, "pos": [80.0, 0.0, 0.0], "hp": 300.0}]
        a = self.decide(support.obs(t=20.0, aim=(0.0, 0.0), crates=crates))
        self.assertEqual(self.s.state, "LOOT")
        self.assertTrue(a["fire"][0])
        self.assertGreater(a["move"][0], 0.5)

    def test_supply_drop_is_contested(self):
        drops = [{"id": 20001, "pos": [40.0, 0.0, 0.0], "radius": 8.0,
                  "lands_in": 0.0, "capture_s": 4, "progress": 0, "holder": None}]
        a = self.decide(support.obs(t=71.0, drops=drops))
        self.assertEqual(self.s.state, "CONTEST")
        self.assertGreater(a["move"][0], 0.5)

    def test_low_hp_reluctance_to_loot_far_crate(self):
        crates = [{"id": 1007, "pos": [80.0, 0.0, 0.0], "hp": 300.0}]
        a = self.decide(support.obs(t=20.0, hp=300.0, crates=crates))
        self.assertNotEqual(self.s.state, "LOOT")

    # ------------------------------------------------------------ robustness
    def test_decide_on_undocumented_observation(self):
        a = self.s.decide({})
        self.assertIsInstance(a, dict)
        self.assertEqual(a["tick"], 0)
        self.assertEqual(a["move"], [0.0, 0.0])

    def test_garbage_entries_do_not_crash(self):
        o = support.obs(t=3.0,
                        mechs=[{"id": "x"}, {"id": 9, "pos": [None, 1, 2]}],
                        pickups=[{"id": "no"}, {"id": 8013, "kind": "laser"}],
                        crates=[{"id": 1007}], drops=[{"id": "z"}],
                        projectiles=[{"kind": "gun", "pos": "bad"}])
        a = self.decide(o)
        self.assertEqual(a["tick"], o["tick"])

    def test_decisions_are_deterministic(self):
        seq = [
            support.obs(t=10.0, aim=(0.0, 0.0),
                        mechs=[support.mech(5, (90.0, 10.0, 0.0), aim=(0.0, 0.0))]),
            support.obs(t=10.2, aim=(0.0, 0.0),
                        mechs=[support.mech(5, (88.0, 11.0, 0.0), aim=(0.0, 0.0))]),
            support.obs(t=10.4, aim=(0.0, 0.0)),
        ]
        a1 = [self.s.decide(o) for o in seq]
        self.setUp()
        a2 = [self.s.decide(o) for o in seq]
        self.assertEqual(json.dumps(a1, sort_keys=True), json.dumps(a2, sort_keys=True))


if __name__ == "__main__":
    unittest.main()
