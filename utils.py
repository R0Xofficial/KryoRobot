# --- utils.py ---
import html
import database as db
import telegram
import aiosqlite
from telegram import Update
from telegram.constants import ChatType
from datetime import datetime, timezone

def safe_escape(text: str) -> str:
    return html.escape(str(text)).replace("&#x27;", "’")

async def create_user_link(user_id: int, context) -> str:
    try:
        chat = await context.bot.get_chat(user_id)
        name = chat.first_name or "User"
        return f'<a href="tg://user?id={user_id}">{safe_escape(name)}</a>'
    except:
        return f'Unknown User'

async def resolve_id(update: Update, context, input_str: str):
    input_str = input_str.strip()

    if update.message and update.message.entities:
        for entity in update.message.entities:
            if entity.type == 'text_mention' and entity.user:
                return entity.user.id, None

    if input_str.isdigit() or (input_str.startswith("-") and input_str[1:].isdigit()):
        uid = int(input_str)
        if uid < 0: 
            return None, "Chats/Channels cannot be globally banned."
        return uid, None

    if input_str.startswith("@"):
        db_id = await db.get_user_by_username(input_str)
        
        if db_id: 
            return db_id, None
            
        return None, (
            "I don't know who this user is. I've probably never seen them before. "
            "For me to see them, you'd have to reply to their message with this command "
            "or use user ID."
        )

    return None, "Invalid input. Please use a numeric ID or @username."

async def send_safe_reply(update: Update, context, text: str, **kwargs):
    try:
        return await update.message.reply_html(text, **kwargs)
    except telegram.error.BadRequest as e:
        if "Message to be replied not found" in str(e):
            return await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=text,
                parse_mode='HTML',
                **kwargs
            )
        raise e

def get_utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
