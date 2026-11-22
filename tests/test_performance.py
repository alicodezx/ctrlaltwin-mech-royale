import statistics
import time
import unittest

from strategy import Strategy
from world_model import WorldModel

from . import support


class PerformanceTests(unittest.TestCase):
    """Timing on this development machine, not the 1-CPU match container.

    The hard server budget is 50 ms per decision; the internal target is a p99
    under 10 ms.  The assertion here is deliberately looser (25 ms) so a busy
    developer machine does not produce false failures; the measured numbers are
    printed and reported separately.
    """

    @classmethod
    def setUpClass(cls):
        cls.hello, cls.tmp = support.make_hello()
        t0 = time.perf_counter()
        cls.world = WorldModel(cls.hello)
        cls.init_ms = (time.perf_counter() - t0) * 1000.0

    @classmethod
    def tearDownClass(cls):
        support.remove_arena(cls.tmp)

    @staticmethod
    def _sequence(n=100):
        out = []
        for i in range(n):
            t = 10.0 + i * 0.2
            x = 0.02 * i
            out.append(support.obs(
                t=t, aim=(0.2, -0.05), energy=100.0 - (i % 30),
                mechs=[support.mech(5, (90.0 - x, 10.0, 0.0), aim=(3.0, 0.0),
                                    weapons=("gun", "laser")),
                       support.mech(6, (40.0, -60.0 + x, 0.0), aim=(0.4, 0.0),
                                    weapons=("lightning",))],
                pickups=[{"id": 8013, "kind": "laser", "pos": [8.0, 3.0, 0.0]}],
                crates=[{"id": 1007, "pos": [70.0, -20.0, 0.0], "hp": 300.0}],
                drops=[{"id": 20001, "pos": [30.0, 25.0, 0.0], "radius": 8.0,
                        "lands_in": max(0.0, 20.0 - i * 0.2), "capture_s": 4,
                        "progress": 0, "holder": None}],
                projectiles=[support.bullet((-20.0 + x, 5.0, 4.5), (250.0, 0.0, 0.0))]
                if i % 7 == 0 else [],
                zone_obs=support.zone(radius=120.0 - i * 0.3),
            ))
        return out

    def test_decision_latency(self):
        seq = self._sequence()
        s = Strategy(support.CONFIG, self.world, seed=11)
        for o in seq[:5]:
            s.decide(o)
        times = []
        for o in seq:
            t0 = time.perf_counter()
            s.decide(o)
            times.append((time.perf_counter() - t0) * 1000.0)
        times.sort()
        p50 = times[len(times) // 2]
        p90 = times[int(len(times) * 0.9)]
        p99 = times[min(len(times) - 1, int(len(times) * 0.99))]
        peak = times[-1]
        print("\nworld init %.1f ms; decisions p50 %.2f  p90 %.2f  p99 %.2f  max %.2f ms; mean %.2f"
              % (self.init_ms, p50, p90, p99, peak, statistics.fmean(times)))
        self.assertLess(self.init_ms, 20000.0, "world model must fit the hello budget")
        self.assertLess(p99, 25.0, "p99 decision time %.2f ms" % p99)
        self.assertLess(peak, 50.0, "hard budget is 50 ms per decision")

    def test_validator_latency_is_negligible(self):
        s = Strategy(support.CONFIG, self.world, seed=12)
        o = self._sequence(1)[0]
        s.decide(o)
        times = []
        for _ in range(50):
            t0 = time.perf_counter()
            s.validator.validate(s.p, {"move": [1, 1], "aim": [0.3, 0.1],
                                       "fire": [True, False],
                                       "debug": {"label": "x", "text": [{"text": "hi"}]}})
            times.append((time.perf_counter() - t0) * 1000.0)
        self.assertLess(max(times), 5.0)


if __name__ == "__main__":
    unittest.main()
