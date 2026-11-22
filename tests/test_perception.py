import math
import unittest

from perception import Perception, ZoneInfo

from . import support


class PerceptionTests(unittest.TestCase):
    def setUp(self):
        self.p = Perception(support.CONFIG)

    def test_enemy_memory_and_seen_flag(self):
        self.p.update(support.obs(t=10.0, mechs=[support.mech(5, (60, 0, 0))]))
        self.assertEqual([t.id for t in self.p.enemies], [5])
        self.assertTrue(self.p.enemies[0].seen_now)
        self.p.update(support.obs(t=13.0))
        self.assertEqual([t.id for t in self.p.enemies], [5], "memory must keep a 3 s old track")
        self.assertFalse(self.p.enemies[0].seen_now, "a remembered track is not currently seen")
        self.p.update(support.obs(t=15.5))
        self.assertEqual(self.p.enemies, [], "tracks expire after vision.memory_s")

    def test_enemy_reappears_refreshes_track(self):
        self.p.update(support.obs(t=10.0, mechs=[support.mech(5, (60, 0, 0))]))
        self.p.update(support.obs(t=12.0))
        self.p.update(support.obs(t=13.0, mechs=[support.mech(5, (50, 5, 0))]))
        self.assertTrue(self.p.enemies[0].seen_now)
        self.assertAlmostEqual(self.p.enemies[0].pos[0], 50.0)
        self.assertAlmostEqual(self.p.enemies[0].seen_t, 13.0)

    def test_npc_tracks_are_separate(self):
        self.p.update(support.obs(t=5.0, mechs=[support.mech(104, (10, 10, 0), kind="npc")]))
        self.assertEqual(self.p.npcs[0].id, 104)
        self.assertEqual(self.p.enemies, [])

    def test_damage_attribution(self):
        self.p.update(support.obs(t=4.0, events=[
            {"type": "damaged", "amount": 90, "weapon": "laser", "bearing": 0.5, "by": 7}]))
        self.assertEqual(self.p.last_attacker, 7)
        self.assertGreater(self.p.recent_damage(7), 0.5)
        self.assertAlmostEqual(self.p.last_damage_bearing, 0.5)

    def test_zone_damage_without_attacker_uses_bearing(self):
        self.p.update(support.obs(t=4.0, events=[
            {"type": "damaged", "amount": 20, "weapon": "zone", "bearing": -1.2, "by": None}]))
        self.assertIsNone(self.p.last_attacker)
        self.assertAlmostEqual(self.p.sound_bearing, -1.2)

    def test_gun_projectile_dodge_is_perpendicular(self):
        self.p.update(support.obs(t=6.0, projectiles=[
            support.bullet((-15.0, 2.0, 4.5), (250.0, 0.0, 0.0))]))
        dodge = self.p.projectile_danger(lambda x, y: 0.0)
        self.assertTrue(dodge.active)
        self.assertEqual(dodge.threat, "gun")
        self.assertLess(abs(dodge.direction[0]), 0.25)

    def test_mortar_landing_point_dodge(self):
        self.p.update(support.obs(t=6.0, projectiles=[
            support.bullet((0.0, -30.0, 6.0), (0.0, 25.0, 4.0), kind="mortar")]))
        dodge = self.p.projectile_danger(lambda x, y: 0.0)
        self.assertTrue(dodge.active)
        self.assertEqual(dodge.threat, "mortar")
        self.assertGreater(dodge.urgent, 0.0)

    def test_offer_and_pickups_pass_through(self):
        offer = {"choices": [{"id": "plating"}, {"id": "gun_rate"}]}
        pickups = [{"id": 8013, "kind": "laser", "pos": [2.0, 0.0, 0.0]}]
        self.p.update(support.obs(t=2.0, offer=offer, pickups=pickups))
        self.assertEqual(self.p.offer, offer)
        self.assertEqual(self.p.pickups[0]["kind"], "laser")

    def test_beam_bearing(self):
        self.p.update(support.obs(t=3.0, beams=[
            {"by": 5, "kind": "lightning", "path": [[10.0, 0.0, 5.0], [40.0, 0.0, 5.0]], "age": 0.1}]))
        self.assertIsNotNone(self.p.beam_bearing)
        self.assertAlmostEqual(self.p.beam_bearing, 0.0, places=6)
        self.assertEqual(self.p.beam_kind, "lightning")


class ZoneTests(unittest.TestCase):
    def _zone(self, t):
        return ZoneInfo.from_obs(support.zone(radius=120.0, next_radius=60.0,
                                              shrink_start=10.0, shrink_end=60.0), t)

    def test_predicted_circle_interpolates(self):
        z = self._zone(35.0)
        _c, r = z.predicted_circle()
        self.assertAlmostEqual(r, 90.0)
        z = self._zone(5.0)
        _c, r = z.predicted_circle()
        self.assertAlmostEqual(r, 120.0)
        z = self._zone(65.0)
        _c, r = z.predicted_circle()
        self.assertAlmostEqual(r, 60.0)

    def test_outside_zone_is_urgent(self):
        risk = self._zone(35.0).risk((200.0, 0.0), 11.7, 19.5)
        self.assertTrue(risk.outside)
        self.assertGreaterEqual(risk.urgency, 3.0)
        self.assertTrue(risk.boost)

    def test_inside_zone_is_calm(self):
        risk = self._zone(35.0).risk((0.0, 0.0), 11.7, 19.5)
        self.assertFalse(risk.outside)
        self.assertLess(risk.urgency, 1.0)

    def test_safe_point_is_inside_circle(self):
        z = self._zone(35.0)
        c, r = z.predicted_circle()
        x, y = z.safe_point((200.0, 0.0), margin=12.0)
        self.assertLessEqual(math.hypot(x - c[0], y - c[1]), max(1.0, r - 12.0) + 1e-6)


if __name__ == "__main__":
    unittest.main()
