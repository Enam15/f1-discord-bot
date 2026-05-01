import os
import asyncio
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from typing import List

import aiohttp
import aiosqlite
import discord
from discord import app_commands

# =======================
# CONFIG
# =======================
BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"


def load_local_env(path: Path = ENV_PATH) -> None:
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        if not key or key in os.environ:
            continue

        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]

        os.environ[key] = value


def env_int(name: str, default=None):
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default

    try:
        return int(value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer.") from exc


def resolve_local_path(value, default) -> Path:
    path = Path(value or default).expanduser()
    if not path.is_absolute():
        path = BASE_DIR / path
    return path


load_local_env()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
ADMIN_ROLE = os.getenv("ADMIN_ROLE", "LeagueAdmin")
REMINDER_ROLE = os.getenv("REMINDER_ROLE", "f1")

LEADERBOARD_CHANNEL_ID = env_int("LEADERBOARD_CHANNEL_ID")
REMINDER_CHANNEL_ID = env_int("REMINDER_CHANNEL_ID")
GUILD_ID = env_int("GUILD_ID")

JOLPICA_BASE = os.getenv("JOLPICA_BASE", "https://api.jolpi.ca/ergast/f1")
DATA_DIR = resolve_local_path(os.getenv("DATA_DIR"), BASE_DIR / "data")
DB_FILE = resolve_local_path(os.getenv("DB_PATH"), DATA_DIR / "f1fantasy.db")
BACKUP_DIR = resolve_local_path(os.getenv("BACKUP_DIR"), Path.home() / "Documents" / "f1-discord-bot-backups")
LEAGUE_SEASON = env_int("LEAGUE_SEASON", 2026)

DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_FILE.parent.mkdir(parents=True, exist_ok=True)
BACKUP_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = str(DB_FILE)

try:
    BDT = ZoneInfo("Asia/Dhaka")
except ZoneInfoNotFoundError:
    BDT = timezone(timedelta(hours=6), "Asia/Dhaka")
PRESEASON_LOCK_BDT = datetime(2026, 3, 5, 23, 59, 0, tzinfo=BDT)
PRESEASON_LOCK_UTC = PRESEASON_LOCK_BDT.astimezone(timezone.utc)

SESSIONS = ("quali", "race")
SESSION_RESULT_DELAY = {
    "quali": timedelta(hours=2),
    "race": timedelta(hours=4),
}

REMINDER_SCHEDULE = (
    ("24h", "24 hours", timedelta(hours=24)),
    ("12h", "12 hours", timedelta(hours=12)),
)
BACKUP_INTERVAL = timedelta(hours=6)


# =======================
# HELPERS
# =======================
def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def from_iso(s: str) -> datetime:
    return datetime.fromisoformat(s)


def parse_list(text: str) -> List[str]:
    return [x.strip().upper() for x in text.replace("\n", " ").replace(",", " ").split(" ") if x.strip()]


def preseason_points(delta: int) -> int:
    d = abs(delta)
    if d == 0:
        return 6
    if d == 1:
        return 4
    if d == 2:
        return 2
    return 0


def session_points(delta: int) -> int:
    d = abs(delta)
    if d == 0:
        return 3
    if d == 1:
        return 2
    if d == 2:
        return 1
    return 0


def season_join_closed() -> bool:
    return now_utc() >= PRESEASON_LOCK_UTC


def preseason_locked() -> bool:
    return now_utc() >= PRESEASON_LOCK_UTC


def reminder_window_open(lock_time: datetime, offset: timedelta, next_offset: timedelta | None) -> bool:
    window_start = lock_time - offset
    window_end = lock_time - next_offset if next_offset else lock_time
    current_time = now_utc()
    return window_start <= current_time < window_end


def find_role_by_name(guild: discord.Guild, role_name: str):
    exact = discord.utils.get(guild.roles, name=role_name)
    if exact:
        return exact

    target = role_name.casefold()
    return discord.utils.find(lambda role: role.name.casefold() == target, guild.roles)


# =======================
# LOCAL BACKUP
# =======================
def create_local_backup() -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = now_utc().strftime("%Y%m%d_%H%M%S")
    backup_file = BACKUP_DIR / f"f1fantasy_backup_{timestamp}.db"

    with sqlite3.connect(DB_PATH) as source:
        with sqlite3.connect(str(backup_file)) as destination:
            source.backup(destination)

    return backup_file


async def backup_database_locally():
    if not DB_FILE.exists():
        return False, f"Database file not found at {DB_FILE}"

    try:
        backup_file = await asyncio.to_thread(create_local_backup)
        print(f"Database backup saved to {backup_file}")
        return True, str(backup_file)
    except Exception as e:
        print("Backup failed:", e)
        return False, str(e)


# =======================
# DB
# =======================
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
        PRAGMA journal_mode=WAL;

        CREATE TABLE IF NOT EXISTS players (
            user_id INTEGER PRIMARY KEY,
            display_name TEXT NOT NULL,
            registered INTEGER NOT NULL DEFAULT 0,
            registered_utc TEXT
        );

        CREATE TABLE IF NOT EXISTS events (
            season INTEGER NOT NULL,
            round INTEGER NOT NULL,
            session TEXT NOT NULL,
            start_utc TEXT NOT NULL,
            locked INTEGER NOT NULL DEFAULT 0,
            scored INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (season, round, session)
        );

        CREATE TABLE IF NOT EXISTS picks (
            season INTEGER NOT NULL,
            round INTEGER NOT NULL,
            session TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            pos INTEGER NOT NULL,
            driver TEXT NOT NULL,
            created_utc TEXT NOT NULL,
            PRIMARY KEY (season, round, session, user_id, pos)
        );

        CREATE TABLE IF NOT EXISTS results (
            season INTEGER NOT NULL,
            round INTEGER NOT NULL,
            session TEXT NOT NULL,
            pos INTEGER NOT NULL,
            driver TEXT NOT NULL,
            PRIMARY KEY (season, round, session, pos)
        );

        CREATE TABLE IF NOT EXISTS scores (
            season INTEGER NOT NULL,
            round INTEGER NOT NULL,
            session TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            points INTEGER NOT NULL,
            computed_utc TEXT NOT NULL,
            PRIMARY KEY (season, round, session, user_id)
        );

        CREATE TABLE IF NOT EXISTS preseason_picks (
            season INTEGER NOT NULL,
            category TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            pos INTEGER NOT NULL,
            item TEXT NOT NULL,
            created_utc TEXT NOT NULL,
            PRIMARY KEY (season, category, user_id, pos)
        );

        CREATE TABLE IF NOT EXISTS preseason_results (
            season INTEGER NOT NULL,
            category TEXT NOT NULL,
            pos INTEGER NOT NULL,
            item TEXT NOT NULL,
            PRIMARY KEY (season, category, pos)
        );

        CREATE TABLE IF NOT EXISTS preseason_scores (
            season INTEGER NOT NULL,
            category TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            points INTEGER NOT NULL,
            computed_utc TEXT NOT NULL,
            PRIMARY KEY (season, category, user_id)
        );

        CREATE TABLE IF NOT EXISTS reminders_sent (
            season INTEGER NOT NULL,
            round INTEGER NOT NULL,
            session TEXT NOT NULL,
            reminder_key TEXT NOT NULL,
            PRIMARY KEY (season, round, session, reminder_key)
        );
        """)
        await db.commit()


async def upsert_player(user: discord.abc.User):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO players(user_id, display_name) VALUES(?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET display_name=excluded.display_name",
            (user.id, user.display_name),
        )
        await db.commit()


async def set_registered(user_id: int, display_name: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO players(user_id, display_name, registered, registered_utc) VALUES(?, ?, 1, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET display_name=excluded.display_name, registered=1, registered_utc=excluded.registered_utc",
            (user_id, display_name, iso(now_utc())),
        )
        await db.commit()


async def is_registered(user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT registered FROM players WHERE user_id=?", (user_id,))
        row = await cur.fetchone()
        if row and row[0] == 1:
            return True

        cur = await db.execute("SELECT 1 FROM preseason_picks WHERE user_id=? LIMIT 1", (user_id,))
        if await cur.fetchone():
            return True

        cur = await db.execute("SELECT 1 FROM picks WHERE user_id=? LIMIT 1", (user_id,))
        if await cur.fetchone():
            return True

        cur = await db.execute("SELECT 1 FROM scores WHERE user_id=? LIMIT 1", (user_id,))
        if await cur.fetchone():
            return True

        cur = await db.execute("SELECT 1 FROM preseason_scores WHERE user_id=? LIMIT 1", (user_id,))
        if await cur.fetchone():
            return True

        return False


async def is_admin(interaction: discord.Interaction) -> bool:
    if not interaction.guild:
        return False

    member = interaction.user if isinstance(interaction.user, discord.Member) else None
    if member is None:
        try:
            member = await interaction.guild.fetch_member(interaction.user.id)
        except Exception:
            return False

    if member.guild_permissions.administrator or member.guild_permissions.manage_guild:
        return True

    return any(getattr(role, "name", "") == ADMIN_ROLE for role in getattr(member, "roles", []))


async def event_locked(season: int, round_: int, session: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT locked, start_utc FROM events WHERE season=? AND round=? AND session=?",
            (season, round_, session),
        )
        row = await cur.fetchone()
        if not row:
            return False
        locked, start_utc = row
        if locked:
            return True
        return now_utc() >= from_iso(start_utc)


async def reminder_sent(season: int, round_: int, session: str, reminder_key: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT 1 FROM reminders_sent WHERE season=? AND round=? AND session=? AND reminder_key=?",
            (season, round_, session, reminder_key),
        )
        return await cur.fetchone() is not None


async def mark_reminder_sent(season: int, round_: int, session: str, reminder_key: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO reminders_sent(season, round, session, reminder_key) VALUES(?, ?, ?, ?)",
            (season, round_, session, reminder_key),
        )
        await db.commit()


# =======================
# API
# =======================
async def jolpica_get_json(path: str) -> dict:
    url = f"{JOLPICA_BASE.rstrip('/')}/{path.lstrip('/')}"
    async with aiohttp.ClientSession() as session:
        async with session.get(url, timeout=30) as resp:
            resp.raise_for_status()
            return await resp.json()


async def sync_schedule(season: int):
    data = await jolpica_get_json(f"{season}.json")
    races = data["MRData"]["RaceTable"]["Races"]

    async with aiosqlite.connect(DB_PATH) as db:
        for r in races:
            rnd = int(r["round"])

            if "Qualifying" in r:
                q = r["Qualifying"]
                qdt = datetime.fromisoformat(f"{q['date']}T{q['time'].replace('Z', '+00:00')}")
                await db.execute(
                    "INSERT INTO events(season, round, session, start_utc, locked, scored) VALUES(?, ?, 'quali', ?, 0, 0) "
                    "ON CONFLICT(season, round, session) DO UPDATE SET start_utc=excluded.start_utc",
                    (season, rnd, iso(qdt)),
                )

            rdt = datetime.fromisoformat(f"{r['date']}T{r['time'].replace('Z', '+00:00')}")
            await db.execute(
                "INSERT INTO events(season, round, session, start_utc, locked, scored) VALUES(?, ?, 'race', ?, 0, 0) "
                "ON CONFLICT(season, round, session) DO UPDATE SET start_utc=excluded.start_utc",
                (season, rnd, iso(rdt)),
            )

        await db.commit()


async def fetch_results_order(season: int, round_: int, session: str) -> List[str]:
    if session == "quali":
        data = await jolpica_get_json(f"{season}/{round_}/qualifying.json")
        races = data["MRData"]["RaceTable"]["Races"]
        if not races:
            return []
        qresults = races[0].get("QualifyingResults", [])
        return [(x["Driver"].get("code") or x["Driver"]["driverId"]).upper() for x in qresults][:22]

    if session == "race":
        data = await jolpica_get_json(f"{season}/{round_}/results.json")
        races = data["MRData"]["RaceTable"]["Races"]
        if not races:
            return []
        results = races[0].get("Results", [])
        return [(x["Driver"].get("code") or x["Driver"]["driverId"]).upper() for x in results][:22]

    return []


# =======================
# SCORING
# =======================
async def compute_session_scores(season: int, round_: int, session: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT pos, driver FROM results WHERE season=? AND round=? AND session=? ORDER BY pos",
            (season, round_, session),
        )
        result_rows = await cur.fetchall()
        if not result_rows:
            return False

        actual_pos_of = {drv: pos for pos, drv in result_rows}

        cur = await db.execute(
            "SELECT DISTINCT user_id FROM picks WHERE season=? AND round=? AND session=?",
            (season, round_, session),
        )
        users = [r[0] for r in await cur.fetchall()]

        for uid in users:
            cur = await db.execute(
                "SELECT pos, driver FROM picks WHERE season=? AND round=? AND session=? AND user_id=? ORDER BY pos",
                (season, round_, session, uid),
            )
            picks = await cur.fetchall()
            if len(picks) < 22:
                continue

            pts = 0
            for pos, predicted_driver in picks:
                pd = predicted_driver.upper()
                if pd not in actual_pos_of:
                    continue
                pts += session_points(actual_pos_of[pd] - pos)

            await db.execute(
                "INSERT INTO scores(season, round, session, user_id, points, computed_utc) "
                "VALUES(?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(season, round, session, user_id) DO UPDATE SET points=excluded.points, computed_utc=excluded.computed_utc",
                (season, round_, session, uid, pts, iso(now_utc())),
            )

        await db.execute(
            "UPDATE events SET scored=1 WHERE season=? AND round=? AND session=?",
            (season, round_, session),
        )
        await db.commit()

    return True


async def compute_preseason_scores(season: int, category: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT pos, item FROM preseason_results WHERE season=? AND category=? ORDER BY pos",
            (season, category),
        )
        result_rows = await cur.fetchall()
        if not result_rows:
            return False

        actual_pos_of = {it.upper(): pos for pos, it in result_rows}

        cur = await db.execute(
            "SELECT DISTINCT user_id FROM preseason_picks WHERE season=? AND category=?",
            (season, category),
        )
        users = [r[0] for r in await cur.fetchall()]

        for uid in users:
            cur = await db.execute(
                "SELECT pos, item FROM preseason_picks WHERE season=? AND category=? AND user_id=? ORDER BY pos",
                (season, category, uid),
            )
            picks = await cur.fetchall()

            pts = 0
            for pos, item in picks:
                it = item.upper()
                if it not in actual_pos_of:
                    continue
                pts += preseason_points(actual_pos_of[it] - pos)

            await db.execute(
                "INSERT INTO preseason_scores(season, category, user_id, points, computed_utc) "
                "VALUES(?, ?, ?, ?, ?) "
                "ON CONFLICT(season, category, user_id) DO UPDATE SET points=excluded.points, computed_utc=excluded.computed_utc",
                (season, category, uid, pts, iso(now_utc())),
            )

        await db.commit()

    return True


# =======================
# REMINDERS / POSTS
# =======================
async def send_role_reminder(bot: discord.Client, message: str):
    if not REMINDER_CHANNEL_ID:
        return

    channel = bot.get_channel(REMINDER_CHANNEL_ID)
    if not channel:
        return

    role = find_role_by_name(channel.guild, REMINDER_ROLE)
    content = f"{role.mention}\n\n{message}" if role else message

    await channel.send(
        content,
        allowed_mentions=discord.AllowedMentions(roles=True),
    )


async def post_leaderboard(bot: discord.Client, season: int, round_: int, session: str):
    if not LEADERBOARD_CHANNEL_ID:
        return

    channel = bot.get_channel(LEADERBOARD_CHANNEL_ID)
    if not channel:
        return

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT p.display_name, s.points
            FROM scores s
            JOIN players p ON p.user_id=s.user_id
            WHERE s.season=? AND s.round=? AND s.session=?
            ORDER BY s.points DESC, p.display_name ASC
        """, (season, round_, session))
        rows = await cur.fetchall()

    if not rows:
        await channel.send(f"✅ {season} R{round_} **{session.upper()}** scored, but no picks were found.")
        return

    msg = "\n".join([f"**{i}. {name}** — {pts} pts" for i, (name, pts) in enumerate(rows, 1)])
    await channel.send(f"🏁 **{season} Round {round_} — {session.upper()} Leaderboard**\n{msg}")


