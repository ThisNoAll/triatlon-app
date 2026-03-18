import os
import sqlite3
import random
from functools import wraps
from datetime import datetime, timedelta
from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    flash,
    session,
    g,
)

# --------------------------------------------------
# Optional Google login via Authlib
# --------------------------------------------------
AUTHLIB_AVAILABLE = True
try:
    from authlib.integrations.flask_client import OAuth
except Exception:
    AUTHLIB_AVAILABLE = False
    OAuth = None

# --------------------------------------------------
# App config
# --------------------------------------------------
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DB_PATH = os.path.join(BASE_DIR, "triatlon.sqlite3")

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "dev-secret-change-me")

app.config["DATABASE"] = DB_PATH
app.config["EVENT_TITLE_DEFAULT"] = "Triatlon"
app.config["ADMIN_PASSWORD"] = os.getenv("ADMIN_PASSWORD", "admin123")
app.config["GOOGLE_CLIENT_ID"] = os.getenv("GOOGLE_CLIENT_ID", "")
app.config["GOOGLE_CLIENT_SECRET"] = os.getenv("GOOGLE_CLIENT_SECRET", "")
app.config["GOOGLE_DISCOVERY_URL"] = "https://accounts.google.com/.well-known/openid-configuration"

MAX_TEAMS = 5
MIN_TEAMS_TO_START = 3
BASE_TEAM_SIZE = 2
MAX_TEAM_SIZE = 4
MAX_PLAYERS = MAX_TEAMS * MAX_TEAM_SIZE

# --------------------------------------------------
# OAuth
# --------------------------------------------------
oauth = None
if AUTHLIB_AVAILABLE and app.config["GOOGLE_CLIENT_ID"] and app.config["GOOGLE_CLIENT_SECRET"]:
    oauth = OAuth(app)
    oauth.register(
        name="google",
        server_metadata_url=app.config["GOOGLE_DISCOVERY_URL"],
        client_id=app.config["GOOGLE_CLIENT_ID"],
        client_secret=app.config["GOOGLE_CLIENT_SECRET"],
        client_kwargs={"scope": "openid email profile"},
    )

