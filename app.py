import os
import re
import sqlite3
import random
import unicodedata
from functools import wraps
from datetime import datetime, timedelta
from dotenv import load_dotenv
from flask import (
    Flask,
    abort,
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
load_dotenv()

BASE_DIR = os.path.abspath(os.path.dirname(__file__))


def resolve_database_path():
    configured_path = os.getenv("DATABASE_PATH", "").strip()
    if configured_path:
        return configured_path

    database_url = os.getenv("DATABASE_URL", "").strip()
    if database_url.startswith("sqlite:///"):
        raw_path = database_url.replace("sqlite:///", "", 1)
        if os.path.isabs(raw_path):
            return raw_path
        return os.path.join(BASE_DIR, raw_path)

    return os.path.join(BASE_DIR, "triatlon.sqlite3")


DB_PATH = resolve_database_path()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "dev-secret-change-me")

app.config["DATABASE"] = DB_PATH
app.config["EVENT_TITLE_DEFAULT"] = "Kupa"
app.config["ADMIN_PASSWORD"] = os.getenv("ADMIN_PASSWORD", "admin123")
app.config["GOOGLE_CLIENT_ID"] = os.getenv("GOOGLE_CLIENT_ID", "")
app.config["GOOGLE_CLIENT_SECRET"] = os.getenv("GOOGLE_CLIENT_SECRET", "")
app.config["GOOGLE_DISCOVERY_URL"] = "https://accounts.google.com/.well-known/openid-configuration"

MAX_TEAMS = 5
MIN_TEAMS_TO_START = 3
BASE_TEAM_SIZE = 2
MAX_TEAM_SIZE = 4
MAX_PLAYERS = MAX_TEAMS * MAX_TEAM_SIZE

PAYMENT_PENDING = "pending"
PAYMENT_PAID = "paid"
PAYMENT_INVALID = "invalid"

