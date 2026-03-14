import os
import asyncio
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from typing import List

import aiohttp
import aiosqlite
import discord
from discord import app_commands

from pydrive2.auth import GoogleAuth
from pydrive2.drive import GoogleDrive
import json
import tempfile

GOOGLE_DRIVE_FOLDER_ID = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")


def get_drive():
    creds = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)

    with tempfile.NamedTemporaryFile(delete=False, mode="w") as f:
        json.dump(creds, f)
        key_path = f.name

    gauth = GoogleAuth()
    gauth.settings["client_config_backend"] = "service"
    gauth.settings["service_config"] = {
        "client_json_file_path": key_path,
        "client_user_email": creds["client_email"],
        "client_json_dict": creds,
    }

    gauth.ServiceAuth()
    return GoogleDrive(gauth)
# =======================
# CONFIG
# =======================
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
ADMIN_ROLE = os.getenv("ADMIN_ROLE", "LeagueAdmin")
REMINDER_ROLE = os.getenv("REMINDER_ROLE", "FantasyPlayer")

LEADERBOARD_CHANNEL_ID = int(os.getenv("LEADERBOARD_CHANNEL_ID")) if os.getenv("LEADERBOARD_CHANNEL_ID") else None
REMINDER_CHANNEL_ID = int(os.getenv("REMINDER_CHANNEL_ID")) if os.getenv("REMINDER_CHANNEL_ID") else None
GUILD_ID = int(os.getenv("GUILD_ID")) if os.getenv("GUILD_ID") else None

JOLPICA_BASE = os.getenv("JOLPICA_BASE", "https://api.jolpi.ca/ergast/f1")
DB_PATH = os.getenv("DB_PATH", "f1fantasy.db")

LEAGUE_SEASON = int(os.getenv("LEAGUE_SEASON", "2026"))

BDT = ZoneInfo("Asia/Dhaka")
PRESEASON_LOCK_BDT = datetime(2026, 3, 5, 23, 59, 0, tzinfo=BDT)
PRESEASON_LOCK_UTC = PRESEASON_LOCK_BDT.astimezone(timezone.utc)

SESSIONS = ("quali", "race")
SESSION_RESULT_DELAY = {
    "quali": timedelta(hours=2),
    "race": timedelta(hours=4),
}