# --------------------------------------------------
# DB helpers
# --------------------------------------------------
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(app.config["DATABASE"])
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(error=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def query_all(sql, params=()):
    return get_db().execute(sql, params).fetchall()


def query_one(sql, params=()):
    return get_db().execute(sql, params).fetchone()


def execute(sql, params=()):
    db = get_db()
    cur = db.execute(sql, params)
    db.commit()
    return cur


def init_db():
    db = get_db()

    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            event_at TEXT NOT NULL,
            registration_deadline TEXT NOT NULL,
            created_at TEXT NOT NULL,
            is_closed INTEGER NOT NULL DEFAULT 0,
            finalized_at TEXT
        );

        CREATE TABLE IF NOT EXISTS registrations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id INTEGER NOT NULL,
            participant_name TEXT NOT NULL,
            participant_email TEXT NOT NULL,
            provider TEXT NOT NULL,
            google_sub TEXT,
            created_at TEXT NOT NULL,

            assigned_team INTEGER,
            assigned_stage INTEGER,  -- 2 / 3 / 4
            assigned_slot INTEGER,   -- 1..4 within team

            pending_stage INTEGER,   -- 3 / 4 if waiting in pool
            pending_position INTEGER,

            FOREIGN KEY(event_id) REFERENCES events(id)
        );

        CREATE TABLE IF NOT EXISTS team_name_proposals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id INTEGER NOT NULL,
            team_number INTEGER NOT NULL,
            proposed_name TEXT NOT NULL,
            proposed_by_registration_id INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending', -- pending / approved / rejected
            created_at TEXT NOT NULL,
            finalized_at TEXT,

            FOREIGN KEY(event_id) REFERENCES events(id),
            FOREIGN KEY(proposed_by_registration_id) REFERENCES registrations(id)
        );

        CREATE TABLE IF NOT EXISTS team_name_votes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            proposal_id INTEGER NOT NULL,
            registration_id INTEGER NOT NULL,
            vote TEXT NOT NULL, -- approve / reject
            created_at TEXT NOT NULL,

            UNIQUE(proposal_id, registration_id),
            FOREIGN KEY(proposal_id) REFERENCES team_name_proposals(id),
            FOREIGN KEY(registration_id) REFERENCES registrations(id)
        );
        """
    )
    db.commit()


@app.before_request
def before_request():
    init_db()
    auto_finalize_due_events()


# --------------------------------------------------
# Utility helpers
# --------------------------------------------------
def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def parse_dt(value):
    return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")


def parse_dt_input(date_str, time_str):
    return datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")


def format_dt_display(value):
    return parse_dt(value).strftime("%Y-%m-%d %H:%M")


def compute_deadline(event_at):
    return event_at - timedelta(hours=6)


def normalize_team_name(name):
    return " ".join((name or "").strip().split())


def team_name_exists(event_id, team_number, candidate_name):
    candidate_norm = normalize_team_name(candidate_name).lower()
    if not candidate_norm:
        return False

    approved = query_all(
        """
        SELECT team_number, proposed_name
        FROM team_name_proposals
        WHERE event_id = ? AND status = 'approved'
        """,
        (event_id,),
    )
    for row in approved:
        if row["team_number"] != team_number and normalize_team_name(row["proposed_name"]).lower() == candidate_norm:
            return True
    return False


def required_name_approvals(team_size):
    if team_size <= 2:
        return 2
    if team_size == 3:
        return 2
    return 3


def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("admin_logged_in"):
            flash("Előbb jelentkezz be szervezőként.", "error")
            return redirect(url_for("admin_login"))
        return fn(*args, **kwargs)
    return wrapper


def get_latest_event():
    return query_one("SELECT * FROM events ORDER BY id DESC LIMIT 1")


def get_event(event_id):
    return query_one("SELECT * FROM events WHERE id = ?", (event_id,))


def event_has_started_or_closed(event_row):
    if not event_row:
        return True
    deadline = parse_dt(event_row["registration_deadline"])
    return event_row["is_closed"] == 1 or datetime.now() >= deadline


def get_registration_count(event_id):
    row = query_one(
        "SELECT COUNT(*) AS cnt FROM registrations WHERE event_id = ?",
        (event_id,),
    )
    return row["cnt"] if row else 0


def get_assigned_registrations(event_id):
    return query_all(
        """
        SELECT *
        FROM registrations
        WHERE event_id = ?
          AND assigned_team IS NOT NULL
        ORDER BY assigned_team, assigned_slot, id
        """,
        (event_id,),
    )


def get_pending_pool(event_id, stage):
    return query_all(
        """
        SELECT *
        FROM registrations
        WHERE event_id = ?
          AND pending_stage = ?
          AND assigned_team IS NULL
        ORDER BY pending_position, id
        """,
        (event_id, stage),
    )


def get_registration_by_id(registration_id):
    return query_one("SELECT * FROM registrations WHERE id = ?", (registration_id,))


def get_registration_by_google_sub(event_id, google_sub):
    return query_one(
        """
        SELECT *
        FROM registrations
        WHERE event_id = ? AND google_sub = ?
        ORDER BY id DESC LIMIT 1
        """,
        (event_id, google_sub),
    )


def get_registration_by_email(event_id, email):
    return query_one(
        """
        SELECT *
        FROM registrations
        WHERE event_id = ? AND lower(participant_email) = lower(?)
        ORDER BY id DESC LIMIT 1
        """,
        (event_id, email),
    )


def get_session_registration_id(event_id):
    key = f"my_registration_{event_id}"
    return session.get(key)


def set_session_registration_id(event_id, registration_id):
    key = f"my_registration_{event_id}"
    session[key] = registration_id


def get_team_members(event_id, team_number):
    return query_all(
        """
        SELECT *
        FROM registrations
        WHERE event_id = ?
          AND assigned_team = ?
        ORDER BY assigned_slot ASC
        """,
        (event_id, team_number),
    )


def get_current_team_size(event_id, team_number):
    row = query_one(
        """
        SELECT COUNT(*) AS cnt
        FROM registrations
        WHERE event_id = ?
          AND assigned_team = ?
        """,
        (event_id, team_number),
    )
    return row["cnt"] if row else 0


def get_team_target_size(event_id, team_number):
    current_size = get_current_team_size(event_id, team_number)
    if current_size >= 4:
        return 4
    if current_size == 3:
        return 4
    if current_size == 2:
        # before stage 3 assignment completes, team can still become 3 later
        # but publicly we show current occupancy vs next target
        pending_stage3 = query_one(
            """
            SELECT COUNT(*) AS cnt
            FROM registrations
            WHERE event_id = ?
              AND pending_stage = 3
            """,
            (event_id,),
        )["cnt"]
        if pending_stage3 > 0:
            return 3
        return 2
    return 2


def build_public_teams(event_id):
    teams = []
    for team_number in range(1, MAX_TEAMS + 1):
        members = get_team_members(event_id, team_number)
        count = len(members)

        if count == 0:
            display_capacity = 2
        elif count == 1:
            display_capacity = 2
        elif count == 2:
            display_capacity = 2
        elif count == 3:
            display_capacity = 3
        else:
            display_capacity = 4

        approved_name = get_approved_team_name(event_id, team_number)

        visible_names = []
        if count == 2:
            visible_names = [m["participant_name"] for m in members]
        elif count == 3:
            visible_names = [m["participant_name"] for m in members]
        elif count == 4:
            visible_names = [m["participant_name"] for m in members]

        teams.append(
            {
                "team_number": team_number,
                "count": count,
                "capacity": display_capacity,
                "visible_names": visible_names,
                "team_name": approved_name,
            }
        )
    return teams


def get_approved_team_name(event_id, team_number):
    row = query_one(
        """
        SELECT *
        FROM team_name_proposals
        WHERE event_id = ?
          AND team_number = ?
          AND status = 'approved'
        ORDER BY id DESC
        LIMIT 1
        """,
        (event_id, team_number),
    )
    return row["proposed_name"] if row else None


def get_pending_team_name_proposal(event_id, team_number):
    return query_one(
        """
        SELECT *
        FROM team_name_proposals
        WHERE event_id = ?
          AND team_number = ?
          AND status = 'pending'
        ORDER BY id DESC
        LIMIT 1
        """,
        (event_id, team_number),
    )


def get_votes_for_proposal(proposal_id):
    return query_all(
        """
        SELECT *
        FROM team_name_votes
        WHERE proposal_id = ?
        ORDER BY id ASC
        """,
        (proposal_id,),
    )


def get_my_status(event_id):
    registration_id = get_session_registration_id(event_id)
    if not registration_id:
        return None

    reg = get_registration_by_id(registration_id)
    if not reg or reg["event_id"] != event_id:
        return None

    if reg["assigned_team"] is not None:
        team_members = get_team_members(event_id, reg["assigned_team"])
        team_size = len(team_members)
        approved_name = get_approved_team_name(event_id, reg["assigned_team"])
        pending_proposal = get_pending_team_name_proposal(event_id, reg["assigned_team"])

        return {
            "registration": reg,
            "status_label": "Csapatba kerültél",
            "team_number": reg["assigned_team"],
            "team_size": team_size,
            "team_name": approved_name,
            "pending_proposal": pending_proposal,
            "can_propose_team_name": team_size >= 2 and not event_has_started_or_closed(get_event(event_id)),
        }

    if reg["pending_stage"] == 3:
        return {
            "registration": reg,
            "status_label": "A 3. fős bővítési körben vagy",
            "team_number": None,
            "team_size": None,
            "team_name": None,
            "pending_proposal": None,
            "can_propose_team_name": False,
        }

    if reg["pending_stage"] == 4:
        return {
            "registration": reg,
            "status_label": "A 4. fős bővítési körben vagy",
            "team_number": None,
            "team_size": None,
            "team_name": None,
            "pending_proposal": None,
            "can_propose_team_name": False,
        }

    return {
        "registration": reg,
        "status_label": "Jelentkezésed rögzítve, társra vársz",
        "team_number": None,
        "team_size": None,
        "team_name": None,
        "pending_proposal": None,
        "can_propose_team_name": False,
    }


# --------------------------------------------------
# Assignment logic
# --------------------------------------------------
def assign_initial_if_needed(event_id, registration_id):
    total = get_registration_count(event_id)
    if total <= 10:
        index = total - 1
        team_number = (index // 2) + 1
        assigned_slot = (index % 2) + 1
        execute(
            """
            UPDATE registrations
            SET assigned_team = ?, assigned_stage = 2, assigned_slot = ?,
                pending_stage = NULL, pending_position = NULL
            WHERE id = ?
            """,
            (team_number, assigned_slot, registration_id),
        )


def queue_for_stage(event_id, registration_id, stage):
    current_pending = query_one(
        """
        SELECT COUNT(*) AS cnt
        FROM registrations
        WHERE event_id = ?
          AND pending_stage = ?
          AND assigned_team IS NULL
        """,
        (event_id, stage),
    )["cnt"]

    execute(
        """
        UPDATE registrations
        SET pending_stage = ?, pending_position = ?
        WHERE id = ?
        """,
        (stage, current_pending + 1, registration_id),
    )


def assign_pending_stage_full(event_id, stage):
    pending = get_pending_pool(event_id, stage)
    if len(pending) != 5:
        return

    team_numbers = [1, 2, 3, 4, 5]
    random.shuffle(team_numbers)

    for reg, team_number in zip(pending, team_numbers):
        slot = stage  # stage 3 => slot 3, stage 4 => slot 4
        execute(
            """
            UPDATE registrations
            SET assigned_team = ?, assigned_stage = ?, assigned_slot = ?,
                pending_stage = NULL, pending_position = NULL
            WHERE id = ?
            """,
            (team_number, stage, slot, reg["id"]),
        )


def finalize_pending_stage_partial(event_id, stage):
    pending = get_pending_pool(event_id, stage)
    if not pending:
        return

    assigned_stage_rows = query_all(
        """
        SELECT DISTINCT assigned_team
        FROM registrations
        WHERE event_id = ?
          AND assigned_stage = ?
          AND assigned_team IS NOT NULL
        """,
        (event_id, stage),
    )
    already_used = {row["assigned_team"] for row in assigned_stage_rows if row["assigned_team"] is not None}
    available_teams = [t for t in range(1, 6) if t not in already_used]

    if len(available_teams) < len(pending):
        # defensive fallback; shouldn't happen
        available_teams = [1, 2, 3, 4, 5]

    random.shuffle(available_teams)
    chosen_teams = available_teams[: len(pending)]

    for reg, team_number in zip(pending, chosen_teams):
        slot = stage
        execute(
            """
            UPDATE registrations
            SET assigned_team = ?, assigned_stage = ?, assigned_slot = ?,
                pending_stage = NULL, pending_position = NULL
            WHERE id = ?
            """,
            (team_number, stage, slot, reg["id"]),
        )


def register_participant(event_id, name, email, provider, google_sub=None):
    event_row = get_event(event_id)
    if not event_row:
        raise ValueError("Az esemény nem található.")

    if event_has_started_or_closed(event_row):
        raise ValueError("A jelentkezés lezárult.")

    current_count = get_registration_count(event_id)
    if current_count >= MAX_PLAYERS:
        raise ValueError("A jelentkezés lezárult, a maximum 20 fő betelt.")

    cur = execute(
        """
        INSERT INTO registrations (
            event_id, participant_name, participant_email, provider, google_sub, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (event_id, name.strip(), email.strip(), provider, google_sub, now_str()),
    )
    registration_id = cur.lastrowid

    total_after_insert = get_registration_count(event_id)

    if total_after_insert <= 10:
        assign_initial_if_needed(event_id, registration_id)
    elif 11 <= total_after_insert <= 15:
        queue_for_stage(event_id, registration_id, 3)
        if total_after_insert == 15:
            assign_pending_stage_full(event_id, 3)
    elif 16 <= total_after_insert <= 20:
        queue_for_stage(event_id, registration_id, 4)
        if total_after_insert == 20:
            assign_pending_stage_full(event_id, 4)

    return registration_id


