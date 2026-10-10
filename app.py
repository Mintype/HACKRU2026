import random
import sqlite3
import time
from pathlib import Path

from flask import Flask, Response, g, jsonify, render_template, request

from camera import Camera

BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "posture.db"

app = Flask(__name__)
cam = Camera()  # started in __main__ so the camera is on whenever the server is

# Settings kept in memory for now (0 = polite, 100 = insulting)
settings = {"insult_level": 20}


# ---------- Database ----------
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS posture_readings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,           -- unix timestamp
            score REAL NOT NULL,        -- 0-100, higher = better posture
            neck_angle REAL,            -- degrees (from OpenCV later)
            back_angle REAL
        );
        CREATE TABLE IF NOT EXISTS reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            body TEXT NOT NULL
        );
        """
    )
    db.commit()
    db.close()


# ---------- Pages ----------
@app.route("/")
def index():
    return render_template("index.html")


# ---------- API ----------
@app.route("/api/health")
def health():
    return jsonify(status="ok", time=time.time())


@app.route("/api/readings", methods=["POST"])
def add_reading():
    """The OpenCV loop will POST (or call directly) with a posture score."""
    data = request.get_json(silent=True) or {}
    if "score" not in data:
        return jsonify(error="score is required"), 400

    db = get_db()
    db.execute(
        "INSERT INTO posture_readings (ts, score, neck_angle, back_angle) VALUES (?, ?, ?, ?)",
        (time.time(), float(data["score"]), data.get("neck_angle"), data.get("back_angle")),
    )
    db.commit()
    return jsonify(ok=True), 201


@app.route("/api/readings")
def list_readings():
    limit = request.args.get("limit", 2000, type=int)
    since = request.args.get("since", 0, type=float)  # seconds ago; 0 = all time
    cutoff = time.time() - since if since else 0
    rows = get_db().execute(
        "SELECT ts, score, neck_angle, back_angle FROM posture_readings "
        "WHERE ts >= ? ORDER BY ts DESC LIMIT ?",
        (cutoff, limit),
    ).fetchall()
    return jsonify([dict(r) for r in reversed(rows)])


@app.route("/api/seed", methods=["POST"])
def seed_demo_data():
    """Fill the DB with fake readings that slowly improve (for demos)."""
    db = get_db()
    now = time.time()
    n = 200
    for i in range(n):
        ts = now - (n - i) * 300  # one reading every 5 minutes
        trend = 45 + 30 * (i / n)  # improves from ~45 to ~75
        score = max(0, min(100, trend + random.gauss(0, 10)))
        db.execute(
            "INSERT INTO posture_readings (ts, score) VALUES (?, ?)", (ts, score)
        )
    db.commit()
    return jsonify(ok=True, added=n), 201


@app.route("/api/readings", methods=["DELETE"])
def clear_readings():
    db = get_db()
    db.execute("DELETE FROM posture_readings")
    db.commit()
    return jsonify(ok=True)


@app.route("/api/settings", methods=["GET", "POST"])
def api_settings():
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        level = data.get("insult_level")
        if not isinstance(level, (int, float)) or not 0 <= level <= 100:
            return jsonify(error="insult_level must be a number 0-100"), 400
        settings["insult_level"] = level
    return jsonify(settings)


@app.route("/api/camera/status")
def camera_status():
    return jsonify(cam.status())


@app.route("/api/cameras", methods=["GET", "POST"])
def api_cameras():
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        index = data.get("index")
        if not isinstance(index, int) or index < 0:
            return jsonify(error="index must be a non-negative integer"), 400
        cam.set_index(index)
    return jsonify(cam.available_cameras())


@app.route("/video_feed")
def video_feed():
    """Debug: live MJPEG view with the detection box drawn on it."""
    return Response(cam.mjpeg(), mimetype="multipart/x-mixed-replace; boundary=frame")


@app.route("/api/reports")
def list_reports():
    rows = get_db().execute(
        "SELECT id, ts, body FROM reports ORDER BY ts DESC LIMIT 20"
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/reports/generate", methods=["POST"])
def generate_report():
    # TODO: pull readings, send to Gemini, store the response
    return jsonify(error="not implemented yet"), 501


if __name__ == "__main__":
    init_db()
    cam.start()  # camera runs in a background thread for as long as the server is up
    # debug/reloader OFF: the reloader would start a second process that fights
    # over the camera, and the Flask debugger is unsafe on a public server.
    # 0.0.0.0 so other devices on the network (or a tunnel) can reach it.
    app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False, threaded=True)