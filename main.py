"""Mention tracker for factory problems.

The user client (the owner's own Telegram account) watches group mentions.
The bot sends each one to the owner as a card with [Read] [Done] buttons.

  read   <- "Read" button, or the mention is seen in any Telegram app
  solved <- "Done" button, or the owner replies to the mentioned message

Solved problems are deleted from the database and their cards disappear from the bot chat.
"""

import asyncio
import contextlib
import getpass
import html
import logging
import os
import re
import sys
import threading
import time
from collections import defaultdict
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

from dotenv import load_dotenv
from pytdbot import Client, ClientManager, types

import db

BASE = Path(__file__).parent
DATA = BASE / "data"
DATA.mkdir(exist_ok=True)
SILENT_AFTER_SECONDS = 600  # cards for older mentions (catch-up) arrive without a sound
FIRST_RUN_LOOKBACK = 24 * 3600  # very first start: pick up mentions from the last day

# ------------------------------------------------------------------ config & logging

load_dotenv(BASE / ".env")


def env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        sys.exit(f"{name} is not set. Fill it in .env (see .env.example).")
    return value


if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(errors="backslashreplace")  # some Windows consoles can't print every character
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").strip().upper(),  # LOG_LEVEL=DEBUG in .env shows every message seen
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        RotatingFileHandler(DATA / "tracker.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8"),
    ],
)
logging.getLogger("pytdbot").setLevel(logging.WARNING)
log = logging.getLogger("tracker")

if not env("API_ID").isdigit():
    sys.exit("API_ID in .env must be a number.")

common = dict(
    api_id=int(env("API_ID")),
    api_hash=env("API_HASH"),
    database_encryption_key=env("DATABASE_ENCRYPTION_KEY"),
    workers=None,  # one task per update: a handler waiting on the network never blocks the others
)
# TDLib's own internal log goes to a file, not the console (one setting covers both clients).
tdlib_log = types.LogStreamFile(path=str(DATA / "tdlib.log"), max_file_size=5 * 1024 * 1024, redirect_stderr=False)
user = Client(files_directory=str(DATA / "user"), user_bot=True, td_log=tdlib_log, **common)
bot = Client(token=env("BOT_TOKEN"), files_directory=str(DATA / "bot"), **common)
for _client in (user, bot):
    _client._print_welcome = lambda: None  # pytdbot's banner would print over the login prompt

owner_id = 0
bot_chat_id = 0  # the owner's chat with the bot, as seen from the owner's account
owner_ready = asyncio.Event()  # both clients logged in; handlers wait for this
stopping = asyncio.Event()
shutting_down = False
card_lock = asyncio.Lock()  # one DB change + card update at a time, so cards never show stale state
sync_lock = asyncio.Lock()
chat_locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
owner_usernames: set[str] = set()
connected = False  # TDLib is online and up to date


# ------------------------------------------------------------------ login (first run only)

async def ask(prompt: str, secret: bool = False) -> str:
    """input() in a daemon thread, so Ctrl+C during login doesn't hang the exit."""
    loop = asyncio.get_running_loop()
    fut = loop.create_future()

    def worker():
        try:
            value = (getpass.getpass if secret else input)(prompt).strip()
            deliver = lambda: fut.done() or fut.set_result(value)
        except EOFError:
            deliver = lambda: fut.done() or fut.set_exception(EOFError())
        with contextlib.suppress(RuntimeError):  # event loop already closed
            loop.call_soon_threadsafe(deliver)

    threading.Thread(target=worker, daemon=True).start()
    return await fut


async def login_step(call, prompt: str, secret: bool = False) -> None:
    while True:
        try:
            value = await ask(prompt, secret)
        except EOFError:
            log.error("Logging in needs a console. Run `uv run python main.py` in a terminal once.")
            stopping.set()
            return
        result = await call(value)
        if not isinstance(result, types.Error):
            return
        print(f"  Error: {result.message}")