def finalize_event_if_needed(event_row):
    if not event_row:
        return

    if event_row["is_closed"] == 1:
        return

    deadline = parse_dt(event_row["registration_deadline"])
    if datetime.now() < deadline:
        return

    event_id = event_row["id"]

    finalize_pending_stage_partial(event_id, 3)
    finalize_pending_stage_partial(event_id, 4)

    execute(
        """
        UPDATE events
        SET is_closed = 1,
            finalized_at = ?
        WHERE id = ?
        """,
        (now_str(), event_id),
    )


def auto_finalize_due_events():
    rows = query_all("SELECT * FROM events WHERE is_closed = 0")
    for row in rows:
        finalize_event_if_needed(row)


# --------------------------------------------------
# Routes - Public
# --------------------------------------------------
@app.route("/")
def home():
    event_row = get_latest_event()
    if event_row:
        finalize_event_if_needed(event_row)
        event_row = get_event(event_row["id"])

    if not event_row:
        return render_template("home.html", event=None)

    public_teams = build_public_teams(event_row["id"])
    my_status = get_my_status(event_row["id"])

    total_registrations = get_registration_count(event_row["id"])
    can_register = not event_has_started_or_closed(event_row) and total_registrations < MAX_PLAYERS

    stage3_pending_count = query_one(
        """
        SELECT COUNT(*) AS cnt
        FROM registrations
        WHERE event_id = ? AND pending_stage = 3 AND assigned_team IS NULL
        """,
        (event_row["id"],),
    )["cnt"]

    stage4_pending_count = query_one(
        """
        SELECT COUNT(*) AS cnt
        FROM registrations
        WHERE event_id = ? AND pending_stage = 4 AND assigned_team IS NULL
        """,
        (event_row["id"],),
    )["cnt"]

    approved_start = total_registrations >= 6

    return render_template(
        "home.html",
        event=event_row,
        public_teams=public_teams,
        my_status=my_status,
        total_registrations=total_registrations,
        can_register=can_register,
        approved_start=approved_start,
        min_players=MIN_TEAMS_TO_START * BASE_TEAM_SIZE,
        max_players=MAX_PLAYERS,
        stage3_pending_count=stage3_pending_count,
        stage4_pending_count=stage4_pending_count,
        google_enabled=oauth is not None,
        format_dt_display=format_dt_display,
    )


