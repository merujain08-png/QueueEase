
from flask import (
    Flask, jsonify, request, render_template,
    redirect, url_for, session
)
from functools import wraps
from datetime import datetime, timezone, timedelta
import sqlite3
import os

app = Flask(__name__)

app.secret_key = os.environ.get(
    "QUEUEEASE_SECRET_KEY", "development-secret-change-this"
)

DB = "queue.db"

ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = os.environ.get(
    "QUEUEEASE_ADMIN_PASSWORD", "QueueEase@2026"
)

SERVICES = {
    "Healthcare": "H",
    "Education": "E",
    "Banking": "B",
    "Government": "G",
    "Service Center": "S"
}


def now():
    return datetime.now(timezone.utc)


def connect_db():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    return con


# -------------------------------
# ADMIN AUTHENTICATION
# -------------------------------

def admin_required(function):
    @wraps(function)
    def wrapper(*args, **kwargs):
        if not session.get("admin_logged_in"):
            if request.path.startswith("/api/"):
                return jsonify({"error": "Login required"}), 401
            return redirect(url_for("login"))
        return function(*args, **kwargs)
    return wrapper


@app.route("/login", methods=["GET", "POST"])
def login():

    if session.get("admin_logged_in"):
        return redirect(url_for("admin"))

    error = None

    if request.method == "POST":

        username = request.form.get("username", "")
        password = request.form.get("password", "")

        if (
            username == ADMIN_USERNAME
            and password == ADMIN_PASSWORD
        ):
            session.clear()
            session["admin_logged_in"] = True
            return redirect(url_for("admin"))

        error = "Invalid username or password"

    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# -------------------------------
# DATABASE SETUP AND MIGRATION
# -------------------------------

def initialize_db():

    with connect_db() as con:

        con.execute("""
            CREATE TABLE IF NOT EXISTS tokens (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                token TEXT UNIQUE,
                status TEXT DEFAULT 'waiting',
                created_at TEXT,
                started_at TEXT,
                completed_at TEXT,
                service TEXT DEFAULT 'Service Center',
                counter_id INTEGER
            )
        """)

        con.execute("""
            CREATE TABLE IF NOT EXISTS counters (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                service TEXT NOT NULL,
                name TEXT NOT NULL,
                UNIQUE(service, name)
            )
        """)

        columns = {
            row["name"]
            for row in con.execute("PRAGMA table_info(tokens)")
        }

        migrations = {
            "created_at": "TEXT",
            "started_at": "TEXT",
            "completed_at": "TEXT",
            "service": "TEXT DEFAULT 'Service Center'",
            "counter_id": "INTEGER"
        }

        for column, datatype in migrations.items():
            if column not in columns:
                con.execute(
                    f"ALTER TABLE tokens ADD COLUMN {column} {datatype}"
                )

        # Create two counters for every sector
        for service in SERVICES:

            for number in range(1, 3):

                con.execute("""
                    INSERT OR IGNORE INTO counters (service, name)
                    VALUES (?, ?)
                """, (service, f"Counter {number}"))

        # Preserve old tokens and assign them to Counter 1
        for service in SERVICES:

            counter = con.execute("""
                SELECT id FROM counters
                WHERE service=?
                ORDER BY id LIMIT 1
            """, (service,)).fetchone()

            con.execute("""
                UPDATE tokens
                SET counter_id=?
                WHERE service=? AND counter_id IS NULL
            """, (counter["id"], service))

        con.execute("""
            UPDATE tokens
            SET created_at=?
            WHERE created_at IS NULL
        """, (now().isoformat(),))


def valid_service(service):
    return service in SERVICES


def get_counter(con, counter_id, service):

    return con.execute("""
        SELECT * FROM counters
        WHERE id=? AND service=?
    """, (counter_id, service)).fetchone()


# -------------------------------
# WAITING TIME CALCULATION
# -------------------------------

def average_service_time(con, service, counter_id):

    rows = con.execute("""
        SELECT started_at, completed_at
        FROM tokens
        WHERE service=?
        AND counter_id=?
        AND status='completed'
        AND started_at IS NOT NULL
        AND completed_at IS NOT NULL
        ORDER BY id DESC
        LIMIT 10
    """, (service, counter_id)).fetchall()

    durations = []

    for row in rows:
        start = datetime.fromisoformat(row["started_at"])
        end = datetime.fromisoformat(row["completed_at"])

        minutes = (end - start).total_seconds() / 60

        if 0 < minutes < 120:
            durations.append(minutes)

    if not durations:
        return 5

    return sum(durations) / len(durations)


# -------------------------------
# CUSTOMER PAGES
# -------------------------------