@user.on_updateAuthorizationState()
async def on_auth(c: Client, u: types.UpdateAuthorizationState) -> None:
    st = u.authorization_state
    if isinstance(st, types.AuthorizationStateWaitPhoneNumber):
        await login_step(lambda v: c.setAuthenticationPhoneNumber(phone_number=v),
                         "Phone number (international format, starting with +): ")
    elif isinstance(st, types.AuthorizationStateWaitEmailAddress):
        await login_step(lambda v: c.setAuthenticationEmailAddress(email_address=v),
                         "Email address for login codes: ")
    elif isinstance(st, types.AuthorizationStateWaitEmailCode):
        await login_step(lambda v: c.checkAuthenticationEmailCode(code=types.EmailAddressAuthenticationCode(code=v)),
                         "Code sent to your email: ")
    elif isinstance(st, types.AuthorizationStateWaitCode):
        await login_step(lambda v: c.checkAuthenticationCode(code=v),
                         "Login code (look in your Telegram app): ")
    elif isinstance(st, types.AuthorizationStateWaitPassword):
        await login_step(lambda v: c.checkAuthenticationPassword(password=v),
                         "Two-step verification password: ", secret=True)
    elif isinstance(st, (types.AuthorizationStateLoggingOut, types.AuthorizationStateClosed)):
        if owner_ready.is_set() and not shutting_down and not stopping.is_set():
            stopping.set()
            log.error("Telegram logged the tracker out (session terminated?). Start it again to log in.")
            await bot_send("⚠️ The tracker was logged out of your Telegram account. "
                           "Start it again on the laptop and log in.")
    elif not isinstance(st, (types.AuthorizationStateWaitTdlibParameters, types.AuthorizationStateReady,
                             types.AuthorizationStateClosing)):
        log.error("Login step %s isn't supported. Log in with an official app first, then retry.", st.getType())


# ------------------------------------------------------------------ cards

def esc(s: str | None) -> str:
    return html.escape(s or "")


def clip(text: str, limit: int = 3000) -> str:
    """Cut to `limit` UTF-16 units, which is how Telegram measures message length."""
    raw = text.encode("utf-16-le")
    if len(raw) <= 2 * limit:
        return text
    return raw[: 2 * limit].decode("utf-16-le", errors="ignore") + "…"


def content_text(content: types.MessageContent) -> str:
    if isinstance(content, types.MessageText):
        return content.text.text
    kind = content.getType().removeprefix("message")  # Photo, VoiceNote, Document, ...
    caption = getattr(content, "caption", None)
    text = caption.text if isinstance(caption, types.FormattedText) else ""
    return f"[{kind}] {text}".strip()


def mentions_owner(m: types.Message) -> bool:
    """True if the text tags the owner. A backup for Telegram's own mention flag."""
    content = m.content
    ft = content.text if isinstance(content, types.MessageText) else getattr(content, "caption", None)
    if not isinstance(ft, types.FormattedText):
        return False
    raw = ft.text.encode("utf-16-le")  # entity offsets count UTF-16 units
    for e in ft.entities or []:
        if isinstance(e.type, types.TextEntityTypeMentionName) and e.type.user_id == owner_id:
            return True
        if isinstance(e.type, types.TextEntityTypeMention):
            name = raw[2 * e.offset: 2 * (e.offset + e.length)].decode("utf-16-le", "ignore")
            if name.lstrip("@").lower() in owner_usernames:
                return True
    return False


def is_basic_group_id(chat_id: int) -> bool:
    return -1_000_000_000_000 < chat_id < 0  # supergroups and channels are -100xxxxxxxxxx


def is_group(chat: types.Chat) -> bool:
    t = chat.type
    return isinstance(t, types.ChatTypeBasicGroup) or (isinstance(t, types.ChatTypeSupergroup) and not t.is_channel)


def button(label: str, data: str) -> types.InlineKeyboardButton:
    return types.InlineKeyboardButton(
        text=label, type=types.InlineKeyboardButtonTypeCallback(data=data.encode())
    )