@app.route("/register", methods=["POST"])
def register_email():
    event_row = get_latest_event()
    if not event_row:
        flash("Nincs aktív esemény.", "error")
        return redirect(url_for("home"))

    name = request.form.get("name", "").strip()
    email = request.form.get("email", "").strip()

    if not name or not email:
        flash("A név és az email megadása kötelező.", "error")
        return redirect(url_for("home"))

    if "@" not in email:
        flash("Érvénytelen email cím.", "error")
        return redirect(url_for("home"))

    existing = get_registration_by_email(event_row["id"], email)
    if existing:
        set_session_registration_id(event_row["id"], existing["id"])
        flash("Ezzel az email címmel már jelentkeztél erre az eseményre.", "info")
        return redirect(url_for("home"))

    try:
        registration_id = register_participant(
            event_row["id"], name, email, provider="email"
        )
        set_session_registration_id(event_row["id"], registration_id)
        flash("Sikeres jelentkezés.", "success")
    except ValueError as e:
        flash(str(e), "error")

    return redirect(url_for("home"))


# --------------------------------------------------
# Google Auth
# --------------------------------------------------
@app.route("/auth/google")
def google_login():
    if oauth is None:
        flash("A Google belépés még nincs bekapcsolva.", "error")
        return redirect(url_for("home"))

    redirect_uri = url_for("google_callback", _external=True)
    return oauth.google.authorize_redirect(redirect_uri)