@app.route("/")
def home():
    return render_template("index.html")


@app.route("/api/services")
def get_services():
    return jsonify(list(SERVICES.keys()))


@app.route("/api/counters")
def customer_counters():

    service = request.args.get("service")

    if not valid_service(service):
        return jsonify({"error": "Invalid service"}), 400

    with connect_db() as con:

        rows = con.execute("""
            SELECT id, name FROM counters
            WHERE service=?
            ORDER BY id
        """, (service,)).fetchall()

    return jsonify([dict(row) for row in rows])


# -------------------------------
# SMART TOKEN GENERATION
# -------------------------------

@app.route("/api/token", methods=["POST"])
def generate_token():

    data = request.get_json(silent=True) or {}

    service = data.get("service")

    if not valid_service(service):
        return jsonify({"error": "Invalid service"}), 400

    prefix = SERVICES[service]

    with connect_db() as con:

        # Find the counter with the smallest active queue
        counters = con.execute("""
            SELECT c.id,
            (
                SELECT COUNT(*)
                FROM tokens t
                WHERE t.counter_id=c.id
                AND t.status IN ('waiting', 'serving')
            ) AS load
            FROM counters c
            WHERE c.service=?
            ORDER BY load ASC, c.id ASC
        """, (service,)).fetchall()

        if not counters:
            return jsonify({"error": "No counters available"}), 400

        selected_counter = counters[0]["id"]

        # Generate unique sector token
        rows = con.execute("""
            SELECT token FROM tokens
            WHERE service=?
        """, (service,)).fetchall()

        numbers = []

        for row in rows:
            value = row["token"]

            if value.startswith(prefix) and value[1:].isdigit():
                numbers.append(int(value[1:]))

        token = f"{prefix}{max(numbers, default=0) + 1:03d}"

        con.execute("""
            INSERT INTO tokens
            (token, status, created_at, service, counter_id)
            VALUES (?, 'waiting', ?, ?, ?)
        """, (
            token,
            now().isoformat(),
            service,
            selected_counter
        ))

        counter = con.execute("""
            SELECT name FROM counters WHERE id=?
        """, (selected_counter,)).fetchone()

    return jsonify({
        "message": "Token generated successfully",
        "token": token,
        "service": service,
        "counter_id": selected_counter,
        "counter": counter["name"]
    })


# -------------------------------
# CUSTOMER TOKEN TRACKING
# -------------------------------

@app.route("/api/status/<token>")
def check_status(token):

    with connect_db() as con:

        user = con.execute("""
            SELECT * FROM tokens WHERE token=?
        """, (token,)).fetchone()

        if not user:
            return jsonify({"error": "Token not found"}), 404

        service = user["service"]
        counter_id = user["counter_id"]

        ahead = con.execute("""
            SELECT COUNT(*) FROM tokens
            WHERE service=?
            AND counter_id=?
            AND id < ?
            AND status='waiting'
        """, (service, counter_id, user["id"])).fetchone()[0]

        serving = con.execute("""
            SELECT * FROM tokens
            WHERE counter_id=? AND status='serving'
            LIMIT 1
        """, (counter_id,)).fetchone()

        avg = average_service_time(
            con, service, counter_id
        )

        remaining = 0

        if serving and serving["started_at"]:

            start = datetime.fromisoformat(serving["started_at"])

            elapsed = (
                now() - start
            ).total_seconds() / 60

            remaining = max(0, avg - elapsed)

        wait = 0

        if user["status"] == "waiting":
            wait = ahead * avg + remaining

        people_ahead = ahead

        if user["status"] == "waiting" and serving:
            people_ahead += 1

        counter = con.execute("""
            SELECT name FROM counters WHERE id=?
        """, (counter_id,)).fetchone()

    return jsonify({
        "token": user["token"],
        "service": service,
        "counter": counter["name"],
        "status": user["status"],
        "people_ahead": people_ahead,
        "estimated_wait_minutes": round(wait),
        "average_service_minutes": round(avg, 1)
    })


# -------------------------------
# ADMIN DASHBOARD
# -------------------------------

@app.route("/admin")
@admin_required
def admin():
    return render_template("admin.html")


# -------------------------------
# ADMIN QUEUE
# -------------------------------

@app.route("/api/queue")
@admin_required
def get_queue():

    service = request.args.get("service")
    counter_id = request.args.get("counter_id", type=int)

    if not valid_service(service) or not counter_id:
        return jsonify({"error": "Select a valid counter"}), 400

    with connect_db() as con:

        if not get_counter(con, counter_id, service):
            return jsonify({"error": "Counter not found"}), 404

        rows = con.execute("""
            SELECT token, status, service
            FROM tokens
            WHERE service=? AND counter_id=?
            ORDER BY id
        """, (service, counter_id)).fetchall()

    return jsonify([dict(row) for row in rows])