async def get_overall_standings_rows(season: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT p.display_name, COALESCE(SUM(s.points), 0) AS pts
            FROM players p
            LEFT JOIN scores s ON s.user_id=p.user_id AND s.season=?
            WHERE p.registered=1
            GROUP BY p.user_id
            ORDER BY pts DESC, p.display_name ASC
        """, (season,))
        return await cur.fetchall()


async def post_overall_standings(bot: discord.Client, season: int, round_: int | None = None):
    if not LEADERBOARD_CHANNEL_ID:
        return

    channel = bot.get_channel(LEADERBOARD_CHANNEL_ID)
    if not channel:
        return

    rows = await get_overall_standings_rows(season)
    if not rows:
        await channel.send(f"🏆 **{season} Overall Standings**\nNo standings yet.")
        return

    msg = "\n".join([f"**{i}. {name}** — {pts} pts" for i, (name, pts) in enumerate(rows, 1)])
    if round_ is None:
        title = f"🏆 **{season} Overall Standings**"
    else:
        title = f"🏆 **{season} Overall Standings After Round {round_}**"

    await channel.send(f"{title}\n{msg}")


# =======================
# DISCORD CLIENT
# =======================
class Client(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        await init_db()

        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()

        asyncio.create_task(background_loop(self))


client = Client()


# =======================
# BACKGROUND LOOP
# =======================
async def background_loop(bot: discord.Client):
    await bot.wait_until_ready()
    last_backup = None

    while not bot.is_closed():
        try:
            # Local SQLite backup
            if not last_backup or now_utc() - last_backup > BACKUP_INTERVAL:
                await backup_database_locally()
                last_backup = now_utc()

            # Preseason reminders
            for index, (reminder_key, reminder_label, offset) in enumerate(REMINDER_SCHEDULE):
                next_offset = REMINDER_SCHEDULE[index + 1][2] if index + 1 < len(REMINDER_SCHEDULE) else None
                if not reminder_window_open(PRESEASON_LOCK_UTC, offset, next_offset):
                    continue

                if not await reminder_sent(LEAGUE_SEASON, 0, "preseason", reminder_key):
                    await send_role_reminder(
                        bot,
                        "⏰ **Preseason Reminder**\n"
                        f"Entries close in **{reminder_label}**.\n"
                        f"Use `/register_preseason season:{LEAGUE_SEASON} drivers:<22 drivers> constructors:<11 teams>`"
                    )
                    await mark_reminder_sent(LEAGUE_SEASON, 0, "preseason", reminder_key)

            # Load all events
            async with aiosqlite.connect(DB_PATH) as db:
                cur = await db.execute("SELECT season, round, session, start_utc, locked, scored FROM events")
                events = await cur.fetchall()

            # Auto-lock started events
            async with aiosqlite.connect(DB_PATH) as db:
                for season, rnd, sess, start_utc, locked, scored in events:
                    start_dt = from_iso(start_utc)
                    if not locked and now_utc() >= start_dt:
                        await db.execute(
                            "UPDATE events SET locked=1 WHERE season=? AND round=? AND session=?",
                            (season, rnd, sess),
                        )
                await db.commit()

            # Session reminders
            for season, rnd, sess, start_utc, locked, scored in events:
                start_dt = from_iso(start_utc)
                session_label = "Qualifying" if sess == "quali" else "Race"

                for index, (reminder_key, reminder_label, offset) in enumerate(REMINDER_SCHEDULE):
                    next_offset = REMINDER_SCHEDULE[index + 1][2] if index + 1 < len(REMINDER_SCHEDULE) else None
                    if not reminder_window_open(start_dt, offset, next_offset):
                        continue

                    if not await reminder_sent(season, rnd, sess, reminder_key):
                        await send_role_reminder(
                            bot,
                            f"⏰ **{session_label} Reminder**\n"
                            f"Round **{rnd} {session_label}** entries close in **{reminder_label}**.\n"
                            f"Lock in your prediction with:\n"
                            f"`/predict season:{season} round:{rnd} session:{sess} grid:<22 drivers>`"
                        )
                        await mark_reminder_sent(season, rnd, sess, reminder_key)

            # Auto-fetch and score
            async with aiosqlite.connect(DB_PATH) as db:
                cur = await db.execute("SELECT season, round, session, start_utc, scored FROM events WHERE scored=0")
                unscored_events = await cur.fetchall()

            for season, rnd, sess, start_utc, scored in unscored_events:
                start_dt = from_iso(start_utc)
                delay = SESSION_RESULT_DELAY.get(sess, timedelta(hours=3))

                if now_utc() < start_dt + delay:
                    continue

                order = await fetch_results_order(season, rnd, sess)
                if not order:
                    continue

                async with aiosqlite.connect(DB_PATH) as db:
                    await db.execute("DELETE FROM results WHERE season=? AND round=? AND session=?", (season, rnd, sess))
                    for i, drv in enumerate(order[:22], 1):
                        await db.execute(
                            "INSERT INTO results(season, round, session, pos, driver) VALUES(?, ?, ?, ?, ?)",
                            (season, rnd, sess, i, drv.upper()),
                        )
                    await db.execute(
                        "UPDATE events SET locked=1, scored=1 WHERE season=? AND round=? AND session=?",
                        (season, rnd, sess),
                    )
                    await db.commit()

                ok = await compute_session_scores(season, rnd, sess)
                if ok:
                    await post_leaderboard(bot, season, rnd, sess)
                    if sess == "race":
                        await post_overall_standings(bot, season, rnd)

        except Exception as e:
            print("BACKGROUND LOOP ERROR:", e)

        await asyncio.sleep(60)


# =======================
# EVENTS
# =======================
@client.event
async def on_ready():
    print(f"Logged in as {client.user} (id={client.user.id})")
    print(f"SQLite database: {DB_FILE}")


# =======================
# COMMANDS - USER
# =======================
@client.tree.command(name="register_preseason", description="Join the league by submitting preseason predictions")
@app_commands.describe(
    season="e.g. 2026",
    drivers="22 driver codes in order (P1..P22)",
    constructors="11 constructor codes in order (P1..P11)"
)
async def register_preseason(interaction: discord.Interaction, season: int, drivers: str, constructors: str):
    await upsert_player(interaction.user)

    if season_join_closed():
        return await interaction.response.send_message(
            f"🚫 Registration is closed. Lock was {PRESEASON_LOCK_BDT.strftime('%Y-%m-%d %I:%M %p')} BDT."
        )

    d = parse_list(drivers)
    c = parse_list(constructors)

    if len(d) != 22:
        return await interaction.response.send_message("Drivers preseason must have exactly **22** entries.")
    if len(c) != 11:
        return await interaction.response.send_message("Constructors preseason must have exactly **11** entries.")

    created = iso(now_utc())

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM preseason_picks WHERE season=? AND category='drivers' AND user_id=?", (season, interaction.user.id))
        await db.execute("DELETE FROM preseason_picks WHERE season=? AND category='constructors' AND user_id=?", (season, interaction.user.id))

        for i, item in enumerate(d, 1):
            await db.execute(
                "INSERT INTO preseason_picks(season, category, user_id, pos, item, created_utc) VALUES(?, 'drivers', ?, ?, ?, ?)",
                (season, interaction.user.id, i, item.upper(), created),
            )

        for i, item in enumerate(c, 1):
            await db.execute(
                "INSERT INTO preseason_picks(season, category, user_id, pos, item, created_utc) VALUES(?, 'constructors', ?, ?, ?, ?)",
                (season, interaction.user.id, i, item.upper(), created),
            )

        await db.commit()

    await set_registered(interaction.user.id, interaction.user.display_name)
    await interaction.response.send_message("✅ Registered! Your preseason predictions are saved.")


@client.tree.command(name="predict", description="Submit qualifying or race prediction (P1..P22)")
@app_commands.describe(
    season="e.g. 2026",
    round="e.g. 1",
    session="quali or race",
    grid="22 driver codes in order (P1..P22)"
)
async def predict(interaction: discord.Interaction, season: int, round: int, session: str, grid: str):
    session = session.lower().strip()

    if session not in SESSIONS:
        return await interaction.response.send_message("Session must be `quali` or `race`.")

    await upsert_player(interaction.user)

    if not await is_registered(interaction.user.id):
        return await interaction.response.send_message("🚫 You must join using `/register_preseason` first (before lock).")

    if await event_locked(season, round, session):
        return await interaction.response.send_message("🔒 This session is locked (started).")

    entries = parse_list(grid)
    if len(entries) != 22:
        return await interaction.response.send_message("You must provide exactly **22** entries (P1..P22).")

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT 1 FROM events WHERE season=? AND round=? AND session=?",
            (season, round, session),
        )
        if not await cur.fetchone():
            return await interaction.response.send_message("Event not found. Admin must run `/sync_schedule` first.")

        created = iso(now_utc())
        await db.execute(
            "DELETE FROM picks WHERE season=? AND round=? AND session=? AND user_id=?",
            (season, round, session, interaction.user.id),
        )

        for i, drv in enumerate(entries, 1):
            await db.execute(
                "INSERT INTO picks(season, round, session, user_id, pos, driver, created_utc) VALUES(?, ?, ?, ?, ?, ?, ?)",
                (season, round, session, interaction.user.id, i, drv.upper(), created),
            )

        await db.commit()

    await interaction.response.send_message(f"✅ Saved your **{session.upper()}** prediction for {season} Round {round}.")


@client.tree.command(name="standings", description="Overall standings (quali + race totals)")
async def standings(interaction: discord.Interaction, season: int):
    rows = await get_overall_standings_rows(season)

    if not rows:
        return await interaction.response.send_message("No standings yet.")

    msg = "\n".join([f"**{i}. {name}** — {pts} pts" for i, (name, pts) in enumerate(rows, 1)])
    await interaction.response.send_message(msg)


@client.tree.command(name="session_board", description="Leaderboard for a specific qualifying or race session")
async def session_board(interaction: discord.Interaction, season: int, round: int, session: str):
    session = session.lower().strip()

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT p.display_name, s.points
            FROM scores s
            JOIN players p ON p.user_id=s.user_id
            WHERE s.season=? AND s.round=? AND s.session=?
            ORDER BY s.points DESC, p.display_name ASC
        """, (season, round, session))
        rows = await cur.fetchall()

    if not rows:
        return await interaction.response.send_message("No scored results for that session yet.")

    msg = "\n".join([f"**{i}. {name}** — {pts} pts" for i, (name, pts) in enumerate(rows, 1)])
    await interaction.response.send_message(msg)


