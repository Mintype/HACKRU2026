#!/usr/bin/env python3
"""Print exactly when the state machine fires roast_due, for a given input.

Answers "when does it actually roast?" without a camera, a browser or a wait.

Usage
    python roast_timeline.py
    python roast_timeline.py --profile bad --cooldown 20
    python roast_timeline.py --roast 10 --cooldown 15
    python roast_timeline.py --profile bad --bad-score 0        # disable the score trigger

Profiles
    scripted  the --simulate sequence: away -> good -> sleep -> good -> slouch ...
    bad       seated and slouching hard from t=0 (trips the deep-slouch trigger)
    moderate  slouching but not deeply (score 0.55), which exercises the
              sustained timer instead of the deep-slouch trigger
    sleeping  collapsed, motionless and head down: trips the slow sleep trigger
    good      seated and sitting well the whole time (should never roast)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Iterator


def project_root() -> Path:
    """Find the repo root so the script runs from anywhere."""
    here = Path(__file__).resolve()
    for parent in [here.parent, *here.parents]:
        if (parent / "run.py").exists() and (parent / "server").is_dir():
            return parent
    raise SystemExit("could not locate the project root (expected run.py + server/)")


ROOT = project_root()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fakes.fake_readings import scripted_readings  # noqa: E402
from server.state_machine import PostureStateMachine, StateMachineConfig  # noqa: E402

INTERESTING = ("session_start", "calibrated", "roast_due", "session_end")


def profile_readings(name: str, seconds: float, fps: float) -> Iterator[dict]:
    if name == "scripted":
        yield from scripted_readings(start=0.0, fps=fps, cycles=1)
        return

    step = 1.0 / max(fps, 0.1)
    ts = 0.0
    while ts < seconds:
        if name == "bad":
            neck, torso, score, part, sleepy = 46.0, 6.0, 0.2, "neck", False
        elif name == "moderate":
            # below the slouch threshold but above the deep-slouch trigger, so
            # only the sustained timer can fire
            neck, torso, score, part, sleepy = 20.0, 6.0, 0.55, "neck", False
        elif name == "sleeping":
            # collapsed, not moving, head down: the slow trigger
            neck, torso, score, part, sleepy = 46.0, 18.0, 0.15, "torso", True
        else:
            neck, torso, score, part, sleepy = 12.0, 4.0, 0.97, "even", False
        yield {
            "ts": ts,
            "person": True,
            "seated": True,
            "neck_angle": neck,
            "torso_angle": torso,
            "score": score,
            "neck_drift": max(0.0, neck - 12.0),
            "torso_drift": max(0.0, torso - 4.0),
            "slouch_part": part,
            "head_down_deg": 55.0 if sleepy else 6.0,
            "motion": 0.001 if sleepy else 0.02,
            "sleepy": sleepy,
        }
        ts += step


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--profile", choices=("scripted", "bad", "moderate", "sleeping", "good"), default="scripted")
    parser.add_argument("--fps", type=float, default=10.0)
    parser.add_argument("--seconds", type=float, default=180.0, help="only used by bad/good")
    parser.add_argument("--roast", type=float, help="override roast_seconds")
    parser.add_argument("--cooldown", type=float, help="override cooldown_seconds")
    parser.add_argument("--bad-score", type=float, help="override bad_score_threshold (0 disables)")
    parser.add_argument("--bad-seconds", type=float, help="override bad_score_seconds")
    args = parser.parse_args(argv)

    config = StateMachineConfig()
    for attr, value in (
        ("roast_seconds", args.roast),
        ("cooldown_seconds", args.cooldown),
        ("bad_score_threshold", args.bad_score),
        ("bad_score_seconds", args.bad_seconds),
    ):
        if value is not None:
            setattr(config, attr, value)

    machine = PostureStateMachine(config)
    print(
        f"profile={args.profile}  roast={config.roast_seconds:g}s  "
        f"cooldown={config.cooldown_seconds:g}s  bad<{config.bad_score_threshold:g} for "
        f"{config.bad_score_seconds:g}s  sleep={config.sleep_threshold_seconds:g}s  "
        f"calibration={config.calibration_seconds:g}s"
    )
    print("-" * 80)

    last_roast: float | None = None
    for reading in profile_readings(args.profile, args.seconds, args.fps):
        for event in machine.update(reading):
            name = event.get("event")
            if name not in INTERESTING:
                continue
            detail = ""
            if name == "roast_due":
                gap = ""
                if last_roast is not None:
                    gap = f"   (+{event['ts'] - last_roast:.1f}s since the last roast)"
                last_roast = event["ts"]
                detail = (
                    f"   reason={event.get('reason')}  slouch#{event.get('slouch_count')}"
                    f"  part={event.get('slouch_part')}  score={event.get('score')}{gap}"
                )
            print(f"  t={event['ts']:7.1f}s  {name}{detail}")

    print("-" * 80)
    print(
        f"  roasts={machine.roasts_due}  slouch episodes={machine.slouch_count}  "
        f"final state={machine.state}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