@app.route("/auth/google/callback")
def google_callback():
    if oauth is None:
        flash("A Google belépés még nincs bekapcsolva.", "error")
        return redirect(url_for("home"))

    event_row = get_latest_event()
    if not event_row:
        flash("Nincs aktív esemény.", "error")
        return redirect(url_for("home"))

    token = oauth.google.authorize_access_token()
    user_info = token.get("userinfo")
    if not user_info:
        user_info = oauth.google.userinfo()

    google_sub = user_info.get("sub")
    email = user_info.get("email")
    name = user_info.get("name") or email

    if not google_sub or not email:
        flash("A Google-fiókból nem sikerült a szükséges adatokat kiolvasni.", "error")
        return redirect(url_for("home"))

    existing = get_registration_by_google_sub(event_row["id"], google_sub)
    if existing:
        set_session_registration_id(event_row["id"], existing["id"])
        flash("Már jelentkeztél ezzel a Google-fiókkal.", "info")
        return redirect(url_for("home"))

    existing_email = get_registration_by_email(event_row["id"], email)
    if existing_email:
        set_session_registration_id(event_row["id"], existing_email["id"])
        flash("Ezzel az email címmel már jelentkeztél erre az eseményre.", "info")
        return redirect(url_for("home"))

    try:
        registration_id = register_participant(
            event_row["id"], name, email, provider="google", google_sub=google_sub
        )
        set_session_registration_id(event_row["id"], registration_id)
        flash("Sikeres Google-jelentkezés.", "success")
    except ValueError as e:
        flash(str(e), "error")

    return redirect(url_for("home"))


