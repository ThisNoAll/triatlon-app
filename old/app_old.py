
import os
import random
from datetime import datetime, timedelta
from functools import wraps

from flask import (
    Flask,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_sqlalchemy import SQLAlchemy
from authlib.integrations.flask_client import OAuth
from sqlalchemy import func

db = SQLAlchemy()
oauth = OAuth()


def create_app():
    app = Flask(__name__)
    app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "dev-secret-change-me")

    database_url = os.getenv("DATABASE_URL", "sqlite:///app.db")
    if database_url.startswith("postgres://"):
        database_url = database_url.replace("postgres://", "postgresql://", 1)
    app.config["SQLALCHEMY_DATABASE_URI"] = database_url
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

    db.init_app(app)
    oauth.init_app(app)

    google_client_id = os.getenv("GOOGLE_CLIENT_ID")
    google_client_secret = os.getenv("GOOGLE_CLIENT_SECRET")
    if google_client_id and google_client_secret:
        oauth.register(
            name="google",
            client_id=google_client_id,
            client_secret=google_client_secret,
            server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
            client_kwargs={"scope": "openid email profile"},
        )

    with app.app_context():
        db.create_all()

    def admin_required(view_func):
        @wraps(view_func)
        def wrapped(*args, **kwargs):
            if not session.get("is_admin"):
                flash("Előbb lépj be szervezőként.", "warning")
                return redirect(url_for("admin_login"))
            return view_func(*args, **kwargs)
        return wrapped

    @app.context_processor
    def inject_helpers():
        return {
            "format_team_name": format_team_name,
            "team_members": team_members,
            "visible_team_counts": visible_team_counts,
            "pending_round_count": pending_round_count,
            "round_label": round_label,
            "now": datetime.now,
        }

    @app.route("/")
    def home():
        event = Event.query.order_by(Event.start_at.asc()).first()
        if event:
            return redirect(url_for("event_page", slug=event.slug))
        return render_template("index.html")

    @app.route("/auth/google")
    def google_login():
        if "google" not in oauth._registry:
            flash("A Google bejelentkezés még nincs beállítva ezen a példányon.", "warning")
            return redirect(request.referrer or url_for("home"))
        redirect_uri = url_for("google_callback", _external=True)
        return oauth.google.authorize_redirect(redirect_uri)

    @app.route("/auth/google/callback")
    def google_callback():
        if "google" not in oauth._registry:
            flash("A Google bejelentkezés nincs beállítva.", "danger")
            return redirect(url_for("home"))
        token = oauth.google.authorize_access_token()
        user_info = token.get("userinfo") or oauth.google.userinfo(token=token)
        session["google_user"] = {
            "name": user_info.get("name") or "",
            "email": user_info.get("email") or "",
        }
        flash("Sikeres Google bejelentkezés.", "success")
        next_url = session.pop("next_after_google", None)
        return redirect(next_url or url_for("home"))

    @app.route("/auth/logout")
    def logout():
        session.pop("google_user", None)
        session.pop("is_admin", None)
        flash("Kiléptél.", "info")
        return redirect(url_for("home"))

    @app.route("/admin/login", methods=["GET", "POST"])
    def admin_login():
        if request.method == "POST":
            password = request.form.get("password", "")
            admin_password = os.getenv("ADMIN_PASSWORD", "admin123")
            if password == admin_password:
                session["is_admin"] = True
                flash("Sikeres szervezői belépés.", "success")
                return redirect(url_for("admin_events"))
            flash("Hibás jelszó.", "danger")
        return render_template("admin_login.html")

    @app.route("/admin/logout")
    def admin_logout():
        session.pop("is_admin", None)
        flash("Kiléptél a szervezői felületről.", "info")
        return redirect(url_for("home"))

    @app.route("/admin/events")
    @admin_required
    def admin_events():
        events = Event.query.order_by(Event.start_at.asc()).all()
        for event in events:
            finalize_pending_if_due(event)
        db.session.commit()
        return render_template("admin_events.html", events=events)

    @app.route("/admin/events/new", methods=["GET", "POST"])
    @admin_required
    def admin_new_event():
        if request.method == "POST":
            title = request.form.get("title", "").strip()
            start_at_raw = request.form.get("start_at", "").strip()
            if not title or not start_at_raw:
                flash("Az eseménynév és az időpont kötelező.", "danger")
                return render_template("admin_new_event.html")
            try:
                start_at = datetime.fromisoformat(start_at_raw)
            except ValueError:
                flash("Hibás dátumformátum.", "danger")
                return render_template("admin_new_event.html")
            slug = unique_slug(title)
            event = Event(
                title=title,
                slug=slug,
                start_at=start_at,
                registration_deadline=start_at - timedelta(hours=6),
            )
            db.session.add(event)
            db.session.flush()
            for number in range(1, 6):
                db.session.add(Team(event_id=event.id, number=number))
            db.session.commit()
            flash("Esemény létrehozva.", "success")
            return redirect(url_for("admin_event", event_id=event.id))
        return render_template("admin_new_event.html")

    @app.route("/admin/events/<int:event_id>", methods=["GET", "POST"])
    @admin_required
    def admin_event(event_id):
        event = Event.query.get_or_404(event_id)
        finalize_pending_if_due(event)
        if request.method == "POST":
            for team in event.teams:
                field = f"team_name_{team.id}"
                team.name = request.form.get(field, "").strip() or None
            db.session.commit()
            flash("Csapatnevek mentve.", "success")
            return redirect(url_for("admin_event", event_id=event.id))
        db.session.commit()
        return render_template("admin_event.html", event=event)

    @app.route("/admin/events/<int:event_id>/print")
    @admin_required
    def admin_print(event_id):
        event = Event.query.get_or_404(event_id)
        finalize_pending_if_due(event)
        db.session.commit()
        return render_template("print.html", event=event, matches=round_robin_matches())

    @app.route("/events/<slug>")
    def event_page(slug):
        event = Event.query.filter_by(slug=slug).first_or_404()
        finalize_pending_if_due(event)
        db.session.commit()
        google_user = session.get("google_user")
        return render_template("event.html", event=event, google_user=google_user)

    @app.post("/events/<slug>/register")
    def register(slug):
        event = Event.query.filter_by(slug=slug).first_or_404()
        finalize_pending_if_due(event)
        if datetime.now() >= event.registration_deadline:
            db.session.commit()
            flash("A jelentkezés lezárult.", "warning")
            return redirect(url_for("event_page", slug=slug))

        google_user = session.get("google_user")
        use_google = request.form.get("use_google") == "1"

        if use_google and not google_user:
            session["next_after_google"] = url_for("event_page", slug=slug)
            return redirect(url_for("google_login"))

        if use_google and google_user:
            name = google_user.get("name", "").strip()
            email = google_user.get("email", "").strip().lower()
            provider = "google"
        else:
            name = request.form.get("name", "").strip()
            email = request.form.get("email", "").strip().lower()
            provider = "email"

        team_name_suggestion = request.form.get("team_name_suggestion", "").strip() or None

        if not name or not email:
            flash("A név és email kötelező.", "danger")
            return redirect(url_for("event_page", slug=slug))

        if not valid_email(email):
            flash("Adj meg érvényes email címet.", "danger")
            return redirect(url_for("event_page", slug=slug))

        total_registrations = Registration.query.filter_by(event_id=event.id).count()
        if total_registrations >= 20:
            flash("Betelt a verseny, már 20 játékos van.", "warning")
            return redirect(url_for("event_page", slug=slug))

        existing = Registration.query.filter(
            func.lower(Registration.email) == email,
            Registration.event_id == event.id,
        ).first()
        if existing:
            flash("Ezzel az email címmel már jelentkeztek erre az eseményre.", "warning")
            return redirect(url_for("event_page", slug=slug))

        registration = Registration(
            event_id=event.id,
            name=name,
            email=email,
            provider=provider,
            team_name_suggestion=team_name_suggestion,
        )
        db.session.add(registration)
        db.session.flush()
        assign_registration(event, registration)
        db.session.commit()
        flash("Sikeres jelentkezés.", "success")
        return redirect(url_for("event_page", slug=slug))

    return app


