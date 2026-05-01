# f1-discord-bot
# 🏁 F1 Fantasy League Discord Bot (2026)

A fully automated **Formula 1 Fantasy League bot for Discord** where players predict race and qualifying results and earn points based on prediction accuracy.
The bot manages registrations, predictions, scoring, reminders, and leaderboards throughout the season.

---

# 🚀 Features

### League Management

* Pre-season registration system
* Automatic lock after the registration deadline
* Admin ability to override locks and edit player data
* List all registered players

### Prediction System

Players predict the **entire grid (P1–P22)** for:

* Qualifying
* Race

Predictions are locked automatically when the session begins.

### Scoring System

Points are awarded based on prediction accuracy.

| Accuracy       | Points |
| -------------- | ------ |
| Exact position | 3      |
| ±1 position    | 2      |
| ±2 positions   | 1      |
| More than ±2   | 0      |

### Pre-Season Predictions

Players predict:

* **Drivers Championship (P1–P22)**
* **Constructors Championship (P1–P11)**

Scoring:

| Accuracy     | Points |
| ------------ | ------ |
| Exact        | 6      |
| ±1           | 4      |
| ±2           | 2      |
| More than ±2 | 0      |

---

# 🔔 Reminder System

The bot automatically reminds players to submit predictions.

| Event                  | Reminder Time                          |
| ---------------------- | -------------------------------------- |
| Preseason predictions  | 24 hours and 12 hours before lock      |
| Qualifying predictions | 24 hours and 12 hours before lock      |
| Race predictions       | 24 hours and 12 hours before lock      |

Reminders ping the configurable league role. The default role name is **f1**.

---

# 🤖 Automatic Race Result Scoring

The bot automatically:

1. Fetches official results from the **F1 Ergast/Jolpica API**
2. Stores results in the database
3. Calculates scores for all players
4. Posts a qualifying or race leaderboard in Discord
5. Posts overall standings after each race is scored

This happens automatically after:

* Qualifying (~2 hours later)
* Race (~4 hours later)

---

# 👑 Admin Controls

Admins can:

* Sync the full F1 schedule
* Manually fetch results
* Add or remove players
* Enter predictions for players
* Delete predictions
* Override preseason locks

Example admin commands:

```
/sync_schedule season:2026
/admin_fetch_and_score season:2026 round:5 session:race
/admin_set_prediction_for_player
/admin_set_preseason_for_player
/admin_delete_prediction
/admin_unregister_player
```

---

# 📊 League Commands

Players can use:

```
/register_preseason
/predict
/standings
/session_board
/preseason_board
/registered_players
```

---

# 💾 Local Data Storage

The bot stores league data locally in **SQLite**:

* player registrations
* predictions
* race results
* scores
* reminders

Default database file:

```
data/f1fantasy.db
```

Local backups are saved automatically every 6 hours and can also be created with `/admin_backup_db`.

Default backup folder:

```
C:\Users\musta\Documents\f1-discord-bot-backups
```

---

# 🖥 Local Server Setup

This version is set up to run from your local machine or local server. The bot reads config from a `.env` file in the project folder, then stores all submitted Discord inputs in the local SQLite database under `data/`.

Recommended local setup:

* Python 3.10+
* A virtual environment
* A `.env` file copied from `.env.example`
* Discord Developer Portal **Server Members Intent** enabled if you want `/admin_register_role_members`

If you run this on a home server, keep the project folder on a drive that is backed up. Do not delete the `data/` folder unless you want to remove the league database.

---

# ⚙️ Local Configuration

Create a `.env` file in this folder:

```
DISCORD_TOKEN=your_bot_token
ADMIN_ROLE=LeagueAdmin
REMINDER_ROLE=f1
LEADERBOARD_CHANNEL_ID=channel_id
REMINDER_CHANNEL_ID=channel_id
GUILD_ID=server_id
LEAGUE_SEASON=2026
DATA_DIR=data
# DB_PATH=data/f1fantasy.db
# BACKUP_DIR=C:\Users\musta\Documents\f1-discord-bot-backups
```

`DB_PATH` and `BACKUP_DIR` are optional. If you leave them unset, the bot uses `data/f1fantasy.db` and `C:\Users\musta\Documents\f1-discord-bot-backups`.

---

# 🛠 Installation

Open PowerShell in this project folder:

```
cd C:\Users\musta\Downloads\f1-discord-bot-main
```

Create and activate a virtual environment:

```
py -m venv .venv
.\.venv\Scripts\Activate.ps1
```

Install dependencies:

```
pip install -r requirements.txt
```

Run the bot:

```
python bot.py
```

You can also double-click `run_local.bat` after creating `.env` and installing dependencies once.

---

# 📅 First Time Setup

After the bot starts, run this in Discord:

```
/sync_schedule season:2026
```

This loads all qualifying and race sessions into the database.

To confirm data is local, check that this file exists after startup:

```
data/f1fantasy.db
```

---

# 🧠 Technologies Used

* Python
* discord.py
* SQLite
* aiohttp
* aiosqlite
* local `.env` config
* F1 Ergast / Jolpica API

---

# 📜 License

This project is open-source and free to modify for personal use.

---

# 🏎 Future Improvements

Potential upgrades:

* Web dashboard for standings
* Constructor scoring automation
* Prediction submission UI with dropdown menus
* Player-specific reminders
* Historical statistics

Created for a private **Formula 1 Fantasy League Discord community**.
