"""Central action validation.

Every action the strategy builds passes through `ActionValidator.validate`, so a
malformed field can never reach the server.  The validator is total: on any
internal error it returns a minimal, protocol-valid action instead of raising.
It mirrors mechfight/1: move is a horizontal vector of length <= 1, aim is
[yaw, pitch] with the configured pitch clamp, fire is two slot triggers, take /
drop / pick are bounded by the current observation, and debug drawing is cut to
the caps advertised in `hello.config.debug`.
"""
from __future__ import annotations

import json
import math
import re
from typing import Any, Dict, List, Optional

_COLOR = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


def _num(v: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(f):
        return default
    return f


def _vec3(v: Any) -> Optional[List[float]]:
    if not isinstance(v, (list, tuple)) or len(v) < 3:
        return None
    out = []
    for c in v[:3]:
        f = _num(c)
        if f is None:
            return None
        out.append(f)
    return out


class ActionValidator:
    def __init__(self, config: dict) -> None:
        config = config or {}
        mech = config.get("mech") or {}
        self.min_pitch = float(mech.get("min_pitch", -0.7))
        self.max_pitch = float(mech.get("max_pitch", 1.3))
        loot = config.get("loot") or {}
        self.pickup_radius = float(loot.get("pickup_radius", 4.0))
        self.caps = dict(config.get("debug") or {})

    # ------------------------------------------------------------------ entry
    def validate(self, p, action: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        try:
            return self._validate(p, action or {})
        except Exception:
            return {
                "tick": int(getattr(p, "tick", 0) or 0),
                "move": [0.0, 0.0],
                "fire": [False, False],
            }

    def _validate(self, p, a: Dict[str, Any]) -> Dict[str, Any]:
        out: Dict[str, Any] = {"tick": int(p.tick)}

        move = a.get("move")
        x = y = 0.0
        if isinstance(move, (list, tuple)) and len(move) >= 2:
            x = _num(move[0], 0.0) or 0.0
            y = _num(move[1], 0.0) or 0.0
        n = math.hypot(x, y)
        if n > 1.0:
            x, y = x / n, y / n
        out["move"] = [x, y]

        aim = a.get("aim")
        yaw, pitch = float(p.aim[0]), float(p.aim[1])
        if isinstance(aim, (list, tuple)) and len(aim) >= 2:
            yw = _num(aim[0])
            pt = _num(aim[1])
            if yw is not None:
                yaw = yw
            if pt is not None:
                pitch = pt
        yaw = math.atan2(math.sin(yaw), math.cos(yaw))
        pitch = max(self.min_pitch, min(self.max_pitch, pitch))
        out["aim"] = [yaw, pitch]

        fire = [False, False]
        rf = a.get("fire")
        if isinstance(rf, (list, tuple)):
            for i in (0, 1):
                if i < len(rf) and bool(rf[i]) and i < len(p.weapons) and p.weapons[i]:
                    fire[i] = True
        out["fire"] = fire

        if a.get("boost"):
            out["boost"] = True
        if a.get("jump"):
            out["jump"] = True
        if a.get("reload") and p.slot_of("gun") is not None:
            out["reload"] = True

        offer = p.offer or {}
        choices = offer.get("choices") or []
        pick = _num(a.get("pick"))
        if pick is not None and choices:
            idx = int(pick)
            if 0 <= idx < len(choices):
                out["pick"] = idx

        take = _num(a.get("take"))
        if take is not None:
            tid = int(take)
            if self._visible_pickup(p, tid) is not None:
                out["take"] = tid
                drop = _num(a.get("drop"))
                if drop is not None:
                    slot = int(drop)
                    if slot in (0, 1) and slot < len(p.weapons) and p.weapons[slot]:
                        out["drop"] = slot

        dbg = self._debug(a.get("debug"))
        if dbg is not None:
            out["debug"] = dbg
        return out

    # ------------------------------------------------------------------ helpers
    def _visible_pickup(self, p, tid: int):
        for pk in p.pickups:
            try:
                if int(pk.get("id", -1)) != tid:
                    continue
                pos = pk.get("pos") or (0, 0, 0)
                dh = math.hypot(float(pos[0]) - p.pos[0], float(pos[1]) - p.pos[1])
                dz = abs(float(pos[2]) - p.pos[2])
            except (TypeError, ValueError, IndexError):
                continue
            if dh <= self.pickup_radius + 0.6 and dz <= 4.2:
                return pk
        return None

    def _debug(self, d: Any) -> Optional[Dict[str, Any]]:
        if not isinstance(d, dict):
            return None
        caps = self.caps
        max_lines = int(caps.get("max_lines", 24) or 0)
        max_points = int(caps.get("max_points", 24) or 0)
        max_text = int(caps.get("max_text", 8) or 0)
        max_chars = int(caps.get("max_text_chars", 80) or 0)
        max_bytes = int(caps.get("max_bytes", 4096) or 0)
        out: Dict[str, Any] = {}
        lines = []
        for ln in (d.get("lines") or [])[:max_lines]:
            if not isinstance(ln, dict):
                continue
            a = _vec3(ln.get("a"))
            b = _vec3(ln.get("b"))
            if a is None or b is None:
                continue
            it: Dict[str, Any] = {"a": a, "b": b}
            c = ln.get("color")
            if isinstance(c, str) and _COLOR.match(c):
                it["color"] = c
            lines.append(it)
        if lines:
            out["lines"] = lines
        points = []
        for pt in (d.get("points") or [])[:max_points]:
            if not isinstance(pt, dict):
                continue
            at = _vec3(pt.get("at"))
            if at is None:
                continue
            it = {"at": at}
            c = pt.get("color")
            if isinstance(c, str) and _COLOR.match(c):
                it["color"] = c
            size = _num(pt.get("size"))
            if size is not None:
                it["size"] = max(1.0, min(12.0, size))
            points.append(it)
        if points:
            out["points"] = points
        texts = []
        for tx in (d.get("text") or [])[:max_text]:
            if not isinstance(tx, dict):
                continue
            s = str(tx.get("text", ""))[:max_chars]
            if not s:
                continue
            it = {"text": s}
            at = _vec3(tx.get("at"))
            if at is not None:
                it["at"] = at
            c = tx.get("color")
            if isinstance(c, str) and _COLOR.match(c):
                it["color"] = c
            texts.append(it)
        if texts:
            out["text"] = texts
        label = d.get("label")
        if isinstance(label, str) and label:
            out["label"] = label[:max_chars]
        if not out:
            return None
        while out and len(json.dumps(out, separators=(",", ":")).encode()) > max_bytes:
            longest = None
            for key in ("lines", "points", "text"):
                items = out.get(key)
                if items and (longest is None or len(items) > len(out[longest])):
                    longest = key
            if longest is not None:
                out[longest].pop()
                if not out[longest]:
                    del out[longest]
            elif "label" in out:
                del out["label"]
            else:
                return None
        return out or None