def render(row, final: str | None = None) -> tuple[str, types.ReplyMarkupInlineKeyboard | None]:
    status = final or ("👁 Read" if row["is_read"] else "🔴 New")
    when = datetime.fromtimestamp(row["date"]).strftime("%d.%m %H:%M")
    lines = [
        f"<b>{status}</b>",
        f"<b>{esc(row['chat_title'] or '?')}</b> · {esc(row['sender'] or '?')} · {when}",
        f"<blockquote>{esc(clip(row['text'] or '—'))}</blockquote>",
    ]
    if final:
        return "\n".join(lines), None  # closed: no link, no buttons
    if row["jump_msg_id"]:
        lines.append("👆 To open the message, click the quote in your message above")
    elif row["link"]:
        label = "Open message (phone app)" if row["link"].startswith("tg://") else "Open message"
        lines.append(f'<a href="{esc(row["link"])}">{label}</a>')
    text = "\n".join(lines)
    buttons = [] if row["is_read"] else [button("👁 Read", f"r:{row['id']}")]
    buttons.append(button("✅ Done", f"d:{row['id']}"))
    return text, types.ReplyMarkupInlineKeyboard(rows=[buttons])


_last_send = 0.0


async def confirmed(send):
    """Await a pytdbot send. pytdbot itself waits for Telegram's confirmation and returns the message
    with its final id; the timeout only stops one lost confirmation from freezing every other card."""
    try:
        return await asyncio.wait_for(send, timeout=120)
    except TimeoutError:
        log.warning("Telegram didn't confirm a message within 2 minutes; it will be retried later.")
        return types.Error(code=408, message="No confirmation from Telegram")


async def send_jump(row) -> int | None:
    """Basic groups have no message links. Instead, from the owner's account, post a reply to the
    original message into the bot chat: clicking its quote opens the message on desktop and phone."""
    if not bot_chat_id or not is_basic_group_id(row["chat_id"]):
        return None
    props = await user.getMessageProperties(chat_id=row["chat_id"], message_id=row["message_id"])
    if isinstance(props, types.Error) or not props.can_be_replied_in_another_chat:
        return None  # e.g. the group forbids copying; the phone-only link stays as a fallback
    msg = await confirmed(user.sendTextMessage(
        chat_id=bot_chat_id, text="👆 Open message", disable_notification=True,
        reply_to=types.InputMessageReplyToExternalMessage(chat_id=row["chat_id"], message_id=row["message_id"]),
    ))
    if isinstance(msg, types.Error):
        log.warning("Couldn't post the jump message: %s", msg.message)
        return None
    return msg.id


async def delete_jump(jump_msg_id: int) -> None:
    # Your own messages in a private chat can be deleted for both sides at any time.
    r = await user.deleteMessages(chat_id=bot_chat_id, message_ids=[jump_msg_id], revoke=True)
    if isinstance(r, types.Error):
        log.warning("Couldn't delete the jump message: %s", r.message)


async def bot_send(text: str, markup=None, silent: bool = False) -> int | None:
    """Send an HTML message to the owner. Returns its id once Telegram has accepted it."""
    global _last_send
    for _ in range(5):
        await asyncio.sleep(max(0.0, _last_send + 1 - time.monotonic()))  # ~1 message/second per chat
        _last_send = time.monotonic()
        msg = await confirmed(bot.sendTextMessage(
            chat_id=owner_id, text=text, parse_mode="html", disable_web_page_preview=True,
            disable_notification=silent, reply_markup=markup,
        ))
        if not isinstance(msg, types.Error):
            return msg.id
        if msg.code != 429:
            log.error("Bot can't message you (%s). Open the bot in Telegram and press Start.", msg.message)
            return None
        await asyncio.sleep(int(m.group()) if (m := re.search(r"\d+", msg.message)) else 5)
    return None


async def edit_card(row, final: str | None = None) -> None:
    if not row["bot_msg_id"]:
        return
    text, markup = render(row, final)
    r = await bot.editTextMessage(
        chat_id=owner_id, message_id=row["bot_msg_id"], text=text, parse_mode="html",
        disable_web_page_preview=True, reply_markup=markup,
    )
    if isinstance(r, types.Error):
        log.warning("Couldn't update card %s: %s", row["id"], r.message)


