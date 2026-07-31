from __future__ import annotations

import atexit
import os
import sqlite3
import statistics
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, jsonify, render_template, request

app = Flask(__name__)
WEEKDAY_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
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


@app.route("/api/weekly-pattern")
def get_weekly_pattern():
    """Aggregate capacity by weekday+hour-of-day to show a typical week.

    For every (weekday, hour) bucket across all recorded weeks, returns the
    average capacity plus the 25th/75th percentile range, so the frontend
    can render a "typical week" chart with an average line and a percentile
    band. Bucketing is done in UTC, matching the stored timestamps.
    """
    weeks_param = request.args.get("weeks", "0")
    try:
        weeks = int(weeks_param)
    except ValueError:
        weeks = 0

    params: list[str] = []
    where_clause = ""
    if weeks > 0:
        where_clause = "WHERE timestamp >= ?"
        params.append((datetime.now(timezone.utc) - timedelta(weeks=weeks)).isoformat())

    query = f"SELECT timestamp, location_name, capacity FROM capacity {where_clause}"

    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.execute(query, params)
        rows = cursor.fetchall()

    # buckets[location][weekday][hour] = [capacity, capacity, ...]
    buckets: dict[str, list[list[list[int]]]] = {}
    for timestamp, loc_name, capacity in rows:
        dt = datetime.fromisoformat(timestamp)
        weekday = dt.weekday()  # Monday == 0
        hour = dt.hour
        loc_buckets = buckets.setdefault(loc_name, [[[] for _ in range(24)] for _ in range(7)])
        loc_buckets[weekday][hour].append(capacity)

    labels = [
        f"{WEEKDAY_LABELS[weekday]} {hour:02d}:00" for weekday in range(7) for hour in range(24)
    ]

    series = {}
    for loc_name, loc_buckets in buckets.items():
        avg_list: list[float | None] = []
        p25_list: list[float | None] = []
        p75_list: list[float | None] = []
        count_list: list[int] = []

        for weekday in range(7):
            for hour in range(24):
                values = loc_buckets[weekday][hour]
                count_list.append(len(values))
                if not values:
                    avg_list.append(None)
                    p25_list.append(None)
                    p75_list.append(None)
                elif len(values) == 1:
                    avg_list.append(round(values[0], 1))
                    p25_list.append(round(values[0], 1))
                    p75_list.append(round(values[0], 1))
                else:
                    q1, _median, q3 = statistics.quantiles(values, n=4, method="inclusive")
                    avg_list.append(round(statistics.fmean(values), 1))
                    p25_list.append(round(q1, 1))
                    p75_list.append(round(q3, 1))

        series[loc_name] = {
            "avg": avg_list,
            "p25": p25_list,
            "p75": p75_list,
            "count": count_list,
        }

    return jsonify({"labels": labels, "series": series})


if __name__ == "__main__":
    start_scheduler()
    app.run(debug=True, use_reloader=False)
