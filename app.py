import os
import re
import sqlite3
import random
import unicodedata
import uuid
import urllib.request
from types import SimpleNamespace
from functools import wraps
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlparse, quote_plus
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
from markupsafe import Markup, escape
from werkzeug.utils import secure_filename

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
PERSIST_SUBDIR = (os.getenv("PERSIST_SUBDIR", "user-data").strip() or "user-data").strip("/\\")
PERSIST_STATIC_DIR = os.path.join(BASE_DIR, "static", PERSIST_SUBDIR)


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
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "dev-secret-change-me")

app.config["DATABASE"] = DB_PATH
app.config["DATABASE_URL"] = DATABASE_URL
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

TEAM_PREF_RANDOM = "random"
TEAM_PREF_FIXED = "fixed"

TEAM_PAIRING_MIXED = "mixed_auto"
TEAM_PAIRING_MIXED_CONFIRM = "mixed"
TEAM_PAIRING_FIXED_ONLY = "fixed_only"
TEAM_PAIRING_RANDOM_ONLY = "random_only"
MOVIE_NIGHT_ALLOWED_NAMES = ("Peti", "Jakab", "Martin")
MOVIE_NIGHT_AVATAR_FILES = {
    "Peti": "movie_night_avatars/peti.jpg",
    "Jakab": "movie_night_avatars/jakab.jpg",
    "Martin": "movie_night_avatars/martin.jpg",
}
MOVIE_NIGHT_STATUS_COMING = "coming"
MOVIE_NIGHT_STATUS_NOT_COMING = "not_coming"
DEFAULT_HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}

DISCIPLINE_IMAGE_UPLOAD_DIR = os.path.join(PERSIST_STATIC_DIR, "uploads", "discipline_images")
ALLOWED_DISCIPLINE_IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}
TEAM_AVATAR_UPLOAD_DIR = os.path.join(PERSIST_STATIC_DIR, "team_avatars", "custom")
ALLOWED_TEAM_AVATAR_UPLOAD_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}
TEAM_AVATAR_TARGET_SIZE = 256
EVENT_RESULT_IMAGE_UPLOAD_DIR = os.path.join(PERSIST_STATIC_DIR, "event_results")
ALLOWED_EVENT_RESULT_IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}
EVENT_RESULT_IMAGE_LANDSCAPE_WIDTH = 800
EVENT_RESULT_IMAGE_LANDSCAPE_HEIGHT = 600
EVENT_RESULT_IMAGE_PORTRAIT_WIDTH = 600
EVENT_RESULT_IMAGE_PORTRAIT_HEIGHT = 800
DISCIPLINE_IMAGE_MAX_WIDTH = 1024
DISCIPLINE_IMAGE_MAX_HEIGHT = 768
TEAM_AVATAR_CATALOG = [
    {"code": "avatar-01", "name": "Láng", "image_path": "/static/team_avatars/avatar-01.svg"},
    {"code": "avatar-02", "name": "Villám", "image_path": "/static/team_avatars/avatar-02.svg"},
    {"code": "avatar-03", "name": "Hullám", "image_path": "/static/team_avatars/avatar-03.svg"},
    {"code": "avatar-04", "name": "Hegy", "image_path": "/static/team_avatars/avatar-04.svg"},
    {"code": "avatar-05", "name": "Csillag", "image_path": "/static/team_avatars/avatar-05.svg"},
    {"code": "avatar-06", "name": "Rakéta", "image_path": "/static/team_avatars/avatar-06.svg"},
    {"code": "avatar-07", "name": "Korona", "image_path": "/static/team_avatars/avatar-07.svg"},
    {"code": "avatar-08", "name": "Sárkány", "image_path": "/static/team_avatars/avatar-08.svg"},
    {"code": "avatar-09", "name": "Pajzs", "image_path": "/static/team_avatars/avatar-09.svg"},
    {"code": "avatar-10", "name": "Sas", "image_path": "/static/team_avatars/avatar-10.svg"},
    {"code": "avatar-11", "name": "Farkas", "image_path": "/static/team_avatars/avatar-11.svg"},
    {"code": "avatar-12", "name": "Meteorit", "image_path": "/static/team_avatars/avatar-12.svg"},
]

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
def get_database_engine():
    database_url = (app.config.get("DATABASE_URL") or "").strip()
    if database_url.startswith("postgres://") or database_url.startswith("postgresql://"):
        return "postgres"
    return "sqlite"


def translate_sql(sql):
    if get_database_engine() == "postgres":
        return sql.replace("", "%s")
    return sql


def connect_postgres():
    try:
        import psycopg
        from psycopg.rows import dict_row
    except Exception as exc:
        raise RuntimeError(
            "A Postgres kapcsolat használatához telepítsd a psycopg csomagot."
        ) from exc

    return psycopg.connect(
        app.config["DATABASE_URL"],
        row_factory=dict_row,
    )


