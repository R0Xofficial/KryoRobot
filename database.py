# --- database.py ---
import sqlite3
import aiosqlite
from datetime import datetime, timezone
from config import DB_NAME, OWNER_ID

def init_db():
    """Initializes the database and creates all necessary tables (Synchronous, run once at startup)."""
    with sqlite3.connect(DB_NAME) as conn:
        # Global Bans table
        conn.execute('''CREATE TABLE IF NOT EXISTS gbans 
                        (user_id INTEGER PRIMARY KEY, reason TEXT, admin_id INTEGER, date TEXT)''')
        
        # Chat Settings table (enforcement on/off)
        conn.execute('''CREATE TABLE IF NOT EXISTS bot_chats 
                        (chat_id INTEGER PRIMARY KEY, enforce_gban INTEGER DEFAULT 1)''')
        
        # Sudo Privileges table
        conn.execute('''CREATE TABLE IF NOT EXISTS sudo_users 
                        (user_id INTEGER PRIMARY KEY)''')
        
        # User Cache table (for resolving @usernames)
        conn.execute('''CREATE TABLE IF NOT EXISTS users 
                        (user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT)''')
        
        # Federation Mapping table (User <-> Chat relationship)
        conn.execute('''CREATE TABLE IF NOT EXISTS user_chats 
                        (user_id INTEGER, chat_id INTEGER, PRIMARY KEY (user_id, chat_id))''')

        # TABLE: Support team members
        conn.execute('CREATE TABLE IF NOT EXISTS support_users (user_id INTEGER PRIMARY KEY)')

        # TABLE: Pending ban/unban requests
        conn.execute('''CREATE TABLE IF NOT EXISTS support_requests (
                        request_id TEXT PRIMARY KEY, 
                        target_id INTEGER, 
                        reason TEXT, 
                        admin_id INTEGER, 
                        chat_id INTEGER, 
                        msg_id INTEGER, 
                        type TEXT,
                        thread_id,
                        target_msg_id INTEGER)''')
        conn.commit()

async def db_query(query, params=(), fetch=None, commit=False):
    """Universal ASYNC SQL engine to handle connections safely."""
    async with aiosqlite.connect(DB_NAME) as conn:
        async with conn.execute(query, params) as cursor:
            if commit:
                await conn.commit()
            
            if fetch == "one":
                return await cursor.fetchone()
            elif fetch == "all":
                return await cursor.fetchall()
            
            return cursor.rowcount

# --- LOGGERS (Data ingestion) ---

async def log_user(user_id, username, first_name):
    """Saves or updates user data in the local cache."""
    await db_query('INSERT OR REPLACE INTO users (user_id, username, first_name) VALUES (?, ?, ?)', 
                   (int(user_id), username.lower() if username else None, first_name), commit=True)

async def log_chat(chat_id):
    """Registers a chat in the database. Returns True if it's a new entry."""
    rowcount = await db_query("INSERT OR IGNORE INTO bot_chats (chat_id, enforce_gban) VALUES (?, ?)", 
                              (int(chat_id), 1), commit=True)
    return rowcount > 0

async def log_user_in_chat(user_id, chat_id):
    """Maps a user to a specific chat where they were seen."""
    await db_query('INSERT OR IGNORE INTO user_chats (user_id, chat_id) VALUES (?, ?)', 
                   (int(user_id), int(chat_id)), commit=True)

# --- GLOBAL BAN ENGINE ---

async def get_gban(user_id):
    """Fetches ban reason, admin ID, and date for a specific user."""
    return await db_query("SELECT reason, admin_id, date FROM gbans WHERE user_id = ?", 
                          (int(user_id),), fetch="one")

async def add_gban(user_id, admin_id, reason):
    """Adds or updates a Global Ban record."""
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    await db_query("INSERT OR REPLACE INTO gbans (user_id, reason, admin_id, date) VALUES (?, ?, ?, ?)", 
                   (int(user_id), reason, int(admin_id), date_str), commit=True)

async def remove_gban(user_id):
    """Removes a Global Ban record. Returns True if user was banned."""
    rowcount = await db_query("DELETE FROM gbans WHERE user_id = ?", (int(user_id),), commit=True)
    return rowcount > 0

# --- SUDO MANAGEMENT ---