async def send_card(row_id: int) -> None:
    async with card_lock:
        row = db.get(row_id)
        if row is None or row["bot_msg_id"]:
            return
        if not row["jump_msg_id"] and (jump_id := await send_jump(row)):
            db.set_jump_msg_id(row_id, jump_id)
            row = db.get(row_id)
        silent = time.time() - row["date"] > SILENT_AFTER_SECONDS
        if msg_id := await bot_send(*render(row), silent=silent):
            db.set_bot_msg_id(row_id, msg_id)


async def refresh_card(row_id: int) -> None:
    async with card_lock:
        if row := db.get(row_id):
            await edit_card(row)


async def move_card_down(row_id: int) -> None:
    """Re-send a card at the bottom of the chat and retire the old one."""
    async with card_lock:
        row = db.get(row_id)
        if row is None:
            return
        db.set_jump_msg_id(row_id, await send_jump(row))  # a fresh jump message goes right above the new card
        new_id = await bot_send(*render(db.get(row_id)), silent=True)
        if new_id is None:
            return
        db.set_bot_msg_id(row_id, new_id)
        if row["jump_msg_id"]:
            await delete_jump(row["jump_msg_id"])
        if old := row["bot_msg_id"]:
            await delete_or_edit(old, "⤵️ Moved down")


async def delete_or_edit(message_id: int, fallback_text: str) -> None:
    """Delete a bot message. Telegram lets bots delete only messages younger than 48 hours,
    so an older one is edited to `fallback_text` (without buttons) instead."""
    props = await bot.getMessageProperties(chat_id=owner_id, message_id=message_id)
    if not isinstance(props, types.Error) and props.can_be_deleted_for_all_users:
        if not isinstance(await bot.deleteMessages(chat_id=owner_id, message_ids=[message_id], revoke=True), types.Error):
            return
    r = await bot.editTextMessage(chat_id=owner_id, message_id=message_id, text=fallback_text,
                                  parse_mode="html", disable_web_page_preview=True, reply_markup=None)
    if isinstance(r, types.Error):
        log.warning("Couldn't remove card %s: %s", message_id, r.message)


# ------------------------------------------------------------------ state changes

async def describe(m: types.Message, chat: types.Chat) -> tuple[str, str, str | None]:
    sender = "?"
    if isinstance(m.sender_id, types.MessageSenderUser):
        u = await user.getUser(user_id=m.sender_id.user_id)
        if not isinstance(u, types.Error):
            sender = f"{u.first_name} {u.last_name}".strip()
    elif isinstance(m.sender_id, types.MessageSenderChat):
        sc = await user.getChat(chat_id=m.sender_id.chat_id)
        if not isinstance(sc, types.Error):
            sender = sc.title
    link = await user.getMessageLink(chat_id=m.chat_id, message_id=m.id)
    if not isinstance(link, types.Error):
        return chat.title, sender, link.link
    if isinstance(chat.type, types.ChatTypeBasicGroup):
        # Telegram has no message links for basic groups. The Android app understands this deep link.
        return chat.title, sender, f"tg://openmessage?chat_id={chat.type.basic_group_id}&message_id={m.id >> 20}"
    return chat.title, sender, None


async def add_mention(m: types.Message) -> bool:
    """Record a group mention and send its card. Returns True if it was new."""
    chat = await user.getChat(chat_id=m.chat_id)  # answered from TDLib's local cache
    if isinstance(chat, types.Error) or not is_group(chat):
        return False  # only mentions in groups are tracked
    # Insert before the slower lookups, so a "read" update arriving meanwhile still finds the row.
    # A mention you already read is still an open problem: it's stored as read, not skipped.
    row_id = db.add(m.chat_id, m.id, m.date, content_text(m.content), is_read=not m.contains_unread_mention)
    if row_id is None:
        return False
    title, sender, link = await describe(m, chat)
    db.set_details(row_id, title, sender, link)
    log.info("New mention in %s from %s", title, sender)
    await send_card(row_id)
    return True


async def set_read(row_id: int) -> None:
    async with card_lock:
        if db.mark_read(row_id):
            await edit_card(db.get(row_id))


