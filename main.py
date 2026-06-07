import logging
import asyncio
import time
import traceback
import io
import os
import sys
import subprocess
import aiosqlite
import telegram
import json
from telegram.error import Forbidden, BadRequest, RetryAfter, TimedOut
from datetime import datetime, timezone
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode, ChatType, ChatMemberStatus
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, ChatMemberHandler, ApplicationHandlerStop, CallbackQueryHandler
from telegram.request import HTTPXRequest

from config import TOKEN, OWNER_ID, LOG_CHAT_ID, APPEAL_CHAT_USERNAME, DB_NAME
import database as db
import utils
from handlers import bot_command, command_router

BOT_START_TIME = datetime.now(timezone.utc)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# --- HELPERS ---
async def register_user(user, chat, context):
    if user and user.id != context.bot.id:
        await db.log_user(user.id, user.username, user.first_name)
        if chat and chat.type in [ChatType.GROUP, ChatType.SUPERGROUP]:
            await db.log_chat(chat.id)
            await db.log_user_in_chat(user.id, chat.id)

async def support_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    admin = query.from_user
    
    if not await db.is_sudo(admin.id):
        await query.answer("Only Sudo or Owner can process requests.", show_alert=True)
        return

    try:
        action, req_id = query.data.split('_', 1)
    except ValueError:
        return

    req = await db.get_support_request(req_id)
    if not req:
        await query.answer("Request not found (likely already processed).")
        await query.delete_message()
        return

    _, target_id, reason, supporter_id, chat_id, msg_id, req_type, thread_id, target_msg_id = req
    
    user_link = await utils.create_user_link(target_id, context)
    supporter_link = await utils.create_user_link(supporter_id, context)
    approver_link = await utils.create_user_link(admin.id, context)
    curr_time = utils.get_utc_now()

    chat_obj = await context.bot.get_chat(chat_id)
        
    if chat_obj.type == ChatType.PRIVATE:
        supporter_obj = await context.bot.get_chat(supporter_id)
        chat_display = f"PM with {utils.safe_escape(supporter_obj.first_name)}"
    elif chat_obj.username:
        chat_link = f"https://t.me/{chat_obj.username}/{msg_id}"
        chat_display = f"<a href='{chat_link}'>{utils.safe_escape(chat_obj.title)}</a>"
    else:
        chat_display = utils.safe_escape(chat_obj.title)

    if action == "apr":
        if req_type in ["gban", "dgban"]:
            old_ban = await db.get_gban(target_id)
            
            await db.add_gban(target_id, supporter_id, reason)
            
            if req_type == "dgban":
                try: await context.bot.delete_message(chat_id=chat_id, message_id=target_msg_id)
                except: pass

            if await db.is_enforced(chat_id):
                try: await context.bot.ban_chat_member(chat_id, target_id)
                except: pass

            hashtag = "#GBANUPDATE" if old_ban else "#GBANNED"
            final_log = (f"<b>{hashtag}</b>\n"
                         f"<b>Initiated From:</b> {chat_display} [<code>{chat_id}</code>]\n\n"
                         f"<b>User:</b> {user_link} [<code>{target_id}</code>]\n"
                         f"<b>Reason:</b> <code>{utils.safe_escape(reason)}</code>\n")
            
            if old_ban: 
                final_log += f"<b>Old Reason:</b> <code>{utils.safe_escape(old_ban[0])}</code>\n"
            
            final_log += (f"<b>Date:</b> <code>{curr_time}</code>\n"
                          f"<b>Admin:</b> {supporter_link} [<code>{supporter_id}</code>]\n")
            
            await query.edit_message_text(final_log, parse_mode=ParseMode.HTML)
            
            if old_ban:
                feedback = (f"Done! Gban reason updated.\n"
                            f"<b>Old Reason:</b> <code>{utils.safe_escape(old_ban[0])}</code>")
            else:
                feedback = "Done! Gbanned."
            
            try:
                if chat_obj.type == ChatType.PRIVATE:
                    await context.bot.send_message(
                        chat_id=chat_id, 
                        text=feedback, 
                        parse_mode=ParseMode.HTML
                    )
                else:
                    await context.bot.send_message(
                        chat_id=chat_id, text=feedback, parse_mode=ParseMode.HTML,
                        reply_to_message_id=msg_id
                    )
            except:
                try:
                    await context.bot.send_message(
                        chat_id=chat_id, text=feedback, 
                        parse_mode=ParseMode.HTML, message_thread_id=thread_id
                    )
                except: pass

        elif req_type == "ungban":
            if await db.remove_gban(target_id):
                is_origin_private = (chat_obj.type == ChatType.PRIVATE)
                final_log = (f"<b>#UNGBANNED</b>\n"
                             f"<b>Initiated From:</b> {chat_display} [<code>{chat_id}</code>]\n\n"
                             f"<b>User:</b> {user_link} [<code>{target_id}</code>]\n"
                             f"<b>Reason:</b> <code>{utils.safe_escape(reason)}</code>\n"
                             f"<b>Date:</b> <code>{curr_time}</code>\n"
                             f"<b>Admin:</b> {supporter_link} [<code>{supporter_id}</code>]\n")
                
                await query.edit_message_text(final_log, parse_mode=ParseMode.HTML)
                
                context.job_queue.run_once(propagate_unban, when=1, data={
                    'user_id': target_id, 
                    'chat_id': chat_id, 
                    'reply_to': msg_id,
                    'thread_id': thread_id, 
                    'is_private': is_origin_private
                })
                
    else:
        await query.delete_message()
        decline_text = "Request Declined."
        try:
            if chat_obj.type == ChatType.PRIVATE:
                await context.bot.send_message(
                    chat_id=chat_id, 
                    text=decline_text, 
                    parse_mode=ParseMode.HTML
                )
            else:
                await context.bot.send_message(
                    chat_id=chat_id, text=decline_text, parse_mode=ParseMode.HTML,
                    reply_to_message_id=msg_id
                )
        except:
            try:
                await context.bot.send_message(
                    chat_id=chat_id, text=decline_text, 
                    parse_mode=ParseMode.HTML, message_thread_id=thread_id
                )
            except: pass

    await db.delete_support_request(req_id)
    await query.answer("Action processed.")

