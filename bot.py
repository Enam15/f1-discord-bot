import os
import asyncio
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from typing import List

import aiohttp
import aiosqlite
import discord
from discord import app_commands

# =======================
# CONFIG
# =======================
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
ADMIN_ROLE = os.getenv("ADMIN_ROLE", "LeagueAdmin")
LEADERBOARD_CHANNEL_ID = os.getenv("LEADERBOARD_CHANNEL_ID")  # optional
JOLPICA_BASE = os.getenv("JOLPICA_BASE", "https://api.jolpi.ca/ergast/f1")
DB_PATH = "f1fantasy.db"

BDT = ZoneInfo("Asia/Dhaka")
# Preseason lock: March 5, 2026 11:59 PM BDT
PRESEASON_LOCK_BDT = datetime(2026, 3, 5, 23, 59, 0, tzinfo=BDT)
PRESEASON_LOCK_UTC = PRESEASON_LOCK_BDT.astimezone(timezone.utc)

SESSIONS = ("quali", "race")
SESSION_RESULT_DELAY = {"quali": timedelta(hours=2), "race": timedelta(hours=4)}


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
    raw = [x.strip().upper() for x in text.replace("\n", " ").replace(",", " ").split(" ") if x.strip()]
    return raw


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


async def jolpica_get_json(path: str) -> dict:
    url = f"{JOLPICA_BASE.rstrip('/')}/{path.lstrip('/')}"
    async with aiohttp.ClientSession() as session:
        async with session.get(url, timeout=30) as resp:
            resp.raise_for_status()
            return await resp.json()


# =======================
# DB
# =======================
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(
            """
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
        """
        )
        await db.commit()


async def upsert_player(user: discord.User):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO players(user_id, display_name) VALUES(?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET display_name=excluded.display_name",
            (user.id, user.display_name),
        )
        await db.commit()


async def set_registered(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE players SET registered=1, registered_utc=? WHERE user_id=?",
            (iso(now_utc()), user_id),
        )
        await db.commit()