@client.tree.command(name="preseason_board", description="Preseason leaderboard")
async def preseason_board(interaction: discord.Interaction, season: int, category: str):
    category = category.lower().strip()

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT p.display_name, s.points
            FROM preseason_scores s
            JOIN players p ON p.user_id=s.user_id
            WHERE s.season=? AND s.category=? AND p.registered=1
            ORDER BY s.points DESC, p.display_name ASC
        """, (season, category))
        rows = await cur.fetchall()

    if not rows:
        return await interaction.response.send_message("No preseason scores yet.")

    msg = "\n".join([f"**{i}. {name}** — {pts} pts" for i, (name, pts) in enumerate(rows, 1)])
    await interaction.response.send_message(msg)


@client.tree.command(name="registered_players", description="Show all registered fantasy league players")
async def registered_players(interaction: discord.Interaction):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT display_name FROM players WHERE registered=1 ORDER BY display_name"
        )
        rows = await cur.fetchall()

    if not rows:
        return await interaction.response.send_message("No players have registered yet.")

    msg = "\n".join([f"{i}. {name}" for i, (name,) in enumerate(rows, 1)])
    await interaction.response.send_message(f"🏁 **Registered Players**\n{msg}")


# =======================
# COMMANDS - ADMIN
# =======================
@client.tree.command(name="sync_schedule", description="Admin: sync qualifying + race schedule for a season")
async def sync_schedule_cmd(interaction: discord.Interaction, season: int):
    if not await is_admin(interaction):
        return await interaction.response.send_message("Admin only.")
    await interaction.response.defer(thinking=True)
    await sync_schedule(season)
    await interaction.followup.send(f"✅ Synced quali + race schedule for {season}.")


@client.tree.command(name="admin_fetch_and_score", description="Admin: fetch results now and score")
async def admin_fetch_and_score(interaction: discord.Interaction, season: int, round: int, session: str):
    if not await is_admin(interaction):
        return await interaction.response.send_message("Admin only.")

    session = session.lower().strip()
    if session not in ("quali", "race"):
        return await interaction.response.send_message("Session must be `quali` or `race`.")

    await interaction.response.defer(thinking=True)
    order = await fetch_results_order(season, round, session)

    if not order:
        return await interaction.followup.send("No results available yet.")

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM results WHERE season=? AND round=? AND session=?", (season, round, session))
        for i, drv in enumerate(order[:22], 1):
            await db.execute(
                "INSERT INTO results(season, round, session, pos, driver) VALUES(?, ?, ?, ?, ?)",
                (season, round, session, i, drv.upper()),
            )
        await db.execute(
            "UPDATE events SET locked=1, scored=1 WHERE season=? AND round=? AND session=?",
            (season, round, session),
        )
        await db.commit()

    ok = await compute_session_scores(season, round, session)
    if ok:
        await interaction.followup.send(f"✅ Scored {season} Round {round} {session}.")
    else:
        await interaction.followup.send("Results stored but scoring failed.")


@client.tree.command(name="admin_set_preseason_results", description="Admin: set final preseason results and score")
async def admin_set_preseason_results(interaction: discord.Interaction, season: int, category: str, grid: str):
    if not await is_admin(interaction):
        return await interaction.response.send_message("Admin only.")

    category = category.lower().strip()
    if category not in ("drivers", "constructors"):
        return await interaction.response.send_message("Category must be `drivers` or `constructors`.")

    entries = parse_list(grid)
    expected = 22 if category == "drivers" else 11

    if len(entries) != expected:
        return await interaction.response.send_message(f"You must provide exactly **{expected}** entries.")

    await interaction.response.defer(thinking=True)

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM preseason_results WHERE season=? AND category=?", (season, category))
        for i, item in enumerate(entries, 1):
            await db.execute(
                "INSERT INTO preseason_results(season, category, pos, item) VALUES(?, ?, ?, ?)",
                (season, category, i, item.upper()),
            )
        await db.commit()

    ok = await compute_preseason_scores(season, category)
    if ok:
        await interaction.followup.send(f"✅ Preseason {category} scored.")
    else:
        await interaction.followup.send("No preseason results found.")


@client.tree.command(name="admin_register_player", description="Admin: manually register a player")
async def admin_register_player(interaction: discord.Interaction, user: discord.Member):
    if not await is_admin(interaction):
        return await interaction.response.send_message("Admin only.")

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO players(user_id, display_name, registered, registered_utc) VALUES(?, ?, 1, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET display_name=excluded.display_name, registered=1, registered_utc=excluded.registered_utc",
            (user.id, user.display_name, iso(now_utc())),
        )
        await db.commit()

    await interaction.response.send_message(f"✅ {user.display_name} registered.")


@client.tree.command(name="admin_unregister_player", description="Admin: remove a player from the league")
async def admin_unregister_player(interaction: discord.Interaction, user: discord.Member):
    if not await is_admin(interaction):
        return await interaction.response.send_message("Admin only.")

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE players SET registered=0 WHERE user_id=?", (user.id,))
        await db.execute("DELETE FROM preseason_picks WHERE user_id=?", (user.id,))
        await db.execute("DELETE FROM preseason_scores WHERE user_id=?", (user.id,))
        await db.execute("DELETE FROM picks WHERE user_id=?", (user.id,))
        await db.execute("DELETE FROM scores WHERE user_id=?", (user.id,))
        await db.commit()

    await interaction.response.send_message(f"🗑️ Removed {user.display_name} from league.")


@client.tree.command(name="admin_repair_registrations", description="Admin: restore registrations for all existing league players")
async def admin_repair_registrations(interaction: discord.Interaction):
    if not await is_admin(interaction):
        return await interaction.response.send_message("Admin only.")

    await interaction.response.defer(thinking=True)

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT DISTINCT user_id FROM (
                SELECT user_id FROM preseason_picks
                UNION
                SELECT user_id FROM picks
                UNION
                SELECT user_id FROM scores
                UNION
                SELECT user_id FROM preseason_scores
            )
        """)
        users = [row[0] for row in await cur.fetchall()]
        repaired = 0

        for user_id in users:
            cur = await db.execute("SELECT display_name FROM players WHERE user_id=?", (user_id,))
            row = await cur.fetchone()
            display_name = row[0] if row else f"User {user_id}"

            await db.execute(
                "INSERT INTO players(user_id, display_name, registered, registered_utc) VALUES(?, ?, 1, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET registered=1, registered_utc=excluded.registered_utc",
                (user_id, display_name, iso(now_utc())),
            )
            repaired += 1

        await db.commit()

    await interaction.followup.send(f"✅ Repaired registration for {repaired} players.")