# --------------------------------------------------
# Team name routes
# --------------------------------------------------
@app.route("/team-name/propose", methods=["POST"])
def propose_team_name():
    event_row = get_latest_event()
    if not event_row:
        flash("Nincs aktív esemény.", "error")
        return redirect(url_for("home"))

    finalize_event_if_needed(event_row)
    event_row = get_event(event_row["id"])

    if event_has_started_or_closed(event_row):
        flash("A csapatnév már nem módosítható.", "error")
        return redirect(url_for("home"))

    registration_id = get_session_registration_id(event_row["id"])
    if not registration_id:
        flash("Ehhez előbb jelentkezned kell.", "error")
        return redirect(url_for("home"))

    reg = get_registration_by_id(registration_id)
    if not reg or reg["assigned_team"] is None:
        flash("Csapatnév csak kész csapathoz adható meg.", "error")
        return redirect(url_for("home"))

    team_number = reg["assigned_team"]
    team_members = get_team_members(event_row["id"], team_number)
    if len(team_members) < 2:
        flash("Csapatnév csak minimum 2 fős csapatnál adható meg.", "error")
        return redirect(url_for("home"))

    proposed_name = normalize_team_name(request.form.get("team_name", ""))
    if not proposed_name:
        flash("Adj meg egy csapatnevet.", "error")
        return redirect(url_for("home"))

    if team_name_exists(event_row["id"], team_number, proposed_name):
        flash("Ezen az eseményen már létezik ilyen csapatnév.", "error")
        return redirect(url_for("home"))

    # Clear previous pending proposal for this team
    old_pending = get_pending_team_name_proposal(event_row["id"], team_number)
    if old_pending:
        execute(
            "UPDATE team_name_proposals SET status = 'rejected', finalized_at = ? WHERE id = ?",
            (now_str(), old_pending["id"]),
        )

    cur = execute(
        """
        INSERT INTO team_name_proposals (
            event_id, team_number, proposed_name, proposed_by_registration_id, status, created_at
        )
        VALUES (?, ?, ?, ?, 'pending', ?)
        """,
        (event_row["id"], team_number, proposed_name, registration_id, now_str()),
    )
    proposal_id = cur.lastrowid

    # proposer auto-approves
    execute(
        """
        INSERT INTO team_name_votes (proposal_id, registration_id, vote, created_at)
        VALUES (?, ?, 'approve', ?)
        """,
        (proposal_id, registration_id, now_str()),
    )

    evaluate_team_name_proposal(proposal_id)
    flash("A csapatnév-javaslat rögzítve lett.", "success")
    return redirect(url_for("home"))