# --- LOGGERS ---
async def passive_data_logger(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Quietly records user and chat data to support global ban efficiency."""
    user = update.effective_user
    chat = update.effective_chat
    
    await register_user(user, chat, context)
        
# --- PROTECTION LOGIC ---

async def gban_enforcer_action(user, chat, update: Update, context: ContextTypes.DEFAULT_TYPE, send_alert: bool = True):
    """Internal helper to execute the ban and send alerts."""
    ban_info = await db.get_gban(user.id)
    if ban_info:
        try:
            # 1. Ban the user technically across Telegram
            await context.bot.ban_chat_member(chat.id, user.id)
            
            # 2. Send alert message only if requested (usually on Join or Message)
            if send_alert:
                user_link = await utils.create_user_link(user.id, context)
                msg = (f"<b>Alert!</b> Detected globally banned user.\n"
                       f"<code>I banned him here!</code>\n"
                       f"<b>User:</b> {user_link} [<code>{user.id}</code>]\n"
                       f"<b>Reason:</b> <code>{utils.safe_escape(ban_info[0])}</code>\n"
                       f"<b>Appeal Chat:</b> {APPEAL_CHAT_USERNAME}")
                
                # Send as a fresh message to avoid "Message not found" errors
                await context.bot.send_message(chat.id, msg, parse_mode=ParseMode.HTML)
            
            # 3. Stop processing other handlers (Security Layering)
            raise ApplicationHandlerStop()
        except ApplicationHandlerStop:
            raise
        except Exception as e:
            logger.error(f"Enforcer execution failed in {chat.id}: {e}")

async def enforcer_radar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Radar: Detects joins/leaves AND logs clean users for the federation map."""
    result = update.chat_member
    if not result: return
    
    chat = update.effective_chat
    # 1. Registration check
    if not await db.is_enforced(chat.id): 
        return

    status_before = result.old_chat_member.status
    status_after = result.new_chat_member.status
    user = result.new_chat_member.user

    if user.id == context.bot.id: 
        return

    is_joining = (status_after == ChatMemberStatus.MEMBER and status_before != ChatMemberStatus.MEMBER)
    is_leaving = (status_after == ChatMemberStatus.LEFT and status_before == ChatMemberStatus.MEMBER)
    is_banned = (status_after == ChatMemberStatus.BANNED and status_before != ChatMemberStatus.BANNED)

    if not (is_joining or is_leaving or is_banned): 
        return

    if is_joining or is_leaving or is_banned:
        await register_user(user, chat, context)

    # 2. Check for Global Ban
    ban_info = await db.get_gban(user.id)
    if ban_info:
        try:
            if is_joining or is_leaving:
                await gban_enforcer_action(user, chat, update, context, send_alert=True)
            
            # Stop the process if banned
            raise ApplicationHandlerStop()
        except ApplicationHandlerStop: raise
        except: pass

async def enforcer_message_checker(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Checker: Bans users who are already in chat and try to speak."""
    chat = update.effective_chat
    user = update.effective_user
    
    if not chat or chat.type == ChatType.PRIVATE or not user:
        return
    
    # Ignore system service messages to prevent duplicate alerts
    if update.message and (update.message.new_chat_members or update.message.left_chat_member):
        return

    if user.id == context.bot.id:
        return

    await register_user(user, chat, context)

    if not await db.is_enforced(chat.id):
        return

    ban_info = await db.get_gban(user.id)
    if ban_info:
        if update.effective_message:
            try: await update.effective_message.delete()
            except: pass
        await gban_enforcer_action(user, chat, update, context, send_alert=True)

# --- COMMANDS ---

async def ignore_edited_commands(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info(f"Ignoring edited command: {update.edited_message.text}")
    raise ApplicationHandlerStop

async def ignore_old_updates(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_message:
        return

    message_date = update.effective_message.date
    current_time = datetime.now(timezone.utc)

    if (current_time - message_date).total_seconds() > 60:
        logger.info(f"Skipped old update from chat {update.effective_chat.id} (Sent {int((current_time - message_date).total_seconds())}s ago)")
        
        raise ApplicationHandlerStop

async def send_startup_log(context: ContextTypes.DEFAULT_TYPE):
    if LOG_CHAT_ID:
        try:
            await context.bot.send_message(LOG_CHAT_ID, "Started")
        except Exception as e:
            logger.error(f"Failed to send startup log: {e}")

@bot_command("start")
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat

    if chat.type != ChatType.PRIVATE:
        return
    
    welcome_text = (
        f"👋 <b>Hello, {utils.safe_escape(user.first_name)}!</b>\n\n"
        f"I am a specialized <b>Security Group System</b> bot. My mission is to protect your groups from spammers, scammers, and malicious entities.\n\n"
        f"<b>How it works:</b>\n"
        f"Once a user is globally banned by my administrators, they will be automatically removed from all groups where I am present.\n\n"
        f"<b>Getting Started:</b>\n"
        f"To see all available commands and manage your settings, use the: <code>/help</code> command.\n\n"
        f"<i>Make sure to add me as an administrator with 'Ban Users' and 'Delete Messages' permissions to ensure full protection.</i>"
    )

    await utils.send_safe_reply(update, context, welcome_text)

@bot_command("help")
async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    is_support = await db.is_support(user_id)
    is_sudo = await db.is_sudo(user_id)
    is_owner = (user_id == OWNER_ID)

    help_parts = [
        "<b>Bot Help</b>\n",
        "<b>User Commands:</b>",
        "• <code>/start</code> - Sends start message.",
        "• <code>/help</code> - Sends this help message.",
        "• <code>/ping</code> - Check bot latency.",
        "• <code>/uptime</code> - See how long bot is running.",
        "• <code>/enforcegban &lt;on/off&gt;</code> - Toggle protection on chat.",
        "• <code>/gbanstat</code> - Check your own ban status.\n"
    ]

    if is_support or is_sudo:
        help_parts.extend([
            "<b>Support Commands:</b>",
            "• <code>/gban &lt;target&gt; &lt;reason&gt;</code> - Issue a global ban.",
            "• <code>/dgban &lt;reply&gt; &lt;reason&gt;</code> - Issue a global ban and delete message.",
            "• <code>/ungban &lt;target&gt; &lt;reason&gt;</code> - Revoke a global ban.",
            "• <code>/gbanstat &lt;target&gt;</code> - Check user's detailed ban info.",
            "• <code>/stats</code> - View database statistics.\n",
        ])

    if is_sudo:
        help_parts.extend([
            "<b>Sudo Commands:</b>",
            "• <code>/sudolist</code> - Show all bot sudos.",
            "• <code>/supportlist</code> - Show all bot support users.",
            "• <code>/leave</code> - Bot leaving current chat.\n"
        ])

    if is_owner:
        help_parts.extend([
            "<b>Master Owner Commands:</b>",
            "• <code>/addsudo &lt;target&gt;</code> - Grant sudo privileges.",
            "• <code>/delsudo &lt;target&gt;</code> - Revoke sudo privileges.",
            "• <code>/addsupport &lt;target&gt;</code> - Grant support privileges.",
            "• <code>/delsupport &lt;target&gt;</code> - Revoke support privileges.",
            "• <code>/cleanup</code> - Remove inactive chats from database.",
            "• <code>/restart</code> - Restart bot process.",
            "• <code>/update</code> - Update bot from Git.",
            "• <code>/restore</code> - Restore database from file.",
            "• <code>/backup</code> - Get the latest database file.",
            "• <code>/importbans</code> - Import bans from json file to bot database.\n"
        ])

    help_parts.append("<i>You can use '/' or '!' as a prefix for all commands.</i>")

    final_text = "\n".join(help_parts)
    
    try:
        await utils.send_safe_reply(update, context, final_text)
    except Exception as e:
        logger.error(f"Help HTML Error: {e}")
        await update.message.reply_text("Error: There is a formatting issue in the help message.")

@bot_command("ping")
async def ping_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    start_time = time.time()
    
    message = await update.message.reply_text("Pinging...")
    end_time = time.time()
    latency = round((end_time - start_time) * 100, 2)
    
    await message.edit_text(
        f"<b>Pong!</b>\n"
        f"<b>Latency:</b> <code>{latency} ms</code>",
        parse_mode=ParseMode.HTML
    )

async def get_readable_time(seconds: int) -> str:
    count = 0
    periods = [('d', 86400), ('h', 3600), ('m', 60), ('s', 1)]
    result = []
    for period_name, period_seconds in periods:
        if seconds >= period_seconds:
            period_value, seconds = divmod(seconds, period_seconds)
            result.append(f"{period_value}{period_name}")
    return ", ".join(result) if result else "0s"

@bot_command("uptime")
async def uptime_command(update: Update, context: ContextTypes.DEFAULT_TYPE):    
    current_time = datetime.now(timezone.utc)
    uptime_seconds = int((current_time - BOT_START_TIME).total_seconds())
    readable_uptime = await get_readable_time(uptime_seconds)
    
    await utils.send_safe_reply(update, context, 
        f"<b>Bot Uptime</b>\n"
        f"<b>Running for:</b> <code>{readable_uptime}</code>"
    )

@bot_command("gban")
async def gban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin = update.effective_user
    chat = update.effective_chat
    
    is_sudo = await db.is_sudo(admin.id)
    is_support = await db.is_support(admin.id)
    
    if not (is_sudo or is_support):
        return

    target_id, reason = None, None
    if update.message.reply_to_message and not update.message.reply_to_message.forum_topic_created:
        target_id = update.message.reply_to_message.from_user.id
        reason = " ".join(context.args) if context.args else None
    elif context.args:
        target_id, err = await utils.resolve_id(update, context, context.args[0])
        if err: 
            await update.message.reply_text(err)
            return
        reason = " ".join(context.args[1:]) if len(context.args) > 1 else None

    if not target_id:
        await update.message.reply_text("Who is the target of the command? The stars in the sky?"); return
    if not reason:
        await update.message.reply_text("Give a reason!"); return
    if await db.is_sudo(target_id) or await db.is_support(target_id) or target_id == context.bot.id:
        await update.message.reply_text("LoL, looks like... Someone tried gban privileged user. Nice Try."); return

    if chat.type == ChatType.PRIVATE:
        chat_display = f"PM with {utils.safe_escape(admin.first_name)}"
    elif chat.username:
        chat_link = f"https://t.me/{chat.username}/{update.effective_message.message_id}"
        chat_display = f"<a href='{chat_link}'>{utils.safe_escape(chat.title)}</a>"
    else:
        chat_display = utils.safe_escape(chat.title)

    await utils.send_safe_reply(update, context, "Ok!")

    old_ban = await db.get_gban(target_id)

    if is_sudo:
        if old_ban:
            if old_ban[0].strip() == reason.strip():
                user_link = await utils.create_user_link(target_id, context)
                await utils.send_safe_reply(update, context, f"User {user_link} [<code>{target_id}</code>] is already globally banned for the same reason. <b>No changes made.</b>")
                return
        
        await db.add_gban(target_id, admin.id, reason)
        if chat.type != ChatType.PRIVATE and await db.is_enforced(chat.id):
            try: await context.bot.ban_chat_member(chat.id, target_id)
            except: pass

        user_link = await utils.create_user_link(target_id, context)
        admin_link = await utils.create_user_link(admin.id, context)
        curr_time = utils.get_utc_now()
        hashtag = "#GBANUPDATE" if old_ban else "#GBANNED"

        log_msg = (f"<b>{hashtag}</b>\n"
                   f"<b>Initiated From:</b> {chat_display} [<code>{chat.id}</code>]\n\n"
                   f"<b>User:</b> {user_link} [<code>{target_id}</code>]\n"
                   f"<b>Reason:</b> <code>{utils.safe_escape(reason)}</code>\n")
        if old_ban: log_msg += f"<b>Old Reason:</b> <code>{utils.safe_escape(old_ban[0])}</code>\n"
        log_msg += f"<b>Date:</b> <code>{curr_time}</code>\n<b>Admin:</b> {admin_link} [<code>{admin.id}</code>]"
        
        if LOG_CHAT_ID: await context.bot.send_message(LOG_CHAT_ID, log_msg, parse_mode=ParseMode.HTML)
        await asyncio.sleep(0.5)
        if old_ban:
            await utils.send_safe_reply(update, context, f"Done! Gban reason updated.\n<b>Old Reason:</b> <code>{utils.safe_escape(old_ban[0])}</code>")
        else:
            await utils.send_safe_reply(update, context, f"Done! Gbanned.")

    elif is_support:
        request_id = f"g_{target_id}_{int(time.time())}"

        await db.save_support_request(
            request_id, target_id, reason, admin.id, 
            chat.id, update.effective_message.message_id, "gban",
            update.effective_message.message_thread_id, None
        )
        
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("Approve", callback_data=f"apr_{request_id}"),
            InlineKeyboardButton("Decline", callback_data=f"dec_{request_id}")
        ]])
        
        user_link = await utils.create_user_link(target_id, context)
        supporter_link = await utils.create_user_link(admin.id, context)
        hashtag = "#GBANUPDATEREQUEST" if old_ban else "#GBANREQUEST"
        
        req_msg = (f"<b>{hashtag}</b>\n"
                   f"<b>Initiated From:</b> {chat_display} [<code>{chat.id}</code>]\n\n"
                   f"<b>User:</b> {user_link} [<code>{target_id}</code>]\n"
                   f"<b>Reason:</b> <code>{reason}</code>\n")
        if old_ban: req_msg += f"<b>Old Reason:</b> <code>{utils.safe_escape(old_ban[0])}</code>\n"
        req_msg += f"<b>Admin:</b> {supporter_link} [<code>{admin.id}</code>]"
        
        if LOG_CHAT_ID:
            await context.bot.send_message(LOG_CHAT_ID, req_msg, reply_markup=keyboard, parse_mode=ParseMode.HTML)
        
        await update.message.reply_text("Request Sended.")

@bot_command("dgban")
async def dgban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin = update.effective_user
    chat = update.effective_chat
    
    is_sudo = await db.is_sudo(admin.id)
    is_support = await db.is_support(admin.id)
    
    if not (is_sudo or is_support): 
        return

    if not update.message.reply_to_message or update.message.reply_to_message.forum_topic_created:
        await update.message.reply_text("Who is the target of the command? The stars in the sky?")
        return

    target_id = update.message.reply_to_message.from_user.id
    msg_text = update.effective_message.text_html
    reason = msg_text.split(None, 1)[1] if len(msg_text.split()) > 1 else None

    if await db.is_sudo(target_id) or await db.is_support(target_id) or target_id == context.bot.id:
        await update.message.reply_text("LoL, looks like... Someone tried gban privileged user. Nice Try."); return
    if not reason:
        await update.message.reply_text("Give a reason!"); return

    if chat.type == ChatType.PRIVATE:
            chat_display = f"PM with {utils.safe_escape(admin.first_name)}"
    elif chat.username:
        chat_link = f"https://t.me/{chat.username}/{update.effective_message.message_id}"
        chat_display = f"<a href='{chat_link}'>{utils.safe_escape(chat.title)}</a>"
    else:
        chat_display = utils.safe_escape(chat.title)

    await utils.send_safe_reply(update, context, "Ok!")

    old_ban = await db.get_gban(target_id)
    
    if is_sudo:
        if old_ban and old_ban[0].strip() == reason.strip():
            user_link = await utils.create_user_link(target_id, context)
            await utils.send_safe_reply(update, context, f"User {user_link} [<code>{target_id}</code>] is already globally banned for the same reason.")
            return

        try:
            await update.message.reply_to_message.delete()
        except:
            pass
        
        await db.add_gban(target_id, admin.id, reason)
        if chat.type != ChatType.PRIVATE and await db.is_enforced(chat.id):
            try: await context.bot.ban_chat_member(chat.id, target_id)
            except: pass

        user_link = await utils.create_user_link(target_id, context)
        admin_link = await utils.create_user_link(admin.id, context)
        curr_time = utils.get_utc_now()
        hashtag = "#GBANUPDATE" if old_ban else "#GBANNED"

        log_msg = (f"<b>{hashtag}</b>\n"
                   f"<b>Initiated From:</b> {chat_display} [<code>{chat.id}</code>]\n\n"
                   f"<b>User:</b> {user_link} [<code>{target_id}</code>]\n"
                   f"<b>Reason:</b> <code>{utils.safe_escape(reason)}</code>\n")
        if old_ban: log_msg += f"<b>Old Reason:</b> <code>{utils.safe_escape(old_ban[0])}</code>\n"
        log_msg += f"<b>Date:</b> <code>{curr_time}</code>\n<b>Admin:</b> {admin_link} [<code>{admin.id}</code>]"
        
        if LOG_CHAT_ID: await context.bot.send_message(LOG_CHAT_ID, log_msg, parse_mode=ParseMode.HTML)
        await asyncio.sleep(0.5)
        if old_ban:
            await utils.send_safe_reply(update, context, f"Done! Gban reason updated.\n<b>Old Reason:</b> <code>{utils.safe_escape(old_ban[0])}</code>")
        else:
            await utils.send_safe_reply(update, context, f"Done! Gbanned.")

    elif is_support:
        request_id = f"d_{target_id}_{int(time.time())}"

        cmd_id = update.effective_message.message_id
        target_msg_id = update.message.reply_to_message.message_id

        await db.save_support_request(
            request_id, target_id, reason, admin.id, 
            chat.id, cmd_id, "dgban",
            update.effective_message.message_thread_id,
            target_msg_id
        )
        
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("Approve", callback_data=f"apr_{request_id}"),
            InlineKeyboardButton("Decline", callback_data=f"dec_{request_id}")
        ]])
        
        user_link = await utils.create_user_link(target_id, context)
        supporter_link = await utils.create_user_link(admin.id, context)
        
        hashtag = "#GBANUPDATEREQUEST" if old_ban else "#GBANREQUEST"
        
        req_msg = (f"<b>{hashtag}</b>\n"
                   f"<b>Initiated From:</b> {chat_display} [<code>{chat.id}</code>]\n\n"
                   f"<b>User:</b> {user_link} [<code>{target_id}</code>]\n"
                   f"<b>Reason:</b> <code>{reason}</code>\n")
        if old_ban: req_msg += f"<b>Old Reason:</b> <code>{utils.safe_escape(old_ban[0])}</code>\n"
        req_msg += f"<b>Admin:</b> {supporter_link} [<code>{admin.id}</code>]"
        
        if LOG_CHAT_ID:
            await context.bot.send_message(LOG_CHAT_ID, req_msg, reply_markup=keyboard, parse_mode=ParseMode.HTML)
        
        await update.message.reply_text("Request Sended.")

@bot_command("ungban")
async def ungban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin = update.effective_user
    chat = update.effective_chat
    thread_id = update.effective_message.message_thread_id
    is_private = chat.type == ChatType.PRIVATE
    
    is_sudo = await db.is_sudo(admin.id)
    is_support = await db.is_support(admin.id)
    
    if not (is_sudo or is_support): 
        return

    target_id, reason = None, None
    if update.message.reply_to_message and not update.message.reply_to_message.forum_topic_created:
        target_id = update.message.reply_to_message.from_user.id
        reason = " ".join(context.args) if context.args else None
    elif context.args:
        target_id, err = await utils.resolve_id(update, context, context.args[0])
        if err: 
            await update.message.reply_text(err)
            return
        reason = " ".join(context.args[1:]) if len(context.args) > 1 else None

    if not target_id:
        await update.message.reply_text("Who is the target of the command? The stars in the sky?"); return
    if not reason:
        await update.message.reply_text("Give a reason!"); return
    if await db.is_sudo(target_id) or await db.is_support(target_id) or target_id == context.bot.id:
        await update.message.reply_text("Privileged users is never gbanned..."); return

    if not await db.get_gban(target_id):
        user_link = await utils.create_user_link(target_id, context)
        await update.message.reply_html(f"User {user_link} [<code>{target_id}</code>] is not globally banned.")
        return

    if is_private:
        chat_display = f"PM with {utils.safe_escape(admin.first_name)}"
    elif chat.username:
        chat_link = f"https://t.me/{chat.username}/{update.effective_message.message_id}"
        chat_display = f"<a href='{chat_link}'>{utils.safe_escape(chat.title)}</a>"
    else:
        chat_display = utils.safe_escape(chat.title)

    await utils.send_safe_reply(update, context, "Let's give him another chance!")

    if is_sudo:
        if await db.remove_gban(target_id):
            user_link = await utils.create_user_link(target_id, context)
            admin_link = await utils.create_user_link(admin.id, context)
            curr_time = utils.get_utc_now()

            log_msg = (f"<b>#UNGBANNED</b>\n"
                       f"<b>Initiated From:</b> {chat_display} [<code>{chat.id}</code>]\n\n"
                       f"<b>User:</b> {user_link} [<code>{target_id}</code>]\n"
                       f"<b>Reason:</b> <code>{utils.safe_escape(reason)}</code>\n"
                       f"<b>Date:</b> <code>{curr_time}</code>\n"
                       f"<b>Admin:</b> {admin_link} [<code>{admin.id}</code>]")

            if LOG_CHAT_ID: 
                await context.bot.send_message(LOG_CHAT_ID, log_msg, parse_mode=ParseMode.HTML)
            
            context.job_queue.run_once(propagate_unban, when=1, data={
                'user_id': target_id,
                'chat_id': chat.id,
                'reply_to': update.message.message_id,
                'thread_id': thread_id,
                'is_private': is_private
            })
        else:
            user_link = await utils.create_user_link(target_id, context)
            await utils.send_safe_reply(update, context, f"User {user_link} [<code>{target_id}</code>] is not globally banned.")

    elif is_support:
        request_id = f"u_{target_id}_{int(time.time())}"
        
        await db.save_support_request(
            request_id, target_id, reason, admin.id, 
            chat.id, update.effective_message.message_id, "ungban",
            update.effective_message.message_thread_id, None
        )
        
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("Approve", callback_data=f"apr_{request_id}"),
            InlineKeyboardButton("Decline", callback_data=f"dec_{request_id}")
        ]])
        
        user_link = await utils.create_user_link(target_id, context)
        supporter_link = await utils.create_user_link(admin.id, context)
        
        req_msg = (f"<b>#UNGBANREQUEST</b>\n"
                   f"<b>Initiated From:</b> {chat_display} [<code>{chat.id}</code>]\n\n"
                   f"<b>User:</b> {user_link} [<code>{target_id}</code>]\n"
                   f"<b>Reason:</b> <code>{reason}</code>\n"
                   f"<b>Admin:</b> {supporter_link} [<code>{admin.id}</code>]")
        
        if LOG_CHAT_ID:
            await context.bot.send_message(LOG_CHAT_ID, req_msg, reply_markup=keyboard, parse_mode=ParseMode.HTML)
        
        await update.message.reply_text("Request Sended.")

async def propagate_unban(context: ContextTypes.DEFAULT_TYPE):
    start_time = time.time()
    job_data = context.job.data
    user_id = job_data['user_id']
    target_chat_id = job_data['chat_id']
    command_msg_id = job_data['reply_to']
    thread_id = job_data.get('thread_id')
    is_private = job_data.get('is_private', False)

    # 1. FETCH ONLY RELATED CHATS
    # Instead of all chats, we only target where the user was seen.
    chats = await db.get_user_seen_chats(user_id)
    
    # Always include the current chat in the sync list
    if target_chat_id not in chats:
        chats.append(target_chat_id)

    logger.info(f"Starting unban for {user_id} on {len(chats)} known chats.")

    for chat_id in chats:
        try:
            # Silent unban: Telegram handles the check via only_if_banned
            await context.bot.unban_chat_member(chat_id, user_id, only_if_banned=True)
                
        except Forbidden:
            # Bot was kicked or blocked -> Remove chat from DB
            await db.remove_chat(chat_id)
            
        except BadRequest as e:
            err = str(e).lower()
            if any(x in err for x in ["chat not found", "bot was kicked", "not member"]):
                await db.remove_chat(chat_id)
            
        except RetryAfter as e:
            # Respect Telegram's flood limits
            await asyncio.sleep(e.retry_after)
            
        except (TimedOut, Exception):
            pass
            
        # Very short sleep because we have very few requests to make now
        await asyncio.sleep(0.05)

    # 2. FINAL REPORT
    duration = round(time.time() - start_time, 2)
    final_text = f"User has been un-gbanned.\nTime taken: <code>{duration}s</code>"
    
    try:
        if is_private:
            await context.bot.send_message(
                chat_id=target_chat_id, 
                text=final_text, 
                parse_mode=ParseMode.HTML
            )
        else:
            await context.bot.send_message(
                chat_id=target_chat_id, text=final_text, parse_mode=ParseMode.HTML,
                reply_to_message_id=command_msg_id
            )
    except:
        try:
            await context.bot.send_message(
                chat_id=target_chat_id, text=final_text, 
                parse_mode=ParseMode.HTML, message_thread_id=thread_id
            )
        except: pass
        
@bot_command(["gbanstat", "gbaninfo"])
async def gbanstat_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    sudo = await db.is_sudo(user.id)
    support = await db.is_support(user.id)
    target_id = None
    error_msg = None

    if sudo or support:
        if update.message.reply_to_message and not update.message.reply_to_message.forum_topic_created:
            target_id = update.message.reply_to_message.from_user.id
        elif context.args:
            target_id, error_msg = await utils.resolve_id(update, context, context.args[0])

    if error_msg:
        await utils.send_safe_reply(update, context, error_msg)
        return

    if not target_id:
        target_id = user.id
        checking_self = True
    else:
        checking_self = (target_id == user.id)
    
    ban = await db.get_gban(target_id)
    u_link = await utils.create_user_link(target_id, context)
    title = "Your Global Ban Status:" if target_id == user.id else "Global Ban Status:"
    if target_id == OWNER_ID:
        msg = (f"<b>{title}</b>\n<b>User:</b> {u_link} [<code>{target_id}</code>]\n\n<b>Status:</b> Bot Owner")
    elif await db.is_sudo(target_id):
        msg = (f"<b>{title}</b>\n<b>User:</b> {u_link} [<code>{target_id}</code>]\n\n<b>Status:</b> Bot Sudo")
    elif await db.is_support(target_id):
        msg = (f"<b>{title}</b>\n<b>User:</b> {u_link} [<code>{target_id}</code>]\n\n<b>Status:</b> Bot Support")
    elif ban:
        msg = (f"<b>{title}</b>\n<b>User:</b> {u_link} [<code>{target_id}</code>]\n\n"
               f"<b>Status:</b> Banned\n"
               f"<b>Reason:</b> <code>{utils.safe_escape(ban[0])}</code>\n<b>Date:</b> <code>{ban[2]}</code>\n")
        if sudo:
            if ban[1] == 0:
                msg += f"<b>Admin:</b> This ban is imported."
            else:
                a_link = await utils.create_user_link(ban[1], context)
                msg += f"<b>Admin:</b> {a_link} [<code>{ban[1]}</code>]"
    else: msg = f"<b>{title}</b>\n<b>User:</b> {u_link} [<code>{target_id}</code>]\n\n<b>Status:</b> Not Banned"
    await utils.send_safe_reply(update, context, msg)

@bot_command("addsudo")
async def addsudo_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID: 
        return
    
    target_id = None
    if update.message.reply_to_message and not update.message.reply_to_message.forum_topic_created:
        target_id = update.message.reply_to_message.from_user.id
    elif context.args:
        target_id, err = await utils.resolve_id(update, context, context.args[0])
        if err: 
            await update.message.reply_text(err)
            return
            
    if not target_id:
        await update.message.reply_text("Who is the target of the command? The stars in the sky?")
        return
    if target_id == OWNER_ID:
        await update.message.reply_text("You are already the Master Owner.")
        return

    if await db.get_gban(target_id):
        user_link = await utils.create_user_link(target_id, context)
        await utils.send_safe_reply(update, context, f"User {user_link} [<code>{target_id}</code>] is <b>globally banned</b> and cannot be promoted.")
        return

    if await db.is_support(target_id):
        user_link = await utils.create_user_link(target_id, context)
        await utils.send_safe_reply(update, context, f"User {user_link} [<code>{target_id}</code>] is <b>already</b> support user.")
        return

    if await db.is_sudo(target_id):
        user_link = await utils.create_user_link(target_id, context)
        await utils.send_safe_reply(update, context, f"User {user_link} [<code>{target_id}</code>] is <b>already</b> sudo.")
        return

    await db.add_sudo(target_id)
    user_link = await utils.create_user_link(target_id, context)
    curr_time = utils.get_utc_now()

    log_msg = (f"<b>#SUDO</b>\n"
                f"<b>User:</b> {user_link} [<code>{target_id}</code>]\n"
                f"<b>Date:</b> <code>{curr_time}</code>")

    await utils.send_safe_reply(update, context, log_msg)
    if LOG_CHAT_ID:
        await context.bot.send_message(LOG_CHAT_ID, log_msg, parse_mode=ParseMode.HTML)

@bot_command("delsudo")
async def delsudo_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID: 
        return
    
    target_id = None
    if update.message.reply_to_message and not update.message.reply_to_message.forum_topic_created:
        target_id = update.message.reply_to_message.from_user.id
    elif context.args:
        target_id, err = await utils.resolve_id(update, context, context.args[0])
        if err: 
            await update.message.reply_text(err)
            return
            
    if not target_id:
        await update.message.reply_text("Who is the target of the command? The stars in the sky?")
        return

    if target_id == OWNER_ID:
        await update.message.reply_text("LoL... You cannot remove yourself.")
        return

    if await db.remove_sudo(target_id):
        user_link = await utils.create_user_link(target_id, context)
        curr_time = utils.get_utc_now()

        log_msg = (f"<b>#UNSUDO</b>\n"
                   f"<b>User:</b> {user_link} [<code>{target_id}</code>]\n"
                   f"<b>Date:</b> <code>{curr_time}</code>")

        await utils.send_safe_reply(update, context, log_msg)
        if LOG_CHAT_ID:
            await context.bot.send_message(LOG_CHAT_ID, log_msg, parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text("This user was not in the Sudo list.")

@bot_command("enforcegban")
async def enforce_gban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if not chat or chat.type == ChatType.PRIVATE:
        await update.message.reply_text("This command can only be used on a group.")
        return
    
    member = await chat.get_member(update.effective_user.id)
    is_sudo = await db.is_sudo(update.effective_user.id)
    if member.status != "creator" and not is_sudo:
        await update.message.reply_text("Only the chat creator can change this setting.")
        return

    current_status = await db.is_enforced(chat.id)
    status_text = "ENABLED" if current_status else "DISABLED"

    if not context.args:
        await utils.send_safe_reply(update, context, 
            f"<b>Global Ban Enforcement</b>\n\n"
            f"Current status for this chat: <b>{status_text}</b>\n"
            f"<b>Usage:</b> <code>/enforcegban &lt;yes/on/no/off&gt;</code>"
        )
        return
    
    choice = context.args[0].lower()
    if choice in ['yes', 'on']:
        await db.set_enforce(chat.id, 1)
        await utils.send_safe_reply(update, context, "<b>Global Ban enforcement has been ENABLED.</b>")
    elif choice in ['no', 'off']:
        await db.set_enforce(chat.id, 0)
        await utils.send_safe_reply(update, context, "<b>Global Ban enforcement has been DISABLED.</b>\n<i>Warning: Gbanned users will no longer be removed automatically.</i>")
    else:
        await utils.send_safe_reply(update, context, 
            f"<b>Invalid choice!</b>\n\n"
            f"Use: <code>/enforcegban on</code> or <code>/enforcegban off</code>"
        )

@bot_command("stats")
async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin = update.effective_user

    is_sudo = await db.is_sudo(admin.id)
    is_support = await db.is_support(admin.id)

    if not (is_sudo or is_support):
        return
    
    gbans = (await db.db_query("SELECT COUNT(*) FROM gbans", fetch="one"))[0]
    users = (await db.db_query("SELECT COUNT(*) FROM users", fetch="one"))[0]
    chats = (await db.db_query("SELECT COUNT(*) FROM bot_chats", fetch="one"))[0]
    
    msg = (f"<b>Bot Statistics:</b>\n\n"
           f"• <b>Global Bans:</b> <code>{gbans}</code>\n"
           f"• <b>Known Users:</b> <code>{users}</code>\n"
           f"• <b>Total Chats:</b> <code>{chats}</code>")
    await utils.send_safe_reply(update, context, msg)

@bot_command("backup")
async def backup_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID: return
    curr_time = utils.get_utc_now()
    try:
        with open(DB_NAME, 'rb') as f:
            await context.bot.send_document(OWNER_ID, document=f, caption=f"Database Backup: {curr_time}")
        await update.message.reply_text("Backup sent to your PM.")
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")

@bot_command("cleanup")
async def cleanup_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID: 
        return

    status_msg = await update.message.reply_text("Starting chat database cleanup...")
    
    chats = await db.db_query("SELECT chat_id FROM bot_chats", fetch="all")

    total = len(chats)
    removed = 0
    checked = 0
    bot_id = context.bot.id

    for (chat_id,) in chats:
        should_remove = False
        try:
            member = await context.bot.get_chat_member(chat_id, bot_id)
            if member.status in [ChatMemberStatus.LEFT, ChatMemberStatus.BANNED]:
                should_remove = True
        except Exception as e:
            should_remove = True
        
        if should_remove:
            await db.remove_chat(chat_id)
            removed += 1
            logger.info(f"Cleanup: Removed inactive chat {chat_id}")

        checked += 1
        if checked % 5 == 0:
            await asyncio.sleep(0.5)
            
    await status_msg.edit_text(
        f"<b>Cleanup chats complete!</b>\n\n"
        f"• Total scanned: <code>{total}</code>\n"
        f"• Removed: <code>{removed}</code>\n"
        f"• Still active: <code>{total - removed}</code>",
        parse_mode=ParseMode.HTML
    )

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    ERRORS_TO_IGNORE = [
        "httpx.ConnectError: All connection attempts failed",
        "httpx.ReadError:"
    ]

    error_str = str(context.error)

    if any(ignored in error_str for ignored in ERRORS_TO_IGNORE):
        # We ignore it silently (or log only to console)
        # logger.info(f"Ignored traceback: {error_str}")
        return
    
    logger.error("Exception while handling an update:", exc_info=context.error)

    tb_list = traceback.format_exception(None, context.error, context.error.__traceback__)
    tb_string = "".join(tb_list)

    chat_info = "N/A"
    user_info = "N/A"
    
    if isinstance(update, Update):
        if update.effective_chat:
            chat_title = update.effective_chat.title or "Private Chat"
            chat_info = f"{utils.safe_escape(chat_title)} [<code>{update.effective_chat.id}</code>]"
        if update.effective_user:
            user_name = update.effective_user.first_name
            user_info = f"{utils.safe_escape(user_name)} [<code>{update.effective_user.id}</code>]"

    summary_message = (
        f"<b>Bot Error Detected!</b>\n\n"
        f"<b>Error:</b> <code>{utils.safe_escape(error_str)}</code>\n"
        f"<b>Chat:</b> {chat_info}\n"
        f"<b>User:</b> {user_info}\n\n"
        f"<i>Full traceback is attached as a file.</i>"
    )

    if LOG_CHAT_ID:
        try:
            with io.BytesIO(str.encode(tb_string)) as traceback_file:
                filename = f"traceback_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
                traceback_file.name = filename
                
                await context.bot.send_document(
                    chat_id=LOG_CHAT_ID,
                    document=traceback_file,
                    caption=summary_message,
                    parse_mode=ParseMode.HTML
                )
        except Exception as e:
            logger.critical(f"Could not send traceback file: {e}")
            try:
                await context.bot.send_message(
                    LOG_CHAT_ID, 
                    f"<b>Critical Error:</b> Failed to send traceback file.\nError: {e}"
                )
            except:
                pass

@bot_command(["sudolist", "sudos"])
async def sudolist_cmd(update, context):
    if not await db.is_sudo(update.effective_user.id): return
    
    sudos = await db.get_all_sudos()
    if not sudos:
        await update.message.reply_text("The Sudo list is empty.")
        return

    msg = "<b>Sudo Privileged Users:</b>\n\n"
    msg += f"• {await utils.create_user_link(OWNER_ID, context)} [<code>{OWNER_ID}</code>] (Owner)\n"
    
    for s_id in sudos:
        if s_id == OWNER_ID: continue
        u_link = await utils.create_user_link(s_id, context)
        msg += f"• {u_link} [<code>{s_id}</code>]\n"
    
    await utils.send_safe_reply(update, context, msg)

@bot_command("update")
async def update_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return

    msg = await update.message.reply_text("Checking updates...", parse_mode=ParseMode.HTML)

    try:
        pull_result = subprocess.check_output(["git", "pull"]).decode("utf-8")
        
        if "Already up to date." in pull_result:
            await msg.edit_text("<b>Bot is already up to date.</b>\nNo restart needed.", parse_mode=ParseMode.HTML)
            return

        successful_msg = (f"<b>Update pulled!</b>\n<i>Restarting now...</i>\n<blockquote><code>{pull_result}</code></blockquote>")

        await msg.edit_text(successful_msg, parse_mode=ParseMode.HTML)

        if LOG_CHAT_ID:
            admin_link = await utils.create_user_link(update.effective_user.id, context)
            await context.bot.send_message(LOG_CHAT_ID, successful_msg, parse_mode=ParseMode.HTML)

        os.execv(sys.executable, [sys.executable] + sys.argv)

    except subprocess.CalledProcessError as e:
        await msg.edit_text(f"<b>Update failed!</b>\nError: <code>{str(e)}</code>", parse_mode=ParseMode.HTML)
    except Exception as e:
        await msg.edit_text(f"<b>Unexpected error:</b>\n<code>{str(e)}</code>", parse_mode=ParseMode.HTML)

@bot_command("restart")
async def restart_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    try:
        await utils.send_safe_reply(update, context, "Restarting...")
        if LOG_CHAT_ID:
            await context.bot.send_message(LOG_CHAT_ID, "Restarting...")
    except Exception as e:
        logger.error(f"Failed to send restart message: {e}")
    os.execv(sys.executable, [sys.executable] + sys.argv)

@bot_command("leave")
async def leave_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await db.is_sudo(update.effective_user.id):
        return

    if update.effective_chat.type == ChatType.PRIVATE:
        await update.message.reply_text("I can only leave groups.")
        return

    chat_id = update.effective_chat.id

    try:
        await update.message.reply_text("Farewell! My duties here are finished. 🫡")
        
        await db.remove_chat(chat_id)
        await context.bot.leave_chat(chat_id)
        
        logger.info(f"Bot left chat {chat_id} via leave command.")
    except Exception as e:
        logger.error(f"Error while leaving chat {chat_id}: {e}")

@bot_command("restore")
async def restore_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return

    message = update.effective_message
    document = message.document or (message.reply_to_message.document if message.reply_to_message else None)

    if not document:
        await message.reply_text("Send the database file or reply to one.")
        return

    required_filename = os.path.basename(DB_NAME)

    if document.file_name != required_filename:
        await message.reply_html(
            f"<b>Wrong filename!</b>\n"
            f"I only accepts: <code>{required_filename}</code>"
        )
        return
        
    status_msg = await utils.send_safe_reply(update, context, "Downloading database...")

    try:
        new_db_file = await context.bot.get_file(document.file_id)
        
        await new_db_file.download_to_drive(DB_NAME)
        
        await status_msg.edit_text(
            f"<b>Database restored!</b>\nRestarting system now...", 
            parse_mode=ParseMode.HTML
        )

        os.execv(sys.executable, [sys.executable] + sys.argv)

    except Exception as e:
        logger.error(f"Restore failed: {e}")
        await status_msg.edit_text(f"<b>Error during restore:</b>\n<code>{str(e)}</code>", parse_mode=ParseMode.HTML)

async def auto_backup_job(context: ContextTypes.DEFAULT_TYPE):
    if OWNER_ID:
        try:
            db_filename = os.path.basename(DB_NAME)
            curr_time = utils.get_utc_now()
            
            with open(DB_NAME, 'rb') as db_file:
                await context.bot.send_document(
                    chat_id=OWNER_ID,
                    document=db_file,
                    filename=db_filename,
                    caption=f"Auto-Database Backup: {curr_time}",
                    parse_mode=ParseMode.HTML
                )
            logger.info("Automatic backup sent to owner.")
        except Exception as e:
            logger.error(f"Auto-backup failed: {e}")

@bot_command("addsupport")
async def addsupport_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID: 
        return
    
    target_id = None
    if update.message.reply_to_message and not update.message.reply_to_message.forum_topic_created:
        target_id = update.message.reply_to_message.from_user.id
    elif context.args:
        target_id, err = await utils.resolve_id(update, context, context.args[0])
        if err: 
            await update.message.reply_text(err)
            return
            
    if not target_id:
        await update.message.reply_text("Who is the target of the command? The stars in the sky?")
        return
    if target_id == OWNER_ID:
        await update.message.reply_text("You are already the Master Owner.")
        return

    if await db.get_gban(target_id):
        user_link = await utils.create_user_link(target_id, context)
        await utils.send_safe_reply(update, context, f"User {user_link} [<code>{target_id}</code>] is <b>globally banned</b> and cannot be promoted")
        return

    if await db.is_sudo(target_id):
        user_link = await utils.create_user_link(target_id, context)
        await utils.send_safe_reply(update, context, f"User {user_link} [<code>{target_id}</code>] is <b>already</b> sudo.")
        return

    if await db.is_support(target_id):
        user_link = await utils.create_user_link(target_id, context)
        await utils.send_safe_reply(update, context, f"User {user_link} [<code>{target_id}</code>] is <b>already</b> support user.")
        return

    await db.add_support(target_id)
    user_link = await utils.create_user_link(target_id, context)
    curr_time = utils.get_utc_now()

    log_msg = (f"<b>#SUPPORT</b>\n"
                f"<b>User:</b> {user_link} [<code>{target_id}</code>]\n"
                f"<b>Date:</b> <code>{curr_time}</code>")

    await utils.send_safe_reply(update, context, log_msg)
    if LOG_CHAT_ID:
        await context.bot.send_message(LOG_CHAT_ID, log_msg, parse_mode=ParseMode.HTML)

@bot_command("delsupport")
async def delsupport_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID: 
        return
    
    target_id = None
    if update.message.reply_to_message and not update.message.reply_to_message.forum_topic_created:
        target_id = update.message.reply_to_message.from_user.id
    elif context.args:
        target_id, err = await utils.resolve_id(update, context, context.args[0])
        if err: 
            await update.message.reply_text(err)
            return
            
    if not target_id:
        await update.message.reply_text("Who is the target of the command? The stars in the sky?")
        return

    if target_id == OWNER_ID:
        await update.message.reply_text("LoL... You cannot remove yourself.")
        return

    if await db.remove_support(target_id):
        user_link = await utils.create_user_link(target_id, context)
        curr_time = utils.get_utc_now()

        log_msg = (f"<b>#UNSUPPORT</b>\n"
                   f"<b>User:</b> {user_link} [<code>{target_id}</code>]\n"
                   f"<b>Date:</b> <code>{curr_time}</code>")

        await utils.send_safe_reply(update, context, log_msg)
        if LOG_CHAT_ID:
            await context.bot.send_message(LOG_CHAT_ID, log_msg, parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text("This user was not in the Support list.")

@bot_command(["supportlist", "supports"])
async def supportlist_cmd(update, context):
    if not await db.is_sudo(update.effective_user.id): 
        return
    
    supports = await db.get_all_supports()
    if not supports:
        await update.message.reply_text("The Support list is empty.")
        return

    msg = "<b>Support Team Members:</b>\n\n"
    
    for s_id in supports:
        u_link = await utils.create_user_link(s_id, context)
        msg += f"• {u_link} [<code>{s_id}</code>]\n"
    
    await utils.send_safe_reply(update, context, msg)

@bot_command("importbans")
async def import_gbans_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return

    msg = update.effective_message
    document = msg.document or (msg.reply_to_message.document if msg.reply_to_message else None)

    if not document or not document.file_name.endswith('.json'):
        await msg.reply_text("Please reply to a valid <code>.json</code> file.", parse_mode=ParseMode.HTML)
        return

    status_msg = await msg.reply_text("Importing bans... Please wait!")

    temp_path = "import_temp.json"

    try:
        file_info = await context.bot.get_file(document.file_id)
        await file_info.download_to_drive(temp_path)
        
        bans_to_add = []
        date_str = utils.get_utc_now()

        with open(temp_path, 'r', encoding='utf-8', errors='ignore') as f:
            for line in f:
                line = line.strip()
                if not line: continue
                
                try:
                    data = json.loads(line)
                    u_id = data.get("user_id")
                    raw_reason = data.get("reason", "No reason given.")
                    
                    if u_id and int(u_id) > 0:
                        clean_reason = str(raw_reason).replace('\n', ' ').replace('\\n', ' ')
                        clean_reason = " ".join(clean_reason.split())
                        
                        bans_to_add.append((int(u_id), clean_reason, 0, date_str))
                except:
                    continue

        count = 0
        if bans_to_add:
            count = await db.import_gbans(bans_to_add)

        final_message = (f"<b>Import Bans Successful!</b>\n"
                         f"Added <code>{count}</code> new bans to the database.")

        await status_msg.edit_text(final_message, parse_mode=ParseMode.HTML)
        
        if LOG_CHAT_ID:
            await context.bot.send_message(LOG_CHAT_ID, final_message, parse_mode=ParseMode.HTML)

    except Exception as e:
        logger.error(f"Import failed: {e}")
        await status_msg.edit_text(f"<b>Error:</b> <code>{str(e)}</code>", parse_mode=ParseMode.HTML)
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)

# --- main.py ---

# --- MAIN ---

def main():
    db.init_db()

    request_settings = HTTPXRequest(
        connect_timeout=20.0,
        read_timeout=20.0,
    )
    
    app = Application.builder().token(TOKEN).build()

    app.add_handler(MessageHandler(filters.ALL, ignore_old_updates), group=-200)
    app.add_handler(MessageHandler(filters.UpdateType.EDITED_MESSAGE & filters.COMMAND, ignore_edited_commands), group=-50)

    app.add_error_handler(error_handler)

    app.add_handler(MessageHandler(filters.Regex(r'^[!/]\w+'), command_router), group=1)

    app.add_handler(ChatMemberHandler(enforcer_radar, ChatMemberHandler.CHAT_MEMBER), group=-100)
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, enforcer_message_checker), group=-100)
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, passive_data_logger), group=10)
    app.add_handler(CallbackQueryHandler(support_callback_handler, pattern=r"^(apr|dec)_"))

    if app.job_queue:
        app.job_queue.run_once(send_startup_log, when=1)

    app.job_queue.run_repeating(auto_backup_job, interval=3600, first=30)

    print("Bot is up and running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