def get_db():
    if "db" not in g:
        if get_database_engine() == "postgres":
            g.db = connect_postgres()
        else:
            g.db = sqlite3.connect(app.config["DATABASE"])
            g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(error=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def query_all(sql, params=()):
    db = get_db()
    sql = translate_sql(sql)
    if get_database_engine() == "postgres":
        with db.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()
    return db.execute(sql, params).fetchall()


def query_one(sql, params=()):
    db = get_db()
    sql = translate_sql(sql)
    if get_database_engine() == "postgres":
        with db.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchone()
    return db.execute(sql, params).fetchone()


def execute(sql, params=()):
    db = get_db()
    sql = translate_sql(sql)
    if get_database_engine() == "postgres":
        lastrowid = None
        with db.cursor() as cur:
            statement = sql.strip()
            lowered = statement.lower()
            if lowered.startswith("insert into") and " returning " not in lowered:
                cur.execute(f"{statement} RETURNING id", params)
                row = cur.fetchone()
                if row:
                    if isinstance(row, dict):
                        lastrowid = row.get("id")
                    else:
                        lastrowid = row[0]
            else:
                cur.execute(statement, params)
        db.commit()
        return SimpleNamespace(lastrowid=lastrowid)

    cur = db.execute(sql, params)
    db.commit()
    return cur


def init_db():
    db = get_db()

    if get_database_engine() == "postgres":
        with db.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS events (
                    id SERIAL PRIMARY KEY,
                    title TEXT NOT NULL,
                    slug TEXT,
                    description TEXT,
                    results_html TEXT,
                    results_published_at TEXT,
                    event_at TEXT NOT NULL,
                    registration_deadline TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    is_closed INTEGER NOT NULL DEFAULT 0,
                    finalized_at TEXT,
                    has_fee INTEGER NOT NULL DEFAULT 0,
                    fee_amount INTEGER DEFAULT 0,
                    beneficiary_name TEXT,
                    bank_account TEXT,
                    team_pairing_mode TEXT NOT NULL DEFAULT 'mixed_auto'
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS registrations (
                    id SERIAL PRIMARY KEY,
                    event_id INTEGER NOT NULL REFERENCES events(id),
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
                pending_team_name_idea TEXT,
                pending_team_avatar_idea INTEGER,
                teammate_preference TEXT NOT NULL DEFAULT 'random',
                fixed_partner_registration_id INTEGER,
                fixed_partner_name TEXT,
                fixed_partner_approved_by_admin INTEGER NOT NULL DEFAULT 0,
                fixed_partner_payment_status TEXT,
                fixed_partner_payment_method TEXT,
                fixed_partner_payment_note TEXT
            )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS team_name_proposals (
                    id SERIAL PRIMARY KEY,
                    event_id INTEGER NOT NULL REFERENCES events(id),
                    team_number INTEGER NOT NULL,
                    proposed_name TEXT NOT NULL,
                    proposed_by_registration_id INTEGER NOT NULL REFERENCES registrations(id),
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL,
                    finalized_at TEXT,
                    is_admin_override INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS team_name_votes (
                    id SERIAL PRIMARY KEY,
                    proposal_id INTEGER NOT NULL REFERENCES team_name_proposals(id),
                    registration_id INTEGER NOT NULL REFERENCES registrations(id),
                    vote TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(proposal_id, registration_id)
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS disciplines (
                    id SERIAL PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
                    description TEXT NOT NULL,
                    image_path TEXT,
                    youtube_url TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS event_disciplines (
                    id SERIAL PRIMARY KEY,
                    event_id INTEGER NOT NULL REFERENCES events(id),
                    discipline_id INTEGER NOT NULL REFERENCES disciplines(id),
                    role TEXT NOT NULL,
                    sort_order INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(event_id, discipline_id, role)
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS event_extra_votes (
                    id SERIAL PRIMARY KEY,
                    event_id INTEGER NOT NULL REFERENCES events(id),
                    registration_id INTEGER NOT NULL REFERENCES registrations(id),
                    discipline_id INTEGER NOT NULL REFERENCES disciplines(id),
                    created_at TEXT NOT NULL,
                    UNIQUE(event_id, registration_id)
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS team_avatars (
                    id SERIAL PRIMARY KEY,
                    code TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    image_path TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS team_avatar_selections (
                    id SERIAL PRIMARY KEY,
                    event_id INTEGER NOT NULL REFERENCES events(id),
                    team_number INTEGER NOT NULL,
                    avatar_id INTEGER NOT NULL REFERENCES team_avatars(id),
                    selected_by_registration_id INTEGER NOT NULL REFERENCES registrations(id),
                    created_at TEXT NOT NULL,
                    UNIQUE(event_id, team_number)
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS event_results (
                    id SERIAL PRIMARY KEY,
                    event_id INTEGER NOT NULL REFERENCES events(id),
                    team_number INTEGER NOT NULL,
                    placement INTEGER NOT NULL,
                    points TEXT,
                    note TEXT,
                    image_path TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(event_id, team_number)
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS event_result_points (
                    id SERIAL PRIMARY KEY,
                    event_id INTEGER NOT NULL REFERENCES events(id),
                    team_number INTEGER NOT NULL,
                    discipline_id INTEGER NOT NULL REFERENCES disciplines(id),
                    points INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(event_id, team_number, discipline_id)
                )
                """
            )
        db.commit()
    else:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                slug TEXT UNIQUE,
                description TEXT,
                results_html TEXT,
                results_published_at TEXT,
                event_at TEXT NOT NULL,
                registration_deadline TEXT NOT NULL,
                created_at TEXT NOT NULL,
                is_closed INTEGER NOT NULL DEFAULT 0,
                finalized_at TEXT,

                has_fee INTEGER NOT NULL DEFAULT 0,
                fee_amount INTEGER DEFAULT 0,
                beneficiary_name TEXT,
                bank_account TEXT,
                team_pairing_mode TEXT NOT NULL DEFAULT 'mixed_auto'
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
                pending_team_name_idea TEXT,
                pending_team_avatar_idea INTEGER,
                teammate_preference TEXT NOT NULL DEFAULT 'random',
                fixed_partner_registration_id INTEGER,
                fixed_partner_name TEXT,
                fixed_partner_approved_by_admin INTEGER NOT NULL DEFAULT 0,
                fixed_partner_payment_status TEXT,
                fixed_partner_payment_method TEXT,
                fixed_partner_payment_note TEXT,

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

            CREATE TABLE IF NOT EXISTS disciplines (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                description TEXT NOT NULL,
                image_path TEXT,
                youtube_url TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS event_disciplines (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id INTEGER NOT NULL,
                discipline_id INTEGER NOT NULL,
                role TEXT NOT NULL,
                sort_order INTEGER NOT NULL DEFAULT 0,

                UNIQUE(event_id, discipline_id, role),
                FOREIGN KEY(event_id) REFERENCES events(id),
                FOREIGN KEY(discipline_id) REFERENCES disciplines(id)
            );

            CREATE TABLE IF NOT EXISTS event_extra_votes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id INTEGER NOT NULL,
                registration_id INTEGER NOT NULL,
                discipline_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,

                UNIQUE(event_id, registration_id),
                FOREIGN KEY(event_id) REFERENCES events(id),
                FOREIGN KEY(registration_id) REFERENCES registrations(id),
                FOREIGN KEY(discipline_id) REFERENCES disciplines(id)
            );

            CREATE TABLE IF NOT EXISTS team_avatars (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                image_path TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS team_avatar_selections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id INTEGER NOT NULL,
                team_number INTEGER NOT NULL,
                avatar_id INTEGER NOT NULL,
                selected_by_registration_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(event_id, team_number),
                FOREIGN KEY(event_id) REFERENCES events(id),
                FOREIGN KEY(avatar_id) REFERENCES team_avatars(id),
                FOREIGN KEY(selected_by_registration_id) REFERENCES registrations(id)
            );

            CREATE TABLE IF NOT EXISTS event_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id INTEGER NOT NULL,
                team_number INTEGER NOT NULL,
                placement INTEGER NOT NULL,
                points TEXT,
                note TEXT,
                image_path TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(event_id, team_number),
                FOREIGN KEY(event_id) REFERENCES events(id)
            );

            CREATE TABLE IF NOT EXISTS event_result_points (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id INTEGER NOT NULL,
                team_number INTEGER NOT NULL,
                discipline_id INTEGER NOT NULL,
                points INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(event_id, team_number, discipline_id),
                FOREIGN KEY(event_id) REFERENCES events(id),
                FOREIGN KEY(discipline_id) REFERENCES disciplines(id)
            );
            """
        )
    ensure_event_schema(db)
    ensure_registration_schema(db)
    ensure_discipline_schema(db)
    ensure_event_results_schema(db)
    ensure_movie_night_schema(db)
    db.commit()


def ensure_event_schema(db):
    if get_database_engine() == "postgres":
        with db.cursor() as cur:
            cur.execute("ALTER TABLE events ADD COLUMN IF NOT EXISTS slug TEXT")
            cur.execute("ALTER TABLE events ADD COLUMN IF NOT EXISTS results_html TEXT")
            cur.execute("ALTER TABLE events ADD COLUMN IF NOT EXISTS results_published_at TEXT")
            cur.execute("ALTER TABLE events ADD COLUMN IF NOT EXISTS team_pairing_mode TEXT")
            cur.execute(
                """
                UPDATE events
                SET team_pairing_mode = 'mixed_auto'
                WHERE team_pairing_mode IS NULL OR team_pairing_mode = ''
                """
            )
            cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_events_slug_unique ON events (slug)")
        db.commit()
        rows = query_all("SELECT id, title, event_at, slug FROM events ORDER BY id ASC")
    else:
        columns = {row["name"] for row in db.execute("PRAGMA table_info(events)").fetchall()}
        if "slug" not in columns:
            db.execute("ALTER TABLE events ADD COLUMN slug TEXT")
        if "results_html" not in columns:
            db.execute("ALTER TABLE events ADD COLUMN results_html TEXT")
        if "results_published_at" not in columns:
            db.execute("ALTER TABLE events ADD COLUMN results_published_at TEXT")
        if "team_pairing_mode" not in columns:
            db.execute("ALTER TABLE events ADD COLUMN team_pairing_mode TEXT")
        db.execute(
            """
            UPDATE events
            SET team_pairing_mode = 'mixed_auto'
            WHERE team_pairing_mode IS NULL OR team_pairing_mode = ''
            """
        )
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
        db.execute("UPDATE events SET slug = ? WHERE id = ? ", (slug, row["id"]))
        used_slugs.add(slug)


def ensure_registration_schema(db):
    if get_database_engine() == "postgres":
        with db.cursor() as cur:
            cur.execute("ALTER TABLE registrations ADD COLUMN IF NOT EXISTS pending_team_name_idea TEXT")
            cur.execute("ALTER TABLE registrations ADD COLUMN IF NOT EXISTS pending_team_avatar_idea INTEGER")
            cur.execute("ALTER TABLE registrations ADD COLUMN IF NOT EXISTS teammate_preference TEXT")
            cur.execute("ALTER TABLE registrations ADD COLUMN IF NOT EXISTS fixed_partner_registration_id INTEGER")
            cur.execute("ALTER TABLE registrations ADD COLUMN IF NOT EXISTS fixed_partner_name TEXT")
            cur.execute("ALTER TABLE registrations ADD COLUMN IF NOT EXISTS fixed_partner_approved_by_admin INTEGER")
            cur.execute("ALTER TABLE registrations ADD COLUMN IF NOT EXISTS fixed_partner_payment_status TEXT")
            cur.execute("ALTER TABLE registrations ADD COLUMN IF NOT EXISTS fixed_partner_payment_method TEXT")
            cur.execute("ALTER TABLE registrations ADD COLUMN IF NOT EXISTS fixed_partner_payment_note TEXT")
            cur.execute(
                """
                UPDATE registrations
                SET teammate_preference = 'random'
                WHERE teammate_preference IS NULL OR teammate_preference = ''
                """
            )
            cur.execute(
                """
                UPDATE registrations
                SET fixed_partner_approved_by_admin = 0
                WHERE fixed_partner_approved_by_admin IS NULL
                """
            )
        db.commit()
    else:
        columns = {row["name"] for row in db.execute("PRAGMA table_info(registrations)").fetchall()}
        if "pending_team_name_idea" not in columns:
            db.execute("ALTER TABLE registrations ADD COLUMN pending_team_name_idea TEXT")
        if "pending_team_avatar_idea" not in columns:
            db.execute("ALTER TABLE registrations ADD COLUMN pending_team_avatar_idea INTEGER")
        if "teammate_preference" not in columns:
            db.execute("ALTER TABLE registrations ADD COLUMN teammate_preference TEXT")
        if "fixed_partner_registration_id" not in columns:
            db.execute("ALTER TABLE registrations ADD COLUMN fixed_partner_registration_id INTEGER")
        if "fixed_partner_name" not in columns:
            db.execute("ALTER TABLE registrations ADD COLUMN fixed_partner_name TEXT")
        if "fixed_partner_approved_by_admin" not in columns:
            db.execute("ALTER TABLE registrations ADD COLUMN fixed_partner_approved_by_admin INTEGER")
        if "fixed_partner_payment_status" not in columns:
            db.execute("ALTER TABLE registrations ADD COLUMN fixed_partner_payment_status TEXT")
        if "fixed_partner_payment_method" not in columns:
            db.execute("ALTER TABLE registrations ADD COLUMN fixed_partner_payment_method TEXT")
        if "fixed_partner_payment_note" not in columns:
            db.execute("ALTER TABLE registrations ADD COLUMN fixed_partner_payment_note TEXT")
        db.execute(
            """
            UPDATE registrations
            SET teammate_preference = 'random'
            WHERE teammate_preference IS NULL OR teammate_preference = ''
            """
        )
        db.execute(
            """
            UPDATE registrations
            SET fixed_partner_approved_by_admin = 0
            WHERE fixed_partner_approved_by_admin IS NULL
            """
        )


def ensure_discipline_schema(db):
    if get_database_engine() == "postgres":
        with db.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS disciplines (
                    id SERIAL PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
                    description TEXT NOT NULL,
                    image_path TEXT,
                    youtube_url TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS event_disciplines (
                    id SERIAL PRIMARY KEY,
                    event_id INTEGER NOT NULL REFERENCES events(id),
                    discipline_id INTEGER NOT NULL REFERENCES disciplines(id),
                    role TEXT NOT NULL,
                    sort_order INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(event_id, discipline_id, role)
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS event_extra_votes (
                    id SERIAL PRIMARY KEY,
                    event_id INTEGER NOT NULL REFERENCES events(id),
                    registration_id INTEGER NOT NULL REFERENCES registrations(id),
                    discipline_id INTEGER NOT NULL REFERENCES disciplines(id),
                    created_at TEXT NOT NULL,
                    UNIQUE(event_id, registration_id)
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS team_avatars (
                    id SERIAL PRIMARY KEY,
                    code TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    image_path TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS team_avatar_selections (
                    id SERIAL PRIMARY KEY,
                    event_id INTEGER NOT NULL REFERENCES events(id),
                    team_number INTEGER NOT NULL,
                    avatar_id INTEGER NOT NULL REFERENCES team_avatars(id),
                    selected_by_registration_id INTEGER NOT NULL REFERENCES registrations(id),
                    created_at TEXT NOT NULL,
                    UNIQUE(event_id, team_number)
                )
                """
            )
            cur.execute("ALTER TABLE disciplines ADD COLUMN IF NOT EXISTS image_path TEXT")
            cur.execute("ALTER TABLE disciplines ADD COLUMN IF NOT EXISTS youtube_url TEXT")
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_event_disciplines_event_role
                ON event_disciplines (event_id, role, sort_order, id)
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_event_extra_votes_event_discipline
                ON event_extra_votes (event_id, discipline_id)
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_team_avatar_selections_event_team
                ON team_avatar_selections (event_id, team_number)
                """
            )
        db.commit()
    else:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS disciplines (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                description TEXT NOT NULL,
                image_path TEXT,
                youtube_url TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS event_disciplines (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id INTEGER NOT NULL,
                discipline_id INTEGER NOT NULL,
                role TEXT NOT NULL,
                sort_order INTEGER NOT NULL DEFAULT 0,

                UNIQUE(event_id, discipline_id, role),
                FOREIGN KEY(event_id) REFERENCES events(id),
                FOREIGN KEY(discipline_id) REFERENCES disciplines(id)
            );

            CREATE TABLE IF NOT EXISTS event_extra_votes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id INTEGER NOT NULL,
                registration_id INTEGER NOT NULL,
                discipline_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,

                UNIQUE(event_id, registration_id),
                FOREIGN KEY(event_id) REFERENCES events(id),
                FOREIGN KEY(registration_id) REFERENCES registrations(id),
                FOREIGN KEY(discipline_id) REFERENCES disciplines(id)
            );

            CREATE TABLE IF NOT EXISTS team_avatars (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                image_path TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS team_avatar_selections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id INTEGER NOT NULL,
                team_number INTEGER NOT NULL,
                avatar_id INTEGER NOT NULL,
                selected_by_registration_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(event_id, team_number),
                FOREIGN KEY(event_id) REFERENCES events(id),
                FOREIGN KEY(avatar_id) REFERENCES team_avatars(id),
                FOREIGN KEY(selected_by_registration_id) REFERENCES registrations(id)
            );

            CREATE INDEX IF NOT EXISTS idx_event_disciplines_event_role
            ON event_disciplines (event_id, role, sort_order, id);

            CREATE INDEX IF NOT EXISTS idx_event_extra_votes_event_discipline
            ON event_extra_votes (event_id, discipline_id);

            CREATE INDEX IF NOT EXISTS idx_team_avatar_selections_event_team
            ON team_avatar_selections (event_id, team_number);
            """
        )
        columns = {row["name"] for row in db.execute("PRAGMA table_info(disciplines)").fetchall()}
        if "image_path" not in columns:
            db.execute("ALTER TABLE disciplines ADD COLUMN image_path TEXT")
        if "youtube_url" not in columns:
            db.execute("ALTER TABLE disciplines ADD COLUMN youtube_url TEXT")

    ensure_team_avatar_catalog_seeded()


def ensure_event_results_schema(db):
    if get_database_engine() == "postgres":
        with db.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS event_results (
                    id SERIAL PRIMARY KEY,
                    event_id INTEGER NOT NULL REFERENCES events(id),
                    team_number INTEGER NOT NULL,
                    placement INTEGER NOT NULL,
                    points TEXT,
                    note TEXT,
                    image_path TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(event_id, team_number)
                )
                """
            )
            cur.execute("ALTER TABLE event_results ADD COLUMN IF NOT EXISTS image_path TEXT")
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS event_result_points (
                    id SERIAL PRIMARY KEY,
                    event_id INTEGER NOT NULL REFERENCES events(id),
                    team_number INTEGER NOT NULL,
                    discipline_id INTEGER NOT NULL REFERENCES disciplines(id),
                    points INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(event_id, team_number, discipline_id)
                )
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_event_results_event_placement
                ON event_results (event_id, placement, team_number)
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_event_result_points_event_team
                ON event_result_points (event_id, team_number, discipline_id)
                """
            )
        db.commit()
    else:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS event_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id INTEGER NOT NULL,
                team_number INTEGER NOT NULL,
                placement INTEGER NOT NULL,
                points TEXT,
                note TEXT,
                image_path TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(event_id, team_number),
                FOREIGN KEY(event_id) REFERENCES events(id)
            );
            CREATE INDEX IF NOT EXISTS idx_event_results_event_placement
            ON event_results (event_id, placement, team_number);

            CREATE TABLE IF NOT EXISTS event_result_points (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id INTEGER NOT NULL,
                team_number INTEGER NOT NULL,
                discipline_id INTEGER NOT NULL,
                points INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(event_id, team_number, discipline_id),
                FOREIGN KEY(event_id) REFERENCES events(id),
                FOREIGN KEY(discipline_id) REFERENCES disciplines(id)
            );

            CREATE INDEX IF NOT EXISTS idx_event_result_points_event_team
            ON event_result_points (event_id, team_number, discipline_id);
            """
        )
        columns = {row["name"] for row in db.execute("PRAGMA table_info(event_results)").fetchall()}
        if "image_path" not in columns:
            db.execute("ALTER TABLE event_results ADD COLUMN image_path TEXT")


def ensure_team_avatar_catalog_seeded():
    existing_count_row = query_one("SELECT COUNT(*) AS cnt FROM team_avatars")
    existing_count = int(existing_count_row["cnt"]) if existing_count_row else 0
    if existing_count > 0:
        return

    existing_codes = {
        row["code"] for row in query_all("SELECT code FROM team_avatars")
    }
    for item in TEAM_AVATAR_CATALOG:
        if item["code"] in existing_codes:
            continue
        execute(
            """
            INSERT INTO team_avatars (code, name, image_path, created_at)
            VALUES ( ?, ?, ?, ?)
            """,
            (item["code"], item["name"], item["image_path"], now_str()),
        )


def ensure_movie_night_schema(db):
    if get_database_engine() == "postgres":
        with db.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS movie_night_entries (
                    id SERIAL PRIMARY KEY,
                    cycle_key TEXT NOT NULL,
                    participant_name TEXT NOT NULL,
                    attendance_status TEXT NOT NULL DEFAULT 'coming',
                    movie_title TEXT NOT NULL,
                    poster_url TEXT,
                    poster_source TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(cycle_key, participant_name)
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS movie_night_draws (
                    id SERIAL PRIMARY KEY,
                    cycle_key TEXT NOT NULL UNIQUE,
                    winner_name TEXT NOT NULL,
                    winner_movie TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_movie_night_entries_cycle
                ON movie_night_entries (cycle_key, participant_name)
                """
            )
        db.commit()
    else:
        db.executescript(
            """
                CREATE TABLE IF NOT EXISTS movie_night_entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    cycle_key TEXT NOT NULL,
                    participant_name TEXT NOT NULL,
                    attendance_status TEXT NOT NULL DEFAULT 'coming',
                    movie_title TEXT NOT NULL,
                    poster_url TEXT,
                    poster_source TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(cycle_key, participant_name)
                );

            CREATE TABLE IF NOT EXISTS movie_night_draws (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cycle_key TEXT NOT NULL UNIQUE,
                winner_name TEXT NOT NULL,
                winner_movie TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_movie_night_entries_cycle
            ON movie_night_entries (cycle_key, participant_name);
            """
        )
    if get_database_engine() == "postgres":
        rows = query_all(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name = 'movie_night_entries'
            """
        )
        columns = {row["column_name"] for row in rows}
    else:
        columns = {
            row["name"] for row in db.execute("PRAGMA table_info(movie_night_entries)").fetchall()
        }
    if "poster_url" not in columns:
        execute("ALTER TABLE movie_night_entries ADD COLUMN poster_url TEXT")
    if "poster_source" not in columns:
        execute("ALTER TABLE movie_night_entries ADD COLUMN poster_source TEXT")
    if "attendance_status" not in columns:
        execute(
            f"ALTER TABLE movie_night_entries ADD COLUMN attendance_status TEXT NOT NULL DEFAULT '{MOVIE_NIGHT_STATUS_COMING}'"
        )
    execute(
        """
        UPDATE movie_night_entries
        SET attendance_status = ?
        WHERE attendance_status IS NULL OR attendance_status = ''
        """,
        (MOVIE_NIGHT_STATUS_COMING,),
    )


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


def movie_night_cycle_key(now=None):
    current = now or datetime.now()
    days_since_wednesday = (current.weekday() - 2) % 7
    cycle_start = current - timedelta(days=days_since_wednesday)
    return cycle_start.strftime("%Y-%m-%d")


def movie_night_next_reset_label(cycle_key):
    cycle_start = datetime.strptime(cycle_key, "%Y-%m-%d")
    next_reset = cycle_start + timedelta(days=7)
    return next_reset.strftime("%Y-%m-%d")


def get_movie_night_entries(cycle_key):
    rows = query_all(
        """
        SELECT participant_name, attendance_status, movie_title, poster_url, poster_source, created_at, updated_at
        FROM movie_night_entries
        WHERE cycle_key = ?
        """,
        (cycle_key,),
    )
    by_name = {row["participant_name"]: row for row in rows}
    ordered = []
    for name in MOVIE_NIGHT_ALLOWED_NAMES:
        row = by_name.get(name)
        if row:
            ordered.append(row)
    return ordered


def get_movie_night_draw(cycle_key):
    return query_one(
        """
        SELECT cycle_key, winner_name, winner_movie, created_at
        FROM movie_night_draws
        WHERE cycle_key = ?
        LIMIT 1
        """,
        (cycle_key,),
    )


def fetch_text_url(url, timeout=4):
    request_obj = urllib.request.Request(url, headers=DEFAULT_HTTP_HEADERS)
    with urllib.request.urlopen(request_obj, timeout=timeout) as response:
        raw = response.read()
    return raw.decode("utf-8", errors="ignore")


def extract_og_image_url(html):
    match = re.search(
        r"""<meta[^>]+property=["']og:image["'][^>]+content=["']([^"']+)["']""",
        html,
        re.IGNORECASE,
    )
    if not match:
        return ""
    return match.group(1).strip()


def extract_imdb_inline_poster_url(html):
    match = re.search(
        r"""https://m\.media-amazon\.com/images/M/[^"'\s>]+\.(?:jpg|jpeg|png)""",
        html,
        re.IGNORECASE,
    )
    return match.group(0) if match else ""


def extract_tmdb_movie_path(html):
    match = re.search(r"""href=["'](/movie/\d+[^"']*)["']""", html, re.IGNORECASE)
    if not match:
        return ""
    return match.group(1)


def extract_imdb_title_path(html):
    match = re.search(r"""href=["'](/title/tt\d+/[^"']*)["']""", html, re.IGNORECASE)
    if not match:
        return ""
    return match.group(1)


def lookup_movie_cover_url(movie_title):
    if app.config.get("TESTING"):
        return "", ""

    query = quote_plus(movie_title)
    tmdb_url = f"https://www.themoviedb.org/search?query={query}"
    imdb_url = f"https://www.imdb.com/find/?q={query}&s=tt"

    try:
        search_html = fetch_text_url(tmdb_url)
        movie_path = extract_tmdb_movie_path(search_html)
        if movie_path:
            movie_html = fetch_text_url(f"https://www.themoviedb.org{movie_path}")
            poster_url = extract_og_image_url(movie_html)
            if poster_url:
                return poster_url, "tmdb"
    except Exception:
        pass

    try:
        search_html = fetch_text_url(imdb_url)
        title_path = extract_imdb_title_path(search_html)
        if title_path:
            title_html = fetch_text_url(f"https://www.imdb.com{title_path}")
            poster_url = extract_og_image_url(title_html)
            if poster_url:
                return poster_url, "imdb"
        poster_url = extract_imdb_inline_poster_url(search_html)
        if poster_url:
            return poster_url, "imdb"
    except Exception:
        pass

    return "", ""


def backfill_movie_night_missing_posters(cycle_key, entries):
    updated = False
    for entry in entries:
        if entry["attendance_status"] == MOVIE_NIGHT_STATUS_NOT_COMING:
            continue
        if entry["poster_url"]:
            continue
        title = (entry["movie_title"] or "").strip()
        if not title:
            continue
        poster_url, poster_source = lookup_movie_cover_url(title)
        if not poster_url:
            continue
        execute(
            """
            UPDATE movie_night_entries
            SET poster_url = ?, poster_source = ?, updated_at = ?
            WHERE cycle_key = ? AND participant_name = ?
            """,
            (poster_url, poster_source, now_str(), cycle_key, entry["participant_name"]),
        )
        updated = True
    return updated


ALLOWED_DESCRIPTION_TAGS = {"p", "br", "strong", "b", "em", "i", "u", "ul", "ol", "li", "a", "blockquote"}
ALLOWED_DESCRIPTION_ATTRS = {"a": {"href", "target", "rel"}}


class DescriptionSanitizer(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag not in ALLOWED_DESCRIPTION_TAGS:
            return
        cleaned = []
        allowed = ALLOWED_DESCRIPTION_ATTRS.get(tag, set())
        for key, value in attrs:
            if key not in allowed or value is None:
                continue
            if key == "href":
                href = value.strip()
                if not (href.startswith("http://") or href.startswith("https://") or href.startswith("mailto:")):
                    continue
                value = href
            cleaned.append(f'{key}="{escape(value)}"')
        attr_text = f" {' '.join(cleaned)}" if cleaned else ""
        self.parts.append(f"<{tag}{attr_text}>")

    def handle_endtag(self, tag):
        if tag in ALLOWED_DESCRIPTION_TAGS and tag != "br":
            self.parts.append(f"</{tag}>")

    def handle_data(self, data):
        self.parts.append(str(escape(data)))

    def get_html(self):
        return "".join(self.parts)


def sanitize_event_description_html(value):
    parser = DescriptionSanitizer()
    parser.feed(value or "")
    parser.close()
    return parser.get_html().strip()


def format_event_description(value):
    raw = (value or "").strip()
    if not raw:
        return Markup("")
    if "<" in raw and ">" in raw:
        return Markup(sanitize_event_description_html(raw))
    paragraphs = [segment.strip() for segment in re.split(r"\r\n\r\n", raw) if segment.strip()]
    if not paragraphs:
        return Markup("")
    html_parts = []
    for paragraph in paragraphs:
        html_parts.append(f"<p>{escape(paragraph).replace(chr(10), Markup('<br>')).replace(chr(13), '')}</p>")
    return Markup("".join(str(part) for part in html_parts))


def compact_bank_account(value):
    return re.sub(r"\s+", "", (value or "").strip())


def build_payment_reference(event_id, participant_name=None):
    if participant_name:
        return f"KUPA + {participant_name} + {get_event_identifier(event_id)}"
    return f"KUPA + sajt nv + {get_event_identifier(event_id)}"


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
    return query_one("SELECT * FROM events WHERE id = ? ", (event_id,))


def get_event_by_slug(slug):
    return query_one("SELECT * FROM events WHERE slug = ? ", (slug,))


def get_all_events():
    return query_all("SELECT * FROM events ORDER BY event_at DESC, id DESC")


def get_discipline_by_id(discipline_id):
    return query_one(
        """
        SELECT id, name, description, image_path, youtube_url, created_at
        FROM disciplines
        WHERE id = ? """,
        (discipline_id,),
    )


def get_all_team_avatars():
    return query_all(
        """
        SELECT id, code, name, image_path
        FROM team_avatars
        ORDER BY id ASC
        """
    )


def get_team_avatar_selection(event_id, team_number):
    return query_one(
        """
        SELECT s.event_id, s.team_number, s.avatar_id, a.code, a.name, a.image_path
        FROM team_avatar_selections s
        JOIN team_avatars a ON a.id = s.avatar_id
        WHERE s.event_id = ? AND s.team_number = ? 
        LIMIT 1
        """,
        (event_id, team_number),
    )


def choose_team_avatar(event_id, team_number, avatar_id, registration_id):
    avatar = query_one("SELECT id FROM team_avatars WHERE id = ? ", (avatar_id,))
    if not avatar:
        raise ValueError("Á‰rvénytelen avatár.")

    execute(
        """
        INSERT INTO team_avatar_selections (
            event_id, team_number, avatar_id, selected_by_registration_id, created_at
        )
        VALUES ( ?, ?, ?, ?, ?)
        ON CONFLICT(event_id, team_number)
        DO UPDATE SET
            avatar_id = excluded.avatar_id,
            selected_by_registration_id = excluded.selected_by_registration_id,
            created_at = excluded.created_at
        """,
        (event_id, team_number, avatar_id, registration_id, now_str()),
    )


def parse_avatar_id(value):
    try:
        avatar_id = int(value)
    except (TypeError, ValueError):
        return None
    return avatar_id if avatar_id > 0 else None


def avatar_exists(avatar_id):
    if not avatar_id:
        return False
    row = query_one("SELECT id FROM team_avatars WHERE id = ? ", (avatar_id,))
    return row is not None


def normalize_discipline_name(name):
    return " ".join((name or "").strip().split())


def normalize_discipline_description(description):
    return " ".join((description or "").strip().split())


def normalize_youtube_embed_url(value):
    raw = (value or "").strip()
    if not raw:
        return ""

    parsed = urlparse(raw)
    host = (parsed.netloc or "").lower()
    path = parsed.path or ""
    video_id = None

    if "youtu.be" in host:
        video_id = path.strip("/").split("/")[0]
    elif "youtube.com" in host:
        if path == "/watch":
            video_id = parse_qs(parsed.query).get("v", [""])[0]
        elif path.startswith("/shorts/"):
            video_id = path.split("/")[2] if len(path.split("/")) > 2 else ""
        elif path.startswith("/embed/"):
            video_id = path.split("/")[2] if len(path.split("/")) > 2 else ""

    video_id = (video_id or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
        return ""
    return f"https://www.youtube.com/embed/{video_id}"


def save_uploaded_discipline_image(uploaded_file):
    if not uploaded_file or not getattr(uploaded_file, "filename", ""):
        return ""

    filename = secure_filename(uploaded_file.filename or "")
    if not filename or "." not in filename:
        raise ValueError("A feltöltött versenyszám-kép fájlneve érvénytelen.")

    extension = filename.rsplit(".", 1)[1].lower()
    if extension not in ALLOWED_DISCIPLINE_IMAGE_EXTENSIONS:
        raise ValueError("A versenyszám-kép csak PNG, JPG, JPEG, GIF vagy WEBP lehet.")

    os.makedirs(DISCIPLINE_IMAGE_UPLOAD_DIR, exist_ok=True)
    stored_name = f"{uuid.uuid4().hex}.{extension}"
    target_path = os.path.join(DISCIPLINE_IMAGE_UPLOAD_DIR, stored_name)

    Image = get_pillow_image_module()
    try:
        with Image.open(uploaded_file.stream) as source:
            width, height = source.size
            if width <= DISCIPLINE_IMAGE_MAX_WIDTH and height <= DISCIPLINE_IMAGE_MAX_HEIGHT:
                uploaded_file.stream.seek(0)
                uploaded_file.save(target_path)
                return f"/static/{PERSIST_SUBDIR}/uploads/discipline_images/{stored_name}"

            image = source.copy()
    except Exception as exc:
        raise ValueError("A feltöltött versenyszám-kép nem értelmezhető képként.") from exc

    resampling = getattr(getattr(Image, "Resampling", Image), "LANCZOS")
    image.thumbnail((DISCIPLINE_IMAGE_MAX_WIDTH, DISCIPLINE_IMAGE_MAX_HEIGHT), resampling)

    save_format = extension.upper()
    save_kwargs = {}
    if extension in {"jpg", "jpeg"}:
        save_format = "JPEG"
        if image.mode not in ("RGB", "L"):
            image = image.convert("RGB")
        save_kwargs = {"quality": 90, "optimize": True}
    elif extension == "png":
        save_format = "PNG"
        save_kwargs = {"optimize": True}
    elif extension == "webp":
        save_format = "WEBP"
        if image.mode not in ("RGB", "RGBA", "L"):
            image = image.convert("RGB")
        save_kwargs = {"quality": 90}
    elif extension == "gif":
        save_format = "GIF"

    image.save(target_path, format=save_format, **save_kwargs)
    return f"/static/{PERSIST_SUBDIR}/uploads/discipline_images/{stored_name}"


def get_pillow_image_module():
    try:
        from PIL import Image
    except Exception as exc:
        raise ValueError(
            "Képfeltöltéshez telepítsd a Pillow csomagot (pip install Pillow)."
        ) from exc
    return Image


def save_uploaded_team_avatar_image(uploaded_file):
    if not uploaded_file or not getattr(uploaded_file, "filename", ""):
        raise ValueError("Válassz ki egy avatar képet feltöltéshez.")

    filename = secure_filename(uploaded_file.filename or "")
    if not filename or "." not in filename:
        raise ValueError("Á‰rvénytelen avatar fájlnév.")

    extension = filename.rsplit(".", 1)[1].lower()
    if extension not in ALLOWED_TEAM_AVATAR_UPLOAD_EXTENSIONS:
        raise ValueError("Az avatar csak PNG, JPG, JPEG, GIF vagy WEBP lehet.")

    Image = get_pillow_image_module()
    os.makedirs(TEAM_AVATAR_UPLOAD_DIR, exist_ok=True)

    try:
        with Image.open(uploaded_file.stream) as source:
            image = source.convert("RGBA")
    except Exception as exc:
        raise ValueError("A feltöltött fájl nem értelmezhető képként.") from exc

    width, height = image.size
    crop_size = min(width, height)
    left = (width - crop_size) // 2
    top = (height - crop_size) // 2
    image = image.crop((left, top, left + crop_size, top + crop_size))

    resampling = getattr(getattr(Image, "Resampling", Image), "LANCZOS")
    image = image.resize((TEAM_AVATAR_TARGET_SIZE, TEAM_AVATAR_TARGET_SIZE), resampling)

    stored_name = f"user-{uuid.uuid4().hex}.png"
    target_path = os.path.join(TEAM_AVATAR_UPLOAD_DIR, stored_name)
    image.save(target_path, format="PNG", optimize=True)
    return f"/static/{PERSIST_SUBDIR}/team_avatars/custom/{stored_name}"


def save_uploaded_event_result_image(uploaded_file):
    if not uploaded_file or not getattr(uploaded_file, "filename", ""):
        return ""

    filename = secure_filename(uploaded_file.filename or "")
    if not filename or "." not in filename:
        raise ValueError("Á‰rvénytelen eredménykép fájlnév.")

    extension = filename.rsplit(".", 1)[1].lower()
    if extension not in ALLOWED_EVENT_RESULT_IMAGE_EXTENSIONS:
        raise ValueError("Az eredménykép csak PNG, JPG, JPEG, GIF vagy WEBP lehet.")

    Image = get_pillow_image_module()
    try:
        from PIL import ImageOps
    except Exception:
        ImageOps = None
    os.makedirs(EVENT_RESULT_IMAGE_UPLOAD_DIR, exist_ok=True)

    try:
        with Image.open(uploaded_file.stream) as source:
            if ImageOps is not None:
                source = ImageOps.exif_transpose(source)
            image = source.convert("RGB")
    except Exception as exc:
        raise ValueError("A feltöltött eredménykép nem értelmezhető képként.") from exc

    width, height = image.size
    is_portrait = height > width
    if is_portrait:
        target_width = EVENT_RESULT_IMAGE_PORTRAIT_WIDTH
        target_height = EVENT_RESULT_IMAGE_PORTRAIT_HEIGHT
    else:
        target_width = EVENT_RESULT_IMAGE_LANDSCAPE_WIDTH
        target_height = EVENT_RESULT_IMAGE_LANDSCAPE_HEIGHT

    resampling = getattr(getattr(Image, "Resampling", Image), "LANCZOS")
    image.thumbnail((target_width, target_height), resampling)

    # Keep the full uploaded content (no center-crop): pad to target canvas.
    canvas = Image.new("RGB", (target_width, target_height), (248, 250, 252))
    paste_x = max((target_width - image.width) // 2, 0)
    paste_y = max((target_height - image.height) // 2, 0)
    canvas.paste(image, (paste_x, paste_y))

    stored_name = f"result-{uuid.uuid4().hex}.jpg"
    target_path = os.path.join(EVENT_RESULT_IMAGE_UPLOAD_DIR, stored_name)
    canvas.save(target_path, format="JPEG", quality=90, optimize=True)
    return f"/static/{PERSIST_SUBDIR}/event_results/{stored_name}"


def delete_event_result_image_file(image_path):
    normalized_path = (image_path or "").strip()
    if not normalized_path:
        return

    prefixes = [
        "/static/event_results/",
        f"/static/{PERSIST_SUBDIR}/event_results/",
    ]

    for prefix in prefixes:
        if not normalized_path.startswith(prefix):
            continue

        filename = normalized_path[len(prefix) :]
        candidate_paths = [
            os.path.join(EVENT_RESULT_IMAGE_UPLOAD_DIR, filename),
            os.path.join(BASE_DIR, "static", "event_results", filename),
        ]
        for abs_path in candidate_paths:
            if os.path.isfile(abs_path):
                try:
                    os.remove(abs_path)
                except OSError:
                    pass
        break


def enrich_discipline_media(discipline_row):
    if not discipline_row:
        return discipline_row
    row = dict(discipline_row)
    row["image_path"] = (row.get("image_path") or "").strip()
    row["youtube_url"] = (row.get("youtube_url") or "").strip()
    row["youtube_embed_url"] = normalize_youtube_embed_url(row["youtube_url"])
    return row


def get_all_disciplines():
    rows = query_all(
        """
        SELECT id, name, description, image_path, youtube_url, created_at
        FROM disciplines
        ORDER BY lower(name) ASC, id ASC
        """
    )
    return [enrich_discipline_media(row) for row in rows]


def get_event_disciplines_by_role(event_id, role):
    rows = query_all(
        """
        SELECT d.id, d.name, d.description, d.image_path, d.youtube_url, ed.sort_order
        FROM event_disciplines ed
        JOIN disciplines d ON d.id = ed.discipline_id
        WHERE ed.event_id = ? AND ed.role = ? ORDER BY ed.sort_order ASC, ed.id ASC
        """,
        (event_id, role),
    )
    return [enrich_discipline_media(row) for row in rows]


def get_event_fixed_disciplines(event_id):
    return get_event_disciplines_by_role(event_id, "fixed")


def get_event_extra_discipline_options(event_id):
    return get_event_disciplines_by_role(event_id, "extra")


def unique_int_list(values):
    unique = []
    seen = set()
    for value in values:
        if value in seen:
            continue
        unique.append(value)
        seen.add(value)
    return unique


def parse_selected_discipline_ids(raw_values, valid_ids):
    selected = []
    for raw in raw_values:
        if raw is None:
            continue
        value = str(raw).strip()
        if not value:
            continue
        try:
            discipline_id = int(value)
        except (TypeError, ValueError):
            continue
        if discipline_id in valid_ids:
            selected.append(discipline_id)
    return unique_int_list(selected)


def parse_new_disciplines_from_form():
    names = request.form.getlist("new_discipline_name[]")
    descriptions = request.form.getlist("new_discipline_description[]")
    targets = request.form.getlist("new_discipline_target[]")
    youtube_urls = request.form.getlist("new_discipline_youtube_url[]")
    images = request.files.getlist("new_discipline_image[]")
    max_len = max(len(names), len(descriptions), len(targets), len(youtube_urls), len(images), 0)

    rows = []
    for index in range(max_len):
        name = normalize_discipline_name(names[index] if index < len(names) else "")
        description = normalize_discipline_description(
            descriptions[index] if index < len(descriptions) else ""
        )
        target = (targets[index] if index < len(targets) else "fixed").strip().lower()
        youtube_url = normalize_youtube_embed_url(
            youtube_urls[index] if index < len(youtube_urls) else ""
        )
        image_file = images[index] if index < len(images) else None
        has_image = bool(image_file and getattr(image_file, "filename", ""))

        if not name and not description and not youtube_url and not has_image:
            continue
        if not name or not description:
            raise ValueError(
                "Ášj versenyszámnál a név és a leírás együtt kötelező."
            )
        image_path = save_uploaded_discipline_image(image_file) if has_image else ""
        if target not in ("fixed", "extra"):
            target = "fixed"
        rows.append(
            {
                "name": name,
                "description": description,
                "target": target,
                "youtube_url": youtube_url,
                "image_path": image_path,
            }
        )
    return rows


def parse_discipline_updates_from_form():
    ids = request.form.getlist("edit_discipline_id[]")
    names = request.form.getlist("edit_discipline_name[]")
    descriptions = request.form.getlist("edit_discipline_description[]")
    youtube_urls = request.form.getlist("edit_discipline_youtube_url[]")
    images = request.files.getlist("edit_discipline_image[]")
    remove_image_ids = set()
    for raw in request.form.getlist("edit_discipline_remove_image[]"):
        try:
            remove_image_ids.add(int(raw))
        except (TypeError, ValueError):
            continue

    max_len = max(len(ids), len(names), len(descriptions), len(youtube_urls), len(images), 0)
    updates = []
    for index in range(max_len):
        raw_id = ids[index] if index < len(ids) else ""
        try:
            discipline_id = int(str(raw_id).strip())
        except (TypeError, ValueError):
            continue

        name = normalize_discipline_name(names[index] if index < len(names) else "")
        description = normalize_discipline_description(
            descriptions[index] if index < len(descriptions) else ""
        )
        youtube_url = normalize_youtube_embed_url(
            youtube_urls[index] if index < len(youtube_urls) else ""
        )
        image_file = images[index] if index < len(images) else None

        if not name or not description:
            raise ValueError("A versenyszám szerkesztésénél a név és a leírás kötelező.")

        image_path = ""
        has_new_image = bool(image_file and getattr(image_file, "filename", ""))
        if has_new_image:
            image_path = save_uploaded_discipline_image(image_file)

        updates.append(
            {
                "id": discipline_id,
                "name": name,
                "description": description,
                "youtube_url": youtube_url,
                "has_new_image": has_new_image,
                "new_image_path": image_path,
                "remove_image": discipline_id in remove_image_ids,
            }
        )
    return updates


def apply_discipline_updates_from_form():
    updates = parse_discipline_updates_from_form()
    for row in updates:
        current = get_discipline_by_id(row["id"])
        if not current:
            continue

        image_path = (current["image_path"] or "").strip()
        if row["remove_image"]:
            image_path = ""
        if row["has_new_image"]:
            image_path = row["new_image_path"]

        try:
            execute(
                """
                UPDATE disciplines
                SET name = ? , description = ? , youtube_url = ? , image_path = ? 
                WHERE id = ? """,
                (
                    row["name"],
                    row["description"],
                    row["youtube_url"],
                    image_path,
                    row["id"],
                ),
            )
        except Exception as exc:
            if "UNIQUE" in str(exc).upper():
                raise ValueError("Már létezik versenyszám ezzel a névvel.")
            raise


def create_or_get_discipline(name, description, image_path="", youtube_url=""):
    normalized_name = normalize_discipline_name(name)
    normalized_description = normalize_discipline_description(description)
    normalized_image_path = (image_path or "").strip()
    normalized_youtube_url = normalize_youtube_embed_url(youtube_url)

    if not normalized_name or not normalized_description:
        raise ValueError("A versenyszám neve és leírása nem lehet üres.")

    existing = query_one(
        """
        SELECT id, image_path, youtube_url
        FROM disciplines
        WHERE lower(name) = lower(?)
        ORDER BY id ASC
        LIMIT 1
        """,
        (normalized_name,),
    )
    if existing:
        updates = []
        params = []
        if normalized_image_path and not (existing["image_path"] or "").strip():
            updates.append("image_path = ")
            params.append(normalized_image_path)
        if normalized_youtube_url and not (existing["youtube_url"] or "").strip():
            updates.append("youtube_url = ")
            params.append(normalized_youtube_url)
        if updates:
            params.append(existing["id"])
            execute(
                f"UPDATE disciplines SET {', '.join(updates)} WHERE id = ",
                tuple(params),
            )
        return existing["id"]

    cur = execute(
        """
        INSERT INTO disciplines (name, description, image_path, youtube_url, created_at)
        VALUES ( ?, ?, ?, ?, ?)
        """,
        (
            normalized_name,
            normalized_description,
            normalized_image_path,
            normalized_youtube_url,
            now_str(),
        ),
    )
    return cur.lastrowid


def resolve_event_discipline_selection():
    all_disciplines = get_all_disciplines()
    valid_ids = {row["id"] for row in all_disciplines}

    fixed_ids = parse_selected_discipline_ids(
        request.form.getlist("fixed_discipline_ids"),
        valid_ids,
    )
    extra_ids = parse_selected_discipline_ids(
        request.form.getlist("extra_option_discipline_ids"),
        valid_ids,
    )

    new_rows = parse_new_disciplines_from_form()
    for row in new_rows:
        discipline_id = create_or_get_discipline(
            row["name"],
            row["description"],
            image_path=row["image_path"],
            youtube_url=row["youtube_url"],
        )
        if row["target"] == "extra":
            extra_ids.append(discipline_id)
        else:
            fixed_ids.append(discipline_id)

    fixed_ids = unique_int_list(fixed_ids)
    fixed_set = set(fixed_ids)
    extra_ids = [discipline_id for discipline_id in unique_int_list(extra_ids) if discipline_id not in fixed_set]

    if not fixed_ids:
        raise ValueError("Legalább egy fix versenyszámot ki kell választani.")

    return fixed_ids, extra_ids


def save_event_discipline_links(event_id, fixed_ids, extra_ids):
    execute("DELETE FROM event_extra_votes WHERE event_id = ? ", (event_id,))
    execute("DELETE FROM event_disciplines WHERE event_id = ? ", (event_id,))

    order = 1
    for discipline_id in unique_int_list(fixed_ids):
        execute(
            """
            INSERT INTO event_disciplines (event_id, discipline_id, role, sort_order)
            VALUES ( ?, ?, 'fixed', ?)
            """,
            (event_id, discipline_id, order),
        )
        order += 1

    fixed_set = set(fixed_ids)
    order = 1
    for discipline_id in unique_int_list(extra_ids):
        if discipline_id in fixed_set:
            continue
        execute(
            """
            INSERT INTO event_disciplines (event_id, discipline_id, role, sort_order)
            VALUES ( ?, ?, 'extra', ?)
            """,
            (event_id, discipline_id, order),
        )
        order += 1


def get_event_extra_vote(event_id, registration_id):
    if not registration_id:
        return None
    return query_one(
        """
        SELECT v.*, d.name AS discipline_name
        FROM event_extra_votes v
        JOIN disciplines d ON d.id = v.discipline_id
        WHERE v.event_id = ? AND v.registration_id = ? 
        LIMIT 1
        """,
        (event_id, registration_id),
    )


def get_event_extra_vote_summary(event_id):
    options = get_event_extra_discipline_options(event_id)
    if not options:
        return {"total_votes": 0, "winner_id": None, "options": []}

    rows = query_all(
        """
        SELECT discipline_id, COUNT(*) AS vote_count
        FROM event_extra_votes
        WHERE event_id = ? 
        GROUP BY discipline_id
        """,
        (event_id,),
    )
    count_by_id = {row["discipline_id"]: int(row["vote_count"]) for row in rows}

    decorated = []
    for option in options:
        option_data = dict(option)
        option_data["vote_count"] = count_by_id.get(option["id"], 0)
        decorated.append(option_data)

    total_votes = sum(option["vote_count"] for option in decorated)
    ranked = sorted(decorated, key=lambda item: item["vote_count"], reverse=True)
    winner_id = None
    if ranked and ranked[0]["vote_count"] > 0:
        if len(ranked) == 1 or ranked[0]["vote_count"] > ranked[1]["vote_count"]:
            winner_id = ranked[0]["id"]

    return {
        "total_votes": total_votes,
        "winner_id": winner_id,
        "options": ranked,
    }


def build_admin_event_form_context(event_row=None):
    event_view = build_public_event_view(event_row) if event_row else None
    selected_fixed_ids = []
    selected_extra_ids = []
    if event_row:
        selected_fixed_ids = [row["id"] for row in get_event_fixed_disciplines(event_row["id"])]
        selected_extra_ids = [row["id"] for row in get_event_extra_discipline_options(event_row["id"])]
    selected_team_pairing_mode = normalize_event_team_pairing_mode(
        event_row["team_pairing_mode"] if event_row and "team_pairing_mode" in event_row.keys() else TEAM_PAIRING_MIXED
    )
    return {
        "mode": "edit" if event_row else "create",
        "event": event_view,
        "all_disciplines": get_all_disciplines(),
        "selected_fixed_ids": selected_fixed_ids,
        "selected_extra_ids": selected_extra_ids,
        "selected_team_pairing_mode": selected_team_pairing_mode,
        "team_pairing_modes": [
            {"value": TEAM_PAIRING_MIXED, "label": team_pairing_mode_label(TEAM_PAIRING_MIXED)},
            {"value": TEAM_PAIRING_MIXED_CONFIRM, "label": team_pairing_mode_label(TEAM_PAIRING_MIXED_CONFIRM)},
            {"value": TEAM_PAIRING_FIXED_ONLY, "label": team_pairing_mode_label(TEAM_PAIRING_FIXED_ONLY)},
            {"value": TEAM_PAIRING_RANDOM_ONLY, "label": team_pairing_mode_label(TEAM_PAIRING_RANDOM_ONLY)},
        ],
    }


def delete_event_and_related_data(event_id):
    proposal_ids = query_all(
        "SELECT id FROM team_name_proposals WHERE event_id = ? ",
        (event_id,),
    )
    for proposal in proposal_ids:
        execute("DELETE FROM team_name_votes WHERE proposal_id = ? ", (proposal["id"],))

    execute("DELETE FROM team_avatar_selections WHERE event_id = ? ", (event_id,))
    execute("DELETE FROM event_results WHERE event_id = ? ", (event_id,))
    execute("DELETE FROM event_result_points WHERE event_id = ? ", (event_id,))
    execute("DELETE FROM team_name_proposals WHERE event_id = ? ", (event_id,))
    execute("DELETE FROM event_extra_votes WHERE event_id = ? ", (event_id,))
    execute("DELETE FROM event_disciplines WHERE event_id = ? ", (event_id,))
    execute("DELETE FROM registrations WHERE event_id = ? ", (event_id,))
    execute("DELETE FROM events WHERE id = ? ", (event_id,))


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
        WHERE event_id = ? AND is_deleted = 0
          AND payment_status != ? """,
        (event_id, PAYMENT_INVALID),
    )
    return row["cnt"] if row else 0


def get_registration_count(event_id):
    row = query_one(
        """
        SELECT COUNT(*) AS cnt
        FROM registrations
        WHERE event_id = ? AND is_deleted = 0
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
        PAYMENT_INVALID: "Á‰rvénytelen",
    }
    return mapping.get(status, status)


def payment_method_label(method):
    mapping = {
        PAYMENT_METHOD_TRANSFER: "Utalás",
        PAYMENT_METHOD_CASH: "Készpénz",
        PAYMENT_METHOD_NONE: "Nincs",
    }
    return mapping.get(method, method)


def team_pairing_mode_label(mode):
    mapping = {
        TEAM_PAIRING_MIXED: "Fix és véletlenszerű csapatelosztás",
        TEAM_PAIRING_MIXED_CONFIRM: "Fix (visszaigazolós) és véletlenszerű csapatelosztás",
        TEAM_PAIRING_FIXED_ONLY: "Fix csapatelosztás",
        TEAM_PAIRING_RANDOM_ONLY: "Véletlenszerű csapatelosztás",
    }
    return mapping.get(mode, mapping[TEAM_PAIRING_MIXED])


def normalize_event_team_pairing_mode(value):
    raw = (value or "").strip()
    allowed = {TEAM_PAIRING_MIXED, TEAM_PAIRING_MIXED_CONFIRM, TEAM_PAIRING_FIXED_ONLY, TEAM_PAIRING_RANDOM_ONLY}
    return raw if raw in allowed else TEAM_PAIRING_MIXED


def normalize_registration_teammate_preference(value):
    raw = (value or "").strip()
    return raw if raw in (TEAM_PREF_RANDOM, TEAM_PREF_FIXED) else TEAM_PREF_RANDOM


def get_registration_by_id(registration_id):
    return query_one("SELECT * FROM registrations WHERE id = ? ", (registration_id,))


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
        WHERE event_id = ? AND lower(participant_email) = lower(?)
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
        WHERE event_id = ? AND assigned_team = ? AND is_deleted = 0
          AND payment_status != ? ORDER BY assigned_slot ASC, id ASC
        """,
        (event_id, team_number, PAYMENT_INVALID),
    )


def get_unassigned_members(event_id):
    return query_all(
        """
        SELECT *
        FROM registrations
        WHERE event_id = ? AND assigned_team IS NULL
          AND is_deleted = 0
          AND payment_status != ? ORDER BY id ASC
        """,
        (event_id, PAYMENT_INVALID),
    )


def detach_fixed_partner_links(removed_registration_id):
    execute(
        """
        UPDATE registrations
        SET fixed_partner_registration_id = NULL,
            fixed_partner_approved_by_admin = 0
        WHERE fixed_partner_registration_id = ? """,
        (removed_registration_id,),
    )


def get_current_team_size(event_id, team_number):
    row = query_one(
        """
        SELECT COUNT(*) AS cnt
        FROM registrations
        WHERE event_id = ? AND assigned_team = ? AND is_deleted = 0
          AND payment_status != ? """,
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
        WHERE event_id = ? AND pending_stage = 3
          AND assigned_team IS NULL
          AND is_deleted = 0
          AND payment_status != ? """,
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
        WHERE event_id = ? AND team_number = ? AND status = 'approved'
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
        WHERE event_id = ? AND team_number = ? AND status = 'pending'
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
        WHERE event_id = ? AND team_number = ? AND status = 'approved'
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
        WHERE proposal_id = ? ORDER BY id ASC
        """,
        (proposal_id,),
    )


def create_or_replace_team_name_proposal(event_id, team_number, registration_id, proposed_name, is_admin_override=0):
    proposed_name = normalize_team_name(proposed_name)
    if not proposed_name:
        raise ValueError("Adj meg egy csapatnevet.")

    if team_name_exists(event_id, team_number, proposed_name):
        raise ValueError("Ezen az eseményen már létezik ilyen csapatnév.")

    old_pending = get_pending_team_name_proposal(event_id, team_number)
    if old_pending:
        execute(
            "UPDATE team_name_proposals SET status = 'rejected', finalized_at = ? WHERE id = ? ",
            (now_str(), old_pending["id"]),
        )

    cur = execute(
        """
        INSERT INTO team_name_proposals (
            event_id, team_number, proposed_name, proposed_by_registration_id, status, created_at, is_admin_override
        )
        VALUES ( ?, ?, ?, ?, 'pending', ?, ?)
        """,
        (event_id, team_number, proposed_name, registration_id, now_str(), is_admin_override),
    )
    proposal_id = cur.lastrowid

    execute(
        """
        INSERT INTO team_name_votes (proposal_id, registration_id, vote, created_at)
        VALUES ( ?, ?, 'approve', ?)
        ON CONFLICT(proposal_id, registration_id)
        DO UPDATE SET vote = excluded.vote, created_at = excluded.created_at
        """,
        (proposal_id, registration_id, now_str()),
    )

    evaluate_team_name_proposal(proposal_id)
    return proposal_id


def activate_stored_team_name_idea(registration_id):
    reg = get_registration_by_id(registration_id)
    if not reg or reg["assigned_team"] is None:
        return

    idea = normalize_team_name(reg["pending_team_name_idea"] or "")
    if not idea:
        return

    existing = get_visible_team_name_proposal(reg["event_id"], reg["assigned_team"])
    if existing:
        execute("UPDATE registrations SET pending_team_name_idea = NULL WHERE id = ? ", (registration_id,))
        return

    try:
        create_or_replace_team_name_proposal(
            reg["event_id"],
            reg["assigned_team"],
            registration_id,
            idea,
        )
    finally:
        execute("UPDATE registrations SET pending_team_name_idea = NULL WHERE id = ? ", (registration_id,))


def activate_stored_team_avatar_idea(registration_id):
    reg = get_registration_by_id(registration_id)
    if not reg or reg["assigned_team"] is None:
        return

    avatar_id = parse_avatar_id(reg["pending_team_avatar_idea"])
    if not avatar_id:
        return

    try:
        existing = get_team_avatar_selection(reg["event_id"], reg["assigned_team"])
        if not existing and avatar_exists(avatar_id):
            choose_team_avatar(
                reg["event_id"],
                reg["assigned_team"],
                avatar_id,
                registration_id,
            )
    finally:
        execute("UPDATE registrations SET pending_team_avatar_idea = NULL WHERE id = ? ", (registration_id,))


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
        "teammate_preference": normalize_registration_teammate_preference(
            reg["teammate_preference"] if "teammate_preference" in reg.keys() else TEAM_PREF_RANDOM
        ),
        "can_switch_to_random": False,
    }

    if reg["payment_status"] == PAYMENT_INVALID:
        status["status_label"] = "A jelentkezésed érvénytelenné vált"
        status["team_number"] = None
        status["team_name"] = None
        status["team_name_state"] = None
        status["team_size"] = None
        status["pending_proposal"] = None
        status["can_propose_team_name"] = False
        status["team_avatar"] = None
        return status

    if reg["assigned_team"] is not None:
        team_members = get_team_members(event_id, reg["assigned_team"])
        team_size = len(team_members)
        team_name_state = build_team_name_state(event_id, reg["assigned_team"])
        approved_name = get_approved_team_name(event_id, reg["assigned_team"])
        pending_proposal = get_pending_team_name_proposal(event_id, reg["assigned_team"])
        team_avatar = get_team_avatar_selection(event_id, reg["assigned_team"])

        status["status_label"] = "Csapatba kerültél"
        status["team_number"] = reg["assigned_team"]
        status["team_size"] = team_size
        status["team_name"] = team_name_state["name"] if team_name_state else approved_name
        status["team_name_state"] = team_name_state
        status["pending_proposal"] = pending_proposal
        status["can_propose_team_name"] = not event_has_started_or_closed(event_row)
        status["team_avatar"] = team_avatar
        status["can_switch_to_random"] = (
            not event_has_started_or_closed(event_row)
            and status["teammate_preference"] == TEAM_PREF_FIXED
            and len(team_members) == 1
            and not reg["fixed_partner_registration_id"]
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
    status["team_avatar"] = None
    return status


def build_public_teams(event_id):
    event_row = get_event(event_id)
    event_closed = bool(event_row and event_row["is_closed"] == 1)
    teams = []
    for team_number in range(1, MAX_TEAMS + 1):
        members = get_team_members_with_approved_virtual_partner(event_id, team_number)
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
        team_avatar = get_team_avatar_selection(event_id, team_number)

        visible_names = []
        single_waiting_message = ""
        show_single_member_name = False
        # a specifikáció szerint:
        # 1/2 -> név rejtett
        # 2/2 -> látszik
        # 2/3 -> 2 látszik, az új nem
        # 3/3 -> látszik
        # 3/4 -> 3 látszik, az új nem
        # 4/4 -> látszik
        if count >= 2:
            visible_names = [m["participant_name"] for m in members]
        elif count == 1:
            member = members[0]
            is_fixed_waiting = (
                member["teammate_preference"] == TEAM_PREF_FIXED
                and not member["fixed_partner_registration_id"]
            )
            if is_fixed_waiting:
                show_single_member_name = True
                visible_names = [member["participant_name"]]
                partner_name = (member["fixed_partner_name"] or "").strip()
                if partner_name:
                    if event_closed:
                        single_waiting_message = f"{partner_name} automatikusan visszaigazoltnak tekintve."
                    else:
                        single_waiting_message = f"{partner_name} visszajelzését várjuk."
                else:
                    single_waiting_message = "Fix csapattárs visszajelzését várjuk."

        teams.append(
            {
                "team_number": team_number,
                "count": count,
                "capacity": display_capacity,
                "visible_names": visible_names,
                "team_name": team_name_state["name"] if team_name_state else None,
                "team_name_state": team_name_state,
                "members": members,
                "team_avatar": team_avatar,
                "show_single_member_name": show_single_member_name,
                "single_waiting_message": single_waiting_message,
            }
        )
    return teams


# --------------------------------------------------
# Assignment logic
# --------------------------------------------------
def assign_to_stage2_team(registration_id, team_number, slot):
    execute(
        """
        UPDATE registrations
        SET assigned_team = ? , assigned_stage = 2, assigned_slot = ? ,
            pending_stage = NULL, pending_position = NULL, removed_from_team = 0
        WHERE id = ? """,
        (team_number, slot, registration_id),
    )


def get_stage2_team_members(event_id, team_number):
    return query_all(
        """
        SELECT *
        FROM registrations
        WHERE event_id = ? AND assigned_team = ? AND assigned_stage = 2
          AND is_deleted = 0
          AND payment_status != ? ORDER BY assigned_slot ASC, id ASC
        """,
        (event_id, team_number, PAYMENT_INVALID),
    )


def get_team_members_with_approved_virtual_partner(event_id, team_number):
    real_members = [dict(row) for row in get_team_members(event_id, team_number)]
    if len(real_members) != 1:
        return real_members

    only_member = real_members[0]
    partner_name = (only_member.get("fixed_partner_name") or "").strip()
    partner_approved = int(only_member.get("fixed_partner_approved_by_admin") or 0) == 1
    is_fixed_waiting = (
        only_member.get("teammate_preference") == TEAM_PREF_FIXED
        and not only_member.get("fixed_partner_registration_id")
    )
    if not (is_fixed_waiting and partner_approved and partner_name):
        return real_members

    event = get_event(event_id)
    has_fee = bool(event and int(event["has_fee"] or 0) == 1)
    virtual_payment_status = only_member.get("fixed_partner_payment_status") or (
        PAYMENT_PENDING if has_fee else PAYMENT_PAID
    )
    virtual_payment_method = only_member.get("fixed_partner_payment_method") or PAYMENT_METHOD_NONE
    virtual_payment_note = only_member.get("fixed_partner_payment_note") or ""

    virtual_member = {
        "id": None,
        "event_id": event_id,
        "participant_name": partner_name,
        "participant_email": "Szervező által jóváhagyott fix csapattárs",
        "payment_status": virtual_payment_status,
        "payment_method": virtual_payment_method,
        "payment_note": virtual_payment_note,
        "payment_status_label": payment_label(virtual_payment_status),
        "payment_method_label": payment_method_label(virtual_payment_method),
        "source_registration_id": only_member["id"],
        "is_virtual_partner": True,
    }
    real_members.append(virtual_member)
    return real_members


def get_fixed_waiting_candidates(event_id):
    candidates = []
    for team_number in range(1, MAX_TEAMS + 1):
        members = get_stage2_team_members(event_id, team_number)
        if len(members) != 1:
            continue
        member = members[0]
        if member["teammate_preference"] != TEAM_PREF_FIXED:
            continue
        if member["fixed_partner_registration_id"]:
            continue
        if member["fixed_partner_approved_by_admin"] == 1:
            continue
        candidates.append(
            {
                "registration_id": member["id"],
                "participant_name": member["participant_name"],
                "team_number": team_number,
                "fixed_partner_name": (member["fixed_partner_name"] or "").strip(),
            }
        )
    candidates.sort(key=lambda item: item["registration_id"])
    return candidates


def find_random_waiting_single(event_id, exclude_registration_id=None):
    for team_number in range(1, MAX_TEAMS + 1):
        members = get_stage2_team_members(event_id, team_number)
        if len(members) != 1:
            continue
        member = members[0]
        if exclude_registration_id and member["id"] == exclude_registration_id:
            continue
        if member["teammate_preference"] != TEAM_PREF_RANDOM:
            continue
        return member
    return None


def find_empty_stage2_team(event_id):
    for team_number in range(1, MAX_TEAMS + 1):
        members = get_stage2_team_members(event_id, team_number)
        if not members:
            return team_number
    return None


def assign_random_stage2(event_id, registration_id):
    waiting = find_random_waiting_single(event_id)
    if waiting:
        assign_to_stage2_team(registration_id, waiting["assigned_team"], 2)
        return

    empty_team = find_empty_stage2_team(event_id)
    if empty_team is None:
        raise ValueError("Nincs szabad csapathely a véletlenszerű párosításhoz.")
    assign_to_stage2_team(registration_id, empty_team, 1)


def assign_fixed_stage2(event_id, registration_id, fixed_partner_registration_id=None):
    if fixed_partner_registration_id:
        partner = query_one(
            """
            SELECT *
            FROM registrations
            WHERE id = ? AND event_id = ? AND is_deleted = 0
              AND payment_status != ? 
            LIMIT 1
            """,
            (fixed_partner_registration_id, event_id, PAYMENT_INVALID),
        )
        if not partner:
            raise ValueError("A választott fix csapattárs nem található.")
        if partner["teammate_preference"] != TEAM_PREF_FIXED:
            raise ValueError("A választott játékos nem fix csapattársra vár.")
        if partner["fixed_partner_registration_id"]:
            raise ValueError("A választott fix csapattárs már párosítva lett.")
        if partner["assigned_team"] is None or partner["assigned_stage"] != 2:
            raise ValueError("A választott fix csapattárs jelenleg nem párosítható.")

        partner_team_members = get_stage2_team_members(event_id, partner["assigned_team"])
        if len(partner_team_members) != 1 or partner_team_members[0]["id"] != partner["id"]:
            raise ValueError("A választott fix csapattárs csapata már nem üres.")

        assign_to_stage2_team(registration_id, partner["assigned_team"], 2)
        current_reg = get_registration_by_id(registration_id)
        execute(
            "UPDATE registrations SET fixed_partner_registration_id = ? , fixed_partner_payment_status = NULL, fixed_partner_payment_method = NULL, fixed_partner_payment_note = NULL WHERE id = ? ",
            (partner["id"], registration_id),
        )
        execute(
            "UPDATE registrations SET fixed_partner_registration_id = ? , fixed_partner_name = ? , fixed_partner_approved_by_admin = 0, fixed_partner_payment_status = NULL, fixed_partner_payment_method = NULL, fixed_partner_payment_note = NULL WHERE id = ? ",
            (registration_id, (current_reg["participant_name"] if current_reg else ""), partner["id"]),
        )
        execute(
            "UPDATE registrations SET fixed_partner_approved_by_admin = 0 WHERE id = ? ",
            (registration_id,),
        )
        return

    empty_team = find_empty_stage2_team(event_id)
    if empty_team is None:
        raise ValueError(
            "Nincs új üres csapat fix várakozáshoz. Válassz a fixen várók közül, vagy állj véletlenszerűre."
        )
    assign_to_stage2_team(registration_id, empty_team, 1)


def try_pair_switched_random_registration(event_id, registration_id):
    reg = get_registration_by_id(registration_id)
    if not reg or reg["event_id"] != event_id or reg["assigned_stage"] != 2 or reg["assigned_team"] is None:
        return

    my_team_members = get_stage2_team_members(event_id, reg["assigned_team"])
    if len(my_team_members) != 1:
        return

    waiting = find_random_waiting_single(event_id, exclude_registration_id=registration_id)
    if not waiting:
        return
    assign_to_stage2_team(registration_id, waiting["assigned_team"], 2)


def queue_for_stage(event_id, registration_id, stage):
    current_pending = query_one(
        """
        SELECT COUNT(*) AS cnt
        FROM registrations
        WHERE event_id = ? AND pending_stage = ? AND assigned_team IS NULL
          AND is_deleted = 0
          AND payment_status != ? """,
        (event_id, stage, PAYMENT_INVALID),
    )["cnt"]

    execute(
        """
        UPDATE registrations
        SET pending_stage = ? , pending_position = ? 
        WHERE id = ? """,
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
            SET assigned_team = ? , assigned_stage = ? , assigned_slot = ? ,
                pending_stage = NULL, pending_position = NULL, removed_from_team = 0
            WHERE id = ? """,
            (team_number, stage, slot, reg["id"]),
        )
        activate_stored_team_name_idea(reg["id"])
        activate_stored_team_avatar_idea(reg["id"])


def get_pending_pool(event_id, stage):
    return query_all(
        """
        SELECT *
        FROM registrations
        WHERE event_id = ? AND pending_stage = ? AND assigned_team IS NULL
          AND is_deleted = 0
          AND payment_status != ? ORDER BY pending_position, id
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
        WHERE event_id = ? AND assigned_stage = ? AND assigned_team IS NOT NULL
          AND is_deleted = 0
          AND payment_status != ? """,
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
            SET assigned_team = ? , assigned_stage = ? , assigned_slot = ? ,
                pending_stage = NULL, pending_position = NULL, removed_from_team = 0
            WHERE id = ? """,
            (team_number, stage, slot, reg["id"]),
        )
        activate_stored_team_name_idea(reg["id"])
        activate_stored_team_avatar_idea(reg["id"])


def register_participant(
    event_id,
    name,
    email,
    provider,
    google_sub=None,
    payment_method=None,
    team_name_idea=None,
    team_avatar_idea_id=None,
    teammate_preference=TEAM_PREF_RANDOM,
    fixed_partner_registration_id=None,
    fixed_partner_name=None,
):
    # stage-2 teammate preference handling
    event_row = get_event(event_id)
    if not event_row:
        raise ValueError("Az esemény nem található.")

    if event_has_started_or_closed(event_row):
        raise ValueError("A jelentkezés lezárult.")

    current_count = get_active_registration_count(event_id)
    if current_count >= MAX_PLAYERS:
        raise ValueError("A jelentkezés lezárult, a maximum 20 fő betelt.")

    teammate_preference = normalize_registration_teammate_preference(teammate_preference)
    event_pairing_mode = normalize_event_team_pairing_mode(
        event_row["team_pairing_mode"] if "team_pairing_mode" in event_row.keys() else TEAM_PAIRING_MIXED
    )
    if provider != "manual":
        if event_pairing_mode == TEAM_PAIRING_FIXED_ONLY and teammate_preference != TEAM_PREF_FIXED:
            raise ValueError("Ehhez az eseményhez csak fix csapattárssal lehet jelentkezni.")
        if event_pairing_mode == TEAM_PAIRING_RANDOM_ONLY and teammate_preference != TEAM_PREF_RANDOM:
            raise ValueError("Ehhez az eseményhez csak véletlenszerű csapattárssal lehet jelentkezni.")
    else:
        teammate_preference = TEAM_PREF_RANDOM

    if teammate_preference == TEAM_PREF_FIXED and current_count >= 10:
        raise ValueError("Fix csapattárs mód csak az első 10 jelentkezőnél használható.")

    if event_row["has_fee"] == 1:
        payment_status = PAYMENT_PENDING
        if payment_method not in (PAYMENT_METHOD_TRANSFER, PAYMENT_METHOD_CASH):
            raise ValueError("Fizetős eseménynél válassz fizetési módot: utalás vagy készpénz.")
    else:
        payment_status = PAYMENT_PAID
        payment_method = PAYMENT_METHOD_NONE
    avatar_idea_id = parse_avatar_id(team_avatar_idea_id)
    if avatar_idea_id and not avatar_exists(avatar_idea_id):
        raise ValueError("Érvénytelen avatar választás.")

    fixed_partner_name = (fixed_partner_name or "").strip()
    fixed_partner_approved_by_admin = 0
    if (
        teammate_preference == TEAM_PREF_FIXED
        and event_pairing_mode == TEAM_PAIRING_MIXED
        and not fixed_partner_registration_id
        and fixed_partner_name
    ):
        fixed_partner_approved_by_admin = 1
    fixed_partner_payment_status = None
    fixed_partner_payment_method = None
    fixed_partner_payment_note = ""
    if teammate_preference == TEAM_PREF_FIXED and fixed_partner_name and not fixed_partner_registration_id:
        fixed_partner_payment_status = PAYMENT_PENDING if event_row["has_fee"] == 1 else PAYMENT_PAID
        fixed_partner_payment_method = PAYMENT_METHOD_NONE
        fixed_partner_payment_note = ""

    cur = execute(
        """
        INSERT INTO registrations (
            event_id, participant_name, participant_email, provider, google_sub, created_at,
            payment_status, payment_method, payment_note, pending_team_name_idea, pending_team_avatar_idea,
            teammate_preference, fixed_partner_registration_id, fixed_partner_name, fixed_partner_approved_by_admin,
            fixed_partner_payment_status, fixed_partner_payment_method, fixed_partner_payment_note
        )
        VALUES ( ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            normalize_team_name(team_name_idea or ""),
            avatar_idea_id,
            teammate_preference,
            fixed_partner_registration_id,
            fixed_partner_name,
            fixed_partner_approved_by_admin,
            fixed_partner_payment_status,
            fixed_partner_payment_method,
            fixed_partner_payment_note,
        ),
    )
    registration_id = cur.lastrowid

    total_after_insert = get_active_registration_count(event_id)

    try:
        if total_after_insert <= 10:
            if teammate_preference == TEAM_PREF_FIXED:
                assign_fixed_stage2(
                    event_id,
                    registration_id,
                    fixed_partner_registration_id=fixed_partner_registration_id,
                )
            else:
                assign_random_stage2(event_id, registration_id)
            activate_stored_team_name_idea(registration_id)
            activate_stored_team_avatar_idea(registration_id)
        elif 11 <= total_after_insert <= 15:
            queue_for_stage(event_id, registration_id, 3)
            if total_after_insert == 15:
                assign_pending_stage_full(event_id, 3)
        elif 16 <= total_after_insert <= 20:
            queue_for_stage(event_id, registration_id, 4)
            if total_after_insert == 20:
                assign_pending_stage_full(event_id, 4)
    except ValueError:
        execute("DELETE FROM registrations WHERE id = ? ", (registration_id,))
        raise

    return registration_id


def remove_invalid_from_teams(event_id):
    invalid_regs = query_all(
        """
        SELECT id
        FROM registrations
        WHERE event_id = ? AND payment_status = ? AND is_deleted = 0
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
            WHERE id = ? """,
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
            WHERE event_id = ? AND payment_status = ? AND is_deleted = 0
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
        WHERE id = ? """,
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
              AND is_deleted = 0 AND payment_status != ? """,
            (event_id, PAYMENT_INVALID),
        )["cnt"],
        "stage4_pending_count": query_one(
            """
            SELECT COUNT(*) AS cnt
            FROM registrations
            WHERE event_id = ? AND pending_stage = 4 AND assigned_team IS NULL
              AND is_deleted = 0 AND payment_status != ? """,
            (event_id, PAYMENT_INVALID),
        )["cnt"],
    }


def get_event_results(event_id):
    return query_all(
        """
        SELECT id, event_id, team_number, placement, points, note, image_path, created_at, updated_at
        FROM event_results
        WHERE event_id = ? ORDER BY placement ASC, team_number ASC
        """,
        (event_id,),
    )


def get_event_results_by_team(event_id):
    rows = get_event_results(event_id)
    return {row["team_number"]: row for row in rows}


def get_event_result_points(event_id):
    return query_all(
        """
        SELECT rp.event_id, rp.team_number, rp.discipline_id, rp.points, d.name AS discipline_name
        FROM event_result_points rp
        JOIN disciplines d ON d.id = rp.discipline_id
        WHERE rp.event_id = ? ORDER BY rp.team_number ASC, d.name ASC
        """,
        (event_id,),
    )


def get_event_result_points_by_team(event_id):
    rows = get_event_result_points(event_id)
    by_team = {}
    for row in rows:
        team_bucket = by_team.setdefault(row["team_number"], {})
        team_bucket[row["discipline_id"]] = {
            "discipline_id": row["discipline_id"],
            "discipline_name": row["discipline_name"],
            "points": int(row["points"]),
        }
    return by_team


def get_team_display_name(event_id, team_number):
    team_name_state = build_team_name_state(event_id, team_number)
    if team_name_state and team_name_state.get("name"):
        return team_name_state["name"]
    approved_name = get_approved_team_name(event_id, team_number)
    if approved_name:
        return approved_name
    return f"Csapat {team_number}"


def build_event_results_editor_rows(event_id):
    existing_results = get_event_results_by_team(event_id)
    points_by_team = get_event_result_points_by_team(event_id)
    disciplines = get_event_fixed_disciplines(event_id)

    teams = []
    for team in build_public_teams(event_id):
        if team["count"] == 0:
            continue
        teams.append(
            {
                "team_number": team["team_number"],
                "team_name": team["team_name"] or f"Csapat {team['team_number']}",
                "member_count": team["count"],
            }
        )

    teams.sort(key=lambda item: item["team_number"])

    if existing_results:
        teams.sort(
            key=lambda item: (
                existing_results[item["team_number"]]["placement"]
                if item["team_number"] in existing_results
                else 9999,
                item["team_number"],
            )
        )

    rows = []
    previous_placement = None
    for team in teams:
        saved = existing_results.get(team["team_number"])
        discipline_points = []
        team_point_map = points_by_team.get(team["team_number"], {})
        total_points = 0
        for discipline in disciplines:
            entry = team_point_map.get(discipline["id"])
            value = entry["points"] if entry else 0
            total_points += value
            discipline_points.append(
                {
                    "discipline_id": discipline["id"],
                    "discipline_name": discipline["name"],
                    "points": value,
                }
            )

        rows.append(
            {
                "team_number": team["team_number"],
                "team_name": team["team_name"],
                "member_count": team["member_count"],
                "placement": saved["placement"] if saved else "",
                "image_path": saved["image_path"] if saved else "",
                "discipline_points": discipline_points,
                "total_points": total_points,
            }
        )
    for row in rows:
        row["is_tie_with_previous"] = previous_placement is not None and row["placement"] == previous_placement
        previous_placement = row["placement"] if row["placement"] else None
    return rows


def build_event_results_public_rows(event_id):
    points_by_team = get_event_result_points_by_team(event_id)
    disciplines = get_event_fixed_disciplines(event_id)
    rows = []
    for row in get_event_results(event_id):
        team_number = row["team_number"]
        discipline_scores = []
        team_total = 0
        team_points = points_by_team.get(team_number, {})
        for discipline in disciplines:
            entry = team_points.get(discipline["id"])
            value = entry["points"] if entry else 0
            team_total += value
            discipline_scores.append(
                {
                    "discipline_name": discipline["name"],
                    "points": value,
                }
            )
        members = get_team_members(event_id, team_number)
        rows.append(
            {
                "team_number": team_number,
                "team_name": get_team_display_name(event_id, team_number),
                "placement": row["placement"],
                "total_points": team_total,
                "image_path": row["image_path"] or "",
                "member_count": len(members),
                "member_names": [member["participant_name"] for member in members],
                "discipline_scores": discipline_scores,
            }
        )
    previous = None
    for row in rows:
        row["is_tie"] = previous is not None and row["placement"] == previous
        previous = row["placement"]
    return rows


def build_public_event_view(event_row):
    if not event_row:
        return None
    data = dict(event_row)
    data["description_html"] = format_event_description(event_row["description"])
    data["results_rendered_html"] = format_event_description(event_row["results_html"])
    data["team_results"] = build_event_results_public_rows(event_row["id"])
    data["podium_results"] = [row for row in data["team_results"] if row["placement"] in (1, 2, 3)]
    data["has_results"] = bool(data["team_results"])
    data["results_pending_announcement"] = (
        event_row["is_closed"] == 1
        and datetime.now() >= parse_dt(event_row["event_at"])
        and not data["has_results"]
    )
    data["team_pairing_mode"] = normalize_event_team_pairing_mode(
        event_row["team_pairing_mode"] if "team_pairing_mode" in event_row.keys() else TEAM_PAIRING_MIXED
    )
    data["fixed_disciplines"] = get_event_fixed_disciplines(event_row["id"])
    data["extra_discipline_options"] = get_event_extra_discipline_options(event_row["id"])
    return data


# --------------------------------------------------
# Team name logic
# --------------------------------------------------
def evaluate_team_name_proposal(proposal_id):
    proposal = query_one("SELECT * FROM team_name_proposals WHERE id = ? ", (proposal_id,))
    if not proposal or proposal["status"] != "pending":
        return

    if proposal["is_admin_override"] == 1:
        if team_name_exists(proposal["event_id"], proposal["team_number"], proposal["proposed_name"]):
            execute(
                "UPDATE team_name_proposals SET status = 'rejected', finalized_at = ? WHERE id = ? ",
                (now_str(), proposal_id),
            )
            return

        execute(
            """
            UPDATE team_name_proposals
            SET status = 'rejected', finalized_at = ? 
            WHERE event_id = ? AND team_number = ? AND status = 'approved'
            """,
            (now_str(), proposal["event_id"], proposal["team_number"]),
        )
        execute(
            "UPDATE team_name_proposals SET status = 'approved', finalized_at = ? WHERE id = ? ",
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
                "UPDATE team_name_proposals SET status = 'rejected', finalized_at = ? WHERE id = ? ",
                (now_str(), proposal_id),
            )
            return

        execute(
            """
            UPDATE team_name_proposals
            SET status = 'rejected', finalized_at = ? 
            WHERE event_id = ? AND team_number = ? AND status = 'approved'
            """,
            (now_str(), proposal["event_id"], proposal["team_number"]),
        )

        execute(
            "UPDATE team_name_proposals SET status = 'approved', finalized_at = ? WHERE id = ? ",
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
            WHERE event_id = ? AND team_number = ? AND status = 'approved'
            """,
            (now_str(), event_id, team_number),
        )
        execute(
            "UPDATE team_name_proposals SET status = 'approved', finalized_at = ? WHERE id = ? ",
            (now_str(), pending["id"]),
        )


# --------------------------------------------------
# Routes - Public
# --------------------------------------------------
@app.route("/film-est-sorsolo", methods=["GET", "POST"])
def movie_night_draw():
    cycle_key = movie_night_cycle_key()
    draw = get_movie_night_draw(cycle_key)

    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        attendance_status = (request.form.get("attendance_status") or MOVIE_NIGHT_STATUS_COMING).strip()
        movie_title = " ".join((request.form.get("movie_title") or "").split())

        if name not in MOVIE_NIGHT_ALLOWED_NAMES:
            flash("Csak Peti, Jakab vagy Martin választható.", "error")
            return redirect(url_for("movie_night_draw"))

        if attendance_status not in (MOVIE_NIGHT_STATUS_COMING, MOVIE_NIGHT_STATUS_NOT_COMING):
            flash("Érvénytelen részvételi állapot.", "error")
            return redirect(url_for("movie_night_draw"))

        if attendance_status == MOVIE_NIGHT_STATUS_COMING and not movie_title:
            flash("Ha tudsz jönni, adj meg egy filmcímet is.", "error")
            return redirect(url_for("movie_night_draw"))

        if attendance_status == MOVIE_NIGHT_STATUS_COMING and len(movie_title) > 140:
            flash("A filmcím legfeljebb 140 karakter lehet.", "error")
            return redirect(url_for("movie_night_draw"))

        if draw:
            flash("Erre a hétre már kisorsoltuk a filmet. Új kör szerdán indul.", "info")
            return redirect(url_for("movie_night_draw"))

        if attendance_status == MOVIE_NIGHT_STATUS_NOT_COMING:
            movie_title = ""
            poster_url = ""
            poster_source = ""
        else:
            poster_url, poster_source = lookup_movie_cover_url(movie_title)

        existing = query_one(
            """
            SELECT id
            FROM movie_night_entries
            WHERE cycle_key = ? AND participant_name = ?
            LIMIT 1
            """,
            (cycle_key, name),
        )
        if existing:
            execute(
                """
                UPDATE movie_night_entries
                SET attendance_status = ?, movie_title = ?, poster_url = ?, poster_source = ?, updated_at = ?
                WHERE id = ?
                """,
                (attendance_status, movie_title, poster_url, poster_source, now_str(), existing["id"]),
            )
            flash(f"{name} válasza frissítve lett.", "success")
        else:
            execute(
                """
                INSERT INTO movie_night_entries (
                    cycle_key, participant_name, attendance_status, movie_title, poster_url, poster_source, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    cycle_key,
                    name,
                    attendance_status,
                    movie_title,
                    poster_url,
                    poster_source,
                    now_str(),
                    now_str(),
                ),
            )
            flash(f"{name} sikeresen rögzítette a válaszát.", "success")

        entries = get_movie_night_entries(cycle_key)
        if len(entries) == len(MOVIE_NIGHT_ALLOWED_NAMES):
            eligible_entries = [
                row
                for row in entries
                if row["attendance_status"] == MOVIE_NIGHT_STATUS_COMING and (row["movie_title"] or "").strip()
            ]
            if eligible_entries:
                selected = random.choice(eligible_entries)
                winner_name = selected["participant_name"]
                winner_movie = selected["movie_title"]
            else:
                winner_name = "Nincs vetítés"
                winner_movie = "Ezen a héten mindhárom fő jelezte, hogy nem tud jönni."
            execute(
                """
                INSERT INTO movie_night_draws (cycle_key, winner_name, winner_movie, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (cycle_key, winner_name, winner_movie, now_str()),
            )
            if eligible_entries:
                flash(f"A heti nyertes film: {winner_movie} ({winner_name}).", "success")
            else:
                flash("Ezen a héten elmarad a vetítés.", "info")

        return redirect(url_for("movie_night_draw"))

    entries = get_movie_night_entries(cycle_key)
    if backfill_movie_night_missing_posters(cycle_key, entries):
        entries = get_movie_night_entries(cycle_key)
    draw = draw or get_movie_night_draw(cycle_key)
    submitted_names = {row["participant_name"] for row in entries}
    missing_names = [name for name in MOVIE_NIGHT_ALLOWED_NAMES if name not in submitted_names]

    return render_template(
        "movie_night_draw.html",
        allowed_names=MOVIE_NIGHT_ALLOWED_NAMES,
        status_coming=MOVIE_NIGHT_STATUS_COMING,
        status_not_coming=MOVIE_NIGHT_STATUS_NOT_COMING,
        avatar_files=MOVIE_NIGHT_AVATAR_FILES,
        entries=entries,
        draw=draw,
        cycle_key=cycle_key,
        next_reset=movie_night_next_reset_label(cycle_key),
        missing_names=missing_names,
    )


@app.route("/")
def home():
    for event_row in query_all("SELECT * FROM events WHERE is_closed = 0"):
        finalize_event_if_needed(event_row)

    events = [build_public_event_view(event) for event in get_all_events()]

    return render_template(
        "events_index.html",
        events=events,
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
    event_view = build_public_event_view(event_row)

    public_teams = build_public_teams(event_row["id"])
    my_status = get_my_status(event_row["id"])

    total_registrations = get_registration_count(event_row["id"])
    active_count = get_active_registration_count(event_row["id"])
    can_register = not event_has_started_or_closed(event_row) and active_count < MAX_PLAYERS

    stage3_pending_count = query_one(
        """
        SELECT COUNT(*) AS cnt
        FROM registrations
        WHERE event_id = ? AND pending_stage = 3
          AND assigned_team IS NULL
          AND is_deleted = 0
          AND payment_status != ? """,
        (event_row["id"], PAYMENT_INVALID),
    )["cnt"]

    stage4_pending_count = query_one(
        """
        SELECT COUNT(*) AS cnt
        FROM registrations
        WHERE event_id = ? AND pending_stage = 4
          AND assigned_team IS NULL
          AND is_deleted = 0
          AND payment_status != ? """,
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

    my_registration_id = my_status["registration"]["id"] if my_status else None
    fixed_waiting_candidates = get_fixed_waiting_candidates(event_row["id"])
    my_extra_vote = get_event_extra_vote(event_row["id"], my_registration_id)
    extra_vote_summary = (
        get_event_extra_vote_summary(event_row["id"])
        if my_extra_vote
        else None
    )
    can_vote_extra = bool(
        my_status
        and not event_has_started_or_closed(event_row)
        and event_view["extra_discipline_options"]
    )
    can_choose_avatar = bool(
        my_status
        and my_status.get("team_number")
        and not event_has_started_or_closed(event_row)
    )
    all_team_avatars = get_all_team_avatars()

    return render_template(
        "home.html",
        event=event_view,
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
        my_extra_vote=my_extra_vote,
        extra_vote_summary=extra_vote_summary,
        can_vote_extra=can_vote_extra,
        can_choose_avatar=can_choose_avatar,
        all_team_avatars=all_team_avatars,
        fixed_waiting_candidates=fixed_waiting_candidates,
        team_pairing_mode=event_view["team_pairing_mode"],
        team_pref_random=TEAM_PREF_RANDOM,
        team_pref_fixed=TEAM_PREF_FIXED,
        team_pairing_mixed=TEAM_PAIRING_MIXED,
        team_pairing_mixed_confirm=TEAM_PAIRING_MIXED_CONFIRM,
        team_pairing_fixed_only=TEAM_PAIRING_FIXED_ONLY,
        team_pairing_random_only=TEAM_PAIRING_RANDOM_ONLY,
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
    team_name_idea = request.form.get("team_name_idea", "").strip()
    team_avatar_idea_id = parse_avatar_id(request.form.get("team_avatar_idea_id"))
    teammate_preference = normalize_registration_teammate_preference(
        request.form.get("teammate_preference", TEAM_PREF_RANDOM)
    )
    fixed_partner_registration_id = request.form.get("fixed_partner_registration_id", type=int)
    fixed_partner_name = request.form.get("fixed_partner_name", "").strip()

    if not name or not email:
        flash("A név és az email megadása kötelező.", "error")
        return redirect(url_for("event_home", slug=slug))

    if "@" not in email:
        flash("Á‰rvénytelen email cím.", "error")
        return redirect(url_for("event_home", slug=slug))

    if teammate_preference == TEAM_PREF_FIXED and not fixed_partner_registration_id and not fixed_partner_name:
        flash("Fix csapattárs módban adj meg egy nevet, vagy válassz a várakozók közül.", "error")
        return redirect(url_for("event_home", slug=slug))

    existing = get_registration_by_email(event_row["id"], email)
    if existing:
        set_session_registration_id(event_row["id"], existing["id"])
        flash("Ezzel az email címmel már jelentkeztél erre az eseményre.", "info")
        return redirect(url_for("event_home", slug=slug))

    try:
        registration_id = register_participant(
            event_row["id"],
            name,
            email,
            provider="email",
            payment_method=payment_method,
            team_name_idea=team_name_idea,
            team_avatar_idea_id=team_avatar_idea_id,
            teammate_preference=teammate_preference,
            fixed_partner_registration_id=fixed_partner_registration_id,
            fixed_partner_name=fixed_partner_name,
        )
        set_session_registration_id(event_row["id"], registration_id)
        flash("Sikeres jelentkezés.", "success")
    except ValueError as e:
        flash(str(e), "error")

    return redirect(url_for("event_home", slug=slug))


@app.route("/e/<slug>/team-preference/random", methods=["POST"])
def switch_to_random_teammate(slug):
    event_row = get_event_by_slug(slug)
    if not event_row:
        flash("Az esemény nem található.", "error")
        return redirect(url_for("home"))

    finalize_event_if_needed(event_row)
    event_row = get_event(event_row["id"])
    if event_has_started_or_closed(event_row):
        flash("A csapatbeállítás már nem módosítható.", "error")
        return redirect(url_for("event_home", slug=slug))

    registration_id = get_session_registration_id(event_row["id"])
    if not registration_id:
        flash("Ehhez előbb jelentkezned kell.", "error")
        return redirect(url_for("event_home", slug=slug))

    reg = get_registration_by_id(registration_id)
    if not reg or reg["event_id"] != event_row["id"] or reg["is_deleted"] == 1:
        flash("A jelentkezésed nem található.", "error")
        return redirect(url_for("event_home", slug=slug))

    if reg["assigned_team"] is None or reg["assigned_stage"] != 2:
        flash("Ez a beállítás most nem módosítható.", "error")
        return redirect(url_for("event_home", slug=slug))

    team_members = get_stage2_team_members(event_row["id"], reg["assigned_team"])
    if len(team_members) != 1:
        flash("A csapat már teljes, ezért ez a beállítás nem módosítható.", "info")
        return redirect(url_for("event_home", slug=slug))

    execute(
        """
        UPDATE registrations
        SET teammate_preference = ? , fixed_partner_registration_id = NULL, fixed_partner_name = NULL,
            fixed_partner_approved_by_admin = 0,
            fixed_partner_payment_status = NULL, fixed_partner_payment_method = NULL, fixed_partner_payment_note = NULL
        WHERE id = ? """,
        (TEAM_PREF_RANDOM, registration_id),
    )
    try_pair_switched_random_registration(event_row["id"], registration_id)
    flash("Átállítottunk véletlenszerű csapattárs módra.", "success")
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

    teammate_preference = normalize_registration_teammate_preference(
        request.args.get("teammate_preference", TEAM_PREF_RANDOM)
    )
    fixed_partner_name = request.args.get("fixed_partner_name", "").strip()
    fixed_partner_registration_id = (request.args.get("fixed_partner_registration_id", "") or "").strip()
    if teammate_preference == TEAM_PREF_FIXED and not fixed_partner_name and not fixed_partner_registration_id:
        flash("Fix csapattárs módban adj meg egy nevet, vagy válassz a várakozók közül.", "error")
        return redirect(url_for("event_home", slug=slug))

    session[f"team_name_idea_{event_row['id']}"] = request.args.get("team_name_idea", "").strip()
    session[f"team_avatar_idea_{event_row['id']}"] = request.args.get("team_avatar_idea_id", "").strip()
    session[f"teammate_preference_{event_row['id']}"] = teammate_preference
    session[f"fixed_partner_registration_id_{event_row['id']}"] = fixed_partner_registration_id
    session[f"fixed_partner_name_{event_row['id']}"] = fixed_partner_name

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
    team_name_idea = session.pop(f"team_name_idea_{event_row['id']}", "")
    team_avatar_idea_id = parse_avatar_id(session.pop(f"team_avatar_idea_{event_row['id']}", ""))
    teammate_preference = normalize_registration_teammate_preference(
        session.pop(f"teammate_preference_{event_row['id']}", TEAM_PREF_RANDOM)
    )
    try:
        fixed_partner_registration_id = int(session.pop(f"fixed_partner_registration_id_{event_row['id']}", "") or 0)
    except (TypeError, ValueError):
        fixed_partner_registration_id = None
    if fixed_partner_registration_id and fixed_partner_registration_id <= 0:
        fixed_partner_registration_id = None
    fixed_partner_name = session.pop(f"fixed_partner_name_{event_row['id']}", "")

    try:
        registration_id = register_participant(
            event_row["id"],
            name,
            email,
            provider="google",
            google_sub=google_sub,
            payment_method=payment_method,
            team_name_idea=team_name_idea,
            team_avatar_idea_id=team_avatar_idea_id,
            teammate_preference=teammate_preference,
            fixed_partner_registration_id=fixed_partner_registration_id,
            fixed_partner_name=fixed_partner_name,
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
    proposed_name = normalize_team_name(request.form.get("team_name", ""))
    try:
        create_or_replace_team_name_proposal(event_row["id"], team_number, registration_id, proposed_name)
    except ValueError as e:
        flash(str(e), "error")
        return redirect(url_for("event_home", slug=slug))
    flash("A csapatnév-javaslat rögzítve lett.", "success")
    return redirect(url_for("event_home", slug=slug))


@app.route("/e/<slug>/team-avatar", methods=["POST"])
def choose_avatar(slug):
    event_row = get_event_by_slug(slug)
    if not event_row:
        flash("Az esemény nem található.", "error")
        return redirect(url_for("home"))

    finalize_event_if_needed(event_row)
    event_row = get_event(event_row["id"])
    if event_has_started_or_closed(event_row):
        flash("Az avatár már nem módosítható.", "error")
        return redirect(url_for("event_home", slug=slug))

    registration_id = get_session_registration_id(event_row["id"])
    if not registration_id:
        flash("Ehhez előbb jelentkezned kell.", "error")
        return redirect(url_for("event_home", slug=slug))

    reg = get_registration_by_id(registration_id)
    if not reg or reg["event_id"] != event_row["id"] or reg["assigned_team"] is None:
        flash("Avatárt csak csapatban lévő játékos választhat.", "error")
        return redirect(url_for("event_home", slug=slug))

    avatar_id = request.form.get("avatar_id", type=int)
    if not avatar_id:
        flash("Válassz egy avatárt.", "error")
        return redirect(url_for("event_home", slug=slug))

    try:
        choose_team_avatar(event_row["id"], reg["assigned_team"], avatar_id, registration_id)
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("event_home", slug=slug))

    flash("A csapat avatárját mentettük.", "success")
    return redirect(url_for("event_home", slug=slug))


@app.route("/e/<slug>/extra-discipline-vote", methods=["POST"])
def vote_extra_discipline(slug):
    event_row = get_event_by_slug(slug)
    if not event_row:
        flash("Az esemény nem található.", "error")
        return redirect(url_for("home"))

    finalize_event_if_needed(event_row)
    event_row = get_event(event_row["id"])
    if event_has_started_or_closed(event_row):
        flash("A szavazás lezárult.", "error")
        return redirect(url_for("event_home", slug=slug))

    registration_id = get_session_registration_id(event_row["id"])
    if not registration_id:
        flash("A szavazáshoz előbb jelentkezz erre az eseményre.", "error")
        return redirect(url_for("event_home", slug=slug))

    reg = get_registration_by_id(registration_id)
    if not reg or reg["event_id"] != event_row["id"] or reg["is_deleted"] == 1:
        flash("A szavazáshoz érvényes jelentkezés szükséges.", "error")
        return redirect(url_for("event_home", slug=slug))

    discipline_id = request.form.get("discipline_id", type=int)
    if not discipline_id:
        flash("Válassz egy extra versenyszámot.", "error")
        return redirect(url_for("event_home", slug=slug))

    option = query_one(
        """
        SELECT ed.id
        FROM event_disciplines ed
        WHERE ed.event_id = ? AND ed.discipline_id = ? AND ed.role = 'extra'
        LIMIT 1
        """,
        (event_row["id"], discipline_id),
    )
    if not option:
        flash("Á‰rvénytelen extra versenyszám.", "error")
        return redirect(url_for("event_home", slug=slug))

    execute(
        """
        INSERT INTO event_extra_votes (event_id, registration_id, discipline_id, created_at)
        VALUES ( ?, ?, ?, ?)
        ON CONFLICT(event_id, registration_id)
        DO UPDATE SET discipline_id = excluded.discipline_id, created_at = excluded.created_at
        """,
        (event_row["id"], registration_id, discipline_id, now_str()),
    )
    flash("Az extra versenyszám szavazatodat rögzítettük.", "success")
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
        flash("Á‰rvénytelen szavazat.", "error")
        return redirect(url_for("event_home", slug=slug))

    proposal = query_one("SELECT * FROM team_name_proposals WHERE id = ? ", (proposal_id,))
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
        VALUES ( ?, ?, ?, ?)
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
        event_view = build_public_event_view(current_event)
        event_cards.append(
            {
                "event": current_event,
                "event_view": event_view,
                "stats": build_event_stats(current_event["id"]),
            }
        )

    return render_template(
        "admin_dashboard.html",
        event_cards=event_cards,
        format_dt_display=format_dt_display,
        payment_label=payment_label,
    )


@app.route("/admin/avatars")
@admin_required
def admin_avatars():
    return render_template(
        "admin_avatars.html",
        team_avatars=get_all_team_avatars(),
    )


@app.route("/admin/avatars/upload", methods=["POST"])
@admin_required
def admin_upload_avatar():
    avatar_file = request.files.get("avatar_file")
    avatar_name = request.form.get("avatar_name", "").strip()

    if not avatar_name:
        avatar_name = f"Egyedi avatar {datetime.now().strftime('%Y-%m-%d %H:%M')}"

    try:
        image_path = save_uploaded_team_avatar_image(avatar_file)
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("admin_dashboard"))

    code = f"user-{uuid.uuid4().hex[:12]}"
    execute(
        """
        INSERT INTO team_avatars (code, name, image_path, created_at)
        VALUES ( ?, ?, ?, ?)
        """,
        (code, avatar_name, image_path, now_str()),
    )
    flash("Az új avatár feltöltve.", "success")
    return redirect(url_for("admin_avatars"))


@app.route("/admin/avatars/<int:avatar_id>/delete", methods=["POST"])
@admin_required
def admin_delete_avatar(avatar_id):
    avatar = query_one("SELECT id, image_path FROM team_avatars WHERE id = ? ", (avatar_id,))
    if not avatar:
        flash("Az avatár nem található.", "error")
        return redirect(url_for("admin_avatars"))

    execute(
        "UPDATE registrations SET pending_team_avatar_idea = NULL WHERE pending_team_avatar_idea = ? ",
        (avatar_id,),
    )
    execute("DELETE FROM team_avatar_selections WHERE avatar_id = ? ", (avatar_id,))
    execute("DELETE FROM team_avatars WHERE id = ? ", (avatar_id,))

    image_path = (avatar["image_path"] or "").strip()
    prefixes = [
        "/static/team_avatars/custom/",
        f"/static/{PERSIST_SUBDIR}/team_avatars/custom/",
    ]
    for prefix in prefixes:
        if not image_path.startswith(prefix):
            continue
        filename = image_path[len(prefix) :]
        candidate_paths = [
            os.path.join(TEAM_AVATAR_UPLOAD_DIR, filename),
            os.path.join(BASE_DIR, "static", "team_avatars", "custom", filename),
        ]
        for abs_path in candidate_paths:
            if os.path.isfile(abs_path):
                try:
                    os.remove(abs_path)
                except OSError:
                    pass
        break

    flash("Az avatár törölve.", "success")
    return redirect(url_for("admin_avatars"))


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


@app.route("/admin/events/<int:event_id>/results", methods=["GET", "POST"])
@admin_required
def admin_event_results(event_id):
    event_row = get_event(event_id)
    if not event_row:
        flash("Az esemény nem található.", "error")
        return redirect(url_for("admin_dashboard"))

    finalize_event_if_needed(event_row)
    event_row = get_event(event_id)

    if event_row["is_closed"] == 0:
        flash("Eredményhirdetést csak lezárt eseménynél lehet rögzíteni.", "error")
        return redirect(url_for("admin_dashboard"))

    if datetime.now() < parse_dt(event_row["event_at"]):
        flash("Eredményhirdetés csak az esemény kezdési időpontja után érhető el.", "error")
        return redirect(url_for("admin_dashboard"))

    if request.method == "GET":
        return render_template(
            "admin_event_results.html",
            event=event_row,
            result_rows=build_event_results_editor_rows(event_id),
            disciplines=get_event_fixed_disciplines(event_id),
            format_dt_display=format_dt_display,
        )

    disciplines = get_event_fixed_disciplines(event_id)
    if not disciplines:
        flash("Nincs fix versenyszám, ezért nem lehet eredményt rögzíteni.", "error")
        return redirect(url_for("admin_event_results", event_id=event_id))

    team_numbers_raw = request.form.getlist("result_team_number")
    existing_images_raw = request.form.getlist("result_existing_image_path")

    if not team_numbers_raw:
        flash("Nincs csapat, amelyhez eredményt lehetne rögzíteni.", "error")
        return redirect(url_for("admin_event_results", event_id=event_id))
    if len(team_numbers_raw) != len(existing_images_raw):
        flash("Hibás eredmény űrlap adatok érkeztek.", "error")
        return redirect(url_for("admin_event_results", event_id=event_id))

    rows_for_ranking = []

    for idx, team_value in enumerate(team_numbers_raw):
        team_number = team_value
        existing_image_path = (existing_images_raw[idx] if idx < len(existing_images_raw) else "").strip()

        try:
            team_number_int = int(team_number)
        except (TypeError, ValueError):
            flash("Á‰rvénytelen csapat az eredménylistában.", "error")
            return redirect(url_for("admin_event_results", event_id=event_id))

        if len(get_team_members(event_id, team_number_int)) == 0:
            flash(f"A(z) {team_number_int}. csapatnak nincs aktív tagja.", "error")
            return redirect(url_for("admin_event_results", event_id=event_id))

        image_path = existing_image_path
        delete_image_requested = request.form.get(f"result_delete_image_{team_number_int}") == "1"
        uploaded_image = request.files.get(f"result_image_{team_number_int}")
        if uploaded_image and getattr(uploaded_image, "filename", ""):
            try:
                new_image_path = save_uploaded_event_result_image(uploaded_image)
            except ValueError as exc:
                flash(str(exc), "error")
                return redirect(url_for("admin_event_results", event_id=event_id))
            if existing_image_path and existing_image_path != new_image_path:
                delete_event_result_image_file(existing_image_path)
            image_path = new_image_path
        elif delete_image_requested and existing_image_path:
            delete_event_result_image_file(existing_image_path)
            image_path = ""

        discipline_points = []
        total_points = 0
        for discipline in disciplines:
            raw_points = (request.form.get(f"points_{team_number_int}_{discipline['id']}") or "").strip()
            if raw_points == "":
                flash(f"A(z) {get_team_display_name(event_id, team_number_int)} csapatnál minden versenyszám pontja kötelező.", "error")
                return redirect(url_for("admin_event_results", event_id=event_id))
            try:
                points = int(raw_points)
            except ValueError:
                flash(f"A(z) {discipline['name']} pontszáma csak egész szám lehet.", "error")
                return redirect(url_for("admin_event_results", event_id=event_id))
            if points < 0:
                flash("A pontszám nem lehet negatív.", "error")
                return redirect(url_for("admin_event_results", event_id=event_id))
            total_points += points
            discipline_points.append(
                {
                    "discipline_id": discipline["id"],
                    "points": points,
                }
            )

        rows_for_ranking.append(
            {
                "team_number": team_number_int,
                "total_points": total_points,
                "discipline_points": discipline_points,
                "image_path": image_path,
            }
        )

    rows_for_ranking.sort(key=lambda row: (-row["total_points"], row["team_number"]))
    previous_total = None
    previous_placement = None
    for index, row in enumerate(rows_for_ranking, start=1):
        if previous_total is not None and row["total_points"] == previous_total:
            row["placement"] = previous_placement
        else:
            row["placement"] = index
            previous_placement = index
        previous_total = row["total_points"]

    execute("DELETE FROM event_results WHERE event_id = ? ", (event_id,))
    execute("DELETE FROM event_result_points WHERE event_id = ? ", (event_id,))

    for row in rows_for_ranking:
        execute(
            """
            INSERT INTO event_results (
                event_id, team_number, placement, points, note, image_path, created_at, updated_at
            )
            VALUES ( ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                row["team_number"],
                row["placement"],
                str(row["total_points"]),
                "",
                row["image_path"],
                now_str(),
                now_str(),
            ),
        )
        for point_row in row["discipline_points"]:
            execute(
                """
                INSERT INTO event_result_points (
                    event_id, team_number, discipline_id, points, created_at, updated_at
                )
                VALUES ( ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    row["team_number"],
                    point_row["discipline_id"],
                    point_row["points"],
                    now_str(),
                    now_str(),
                ),
            )

    execute(
        "UPDATE events SET results_published_at = COALESCE(results_published_at, ?) WHERE id = ? ",
        (now_str(), event_id),
    )

    flash("Az eredményhirdetés mentve. A dobogósok kiemelve látszanak a publikus oldalon.", "success")
    return redirect(url_for("admin_event_results", event_id=event_id))

@app.route("/admin/events/new", methods=["GET", "POST"])
@admin_required
def admin_new_event():
    if request.method == "POST":
        title = request.form.get("title", "").strip() or app.config["EVENT_TITLE_DEFAULT"]
        description = sanitize_event_description_html(request.form.get("description", ""))
        event_date = request.form.get("event_date", "").strip()
        event_time = request.form.get("event_time", "").strip()
        has_fee = 1 if request.form.get("has_fee") == "on" else 0
        fee_amount = request.form.get("fee_amount", type=int) or 0
        beneficiary_name = request.form.get("beneficiary_name", "").strip()
        bank_account = request.form.get("bank_account", "").strip()
        team_pairing_mode = normalize_event_team_pairing_mode(request.form.get("team_pairing_mode"))

        if not event_date or not event_time:
            flash("Az esemény dátuma és időpontja kötelező.", "error")
            return redirect(url_for("admin_new_event"))

        try:
            event_at = parse_dt_input(event_date, event_time)
        except ValueError:
            flash("Á‰rvénytelen dátum vagy idő.", "error")
            return redirect(url_for("admin_new_event"))

        try:
            apply_discipline_updates_from_form()
        except ValueError as exc:
            flash(str(exc), "error")
            return redirect(url_for("admin_new_event"))

        try:
            fixed_ids, extra_ids = resolve_event_discipline_selection()
        except ValueError as exc:
            flash(str(exc), "error")
            return redirect(url_for("admin_new_event"))

        deadline = compute_deadline(event_at)
        slug = generate_unique_slug(title, event_at.strftime("%Y-%m-%d"))

        cur = execute(
            """
            INSERT INTO events (
                title, slug, description, event_at, registration_deadline, created_at, is_closed,
                has_fee, fee_amount, beneficiary_name, bank_account, team_pairing_mode
            )
            VALUES ( ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?)
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
                team_pairing_mode,
            ),
        )
        save_event_discipline_links(cur.lastrowid, fixed_ids, extra_ids)
        flash("Az esemény létrejött.", "success")
        return redirect(url_for("admin_event_dashboard", event_id=cur.lastrowid))
    return render_template("admin_event_form.html", **build_admin_event_form_context())


@app.route("/admin/events/<int:event_id>/edit", methods=["GET", "POST"])
@admin_required
def admin_edit_event(event_id):
    event_row = get_event(event_id)
    if not event_row:
        flash("Az esemény nem található.", "error")
        return redirect(url_for("admin_dashboard"))

    if request.method == "POST":
        title = request.form.get("title", "").strip() or app.config["EVENT_TITLE_DEFAULT"]
        description = sanitize_event_description_html(request.form.get("description", ""))
        event_date = request.form.get("event_date", "").strip()
        event_time = request.form.get("event_time", "").strip()
        has_fee = 1 if request.form.get("has_fee") == "on" else 0
        fee_amount = request.form.get("fee_amount", type=int) or 0
        beneficiary_name = request.form.get("beneficiary_name", "").strip()
        bank_account = request.form.get("bank_account", "").strip()
        team_pairing_mode = normalize_event_team_pairing_mode(request.form.get("team_pairing_mode"))

        try:
            event_at = parse_dt_input(event_date, event_time)
        except ValueError:
            flash("Á‰rvénytelen dátum vagy idő.", "error")
            return redirect(url_for("admin_edit_event", event_id=event_id))

        try:
            apply_discipline_updates_from_form()
        except ValueError as exc:
            flash(str(exc), "error")
            return redirect(url_for("admin_edit_event", event_id=event_id))

        try:
            fixed_ids, extra_ids = resolve_event_discipline_selection()
        except ValueError as exc:
            flash(str(exc), "error")
            return redirect(url_for("admin_edit_event", event_id=event_id))

        deadline = compute_deadline(event_at)
        slug = event_row["slug"] or generate_unique_slug(title, event_at.strftime("%Y-%m-%d"))

        execute(
            """
            UPDATE events
            SET title = ? , slug = ? , description = ? , event_at = ? , registration_deadline = ? ,
                has_fee = ? , fee_amount = ? , beneficiary_name = ? , bank_account = ? , team_pairing_mode = ? 
            WHERE id = ? """,
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
                team_pairing_mode,
                event_id,
            ),
        )
        save_event_discipline_links(event_id, fixed_ids, extra_ids)
        flash("Az esemény frissítve.", "success")
        return redirect(url_for("admin_event_dashboard", event_id=event_id))
    return render_template("admin_event_form.html", **build_admin_event_form_context(event_row))


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
            WHERE event_id = ? AND payment_status = ? AND is_deleted = 0
            """,
            (PAYMENT_INVALID, event_id, PAYMENT_PENDING),
        )
        remove_invalid_from_teams(event_id)

    finalize_team_names_for_event(event_id)

    execute(
        """
        UPDATE events
        SET is_closed = 1, finalized_at = ? 
        WHERE id = ? """,
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
        members = get_team_members_with_approved_virtual_partner(event_id, team_number)
        teams.append(
            {
                "team_number": team_number,
                "team_name": get_approved_team_name(event_id, team_number) or "",
                "team_name_state": build_team_name_state(event_id, team_number),
                "team_avatar": get_team_avatar_selection(event_id, team_number),
                "members": [dict(member) for member in members],
            }
        )

    unassigned = get_unassigned_members(event_id)
    return render_template(
        "admin_teams.html",
        event=event_row,
        teams=teams,
        unassigned=unassigned,
        max_teams=MAX_TEAMS,
        all_team_avatars=get_all_team_avatars(),
        format_dt_display=format_dt_display,
        payment_label=payment_label,
        payment_method_label=payment_method_label,
    )


@app.route("/admin/team/<int:event_id>/<int:team_number>/avatar", methods=["POST"])
@admin_required
def admin_set_team_avatar(event_id, team_number):
    event_row = get_event(event_id)
    if not event_row:
        flash("Az esemény nem található.", "error")
        return redirect(url_for("admin_dashboard"))

    if team_number < 1 or team_number > MAX_TEAMS:
        flash("Érvénytelen csapat.", "error")
        return redirect(url_for("admin_teams", event_id=event_id))

    avatar_id = parse_avatar_id(request.form.get("avatar_id"))
    if not avatar_id or not avatar_exists(avatar_id):
        flash("Válassz érvényes avatárt.", "error")
        return redirect(url_for("admin_teams", event_id=event_id))

    real_members = get_team_members(event_id, team_number)
    if not real_members:
        flash("Üres csapathoz még nem állítható avatár.", "error")
        return redirect(url_for("admin_teams", event_id=event_id))

    selector_registration_id = real_members[0]["id"]
    try:
        choose_team_avatar(event_id, team_number, avatar_id, selector_registration_id)
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("admin_teams", event_id=event_id))

    flash(f"A {team_number}. csapat avatárja frissítve.", "success")
    return redirect(url_for("admin_teams", event_id=event_id))


@app.route("/admin/events/<int:event_id>/players/add", methods=["POST"])
@admin_required
def admin_add_player(event_id):
    event_row = get_event(event_id)
    if not event_row:
        flash("Az esemény nem található.", "error")
        return redirect(url_for("admin_dashboard"))

    finalize_event_if_needed(event_row)
    event_row = get_event(event_id)
    if event_has_started_or_closed(event_row):
        flash("Lezárt eseményhez már nem adhatsz hozzá új játékost.", "error")
        return redirect(url_for("admin_teams", event_id=event_id))

    name = (request.form.get("participant_name") or "").strip()
    email = (request.form.get("participant_email") or "").strip()
    team_number = request.form.get("team_number", type=int)
    payment_method = request.form.get("payment_method", PAYMENT_METHOD_CASH)

    if not name:
        flash("Adj meg játékosnevet.", "error")
        return redirect(url_for("admin_teams", event_id=event_id))
    if not email or "@" not in email:
        flash("Adj meg érvényes email címet.", "error")
        return redirect(url_for("admin_teams", event_id=event_id))
    if team_number is None or team_number < 1 or team_number > MAX_TEAMS:
        flash("Válassz érvényes csapatot.", "error")
        return redirect(url_for("admin_teams", event_id=event_id))
    if get_registration_by_email(event_id, email):
        flash("Erre az email címre már van jelentkezés ezen az eseményen.", "error")
        return redirect(url_for("admin_teams", event_id=event_id))
    if event_row["has_fee"] == 1 and payment_method not in (PAYMENT_METHOD_TRANSFER, PAYMENT_METHOD_CASH):
        flash("Fizetős eseménynél a fizetési mód csak utalás vagy készpénz lehet.", "error")
        return redirect(url_for("admin_teams", event_id=event_id))

    try:
        registration_id = register_participant(
            event_id,
            name,
            email,
            provider="manual",
            payment_method=payment_method if event_row["has_fee"] == 1 else PAYMENT_METHOD_NONE,
        )
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("admin_teams", event_id=event_id))

    reg = get_registration_by_id(registration_id)
    if not reg:
        flash("A játékost nem sikerült létrehozni.", "error")
        return redirect(url_for("admin_teams", event_id=event_id))

    if get_current_team_size(event_id, team_number) >= MAX_TEAM_SIZE and reg["assigned_team"] != team_number:
        execute("UPDATE registrations SET is_manual = 1 WHERE id = ? ", (registration_id,))
        flash("A játékos hozzáadva, de a kiválasztott csapat tele van (max 4 fő).", "info")
        return redirect(url_for("admin_teams", event_id=event_id))

    slot = get_current_team_size(event_id, team_number) + 1
    execute(
        """
        UPDATE registrations
        SET assigned_team = ? , assigned_stage = ? , assigned_slot = ? ,
            pending_stage = NULL, pending_position = NULL,
            removed_from_team = 0, is_manual = 1
        WHERE id = ? """,
        (team_number, max(2, slot), slot, registration_id),
    )
    activate_stored_team_name_idea(registration_id)
    activate_stored_team_avatar_idea(registration_id)
    flash("Ášj játékos hozzáadva a csapathoz.", "success")
    return redirect(url_for("admin_teams", event_id=event_id))


@app.route("/admin/player/<int:registration_id>/remove-from-team", methods=["POST"])
@admin_required
def admin_remove_from_team(registration_id):
    reg = get_registration_by_id(registration_id)
    if not reg:
        flash("A játékos nem található.", "error")
        return redirect(url_for("admin_dashboard"))

    detach_fixed_partner_links(registration_id)

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
        WHERE id = ? """,
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

    detach_fixed_partner_links(registration_id)

    execute(
        """
        UPDATE registrations
        SET is_deleted = 1,
            assigned_team = NULL,
            assigned_stage = NULL,
            assigned_slot = NULL,
            pending_stage = NULL,
            pending_position = NULL
        WHERE id = ? """,
        (registration_id,),
    )
    flash("A jelentkező végleg törölve.", "success")
    return redirect(url_for("admin_teams", event_id=reg["event_id"]))


@app.route("/admin/player/<int:registration_id>/approve-fixed-partner", methods=["POST"])
@admin_required
def admin_approve_fixed_partner(registration_id):
    reg = get_registration_by_id(registration_id)
    if not reg:
        flash("A játékos nem található.", "error")
        return redirect(url_for("admin_dashboard"))

    if reg["assigned_team"] is None or reg["assigned_stage"] != 2:
        flash("Ez a játékos jelenleg nem fix csapattárs-várakozó állapotban van.", "error")
        return redirect(url_for("admin_teams", event_id=reg["event_id"]))

    if reg["teammate_preference"] != TEAM_PREF_FIXED:
        flash("A játékos nem fix csapattárs módot választott.", "error")
        return redirect(url_for("admin_teams", event_id=reg["event_id"]))

    if reg["fixed_partner_registration_id"]:
        flash("A fix csapattárs már párosítva lett.", "info")
        return redirect(url_for("admin_teams", event_id=reg["event_id"]))

    if not (reg["fixed_partner_name"] or "").strip():
        flash("Nincs megadott fix csapattárs név ehhez a jóváhagyáshoz.", "error")
        return redirect(url_for("admin_teams", event_id=reg["event_id"]))

    team_members = get_stage2_team_members(reg["event_id"], reg["assigned_team"])
    if len(team_members) != 1 or team_members[0]["id"] != registration_id:
        flash("A csapat állapota időközben megváltozott, nem jóváhagyható.", "error")
        return redirect(url_for("admin_teams", event_id=reg["event_id"]))

    execute(
        "UPDATE registrations SET fixed_partner_approved_by_admin = 1 WHERE id = ? ",
        (registration_id,),
    )
    flash("A fix csapattárs szervezői jóváhagyása mentve.", "success")
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
        flash("Á‰rvénytelen csapat.", "error")
        return redirect(url_for("admin_teams", event_id=event_id))

    if reg["payment_status"] == PAYMENT_INVALID:
        flash("Á‰rvénytelen státuszú játékos nem tehető aktív csapatba.", "error")
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
        SET assigned_team = ? , assigned_stage = ? , assigned_slot = ? ,
            pending_stage = NULL, pending_position = NULL,
            removed_from_team = 0, is_manual = 1
        WHERE id = ? """,
        (team_number, max(2, slot), slot, registration_id),
    )
    activate_stored_team_name_idea(registration_id)
    activate_stored_team_avatar_idea(registration_id)
    flash("A játékos áthelyezve.", "success")
    return redirect(url_for("admin_teams", event_id=event_id))


@app.route("/admin/player/<int:registration_id>/virtual-partner/payment", methods=["POST"])
@admin_required
def admin_update_virtual_partner_payment(registration_id):
    reg = get_registration_by_id(registration_id)
    if not reg:
        flash("A játékos nem található.", "error")
        return redirect(url_for("admin_dashboard"))

    if reg["teammate_preference"] != TEAM_PREF_FIXED or reg["fixed_partner_registration_id"]:
        flash("Ehhez a jelentkezőhöz nem tartozik kézzel megadott fix csapattárs.", "error")
        return redirect(url_for("admin_teams", event_id=reg["event_id"]))

    if not (reg["fixed_partner_name"] or "").strip():
        flash("A fix csapattárs neve hiányzik.", "error")
        return redirect(url_for("admin_teams", event_id=reg["event_id"]))

    payment_status = request.form.get("payment_status", PAYMENT_PENDING)
    payment_method = request.form.get("payment_method", PAYMENT_METHOD_NONE)
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
        SET fixed_partner_payment_status = ? ,
            fixed_partner_payment_method = ? ,
            fixed_partner_payment_note = ? ,
            is_manual = 1
        WHERE id = ? """,
        (payment_status, payment_method, payment_note, registration_id),
    )
    flash("A fix csapattárs fizetési státusza frissítve.", "success")
    return redirect(url_for("admin_teams", event_id=reg["event_id"]))


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
        flash("Á‰rvénytelen fizetési státusz.", "error")
        return redirect(url_for("admin_teams", event_id=reg["event_id"]))

    if payment_method not in (PAYMENT_METHOD_TRANSFER, PAYMENT_METHOD_CASH, PAYMENT_METHOD_NONE):
        flash("Á‰rvénytelen fizetési mód.", "error")
        return redirect(url_for("admin_teams", event_id=reg["event_id"]))

    execute(
        """
        UPDATE registrations
        SET payment_status = ? , payment_method = ? , payment_note = ? , is_manual = 1
        WHERE id = ? """,
        (payment_status, payment_method, payment_note, registration_id),
    )

    if payment_status == PAYMENT_INVALID:
        detach_fixed_partner_links(registration_id)
        execute(
            """
            UPDATE registrations
            SET assigned_team = NULL, assigned_stage = NULL, assigned_slot = NULL,
                pending_stage = NULL, pending_position = NULL, removed_from_team = 1
            WHERE id = ? """,
            (registration_id,),
        )

    flash("Fizetési státusz frissítve.", "success")
    return redirect(url_for("admin_teams", event_id=reg["event_id"]))


@app.route("/admin/team/<int:event_id>/<int:team_number>/approve-name", methods=["POST"])
@admin_required
def admin_approve_team_name(event_id, team_number):
    event_row = get_event(event_id)
    if not event_row:
        flash("Az esemény nem található.", "error")
        return redirect(url_for("admin_dashboard"))

    pending = get_pending_team_name_proposal(event_id, team_number)
    if not pending:
        flash("Ehhez a csapathoz nincs függő csapatnév-javaslat.", "info")
        return redirect(url_for("admin_teams", event_id=event_id))

    if team_name_exists(event_id, team_number, pending["proposed_name"]):
        execute(
            "UPDATE team_name_proposals SET status = 'rejected', finalized_at = ? WHERE id = ? ",
            (now_str(), pending["id"]),
        )
        flash("A csapatnév nem hagyható jóvá, mert már létezik ilyen név.", "error")
        return redirect(url_for("admin_teams", event_id=event_id))

    execute(
        """
        UPDATE team_name_proposals
        SET status = 'rejected', finalized_at = ?
        WHERE event_id = ? AND team_number = ? AND status = 'approved'
        """,
        (now_str(), event_id, team_number),
    )
    execute(
        "UPDATE team_name_proposals SET status = 'approved', finalized_at = ? WHERE id = ? ",
        (now_str(), pending["id"]),
    )
    flash("A csapatnév szervezői jóváhagyással véglegesítve.", "success")
    return redirect(url_for("admin_teams", event_id=event_id))


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
        WHERE event_id = ? ORDER BY id ASC LIMIT 1
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
        VALUES ( ?, ?, ?, ?, 'pending', ?, 1)
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

    disciplines = get_event_fixed_disciplines(event_id)

    teams = []
    for team_number in range(1, MAX_TEAMS + 1):
        members = get_team_members_with_approved_virtual_partner(event_id, team_number)
        if len(members) >= 2:
            visible_proposal = get_visible_team_name_proposal(event_id, team_number)
            team_name = (
                normalize_team_name(visible_proposal["proposed_name"])
                if visible_proposal
                else f"Csapat {team_number}"
            )
            teams.append(
                {
                    "team_number": team_number,
                    "team_name": team_name,
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
        disciplines=disciplines,
        format_dt_display=format_dt_display,
    )


# --------------------------------------------------
# Startup
# --------------------------------------------------
if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)