# -------------------------------
# ADMIN ANALYTICS
# -------------------------------

@app.route("/api/stats")
@admin_required
def get_stats():

    service = request.args.get("service")
    counter_id = request.args.get("counter_id", type=int)

    if not valid_service(service) or not counter_id:
        return jsonify({"error": "Select a valid counter"}), 400

    with connect_db() as con:

        if not get_counter(con, counter_id, service):
            return jsonify({"error": "Counter not found"}), 404

        total = con.execute("""
            SELECT COUNT(*) FROM tokens
            WHERE service=? AND counter_id=?
        """, (service, counter_id)).fetchone()[0]

        waiting = con.execute("""
            SELECT COUNT(*) FROM tokens
            WHERE service=? AND counter_id=?
            AND status='waiting'
        """, (service, counter_id)).fetchone()[0]

        completed = con.execute("""
            SELECT COUNT(*) FROM tokens
            WHERE service=? AND counter_id=?
            AND status='completed'
        """, (service, counter_id)).fetchone()[0]

        serving = con.execute("""
            SELECT token FROM tokens
            WHERE service=? AND counter_id=?
            AND status='serving'
            LIMIT 1
        """, (service, counter_id)).fetchone()

        avg = average_service_time(
            con, service, counter_id
        )

    return jsonify({
        "total": total,
        "waiting": waiting,
        "completed": completed,
        "serving": serving["token"] if serving else "None",
        "average_service_minutes": round(avg, 1)
    })


# -------------------------------
# SEVEN-DAY ANALYTICS
# -------------------------------

@app.route("/api/analytics")
@admin_required
def get_analytics():

    service = request.args.get("service")
    counter_id = request.args.get("counter_id", type=int)

    if not valid_service(service) or not counter_id:
        return jsonify({"error": "Select a valid counter"}), 400

    today = now().date()

    labels = [
        (today - timedelta(days=i)).isoformat()
        for i in range(6, -1, -1)
    ]

    with connect_db() as con:

        if not get_counter(con, counter_id, service):
            return jsonify({"error": "Counter not found"}), 404

        rows = con.execute("""
            SELECT substr(completed_at, 1, 10) AS day,
            COUNT(*) AS count
            FROM tokens
            WHERE service=?
            AND counter_id=?
            AND status='completed'
            AND completed_at IS NOT NULL
            AND substr(completed_at, 1, 10) >= ?
            GROUP BY day
        """, (service, counter_id, labels[0])).fetchall()

    counts = {row["day"]: row["count"] for row in rows}

    return jsonify({
        "labels": labels,
        "completed": [counts.get(day, 0) for day in labels]
    })


# -------------------------------
# CALL NEXT TOKEN
# -------------------------------

@app.route("/api/next", methods=["POST"])
@admin_required
def next_token():

    data = request.get_json(silent=True) or {}

    service = data.get("service")
    counter_id = data.get("counter_id")

    if not valid_service(service) or not isinstance(counter_id, int):
        return jsonify({"error": "Invalid counter selection"}), 400

    with connect_db() as con:

        if not get_counter(con, counter_id, service):
            return jsonify({"error": "Counter not found"}), 404

        # Complete previous customer
        con.execute("""
            UPDATE tokens
            SET status='completed', completed_at=?
            WHERE counter_id=? AND status='serving'
        """, (now().isoformat(), counter_id))

        # Start next waiting customer
        next_user = con.execute("""
            SELECT id, token FROM tokens
            WHERE counter_id=? AND status='waiting'
            ORDER BY id LIMIT 1
        """, (counter_id,)).fetchone()

        if not next_user:
            return jsonify({"message": "Queue is empty"})

        con.execute("""
            UPDATE tokens
            SET status='serving', started_at=?
            WHERE id=?
        """, (now().isoformat(), next_user["id"]))

    return jsonify({
        "message": "Next token called",
        "token": next_user["token"]
    })


# -------------------------------
# SKIP TOKEN
# -------------------------------

@app.route("/api/skip/<token>", methods=["POST"])
@admin_required
def skip_token(token):

    with connect_db() as con:

        result = con.execute("""
            UPDATE tokens
            SET status='skipped'
            WHERE token=? AND status='waiting'
        """, (token,))

        if result.rowcount == 0:
            return jsonify({"error": "Cannot skip token"}), 400

    return jsonify({"message": "Token skipped successfully"})


if __name__ == "__main__":
    initialize_db()
    app.run(debug=True)
