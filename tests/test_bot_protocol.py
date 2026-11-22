import json
import os
import queue
import subprocess
import sys
import threading
import time
import unittest

from . import support

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class BotProcessTests(unittest.TestCase):
    """Runs bot.py as the server would: hello, one observation at a time, end."""

    @classmethod
    def setUpClass(cls):
        cls.hello, cls.tmp = support.make_hello()

    @classmethod
    def tearDownClass(cls):
        support.remove_arena(cls.tmp)

    def _run(self, observations, wait_s=25.0):
        proc = subprocess.Popen([sys.executable, "-u", "bot.py"], cwd=ROOT,
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE)
        lines = queue.Queue()

        def reader():
            for raw in proc.stdout:
                lines.put(raw)
            lines.put(None)

        threading.Thread(target=reader, daemon=True).start()

        def send(m):
            proc.stdin.write((json.dumps(m) + "\n").encode())
            proc.stdin.flush()

        def read_one(deadline):
            try:
                raw = lines.get(timeout=max(0.05, deadline - time.time()))
            except queue.Empty:
                return None
            if raw is None:
                return None
            return json.loads(raw)

        out = []
        try:
            send(self.hello)
            ready = read_one(time.time() + wait_s)
            if ready is not None:
                out.append(ready)
            for o in observations:
                send(o)
                a = read_one(time.time() + wait_s)
                if a is None:
                    break
                out.append(a)
            send({"type": "end", "place": 3, "kills": 0})
        finally:
            try:
                proc.stdin.close()
            except OSError:
                pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        err = proc.stderr.read().decode("utf-8", "replace")
        try:
            proc.stdout.close()
            proc.stderr.close()
        except OSError:
            pass
        return out, err, proc.returncode

    def test_ready_actions_and_clean_exit(self):
        obs1 = support.obs(t=10.0, aim=(0.0, 0.0),
                           mechs=[support.mech(5, (90.0, 0.0, 0.0), aim=(0.0, 0.0))])
        obs2 = support.obs(t=10.2, aim=(0.0, 0.0),
                           mechs=[support.mech(5, (88.0, 2.0, 0.0), aim=(0.0, 0.0))])
        out, err, code = self._run([obs1, obs2])
        self.assertEqual(len(out), 3, "expected ready + 2 actions, stderr: %s" % err)
        self.assertEqual(out[0]["type"], "ready")
        self.assertEqual(out[0]["name"], "ctrlaltwin-v1")
        self.assertEqual(out[1]["tick"], obs1["tick"])
        self.assertEqual(out[2]["tick"], obs2["tick"])
        for a in out[1:]:
            self.assertEqual(len(a["move"]), 2)
            self.assertLessEqual((a["move"][0] ** 2 + a["move"][1] ** 2) ** 0.5, 1.0001)
            self.assertEqual(len(a["fire"]), 2)
            self.assertEqual(len(a["aim"]), 2)
        self.assertEqual(code, 0, "bot should exit cleanly after end, stderr: %s" % err)

    def test_bot_survives_odd_observations(self):
        weird = support.obs(t=3.0)
        weird["mechs"] = [{"id": "x"}, {"pos": []}]
        weird["projectiles"] = [{"kind": "gun", "pos": [1, 2]}]
        out, err, code = self._run([weird])
        self.assertEqual(len(out), 2, "stderr: %s" % err)
        self.assertEqual(out[1]["tick"], weird["tick"])
        self.assertEqual(code, 0, "stderr: %s" % err)


if __name__ == "__main__":
    unittest.main()
