import logging
import os
import random
import re
import secrets
from datetime import datetime, timedelta
from functools import wraps
from zoneinfo import ZoneInfo

import requests
from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, abort, flash, g, redirect, render_template, request, session, url_for
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_sqlalchemy import SQLAlchemy
from flask_wtf.csrf import CSRFProtect
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from werkzeug.security import check_password_hash, generate_password_hash

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover
    pass

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("arena")

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
TZ = ZoneInfo("Asia/Tashkent")
PRODUCTION = os.environ.get("PRODUCTION") == "1"
REQUIRE_GMAIL = os.environ.get("REQUIRE_GMAIL", "1") == "1"

GAMES = {
    "mlbb": {"name": "Mobile Legends: Bang Bang", "short": "MLBB", "team_size": 5, "max_teams": 30},
    "pubg": {"name": "PUBG Mobile", "short": "PUBG", "team_size": 4, "max_teams": 20},
}
MODES = {"pvp": "PvP (1 ga 1)", "team": "Jamoaviy"}
PVP_MAX = 50


def now():
    """Toshkent vaqti (naive datetime)."""
    return datetime.now(TZ).replace(tzinfo=None)


def _secret_key():
    key = os.environ.get("SECRET_KEY")
    if key:
        return key
    path = os.path.join(BASE_DIR, ".secret_key")
    if os.path.exists(path):
        with open(path) as f:
            return f.read().strip()
    key = secrets.token_hex(32)
    with open(path, "w") as f:
        f.write(key)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return key


app = Flask(__name__)
db_url = os.environ.get("DATABASE_URL", "sqlite:///" + os.path.join(BASE_DIR, "arena.db"))
if db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)
app.config.update(
    SECRET_KEY=_secret_key(),
    SQLALCHEMY_DATABASE_URI=db_url,
    SQLALCHEMY_TRACK_MODIFICATIONS=False,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=PRODUCTION,
    PERMANENT_SESSION_LIFETIME=timedelta(days=7),
    MAX_CONTENT_LENGTH=64 * 1024,
    WTF_CSRF_TIME_LIMIT=28800,
)
if os.environ.get("TRUST_PROXY") == "1":
    from werkzeug.middleware.proxy_fix import ProxyFix

    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)

db = SQLAlchemy(app)
csrf = CSRFProtect(app)
limiter = Limiter(get_remote_address, app=app, default_limits=["600 per hour"], storage_uri="memory://")


@limiter.request_filter
def _skip_static():
    return request.endpoint == "static"


# ----------------------------------------------------------------- modellar
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True, nullable=False, index=True)
    nickname = db.Column(db.String(30), unique=True, nullable=False)
    phone = db.Column(db.String(12), nullable=False)  # 998901234567
    password_hash = db.Column(db.String(255), nullable=False)
    is_admin = db.Column(db.Boolean, default=False, nullable=False)
    failed_logins = db.Column(db.Integer, default=0, nullable=False)
    locked_until = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=now, nullable=False)


class Tournament(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(80), nullable=False)
    game = db.Column(db.String(10), nullable=False)
    mode = db.Column(db.String(10), nullable=False)
    description = db.Column(db.Text, default="")
    start_at = db.Column(db.DateTime, nullable=False)
    capacity = db.Column(db.Integer, nullable=False)  # pvp: o'yinchi, team: jamoa soni
    team_size = db.Column(db.Integer, default=1, nullable=False)
    max_players = db.Column(db.Integer, nullable=False)
    prize_unit = db.Column(db.String(20), default="so'm", nullable=False)
    status = db.Column(db.String(10), default="open", nullable=False)  # open/running/finished
    rounds = db.Column(db.Integer, default=0, nullable=False)
    sent_30 = db.Column(db.Boolean, default=False, nullable=False)
    sent_10 = db.Column(db.Boolean, default=False, nullable=False)
    sent_1 = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=now, nullable=False)

    prizes = db.relationship("Prize", backref="tournament", cascade="all, delete-orphan", order_by="Prize.place")
    entries = db.relationship("Entry", backref="tournament", cascade="all, delete-orphan")
    teams = db.relationship("Team", backref="tournament", cascade="all, delete-orphan", order_by="Team.id")
    matches = db.relationship("Match", backref="tournament", cascade="all, delete-orphan")
    placements = db.relationship("Placement", backref="tournament", cascade="all, delete-orphan")

    @property
    def uses_bracket(self):
        return self.mode == "pvp" or self.game == "mlbb"

    @property
    def entry_count(self):
        return Entry.query.filter_by(tournament_id=self.id).count()

    @property
    def is_full(self):
        return self.entry_count >= self.max_players

    @property
    def registration_open(self):
        return self.status == "open" and self.start_at > now() and not self.is_full