@client.tree.command(name="admin_register_role_members", description="Admin: register everyone who has the fantasy player role")
async def admin_register_role_members(interaction: discord.Interaction):
    if not await is_admin(interaction):
        return await interaction.response.send_message("Admin only.")

    if not interaction.guild:
        return await interaction.response.send_message("This command must be used in a server.")

    role = find_role_by_name(interaction.guild, REMINDER_ROLE)
    if not role:
        return await interaction.response.send_message(f"Role `{REMINDER_ROLE}` not found.")

    await interaction.response.defer(thinking=True)
    registered_count = 0
    members = list(role.members)

    if not members:
        try:
            members = [member async for member in interaction.guild.fetch_members(limit=None) if role in member.roles]
        except discord.Forbidden:
            return await interaction.followup.send(
                "I need the Discord Server Members Intent enabled to read role members."
            )
        except discord.HTTPException as exc:
            return await interaction.followup.send(f"Could not fetch server members: {exc}")

    async with aiosqlite.connect(DB_PATH) as db:
        for member in members:
            if member.bot:
                continue

            await db.execute(
                "INSERT INTO players(user_id, display_name, registered, registered_utc) VALUES(?, ?, 1, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET display_name=excluded.display_name, registered=1, registered_utc=excluded.registered_utc",
                (member.id, member.display_name, iso(now_utc())),
            )
            registered_count += 1

        await db.commit()

    await interaction.followup.send(f"✅ Registered {registered_count} players from the `{REMINDER_ROLE}` role.")


