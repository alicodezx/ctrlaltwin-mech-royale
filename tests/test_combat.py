import math
import unittest

from combat import Combat
from perception import Perception
from threat_assessment import ThreatAssessor
from world_model import WorldModel

from . import support


class CombatTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.hello, cls.tmp = support.make_hello()
        cls.world = WorldModel(cls.hello)

    @classmethod
    def tearDownClass(cls):
        support.remove_arena(cls.tmp)

    def setUp(self):
        self.cfg = support.CONFIG
        self.combat = Combat(self.cfg)
        self.p = Perception(self.cfg)

    def _target(self, o):
        self.p.update(o)
        tr = [t for t in self.p.enemies if not t.is_npc][0]
        return self.combat.make_target(tr)

    def test_gun_fires_when_aligned(self):
        target = self._target(support.obs(t=5.0, aim=(0.0, 0.0),
                                          mechs=[support.mech(5, (100.0, 0.0, 0.0))]))
        sol = self.combat.solve(self.p, target, self.world, 20.0)
        self.assertEqual((sol.slot, sol.kind), (0, "gun"))
        self.assertTrue(sol.fire, sol.reason)

    def test_gun_leads_a_moving_target(self):
        target = self._target(support.obs(t=5.0, aim=(0.0, 0.0),
                                          mechs=[support.mech(5, (100.0, 0.0, 0.0),
                                                              vel=(0.0, 30.0, 0.0))]))
        sol = self.combat.solve(self.p, target, self.world, 20.0)
        self.assertGreater(sol.aim[0], 0.0, "aim should lead north of a northbound target")

    def test_no_shot_beyond_gun_range(self):
        target = self._target(support.obs(t=5.0, aim=(0.0, 0.0),
                                          mechs=[support.mech(5, (250.0, 0.0, 0.0))]))
        sol = self.combat.solve(self.p, target, self.world, 20.0)
        self.assertFalse(sol.fire)

    def test_laser_cancels_charge_out_of_range(self):
        self.p.update(support.obs(t=5.0, aim=(0.0, 0.0),
                                  weapons=[support.laser(charge=0.4), None],
                                  mechs=[support.mech(5, (300.0, 0.0, 0.0))]))
        tr = self.p.enemies[0]
        target = self.combat.make_target(tr)
        sol = self.combat.solve(self.p, target, self.world, 20.0)
        self.assertEqual(sol.kind, "laser")
        self.assertEqual(sol.reason, "cancel")
        self.assertFalse(sol.fire)

    def test_laser_holds_while_charging(self):
        self.p.update(support.obs(t=5.0, aim=(0.0, 0.0),
                                  weapons=[support.laser(charge=0.3), None],
                                  mechs=[support.mech(5, (120.0, 0.0, 0.0))]))
        target = self.combat.make_target(self.p.enemies[0])
        sol = self.combat.solve(self.p, target, self.world, 20.0)
        self.assertEqual(sol.reason, "charge")
        self.assertTrue(sol.fire, "holding the trigger keeps the charge")

    def test_laser_with_close_lightning_track_does_not_crash(self):
        # Regression for passing ThreatReport.threats (no distance_to) into combat.
        self.p.update(support.obs(t=5.0, aim=(0.0, 0.0),
                                  weapons=[support.laser(), None],
                                  mechs=[support.mech(5, (60.0, 0.0, 0.0)),
                                         support.mech(6, (40.0, -6.0, 0.0),
                                                      weapons=("lightning",))]))
        with_lightning = self.p.tracks[6]
        target = self.combat.make_target(self.p.tracks[5])
        sol = self.combat.solve(self.p, target, self.world, 26.0, [with_lightning])
        self.assertEqual(sol.slot, 0)
        self.assertEqual(sol.kind, "laser")

    def test_lightning_energy_and_range_gates(self):
        self.p.update(support.obs(t=5.0, aim=(0.0, 0.0), energy=2.0,
                                  weapons=[support.lightning(), None],
                                  mechs=[support.mech(5, (30.0, 0.0, 0.0))]))
        target = self.combat.make_target(self.p.enemies[0])
        sol = self.combat.solve(self.p, target, self.world, 20.0)
        self.assertFalse(sol.fire)
        self.assertEqual(sol.slot, -1, "no slot can pay the energy cost")

        self.p.update(support.obs(t=5.2, aim=(0.0, 0.0), energy=100.0,
                                  weapons=[support.lightning(), None],
                                  mechs=[support.mech(5, (30.0, 0.0, 0.0))]))
        target = self.combat.make_target(self.p.enemies[0])
        sol = self.combat.solve(self.p, target, self.world, 20.0)
        self.assertEqual(sol.kind, "lightning")
        self.assertTrue(sol.fire)

    def test_lightning_respects_cone(self):
        # Facing 60 degrees away from a target 20 m off: no arc.
        self.p.update(support.obs(t=5.0, aim=(1.2, 0.0),
                                  weapons=[support.lightning(), None],
                                  mechs=[support.mech(5, (0.0, 20.0, 0.0))]))
        target = self.combat.make_target(self.p.enemies[0])
        sol = self.combat.solve(self.p, target, self.world, 20.0)
        self.assertEqual(sol.reason, "cone")
        self.assertFalse(sol.fire)

    def test_mortar_ballistic_arc(self):
        self.p.update(support.obs(t=5.0, aim=(0.0, 0.0),
                                  weapons=[support.mortar(), None],
                                  mechs=[support.mech(5, (100.0, 0.0, 0.0))]))
        target = self.combat.make_target(self.p.enemies[0])
        sol = self.combat.solve(self.p, target, self.world, 20.0,
                                allow_mortar=True, aggressive=False)
        self.assertEqual(sol.kind, "mortar")
        self.assertGreater(sol.aim[1], 0.05, "a 100 m flat shot needs positive pitch")
        self.assertAlmostEqual(sol.aim[0], 0.0, delta=0.05)

    def test_mortar_too_close_is_refused(self):
        self.p.update(support.obs(t=5.0, aim=(0.0, 0.0),
                                  weapons=[support.mortar(), None],
                                  mechs=[support.mech(5, (10.0, 0.0, 0.0))]))
        target = self.combat.make_target(self.p.enemies[0])
        sol = self.combat.solve(self.p, target, self.world, 20.0,
                                allow_mortar=True, aggressive=False)
        self.assertFalse(sol.fire)


class ThreatTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.flat_hello, cls.flat_tmp = support.make_hello()
        cls.wall_hello, cls.wall_tmp = support.make_hello(
            [support.wall(-3, -60, 3, 60)])
        cls.flat = WorldModel(cls.flat_hello)
        cls.walled = WorldModel(cls.wall_hello)

    @classmethod
    def tearDownClass(cls):
        support.remove_arena(cls.flat_tmp)
        support.remove_arena(cls.wall_tmp)

    def setUp(self):
        self.p = Perception(support.CONFIG)
        self.assessor = ThreatAssessor(support.CONFIG)

    def test_charging_laser_facing_us_is_the_biggest_threat(self):
        self.p.update(support.obs(t=10.0, pos=(-80.0, 0.0, 0.0), aim=(0.0, 0.0),
                                  mechs=[support.mech(5, (0.0, 0.0, 0.0),
                                                      aim=(math.pi, 0.0),
                                                      weapons=("laser",), charging=True),
                                         support.mech(6, (0.0, 40.0, 0.0),
                                                      aim=(0.0, 0.0),
                                                      weapons=("gun",))]))
        report = self.assessor.assess(self.p, self.flat)
        self.assertIsNotNone(report.primary)
        self.assertEqual(report.primary.id, 5)
        self.assertTrue(report.primary.charging)
        self.assertGreater(report.pressure, 0.0)
        self.assertGreaterEqual(report.close_count, 0)

    def test_wall_blocks_line_of_sight(self):
        self.p.update(support.obs(t=10.0, pos=(-20.0, 30.0, 0.0), aim=(0.0, 0.0),
                                  mechs=[support.mech(5, (20.0, 30.0, 0.0),
                                                      aim=(math.pi, 0.0))]))
        open_report = self.assessor.assess(self.p, self.flat)
        self.assertTrue(open_report.primary.los)
        walled = self.assessor.assess(self.p, self.walled)
        self.assertFalse(walled.primary.los)
        self.assertLess(walled.primary.score, open_report.primary.score)

    def test_recent_damage_raises_score(self):
        self.p.update(support.obs(t=10.0, pos=(-80.0, 0.0, 0.0), aim=(0.0, 0.0),
                                  mechs=[support.mech(5, (0.0, 0.0, 0.0),
                                                      aim=(math.pi, 0.0))]))
        base = self.assessor.assess(self.p, self.flat).primary.score
        self.p.update(support.obs(t=10.1, pos=(-80.0, 0.0, 0.0), aim=(0.0, 0.0),
                                  mechs=[support.mech(5, (0.0, 0.0, 0.0),
                                                      aim=(math.pi, 0.0))],
                                  events=[{"type": "damaged", "amount": 120,
                                           "weapon": "gun", "bearing": 0.0, "by": 5}]))
        hurt = self.assessor.assess(self.p, self.flat).primary
        self.assertGreater(hurt.score, base)
        self.assertEqual(self.p.last_attacker, 5)


if __name__ == "__main__":
    unittest.main()