async def is_registered(user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT registered FROM players WHERE user_id=?", (user_id,))
        row = await cur.fetchone()
        return bool(row and row[0] == 1)


async def is_admin(interaction: discord.Interaction) -> bool:
    if not interaction.guild:
        return False
    member = interaction.guild.get_member(interaction.user.id)
    if not member:
        return False
    return any(r.name == ADMIN_ROLE for r in member.roles)


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


# =======================
# SCHEDULE + RESULTS
# =======================
async def sync_schedule(season: int):
    data = await jolpica_get_json(f"{season}.json")
    races = data["MRData"]["RaceTable"]["Races"]
    async with aiosqlite.connect(DB_PATH) as db:
        for r in races:
            rnd = int(r["round"])

            if "Qualifying" in r:
                q = r["Qualifying"]
                qdt = datetime.fromisoformat(f"{q['date']}T{q['time'].replace('Z','+00:00')}")
                await db.execute(
                    "INSERT OR REPLACE INTO events(season, round, session, start_utc, locked, scored) "
                    "VALUES(?, ?, 'quali', ?, 0, 0)",
                    (season, rnd, iso(qdt)),
                )

            rdt = datetime.fromisoformat(f"{r['date']}T{r['time'].replace('Z','+00:00')}")
            await db.execute(
                "INSERT OR REPLACE INTO events(season, round, session, start_utc, locked, scored) "
                "VALUES(?, ?, 'race', ?, 0, 0)",
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
# DISCORD CLIENT
# =======================
class Client(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        await init_db()
        await self.tree.sync()
        asyncio.create_task(background_loop(self))


client = Client()


# =======================
# BACKGROUND LOOP
# =======================
async def post_leaderboard(bot: discord.Client, season: int, round_: int, session: str):
    if not LEADERBOARD_CHANNEL_ID:
        return
    channel = bot.get_channel(int(LEADERBOARD_CHANNEL_ID))
    if not channel:
        return

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """
            SELECT p.display_name, s.points
            FROM scores s
            JOIN players p ON p.user_id=s.user_id
            WHERE s.season=? AND s.round=? AND s.session=?
            ORDER BY s.points DESC, p.display_name ASC
            """,
            (season, round_, session),
        )
        rows = await cur.fetchall()

    if not rows:
        await channel.send(f"✅ {season} R{round_} **{session.upper()}** scored, but no picks found.")
        return

    lines = [f"**{i}. {name}** — {pts} pts" for i, (name, pts) in enumerate(rows, 1)]
    await channel.send(f"🏁 **{season} Round {round_} — {session.upper()} Leaderboard**\n" + "\n".join(lines))


async def background_loop(bot: discord.Client):
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            # Auto-lock sessions past start
            async with aiosqlite.connect(DB_PATH) as db:
                cur = await db.execute("SELECT season, round, session, start_utc, locked FROM events")
                for season, rnd, sess, start_utc, locked in await cur.fetchall():
                    if locked:
                        continue
                    if now_utc() >= from_iso(start_utc):
                        await db.execute(
                            "UPDATE events SET locked=1 WHERE season=? AND round=? AND session=?",
                            (season, rnd, sess),
                        )
                await db.commit()

            # Auto-fetch results for unscored sessions after delay
            async with aiosqlite.connect(DB_PATH) as db:
                cur = await db.execute("SELECT season, round, session, start_utc, scored FROM events WHERE scored=0")
                events = await cur.fetchall()

            for season, rnd, sess, start_utc, scored in events:
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
                        "UPDATE events SET locked=1 WHERE season=? AND round=? AND session=?",
                        (season, rnd, sess),
                    )
                    await db.commit()

                ok = await compute_session_scores(season, rnd, sess)
                if ok:
                    await post_leaderboard(bot, season, rnd, sess)

        except Exception:
            pass

        await asyncio.sleep(60)


# =======================
# COMMANDS
# =======================
@client.event
async def on_ready():
    print(f"Logged in as {client.user} (id={client.user.id})")


@client.tree.command(name="sync_schedule", description="(Admin) Sync qualifying + race calendar for a season")
async def sync_schedule_cmd(interaction: discord.Interaction, season: int):
    if not await is_admin(interaction):
        return await interaction.response.send_message("Admin only.")
    await interaction.response.defer(thinking=True)
    await sync_schedule(season)
    await interaction.followup.send(f"✅ Synced quali + race schedule for {season}.")


@client.tree.command(
    name="register_preseason",
    description="Join the league by submitting preseason predictions (required, closes at lock time)",
)
@app_commands.describe(
    season="e.g. 2026",
    drivers="22 driver codes in order (P1..P22), comma/space separated",
    constructors="11 constructor codes in order (P1..P11), comma/space separated",
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
        await db.execute(
            "DELETE FROM preseason_picks WHERE season=? AND category=? AND user_id=?",
            (season, "drivers", interaction.user.id),
        )
        await db.execute(
            "DELETE FROM preseason_picks WHERE season=? AND category=? AND user_id=?",
            (season, "constructors", interaction.user.id),
        )

        for i, item in enumerate(d, 1):
            await db.execute(
                "INSERT INTO preseason_picks(season, category, user_id, pos, item, created_utc) "
                "VALUES(?, 'drivers', ?, ?, ?, ?)",
                (season, interaction.user.id, i, item.upper(), created),
            )
        for i, item in enumerate(c, 1):
            await db.execute(
                "INSERT INTO preseason_picks(season, category, user_id, pos, item, created_utc) "
                "VALUES(?, 'constructors', ?, ?, ?, ?)",
                (season, interaction.user.id, i, item.upper(), created),
            )

        await db.commit()

    await set_registered(interaction.user.id)
    await interaction.response.send_message("✅ Registered! Your preseason predictions are saved.")


@client.tree.command(name="predict", description="Submit qualifying or race grid prediction (P1..P22)")
@app_commands.describe(
    season="e.g. 2026",
    round="e.g. 1",
    session="quali or race",
    grid="22 driver codes in order (P1..P22)",
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
                "INSERT INTO picks(season, round, session, user_id, pos, driver, created_utc) "
                "VALUES(?, ?, ?, ?, ?, ?, ?)",
                (season, round, session, interaction.user.id, i, drv.upper(), created),
            )
        await db.commit()

    await interaction.response.send_message(f"✅ Saved your **{session.upper()}** prediction for {season} Round {round}.")


@client.tree.command(name="standings", description="Overall standings (quali + race totals)")
async def standings(interaction: discord.Interaction, season: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """
            SELECT p.display_name, COALESCE(SUM(s.points),0) AS pts
            FROM players p
            LEFT JOIN scores s ON s.user_id=p.user_id AND s.season=?
            WHERE p.registered=1
            GROUP BY p.user_id
            ORDER BY pts DESC, p.display_name ASC
            """,
            (season,),
        )
        rows = await cur.fetchall()

    if not rows:
        return await interaction.response.send_message("No standings yet.")
    msg = "\n".join([f"**{i}. {name}** — {pts} pts" for i, (name, pts) in enumerate(rows, 1)])
    await interaction.response.send_message(msg)


@client.tree.command(name="session_board", description="Leaderboard for a specific qualifying/race session")
async def session_board(interaction: discord.Interaction, season: int, round: int, session: str):
    session = session.lower().strip()
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """
            SELECT p.display_name, s.points
            FROM scores s
            JOIN players p ON p.user_id=s.user_id
            WHERE s.season=? AND s.round=? AND s.session=?
            ORDER BY s.points DESC, p.display_name ASC
            """,
            (season, round, session),
        )
        rows = await cur.fetchall()

    if not rows:
        return await interaction.response.send_message("No scored results for that session yet.")
    msg = "\n".join([f"**{i}. {name}** — {pts} pts" for i, (name, pts) in enumerate(rows, 1)])
    await interaction.response.send_message(msg)


@client.tree.command(name="admin_fetch_and_score", description="(Admin) Fetch results now and score (quali/race)")
async def admin_fetch_and_score(interaction: discord.Interaction, season: int, round: int, session: str):
    if not await is_admin(interaction):
        return await interaction.response.send_message("Admin only.")
    session = session.lower().strip()
    if session not in SESSIONS:
        return await interaction.response.send_message("Session must be `quali` or `race`.")

    await interaction.response.defer(thinking=True)
    order = await fetch_results_order(season, round, session)
    if len(order) < 22:
        return await interaction.followup.send("No results available yet.")

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM results WHERE season=? AND round=? AND session=?", (season, round, session))
        for i, drv in enumerate(order[:22], 1):
            await db.execute(
                "INSERT INTO results(season, round, session, pos, driver) VALUES(?, ?, ?, ?, ?)",
                (season, round, session, i, drv.upper()),
            )
        await db.execute(
            "UPDATE events SET locked=1 WHERE season=? AND round=? AND session=?",
            (season, round, session),
        )
        await db.commit()

    ok = await compute_session_scores(season, round, session)
    if ok:
        await interaction.followup.send(f"✅ Scored {season} Round {round} {session}.")
    else:
        await interaction.followup.send("Results stored, but scoring failed (missing picks or results).")


@client.tree.command(
    name="admin_set_preseason_results",
    description="(Admin) Set final championship standings and score preseason (end of season)",
)
@app_commands.describe(
    season="e.g. 2026",
    category="drivers or constructors",
    grid="Final standings in order (22 drivers or 11 constructors)",
)
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
        await interaction.followup.send("No preseason results found to score.")


@client.tree.command(name="preseason_board", description="Preseason leaderboard (drivers or constructors)")
async def preseason_board(interaction: discord.Interaction, season: int, category: str):
    category = category.lower().strip()
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """
            SELECT p.display_name, s.points
            FROM preseason_scores s
            JOIN players p ON p.user_id=s.user_id
            WHERE s.season=? AND s.category=? AND p.registered=1
            ORDER BY s.points DESC, p.display_name ASC
            """,
            (season, category),
        )
        rows = await cur.fetchall()

    if not rows:
        return await interaction.response.send_message("No preseason scores yet.")
    msg = "\n".join([f"**{i}. {name}** — {pts} pts" for i, (name, pts) in enumerate(rows, 1)])
    await interaction.response.send_message(msg)


if not DISCORD_TOKEN:
    raise RuntimeError("Missing DISCORD_TOKEN env var.")

client.run(DISCORD_TOKEN)