@client.tree.command(
    name="admin_set_preseason_for_player",
    description="Admin: enter or overwrite a player's preseason predictions even after the lock"
)
@app_commands.describe(
    user="The player",
    season="e.g. 2026",
    drivers="22 driver codes in order (P1..P22)",
    constructors="11 constructor codes in order (P1..P11)"
)
async def admin_set_preseason_for_player(
    interaction: discord.Interaction,
    user: discord.Member,
    season: int,
    drivers: str,
    constructors: str
):
    if not await is_admin(interaction):
        return await interaction.response.send_message("Admin only.")

    d = parse_list(drivers)
    c = parse_list(constructors)

    if len(d) != 22:
        return await interaction.response.send_message("Drivers preseason must have exactly **22** entries.")
    if len(c) != 11:
        return await interaction.response.send_message("Constructors preseason must have exactly **11** entries.")

    created = iso(now_utc())

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO players(user_id, display_name, registered, registered_utc) VALUES(?, ?, 1, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET display_name=excluded.display_name, registered=1, registered_utc=excluded.registered_utc",
            (user.id, user.display_name, created),
        )

        await db.execute("DELETE FROM preseason_picks WHERE season=? AND category='drivers' AND user_id=?", (season, user.id))
        await db.execute("DELETE FROM preseason_picks WHERE season=? AND category='constructors' AND user_id=?", (season, user.id))
        await db.execute("DELETE FROM preseason_scores WHERE season=? AND user_id=?", (season, user.id))

        for i, item in enumerate(d, 1):
            await db.execute(
                "INSERT INTO preseason_picks(season, category, user_id, pos, item, created_utc) VALUES(?, 'drivers', ?, ?, ?, ?)",
                (season, user.id, i, item.upper(), created),
            )

        for i, item in enumerate(c, 1):
            await db.execute(
                "INSERT INTO preseason_picks(season, category, user_id, pos, item, created_utc) VALUES(?, 'constructors', ?, ?, ?, ?)",
                (season, user.id, i, item.upper(), created),
            )

        await db.commit()

    await interaction.response.send_message(f"✅ Saved preseason predictions for {user.display_name}.")


