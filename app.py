from __future__ import annotations

import atexit
import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, jsonify, render_template, request

app = Flask(__name__)
API_URL = (
    "https://boulderbar.net/wp-json/boulderbar/v1/capacity?locations=260,261,262,263,264,265,284"
)
DB_BASE_PATH = Path(os.environ.get("DB_PATH", ".")).expanduser()
DB_PATH = DB_BASE_PATH / "capacity.db"
scheduler = BackgroundScheduler()
scheduler_started = False
scheduler_lock = threading.Lock()


def init_db() -> None:
    """Initialize the SQLite database with the capacity table."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS capacity
               (timestamp TEXT NOT NULL,
                location_id INTEGER NOT NULL,
                location_name TEXT NOT NULL,
                capacity INTEGER NOT NULL,
                PRIMARY KEY (timestamp, location_id))"""
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_capacity_location_timestamp "
            "ON capacity (location_id, timestamp)"
        )
        conn.commit()


def fetch_and_store() -> None:
    """Fetch capacity data from API and store in database."""
    try:
        response = requests.get(API_URL, timeout=10)
        response.raise_for_status()
        data = response.json()

        if data.get("status") == 1:
            timestamp = datetime.now(timezone.utc).isoformat()
            with sqlite3.connect(DB_PATH) as conn:
                for location in data["data"]:
                    conn.execute(
                        "INSERT INTO capacity (timestamp, location_id, location_name, capacity) "
                        "VALUES (?, ?, ?, ?)",
                        (timestamp, location["id"], location["title"], location["capacity"]),
                    )
                conn.commit()
    except Exception as e:
        print(f"Error fetching data: {e}")


def start_scheduler() -> None:
    """Initialize the database and start the background scheduler."""
    init_db()
    fetch_and_store()
    if not scheduler.running:
        scheduler.add_job(func=fetch_and_store, trigger="interval", minutes=5)
        scheduler.start()


@app.before_request
def _ensure_scheduler_started() -> None:
    global scheduler_started
    if scheduler_started:
        return
    with scheduler_lock:
        if scheduler_started:
            return
        start_scheduler()
        scheduler_started = True


def _shutdown_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)


atexit.register(_shutdown_scheduler)


@app.route("/")
def index():
    """Render the main page."""
    return render_template("index.html")


@app.route("/api/data")
def get_data():
    """Get capacity data from database."""
    hours_param = request.args.get("hours", "24")
    try:
        hours = int(hours_param)
    except ValueError:
        hours = 24

    params: list[str] = []
    where_clause = ""

    if hours > 0:
        where_clause = "WHERE timestamp >= ?"
        params.append((datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat())

    query = (
        "SELECT timestamp, location_id, location_name, capacity "
        f"FROM capacity {where_clause} ORDER BY timestamp, location_id"
    )

    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.execute(query, params)
        rows = cursor.fetchall()

    data = {}
    for timestamp, _loc_id, loc_name, capacity in rows:
        if loc_name not in data:
            data[loc_name] = {"timestamps": [], "capacities": []}
        data[loc_name]["timestamps"].append(timestamp)
        data[loc_name]["capacities"].append(capacity)

    return jsonify(data)


if __name__ == "__main__":
    start_scheduler()
    app.run(debug=True, use_reloader=False)