PRESEASON_REMINDER_BEFORE = timedelta(hours=24)
QUALI_REMINDER_BEFORE = timedelta(hours=24)
RACE_REMINDER_BEFORE = timedelta(hours=12)


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
        # Normal registration check
        cur = await db.execute(
            "SELECT registered FROM players WHERE user_id=?",
            (user_id,)
        )
        row = await cur.fetchone()
        if row and row[0] == 1:
            return True

        # Fallback 1: preseason picks exist
        cur = await db.execute(
            "SELECT 1 FROM preseason_picks WHERE user_id=? LIMIT 1",
            (user_id,)
        )
        if await cur.fetchone():
            return True

        # Fallback 2: normal session picks exist
        cur = await db.execute(
            "SELECT 1 FROM picks WHERE user_id=? LIMIT 1",
            (user_id,)
        )
        if await cur.fetchone():
            return True

        # Fallback 3: scores exist
        cur = await db.execute(
            "SELECT 1 FROM scores WHERE user_id=? LIMIT 1",
            (user_id,)
        )
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
                    "INSERT OR REPLACE INTO events(season, round, session, start_utc, locked, scored) VALUES(?, ?, 'quali', ?, 0, 0)",
                    (season, rnd, iso(qdt)),
                )

            rdt = datetime.fromisoformat(f"{r['date']}T{r['time'].replace('Z', '+00:00')}")
            await db.execute(
                "INSERT OR REPLACE INTO events(season, round, session, start_utc, locked, scored) VALUES(?, ?, 'race', ?, 0, 0)",
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
        if len(result_rows) < 22:
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

    role = discord.utils.get(channel.guild.roles, name=REMINDER_ROLE)
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

async def backup_database_to_drive():

    if not GOOGLE_DRIVE_FOLDER_ID:
        return

    try:
        drive = get_drive()

        file = drive.CreateFile({
            "title": f"f1fantasy_backup_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.db",
            "parents": [{"id": GOOGLE_DRIVE_FOLDER_ID}]
        })

        file.SetContentFile(DB_PATH)
        file.Upload()

        print("Database backup uploaded to Google Drive")

    except Exception as e:
        print("Backup failed:", e)

# =======================
# DISCORD CLIENT
# =======================
class Client(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
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

    while not bot.is_closed():
        try:
            # Preseason 24h reminder
            preseason_reminder_time = PRESEASON_LOCK_UTC - PRESEASON_REMINDER_BEFORE
            if preseason_reminder_time <= now_utc() < PRESEASON_LOCK_UTC:
                if not await reminder_sent(LEAGUE_SEASON, 0, "preseason", "24h"):
                    await send_role_reminder(
                        bot,
                        "⏰ **Preseason Reminder**\n"
                        "There are **24 hours left** before preseason predictions lock.\n"
                        f"Use `/register_preseason season:{LEAGUE_SEASON} drivers:<22 drivers> constructors:<11 teams>`"
                    )
                    await mark_reminder_sent(LEAGUE_SEASON, 0, "preseason", "24h")

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

                if sess == "quali":
                    reminder_time = start_dt - QUALI_REMINDER_BEFORE
                    if reminder_time <= now_utc() < start_dt:
                        if not await reminder_sent(season, rnd, sess, "24h"):
                            await send_role_reminder(
                                bot,
                                f"⏰ **Qualifying Reminder**\n"
                                f"Round **{rnd} Qualifying** starts in **24 hours**.\n"
                                f"Lock in your prediction with:\n"
                                f"`/predict season:{season} round:{rnd} session:quali grid:<22 drivers>`"
                            )
                            await mark_reminder_sent(season, rnd, sess, "24h")

                elif sess == "race":
                    reminder_time = start_dt - RACE_REMINDER_BEFORE
                    if reminder_time <= now_utc() < start_dt:
                        if not await reminder_sent(season, rnd, sess, "12h"):
                            await send_role_reminder(
                                bot,
                                f"⏰ **Race Reminder**\n"
                                f"Round **{rnd} Race** starts in **12 hours**.\n"
                                f"Lock in your prediction with:\n"
                                f"`/predict season:{season} round:{rnd} session:race grid:<22 drivers>`"
                            )
                            await mark_reminder_sent(season, rnd, sess, "12h")

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
                if len(order) < 22:
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

        except Exception as e:
            print("BACKGROUND LOOP ERROR:", e)

        await asyncio.sleep(60)
last_backup = None
if not last_backup or now_utc() - last_backup > timedelta(hours=6):
    await backup_database_to_drive()
    last_backup = now_utc()

# =======================
# EVENTS
# =======================
@client.event
async def on_ready():
    print(f"Logged in as {client.user} (id={client.user.id})")


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
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT p.display_name, COALESCE(SUM(s.points), 0) AS pts
            FROM players p
            LEFT JOIN scores s ON s.user_id=p.user_id AND s.season=?
            WHERE p.registered=1
            GROUP BY p.user_id
            ORDER BY pts DESC, p.display_name ASC
        """, (season,))
        rows = await cur.fetchall()

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

    if len(order) < 22:
        return await interaction.followup.send("No results available yet.")

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM results WHERE season=? AND round=? AND session=?",
            (season, round, session),
        )

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

        await db.execute(
            "DELETE FROM preseason_results WHERE season=? AND category=?",
            (season, category),
        )

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

            cur = await db.execute(
                "SELECT display_name FROM players WHERE user_id=?",
                (user_id,),
            )

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

    await interaction.response.send_message(
        f"🗑️ Deleted {user.display_name}'s prediction."
    )

@client.tree.command(name="admin_register_role_members", description="Admin: register everyone who has the fantasy player role")
async def admin_register_role_members(interaction: discord.Interaction):
    if not await is_admin(interaction):
        return await interaction.response.send_message("Admin only.")

    if not interaction.guild:
        return await interaction.response.send_message("This command must be used in a server.")

    role = discord.utils.get(interaction.guild.roles, name=REMINDER_ROLE)

    if not role:
        return await interaction.response.send_message(f"Role `{REMINDER_ROLE}` not found.")

    await interaction.response.defer(thinking=True)

    registered_count = 0

    async with aiosqlite.connect(DB_PATH) as db:
        for member in role.members:
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

if not DISCORD_TOKEN:
    raise RuntimeError("Missing DISCORD_TOKEN env var.")

client.run(DISCORD_TOKEN)


