# Mech Royale 2026 — Baseline Python Bot

Entry for the **JoinBuilders 2026 Mech Royale Buildathon**: a bot that pilots one mech in a
16-mech free-for-all using protocol `mechfight/1`.

This repository contains the **official Python starter kit bot, unmodified**. It is submitted to
establish the server-side baseline score before strategy work begins.

## What the bot does

The starter strategy, unchanged from the official kit:

- walks toward the next zone center,
- picks up a non-gun weapon when the second slot is empty,
- aims at and fires both weapon slots at the nearest visible mech,
- draws debug lines/labels (visible only to the team, never to spectators or replays).

## Repository layout

| File | Purpose |
| --- | --- |
| `Dockerfile` | Submission image: `python:3.13-slim`, entrypoint runs the bot unbuffered |
| `example_bot.py` | Bot implementation (the starter strategy) |
| `mechfight.py` | Single-file protocol wrapper: typed messages, stdin/stdout loop, debug drawing |
| `.dockerignore` | Keeps VCS and bytecode artifacts out of the image |
| `README.md` | This file |

Dependencies: **Python standard library only** (Python 3.9+; the image uses 3.13). No
`requirements.txt` and no package installation are needed.

## How it talks to the game

- **stdin**: one JSON object per line — `hello` once, then `obs` every decision, then `end`.
- **stdout**: one JSON object per line — `ready` once, then one action per observation, echoing its `tick`.
- **stderr**: logs only.
- Every decision has a 50 ms wall-clock budget; the wrapper answers only the newest observation
  when several are queued, keeps the mech coasting if the bot raises, and exits on `end`.
- At runtime the bot has no network, a read-only filesystem except `/tmp`, 1 CPU, 512 MB RAM,
  an unprivileged user (uid 65534), and the static map mounted read-only at `/arena`.

## Build the submission image

```sh
docker build -t mech-royale-baseline .
```

## Test locally

The conformance checker ships with the official bot kit (not included in this bot repository).
From the kit root, point it at this repository's bot:

```sh
python3 check/check.py -- python3 -u /path/to/this/repo/example_bot.py
```

Run the image exactly the way a match starts it (the same command the scorer uses as a
pre-match gate):

```sh
python3 check/check.py --map-dir /arena -- docker run --rm -i --network none --cpus 1 \
  --memory 512m --read-only --tmpfs /tmp:rw,size=64m --user 65534:65534 \
  -v "/path/to/bot-kit/arena:/arena:ro" mech-royale-baseline
```

The checker replays a recorded `hello`, 300 real observations, and `end`. It verifies readiness,
schema-valid actions with the correct tick, the 50 ms budget, newest-observation handling, debug
caps, and a clean exit. It is not a simulation: observations do not react to actions, so it
measures protocol compliance rather than skill.

## Baseline status

- Protocol conformance: PASS (5/5 runs) on the development machine.
- Decision time: about 2 ms at the median against the 50 ms budget.
- No gameplay score is claimed here; scores come from the 48 practice matches the scorer runs
  after submission.

## Submission checklist

- Public GitHub repository with `Dockerfile` at the repository root.
- Repository ≤ 20 MB at the submitted commit.
- Image builds on `linux/amd64` with network on, within 10 minutes.
- Submit the repository URL on the Mech Royale track page (joinbuilders.org/buildathon).