PAYMENT_METHOD_TRANSFER = "transfer"
PAYMENT_METHOD_CASH = "cash"
PAYMENT_METHOD_NONE = "none"

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
            slug TEXT UNIQUE,
            description TEXT,
            event_at TEXT NOT NULL,
            registration_deadline TEXT NOT NULL,
            created_at TEXT NOT NULL,
            is_closed INTEGER NOT NULL DEFAULT 0,
            finalized_at TEXT,

            has_fee INTEGER NOT NULL DEFAULT 0,
            fee_amount INTEGER DEFAULT 0,
            beneficiary_name TEXT,
            bank_account TEXT
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
            assigned_stage INTEGER,
            assigned_slot INTEGER,

            pending_stage INTEGER,
            pending_position INTEGER,

            payment_status TEXT NOT NULL DEFAULT 'paid',
            payment_method TEXT NOT NULL DEFAULT 'none',
            payment_note TEXT,
            is_manual INTEGER NOT NULL DEFAULT 0,
            removed_from_team INTEGER NOT NULL DEFAULT 0,
            is_deleted INTEGER NOT NULL DEFAULT 0,

            FOREIGN KEY(event_id) REFERENCES events(id)
        );

        CREATE TABLE IF NOT EXISTS team_name_proposals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id INTEGER NOT NULL,
            team_number INTEGER NOT NULL,
            proposed_name TEXT NOT NULL,
            proposed_by_registration_id INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            finalized_at TEXT,
            is_admin_override INTEGER NOT NULL DEFAULT 0,

            FOREIGN KEY(event_id) REFERENCES events(id),
            FOREIGN KEY(proposed_by_registration_id) REFERENCES registrations(id)
        );

        CREATE TABLE IF NOT EXISTS team_name_votes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            proposal_id INTEGER NOT NULL,
            registration_id INTEGER NOT NULL,
            vote TEXT NOT NULL,
            created_at TEXT NOT NULL,

            UNIQUE(proposal_id, registration_id),
            FOREIGN KEY(proposal_id) REFERENCES team_name_proposals(id),
            FOREIGN KEY(registration_id) REFERENCES registrations(id)
        );
        """
    )
    ensure_event_schema(db)
    db.commit()


def ensure_event_schema(db):
    columns = {row["name"] for row in db.execute("PRAGMA table_info(events)").fetchall()}
    if "slug" not in columns:
        db.execute("ALTER TABLE events ADD COLUMN slug TEXT")

    rows = db.execute("SELECT id, title, event_at, slug FROM events ORDER BY id ASC").fetchall()
    used_slugs = set()
    for row in rows:
        current_slug = (row["slug"] or "").strip()
        if current_slug and current_slug not in used_slugs:
            used_slugs.add(current_slug)
            continue

        slug = generate_unique_slug(
            row["title"],
            row["event_at"][:10] if row["event_at"] else "",
            used_slugs,
        )
        db.execute("UPDATE events SET slug = ? WHERE id = ?", (slug, row["id"]))
        used_slugs.add(slug)


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


def compute_payment_deadline(event_at):
    return event_at - timedelta(hours=24)


def normalize_team_name(name):
    return " ".join((name or "").strip().split())


def compact_bank_account(value):
    return re.sub(r"\s+", "", (value or "").strip())


def build_payment_reference(event_id, participant_name=None):
    if participant_name:
        return f"KUPA + {participant_name} + {get_event_identifier(event_id)}"
    return f"KUPA + saj?t n?v + {get_event_identifier(event_id)}"


def build_payment_details(event_row, participant_name=None):
    if not event_row or event_row["has_fee"] != 1:
        return None

    beneficiary = (event_row["beneficiary_name"] or "").strip()
    bank_account = compact_bank_account(event_row["bank_account"])
    if not beneficiary or not bank_account:
        return None

    return {
        "beneficiary": beneficiary,
        "bank_account": bank_account,
        "amount": int(event_row["fee_amount"] or 0),
        "reference": build_payment_reference(event_row["id"], participant_name),
        "deadline": compute_payment_deadline(parse_dt(event_row["event_at"])).strftime("%Y-%m-%d %H:%M"),
    }


def slugify(value):
    normalized = unicodedata.normalize("NFKD", value or "")
    ascii_value = normalized.encode("ascii", "ignore").decode("ascii").lower()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_value).strip("-")
    return slug or "esemeny"


def generate_unique_slug(title, suffix_hint="", used_slugs=None):
    used = used_slugs if used_slugs is not None else {
        row["slug"] for row in query_all("SELECT slug FROM events WHERE slug IS NOT NULL AND slug != ''")
    }
    base_parts = [slugify(title)]
    hint = slugify(suffix_hint)
    if hint:
        base_parts.append(hint)
    base_slug = "-".join(part for part in base_parts if part)
    slug = base_slug
    counter = 2
    while slug in used:
        slug = f"{base_slug}-{counter}"
        counter += 1
    return slug


def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("admin_logged_in"):
            flash("Előbb jelentkezz be szervezőként.", "error")
            return redirect(url_for("admin_login"))
        return fn(*args, **kwargs)
    return wrapper


def get_event(event_id):
    return query_one("SELECT * FROM events WHERE id = ?", (event_id,))


def get_event_by_slug(slug):
    return query_one("SELECT * FROM events WHERE slug = ?", (slug,))


def get_all_events():
    return query_all("SELECT * FROM events ORDER BY event_at DESC, id DESC")


def delete_event_and_related_data(event_id):
    proposal_ids = query_all(
        "SELECT id FROM team_name_proposals WHERE event_id = ?",
        (event_id,),
    )
    for proposal in proposal_ids:
        execute("DELETE FROM team_name_votes WHERE proposal_id = ?", (proposal["id"],))

    execute("DELETE FROM team_name_proposals WHERE event_id = ?", (event_id,))
    execute("DELETE FROM registrations WHERE event_id = ?", (event_id,))
    execute("DELETE FROM events WHERE id = ?", (event_id,))


def event_has_started_or_closed(event_row):
    if not event_row:
        return True
    deadline = parse_dt(event_row["registration_deadline"])
    return event_row["is_closed"] == 1 or datetime.now() >= deadline


def get_active_registration_count(event_id):
    row = query_one(
        """
        SELECT COUNT(*) AS cnt
        FROM registrations
        WHERE event_id = ?
          AND is_deleted = 0
          AND payment_status != ?
        """,
        (event_id, PAYMENT_INVALID),
    )
    return row["cnt"] if row else 0


def get_registration_count(event_id):
    row = query_one(
        """
        SELECT COUNT(*) AS cnt
        FROM registrations
        WHERE event_id = ?
          AND is_deleted = 0
        """,
        (event_id,),
    )
    return row["cnt"] if row else 0


def get_event_identifier(event_id):
    return f"{event_id:04d}"


def payment_label(status):
    mapping = {
        PAYMENT_PENDING: "Fizetésre vár",
        PAYMENT_PAID: "Befizetve",
        PAYMENT_INVALID: "Érvénytelen",
    }
    return mapping.get(status, status)


def payment_method_label(method):
    mapping = {
        PAYMENT_METHOD_TRANSFER: "Utalás",
        PAYMENT_METHOD_CASH: "Készpénz",
        PAYMENT_METHOD_NONE: "Nincs",
    }
    return mapping.get(method, method)


def get_registration_by_id(registration_id):
    return query_one("SELECT * FROM registrations WHERE id = ?", (registration_id,))


def get_session_registration_id(event_id):
    key = f"my_registration_{event_id}"
    return session.get(key)


def set_session_registration_id(event_id, registration_id):
    key = f"my_registration_{event_id}"
    session[key] = registration_id


def get_registration_by_google_sub(event_id, google_sub):
    return query_one(
        """
        SELECT *
        FROM registrations
        WHERE event_id = ? AND google_sub = ? AND is_deleted = 0
        ORDER BY id DESC LIMIT 1
        """,
        (event_id, google_sub),
    )


def get_registration_by_email(event_id, email):
    return query_one(
        """
        SELECT *
        FROM registrations
        WHERE event_id = ?
          AND lower(participant_email) = lower(?)
          AND is_deleted = 0
        ORDER BY id DESC LIMIT 1
        """,
        (event_id, email),
    )


def get_team_members(event_id, team_number):
    return query_all(
        """
        SELECT *
        FROM registrations
        WHERE event_id = ?
          AND assigned_team = ?
          AND is_deleted = 0
          AND payment_status != ?
        ORDER BY assigned_slot ASC, id ASC
        """,
        (event_id, team_number, PAYMENT_INVALID),
    )


def get_unassigned_members(event_id):
    return query_all(
        """
        SELECT *
        FROM registrations
        WHERE event_id = ?
          AND assigned_team IS NULL
          AND is_deleted = 0
          AND payment_status != ?
        ORDER BY id ASC
        """,
        (event_id, PAYMENT_INVALID),
    )


def get_current_team_size(event_id, team_number):
    row = query_one(
        """
        SELECT COUNT(*) AS cnt
        FROM registrations
        WHERE event_id = ?
          AND assigned_team = ?
          AND is_deleted = 0
          AND payment_status != ?
        """,
        (event_id, team_number, PAYMENT_INVALID),
    )
    return row["cnt"] if row else 0


def get_team_target_size(event_id, team_number):
    current_size = get_current_team_size(event_id, team_number)
    if current_size >= 4:
        return 4
    if current_size == 3:
        return 4

    pending_stage3 = query_one(
        """
        SELECT COUNT(*) AS cnt
        FROM registrations
        WHERE event_id = ?
          AND pending_stage = 3
          AND assigned_team IS NULL
          AND is_deleted = 0
          AND payment_status != ?
        """,
        (event_id, PAYMENT_INVALID),
    )["cnt"]

    if current_size == 2 and pending_stage3 > 0:
        return 3
    return 2


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


def get_visible_team_name_proposal(event_id, team_number):
    pending = get_pending_team_name_proposal(event_id, team_number)
    if pending:
        return pending

    return query_one(
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


def build_team_name_state(event_id, team_number):
    proposal = get_visible_team_name_proposal(event_id, team_number)
    if not proposal:
        return None

    team_size = len(get_team_members(event_id, team_number))
    required = required_name_approvals(team_size)
    votes = get_votes_for_proposal(proposal["id"])
    approvals = sum(1 for vote in votes if vote["vote"] == "approve")
    rejections = sum(1 for vote in votes if vote["vote"] == "reject")

    return {
        "proposal": proposal,
        "name": proposal["proposed_name"],
        "is_final": proposal["status"] == "approved",
        "approvals": approvals,
        "rejections": rejections,
        "required": required,
        "team_size": team_size,
        "status": proposal["status"],
    }


def get_my_status(event_id):
    registration_id = get_session_registration_id(event_id)
    if not registration_id:
        return None

    reg = get_registration_by_id(registration_id)
    if not reg or reg["event_id"] != event_id or reg["is_deleted"] == 1:
        return None

    event_row = get_event(event_id)

    status = {
        "registration": reg,
        "payment_status_label": payment_label(reg["payment_status"]),
        "payment_method_label": payment_method_label(reg["payment_method"]),
        "event_identifier": get_event_identifier(event_id),
        "event": event_row,
    }

    if reg["payment_status"] == PAYMENT_INVALID:
        status["status_label"] = "A jelentkezésed érvénytelenné vált"
        status["team_number"] = None
        status["team_name"] = None
        status["team_name_state"] = None
        status["team_size"] = None
        status["pending_proposal"] = None
        status["can_propose_team_name"] = False
        return status

    if reg["assigned_team"] is not None:
        team_members = get_team_members(event_id, reg["assigned_team"])
        team_size = len(team_members)
        team_name_state = build_team_name_state(event_id, reg["assigned_team"])
        approved_name = get_approved_team_name(event_id, reg["assigned_team"])
        pending_proposal = get_pending_team_name_proposal(event_id, reg["assigned_team"])

        status["status_label"] = "Csapatba kerültél"
        status["team_number"] = reg["assigned_team"]
        status["team_size"] = team_size
        status["team_name"] = team_name_state["name"] if team_name_state else approved_name
        status["team_name_state"] = team_name_state
        status["pending_proposal"] = pending_proposal
        status["can_propose_team_name"] = (
            team_size >= 2
            and not event_has_started_or_closed(event_row)
        )
        return status

    if reg["pending_stage"] == 3:
        status["status_label"] = "A 3. fős bővítési körben vagy"
    elif reg["pending_stage"] == 4:
        status["status_label"] = "A 4. fős bővítési körben vagy"
    else:
        status["status_label"] = "Jelentkezésed rögzítve, társra vársz"

    status["team_number"] = None
    status["team_size"] = None
    status["team_name"] = None
    status["team_name_state"] = None
    status["pending_proposal"] = None
    status["can_propose_team_name"] = False
    return status


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

        team_name_state = build_team_name_state(event_id, team_number)

        visible_names = []
        # a specifikáció szerint:
        # 1/2 -> név rejtett
        # 2/2 -> látszik
        # 2/3 -> 2 látszik, az új nem
        # 3/3 -> látszik
        # 3/4 -> 3 látszik, az új nem
        # 4/4 -> látszik
        if count >= 2:
            visible_names = [m["participant_name"] for m in members]

        teams.append(
            {
                "team_number": team_number,
                "count": count,
                "capacity": display_capacity,
                "visible_names": visible_names,
                "team_name": team_name_state["name"] if team_name_state else None,
                "team_name_state": team_name_state,
                "members": members,
            }
        )
    return teams


# --------------------------------------------------
# Assignment logic
# --------------------------------------------------
def assign_initial_if_needed(event_id, registration_id):
    total = get_active_registration_count(event_id)
    if total <= 10:
        index = total - 1
        team_number = (index // 2) + 1
        assigned_slot = (index % 2) + 1
        execute(
            """
            UPDATE registrations
            SET assigned_team = ?, assigned_stage = 2, assigned_slot = ?,
                pending_stage = NULL, pending_position = NULL, removed_from_team = 0
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
          AND is_deleted = 0
          AND payment_status != ?
        """,
        (event_id, stage, PAYMENT_INVALID),
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
        slot = stage
        execute(
            """
            UPDATE registrations
            SET assigned_team = ?, assigned_stage = ?, assigned_slot = ?,
                pending_stage = NULL, pending_position = NULL, removed_from_team = 0
            WHERE id = ?
            """,
            (team_number, stage, slot, reg["id"]),
        )


def get_pending_pool(event_id, stage):
    return query_all(
        """
        SELECT *
        FROM registrations
        WHERE event_id = ?
          AND pending_stage = ?
          AND assigned_team IS NULL
          AND is_deleted = 0
          AND payment_status != ?
        ORDER BY pending_position, id
        """,
        (event_id, stage, PAYMENT_INVALID),
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
          AND is_deleted = 0
          AND payment_status != ?
        """,
        (event_id, stage, PAYMENT_INVALID),
    )
    already_used = {row["assigned_team"] for row in assigned_stage_rows if row["assigned_team"] is not None}
    available_teams = [t for t in range(1, 6) if t not in already_used]

    if len(available_teams) < len(pending):
        available_teams = [1, 2, 3, 4, 5]

    random.shuffle(available_teams)
    chosen_teams = available_teams[: len(pending)]

    for reg, team_number in zip(pending, chosen_teams):
        slot = stage
        execute(
            """
            UPDATE registrations
            SET assigned_team = ?, assigned_stage = ?, assigned_slot = ?,
                pending_stage = NULL, pending_position = NULL, removed_from_team = 0
            WHERE id = ?
            """,
            (team_number, stage, slot, reg["id"]),
        )


