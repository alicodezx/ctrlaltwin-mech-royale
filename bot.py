"""CtrlAltWin v1 - mechfight/1 entry point.

`mechfight.py` is the untouched official wrapper: it owns the protocol stream,
echoes the tick and exits on `end`.  This file only builds the world model and
the strategy, and keeps any strategy failure from taking the mech down.
"""
import sys
import traceback

import mechfight as mf
from strategy import Strategy
from world_model import WorldModel


class Bot:
    name = "ctrlaltwin-v1"

    def __init__(self, hello: dict) -> None:
        config = hello.get("config") or {}
        seed = int(hello.get("seed", 0) or 0)
        world = WorldModel(hello, log=mf.log)
        ok, reason = world.validation
        mf.log(f"world model: {'ok' if ok else 'unavailable'} ({reason})")
        self.strategy = Strategy(config, world, seed=seed, log=mf.log)
        self.last_action = None

    def act(self, obs: dict) -> dict:
        try:
            self.last_action = self.strategy.decide(obs)
            return self.last_action
        except Exception:
            mf.log("act() failed:\n" + traceback.format_exc())
            try:
                if self.last_action is not None:
                    fallback = dict(self.last_action)
                    fallback["tick"] = obs.get("tick", 0)
                    fallback["fire"] = [False, False]
                    return fallback
                return self.strategy.fallback(obs)
            except Exception:
                mf.log("fallback failed:\n" + traceback.format_exc())
                return {"tick": obs.get("tick", 0)}


if __name__ == "__main__":
    mf.run(Bot)
