import json
import math
import unittest

from action_safety import ActionValidator
from perception import Perception

from . import support


def _world_obs(**kw):
    kw.setdefault("weapons", [support.gun(), None])
    return support.obs(**kw)


class ActionValidatorTests(unittest.TestCase):
    def setUp(self):
        self.cfg = json.loads(json.dumps(support.CONFIG))
        self.validator = ActionValidator(self.cfg)
        self.p = Perception(self.cfg)
        self.p.update(_world_obs(t=4.0, pickups=[
            {"id": 8013, "kind": "laser", "pos": [2.0, 0.0, 0.0]},
            {"id": 8014, "kind": "mortar", "pos": [40.0, 0.0, 0.0]},
        ], offer={"choices": [{"id": "plating"}, {"id": "servos"}, {"id": "jets"}]}))

    def test_tick_and_background_defaults(self):
        a = self.validator.validate(self.p, {})
        self.assertEqual(a["tick"], self.p.tick)
        self.assertEqual(a["move"], [0.0, 0.0])
        self.assertEqual(a["fire"], [False, False])
        self.assertIn("aim", a)

    def test_move_is_clamped_to_unit_length(self):
        a = self.validator.validate(self.p, {"move": [3.0, 4.0]})
        self.assertAlmostEqual(math.hypot(*a["move"]), 1.0)
        self.assertAlmostEqual(a["move"][0], 0.6)
        self.assertAlmostEqual(a["move"][1], 0.8)
        a = self.validator.validate(self.p, {"move": ["nan", None]})
        self.assertEqual(a["move"], [0.0, 0.0])

    def test_aim_wraps_yaw_and_clamps_pitch(self):
        a = self.validator.validate(self.p, {"aim": [3.0 * math.pi, 9.0]})
        self.assertAlmostEqual(a["aim"][0], math.pi)
        self.assertAlmostEqual(a["aim"][1], 1.3)
        a = self.validator.validate(self.p, {"aim": [0.0, -9.0]})
        self.assertAlmostEqual(a["aim"][1], -0.7)

    def test_fire_only_for_occupied_slots(self):
        a = self.validator.validate(self.p, {"fire": [True, True]})
        self.assertEqual(a["fire"], [True, False])

    def test_pick_is_bounded_by_offer(self):
        self.assertEqual(self.validator.validate(self.p, {"pick": 2})["pick"], 2)
        self.assertNotIn("pick", self.validator.validate(self.p, {"pick": 3}))
        self.assertNotIn("pick", self.validator.validate(self.p, {"pick": -1}))
        self.p.offer = None
        self.assertNotIn("pick", self.validator.validate(self.p, {"pick": 0}))

    def test_take_requires_a_near_visible_pickup(self):
        near = self.validator.validate(self.p, {"take": 8013})
        self.assertEqual(near["take"], 8013)
        far = self.validator.validate(self.p, {"take": 8014})
        self.assertNotIn("take", far)
        self.assertNotIn("take", self.validator.validate(self.p, {"take": 9999}))

    def test_drop_only_with_take_and_occupied_slot(self):
        a = self.validator.validate(self.p, {"take": 8013, "drop": 0})
        self.assertEqual(a["take"], 8013)
        self.assertEqual(a["drop"], 0)
        # Slot 1 is empty, so dropping it makes no sense.
        self.assertNotIn("drop", self.validator.validate(self.p, {"take": 8013, "drop": 1}))

    def test_reload_requires_a_gun(self):
        self.assertTrue(self.validator.validate(self.p, {"reload": True})["reload"])
        self.p.update(_world_obs(t=4.2, weapons=[None, support.laser()]))
        self.assertNotIn("reload", self.validator.validate(self.p, {"reload": True}))

    def test_debug_caps_are_enforced(self):
        d = {
            "label": "x" * 500,
            "lines": [{"a": [0, 0, 0], "b": [1, 1, 1], "color": "#ff0000"}] * 80,
            "points": [{"at": [0, 0, 0], "size": 99.0, "color": "not-a-color"}] * 80,
            "text": [{"text": "y" * 500}] * 40,
        }
        out = self.validator.validate(self.p, {"debug": d})
        dbg = out["debug"]
        self.assertLessEqual(len(dbg["lines"]), 64)
        self.assertLessEqual(len(dbg["points"]), 64)
        self.assertLessEqual(len(dbg["text"]), 16)
        self.assertLessEqual(len(dbg["label"]), 80)
        self.assertEqual(dbg["points"][0]["size"], 12.0)
        self.assertNotIn("color", dbg["points"][0])
        self.assertEqual(len(dbg["text"][0]["text"]), 80)
        self.assertEqual(dbg["lines"][0]["color"], "#ff0000")
        self.assertLessEqual(len(json.dumps(dbg, separators=(",", ":")).encode()), 8192)

    def test_debug_is_trimmed_to_byte_cap(self):
        self.validator.caps["max_bytes"] = 300
        d = {"text": [{"text": "z" * 80, "at": [123456.789, 987654.321, -12345.678]}] * 16}
        out = self.validator.validate(self.p, {"debug": d})
        size = len(json.dumps(out["debug"], separators=(",", ":")).encode())
        self.assertLessEqual(size, 300)

    def test_validator_is_total(self):
        for bad in (None, "nonsense", {"move": object(), "aim": object(),
                                       "fire": object(), "pick": object(), "take": object(),
                                       "drop": object(), "debug": {"lines": object()}}):
            a = self.validator.validate(self.p, bad)
            self.assertIsInstance(a, dict)
            self.assertEqual(set(a) - {"tick", "move", "aim", "fire", "boost", "jump",
                                       "reload", "pick", "take", "drop", "debug"}, set())
            self.assertEqual(a["tick"], self.p.tick)


if __name__ == "__main__":
    unittest.main()
