"""End-to-end smoke test against the Flask test client (simulation mode).

Runs without a camera, MediaPipe, OpenCV or any network access::

    python -m tests.smoke

It drives a sped-up scripted session so a real ``roast_due`` fires within a few
seconds, proving the whole chain works: readings -> state machine -> roast
service -> player -> SQLite -> API.
"""

from __future__ import annotations

import os
import sys
import time

os.environ.setdefault("POSTURE_DISABLE_LOCAL_TTS", "1")  # keep the smoke test fast
os.environ.setdefault("POSTURE_DATA_DIR", os.path.join(os.path.dirname(__file__), "..", ".smoke-data"))

from fakes.fake_readings import scripted_readings  # noqa: E402
from server.app import create_app  # noqa: E402


def main() -> int:
    # fps=2 -> reading timestamps step 0.5 s while the vision thread renders at
    # 10 fps, so the simulated day runs 5x faster than wall-clock time.
    app = create_app(
        simulate=True,
        no_audio=True,
        port=5999,
        settings_path=os.path.join(os.environ["POSTURE_DATA_DIR"], "settings.json"),
        fake_readings=scripted_readings(fps=2.0, cycles=3),
    )
    coach = app.coach
    coach.start()

    client = app.test_client()
    checks = []

    def check(name, response, want=200):
        ok = response.status_code == want
        checks.append((name, ok, response.status_code))
        return response

    def wait_for(predicate, timeout, interval=0.5):
        deadline = time.time() + timeout
        while time.time() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(interval)
        return None

    # -- wait for the scripted person to sit down ------------------------
    state = wait_for(lambda: client.get("/api/stats").get_json().get("state") not in (None, "away"), 20)
    print("  session :", state)

    page = check("GET /", client.get("/"))
    assert b"Posture Coach" in page.data, "index.html did not render"

    stats = check("GET /api/stats", client.get("/api/stats")).get_json()
    assert "live" in stats and "state" in stats, stats
    print("  state   :", stats.get("state"))

    check("GET /api/history", client.get("/api/history"))
    check("GET /api/status", client.get("/api/status"))
    settings = check("GET /api/settings", client.get("/api/settings")).get_json()
    assert settings["audio_routes"] == ["bluetooth", "cast", "off"], settings

    applied = check(
        "POST /api/settings",
        client.post("/api/settings", json={"roast_seconds": 7, "nice_mode": True}),
    ).get_json()
    assert applied["applied"]["roast_seconds"] == 7.0, applied

    roast = check("POST /api/test-roast", client.post("/api/test-roast")).get_json()
    assert roast["ok"] and roast["text"], roast
    print("  test    :", roast["text"])

    jpeg = coach.vision.latest_jpeg()
    print("  frame   :", f"{len(jpeg)} bytes JPEG" if jpeg else "no OpenCV (video feed disabled)")

    shame = check("GET /api/roasts", client.get("/api/roasts")).get_json()
    assert len(shame["roasts"]) <= 3, shame
    limited = check("GET /api/roasts?limit=1", client.get("/api/roasts?limit=1")).get_json()
    assert len(limited["roasts"]) <= 1, limited
    check("POST /api/calibrate", client.post("/api/calibrate"))

    # -- wait for a real roast_due to reach the DB -----------------------
    baseline = client.get("/api/stats").get_json().get("roasts", 0)
    roast_count = wait_for(
        lambda: (
            lambda n: n if n > baseline else None
        )(client.get("/api/stats").get_json().get("roasts", 0)),
        timeout=45,
    )
    checks.append(("roast_due reached SQLite", roast_count is not None, roast_count))
    if roast_count:
        recent = client.get("/api/roasts").get_json()["roasts"]
        print("  roasted :", recent[0]["text"], f"(session roasts: {roast_count})")

    final = client.get("/api/stats").get_json()
    print("  stats   :", {k: final.get(k) for k in ("good_percent", "slouch_count", "roasts", "longest_good_streak")})

    coach.stop()
    time.sleep(0.3)

    print()
    for name, ok, detail in checks:
        print(f"  [{'ok' if ok else 'FAIL'}] {name} -> {detail}")
    failed = [c for c in checks if not c[1]]
    print()
    if failed:
        print(f"{len(failed)} check(s) failed")
        return 1
    print(f"all {len(checks)} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