async def close(row_id: int, status: str):
    """Remove from the open list and delete its card (a card older than 48h becomes a final note)."""
    async with card_lock:
        row = db.get(row_id)
        if row is None:
            return None
        db.close(row_id)
        if row["jump_msg_id"]:
            await delete_jump(row["jump_msg_id"])
        if row["bot_msg_id"]:
            await delete_or_edit(row["bot_msg_id"], render(row, status)[0])
        return row


async def mark_read_in_telegram(row) -> None:
    r = await user.viewMessages(chat_id=row["chat_id"], message_ids=[row["message_id"]], force_read=True)
    if isinstance(r, types.Error):
        log.warning("Couldn't mark message read in Telegram: %s", r.message)


async def solve(row_id: int, reason: str) -> None:
    row = await close(row_id, f"✅ Solved — {reason}")
    if row and not row["is_read"]:
        await mark_read_in_telegram(row)
    if row:
        log.info("Solved: %s (%s)", row["chat_title"], reason)


def replied_row(m: types.Message) -> int | None:
    """Row id of the open problem this message replies to, if any."""
    rt = m.reply_to
    if isinstance(rt, types.MessageReplyToMessage) and rt.chat_id == m.chat_id:
        if row := db.get_by_message(m.chat_id, rt.message_id):
            return row["id"]
    return None


# ------------------------------------------------------------------ syncing (startup, reconnect, wake-up)

async def sync_chat(chat_id: int) -> None:
    """Make the open list match Telegram's unread mentions for one chat."""
    async with chat_locks[chat_id]:
        chat = await user.getChat(chat_id=chat_id)
        if isinstance(chat, types.Error):
            return
        if not is_group(chat):  # only groups are tracked; clears anything recorded from other chats
            for row in db.open_in_chat(chat_id):
                await close(row["id"], "Ignored: not a group chat")
            return
        unread: set[int] = set()
        if chat.unread_mention_count:
            found = await user.searchChatMessages(
                chat_id=chat_id, limit=100, filter=types.SearchMessagesFilterUnreadMention()
            )
            if isinstance(found, types.Error):
                log.warning("Couldn't load unread mentions of %s: %s", chat.title, found.message)
                return
            for m in found.messages:
                unread.add(m.id)
                await add_mention(m)
        for row in db.open_in_chat(chat_id):
            if row["is_read"] or row["message_id"] in unread:
                continue
            if not chat.unread_mention_count:
                await set_read(row["id"])
                continue
            # Not in the unread list. Only trust the message's own flag, never mark read on a guess.
            msg = await user.getMessage(chat_id=chat_id, message_id=row["message_id"])
            if not isinstance(msg, types.Error) and not msg.contains_unread_mention:
                await set_read(row["id"])


async def find_replies(chat_id: int) -> None:
    """Solve problems the owner replied to while the tracker wasn't watching."""
    if not db.open_in_chat(chat_id):
        return
    found = await user.searchChatMessages(
        chat_id=chat_id, sender_id=types.MessageSenderUser(user_id=owner_id), limit=100
    )
    if isinstance(found, types.Error):
        return
    for m in found.messages:
        if row_id := replied_row(m):
            await solve(row_id, "you replied")


async def catch_up_mentions(chat_id: int, since: int) -> int:
    """Mentions that arrived while the tracker was off, including ones you've read since."""
    found = await user.searchChatMessages(chat_id=chat_id, limit=100, filter=types.SearchMessagesFilterMention())
    if isinstance(found, types.Error):
        log.warning("Couldn't search mentions in chat %s: %s", chat_id, found.message)
        return 0
    added = 0
    for m in found.messages:
        if m.date >= since and not m.is_outgoing:
            added += await add_mention(m)
    return added