@client.tree.command(
    name="admin_set_prediction_for_player",
    description="Admin: enter or overwrite a player's qualifying or race prediction even after lock"
)
@app_commands.describe(
    user="The player",
    season="e.g. 2026",
    round="e.g. 1",
    session="quali or race",
    grid="22 driver codes in order (P1..P22)"
)
async def admin_set_prediction_for_player(
    interaction: discord.Interaction,
    user: discord.Member,
    season: int,
    round: int,
    session: str,
    grid: str
):
    if not await is_admin(interaction):
        return await interaction.response.send_message("Admin only.")

    session = session.lower().strip()
    if session not in ("quali", "race"):
        return await interaction.response.send_message("Session must be `quali` or `race`.")

    entries = parse_list(grid)
    if len(entries) != 22:
        return await interaction.response.send_message("You must provide exactly **22** entries.")

    created = iso(now_utc())

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT 1 FROM events WHERE season=? AND round=? AND session=?",
            (season, round, session),
        )
        if not await cur.fetchone():
            return await interaction.response.send_message("Event not found. Run `/sync_schedule` first.")

        await db.execute(
            "INSERT INTO players(user_id, display_name, registered, registered_utc) VALUES(?, ?, 1, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET display_name=excluded.display_name, registered=1, registered_utc=excluded.registered_utc",
            (user.id, user.display_name, created),
        )

        await db.execute(
            "DELETE FROM picks WHERE season=? AND round=? AND session=? AND user_id=?",
            (season, round, session, user.id),
        )
        await db.execute(
            "DELETE FROM scores WHERE season=? AND round=? AND session=? AND user_id=?",
            (season, round, session, user.id),
        )

        for i, drv in enumerate(entries, 1):
            await db.execute(
                "INSERT INTO picks(season, round, session, user_id, pos, driver, created_utc) VALUES(?, ?, ?, ?, ?, ?, ?)",
                (season, round, session, user.id, i, drv.upper(), created),
            )

        await db.commit()

    await interaction.response.send_message(
        f"✅ Saved {session.upper()} prediction for {user.display_name} in {season} Round {round}."
    )


