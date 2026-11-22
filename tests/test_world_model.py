import unittest

from world_model import WorldModel

from . import support


class WorldModelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # A 6 m thick, 120 m long wall centred on the origin, 12 m tall.
        cls.hello, cls.tmp = support.make_hello([support.wall(-3, -60, 3, 60)])
        cls.world = WorldModel(cls.hello)
        assert cls.world.validation[0], cls.world.validation

    @classmethod
    def tearDownClass(cls):
        support.remove_arena(cls.tmp)

    def test_map_loads(self):
        self.assertTrue(self.world.available)
        self.assertEqual(self.world.terrain_height(10.0, -20.0), 0.0)
        self.assertTrue(self.world.point_free(40.0, 20.0))
        self.assertFalse(self.world.point_free(0.0, 30.0))

    def test_los_through_wall_is_blocked(self):
        a = (-20.0, 30.0, 9.0)
        b = (20.0, 30.0, 9.0)
        self.assertTrue(self.world.los_blocked(a, b))
        self.assertTrue(self.world.los_blocked_fast(a, b))

    def test_los_over_wall_and_around_it(self):
        self.assertFalse(self.world.los_blocked((-20.0, 30.0, 20.0), (20.0, 30.0, 20.0)))
        self.assertFalse(self.world.los_blocked((-20.0, 90.0, 9.0), (20.0, 90.0, 9.0)))
        self.assertFalse(self.world.los_blocked_fast((-20.0, 90.0, 9.0), (20.0, 90.0, 9.0)))

    def test_path_goes_around_the_wall(self):
        path = self.world.find_path((-20.0, 30.0), (20.0, 30.0), max_expand=900, now=0.0)
        self.assertTrue(path, "no path around a finite wall")
        for x, y in path:
            self.assertFalse(self.world.point_free(x, y) is False,
                             "path point inside geometry: %r" % ((x, y),))
            self.assertFalse(self.world.cell_blocked[self.world.cell_index(x, y)])

    def test_unreachable_path_is_empty_not_an_error(self):
        # A goal deep inside the wall cannot be reached: A* must fail cleanly.
        path = self.world.find_path((-20.0, 30.0), (0.0, 30.0), max_expand=200, now=0.0)
        self.assertIsInstance(path, list)
        for x, y in path:
            self.assertTrue(self.world.point_free(x, y), "path point in geometry")


class MissingMapTests(unittest.TestCase):
    def test_missing_map_degrades_gracefully(self):
        hello = {"type": "hello", "config": dict(support.CONFIG),
                 "map": {"dir": "Z:/definitely/not/here", "files": ["v2/world.json"]}}
        world = WorldModel(hello)
        self.assertFalse(world.available)
        self.assertFalse(world.validation[0])
        self.assertFalse(world.los_blocked((0, 0, 0), (50, 0, 0)))
        # Without a map the navigator falls back to straight-line movement.
        self.assertEqual(world.find_path((0, 0), (50, 50)), [(50, 50)])


if __name__ == "__main__":
    unittest.main()