class Entry(db.Model):
    __table_args__ = (db.UniqueConstraint("tournament_id", "user_id"),)
    id = db.Column(db.Integer, primary_key=True)
    tournament_id = db.Column(db.Integer, db.ForeignKey("tournament.id"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    team_id = db.Column(db.Integer, db.ForeignKey("team.id"))
    game_id = db.Column(db.String(40))  # o'yindagi ID (MLBB: User ID, PUBG: Player ID)
    zone_id = db.Column(db.String(10))  # faqat MLBB: server (zone) ID
    created_at = db.Column(db.DateTime, default=now, nullable=False)
    user = db.relationship("User")


class Team(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    tournament_id = db.Column(db.Integer, db.ForeignKey("tournament.id"), nullable=False, index=True)
    name = db.Column(db.String(40), nullable=False)
    points = db.Column(db.Integer, default=0, nullable=False)


class Match(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    tournament_id = db.Column(db.Integer, db.ForeignKey("tournament.id"), nullable=False, index=True)
    round = db.Column(db.Integer, nullable=False)
    position = db.Column(db.Integer, nullable=False)
    a_id = db.Column(db.Integer)  # pvp: user.id, team: team.id
    b_id = db.Column(db.Integer)
    winner_id = db.Column(db.Integer)


class Prize(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    tournament_id = db.Column(db.Integer, db.ForeignKey("tournament.id"), nullable=False, index=True)
    place = db.Column(db.Integer, nullable=False)
    amount = db.Column(db.BigInteger, nullable=False)


class Placement(db.Model):
    """Bracket turnirlarida 3+ o'rinlarni admin qo'lda belgilaydi."""

    id = db.Column(db.Integer, primary_key=True)
    tournament_id = db.Column(db.Integer, db.ForeignKey("tournament.id"), nullable=False, index=True)
    place = db.Column(db.Integer, nullable=False)
    ref_id = db.Column(db.Integer, nullable=False)


# ------------------------------------------------------------------ yordamchi
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,200}\.[A-Za-z]{2,}$")
NICK_RE = re.compile(r"^[A-Za-z0-9_.]{3,20}$")


def normalize_phone(raw):
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 9:
        digits = "998" + digits
    if len(digits) == 12 and digits.startswith("998"):
        return digits
    return None


def mask_phone(p):
    return f"{p[:3]}*****{p[-4:]}" if p and len(p) >= 7 else "***"


def validate_password(pw):
    if len(pw) < 8 or len(pw) > 128:
        return "Parol kamida 8 ta belgidan iborat bo'lsin."
    if not re.search(r"[A-Za-z]", pw) or not re.search(r"\d", pw):
        return "Parolda harf ham, raqam ham bo'lishi kerak."
    return None


def validate_game_ids(game, gid, zone):
    """O'yin ID (va MLBB uchun server ID) ni tekshiradi. (gid, zone, xato) qaytaradi."""
    gid = re.sub(r"\s", "", gid or "")
    zone = re.sub(r"\s", "", zone or "")
    if not re.fullmatch(r"[0-9]+", gid):
        return None, None, "O'yin ID faqat raqamlardan iborat bo'lsin."
    if game == "mlbb":
        if not (5 <= len(gid) <= 12):
            return None, None, "MLBB ID 5–12 ta raqamdan iborat bo'lsin."
        if not re.fullmatch(r"[0-9]{1,6}", zone):
            return None, None, "Server (Zone) ID ni kiriting, masalan: 2345."
        return gid, zone, None
    if not (6 <= len(gid) <= 14):
        return None, None, "PUBG ID 6–14 ta raqamdan iborat bo'lsin."
    return gid, "", None


def game_id_taken(t, gid, zone, exclude_entry_id=None):
    q = Entry.query.filter_by(tournament_id=t.id, game_id=gid, zone_id=zone)
    if exclude_entry_id:
        q = q.filter(Entry.id != exclude_entry_id)
    return q.first() is not None


def fmt_dt(dt):
    return dt.strftime("%d.%m.%Y, %H:%M") if dt else "—"


def money(n):
    return f"{int(n):,}".replace(",", " ")


@app.before_request
def load_user():
    uid = session.get("uid")
    g.user = db.session.get(User, uid) if uid else None


@app.context_processor
def inject():
    return dict(current_user=g.get("user"), GAMES=GAMES, MODES=MODES, fmt_dt=fmt_dt, money=money, mask_phone=mask_phone)


def login_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if not g.user:
            flash("Avval tizimga kiring.", "warn")
            return redirect(url_for("login"))
        return fn(*a, **kw)

    return wrapper


def admin_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if not g.user:
            return redirect(url_for("login"))
        if not g.user.is_admin:
            abort(403)
        return fn(*a, **kw)

    return wrapper


@app.after_request
def security_headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "same-origin"
    resp.headers["Permissions-Policy"] = "geolocation=(), camera=(), microphone=()"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
        "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    )
    if PRODUCTION:
        resp.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    if g.get("user"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


# ------------------------------------------------------------ turnir mantiqi
def build_names(t):
    if t.mode == "pvp":
        return {e.user_id: e.user.nickname for e in Entry.query.filter_by(tournament_id=t.id).all()}
    return {tm.id: tm.name for tm in t.teams}


def round_label(r, total):
    left = total - r
    if left == 0:
        return "Final"
    if left == 1:
        return "Yarim final"
    if left == 2:
        return "Chorak final"
    return f"1/{2 ** (total - r + 1)} final"


def build_rounds(t):
    if not t.uses_bracket or not t.rounds:
        return []
    ms = Match.query.filter_by(tournament_id=t.id).order_by(Match.round, Match.position).all()
    return [
        {"label": round_label(r, t.rounds), "matches": [m for m in ms if m.round == r]}
        for r in range(1, t.rounds + 1)
    ]


def propagate(m, t):
    if m.round >= t.rounds or not m.winner_id:
        return
    nxt = Match.query.filter_by(tournament_id=t.id, round=m.round + 1, position=m.position // 2).first()
    if m.position % 2 == 0:
        nxt.a_id = m.winner_id
    else:
        nxt.b_id = m.winner_id


def build_bracket(t, refs):
    refs = list(refs)
    random.shuffle(refs)
    n = len(refs)
    size = 1
    while size < n:
        size *= 2
    t.rounds = size.bit_length() - 1
    pairs, byes = size // 2, size - n
    bye_idx = set(random.sample(range(pairs), byes)) if byes else set()
    it = iter(refs)
    for p in range(pairs):
        a = next(it)
        b = None if p in bye_idx else next(it)
        db.session.add(Match(tournament_id=t.id, round=1, position=p, a_id=a, b_id=b))
    for r in range(2, t.rounds + 1):
        for p in range(size // (2 ** r)):
            db.session.add(Match(tournament_id=t.id, round=r, position=p))
    db.session.flush()
    for m in Match.query.filter_by(tournament_id=t.id, round=1, b_id=None).all():
        m.winner_id = m.a_id  # bye
        propagate(m, t)
    db.session.flush()


def standings_map(t):
    res = {}
    if t.status != "finished":
        return res
    if t.uses_bracket:
        final = Match.query.filter_by(tournament_id=t.id, round=t.rounds, position=0).first()
        if final and final.winner_id:
            res[1] = final.winner_id
            res[2] = final.b_id if final.winner_id == final.a_id else final.a_id
        for p in t.placements:
            res[p.place] = p.ref_id
    else:
        for i, tm in enumerate(sorted(t.teams, key=lambda x: (-x.points, x.id))):
            res[i + 1] = tm.id
    return res


# --------------------------------------------------------------------- SMS
class SmsClient:
    """Eskiz.uz orqali SMS. ESKIZ_EMAIL bo'sh bo'lsa faqat logga yozadi (test rejim)."""

    base = "https://notify.eskiz.uz/api"
    token = None

    def _login(self):
        r = requests.post(
            f"{self.base}/auth/login",
            data={"email": os.environ["ESKIZ_EMAIL"], "password": os.environ["ESKIZ_PASSWORD"]},
            timeout=10,
        )
        r.raise_for_status()
        self.token = r.json()["data"]["token"]

    def send(self, phone, text):
        if not os.environ.get("ESKIZ_EMAIL"):
            log.info("[SMS-TEST] %s -> %s", mask_phone(phone), text)
            return True
        for attempt in (1, 2):
            try:
                if not self.token:
                    self._login()
                r = requests.post(
                    f"{self.base}/message/sms/send",
                    headers={"Authorization": f"Bearer {self.token}"},
                    data={"mobile_phone": phone, "message": text, "from": os.environ.get("ESKIZ_FROM", "4546")},
                    timeout=10,
                )
                if r.status_code == 401 and attempt == 1:
                    self.token = None
                    continue
                r.raise_for_status()
                return True
            except Exception as exc:  # noqa: BLE001
                log.warning("SMS xatosi (%s): %s", mask_phone(phone), exc)
                self.token = None
        return False


sms = SmsClient()

REMINDERS = (("sent_30", 1800, 600, 30), ("sent_10", 600, 60, 10), ("sent_1", 60, 0, 1))


def check_reminders():
    with app.app_context():
        n = now()
        upcoming = Tournament.query.filter(
            Tournament.status == "open", Tournament.start_at > n, Tournament.start_at <= n + timedelta(minutes=31)
        ).all()
        for t in upcoming:
            remaining = (t.start_at - n).total_seconds()
            for flag, upper, lower, label in REMINDERS:
                if lower < remaining <= upper:
                    # bir nechta worker bo'lsa ham SMS bir marta ketishi uchun "claim"
                    res = db.session.execute(
                        update(Tournament).where(Tournament.id == t.id, getattr(Tournament, flag).is_(False)).values({flag: True})
                    )
                    db.session.commit()
                    if res.rowcount == 1:
                        send_reminder(t, label)


def send_reminder(t, minutes):
    text = f"{t.title} turniri {minutes} daqiqadan keyin boshlanadi ({t.start_at:%H:%M}). Tayyor bo'ling!"
    users = User.query.join(Entry, Entry.user_id == User.id).filter(Entry.tournament_id == t.id).all()
    for u in users:
        sms.send(u.phone, text)
    log.info("Turnir %s: %s daqiqalik eslatma %s kishiga yuborildi", t.id, minutes, len(users))


# ------------------------------------------------------------ ommaviy sahifalar
@app.route("/")
def index():
    upcoming = Tournament.query.filter(Tournament.status != "finished").order_by(Tournament.start_at).limit(6).all()
    stats = {
        "players": User.query.count(),
        "tournaments": Tournament.query.count(),
        "finished": Tournament.query.filter_by(status="finished").count(),
    }
    return render_template("index.html", upcoming=upcoming, stats=stats)


@app.route("/tournaments")
def tournaments():
    game = request.args.get("game", "")
    mode = request.args.get("mode", "")
    state = request.args.get("state", "active")
    q = Tournament.query
    if game in GAMES:
        q = q.filter_by(game=game)
    if mode in MODES:
        q = q.filter_by(mode=mode)
    if state == "finished":
        q = q.filter_by(status="finished").order_by(Tournament.start_at.desc())
    else:
        state = "active"
        q = q.filter(Tournament.status != "finished").order_by(Tournament.start_at)
    return render_template("tournaments.html", items=q.all(), game=game, mode=mode, state=state)


@app.route("/tournament/<int:tid>")
def tournament(tid):
    t = db.get_or_404(Tournament, tid)
    entries = Entry.query.filter_by(tournament_id=t.id).order_by(Entry.created_at).all()
    my_entry = next((e for e in entries if g.user and e.user_id == g.user.id), None)
    joined = my_entry is not None
    names = build_names(t)
    members = {}
    for e in entries:
        if e.team_id:
            members.setdefault(e.team_id, []).append(e.user.nickname)
    return render_template(
        "tournament.html",
        t=t,
        entries=entries,
        joined=joined,
        my_entry=my_entry,
        names=names,
        members=members,
        rounds=build_rounds(t),
        winners=standings_map(t),
        teams_sorted=sorted(t.teams, key=lambda x: (-x.points, x.id)),
    )


@app.post("/tournament/<int:tid>/join")
@login_required
def join(tid):
    t = db.get_or_404(Tournament, tid)
    if not t.registration_open:
        flash("Bu turnirga ro'yxatdan o'tish yopiq yoki joylar to'lgan.", "error")
        return redirect(url_for("tournament", tid=tid))
    if Entry.query.filter_by(tournament_id=t.id, user_id=g.user.id).first():
        flash("Siz allaqachon ro'yxatdasiz.", "warn")
        return redirect(url_for("tournament", tid=tid))
    gid, zone, err = validate_game_ids(t.game, request.form.get("game_id"), request.form.get("zone_id"))
    if err:
        flash(err, "error")
        return redirect(url_for("tournament", tid=tid))
    if game_id_taken(t, gid, zone):
        flash("Bu o'yin ID bilan boshqa ishtirokchi allaqachon ro'yxatdan o'tgan.", "error")
        return redirect(url_for("tournament", tid=tid))
    db.session.add(Entry(tournament_id=t.id, user_id=g.user.id, game_id=gid, zone_id=zone))
    try:
        db.session.commit()
        flash("Ro'yxatdan o'tdingiz! Boshlanishidan oldin SMS orqali eslatamiz.", "ok")
    except IntegrityError:
        db.session.rollback()
    return redirect(url_for("tournament", tid=tid))


@app.post("/tournament/<int:tid>/gameid")
@login_required
def update_game_id(tid):
    t = db.get_or_404(Tournament, tid)
    e = Entry.query.filter_by(tournament_id=t.id, user_id=g.user.id).first_or_404()
    if t.status != "open":
        flash("Turnir boshlangan, ID ni o'zgartirib bo'lmaydi.", "error")
        return redirect(url_for("tournament", tid=tid))
    gid, zone, err = validate_game_ids(t.game, request.form.get("game_id"), request.form.get("zone_id"))
    if err:
        flash(err, "error")
    elif game_id_taken(t, gid, zone, exclude_entry_id=e.id):
        flash("Bu o'yin ID bilan boshqa ishtirokchi ro'yxatdan o'tgan.", "error")
    else:
        e.game_id, e.zone_id = gid, zone
        db.session.commit()
        flash("O'yin ID saqlandi.", "ok")
    return redirect(url_for("tournament", tid=tid))


@app.post("/tournament/<int:tid>/leave")
@login_required
def leave(tid):
    t = db.get_or_404(Tournament, tid)
    if t.status != "open":
        flash("Turnir boshlangan, chiqib bo'lmaydi.", "error")
    else:
        Entry.query.filter_by(tournament_id=t.id, user_id=g.user.id).delete()
        db.session.commit()
        flash("Ro'yxatdan chiqdingiz.", "ok")
    return redirect(url_for("tournament", tid=tid))


# ---------------------------------------------------------------- autentifikatsiya
@app.route("/register", methods=["GET", "POST"])
@limiter.limit("8 per hour", methods=["POST"])
def register():
    if g.user:
        return redirect(url_for("index"))
    form = {}
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        nick = request.form.get("nickname", "").strip()
        phone = normalize_phone(request.form.get("phone", ""))
        pw = request.form.get("password", "")
        pw2 = request.form.get("password2", "")
        form = {"email": email, "nickname": nick, "phone": request.form.get("phone", "")}
        err = None
        if not EMAIL_RE.match(email):
            err = "Email manzil noto'g'ri."
        elif REQUIRE_GMAIL and not email.endswith("@gmail.com"):
            err = "Faqat Gmail manzil (…@gmail.com) qabul qilinadi."
        elif not NICK_RE.match(nick):
            err = "Nikneym 3–20 ta belgi: lotin harflar, raqam, _ yoki nuqta."
        elif not phone:
            err = "Telefon raqam noto'g'ri. Masalan: +998 90 123 45 67"
        elif pw != pw2:
            err = "Parollar bir xil emas."
        else:
            err = validate_password(pw)
        if not err:
            if User.query.filter_by(email=email).first():
                err = "Bu email bilan allaqachon ro'yxatdan o'tilgan."
            elif User.query.filter(db.func.lower(User.nickname) == nick.lower()).first():
                err = "Bu nikneym band."
        if err:
            flash(err, "error")
        else:
            u = User(email=email, nickname=nick, phone=phone, password_hash=generate_password_hash(pw))
            db.session.add(u)
            db.session.commit()
            session.clear()
            session["uid"] = u.id
            session.permanent = True
            flash("Xush kelibsiz! Endi turnirlarga qo'shilishingiz mumkin.", "ok")
            return redirect(url_for("tournaments"))
    return render_template("register.html", form=form)


DUMMY_HASH = generate_password_hash("dummy-password-1")


@app.route("/login", methods=["GET", "POST"])
@limiter.limit("15 per 10 minutes", methods=["POST"])
def login():
    if g.user:
        return redirect(url_for("index"))
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        pw = request.form.get("password", "")
        u = User.query.filter_by(email=email).first()
        if u and u.locked_until and u.locked_until > now():
            flash("Juda ko'p xato urinish. 10 daqiqadan keyin qayta urinib ko'ring.", "error")
        elif u and check_password_hash(u.password_hash, pw):
            u.failed_logins, u.locked_until = 0, None
            db.session.commit()
            session.clear()
            session["uid"] = u.id
            session.permanent = True
            return redirect(url_for("admin_dashboard") if u.is_admin else url_for("tournaments"))
        else:
            if u:
                u.failed_logins += 1
                if u.failed_logins >= 5:
                    u.locked_until = now() + timedelta(minutes=10)
                    u.failed_logins = 0
                db.session.commit()
            else:
                check_password_hash(DUMMY_HASH, pw)  # vaqt farqini yashirish
            flash("Email yoki parol noto'g'ri.", "error")
    return render_template("login.html")


@app.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))


@app.route("/profile")
@login_required
def profile():
    rows = (
        db.session.query(Entry, Tournament)
        .join(Tournament, Tournament.id == Entry.tournament_id)
        .filter(Entry.user_id == g.user.id)
        .order_by(Tournament.start_at.desc())
        .all()
    )
    return render_template("profile.html", rows=rows)


@app.post("/profile/password")
@login_required
@limiter.limit("10 per hour")
def change_password():
    cur = request.form.get("current", "")
    new = request.form.get("new", "")
    if not check_password_hash(g.user.password_hash, cur):
        flash("Joriy parol noto'g'ri.", "error")
    elif validate_password(new):
        flash(validate_password(new), "error")
    else:
        g.user.password_hash = generate_password_hash(new)
        db.session.commit()
        flash("Parol yangilandi.", "ok")
    return redirect(url_for("profile"))


# --------------------------------------------------------------------- admin
@app.route("/admin")
@admin_required
def admin_dashboard():
    items = Tournament.query.order_by(Tournament.start_at.desc()).all()
    return render_template("admin/dashboard.html", items=items, users=User.query.count())


def _parse_tournament_form(f):
    title = f.get("title", "").strip()
    game = f.get("game", "")
    mode = f.get("mode", "")
    desc = f.get("description", "").strip()[:2000]
    unit = f.get("prize_unit", "").strip()[:20] or "so'm"
    if not (3 <= len(title) <= 80):
        return None, "Turnir nomi 3–80 belgi bo'lsin."
    if game not in GAMES or mode not in MODES:
        return None, "O'yin yoki tur noto'g'ri."
    try:
        start = datetime.strptime(f.get("start_at", ""), "%Y-%m-%dT%H:%M")
    except ValueError:
        return None, "Boshlanish vaqtini kiriting."
    if start <= now() + timedelta(minutes=1):
        return None, "Boshlanish vaqti kelajakda bo'lishi kerak."
    try:
        cap = int(f.get("capacity", ""))
    except ValueError:
        return None, "Sig'imni son bilan kiriting."
    if mode == "pvp":
        if not (2 <= cap <= PVP_MAX):
            return None, f"PvP uchun ishtirokchilar soni 2–{PVP_MAX} oralig'ida."
        team_size, max_players = 1, cap
    else:
        gmax = GAMES[game]["max_teams"]
        if not (2 <= cap <= gmax):
            return None, f"{GAMES[game]['short']} uchun jamoalar soni 2–{gmax} oralig'ida."
        team_size = GAMES[game]["team_size"]
        max_players = cap * team_size
    amounts = []
    for raw in f.getlist("prize_amount"):
        raw = raw.strip().replace(" ", "")
        if not raw:
            continue
        if not raw.isdigit() or not (0 < int(raw) <= 10 ** 12):
            return None, "Mukofot summasi musbat butun son bo'lsin."
        amounts.append(int(raw))
    if not amounts:
        return None, "Kamida 1-o'rin uchun mukofot kiriting."
    if len(amounts) > cap:
        return None, "Mukofotli o'rinlar soni ishtirokchilar (jamoalar) sonidan oshmasin."
    return dict(title=title, game=game, mode=mode, description=desc, start_at=start, capacity=cap,
                team_size=team_size, max_players=max_players, prize_unit=unit, amounts=amounts), None


@app.route("/admin/new", methods=["GET", "POST"])
@admin_required
def admin_new():
    if request.method == "POST":
        data, err = _parse_tournament_form(request.form)
        if err:
            flash(err, "error")
        else:
            amounts = data.pop("amounts")
            t = Tournament(**data)
            db.session.add(t)
            db.session.flush()
            for i, a in enumerate(amounts, 1):
                db.session.add(Prize(tournament_id=t.id, place=i, amount=a))
            db.session.commit()
            flash("Turnir yaratildi.", "ok")
            return redirect(url_for("admin_manage", tid=t.id))
    return render_template("admin/new.html", form=request.form)


@app.route("/admin/t/<int:tid>")
@admin_required
def admin_manage(tid):
    t = db.get_or_404(Tournament, tid)
    entries = Entry.query.filter_by(tournament_id=t.id).order_by(Entry.created_at).all()
    names = build_names(t)
    members = {}
    for e in entries:
        if e.team_id:
            label = e.user.nickname + (f" · {e.game_id}" if e.game_id else "")
            members.setdefault(e.team_id, []).append(label)
    return render_template(
        "admin/manage.html", t=t, entries=entries, names=names, members=members,
        rounds=build_rounds(t), winners=standings_map(t),
        teams_sorted=sorted(t.teams, key=lambda x: (-x.points, x.id)),
    )


@app.post("/admin/t/<int:tid>/start")
@admin_required
def admin_start(tid):
    t = db.get_or_404(Tournament, tid)
    if t.status != "open":
        flash("Turnir allaqachon boshlangan.", "error")
        return redirect(url_for("admin_manage", tid=tid))
    entries = Entry.query.filter_by(tournament_id=t.id).all()
    if t.mode == "pvp":
        if len(entries) < 2:
            flash("Boshlash uchun kamida 2 ishtirokchi kerak.", "error")
            return redirect(url_for("admin_manage", tid=tid))
        build_bracket(t, [e.user_id for e in entries])
    else:
        n_teams = min(len(entries) // t.team_size, t.capacity)
        if n_teams < 2:
            flash(f"Kamida 2 ta to'liq jamoa ({2 * t.team_size} o'yinchi) kerak.", "error")
            return redirect(url_for("admin_manage", tid=tid))
        random.shuffle(entries)
        ids = []
        for i in range(n_teams):
            tm = Team(tournament_id=t.id, name=f"Jamoa {i + 1}")
            db.session.add(tm)
            db.session.flush()
            ids.append(tm.id)
            for e in entries[i * t.team_size:(i + 1) * t.team_size]:
                e.team_id = tm.id
        left = len(entries) - n_teams * t.team_size
        if left:
            flash(f"{left} ta o'yinchi jamoasiz qoldi (to'liq jamoa chiqmadi).", "warn")
        if t.uses_bracket:
            build_bracket(t, ids)
    t.status = "running"
    db.session.commit()
    flash("Turnir boshlandi.", "ok")
    return redirect(url_for("admin_manage", tid=tid))


@app.post("/admin/match/<int:mid>/winner")
@admin_required
def admin_match_winner(mid):
    m = db.get_or_404(Match, mid)
    t = db.session.get(Tournament, m.tournament_id)
    try:
        wid = int(request.form.get("winner_id", ""))
    except ValueError:
        abort(400)
    if not (m.a_id and m.b_id) or wid not in (m.a_id, m.b_id):
        abort(400)
    if t.status == "finished" and m.round != t.rounds:
        flash("Turnir yakunlangan.", "error")
    elif t.status not in ("running", "finished"):
        flash("Turnir hali boshlanmagan.", "error")
    else:
        nxt = None
        if m.round < t.rounds:
            nxt = Match.query.filter_by(tournament_id=t.id, round=m.round + 1, position=m.position // 2).first()
        if nxt and nxt.winner_id:
            flash("Keyingi o'yin natijasi kiritilgan. Avval o'sha natijani o'zgartiring.", "error")
        else:
            m.winner_id = wid
            propagate(m, t)
            if m.round == t.rounds:
                t.status = "finished"
            db.session.commit()
            flash("Natija saqlandi.", "ok")
    return redirect(url_for("admin_manage", tid=t.id))


@app.post("/admin/t/<int:tid>/points")
@admin_required
def admin_points(tid):
    t = db.get_or_404(Tournament, tid)
    if t.status != "running" or t.uses_bracket:
        abort(400)
    for tm in t.teams:
        raw = request.form.get(f"points_{tm.id}", "").strip()
        if raw.isdigit() and int(raw) <= 100000:
            tm.points = int(raw)
    db.session.commit()
    flash("Ballar saqlandi.", "ok")
    return redirect(url_for("admin_manage", tid=tid))


@app.post("/admin/t/<int:tid>/finish")
@admin_required
def admin_finish(tid):
    t = db.get_or_404(Tournament, tid)
    if t.status == "running" and not t.uses_bracket:
        t.status = "finished"
        db.session.commit()
        flash("Turnir yakunlandi.", "ok")
    return redirect(url_for("admin_manage", tid=tid))


@app.post("/admin/t/<int:tid>/placements")
@admin_required
def admin_placements(tid):
    t = db.get_or_404(Tournament, tid)
    if t.status != "finished" or not t.uses_bracket:
        abort(400)
    valid = set(build_names(t).keys())
    Placement.query.filter_by(tournament_id=t.id).delete()
    used = set()
    for p in t.prizes:
        if p.place < 3:
            continue
        raw = request.form.get(f"place_{p.place}", "")
        if raw.isdigit() and int(raw) in valid and int(raw) not in used:
            used.add(int(raw))
            db.session.add(Placement(tournament_id=t.id, place=p.place, ref_id=int(raw)))
    db.session.commit()
    flash("O'rinlar saqlandi.", "ok")
    return redirect(url_for("admin_manage", tid=tid))


@app.post("/admin/t/<int:tid>/kick/<int:uid>")
@admin_required
def admin_kick(tid, uid):
    t = db.get_or_404(Tournament, tid)
    if t.status == "open":
        Entry.query.filter_by(tournament_id=t.id, user_id=uid).delete()
        db.session.commit()
        flash("Ishtirokchi olib tashlandi.", "ok")
    return redirect(url_for("admin_manage", tid=tid))


@app.post("/admin/t/<int:tid>/delete")
@admin_required
def admin_delete(tid):
    t = db.get_or_404(Tournament, tid)
    for e in Entry.query.filter_by(tournament_id=t.id).all():
        e.team_id = None
    db.session.flush()
    db.session.delete(t)
    db.session.commit()
    flash("Turnir o'chirildi.", "ok")
    return redirect(url_for("admin_dashboard"))


# ------------------------------------------------------------------- xatolar
@app.errorhandler(400)
@app.errorhandler(403)
@app.errorhandler(404)
@app.errorhandler(429)
@app.errorhandler(500)
def on_error(e):
    code = getattr(e, "code", 500)
    msgs = {
        400: "So'rov noto'g'ri.",
        403: "Bu sahifaga kirish huquqingiz yo'q.",
        404: "Sahifa topilmadi.",
        429: "Juda ko'p so'rov. Birozdan keyin urinib ko'ring.",
        500: "Serverda xatolik. Keyinroq urinib ko'ring.",
    }
    return render_template("error.html", code=code, msg=msgs.get(code, "Xatolik")), code


@app.errorhandler(Exception)
def on_exception(e):
    from werkzeug.exceptions import HTTPException

    if isinstance(e, HTTPException):
        return on_error(e)
    log.exception("Kutilmagan xatolik")
    db.session.rollback()
    return on_error(type("E", (), {"code": 500})())


# ------------------------------------------------------------------ ishga tushirish
def seed_admin():
    email = os.environ.get("ADMIN_EMAIL", "").strip().lower()
    pw = os.environ.get("ADMIN_PASSWORD", "")
    if email and pw and not User.query.filter_by(is_admin=True).first():
        db.session.add(
            User(email=email, nickname="admin", phone="998000000000", password_hash=generate_password_hash(pw), is_admin=True)
        )
        db.session.commit()
        log.info("Admin yaratildi: %s", email)


def ensure_columns():
    """Eski bazada yangi ustunlar bo'lmasa qo'shadi (create_all mavjud jadvalni o'zgartirmaydi)."""
    from sqlalchemy import inspect, text

    cols = {c["name"] for c in inspect(db.engine).get_columns("entry")}
    with db.engine.begin() as conn:
        if "game_id" not in cols:
            conn.execute(text("ALTER TABLE entry ADD COLUMN game_id VARCHAR(40)"))
        if "zone_id" not in cols:
            conn.execute(text("ALTER TABLE entry ADD COLUMN zone_id VARCHAR(10)"))


with app.app_context():
    db.create_all()
    ensure_columns()
    seed_admin()


@app.cli.command("create-admin")
def create_admin_cmd():
    """Admin yaratish: flask --app app create-admin"""
    import getpass

    email = input("Admin email: ").strip().lower()
    pw = getpass.getpass("Parol: ")
    err = validate_password(pw)
    if err or not EMAIL_RE.match(email):
        print(err or "Email noto'g'ri")
        return
    nick = input("Nikneym [admin]: ").strip() or "admin"
    db.session.add(User(email=email, nickname=nick, phone="998000000000", password_hash=generate_password_hash(pw), is_admin=True))
    db.session.commit()
    print("Tayyor.")


if os.environ.get("DISABLE_SCHEDULER") != "1":
    scheduler = BackgroundScheduler(daemon=True, timezone=TZ)
    scheduler.add_job(check_reminders, "interval", seconds=20, id="reminders", max_instances=1, coalesce=True)
    scheduler.start()

if __name__ == "__main__":
    app.run(debug=False, port=int(os.environ.get("PORT", 5000)))