async def is_sudo(user_id):
    """Checks if a user has sudo privileges or is the Owner."""
    if int(user_id) == OWNER_ID: return True
    res = await db_query("SELECT 1 FROM sudo_users WHERE user_id = ?", (int(user_id),), fetch="one")
    return res is not None

async def add_sudo(user_id):
    """Grants sudo privileges to a user."""
    await db_query("INSERT OR IGNORE INTO sudo_users (user_id) VALUES (?)", (int(user_id),), commit=True)

async def remove_sudo(user_id):
    """Revokes sudo privileges. Returns True if user existed in the list."""
    rowcount = await db_query("DELETE FROM sudo_users WHERE user_id = ?", (int(user_id),), commit=True)
    return rowcount > 0

async def get_all_sudos():
    """Returns a list of all sudo user IDs."""
    res = await db_query("SELECT user_id FROM sudo_users", fetch="all")
    return [row[0] for row in res]

# --- SUPPORT MANAGMENT ---

async def is_support(user_id):
    """Checks if a user has support privileges."""
    res = await db_query("SELECT 1 FROM support_users WHERE user_id = ?", (int(user_id),), fetch="one")
    return res is not None

async def add_support(user_id):
    """Grants support privileges to a user."""
    await db_query("INSERT OR IGNORE INTO support_users (user_id) VALUES (?)", (int(user_id),), commit=True)

async def remove_support(user_id):
    """Revokes support privileges. Returns True if user was in the list."""
    rowcount = await db_query("DELETE FROM support_users WHERE user_id = ?", (int(user_id),), commit=True)
    return rowcount > 0

async def get_all_supports():
    """Returns a list of all support user IDs."""
    res = await db_query("SELECT user_id FROM support_users", fetch="all")
    return [row[0] for row in res]

# --- CHAT & FEDERATION LOOKUP ---

async def is_enforced(chat_id):
    """Checks if gban enforcement is enabled for a specific chat."""
    res = await db_query("SELECT enforce_gban FROM bot_chats WHERE chat_id = ?", (int(chat_id),), fetch="one")
    return res[0] == 1 if res else True # Default to True for new chats

async def set_enforce(chat_id, status):
    """Enables (1) or disables (0) ban enforcement for a chat."""
    await db_query("INSERT OR REPLACE INTO bot_chats (chat_id, enforce_gban) VALUES (?, ?)", 
                   (int(chat_id), int(status)), commit=True)

async def get_user_seen_chats(user_id):
    """Federation: Fetches a list of chat IDs where the user was active."""
    res = await db_query('SELECT chat_id FROM user_chats WHERE user_id = ?', (int(user_id),), fetch="all")
    return [row[0] for row in res]

async def get_user_by_username(username):
    """Attempts to find a user ID in the cache by their @username."""
    username = username.lstrip('@').lower()
    res = await db_query("SELECT user_id FROM users WHERE username = ?", (username,), fetch="one")
    return res[0] if res else None

async def remove_chat(chat_id):
    """Removes a chat from settings and federation mapping (Cleanup)."""
    await db_query("DELETE FROM bot_chats WHERE chat_id = ?", (int(chat_id),), commit=True)
    await db_query("DELETE FROM user_chats WHERE chat_id = ?", (int(chat_id),), commit=True)

# --- SUPPORT REQUEST ENGINE ---

async def save_support_request(req_id, target_id, reason, admin_id, chat_id, msg_id, req_type, thread_id=None, target_msg_id=None):
    await db_query('''INSERT INTO support_requests VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''', 
                   (req_id, int(target_id), reason, int(admin_id), int(chat_id), int(msg_id), req_type, thread_id, target_msg_id), 
                   commit=True)

async def get_support_request(req_id):
    return await db_query("SELECT * FROM support_requests WHERE request_id = ?", (req_id,), fetch="one")
    
async def delete_support_request(req_id):
    """Removes a request from the database once processed or declined."""
    await db_query("DELETE FROM support_requests WHERE request_id = ?", (req_id,), commit=True)

async def import_gban(user_id, reason):
    date_str = utils.get_utc_now()
    await db_query(
        "INSERT OR IGNORE INTO gbans (user_id, reason, admin_id, date) VALUES (?, ?, ?, ?)",
        (int(user_id), reason, 0, date_str),
        commit=True
    )