@app.route("/team-name/vote", methods=["POST"])
def vote_team_name():
    event_row = get_latest_event()
    if not event_row:
        flash("Nincs aktív esemény.", "error")
        return redirect(url_for("home"))

    registration_id = get_session_registration_id(event_row["id"])
    if not registration_id:
        flash("Ehhez előbb jelentkezned kell.", "error")
        return redirect(url_for("home"))

    proposal_id = request.form.get("proposal_id", type=int)
    vote = request.form.get("vote", "").strip().lower()
    if vote not in ("approve", "reject"):
        flash("Érvénytelen szavazat.", "error")
        return redirect(url_for("home"))

    proposal = query_one("SELECT * FROM team_name_proposals WHERE id = ?", (proposal_id,))
    if not proposal or proposal["status"] != "pending":
        flash("A javaslat már nem aktív.", "error")
        return redirect(url_for("home"))

    reg = get_registration_by_id(registration_id)
    if not reg or reg["event_id"] != proposal["event_id"] or reg["assigned_team"] != proposal["team_number"]:
        flash("Csak a saját csapatod névjavaslatáról szavazhatsz.", "error")
        return redirect(url_for("home"))

    execute(
        """
        INSERT INTO team_name_votes (proposal_id, registration_id, vote, created_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(proposal_id, registration_id)
        DO UPDATE SET vote = excluded.vote, created_at = excluded.created_at
        """,
        (proposal_id, registration_id, vote, now_str()),
    )

    evaluate_team_name_proposal(proposal_id)
    flash("A szavazatodat rögzítettük.", "success")
    return redirect(url_for("home"))


def evaluate_team_name_proposal(proposal_id):
    proposal = query_one("SELECT * FROM team_name_proposals WHERE id = ?", (proposal_id,))
    if not proposal or proposal["status"] != "pending":
        return

    members = get_team_members(proposal["event_id"], proposal["team_number"])
    team_size = len(members)
    required = required_name_approvals(team_size)

    votes = get_votes_for_proposal(proposal_id)
    approvals = sum(1 for v in votes if v["vote"] == "approve")
    rejections = sum(1 for v in votes if v["vote"] == "reject")

    if rejections > 0:
        execute(
            "UPDATE team_name_proposals SET status = 'rejected', finalized_at = ? WHERE id = ?",
            (now_str(), proposal_id),
        )
        return

    if approvals >= required:
        # uniqueness check one more time
        if team_name_exists(proposal["event_id"], proposal["team_number"], proposal["proposed_name"]):
            execute(
                "UPDATE team_name_proposals SET status = 'rejected', finalized_at = ? WHERE id = ?",
                (now_str(), proposal_id),
            )
            return

        # revoke previously approved names for same team
        execute(
            """
            UPDATE team_name_proposals
            SET status = 'rejected', finalized_at = ?
            WHERE event_id = ?
              AND team_number = ?
              AND status = 'approved'
            """,
            (now_str(), proposal["event_id"], proposal["team_number"]),
        )

        execute(
            "UPDATE team_name_proposals SET status = 'approved', finalized_at = ? WHERE id = ?",
            (now_str(), proposal_id),
        )


# --------------------------------------------------
# Admin
# --------------------------------------------------
@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        password = request.form.get("password", "")
        if password == app.config["ADMIN_PASSWORD"]:
            session["admin_logged_in"] = True
            flash("Sikeres szervezői belépés.", "success")
            return redirect(url_for("admin_dashboard"))
        flash("Hibás jelszó.", "error")

    return render_template("admin_login.html")


@app.route("/admin/logout")
def admin_logout():
    session.pop("admin_logged_in", None)
    flash("Kiléptél a szervezői felületről.", "info")
    return redirect(url_for("home"))