@client.tree.command(name="admin_delete_prediction", description="Admin: delete a player's prediction")
async def admin_delete_prediction(interaction: discord.Interaction, user: discord.Member, season: int, round: int, session: str):
    if not await is_admin(interaction):
        return await interaction.response.send_message("Admin only.")

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM picks WHERE user_id=? AND season=? AND round=? AND session=?",
            (user.id, season, round, session),
        )
        await db.execute(
            "DELETE FROM scores WHERE user_id=? AND season=? AND round=? AND session=?",
            (user.id, season, round, session),
        )
        await db.commit()

    await interaction.response.send_message(f"🗑️ Deleted {user.display_name}'s prediction.")


@client.tree.command(name="admin_delete_preseason", description="Admin: delete a player's preseason predictions")
async def admin_delete_preseason(interaction: discord.Interaction, user: discord.Member, season: int):
    if not await is_admin(interaction):
        return await interaction.response.send_message("Admin only.")

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM preseason_picks WHERE user_id=? AND season=?", (user.id, season))
        await db.execute("DELETE FROM preseason_scores WHERE user_id=? AND season=?", (user.id, season))
        await db.commit()

    await interaction.response.send_message(f"🗑️ Deleted {user.display_name}'s preseason predictions for {season}.")


@client.tree.command(name="admin_backup_db", description="Admin: backup database locally")
async def admin_backup_db(interaction: discord.Interaction):
    if not await is_admin(interaction):
        return await interaction.response.send_message("Admin only.")

    await interaction.response.defer(thinking=True)
    ok, msg = await backup_database_locally()

    if ok:
        await interaction.followup.send(f"✅ Database backup saved locally: `{msg}`")
    else:
        await interaction.followup.send(f"❌ Backup failed: {msg}")


def main():
    if not DISCORD_TOKEN:
        raise RuntimeError("Missing DISCORD_TOKEN env var.")

    client.run(DISCORD_TOKEN)


if __name__ == "__main__":
    main()