class Event(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(150), nullable=False)
    slug = db.Column(db.String(180), unique=True, nullable=False)
    start_at = db.Column(db.DateTime, nullable=False)
    registration_deadline = db.Column(db.DateTime, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.now, nullable=False)

    teams = db.relationship("Team", backref="event", lazy=True, order_by="Team.number")
    registrations = db.relationship("Registration", backref="event", lazy=True, order_by="Registration.created_at")


class Team(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    event_id = db.Column(db.Integer, db.ForeignKey("event.id"), nullable=False)
    number = db.Column(db.Integer, nullable=False)
    name = db.Column(db.String(80))
    created_at = db.Column(db.DateTime, default=datetime.now, nullable=False)

    registrations = db.relationship("Registration", backref="team", lazy=True, order_by="Registration.created_at")


class Registration(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    event_id = db.Column(db.Integer, db.ForeignKey("event.id"), nullable=False)
    team_id = db.Column(db.Integer, db.ForeignKey("team.id"))
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(190), nullable=False)
    provider = db.Column(db.String(20), nullable=False, default="email")
    team_name_suggestion = db.Column(db.String(80))
    level = db.Column(db.Integer)  # 2, 3, 4 when assigned
    pending_level = db.Column(db.Integer)  # 3 or 4 when waiting in pool
    created_at = db.Column(db.DateTime, default=datetime.now, nullable=False)


def valid_email(value: str) -> bool:
    return "@" in value and "." in value.split("@")[-1]


def unique_slug(title: str) -> str:
    base = "".join(c.lower() if c.isalnum() else "-" for c in title).strip("-")
    base = "-".join([chunk for chunk in base.split("-") if chunk]) or "esemeny"
    slug = base
    counter = 2
    while Event.query.filter_by(slug=slug).first():
        slug = f"{base}-{counter}"
        counter += 1
    return slug


def format_team_name(team: Team) -> str:
    return team.name or f"Csapat {team.number}"


def team_members(team: Team):
    return [r for r in team.registrations if r.team_id == team.id and r.level is not None]


def visible_team_counts(event: Event):
    counts = {}
    for team in event.teams:
        counts[team.id] = len(team_members(team))
    return counts


def pending_round_count(event: Event, level: int) -> int:
    return Registration.query.filter_by(event_id=event.id, pending_level=level).count()


def round_label(level: int) -> str:
    return "3. fős bővítés" if level == 3 else "4. fős bővítés"


def assign_registration(event: Event, registration: Registration):
    assigned_count = Registration.query.filter(
        Registration.event_id == event.id,
        Registration.team_id.isnot(None),
    ).count()

    if assigned_count < 10:
        team_number = (assigned_count // 2) + 1
        team = Team.query.filter_by(event_id=event.id, number=team_number).first()
        registration.team_id = team.id
        registration.level = 2
        registration.pending_level = None
        maybe_apply_team_name(team, registration.team_name_suggestion)
        return

    # third member expansion
    if teams_at_or_above_level(event, 2) and not teams_at_or_above_level(event, 3):
        registration.pending_level = 3
        registration.level = None
        maybe_release_full_pool(event, 3)
        return

    # fourth member expansion
    if teams_at_or_above_level(event, 3) and not teams_at_or_above_level(event, 4):
        registration.pending_level = 4
        registration.level = None
        maybe_release_full_pool(event, 4)
        return

    # fallback if something got out of sync
    finalize_pending_if_due(event, force=True)


def teams_at_or_above_level(event: Event, level: int) -> bool:
    for team in event.teams:
        count = Registration.query.filter_by(event_id=event.id, team_id=team.id).count()
        if count < level:
            return False
    return True


def maybe_release_full_pool(event: Event, level: int):
    pending = Registration.query.filter_by(event_id=event.id, pending_level=level).order_by(Registration.created_at).all()
    if len(pending) >= 5:
        release_pending(event, level, pending[:5])


def finalize_pending_if_due(event: Event, force: bool = False):
    if not force and datetime.now() < event.registration_deadline:
        return

    pending3 = Registration.query.filter_by(event_id=event.id, pending_level=3).order_by(Registration.created_at).all()
    if pending3:
        release_pending(event, 3, pending3)

    pending4 = Registration.query.filter_by(event_id=event.id, pending_level=4).order_by(Registration.created_at).all()
    if pending4:
        release_pending(event, 4, pending4)


def release_pending(event: Event, level: int, registrations):
    candidate_teams = [
        team for team in event.teams
        if Registration.query.filter_by(event_id=event.id, team_id=team.id).count() == level - 1
    ]
    if not candidate_teams or not registrations:
        return
    random.shuffle(candidate_teams)
    random.shuffle(registrations)
    for team, registration in zip(candidate_teams, registrations):
        registration.team_id = team.id
        registration.level = level
        registration.pending_level = None
        maybe_apply_team_name(team, registration.team_name_suggestion)


def maybe_apply_team_name(team: Team, suggestion: str | None):
    if suggestion and not team.name:
        team.name = suggestion.strip()[:80]


def round_robin_matches():
    teams = [1, 2, 3, 4, 5]
    matches = []
    for i in range(len(teams)):
        for j in range(i + 1, len(teams)):
            matches.append((teams[i], teams[j]))
    return matches


app = create_app()

if __name__ == "__main__":
    app.run(debug=True)