async def full_sync() -> None:
    if sync_lock.locked():
        return
    async with sync_lock:
        started = int(time.time())
        last_seen = db.get_meta("last_seen")
        since = (int(last_seen) if last_seen else started - FIRST_RUN_LOOKBACK) - 300  # 5 min overlap
        chat_ids = {row["chat_id"] for row in db.all_open()}
        active: list[int] = []  # groups with messages since we were last online
        for chat_list in (types.ChatListMain(), types.ChatListArchive()):
            for _ in range(1000):  # loads page by page; an error (404) means everything is loaded
                if isinstance(await user.loadChats(chat_list=chat_list, limit=100), types.Error):
                    break
            chats = await user.getChats(chat_list=chat_list, limit=10_000)
            if isinstance(chats, types.Error):
                continue
            for chat_id in chats.chat_ids:
                chat = await user.getChat(chat_id=chat_id)
                if isinstance(chat, types.Error):
                    continue
                if chat.unread_mention_count:
                    chat_ids.add(chat_id)
                if is_group(chat) and chat.last_message and chat.last_message.date >= since:
                    active.append(chat_id)
        missed = 0
        for chat_id in active:
            missed += await catch_up_mentions(chat_id, since)
        chat_ids |= {row["chat_id"] for row in db.all_open()}
        for chat_id in chat_ids:
            await sync_chat(chat_id)
            await find_replies(chat_id)
        for row in db.unsent():
            await send_card(row["id"])
        db.set_meta("last_seen", str(started))
        rows = db.all_open()
        log.info("In sync (checked %d active group(s), caught %d missed mention(s)): %d open problem(s), %d unread",
                 len(active), missed, len(rows), sum(not r["is_read"] for r in rows))


async def heartbeat() -> None:
    """Remember when we were last online, so the next start knows how far back to look."""
    while True:
        await asyncio.sleep(60)
        if connected and not sync_lock.locked():
            db.set_meta("last_seen", str(int(time.time())))


# ------------------------------------------------------------------ user account updates

@user.on_updateNewMessage()
async def on_new_message(c: Client, u: types.UpdateNewMessage) -> None:
    await owner_ready.wait()
    m = u.message
    log.debug("Message %s in chat %s: outgoing=%s unread_mention=%s",
              m.id, m.chat_id, m.is_outgoing, m.contains_unread_mention)
    if m.is_outgoing:
        if row_id := replied_row(m):
            await solve(row_id, "you replied")
    elif m.contains_unread_mention or mentions_owner(m):
        await add_mention(m)


@user.on_updateMessageMentionRead()
async def on_mention_read(c: Client, u: types.UpdateMessageMentionRead) -> None:
    await owner_ready.wait()
    if row := db.get_by_message(u.chat_id, u.message_id):
        await set_read(row["id"])


@user.on_updateChatUnreadMentionCount()
async def on_mention_count(c: Client, u: types.UpdateChatUnreadMentionCount) -> None:
    # Catches bulk "mark as read" and mentions whose message update never arrived.
    await owner_ready.wait()
    await sync_chat(u.chat_id)


@user.on_updateMessageContent()
async def on_edited(c: Client, u: types.UpdateMessageContent) -> None:
    await owner_ready.wait()
    row = db.get_by_message(u.chat_id, u.message_id)
    if row and (text := content_text(u.new_content)) != row["text"]:
        db.set_text(row["id"], text)
        await refresh_card(row["id"])


@user.on_updateDeleteMessages()
async def on_deleted(c: Client, u: types.UpdateDeleteMessages) -> None:
    if not u.is_permanent:  # just dropped from TDLib's cache
        return
    await owner_ready.wait()
    for message_id in u.message_ids:
        if row := db.get_by_message(u.chat_id, message_id):
            await close(row["id"], "🗑 Message was deleted")


@user.on_updateConnectionState()
async def on_connection(c: Client, u: types.UpdateConnectionState) -> None:
    # Back online (after Wi-Fi loss or sleep): catch up on anything missed.
    global connected
    connected = isinstance(u.state, types.ConnectionStateReady)
    if connected and owner_ready.is_set():
        await full_sync()


# ------------------------------------------------------------------ bot updates

@bot.on_updateNewCallbackQuery()
async def on_button(c: Client, u: types.UpdateNewCallbackQuery) -> None:
    await owner_ready.wait()
    action, _, rid = u.text.partition(":")
    row = db.get(int(rid)) if rid.isdigit() else None
    if u.sender_user_id != owner_id:
        await c.answerCallbackQuery(callback_query_id=u.id, text="Not allowed")
        return
    if row is None:
        await c.answerCallbackQuery(callback_query_id=u.id, text="Already closed")
        return
    await c.answerCallbackQuery(callback_query_id=u.id, text="Marked as read" if action == "r" else "Done ✅")
    if action == "r":
        await set_read(row["id"])
        await mark_read_in_telegram(row)
    elif action == "d":
        await solve(row["id"], "marked done")


