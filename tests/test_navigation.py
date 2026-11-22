import math
import unittest

from navigation import Navigator
from perception import Perception
from world_model import WorldModel

from . import support


class NavigationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.hello, cls.tmp = support.make_hello([support.wall(-3, -60, 3, 60)])
        cls.world = WorldModel(cls.hello)
        assert cls.world.validation[0]

    @classmethod
    def tearDownClass(cls):
        support.remove_arena(cls.tmp)

    def setUp(self):
        self.p = Perception(support.CONFIG)
        self.nav = Navigator(support.CONFIG, self.world, seed=3)

    def _feed(self, t, pos):
        self.p.update(support.obs(t=t, pos=pos))
        return self.p

    def test_straight_line_when_clear(self):
        p = self._feed(1.0, (0.0, 90.0, 0.0))
        res = self.nav.move_to(p, (50.0, 90.0))
        self.assertFalse(res.reached)
        self.assertGreater(res.move[0], 0.9)
        self.assertLess(abs(res.move[1]), 0.1)

    def test_reached_inside_stop_radius(self):
        p = self._feed(1.0, (0.0, 90.0, 0.0))
        res = self.nav.move_to(p, (1.5, 90.0))
        self.assertTrue(res.reached)
        self.assertEqual(res.move, (0.0, 0.0))

    def test_goal_behind_wall_steers_around(self):
        # Local probes only look 8 m ahead; start within that of the wall.
        p = self._feed(1.0, (-10.0, 30.0, 0.0))
        res = self.nav.move_to(p, (20.0, 30.0))
        self.assertGreater(abs(res.move[1]), 0.1,
                           "should slide along the wall instead of pushing into it")
        self.assertNotEqual(res.move, (0.0, 0.0))

    def test_stuck_escalates_to_jump_then_jet(self):
        pos = (-6.0, 30.0, 0.0)  # pressed against the wall's west face
        got_jump = got_jet = False
        t = 1.0
        for _ in range(20):
            p = self._feed(t, pos)
            res = self.nav.move_to(p, (20.0, 30.0))
            got_jump = got_jump or res.jump
            got_jet = got_jet or res.jet
            t += 0.2
        self.assertTrue(got_jump, "stuck level 2 should request a jump")
        self.assertTrue(got_jet, "stuck level 3 should fire the jets")

    def test_hold_point_is_inside_zone(self):
        p = self._feed(10.0, (30.0, 0.0, 0.0))
        zone = p.zone
        hp = self.nav.choose_hold_point(p, zone, [], "HOLD")
        self.assertIsNotNone(hp)
        c, r = zone.predicted_circle()
        self.assertLessEqual(math.hypot(hp[0] - c[0], hp[1] - c[1]), r)

    def test_retreat_point_is_inside_zone(self):
        p = self._feed(10.0, (30.0, 0.0, 0.0))
        p.update(support.obs(t=10.2, pos=(30.0, 0.0, 0.0),
                             mechs=[support.mech(5, (0.0, 60.0, 0.0))]))
        threats = list(p.enemies)
        rp = self.nav.choose_retreat_point(p, p.zone, threats)
        self.assertIsNotNone(rp)
        c, r = p.zone.predicted_circle()
        self.assertLessEqual(math.hypot(rp[0] - c[0], rp[1] - c[1]), r + 1e-6)

    def test_zone_goal_points_inward_when_outside(self):
        p = self._feed(10.0, (140.0, 0.0, 0.0))
        risk = p.zone.risk(p.pos, p.walk_speed(), p.boost_speed())
        self.assertTrue(risk.outside)
        goal = self.nav.zone_goal(p, p.zone, risk)
        self.assertLess(goal[0], p.pos[0])


if __name__ == "__main__":
    unittest.main()
