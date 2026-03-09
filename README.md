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

| Event                  | Reminder Time           |
| ---------------------- | ----------------------- |
| Preseason predictions  | 24 hours before lock    |
| Qualifying predictions | 24 hours before session |
| Race predictions       | 12 hours before session |

Reminders ping a configurable **FantasyPlayer role**.

---

# 🤖 Automatic Race Result Scoring

The bot automatically:

1. Fetches official results from the **F1 Ergast/Jolpica API**
2. Stores results in the database
3. Calculates scores for all players
4. Posts a leaderboard in Discord

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

# 💾 Data Storage

The bot uses **SQLite** for storing:

* player registrations
* predictions
* race results
* scores
* reminders

Database file:

```
f1fantasy.db
```

For production hosting, persistent storage is recommended.

---

# ☁️ Deployment

The bot can run on:

* Railway
* Oracle Cloud VM
* Any Linux server with Python

Recommended production setup:

* **Oracle Cloud Always Free VM**
* Persistent storage volume
* Python virtual environment
* Systemd service for automatic restart

---

# ⚙️ Environment Variables

Required environment variables:

```
DISCORD_TOKEN=your_bot_token
ADMIN_ROLE=LeagueAdmin
REMINDER_ROLE=FantasyPlayer
LEADERBOARD_CHANNEL_ID=channel_id
REMINDER_CHANNEL_ID=channel_id
GUILD_ID=server_id
DB_PATH=/data/f1fantasy.db
LEAGUE_SEASON=2026
```

---

# 🛠 Installation

Clone the repository:

```
git clone https://github.com/yourusername/f1-fantasy-bot.git
cd f1-fantasy-bot
```

Create virtual environment:

```
python3 -m venv .venv
source .venv/bin/activate
```

Install dependencies:

```
pip install -r requirements.txt
```

Run the bot:

```
python bot.py
```

---

# 📅 First Time Setup

After starting the bot, run:

```
/sync_schedule season:2026
```

This loads all qualifying and race sessions into the database.

---

# 🧠 Technologies Used

* Python
* discord.py
* SQLite
* aiohttp
* aiosqlite
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