@bot.on_updateNewMessage()
async def on_command(c: Client, u: types.UpdateNewMessage) -> None:
    await owner_ready.wait()
    m = u.message
    if m.chat_id != owner_id or m.is_outgoing:
        return
    command = m.text.split()[0].split("@")[0] if m.text else ""
    if command == "/list":
        rows = db.all_open()
        if not rows:
            await bot_send("No open problems 🎉")
            return
        unread = sum(not r["is_read"] for r in rows)
        await bot_send(f"📋 <b>{len(rows)}</b> open problem(s), {unread} unread:")
        for row in rows:
            await move_card_down(row["id"])
    elif command == "/start":
        await bot_send("Hi! Every mention of you in groups will show up here.\n/list: show all open problems")
        for row in db.unsent():
            await send_card(row["id"])


# ------------------------------------------------------------------ main

async def logged_in(client: Client, what: str) -> types.User:
    while not isinstance(client.me, types.User):
        if stopping.is_set():
            raise SystemExit(1)  # the reason was already logged
        if client.authorization_state in ("authorizationStateClosing", "authorizationStateClosed"):
            raise SystemExit(f"{what} login failed. Check API_ID, API_HASH and BOT_TOKEN in .env.")
        await asyncio.sleep(0.2)
    return client.me


async def main() -> None:
    global owner_id, bot_chat_id, shutting_down
    db.init()
    loop = asyncio.get_running_loop()
    # Requests cut off by our own shutdown (e.g. a bot login still in progress) aren't real errors.
    loop.set_exception_handler(lambda lp, ctx: None if shutting_down else lp.default_exception_handler(ctx))
    # Pass the loop explicitly: pytdbot 0.10.1's fallback calls a helper that doesn't exist.
    manager = ClientManager([user, bot], verbosity=2, loop=loop)  # 2 = errors + warnings, into tdlib.log
    await manager.start()
    try:
        me = await logged_in(user, "Telegram account")
        owner_id = me.id
        owner_usernames.update(n.lower() for n in (me.usernames.active_usernames if me.usernames else []))
        # Stay "offline": while an account looks online, Telegram stops push notifications on the phone.
        await user.setOption(name="online", value=types.OptionValueBoolean(value=False))
        bot_me = await logged_in(bot, "Bot")
        bot_chat = await user.searchPublicChat(username=bot_me.usernames.editable_username)
        if isinstance(bot_chat, types.Error):
            log.warning("Couldn't open the bot chat from your account (%s); basic-group cards get "
                        "phone-only links.", bot_chat.message)
        else:
            bot_chat_id = bot_chat.id  # your account's chat with the bot, for the jump messages
        await bot.setCommands(commands=[types.BotCommand(command="list", description="Show all open problems")])
        if isinstance(await bot.createPrivateChat(user_id=owner_id), types.Error):
            log.warning("Open the bot in Telegram and press Start, or it can't message you.")
        owner_ready.set()
        log.info("Tracking mentions of %s. Cards go to @%s.", me.first_name,
                 getattr(bot_me.usernames, "editable_username", "?"))
        await full_sync()
        rows = db.all_open()
        await bot_send(f"🟢 Tracker is running. Open problems: <b>{len(rows)}</b> "
                       f"({sum(not r['is_read'] for r in rows)} unread). /list shows them.", silent=True)
        _heartbeat = asyncio.create_task(heartbeat())
        await stopping.wait()
    finally:
        shutting_down = True
        # Stop the clients while the manager still delivers their "closed" updates, then the manager.
        # (pytdbot's manager.close(close_all_clients=True) stops listening first and hangs.)
        for client in (user, bot):
            with contextlib.suppress(Exception):
                await asyncio.wait_for(client.stop(), timeout=10)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(manager.close(), timeout=5)


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