@app.route("/admin")
@admin_required
def admin_dashboard():
    events = query_all("SELECT * FROM events ORDER BY id DESC")
    latest_event = get_latest_event()

    event_stats = None
    if latest_event:
        finalize_event_if_needed(latest_event)
        latest_event = get_event(latest_event["id"])
        total = get_registration_count(latest_event["id"])
        teams = build_public_teams(latest_event["id"])
        can_start = total >= (MIN_TEAMS_TO_START * BASE_TEAM_SIZE)

        event_stats = {
            "total_registrations": total,
            "can_start": can_start,
            "teams": teams,
            "stage3_pending_count": query_one(
                "SELECT COUNT(*) AS cnt FROM registrations WHERE event_id = ? AND pending_stage = 3 AND assigned_team IS NULL",
                (latest_event["id"],),
            )["cnt"],
            "stage4_pending_count": query_one(
                "SELECT COUNT(*) AS cnt FROM registrations WHERE event_id = ? AND pending_stage = 4 AND assigned_team IS NULL",
                (latest_event["id"],),
            )["cnt"],
        }

    return render_template(
        "admin_dashboard.html",
        events=events,
        latest_event=latest_event,
        event_stats=event_stats,
        format_dt_display=format_dt_display,
    )


@app.route("/admin/events/new", methods=["GET", "POST"])
@admin_required
def admin_new_event():
    if request.method == "POST":
        title = request.form.get("title", "").strip() or app.config["EVENT_TITLE_DEFAULT"]
        event_date = request.form.get("event_date", "").strip()
        event_time = request.form.get("event_time", "").strip()

        if not event_date or not event_time:
            flash("Az esemény dátuma és időpontja kötelező.", "error")
            return redirect(url_for("admin_new_event"))

        try:
            event_at = parse_dt_input(event_date, event_time)
        except ValueError:
            flash("Érvénytelen dátum vagy idő.", "error")
            return redirect(url_for("admin_new_event"))

        deadline = compute_deadline(event_at)

        execute(
            """
            INSERT INTO events (title, event_at, registration_deadline, created_at, is_closed)
            VALUES (?, ?, ?, ?, 0)
            """,
            (
                title,
                event_at.strftime("%Y-%m-%d %H:%M:%S"),
                deadline.strftime("%Y-%m-%d %H:%M:%S"),
                now_str(),
            ),
        )
        flash("Az esemény létrejött.", "success")
        return redirect(url_for("admin_dashboard"))

    return render_template("admin_event_form.html")


@app.route("/admin/finalize/<int:event_id>", methods=["POST"])
@admin_required
def admin_finalize_event(event_id):
    event_row = get_event(event_id)
    if not event_row:
        flash("Az esemény nem található.", "error")
        return redirect(url_for("admin_dashboard"))

    finalize_pending_stage_partial(event_id, 3)
    finalize_pending_stage_partial(event_id, 4)

    execute(
        """
        UPDATE events
        SET is_closed = 1, finalized_at = ?
        WHERE id = ?
        """,
        (now_str(), event_id),
    )
    flash("Az esemény jelentkezése lezárva és véglegesítve.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/score-sheet/<int:event_id>")
@admin_required
def admin_score_sheet(event_id):
    event_row = get_event(event_id)
    if not event_row:
        flash("Az esemény nem található.", "error")
        return redirect(url_for("admin_dashboard"))

    finalize_event_if_needed(event_row)
    event_row = get_event(event_id)

    teams = []
    for team_number in range(1, MAX_TEAMS + 1):
        members = get_team_members(event_id, team_number)
        if members:
            teams.append(
                {
                    "team_number": team_number,
                    "team_name": get_approved_team_name(event_id, team_number) or f"Csapat {team_number}",
                    "members": members,
                }
            )

    active_teams = [team for team in teams if len(team["members"]) >= 2]

    matches = []
    for i in range(len(active_teams)):
        for j in range(i + 1, len(active_teams)):
            matches.append(
                {
                    "team_a": active_teams[i],
                    "team_b": active_teams[j],
                }
            )

    return render_template(
        "score_sheet.html",
        event=event_row,
        teams=teams,
        matches=matches,
        format_dt_display=format_dt_display,
    )


# --------------------------------------------------
# Startup
# --------------------------------------------------
if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)