def register_participant(event_id, name, email, provider, google_sub=None, payment_method=None):
    event_row = get_event(event_id)
    if not event_row:
        raise ValueError("Az esemény nem található.")

    if event_has_started_or_closed(event_row):
        raise ValueError("A jelentkezés lezárult.")

    current_count = get_active_registration_count(event_id)
    if current_count >= MAX_PLAYERS:
        raise ValueError("A jelentkezés lezárult, a maximum 20 fő betelt.")

    if event_row["has_fee"] == 1:
        payment_status = PAYMENT_PENDING
        if payment_method not in (PAYMENT_METHOD_TRANSFER, PAYMENT_METHOD_CASH):
            raise ValueError("Fizetős eseménynél válassz fizetési módot: utalás vagy készpénz.")
    else:
        payment_status = PAYMENT_PAID
        payment_method = PAYMENT_METHOD_NONE

    cur = execute(
        """
        INSERT INTO registrations (
            event_id, participant_name, participant_email, provider, google_sub, created_at,
            payment_status, payment_method, payment_note
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event_id,
            name.strip(),
            email.strip(),
            provider,
            google_sub,
            now_str(),
            payment_status,
            payment_method,
            "",
        ),
    )
    registration_id = cur.lastrowid

    total_after_insert = get_active_registration_count(event_id)

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


def remove_invalid_from_teams(event_id):
    invalid_regs = query_all(
        """
        SELECT id
        FROM registrations
        WHERE event_id = ?
          AND payment_status = ?
          AND is_deleted = 0
        """,
        (event_id, PAYMENT_INVALID),
    )
    for reg in invalid_regs:
        execute(
            """
            UPDATE registrations
            SET assigned_team = NULL,
                assigned_stage = NULL,
                assigned_slot = NULL,
                pending_stage = NULL,
                pending_position = NULL,
                removed_from_team = 1
            WHERE id = ?
            """,
            (reg["id"],),
        )


def finalize_event_if_needed(event_row):
    if not event_row:
        return

    if event_row["is_closed"] == 1:
        return

    deadline = parse_dt(event_row["registration_deadline"])
    if datetime.now() < deadline:
        return

    event_id = event_row["id"]

    if event_row["has_fee"] == 1:
        execute(
            """
            UPDATE registrations
            SET payment_status = ?
            WHERE event_id = ?
              AND payment_status = ?
              AND is_deleted = 0
            """,
            (PAYMENT_INVALID, event_id, PAYMENT_PENDING),
        )
        remove_invalid_from_teams(event_id)

    finalize_pending_stage_partial(event_id, 3)
    finalize_pending_stage_partial(event_id, 4)
    finalize_team_names_for_event(event_id)

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


def build_event_stats(event_id):
    total = get_registration_count(event_id)
    active = get_active_registration_count(event_id)
    teams = build_public_teams(event_id)
    return {
        "total_registrations": total,
        "active_registrations": active,
        "can_start": active >= (MIN_TEAMS_TO_START * BASE_TEAM_SIZE),
        "teams": teams,
        "stage3_pending_count": query_one(
            """
            SELECT COUNT(*) AS cnt
            FROM registrations
            WHERE event_id = ? AND pending_stage = 3 AND assigned_team IS NULL
              AND is_deleted = 0 AND payment_status != ?
            """,
            (event_id, PAYMENT_INVALID),
        )["cnt"],
        "stage4_pending_count": query_one(
            """
            SELECT COUNT(*) AS cnt
            FROM registrations
            WHERE event_id = ? AND pending_stage = 4 AND assigned_team IS NULL
              AND is_deleted = 0 AND payment_status != ?
            """,
            (event_id, PAYMENT_INVALID),
        )["cnt"],
    }


# --------------------------------------------------
# Team name logic
# --------------------------------------------------
def evaluate_team_name_proposal(proposal_id):
    proposal = query_one("SELECT * FROM team_name_proposals WHERE id = ?", (proposal_id,))
    if not proposal or proposal["status"] != "pending":
        return

    if proposal["is_admin_override"] == 1:
        if team_name_exists(proposal["event_id"], proposal["team_number"], proposal["proposed_name"]):
            execute(
                "UPDATE team_name_proposals SET status = 'rejected', finalized_at = ? WHERE id = ?",
                (now_str(), proposal_id),
            )
            return

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
        return

    members = get_team_members(proposal["event_id"], proposal["team_number"])
    team_size = len(members)
    required = required_name_approvals(team_size)

    votes = get_votes_for_proposal(proposal_id)
    approvals = sum(1 for v in votes if v["vote"] == "approve")

    if approvals >= required:
        if team_name_exists(proposal["event_id"], proposal["team_number"], proposal["proposed_name"]):
            execute(
                "UPDATE team_name_proposals SET status = 'rejected', finalized_at = ? WHERE id = ?",
                (now_str(), proposal_id),
            )
            return

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


def finalize_team_names_for_event(event_id):
    for team_number in range(1, MAX_TEAMS + 1):
        pending = get_pending_team_name_proposal(event_id, team_number)
        if not pending:
            continue

        execute(
            """
            UPDATE team_name_proposals
            SET status = 'rejected', finalized_at = ?
            WHERE event_id = ?
              AND team_number = ?
              AND status = 'approved'
            """,
            (now_str(), event_id, team_number),
        )
        execute(
            "UPDATE team_name_proposals SET status = 'approved', finalized_at = ? WHERE id = ?",
            (now_str(), pending["id"]),
        )


# --------------------------------------------------
# Routes - Public
# --------------------------------------------------
@app.route("/")
def home():
    for event_row in query_all("SELECT * FROM events WHERE is_closed = 0"):
        finalize_event_if_needed(event_row)

    return render_template(
        "events_index.html",
        events=get_all_events(),
        min_players=MIN_TEAMS_TO_START * BASE_TEAM_SIZE,
        max_players=MAX_PLAYERS,
        format_dt_display=format_dt_display,
    )


@app.route("/e/<slug>")
def event_home(slug):
    event_row = get_event_by_slug(slug)
    if not event_row:
        abort(404)

    finalize_event_if_needed(event_row)
    event_row = get_event(event_row["id"])

    public_teams = build_public_teams(event_row["id"])
    my_status = get_my_status(event_row["id"])

    total_registrations = get_registration_count(event_row["id"])
    active_count = get_active_registration_count(event_row["id"])
    can_register = not event_has_started_or_closed(event_row) and active_count < MAX_PLAYERS

    stage3_pending_count = query_one(
        """
        SELECT COUNT(*) AS cnt
        FROM registrations
        WHERE event_id = ?
          AND pending_stage = 3
          AND assigned_team IS NULL
          AND is_deleted = 0
          AND payment_status != ?
        """,
        (event_row["id"], PAYMENT_INVALID),
    )["cnt"]

    stage4_pending_count = query_one(
        """
        SELECT COUNT(*) AS cnt
        FROM registrations
        WHERE event_id = ?
          AND pending_stage = 4
          AND assigned_team IS NULL
          AND is_deleted = 0
          AND payment_status != ?
        """,
        (event_row["id"], PAYMENT_INVALID),
    )["cnt"]

    approved_start = active_count >= 6
    payment_deadline_display = compute_payment_deadline(parse_dt(event_row["event_at"])).strftime("%Y-%m-%d %H:%M")
    payment_details = build_payment_details(event_row)

    my_payment_details = None
    if my_status and my_status["registration"]["payment_method"] == PAYMENT_METHOD_TRANSFER:
        my_payment_details = build_payment_details(
            event_row, my_status["registration"]["participant_name"]
        )

    return render_template(
        "home.html",
        event=event_row,
        public_teams=public_teams,
        my_status=my_status,
        total_registrations=total_registrations,
        active_count=active_count,
        can_register=can_register,
        approved_start=approved_start,
        min_players=MIN_TEAMS_TO_START * BASE_TEAM_SIZE,
        max_players=MAX_PLAYERS,
        stage3_pending_count=stage3_pending_count,
        stage4_pending_count=stage4_pending_count,
        google_enabled=oauth is not None,
        format_dt_display=format_dt_display,
        payment_label=payment_label,
        payment_method_label=payment_method_label,
        event_identifier=get_event_identifier(event_row["id"]),
        payment_deadline_display=payment_deadline_display,
        payment_details=payment_details,
        my_payment_details=my_payment_details,
    )


@app.route("/e/<slug>/register", methods=["POST"])
def register_email(slug):
    event_row = get_event_by_slug(slug)
    if not event_row:
        flash("Az esemény nem található.", "error")
        return redirect(url_for("home"))

    name = request.form.get("name", "").strip()
    email = request.form.get("email", "").strip()
    payment_method = request.form.get("payment_method", "").strip()

    if not name or not email:
        flash("A név és az email megadása kötelező.", "error")
        return redirect(url_for("event_home", slug=slug))

    if "@" not in email:
        flash("Érvénytelen email cím.", "error")
        return redirect(url_for("event_home", slug=slug))

    existing = get_registration_by_email(event_row["id"], email)
    if existing:
        set_session_registration_id(event_row["id"], existing["id"])
        flash("Ezzel az email címmel már jelentkeztél erre az eseményre.", "info")
        return redirect(url_for("event_home", slug=slug))

    try:
        registration_id = register_participant(
            event_row["id"], name, email, provider="email", payment_method=payment_method
        )
        set_session_registration_id(event_row["id"], registration_id)
        flash("Sikeres jelentkezés.", "success")
    except ValueError as e:
        flash(str(e), "error")

    return redirect(url_for("event_home", slug=slug))


# --------------------------------------------------
# Google Auth
# --------------------------------------------------
@app.route("/e/<slug>/auth/google")
def google_login(slug):
    if oauth is None:
        flash("A Google belépés még nincs bekapcsolva.", "error")
        return redirect(url_for("event_home", slug=slug))

    event_row = get_event_by_slug(slug)
    if not event_row:
        flash("Az esemény nem található.", "error")
        return redirect(url_for("home"))

    if event_row["has_fee"] == 1:
        payment_method = request.args.get("payment_method", "").strip()
        if payment_method not in (PAYMENT_METHOD_TRANSFER, PAYMENT_METHOD_CASH):
            flash("Google jelentkezéshez is válassz fizetési módot.", "error")
            return redirect(url_for("event_home", slug=slug))
        session[f"payment_method_{event_row['id']}"] = payment_method

    redirect_uri = url_for("google_callback", slug=slug, _external=True)
    return oauth.google.authorize_redirect(redirect_uri)


@app.route("/e/<slug>/auth/google/callback")
def google_callback(slug):
    if oauth is None:
        flash("A Google belépés még nincs bekapcsolva.", "error")
        return redirect(url_for("event_home", slug=slug))

    event_row = get_event_by_slug(slug)
    if not event_row:
        flash("Az esemény nem található.", "error")
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
        return redirect(url_for("event_home", slug=slug))

    existing = get_registration_by_google_sub(event_row["id"], google_sub)
    if existing:
        set_session_registration_id(event_row["id"], existing["id"])
        flash("Már jelentkeztél ezzel a Google-fiókkal.", "info")
        return redirect(url_for("event_home", slug=slug))

    existing_email = get_registration_by_email(event_row["id"], email)
    if existing_email:
        set_session_registration_id(event_row["id"], existing_email["id"])
        flash("Ezzel az email címmel már jelentkeztél erre az eseményre.", "info")
        return redirect(url_for("event_home", slug=slug))

    payment_method = session.pop(f"payment_method_{event_row['id']}", None)

    try:
        registration_id = register_participant(
            event_row["id"], name, email, provider="google", google_sub=google_sub, payment_method=payment_method
        )
        set_session_registration_id(event_row["id"], registration_id)
        flash("Sikeres Google-jelentkezés.", "success")
    except ValueError as e:
        flash(str(e), "error")

    return redirect(url_for("event_home", slug=slug))


# --------------------------------------------------
# Team name routes
# --------------------------------------------------
@app.route("/e/<slug>/team-name/propose", methods=["POST"])
def propose_team_name(slug):
    event_row = get_event_by_slug(slug)
    if not event_row:
        flash("Az esemény nem található.", "error")
        return redirect(url_for("home"))

    finalize_event_if_needed(event_row)
    event_row = get_event(event_row["id"])

    if event_has_started_or_closed(event_row):
        flash("A csapatnév már nem módosítható.", "error")
        return redirect(url_for("event_home", slug=slug))

    registration_id = get_session_registration_id(event_row["id"])
    if not registration_id:
        flash("Ehhez előbb jelentkezned kell.", "error")
        return redirect(url_for("event_home", slug=slug))

    reg = get_registration_by_id(registration_id)
    if not reg or reg["assigned_team"] is None:
        flash("Csapatnév csak kész csapathoz adható meg.", "error")
        return redirect(url_for("event_home", slug=slug))

    team_number = reg["assigned_team"]
    team_members = get_team_members(event_row["id"], team_number)
    if len(team_members) < 2:
        flash("Csapatnév csak minimum 2 fős csapatnál adható meg.", "error")
        return redirect(url_for("event_home", slug=slug))

    proposed_name = normalize_team_name(request.form.get("team_name", ""))
    if not proposed_name:
        flash("Adj meg egy csapatnevet.", "error")
        return redirect(url_for("event_home", slug=slug))

    if team_name_exists(event_row["id"], team_number, proposed_name):
        flash("Ezen az eseményen már létezik ilyen csapatnév.", "error")
        return redirect(url_for("event_home", slug=slug))

    old_pending = get_pending_team_name_proposal(event_row["id"], team_number)
    if old_pending:
        execute(
            "UPDATE team_name_proposals SET status = 'rejected', finalized_at = ? WHERE id = ?",
            (now_str(), old_pending["id"]),
        )

    cur = execute(
        """
        INSERT INTO team_name_proposals (
            event_id, team_number, proposed_name, proposed_by_registration_id, status, created_at, is_admin_override
        )
        VALUES (?, ?, ?, ?, 'pending', ?, 0)
        """,
        (event_row["id"], team_number, proposed_name, registration_id, now_str()),
    )
    proposal_id = cur.lastrowid

    execute(
        """
        INSERT INTO team_name_votes (proposal_id, registration_id, vote, created_at)
        VALUES (?, ?, 'approve', ?)
        """,
        (proposal_id, registration_id, now_str()),
    )

    evaluate_team_name_proposal(proposal_id)
    flash("A csapatnév-javaslat rögzítve lett.", "success")
    return redirect(url_for("event_home", slug=slug))


@app.route("/e/<slug>/team-name/vote", methods=["POST"])
def vote_team_name(slug):
    event_row = get_event_by_slug(slug)
    if not event_row:
        flash("Az esemény nem található.", "error")
        return redirect(url_for("home"))

    registration_id = get_session_registration_id(event_row["id"])
    if not registration_id:
        flash("Ehhez előbb jelentkezned kell.", "error")
        return redirect(url_for("event_home", slug=slug))

    proposal_id = request.form.get("proposal_id", type=int)
    vote = request.form.get("vote", "").strip().lower()
    if vote not in ("approve", "reject"):
        flash("Érvénytelen szavazat.", "error")
        return redirect(url_for("event_home", slug=slug))

    proposal = query_one("SELECT * FROM team_name_proposals WHERE id = ?", (proposal_id,))
    if not proposal or proposal["status"] != "pending":
        flash("A javaslat már nem aktív.", "error")
        return redirect(url_for("event_home", slug=slug))

    reg = get_registration_by_id(registration_id)
    if not reg or reg["event_id"] != proposal["event_id"] or reg["assigned_team"] != proposal["team_number"]:
        flash("Csak a saját csapatod névjavaslatáról szavazhatsz.", "error")
        return redirect(url_for("event_home", slug=slug))

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
    return redirect(url_for("event_home", slug=slug))


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
    event_cards = []
    for event in get_all_events():
        finalize_event_if_needed(event)
        current_event = get_event(event["id"])
        event_cards.append(
            {
                "event": current_event,
                "stats": build_event_stats(current_event["id"]),
            }
        )

    return render_template(
        "admin_dashboard.html",
        event_cards=event_cards,
        format_dt_display=format_dt_display,
        payment_label=payment_label,
    )


@app.route("/admin/events/<int:event_id>")
@admin_required
def admin_event_dashboard(event_id):
    event_row = get_event(event_id)
    if not event_row:
        flash("Az esemény nem található.", "error")
        return redirect(url_for("admin_dashboard"))

    finalize_event_if_needed(event_row)
    event_row = get_event(event_id)
    event_stats = build_event_stats(event_id)

    return render_template(
        "admin_event_dashboard.html",
        event=event_row,
        event_stats=event_stats,
        format_dt_display=format_dt_display,
        payment_label=payment_label,
    )

@app.route("/admin/events/new", methods=["GET", "POST"])
@admin_required
def admin_new_event():
    if request.method == "POST":
        title = request.form.get("title", "").strip() or app.config["EVENT_TITLE_DEFAULT"]
        description = request.form.get("description", "").strip()
        event_date = request.form.get("event_date", "").strip()
        event_time = request.form.get("event_time", "").strip()
        has_fee = 1 if request.form.get("has_fee") == "on" else 0
        fee_amount = request.form.get("fee_amount", type=int) or 0
        beneficiary_name = request.form.get("beneficiary_name", "").strip()
        bank_account = request.form.get("bank_account", "").strip()

        if not event_date or not event_time:
            flash("Az esemény dátuma és időpontja kötelező.", "error")
            return redirect(url_for("admin_new_event"))

        try:
            event_at = parse_dt_input(event_date, event_time)
        except ValueError:
            flash("Érvénytelen dátum vagy idő.", "error")
            return redirect(url_for("admin_new_event"))

        deadline = compute_deadline(event_at)
        slug = generate_unique_slug(title, event_at.strftime("%Y-%m-%d"))

        cur = execute(
            """
            INSERT INTO events (
                title, slug, description, event_at, registration_deadline, created_at, is_closed,
                has_fee, fee_amount, beneficiary_name, bank_account
            )
            VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)
            """,
            (
                title,
                slug,
                description,
                event_at.strftime("%Y-%m-%d %H:%M:%S"),
                deadline.strftime("%Y-%m-%d %H:%M:%S"),
                now_str(),
                has_fee,
                fee_amount,
                beneficiary_name if has_fee else "",
                bank_account if has_fee else "",
            ),
        )
        flash("Az esemény létrejött.", "success")
        return redirect(url_for("admin_event_dashboard", event_id=cur.lastrowid))
    return render_template("admin_event_form.html", mode="create", event=None)


@app.route("/admin/events/<int:event_id>/edit", methods=["GET", "POST"])
@admin_required
def admin_edit_event(event_id):
    event_row = get_event(event_id)
    if not event_row:
        flash("Az esemény nem található.", "error")
        return redirect(url_for("admin_dashboard"))

    if request.method == "POST":
        title = request.form.get("title", "").strip() or app.config["EVENT_TITLE_DEFAULT"]
        description = request.form.get("description", "").strip()
        event_date = request.form.get("event_date", "").strip()
        event_time = request.form.get("event_time", "").strip()
        has_fee = 1 if request.form.get("has_fee") == "on" else 0
        fee_amount = request.form.get("fee_amount", type=int) or 0
        beneficiary_name = request.form.get("beneficiary_name", "").strip()
        bank_account = request.form.get("bank_account", "").strip()

        try:
            event_at = parse_dt_input(event_date, event_time)
        except ValueError:
            flash("Érvénytelen dátum vagy idő.", "error")
            return redirect(url_for("admin_edit_event", event_id=event_id))

        deadline = compute_deadline(event_at)
        slug = event_row["slug"] or generate_unique_slug(title, event_at.strftime("%Y-%m-%d"))

        execute(
            """
            UPDATE events
            SET title = ?, slug = ?, description = ?, event_at = ?, registration_deadline = ?,
                has_fee = ?, fee_amount = ?, beneficiary_name = ?, bank_account = ?
            WHERE id = ?
            """,
            (
                title,
                slug,
                description,
                event_at.strftime("%Y-%m-%d %H:%M:%S"),
                deadline.strftime("%Y-%m-%d %H:%M:%S"),
                has_fee,
                fee_amount,
                beneficiary_name if has_fee else "",
                bank_account if has_fee else "",
                event_id,
            ),
        )
        flash("Az esemény frissítve.", "success")
        return redirect(url_for("admin_event_dashboard", event_id=event_id))
    return render_template("admin_event_form.html", mode="edit", event=event_row)


@app.route("/admin/events/<int:event_id>/delete", methods=["GET", "POST"])
@admin_required
def admin_delete_event(event_id):
    event_row = get_event(event_id)
    if not event_row:
        flash("Az esemény nem található.", "error")
        return redirect(url_for("admin_dashboard"))

    if request.method == "POST":
        delete_event_and_related_data(event_id)
        flash("Az esemény és a hozzá tartozó adatok törölve lettek.", "success")
        return redirect(url_for("admin_dashboard"))

    return render_template(
        "admin_event_delete_confirm.html",
        event=event_row,
        event_stats=build_event_stats(event_id),
        format_dt_display=format_dt_display,
    )


@app.route("/admin/finalize/<int:event_id>", methods=["POST"])
@admin_required
def admin_finalize_event(event_id):
    event_row = get_event(event_id)
    if not event_row:
        flash("Az esemény nem található.", "error")
        return redirect(url_for("admin_dashboard"))

    finalize_pending_stage_partial(event_id, 3)
    finalize_pending_stage_partial(event_id, 4)

    if event_row["has_fee"] == 1:
        execute(
            """
            UPDATE registrations
            SET payment_status = ?
            WHERE event_id = ?
              AND payment_status = ?
              AND is_deleted = 0
            """,
            (PAYMENT_INVALID, event_id, PAYMENT_PENDING),
        )
        remove_invalid_from_teams(event_id)

    finalize_team_names_for_event(event_id)

    execute(
        """
        UPDATE events
        SET is_closed = 1, finalized_at = ?
        WHERE id = ?
        """,
        (now_str(), event_id),
    )
    flash("Az esemény jelentkezése lezárva és véglegesítve.", "success")
    return redirect(url_for("admin_event_dashboard", event_id=event_id))


@app.route("/admin/teams/<int:event_id>")
@admin_required
def admin_teams(event_id):
    event_row = get_event(event_id)
    if not event_row:
        flash("Az esemény nem található.", "error")
        return redirect(url_for("admin_dashboard"))

    teams = []
    for team_number in range(1, MAX_TEAMS + 1):
        teams.append(
            {
                "team_number": team_number,
                "team_name": get_approved_team_name(event_id, team_number) or "",
                "members": get_team_members(event_id, team_number),
            }
        )

    unassigned = get_unassigned_members(event_id)
    return render_template(
        "admin_teams.html",
        event=event_row,
        teams=teams,
        unassigned=unassigned,
        format_dt_display=format_dt_display,
        payment_label=payment_label,
        payment_method_label=payment_method_label,
    )


@app.route("/admin/player/<int:registration_id>/remove-from-team", methods=["POST"])
@admin_required
def admin_remove_from_team(registration_id):
    reg = get_registration_by_id(registration_id)
    if not reg:
        flash("A játékos nem található.", "error")
        return redirect(url_for("admin_dashboard"))

    execute(
        """
        UPDATE registrations
        SET assigned_team = NULL,
            assigned_stage = NULL,
            assigned_slot = NULL,
            pending_stage = NULL,
            pending_position = NULL,
            removed_from_team = 1,
            is_manual = 1
        WHERE id = ?
        """,
        (registration_id,),
    )
    flash("A játékos kikerült a csapatból.", "success")
    return redirect(url_for("admin_teams", event_id=reg["event_id"]))


@app.route("/admin/player/<int:registration_id>/delete", methods=["POST"])
@admin_required
def admin_delete_player(registration_id):
    reg = get_registration_by_id(registration_id)
    if not reg:
        flash("A játékos nem található.", "error")
        return redirect(url_for("admin_dashboard"))

    execute(
        """
        UPDATE registrations
        SET is_deleted = 1,
            assigned_team = NULL,
            assigned_stage = NULL,
            assigned_slot = NULL,
            pending_stage = NULL,
            pending_position = NULL
        WHERE id = ?
        """,
        (registration_id,),
    )
    flash("A jelentkező végleg törölve.", "success")
    return redirect(url_for("admin_teams", event_id=reg["event_id"]))


@app.route("/admin/player/<int:registration_id>/move", methods=["POST"])
@admin_required
def admin_move_player(registration_id):
    reg = get_registration_by_id(registration_id)
    if not reg:
        flash("A játékos nem található.", "error")
        return redirect(url_for("admin_dashboard"))

    event_id = reg["event_id"]
    team_number = request.form.get("team_number", type=int)
    if team_number is None or team_number < 1 or team_number > 5:
        flash("Érvénytelen csapat.", "error")
        return redirect(url_for("admin_teams", event_id=event_id))

    if reg["payment_status"] == PAYMENT_INVALID:
        flash("Érvénytelen státuszú játékos nem tehető aktív csapatba.", "error")
        return redirect(url_for("admin_teams", event_id=event_id))

    current_size = get_current_team_size(event_id, team_number)
    if current_size >= 4 and reg["assigned_team"] != team_number:
        flash("A célcsapat már elérte a maximum 4 főt.", "error")
        return redirect(url_for("admin_teams", event_id=event_id))

    slot = current_size + 1
    if reg["assigned_team"] == team_number:
        flash("A játékos már ebben a csapatban van.", "info")
        return redirect(url_for("admin_teams", event_id=event_id))

    execute(
        """
        UPDATE registrations
        SET assigned_team = ?, assigned_stage = ?, assigned_slot = ?,
            pending_stage = NULL, pending_position = NULL,
            removed_from_team = 0, is_manual = 1
        WHERE id = ?
        """,
        (team_number, max(2, slot), slot, registration_id),
    )
    flash("A játékos áthelyezve.", "success")
    return redirect(url_for("admin_teams", event_id=event_id))


@app.route("/admin/player/<int:registration_id>/payment", methods=["POST"])
@admin_required
def admin_update_payment(registration_id):
    reg = get_registration_by_id(registration_id)
    if not reg:
        flash("A játékos nem található.", "error")
        return redirect(url_for("admin_dashboard"))

    payment_status = request.form.get("payment_status", PAYMENT_PENDING)
    payment_method = request.form.get("payment_method", PAYMENT_METHOD_TRANSFER)
    payment_note = request.form.get("payment_note", "").strip()

    if payment_status not in (PAYMENT_PENDING, PAYMENT_PAID, PAYMENT_INVALID):
        flash("Érvénytelen fizetési státusz.", "error")
        return redirect(url_for("admin_teams", event_id=reg["event_id"]))

    if payment_method not in (PAYMENT_METHOD_TRANSFER, PAYMENT_METHOD_CASH, PAYMENT_METHOD_NONE):
        flash("Érvénytelen fizetési mód.", "error")
        return redirect(url_for("admin_teams", event_id=reg["event_id"]))

    execute(
        """
        UPDATE registrations
        SET payment_status = ?, payment_method = ?, payment_note = ?, is_manual = 1
        WHERE id = ?
        """,
        (payment_status, payment_method, payment_note, registration_id),
    )

    if payment_status == PAYMENT_INVALID:
        execute(
            """
            UPDATE registrations
            SET assigned_team = NULL, assigned_stage = NULL, assigned_slot = NULL,
                pending_stage = NULL, pending_position = NULL, removed_from_team = 1
            WHERE id = ?
            """,
            (registration_id,),
        )

    flash("Fizetési státusz frissítve.", "success")
    return redirect(url_for("admin_teams", event_id=reg["event_id"]))


@app.route("/admin/team/<int:event_id>/<int:team_number>/rename", methods=["POST"])
@admin_required
def admin_rename_team(event_id, team_number):
    event_row = get_event(event_id)
    if not event_row:
        flash("Az esemény nem található.", "error")
        return redirect(url_for("admin_dashboard"))

    new_name = normalize_team_name(request.form.get("team_name", ""))
    if not new_name:
        flash("Adj meg csapatnevet.", "error")
        return redirect(url_for("admin_teams", event_id=event_id))

    if team_name_exists(event_id, team_number, new_name):
        flash("Ezen az eseményen már létezik ilyen csapatnév.", "error")
        return redirect(url_for("admin_teams", event_id=event_id))

    admin_registration = query_one(
        """
        SELECT * FROM registrations
        WHERE event_id = ?
        ORDER BY id ASC LIMIT 1
        """,
        (event_id,),
    )
    if not admin_registration:
        flash("Ehhez a csapathoz még nincs jelentkező.", "error")
        return redirect(url_for("admin_teams", event_id=event_id))

    cur = execute(
        """
        INSERT INTO team_name_proposals (
            event_id, team_number, proposed_name, proposed_by_registration_id,
            status, created_at, is_admin_override
        )
        VALUES (?, ?, ?, ?, 'pending', ?, 1)
        """,
        (event_id, team_number, new_name, admin_registration["id"], now_str()),
    )
    evaluate_team_name_proposal(cur.lastrowid)

    flash("A csapatnév frissítve.", "success")
    return redirect(url_for("admin_teams", event_id=event_id))


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
        if len(members) >= 2:
            teams.append(
                {
                    "team_number": team_number,
                    "team_name": get_approved_team_name(event_id, team_number) or f"Csapat {team_number}",
                    "members": members,
                }
            )

    matches = []
    for i in range(len(teams)):
        for j in range(i + 1, len(teams)):
            matches.append(
                {
                    "team_a": teams[i],
                    "team_b": teams[j],
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
