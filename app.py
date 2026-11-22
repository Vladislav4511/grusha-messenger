import json
import os
import time
import uuid
from flask import Flask, request, jsonify, render_template_string, make_response

try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
except ImportError:
    psycopg2 = None
    RealDictCursor = None

app = Flask(__name__)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.path.join(BASE_DIR, "db.json")
DATABASE_URL = (os.environ.get("DATABASE_URL") or "").strip()
USE_PG = bool(DATABASE_URL and psycopg2 is not None)

SEVEN_DAYS_MS = 7 * 24 * 60 * 60 * 1000
TYPING_TTL_MS = 4500
OWNER_NICK = "владислав"
TRIPLE_BADGE_GROUP_TITLE = "ырка сквад"
MAX_IMAGE_LEN = 1_500_000
DEFAULT_GROUP_ICON = "🦜"

TYPING_STATUS = {}


def get_pg_conn():
    return psycopg2.connect(DATABASE_URL)


def init_db():
    if USE_PG:
        with get_pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS users (
                        username TEXT PRIMARY KEY,
                        password TEXT NOT NULL,
                        emoji TEXT NOT NULL DEFAULT '🍐',
                        created_at BIGINT NOT NULL,
                        last_nick_change_at BIGINT NOT NULL
                    )
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS messages (
                        id SERIAL PRIMARY KEY,
                        chat_key TEXT NOT NULL,
                        sender TEXT NOT NULL,
                        text TEXT NOT NULL DEFAULT '',
                        image TEXT NOT NULL DEFAULT '',
                        ts BIGINT NOT NULL,
                        reply_to_json TEXT NOT NULL DEFAULT '',
                        reactions_json TEXT NOT NULL DEFAULT '{}'
                    )
                """)
                cur.execute("ALTER TABLE messages ADD COLUMN IF NOT EXISTS reply_to_json TEXT NOT NULL DEFAULT ''")
                cur.execute("ALTER TABLE messages ADD COLUMN IF NOT EXISTS reactions_json TEXT NOT NULL DEFAULT '{}'")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_messages_chat_key ON messages(chat_key)")

                cur.execute("""
                    CREATE TABLE IF NOT EXISTS groups (
                        id TEXT PRIMARY KEY,
                        title TEXT NOT NULL,
                        emoji TEXT NOT NULL DEFAULT '🦜',
                        owner TEXT NOT NULL,
                        members_json TEXT NOT NULL DEFAULT '[]',
                        muted_json TEXT NOT NULL DEFAULT '[]',
                        created_at BIGINT NOT NULL
                    )
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS group_messages (
                        id SERIAL PRIMARY KEY,
                        group_id TEXT NOT NULL,
                        sender TEXT NOT NULL,
                        text TEXT NOT NULL DEFAULT '',
                        image TEXT NOT NULL DEFAULT '',
                        ts BIGINT NOT NULL,
                        reply_to_json TEXT NOT NULL DEFAULT '',
                        reactions_json TEXT NOT NULL DEFAULT '{}'
                    )
                """)
                cur.execute("ALTER TABLE group_messages ADD COLUMN IF NOT EXISTS reply_to_json TEXT NOT NULL DEFAULT ''")
                cur.execute("ALTER TABLE group_messages ADD COLUMN IF NOT EXISTS reactions_json TEXT NOT NULL DEFAULT '{}'")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_group_messages_gid ON group_messages(group_id)")
    else:
        if not os.path.exists(DB_FILE):
            with open(DB_FILE, "w", encoding="utf-8") as f:
                json.dump({"users": {}, "chats": {}, "groups": {}, "group_msgs": {}}, f, ensure_ascii=False, indent=2)


def load_json_db():
    try:
        with open(DB_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            data.setdefault("users", {})
            data.setdefault("chats", {})
            data.setdefault("groups", {})
            data.setdefault("group_msgs", {})
            return data
    except Exception:
        return {"users": {}, "chats": {}, "groups": {}, "group_msgs": {}}


def save_json_db(db):
    tmp_file = DB_FILE + ".tmp"
    with open(tmp_file, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)
    os.replace(tmp_file, DB_FILE)


def chat_key(a, b):
    return "::".join(sorted([a, b]))


def group_typing_key(group_id):
    return f"grp::{group_id}"


def is_owner_nick(username):
    return (username or "").strip().lower() == OWNER_NICK


def is_triple_badge_group(title):
    return (title or "").strip().lower() == TRIPLE_BADGE_GROUP_TITLE


def normalize_group_emoji(em):
    val = (em or "").strip()
    if not val or val == "👥":
        return DEFAULT_GROUP_ICON
    return val


def is_peer_typing(peer, me_nick):
    ts = TYPING_STATUS.get((peer, me_nick), 0)
    return (int(time.time() * 1000) - ts) <= TYPING_TTL_MS


def get_group_typing_users(group_id, exclude_nick):
    now = int(time.time() * 1000)
    target = group_typing_key(group_id)
    active = []
    for (sender, room_id), ts in TYPING_STATUS.items():
        if room_id == target and sender != exclude_nick and (now - ts) <= TYPING_TTL_MS:
            active.append(sender)
    return active


def format_user(row):
    username = row["username"]
    return {
        "username": username,
        "emoji": row.get("emoji") or "🍐",
        "createdAt": int(row.get("createdAt", row.get("created_at", 0)) or 0),
        "lastNickChangeAt": int(row.get("lastNickChangeAt", row.get("last_nick_change_at", 0)) or 0),
        "isOwner": is_owner_nick(username),
    }


def get_user(username):
    if not username:
        return None
    if USE_PG:
        with get_pg_conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT * FROM users WHERE username = %s", (username,))
                return cur.fetchone()
    db = load_json_db()
    return db["users"].get(username)


def parse_json_list(val):
    if isinstance(val, list):
        return val
    try:
        parsed = json.loads(val or "[]")
        return parsed if isinstance(parsed, list) else []
    except Exception:
        return []


def parse_json_dict(val):
    if isinstance(val, dict):
        return val
    try:
        parsed = json.loads(val or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def sanitize_reply_to(raw):
    if not isinstance(raw, dict):
        return None
    sender = str(raw.get("from") or "").strip()
    text = str(raw.get("text") or "").strip()[:140]
    has_image = bool(raw.get("hasImage"))
    if not sender and not text and not has_image:
        return None
    return {
        "from": sender,
        "text": text,
        "hasImage": has_image,
    }


def normalize_reactions(raw):
    if not isinstance(raw, dict):
        return {}
    out = {}
    for em, users in raw.items():
        emoji_key = str(em or "").strip()
        if not emoji_key or not isinstance(users, list):
            continue
        cleaned = []
        seen = set()
        for u in users:
            nick = str(u or "").strip()
            if nick and nick not in seen:
                seen.add(nick)
                cleaned.append(nick)
        if cleaned:
            out[emoji_key] = cleaned
    return out


def format_group_row(row):
    members = parse_json_list(row.get("members_json") if isinstance(row, dict) else getattr(row, "members_json", "[]"))
    muted = parse_json_list(row.get("muted_json") if isinstance(row, dict) else getattr(row, "muted_json", "[]"))
    title = row["title"]
    return {
        "id": row["id"],
        "title": title,
        "emoji": normalize_group_emoji(row.get("emoji")),
        "owner": row["owner"],
        "members": members,
        "muted": muted,
        "createdAt": int(row.get("created_at", 0) or 0),
        "tripleBadge": is_triple_badge_group(title),
    }


def get_group_by_id(group_id):
    if not group_id:
        return None
    if USE_PG:
        with get_pg_conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT * FROM groups WHERE id = %s", (group_id,))
                row = cur.fetchone()
                return format_group_row(row) if row else None
    db = load_json_db()
    g = db["groups"].get(group_id)
    if not g:
        return None
    title = g["title"]
    return {
        "id": g["id"],
        "title": title,
        "emoji": normalize_group_emoji(g.get("emoji")),
        "owner": g["owner"],
        "members": list(g.get("members") or []),
        "muted": list(g.get("muted") or []),
        "createdAt": int(g.get("createdAt") or 0),
        "tripleBadge": is_triple_badge_group(title),
    }


def is_group_title_taken(title, exclude_group_id=None):
    target = (title or "").strip().lower()
    if not target:
        return False
    if USE_PG:
        with get_pg_conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                if exclude_group_id:
                    cur.execute("SELECT id FROM groups WHERE LOWER(TRIM(title)) = %s AND id <> %s", (target, exclude_group_id))
                else:
                    cur.execute("SELECT id FROM groups WHERE LOWER(TRIM(title)) = %s", (target,))
                return cur.fetchone() is not None
    db = load_json_db()
    for gid, g in db["groups"].items():
        if exclude_group_id and gid == exclude_group_id:
            continue
        if str(g.get("title") or "").strip().lower() == target:
            return True
    return False


def format_message_record(msg_id, sender, text, image, ts, reply_to_raw=None, reactions_raw=None):
    item = {
        "id": msg_id,
        "from": sender,
        "text": text or "",
        "ts": int(ts or 0),
        "reactions": normalize_reactions(reactions_raw),
    }
    if image:
        item["image"] = image
    rep = sanitize_reply_to(reply_to_raw)
    if rep:
        item["replyTo"] = rep
    return item


HTML_PAGE = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no, interactive-widget=resizes-content">
<title>Груша</title>
<style>
  :root {
    --msg-font-size: 15px;
    --emoji-size: 24px;
    --photo-max-size: 240px;
    --app-top: 0px;
    --app-height: 100dvh;
    --kb-lift: 0px;
  }
  * { box-sizing: border-box; }
  html, body {
    margin: 0;
    padding: 0;
    background: #101511;
    color: #f3f7f3;
    font-family: system-ui, -apple-system, sans-serif;
    width: 100%;
    height: 100%;
    overflow: hidden;
    overscroll-behavior: none;
  }
  .wrap {
    position: fixed;
    left: 0;
    right: 0;
    top: var(--app-top);
    height: var(--app-height);
    max-width: 480px;
    margin: 0 auto;
    background: #171e18;
    display: flex;
    flex-direction: column;
    overflow: hidden;
    transform: translateY(var(--kb-lift));
    transition: transform 0.16s ease-out;
  }
  .top {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 10px 14px;
    background: #1f2920;
    border-bottom: 1px solid #2b382d;
    gap: 8px;
    flex-shrink: 0;
  }
  .top-actions {
    display: flex;
    align-items: center;
    gap: 8px;
    flex-shrink: 0;
  }
  .brand { font-size: 20px; font-weight: 800; }
  .pad {
    padding: 14px;
    flex: 1;
    display: flex;
    flex-direction: column;
    overflow-y: auto;
    min-height: 0;
  }
  input[type="text"], input[type="password"], input:not([type="range"]):not([type="file"]):not([type="checkbox"]), textarea {
    width: 100%;
    padding: 10px 12px;
    border-radius: 12px;
    border: 1px solid #2f3d31;
    background: #101511;
    color: #fff;
    font-size: 15px;
    font-family: inherit;
    outline: none;
  }
  input:focus, textarea:focus { border-color: #9be15d; }
  input[type="range"] {
    width: 100%;
    accent-color: #9be15d;
    cursor: pointer;
  }
  button {
    padding: 10px 14px;
    border-radius: 10px;
    border: none;
    background: #9be15d;
    color: #101511;
    font-weight: 700;
    font-size: 14px;
    cursor: pointer;
    flex-shrink: 0;
  }
  button.sec { background: #263228; color: #eef5ee; }
  button.del { background: #b93838; color: #fff; }
  button.warn { background: #c88a1e; color: #101511; }
  button.blue-btn { background: #2da9ff; color: #ffffff; }
  .mt-8 { margin-top: 8px; }
  .mt-12 { margin-top: 12px; }
  .err { color: #ff7b7b; font-size: 13px; margin-top: 8px; min-height: 18px; }
  .ok { color: #9be15d; font-size: 13px; margin-top: 8px; }
  .muted { color: #96a598; font-size: 13px; }
  .typing-line {
    font-size: 12px;
    color: #9be15d;
    min-height: 16px;
    line-height: 1.2;
  }
  .typing-line:empty {
    display: none;
  }
  .chat-card {
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 11px;
    border-radius: 12px;
    background: #202a21;
    margin-top: 8px;
    cursor: pointer;
  }
  .ava {
    width: 44px;
    height: 44px;
    border-radius: 50%;
    background: #141a15;
    display: grid;
    place-items: center;
    font-size: 22px;
    flex-shrink: 0;
  }
  .chat-info { min-width: 0; flex: 1; }
  .chat-name { font-weight: 700; font-size: 15px; display: inline-flex; align-items: center; gap: 5px; }
  .chat-last { color: #9aa89c; font-size: 13px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; margin-top: 2px; }
  .chat-last.typing-active { color: #9be15d; font-weight: 600; }
  .verified-badge {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 17px;
    height: 17px;
    border-radius: 50%;
    background: #2da9ff;
    color: #ffffff;
    font-size: 11px;
    font-weight: 900;
    line-height: 1;
    flex-shrink: 0;
  }
  .triple-badges {
    display: inline-flex;
    align-items: center;
    gap: 2px;
    flex-shrink: 0;
  }

  /* Плавающий голубой кружок с карандашом */
  .fab-create {
    position: fixed;
    right: max(18px, calc(50% - 220px));
    bottom: 22px;
    width: 54px;
    height: 54px;
    border-radius: 50%;
    background: #2da9ff;
    color: #ffffff;
    font-size: 22px;
    display: grid;
    place-items: center;
    box-shadow: 0 8px 22px rgba(0, 0, 0, 0.45);
    z-index: 25;
    transition: transform 0.22s ease, opacity 0.22s ease;
  }
  .fab-create.fab-hidden {
    transform: translateY(84px) scale(0.8);
    opacity: 0;
    pointer-events: none;
  }

  /* Выбор участников для группы */
  .member-pick {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 10px 12px;
    border-radius: 12px;
    background: #202a21;
    margin-top: 8px;
    cursor: pointer;
    border: 1px solid transparent;
  }
  .member-pick.selected {
    border-color: #2da9ff;
    background: #1c2b36;
  }
  .member-pick input[type="checkbox"] {
    width: 18px;
    height: 18px;
    accent-color: #2da9ff;
    cursor: pointer;
    flex-shrink: 0;
  }

  /* Настройки группы */
  .group-edit-card {
    padding: 14px;
    border-radius: 14px;
    background: #202a21;
    border: 1px solid #2d3b2f;
    margin-top: 14px;
  }
  .group-member-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    padding: 10px 12px;
    border-radius: 12px;
    background: #202a21;
    margin-top: 8px;
  }
  .role-badge-owner {
    display: inline-block;
    padding: 2px 8px;
    border-radius: 999px;
    background: rgba(45, 169, 255, 0.16);
    color: #5bc0ff;
    font-size: 12px;
    font-weight: 700;
    margin-top: 3px;
  }
  .role-badge-member {
    display: inline-block;
    padding: 2px 8px;
    border-radius: 999px;
    background: #28342a;
    color: #9aa89c;
    font-size: 12px;
    font-weight: 600;
    margin-top: 3px;
  }
  .role-badge-muted {
    display: inline-block;
    padding: 2px 8px;
    border-radius: 999px;
    background: rgba(200, 138, 30, 0.2);
    color: #ffcf70;
    font-size: 12px;
    font-weight: 600;
    margin-top: 3px;
  }
  .group-member-btns {
    display: flex;
    gap: 6px;
    flex-shrink: 0;
  }
  .mini-btn {
    padding: 6px 9px;
    font-size: 12px;
    border-radius: 8px;
  }

  #roomScreen {
    flex: 1;
    display: flex;
    flex-direction: column;
    min-height: 0;
    overflow: hidden;
    position: relative;
  }
  .room-header-main {
    display: flex;
    align-items: center;
    gap: 10px;
    min-width: 0;
    flex: 1;
  }
  .room-header-main.clickable-header {
    cursor: pointer;
    padding: 4px 6px;
    border-radius: 10px;
  }
  .room-header-main.clickable-header:active {
    background: #263228;
  }
  .room-title-text {
    font-size: 15px;
    font-weight: 700;
    display: inline-flex;
    align-items: center;
    gap: 5px;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .msgs {
    flex: 1;
    min-height: 0;
    overflow-y: auto;
    display: flex;
    flex-direction: column;
    gap: 8px;
    padding: 12px 14px;
  }
  .msg-sender {
    font-size: 11px;
    font-weight: 700;
    opacity: 0.78;
    margin-bottom: 3px;
  }
  .reply-quote {
    border-left: 3px solid currentColor;
    padding: 4px 8px;
    margin-bottom: 5px;
    border-radius: 6px;
    background: rgba(0, 0, 0, 0.16);
    font-size: 12px;
    line-height: 1.25;
    opacity: 0.92;
  }
  .reply-quote-author {
    font-weight: 700;
    font-size: 11px;
  }
  .reply-quote-txt {
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    max-width: 240px;
  }
  .m {
    padding: 9px 12px;
    border-radius: 14px;
    max-width: 82%;
    word-break: break-word;
    white-space: pre-wrap;
    font-size: var(--msg-font-size);
    line-height: 1.35;
    user-select: none;
    -webkit-user-select: none;
    -webkit-touch-callout: none;
  }
  .me { align-self: flex-end; background: #9be15d; color: #101511; font-weight: 500; }
  .them { align-self: flex-start; background: #243026; color: #f3f7f3; }
  .msg-img {
    display: block;
    max-width: min(100%, var(--photo-max-size));
    max-height: var(--photo-max-size);
    border-radius: 10px;
    object-fit: cover;
    cursor: pointer;
  }
  .msg-txt { margin-top: 6px; font-size: var(--msg-font-size); white-space: pre-wrap; }
  .msg-txt:first-child { margin-top: 0; }
  .emoji-only-bubble {
    background: transparent !important;
    padding: 2px 4px !important;
    font-size: var(--emoji-size) !important;
    line-height: 1.15 !important;
  }
  .inline-emoji {
    font-size: var(--emoji-size);
    line-height: 1.1;
    vertical-align: middle;
  }
  .reactions-row {
    display: flex;
    flex-wrap: wrap;
    gap: 5px;
    margin-top: 6px;
  }
  .reaction-chip {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    padding: 2px 7px;
    border-radius: 999px;
    font-size: 12px;
    font-weight: 700;
    background: rgba(16, 21, 17, 0.35);
    color: inherit;
    border: 1px solid rgba(255, 255, 255, 0.16);
    cursor: pointer;
  }
  .reaction-chip.reacted-by-me {
    background: rgba(45, 169, 255, 0.28);
    border-color: #2da9ff;
    color: #ffffff;
  }

  /* Меню долгого нажатия на сообщение */
  .msg-menu-backdrop {
    position: fixed;
    inset: 0;
    background: rgba(0, 0, 0, 0.55);
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 18px;
    z-index: 60;
  }
  .msg-menu-card {
    width: 100%;
    max-width: 340px;
    background: #1f2920;
    border: 1px solid #324234;
    border-radius: 16px;
    padding: 14px;
    box-shadow: 0 12px 32px rgba(0, 0, 0, 0.6);
  }
  .msg-menu-preview {
    font-size: 12px;
    color: #a8b7aa;
    background: #151c16;
    padding: 8px 10px;
    border-radius: 10px;
    margin-bottom: 12px;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .reaction-picker-grid {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    margin-top: 10px;
  }
  .reaction-pick-btn {
    background: #28342a;
    color: #fff;
    padding: 7px 10px;
    font-size: 20px;
    line-height: 1;
    border-radius: 10px;
  }

  .reply-bar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    padding: 7px 12px;
    background: #212c22;
    border-top: 1px solid #2f3d31;
    font-size: 12px;
  }
  .reply-bar-info {
    min-width: 0;
    flex: 1;
    border-left: 3px solid #2da9ff;
    padding-left: 8px;
  }
  .reply-bar-title {
    font-weight: 700;
    color: #5bc0ff;
  }
  .reply-bar-text {
    color: #c2d0c3;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .bottom-dock {
    flex-shrink: 0;
    background: #1b231c;
    border-top: 1px solid #28342a;
    padding-bottom: env(safe-area-inset-bottom, 0px);
  }
  .muted-banner {
    padding: 10px 14px;
    background: #2c2115;
    color: #ffcf70;
    font-size: 13px;
    text-align: center;
    border-top: 1px solid #47351d;
  }
  .em-row {
    display: flex;
    align-items: center;
    gap: 6px;
    padding: 6px 12px;
    background: #1b231c;
    overflow-x: auto;
  }
  .em-btn {
    background: #263228;
    color: #fff;
    padding: 5px 9px;
    font-size: var(--emoji-size);
    line-height: 1.1;
    border-radius: 8px;
    flex-shrink: 0;
  }
  .attach-preview {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 8px 14px;
    background: #1f2920;
    border-top: 1px solid #2b382d;
  }
  .attach-preview img {
    width: 52px;
    height: 52px;
    object-fit: cover;
    border-radius: 8px;
    border: 1px solid #394a3c;
  }
  .attach-remove { padding: 6px 10px; font-size: 12px; border-radius: 8px; background: #b93838; color: #fff; margin-left: auto; }
  .composer {
    display: flex;
    align-items: flex-end;
    gap: 6px;
    padding: 8px 12px 10px;
    background: #1b231c;
  }
  .clip-btn {
    background: #263228;
    color: #eef5ee;
    padding: 10px 11px;
    font-size: 17px;
    border-radius: 10px;
    flex-shrink: 0;
  }
  .composer textarea {
    flex: 1;
    min-width: 0;
    min-height: 42px;
    max-height: 120px;
    resize: none;
    overflow-y: auto;
    line-height: 1.35;
  }

  .custom-box {
    margin-top: 22px;
    padding: 14px;
    border-radius: 14px;
    background: #202a21;
    border: 1px solid #2d3b2f;
  }
  .slider-row { margin-top: 12px; }
  .slider-head {
    display: flex;
    justify-content: space-between;
    align-items: center;
    font-size: 13px;
    margin-bottom: 6px;
  }
  .hidden { display: none !important; }
</style>
</head>
<body>
<div class="wrap" id="appRoot">
  <div id="loadingScreen" class="pad" style="justify-content:center; align-items:center;">
    <div class="brand">🍐 Груша</div>
    <div class="muted mt-8">Загрузка аккаунта...</div>
  </div>

  <div id="authScreen" class="pad hidden">
    <div class="brand">🍐 Груша</div>
    <div class="muted mt-8" id="authSub">Войди в свой аккаунт</div>
    <div style="display:flex; gap:8px; margin-top:12px;">
      <button id="btnTabLogin" style="flex:1;" onclick="setAuthTab('login')">Вход</button>
      <button id="btnTabReg" class="sec" style="flex:1;" onclick="setAuthTab('reg')">Регистрация</button>
    </div>
    <input id="authNick" class="mt-12" placeholder="Никнейм" autocomplete="off" autocorrect="off" autocapitalize="none" spellcheck="false">
    <input id="authPass" class="mt-8" type="password" placeholder="Пароль" autocomplete="off">
    <div id="authErr" class="err"></div>
    <button class="mt-8" style="width:100%;" onclick="doAuth()">Продолжить</button>
  </div>

  <div id="chatsScreen" class="hidden" style="flex:1; display:flex; flex-direction:column; min-height:0;">
    <div class="top">
      <div style="display:flex; align-items:center; gap:10px; min-width:0;">
        <div class="ava" id="myAva">🍐</div>
        <div class="chat-name" id="myNick">@user</div>
      </div>
      <div class="top-actions">
        <button class="sec" title="Поиск пользователей" onclick="openSearch()">🔍</button>
        <button class="sec" title="Настройки" onclick="openSettings()">⚙️</button>
      </div>
    </div>
    <div class="pad" id="chatsScrollArea" onscroll="onChatsScroll()">
      <div class="muted" style="font-weight:700;">Чаты</div>
      <div id="chatList" style="padding-bottom:76px;"></div>
    </div>
    <button type="button" id="fabCreateBtn" class="fab-create fab-hidden" title="Создать группу" onclick="openCreateGroup()">✏️</button>
  </div>

  <div id="createGroupScreen" class="hidden" style="flex:1; display:flex; flex-direction:column; min-height:0;">
    <div class="top">
      <button class="sec" onclick="backToChats()">← Назад</button>
      <strong>Создать группу (скоро добавим больше)</strong>
      <span style="width:44px;"></span>
    </div>
    <div class="pad">
      <div style="font-weight:700;">Название группы</div>
      <input id="groupTitleInput" class="mt-8" placeholder="Например: Свои люди" maxlength="40" autocomplete="off">

      <div style="font-weight:700; margin-top:14px;">Иконка группы</div>
      <div id="groupIconPicker" style="display:flex; flex-wrap:wrap; gap:8px; margin-top:8px;"></div>
      <div class="muted mt-8">Выбрана иконка: <strong id="selectedGroupIconLabel">🦜</strong></div>

      <div style="font-weight:700; margin-top:16px;">Добавить участников (из тех, с кем был чат)</div>
      <div id="groupCandidatesList" class="mt-8"></div>

      <div id="createGroupErr" class="err"></div>
      <button type="button" class="blue-btn mt-12" style="width:100%;" onclick="submitCreateGroup()">Создать группу</button>
    </div>
  </div>

  <div id="groupSettingsScreen" class="hidden" style="flex:1; display:flex; flex-direction:column; min-height:0;">
    <div class="top">
      <button class="sec" onclick="backToRoomFromGroupSettings()">← В чат</button>
      <strong>Настройки чата</strong>
      <span style="width:52px;"></span>
    </div>
    <div class="pad" id="groupSettingsBody"></div>
  </div>

  <div id="searchScreen" class="hidden" style="flex:1; display:flex; flex-direction:column; min-height:0;">
    <div class="top">
      <button class="sec" onclick="backToChats()">← Назад</button>
      <strong>🔍 Поиск людей</strong>
      <span style="width:52px;"></span>
    </div>
    <div class="pad">
      <input id="searchInput" placeholder="Введи первые буквы никнейма..." autocomplete="off" autocorrect="off" autocapitalize="none" spellcheck="false" oninput="onSearchInput()">
      <div id="searchHint" class="muted mt-8">Показываются пользователи, чей ник начинается с введённых букв</div>
      <div id="searchResults" class="mt-8"></div>
    </div>
  </div>

  <div id="roomScreen" class="hidden">
    <div class="top">
      <button class="sec" onclick="backToChats()">← Чаты</button>
      <div id="roomHeaderMain" class="room-header-main" onclick="onRoomTitleClick()">
        <div class="ava" id="peerAva">🍐</div>
        <div style="min-width:0; flex:1;">
          <div class="room-title-text" id="peerNick">@peer</div>
          <div class="typing-line" id="peerStatus"></div>
        </div>
      </div>
    </div>
    <div id="msgsBox" class="msgs"></div>
    <div class="bottom-dock" id="bottomDock">
      <div id="mutedBanner" class="muted-banner hidden">Владелец запретил вам писать сообщения в этой группе.</div>
      <div id="activeInputControls">
        <div class="em-row">
          <button type="button" class="em-btn" onclick="insertEmoji('😀')">😀</button>
          <button type="button" class="em-btn" onclick="insertEmoji('😂')">😂</button>
          <button type="button" class="em-btn" onclick="insertEmoji('❤️')">❤️</button>
          <button type="button" class="em-btn" onclick="insertEmoji('🔥')">🔥</button>
          <button type="button" class="em-btn" onclick="insertEmoji('👍')">👍</button>
          <button type="button" class="em-btn" onclick="insertEmoji('🍐')">🍐</button>
          <button type="button" class="em-btn" onclick="insertEmoji('🎉')">🎉</button>
          <button type="button" class="em-btn" onclick="insertEmoji('😭')">😭</button>
        </div>
        <div id="replyBar" class="reply-bar hidden">
          <div class="reply-bar-info">
            <div class="reply-bar-title" id="replyBarAuthor">Ответ @user</div>
            <div class="reply-bar-text" id="replyBarText"></div>
          </div>
          <button type="button" class="mini-btn sec" onclick="clearReplyTarget()">✕</button>
        </div>
        <div id="attachPreview" class="attach-preview hidden">
          <img id="attachThumb" alt="Превью">
          <div class="muted" id="attachName">Картинка прикреплена</div>
          <button type="button" class="attach-remove" onclick="clearAttachment()">Убрать</button>
        </div>
        <div class="composer">
          <input id="fileInput" type="file" accept="image/*" class="hidden" onchange="onFileSelected(event)">
          <button type="button" class="clip-btn" title="Прикрепить картинку" onclick="document.getElementById('fileInput').click()">📎</button>
          <textarea id="msgInput" rows="1" placeholder="Сообщение..." autocomplete="off" spellcheck="false" onfocus="onComposerFocus()" onblur="onComposerBlur()" oninput="onComposerInput()" onkeydown="onComposerKeydown(event)"></textarea>
          <button type="button" onclick="sendMsg()">➤</button>
        </div>
      </div>
    </div>
  </div>

  <div id="msgActionModal" class="msg-menu-backdrop hidden" onclick="closeMsgActionModal(event)">
    <div class="msg-menu-card" onclick="event.stopPropagation()">
      <div class="msg-menu-preview" id="msgActionPreview">Сообщение</div>
      <button type="button" class="blue-btn" style="width:100%;" onclick="startReplyToSelectedMsg()">↩️ Ответить на сообщение</button>
      <div class="muted mt-12" style="font-weight:700;">Ответить реакцией:</div>
      <div id="msgReactionChoices" class="reaction-picker-grid"></div>
      <button type="button" class="sec mt-12" style="width:100%;" onclick="closeMsgActionModal()">Отмена</button>
    </div>
  </div>

  <div id="settingsScreen" class="hidden" style="flex:1; display:flex; flex-direction:column; min-height:0;">
    <div class="top">
      <button class="sec" onclick="backToChats()">← Назад</button>
      <strong>Настройки</strong>
      <span style="width:52px;"></span>
    </div>
    <div class="pad">
      <div style="font-weight:700;">Аватарка (только эмодзи)</div>
      <div id="avaPicker" style="display:flex; flex-wrap:wrap; gap:8px; margin-top:8px;"></div>

      <div style="font-weight:700; margin-top:18px;">Изменить никнейм</div>
      <div id="nickHint" class="muted mt-8"></div>
      <input id="newNickInput" class="mt-8" autocomplete="off" autocorrect="off" autocapitalize="none" spellcheck="false">
      <button class="mt-8" style="width:100%;" id="btnSaveNick" onclick="changeNick()">Сохранить никнейм</button>

      <div id="setErr" class="err"></div>
      <div id="setOk" class="ok"></div>

      <button class="sec mt-12" style="width:100%;" onclick="logout()">Выйти из аккаунта</button>
      <button class="del mt-8" style="width:100%;" onclick="deleteAccount()">Удалить аккаунт</button>

      <div class="custom-box" id="customizationTab">
        <div style="font-weight:800; font-size:15px;">🎨 Кастомизация</div>
        <div class="muted mt-8">Настрой размер элементов под себя</div>

        <div class="slider-row">
          <div class="slider-head">
            <span>Размер сообщений</span>
            <strong id="msgSizeVal">15px</strong>
          </div>
          <input id="msgSizeRange" type="range" min="12" max="22" step="1" oninput="updateCustomization()">
        </div>

        <div class="slider-row">
          <div class="slider-head">
            <span>Размер смайликов</span>
            <strong id="emojiSizeVal">24px</strong>
          </div>
          <input id="emojiSizeRange" type="range" min="18" max="44" step="2" oninput="updateCustomization()">
        </div>

        <div class="slider-row">
          <div class="slider-head">
            <span>Размер фотографий</span>
            <strong id="photoSizeVal">240px</strong>
          </div>
          <input id="photoSizeRange" type="range" min="140" max="360" step="10" oninput="updateCustomization()">
        </div>

        <button type="button" class="sec mt-12" style="width:100%;" onclick="resetCustomization()">Сбросить кастомизацию</button>
      </div>
    </div>
  </div>
</div>

<script>
const AVATAR_EMOJIS = [
  "🍐", "😎", "🐱", "🐸", "🔥", "👾",
  "⚡", "🍕", "🦊", "🐼", "🤖", "👻",
  "🦄", "🐉", "🌙", "⭐", "💎", "🎮",
  "🎧", "🚀", "🌵", "🍀", "🍓", "👑",
  "🐄", "🐗", "🔶"
];

const GROUP_ICONS = [
  "🦜", "🍐", "🔥", "⚡", "🎮", "🚀",
  "🍕", "👑", "💎", "🐗", "🐄", "🔶"
];

const REACTION_EMOJIS = [
  "😀", "😂", "❤️", "🔥", "👍", "🍐", "🎉", "😭"
];

const DEFAULT_CUSTOM = {
  msgSize: 15,
  emojiSize: 24,
  photoSize: 240
};

const EMOJI_ONLY_RE = /^(?:[\p{Emoji_Presentation}\p{Extended_Pictographic}]\uFE0F?(?:\u200D[\p{Emoji_Presentation}\p{Extended_Pictographic}]\uFE0F?)*|\s)+$/u;
const SINGLE_EMOJI_RE = /([\p{Emoji_Presentation}\p{Extended_Pictographic}]\uFE0F?(?:\u200D[\p{Emoji_Presentation}\p{Extended_Pictographic}]\uFE0F?)*)/gu;

let me = null;
let authTab = "login";
let activeChat = null; // { type: 'direct' | 'group', id, username, emoji, isOwner, tripleBadge }
let pendingImageData = "";
let replyTarget = null; // { from, text, hasImage }
let currentRoomMsgs = [];
let selectedMsgForAction = null;
let suppressNextBubbleClick = false;
let longPressTimer = null;

let lastRenderedMsgsJson = "";
let lastRenderedCustomJson = "";
let lastTypingPingTs = 0;
let pollTimer = null;
let fabPulseTimer = null;
let lastChatsScrollTop = 0;

let selectedGroupEmoji = "🦜";
let selectedGroupMembers = new Set();
let editGroupEmoji = "🦜";

let composerFocused = false;
let baselineViewportHeight = window.visualViewport ? window.visualViewport.height : window.innerHeight;

function isSystemOwnerNick(nick) {
  return String(nick || "").trim().toLowerCase() === "владислав";
}

function isTripleBadgeGroupTitle(title) {
  return String(title || "").trim().toLowerCase() === "ырка сквад";
}

function groupTitleHtml(title, tripleBadge = false) {
  const showTriple = Boolean(tripleBadge) || isTripleBadgeGroupTitle(title);
  if (!showTriple) return esc(title);
  return `${esc(title)} <span class="triple-badges" title="Подтверждённая группа"><span class="verified-badge">✓</span><span class="verified-badge">✓</span><span class="verified-badge">✓</span></span>`;
}

function setKeyboardLift(px) {
  const safePx = Math.max(0, Math.min(Math.round(px), Math.round(window.innerHeight * 0.55)));
  document.documentElement.style.setProperty("--kb-lift", `${-safePx}px`);
}

function syncViewportHeight() {
  const vv = window.visualViewport;
  const currentH = vv ? vv.height : window.innerHeight;

  if (!composerFocused && currentH > baselineViewportHeight * 0.85) {
    baselineViewportHeight = Math.max(baselineViewportHeight, currentH);
  }

  if (vv) {
    document.documentElement.style.setProperty("--app-top", `${Math.max(0, Math.round(vv.offsetTop))}px`);
    document.documentElement.style.setProperty("--app-height", `${Math.round(vv.height)}px`);
  } else {
    document.documentElement.style.setProperty("--app-top", "0px");
    document.documentElement.style.setProperty("--app-height", `${window.innerHeight}px`);
  }

  const viewportShrank = baselineViewportHeight - currentH > 90;
  if (viewportShrank || !composerFocused) {
    setKeyboardLift(0);
  }

  if (window.scrollY !== 0) {
    window.scrollTo(0, 0);
  }
  const box = document.getElementById("msgsBox");
  if (box && activeChat) {
    box.scrollTop = box.scrollHeight;
  }
}

function applyFallbackLiftIfNeeded() {
  if (!composerFocused) return;
  const vv = window.visualViewport;
  const currentH = vv ? vv.height : window.innerHeight;
  const alreadyResized = baselineViewportHeight - currentH > 90;

  if (alreadyResized) {
    setKeyboardLift(0);
    return;
  }

  const estimatedKb = Math.round(window.innerHeight * 0.42);
  setKeyboardLift(estimatedKb);
}

function onComposerFocus() {
  composerFocused = true;
  [30, 140, 320, 550].forEach(delay => {
    setTimeout(() => {
      syncViewportHeight();
      applyFallbackLiftIfNeeded();
      const box = document.getElementById("msgsBox");
      if (box) box.scrollTop = box.scrollHeight;
    }, delay);
  });
}

function onComposerBlur() {
  setTimeout(() => {
    const active = document.activeElement;
    const stillInDock = active && active.closest && active.closest("#bottomDock");
    if (!stillInDock) {
      composerFocused = false;
      setKeyboardLift(0);
      syncViewportHeight();
    }
  }, 120);
}

if (window.visualViewport) {
  window.visualViewport.addEventListener("resize", syncViewportHeight);
  window.visualViewport.addEventListener("scroll", syncViewportHeight);
}
window.addEventListener("resize", syncViewportHeight);
window.addEventListener("orientationchange", () => setTimeout(syncViewportHeight, 150));
syncViewportHeight();

function setFabVisible(visible) {
  const fab = document.getElementById("fabCreateBtn");
  if (!fab) return;
  fab.classList.toggle("fab-hidden", !visible);
}

function triggerFabAppearCycle() {
  const chatsScreen = document.getElementById("chatsScreen");
  if (!chatsScreen || chatsScreen.classList.contains("hidden")) {
    setFabVisible(false);
    return;
  }
  setFabVisible(true);
  if (fabPulseTimer) clearTimeout(fabPulseTimer);
  fabPulseTimer = setTimeout(() => {
    const area = document.getElementById("chatsScrollArea");
    if (area && area.scrollTop > 24) {
      setFabVisible(false);
    }
  }, 3800);
}

function onChatsScroll() {
  const area = document.getElementById("chatsScrollArea");
  if (!area) return;
  const cur = area.scrollTop;
  if (cur > lastChatsScrollTop + 8) {
    setFabVisible(false);
  } else if (cur < lastChatsScrollTop - 8 || cur <= 10) {
    triggerFabAppearCycle();
  }
  lastChatsScrollTop = cur;
}

function autoResizeComposer() {
  const inp = document.getElementById("msgInput");
  if (!inp) return;
  inp.style.height = "auto";
  const next = Math.min(inp.scrollHeight, 120);
  inp.style.height = `${Math.max(42, next)}px`;
}

function sendTypingSignal(force = false) {
  if (!me || !activeChat) return;
  const now = Date.now();
  if (!force && now - lastTypingPingTs < 1500) return;
  lastTypingPingTs = now;
  if (activeChat.type === "group") {
    api("/api/typing", { from: me.username, groupId: activeChat.id }).catch(() => {});
  } else {
    api("/api/typing", { from: me.username, to: activeChat.username }).catch(() => {});
  }
}

function onComposerInput() {
  autoResizeComposer();
  const inp = document.getElementById("msgInput");
  if (inp && inp.value.trim().length > 0) {
    sendTypingSignal(false);
  }
}

function onComposerKeydown(event) {
  const isTouchDevice = "ontouchstart" in window || navigator.maxTouchPoints > 0;
  if (event.key === "Enter" && !event.shiftKey && !isTouchDevice) {
    event.preventDefault();
    sendMsg();
  }
}

function loadCustom() {
  try {
    const raw = JSON.parse(localStorage.getItem("grusha_custom") || "{}");
    return {
      msgSize: Number(raw.msgSize) || DEFAULT_CUSTOM.msgSize,
      emojiSize: Number(raw.emojiSize) || DEFAULT_CUSTOM.emojiSize,
      photoSize: Number(raw.photoSize) || DEFAULT_CUSTOM.photoSize
    };
  } catch (e) {
    return { ...DEFAULT_CUSTOM };
  }
}

function applyCustom(cfg) {
  document.documentElement.style.setProperty("--msg-font-size", `${cfg.msgSize}px`);
  document.documentElement.style.setProperty("--emoji-size", `${cfg.emojiSize}px`);
  document.documentElement.style.setProperty("--photo-max-size", `${cfg.photoSize}px`);

  const rMsg = document.getElementById("msgSizeRange");
  const rEm = document.getElementById("emojiSizeRange");
  const rPh = document.getElementById("photoSizeRange");
  if (rMsg) rMsg.value = cfg.msgSize;
  if (rEm) rEm.value = cfg.emojiSize;
  if (rPh) rPh.value = cfg.photoSize;

  const vMsg = document.getElementById("msgSizeVal");
  const vEm = document.getElementById("emojiSizeVal");
  const vPh = document.getElementById("photoSizeVal");
  if (vMsg) vMsg.textContent = `${cfg.msgSize}px`;
  if (vEm) vEm.textContent = `${cfg.emojiSize}px`;
  if (vPh) vPh.textContent = `${cfg.photoSize}px`;
}

function updateCustomization() {
  const cfg = {
    msgSize: Number(document.getElementById("msgSizeRange").value) || DEFAULT_CUSTOM.msgSize,
    emojiSize: Number(document.getElementById("emojiSizeRange").value) || DEFAULT_CUSTOM.emojiSize,
    photoSize: Number(document.getElementById("photoSizeRange").value) || DEFAULT_CUSTOM.photoSize
  };
  try { localStorage.setItem("grusha_custom", JSON.stringify(cfg)); } catch (e) {}
  applyCustom(cfg);
  if (activeChat) {
    lastRenderedMsgsJson = "";
    refreshMessages(false);
  }
}

function resetCustomization() {
  try { localStorage.setItem("grusha_custom", JSON.stringify(DEFAULT_CUSTOM)); } catch (e) {}
  applyCustom(DEFAULT_CUSTOM);
  if (activeChat) {
    lastRenderedMsgsJson = "";
    refreshMessages(false);
  }
}

function renderAvatarChoices() {
  const box = document.getElementById("avaPicker");
  if (!box) return;
  box.innerHTML = AVATAR_EMOJIS.map(em =>
    `<button type="button" class="em-btn" onclick="setEmoji('${em}')">${em}</button>`
  ).join("");
}

function isFullscreenNow() {
  return Boolean(document.fullscreenElement || document.webkitFullscreenElement);
}

async function toggleFullscreenByTap(e) {
  if (!e.target.closest) return;
  const interactive = e.target.closest(
    "button, input, textarea, select, a, .chat-card, .member-pick, .group-member-row, .group-edit-card, .room-header-main, .m, .msg-img, .em-btn, .clip-btn, .attach-preview, .reply-bar, .msg-menu-card, .reaction-chip, .top, .bottom-dock, .custom-box, .fab-create"
  );
  if (interactive) return;

  try {
    if (!isFullscreenNow()) {
      const el = document.documentElement;
      if (el.requestFullscreen) await el.requestFullscreen();
      else if (el.webkitRequestFullscreen) el.webkitRequestFullscreen();
    } else {
      if (document.exitFullscreen) await document.exitFullscreen();
      else if (document.webkitExitFullscreen) document.webkitExitFullscreen();
    }
  } catch (err) {}
}

document.addEventListener("click", toggleFullscreenByTap);

function nickLabelHtml(username, isOwner) {
  const badge = isOwner ? ` <span class="verified-badge" title="Владелец">✓</span>` : "";
  return `@${esc(username)}${badge}`;
}

async function api(url, body = null) {
  const opts = body
    ? { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }
    : { credentials: "same-origin" };
  const res = await fetch(url, opts);
  return await res.json();
}

function saveLocalUser(user) {
  me = user;
  try {
    if (user) localStorage.setItem("grusha_nick", user.username);
    else localStorage.removeItem("grusha_nick");
  } catch (e) {}
}

function showScreen(id) {
  composerFocused = false;
  setKeyboardLift(0);
  closeMsgActionModal();
  ["loadingScreen", "authScreen", "chatsScreen", "createGroupScreen", "groupSettingsScreen", "searchScreen", "roomScreen", "settingsScreen"].forEach(s => {
    document.getElementById(s).classList.toggle("hidden", s !== id);
  });
  setFabVisible(id === "chatsScreen");
  if (id === "chatsScreen") {
    triggerFabAppearCycle();
  }
  syncViewportHeight();
}

function setAuthTab(tab) {
  authTab = tab;
  document.getElementById("btnTabLogin").classList.toggle("sec", tab !== "login");
  document.getElementById("btnTabReg").classList.toggle("sec", tab !== "reg");
  document.getElementById("authSub").textContent = tab === "login" ? "Войди в свой аккаунт" : "Создай аккаунт по никнейму и паролю";
  document.getElementById("authErr").textContent = "";
}

async function doAuth() {
  const u = document.getElementById("authNick").value.trim();
  const p = document.getElementById("authPass").value;
  const res = await api(authTab === "login" ? "/api/login" : "/api/register", { username: u, password: p });
  if (!res.ok) {
    document.getElementById("authErr").textContent = res.error || "Ошибка";
    return;
  }
  saveLocalUser(res.user);
  document.getElementById("authErr").textContent = "";
  enterChats();
}

function enterChats() {
  activeChat = null;
  replyTarget = null;
  clearAttachment();
  lastRenderedMsgsJson = "";
  document.getElementById("myAva").textContent = me.emoji || "🍐";
  document.getElementById("myNick").innerHTML = nickLabelHtml(me.username, me.isOwner);
  showScreen("chatsScreen");
  refreshChats();
  startPolling();
}

function backToChats() {
  activeChat = null;
  replyTarget = null;
  clearAttachment();
  lastRenderedMsgsJson = "";
  showScreen("chatsScreen");
  refreshChats();
}

function backToRoomFromGroupSettings() {
  if (!activeChat) {
    backToChats();
    return;
  }
  showScreen("roomScreen");
  refreshMessages(false);
}

/* Создание группы */
function selectGroupEmoji(em) {
  selectedGroupEmoji = em;
  document.getElementById("selectedGroupIconLabel").textContent = em;
  renderGroupIconPicker();
}

function renderGroupIconPicker() {
  const box = document.getElementById("groupIconPicker");
  if (!box) return;
  box.innerHTML = GROUP_ICONS.map(em => {
    const cls = em === selectedGroupEmoji ? "em-btn" : "em-btn sec";
    return `<button type="button" class="${cls}" style="${em === selectedGroupEmoji ? 'outline:2px solid #2da9ff;' : ''}" onclick="selectGroupEmoji('${em}')">${em}</button>`;
  }).join("");
}

function toggleGroupMember(nick) {
  if (selectedGroupMembers.has(nick)) selectedGroupMembers.delete(nick);
  else selectedGroupMembers.add(nick);
  renderGroupCandidates();
}

function renderGroupCandidates(candidates = null) {
  const box = document.getElementById("groupCandidatesList");
  if (!box) return;
  if (candidates) {
    box.dataset.candidates = JSON.stringify(candidates);
  }
  let list = candidates;
  if (!list) {
    try { list = JSON.parse(box.dataset.candidates || "[]"); } catch (e) { list = []; }
  }
  if (!list.length) {
    box.innerHTML = `<div class="muted">Нет собеседников, с которыми уже был личный чат. Сначала напиши кому-нибудь в личку.</div>`;
    return;
  }
  box.innerHTML = list.map(u => {
    const checked = selectedGroupMembers.has(u.username);
    return `
      <div class="member-pick ${checked ? 'selected' : ''}" onclick="toggleGroupMember('${esc(u.username)}')">
        <input type="checkbox" ${checked ? 'checked' : ''} onclick="event.stopPropagation(); toggleGroupMember('${esc(u.username)}')">
        <div class="ava" style="width:36px; height:36px; font-size:18px;">${u.emoji || '🍐'}</div>
        <div class="chat-name">${nickLabelHtml(u.username, u.isOwner)}</div>
      </div>
    `;
  }).join("");
}

async function openCreateGroup() {
  selectedGroupEmoji = "🦜";
  selectedGroupMembers = new Set();
  document.getElementById("groupTitleInput").value = "";
  document.getElementById("selectedGroupIconLabel").textContent = selectedGroupEmoji;
  document.getElementById("createGroupErr").textContent = "";
  renderGroupIconPicker();
  renderGroupCandidates([]);
  showScreen("createGroupScreen");

  const res = await api(`/api/chats?user=${encodeURIComponent(me.username)}`);
  const directPeers = (res.ok && Array.isArray(res.chats))
    ? res.chats.filter(c => c.type !== "group")
    : [];
  renderGroupCandidates(directPeers);
}

async function submitCreateGroup() {
  const title = document.getElementById("groupTitleInput").value.trim();
  const errEl = document.getElementById("createGroupErr");
  if (!title) {
    errEl.textContent = "Введите название группы";
    return;
  }
  errEl.textContent = "";
  const res = await api("/api/groups/create", {
    owner: me.username,
    title,
    emoji: selectedGroupEmoji,
    members: Array.from(selectedGroupMembers)
  });
  if (!res.ok) {
    errEl.textContent = res.error || "Не удалось создать группу";
    return;
  }
  await openGroupChat(res.group.id, res.group.title, res.group.emoji, res.group.tripleBadge);
}

/* Настройки группы при нажатии на шапку чата */
function onRoomTitleClick() {
  if (!activeChat || activeChat.type !== "group") return;
  openGroupSettings();
}

async function openGroupSettings() {
  if (!activeChat || activeChat.type !== "group") return;
  showScreen("groupSettingsScreen");
  await renderGroupSettings();
}

function selectEditGroupEmoji(em) {
  editGroupEmoji = em;
  const lbl = document.getElementById("editGroupIconLabel");
  if (lbl) lbl.textContent = em;
  renderEditGroupIconPicker();
}

function renderEditGroupIconPicker() {
  const box = document.getElementById("editGroupIconPicker");
  if (!box) return;
  box.innerHTML = GROUP_ICONS.map(em => {
    const cls = em === editGroupEmoji ? "em-btn" : "em-btn sec";
    return `<button type="button" class="${cls}" style="${em === editGroupEmoji ? 'outline:2px solid #2da9ff;' : ''}" onclick="selectEditGroupEmoji('${em}')">${em}</button>`;
  }).join("");
}

async function saveGroupMeta() {
  if (!activeChat || activeChat.type !== "group") return;
  const inp = document.getElementById("editGroupTitleInput");
  const errEl = document.getElementById("editGroupErr");
  const okEl = document.getElementById("editGroupOk");
  const newTitle = (inp ? inp.value : "").trim();
  if (!newTitle) {
    if (errEl) errEl.textContent = "Введите название группы";
    if (okEl) okEl.textContent = "";
    return;
  }
  const res = await api("/api/groups/update", {
    actor: me.username,
    groupId: activeChat.id,
    title: newTitle,
    emoji: editGroupEmoji
  });
  if (!res.ok) {
    if (errEl) errEl.textContent = res.error || "Не удалось сохранить изменения";
    if (okEl) okEl.textContent = "";
    return;
  }
  activeChat.username = res.group.title;
  activeChat.emoji = res.group.emoji;
  activeChat.tripleBadge = Boolean(res.group.tripleBadge);
  document.getElementById("peerAva").textContent = res.group.emoji || "🦜";
  document.getElementById("peerNick").innerHTML = groupTitleHtml(res.group.title, activeChat.tripleBadge);
  await renderGroupSettings();
  const okAfter = document.getElementById("editGroupOk");
  if (okAfter) okAfter.textContent = "Название и иконка обновлены!";
  await refreshChats();
}

async function renderGroupSettings() {
  const body = document.getElementById("groupSettingsBody");
  if (!body || !activeChat || activeChat.type !== "group") return;

  body.innerHTML = `<div class="muted">Загрузка настроек группы...</div>`;

  const res = await api(`/api/groups/info?groupId=${encodeURIComponent(activeChat.id)}&user=${encodeURIComponent(me.username)}`);
  if (!res.ok || !res.group) {
    body.innerHTML = `<div class="muted">Группа не найдена или была удалена.</div><button class="sec mt-12" onclick="backToChats()">← К чатам</button>`;
    return;
  }

  const g = res.group;
  const isGroupOwner = g.owner === me.username;
  const mutedSet = new Set(g.muted || []);
  editGroupEmoji = g.emoji || "🦜";

  activeChat.username = g.title;
  activeChat.emoji = editGroupEmoji;
  activeChat.tripleBadge = Boolean(g.tripleBadge);
  document.getElementById("peerAva").textContent = editGroupEmoji;
  document.getElementById("peerNick").innerHTML = groupTitleHtml(g.title, activeChat.tripleBadge);

  const ownerEditMetaHtml = isGroupOwner ? `
    <div class="group-edit-card">
      <div style="font-weight:700;">Изменить название и иконку</div>
      <input id="editGroupTitleInput" class="mt-8" value="${esc(g.title)}" maxlength="40" placeholder="Название группы" autocomplete="off">
      <div id="editGroupIconPicker" style="display:flex; flex-wrap:wrap; gap:8px; margin-top:10px;"></div>
      <div class="muted mt-8">Выбрана иконка: <strong id="editGroupIconLabel">${editGroupEmoji}</strong></div>
      <div id="editGroupErr" class="err"></div>
      <div id="editGroupOk" class="ok"></div>
      <button type="button" class="blue-btn mt-8" style="width:100%;" onclick="saveGroupMeta()">Сохранить изменения</button>
    </div>
  ` : "";

  const orderedMembers = [
    g.owner,
    ...(g.members || []).filter(m => m !== g.owner)
  ];

  const membersHtml = orderedMembers.map(nick => {
    const isMemberOwner = nick === g.owner;
    const isMuted = mutedSet.has(nick);
    const isMeTag = nick === me.username ? ` <span class="muted" style="font-weight:500;">(вы)</span>` : "";

    let roleBadgeHtml = `<span class="role-badge-member">Обычный участник</span>`;
    if (isMemberOwner) {
      roleBadgeHtml = `<span class="role-badge-owner">👑 Владелец</span>`;
    } else if (isMuted) {
      roleBadgeHtml = `<span class="role-badge-muted">Обычный участник • не может писать</span>`;
    }

    const controlsHtml = (isGroupOwner && !isMemberOwner) ? `
      <div class="group-member-btns">
        <button type="button" class="mini-btn ${isMuted ? 'ok' : 'warn'}" onclick="toggleMuteMember('${esc(nick)}')">
          ${isMuted ? 'Разрешить писать' : 'Запретить писать'}
        </button>
        <button type="button" class="mini-btn del" onclick="kickGroupMember('${esc(nick)}')">Удалить</button>
      </div>
    ` : "";

    return `
      <div class="group-member-row">
        <div style="min-width:0;">
          <div class="chat-name">${nickLabelHtml(nick, isSystemOwnerNick(nick))}${isMeTag}</div>
          <div>${roleBadgeHtml}</div>
        </div>
        ${controlsHtml}
      </div>
    `;
  }).join("");

  const ownerDeleteHtml = isGroupOwner ? `
    <div style="margin-top:22px; padding-top:14px; border-top:1px solid #2b382d;">
      <div style="font-weight:700; color:#ff8c8c;">Управление группой (только владелец)</div>
      <button type="button" class="del mt-8" style="width:100%;" onclick="deleteCurrentGroup()">Удалить группу</button>
    </div>
  ` : "";

  body.innerHTML = `
    <div style="display:flex; align-items:center; gap:12px;">
      <div class="ava" style="width:52px; height:52px; font-size:26px;">${editGroupEmoji}</div>
      <div>
        <div style="font-size:18px; font-weight:800; display:flex; align-items:center; gap:5px;">${groupTitleHtml(g.title, activeChat.tripleBadge)}</div>
        <div class="muted">Владелец: ${nickLabelHtml(g.owner, isSystemOwnerNick(g.owner))}</div>
      </div>
    </div>

    ${ownerEditMetaHtml}

    <div style="font-weight:700; margin-top:18px;">Участники (${orderedMembers.length})</div>
    <div id="groupMembersList">${membersHtml}</div>
    ${ownerDeleteHtml}
  `;

  if (isGroupOwner) {
    renderEditGroupIconPicker();
  }
}

async function toggleMuteMember(targetNick) {
  if (!activeChat || activeChat.type !== "group") return;
  const res = await api("/api/groups/mute", {
    actor: me.username,
    groupId: activeChat.id,
    target: targetNick
  });
  if (res.ok) {
    await renderGroupSettings();
    await refreshMessages(false);
  }
}

async function kickGroupMember(targetNick) {
  if (!activeChat || activeChat.type !== "group") return;
  const res = await api("/api/groups/kick", {
    actor: me.username,
    groupId: activeChat.id,
    target: targetNick
  });
  if (res.ok) {
    await renderGroupSettings();
    await refreshMessages(false);
    await refreshChats();
  }
}

async function deleteCurrentGroup() {
  if (!activeChat || activeChat.type !== "group") return;
  const res = await api("/api/groups/delete", {
    actor: me.username,
    groupId: activeChat.id
  });
  if (res.ok) {
    backToChats();
  }
}

function openSettings() {
  renderAvatarChoices();
  applyCustom(loadCustom());
  document.getElementById("setErr").textContent = "";
  document.getElementById("setOk").textContent = "";
  const leftMs = 7 * 24 * 3600 * 1000 - (Date.now() - (me.lastNickChangeAt || me.createdAt || Date.now()));
  const canChange = leftMs > 0;
  const days = Math.max(0, Math.floor(leftMs / 86400000));
  document.getElementById("nickHint").textContent = canChange
    ? `Можно сменить ещё примерно ${days} дн. (доступно только первые 7 дней)`
    : "Срок смены никнейма (7 дней) истёк";
  const inp = document.getElementById("newNickInput");
  inp.value = me.username;
  inp.disabled = !canChange;
  document.getElementById("btnSaveNick").disabled = !canChange;
  showScreen("settingsScreen");
}

function openSearch() {
  const inp = document.getElementById("searchInput");
  inp.value = "";
  document.getElementById("searchResults").innerHTML = "";
  showScreen("searchScreen");
  setTimeout(() => inp.focus(), 60);
}

async function onSearchInput() {
  const q = document.getElementById("searchInput").value.trim();
  const box = document.getElementById("searchResults");
  if (!q) {
    box.innerHTML = "";
    return;
  }
  const res = await api(`/api/search?q=${encodeURIComponent(q)}&exclude=${encodeURIComponent(me.username)}`);
  if (!res.users || !res.users.length) {
    box.innerHTML = `<div class="muted mt-8">Никого не найдено на «${esc(q)}»</div>`;
    return;
  }
  box.innerHTML = res.users.map(u => `
    <div class="chat-card" onclick="openDirectChat('${esc(u.username)}', '${esc(u.emoji || '🍐')}', ${u.isOwner ? 'true' : 'false'})">
      <div class="ava">${u.emoji || '🍐'}</div>
      <div class="chat-info">
        <div class="chat-name">${nickLabelHtml(u.username, u.isOwner)}</div>
        <div class="chat-last">Нажми, чтобы открыть чат</div>
      </div>
    </div>
  `).join("");
}

async function refreshChats() {
  if (!me) return;
  const res = await api(`/api/chats?user=${encodeURIComponent(me.username)}`);
  const list = document.getElementById("chatList");
  if (!list || !res.ok) return;

  if (activeChat) {
    if (activeChat.type === "direct") {
      const matched = res.chats.find(c => c.type !== "group" && c.username === activeChat.username);
      if (matched && matched.emoji && matched.emoji !== activeChat.emoji) {
        activeChat.emoji = matched.emoji;
        const peerAvaEl = document.getElementById("peerAva");
        if (peerAvaEl) peerAvaEl.textContent = matched.emoji;
      }
    } else if (activeChat.type === "group") {
      const matchedGroup = res.chats.find(c => c.type === "group" && c.groupId === activeChat.id);
      if (!matchedGroup) {
        backToChats();
        return;
      }
      activeChat.username = matchedGroup.title;
      activeChat.emoji = matchedGroup.emoji;
      activeChat.tripleBadge = Boolean(matchedGroup.tripleBadge);
      document.getElementById("peerAva").textContent = matchedGroup.emoji || "🦜";
      document.getElementById("peerNick").innerHTML = groupTitleHtml(matchedGroup.title, activeChat.tripleBadge);
    }
  }

  if (!res.chats.length) {
    list.innerHTML = `<div class="muted mt-12">Чатов пока нет. Нажми на 🔍 в правом верхнем углу или на синий ✏️ внизу.</div>`;
    return;
  }
  list.innerHTML = res.chats.map(c => {
    if (c.type === "group") {
      const typingLabel = c.typingText ? `${esc(c.typingText)} печатает...` : "";
      return `
        <div class="chat-card" onclick="openGroupChat('${esc(c.groupId)}', '${esc(c.title)}', '${esc(c.emoji || '🦜')}', ${c.tripleBadge ? 'true' : 'false'})">
          <div class="ava">${c.emoji || '🦜'}</div>
          <div class="chat-info">
            <div class="chat-name">${groupTitleHtml(c.title, c.tripleBadge)} <span class="muted" style="font-weight:500; font-size:12px;">• группа</span></div>
            <div class="chat-last ${typingLabel ? 'typing-active' : ''}">${typingLabel || esc(c.last || 'Сообщений пока нет')}</div>
          </div>
        </div>
      `;
    }
    return `
      <div class="chat-card" onclick="openDirectChat('${esc(c.username)}', '${esc(c.emoji || '🍐')}', ${c.isOwner ? 'true' : 'false'})">
        <div class="ava">${c.emoji || '🍐'}</div>
        <div class="chat-info">
          <div class="chat-name">${nickLabelHtml(c.username, c.isOwner)}</div>
          <div class="chat-last ${c.typing ? 'typing-active' : ''}">${c.typing ? 'печатает...' : esc(c.last || 'Сообщений пока нет')}</div>
        </div>
      </div>
    `;
  }).join("");
}

function syncRoomComposerLock(isMuted) {
  const banner = document.getElementById("mutedBanner");
  const controls = document.getElementById("activeInputControls");
  if (banner) banner.classList.toggle("hidden", !isMuted);
  if (controls) controls.classList.toggle("hidden", Boolean(isMuted));
}

function clearReplyTarget() {
  replyTarget = null;
  const bar = document.getElementById("replyBar");
  if (bar) bar.classList.add("hidden");
}

function setReplyTarget(msg) {
  if (!msg) return;
  replyTarget = {
    from: msg.from,
    text: (msg.text || "").trim(),
    hasImage: Boolean(msg.image)
  };
  const bar = document.getElementById("replyBar");
  const authorEl = document.getElementById("replyBarAuthor");
  const txtEl = document.getElementById("replyBarText");
  if (bar && authorEl && txtEl) {
    authorEl.textContent = `Ответ @${replyTarget.from}`;
    txtEl.textContent = replyTarget.text || (replyTarget.hasImage ? "📷 Картинка" : "Сообщение");
    bar.classList.remove("hidden");
  }
  const inp = document.getElementById("msgInput");
  if (inp) inp.focus();
}

function openMsgActionModal(msgIndex) {
  const msg = currentRoomMsgs[msgIndex];
  if (!msg) return;
  selectedMsgForAction = msg;
  const prev = document.getElementById("msgActionPreview");
  const snippet = (msg.text || "").trim() || (msg.image ? "📷 Картинка" : "Сообщение");
  if (prev) prev.textContent = `@${msg.from}: ${snippet}`;

  const grid = document.getElementById("msgReactionChoices");
  if (grid) {
    const myReactions = msg.reactions || {};
    grid.innerHTML = REACTION_EMOJIS.map(em => {
      const users = myReactions[em] || [];
      const active = users.includes(me.username);
      return `<button type="button" class="reaction-pick-btn" style="${active ? 'outline:2px solid #2da9ff; background:#1c3242;' : ''}" onclick="pickReactionForSelected('${em}')">${em}</button>`;
    }).join("");
  }

  const modal = document.getElementById("msgActionModal");
  if (modal) modal.classList.remove("hidden");
}

function closeMsgActionModal(event) {
  if (event && event.target && event.target.id !== "msgActionModal") return;
  selectedMsgForAction = null;
  const modal = document.getElementById("msgActionModal");
  if (modal) modal.classList.add("hidden");
}

function startReplyToSelectedMsg() {
  const target = selectedMsgForAction;
  closeMsgActionModal();
  if (target) setReplyTarget(target);
}

async function pickReactionForSelected(emoji) {
  const target = selectedMsgForAction;
  closeMsgActionModal();
  if (!target || !activeChat || !me) return;
  await toggleMessageReaction(target.id, emoji);
}

async function toggleMessageReaction(msgId, emoji) {
  if (!msgId || !emoji || !activeChat || !me) return;
  const payload = {
    actor: me.username,
    msgId,
    emoji,
    chatType: activeChat.type
  };
  if (activeChat.type === "group") {
    payload.groupId = activeChat.id;
  } else {
    payload.peer = activeChat.username;
  }
  const res = await api("/api/react", payload);
  if (res.ok) {
    lastRenderedMsgsJson = "";
    await refreshMessages(false);
  }
}

function bindLongPressToBubble(el, msgIndex) {
  const startPress = (e) => {
    if (e.type === "mousedown" && e.button !== 0) return;
    if (longPressTimer) clearTimeout(longPressTimer);
    longPressTimer = setTimeout(() => {
      suppressNextBubbleClick = true;
      openMsgActionModal(msgIndex);
    }, 460);
  };
  const cancelPress = () => {
    if (longPressTimer) {
      clearTimeout(longPressTimer);
      longPressTimer = null;
    }
  };
  el.addEventListener("touchstart", startPress, { passive: true });
  el.addEventListener("touchend", cancelPress);
  el.addEventListener("touchcancel", cancelPress);
  el.addEventListener("touchmove", cancelPress, { passive: true });
  el.addEventListener("mousedown", startPress);
  el.addEventListener("mouseup", cancelPress);
  el.addEventListener("mouseleave", cancelPress);
  el.addEventListener("contextmenu", (e) => {
    e.preventDefault();
    cancelPress();
    openMsgActionModal(msgIndex);
  });
}

async function openDirectChat(username, emoji, isOwner = false) {
  activeChat = { type: "direct", id: username, username, emoji, isOwner, tripleBadge: false };
  replyTarget = null;
  clearReplyTarget();
  clearAttachment();
  lastRenderedMsgsJson = "";
  syncRoomComposerLock(false);
  const headerMain = document.getElementById("roomHeaderMain");
  if (headerMain) headerMain.classList.remove("clickable-header");
  document.getElementById("peerAva").textContent = emoji || "🍐";
  document.getElementById("peerNick").innerHTML = nickLabelHtml(username, isOwner);
  document.getElementById("peerStatus").textContent = "";
  const inp = document.getElementById("msgInput");
  inp.value = "";
  autoResizeComposer();
  showScreen("roomScreen");
  await refreshMessages(true);
  await refreshChats();
}

async function openGroupChat(groupId, title, emoji, tripleBadge = false) {
  const badgeFlag = Boolean(tripleBadge) || isTripleBadgeGroupTitle(title);
  activeChat = { type: "group", id: groupId, username: title, emoji: emoji || "🦜", isOwner: false, tripleBadge: badgeFlag };
  replyTarget = null;
  clearReplyTarget();
  clearAttachment();
  lastRenderedMsgsJson = "";
  syncRoomComposerLock(false);
  const headerMain = document.getElementById("roomHeaderMain");
  if (headerMain) headerMain.classList.add("clickable-header");
  document.getElementById("peerAva").textContent = emoji || "🦜";
  document.getElementById("peerNick").innerHTML = groupTitleHtml(title, badgeFlag);
  document.getElementById("peerStatus").textContent = "";
  const inp = document.getElementById("msgInput");
  inp.value = "";
  autoResizeComposer();
  showScreen("roomScreen");
  await refreshMessages(true);
  await refreshChats();
}

function formatTextWithEmojis(rawText) {
  const safe = esc(rawText);
  return safe.replace(SINGLE_EMOJI_RE, match => `<span class="inline-emoji">${match}</span>`);
}

function renderMsgItem(m, index) {
  const isMe = m.from === me.username;
  const cls = isMe ? "me" : "them";
  const text = (m.text || "").trim();
  const hasReply = Boolean(m.replyTo);
  const reactionsObj = m.reactions || {};
  const reactionKeys = Object.keys(reactionsObj).filter(k => Array.isArray(reactionsObj[k]) && reactionsObj[k].length > 0);
  const hasReactions = reactionKeys.length > 0;

  const isEmojiOnly = !m.image && !hasReply && !hasReactions && text.length > 0 && EMOJI_ONLY_RE.test(text);
  const bubbleExtra = isEmojiOnly ? " emoji-only-bubble" : "";

  const senderHtml = (activeChat && activeChat.type === "group" && !isMe)
    ? `<div class="msg-sender">@${esc(m.from)}</div>`
    : "";

  const replyHtml = m.replyTo
    ? `<div class="reply-quote"><div class="reply-quote-author">@${esc(m.replyTo.from || "пользователь")}</div><div class="reply-quote-txt">${esc(m.replyTo.text || (m.replyTo.hasImage ? "📷 Картинка" : "Сообщение"))}</div></div>`
    : "";

  const imgHtml = m.image
    ? `<img class="msg-img" src="${m.image}" alt="Картинка" onclick="if(suppressNextBubbleClick){suppressNextBubbleClick=false;return;}event.stopPropagation();window.open('${m.image}', '_blank')">`
    : "";

  const txtHtml = text
    ? `<div class="msg-txt">${formatTextWithEmojis(text)}</div>`
    : "";

  const reactionsHtml = hasReactions
    ? `<div class="reactions-row">${reactionKeys.map(em => {
        const users = reactionsObj[em];
        const reactedByMe = users.includes(me.username);
        return `<span class="reaction-chip ${reactedByMe ? 'reacted-by-me' : ''}" onclick="event.stopPropagation(); toggleMessageReaction(${Number(m.id)}, '${em}')">${em} <span>${users.length}</span></span>`;
      }).join("")}</div>`
    : "";

  return `<div class="m ${cls}${bubbleExtra}" data-msg-index="${index}">${senderHtml}${replyHtml}${imgHtml}${txtHtml}${reactionsHtml}</div>`;
}

function attachMessageInteractions() {
  const box = document.getElementById("msgsBox");
  if (!box) return;
  box.querySelectorAll(".m[data-msg-index]").forEach(el => {
    const idx = Number(el.getAttribute("data-msg-index"));
    bindLongPressToBubble(el, idx);
  });
}

async function refreshMessages(forceScroll = false) {
  if (!me || !activeChat) return;

  if (activeChat.type === "group") {
    const res = await api(`/api/groups/msgs?groupId=${encodeURIComponent(activeChat.id)}&user=${encodeURIComponent(me.username)}`);
    if (!res.ok) {
      if (res.deleted || res.kicked) backToChats();
      return;
    }
    syncRoomComposerLock(Boolean(res.isMuted));
    activeChat.username = res.title || activeChat.username;
    activeChat.emoji = res.emoji || activeChat.emoji;
    activeChat.tripleBadge = Boolean(res.tripleBadge) || isTripleBadgeGroupTitle(activeChat.username);
    document.getElementById("peerAva").textContent = activeChat.emoji || "🦜";
    document.getElementById("peerNick").innerHTML = groupTitleHtml(activeChat.username, activeChat.tripleBadge);

    const statusEl = document.getElementById("peerStatus");
    if (statusEl) {
      const typers = res.typingUsers || [];
      if (typers.length) {
        const formattedTypers = typers.map(n => `@${n}`).join(", ");
        statusEl.textContent = `${formattedTypers} печатает...`;
      } else {
        statusEl.textContent = "";
      }
    }

    currentRoomMsgs = res.msgs || [];
    const customStamp = JSON.stringify(loadCustom());
    const json = JSON.stringify(currentRoomMsgs);
    if (json === lastRenderedMsgsJson && customStamp === lastRenderedCustomJson && !forceScroll) return;
    lastRenderedMsgsJson = json;
    lastRenderedCustomJson = customStamp;

    const box = document.getElementById("msgsBox");
    if (!box) return;
    if (!currentRoomMsgs.length) {
      box.innerHTML = `<div class="muted" style="margin:auto;">Группа создана! Напишите первое сообщение.</div>`;
      return;
    }
    box.innerHTML = currentRoomMsgs.map((m, i) => renderMsgItem(m, i)).join("");
    attachMessageInteractions();
    box.scrollTop = box.scrollHeight;
    return;
  }

  const res = await api(`/api/msgs?a=${encodeURIComponent(me.username)}&b=${encodeURIComponent(activeChat.username)}`);
  if (!res.ok) return;

  if (res.peerEmoji && res.peerEmoji !== activeChat.emoji) {
    activeChat.emoji = res.peerEmoji;
    const peerAvaEl = document.getElementById("peerAva");
    if (peerAvaEl) peerAvaEl.textContent = res.peerEmoji;
  }

  const statusEl = document.getElementById("peerStatus");
  if (statusEl) {
    statusEl.textContent = res.peerTyping ? "печатает..." : "";
  }

  currentRoomMsgs = res.msgs || [];
  const customStamp = JSON.stringify(loadCustom());
  const json = JSON.stringify(currentRoomMsgs);
  if (json === lastRenderedMsgsJson && customStamp === lastRenderedCustomJson && !forceScroll) return;
  lastRenderedMsgsJson = json;
  lastRenderedCustomJson = customStamp;

  const box = document.getElementById("msgsBox");
  if (!box) return;
  if (!currentRoomMsgs.length) {
    box.innerHTML = `<div class="muted" style="margin:auto;">Здесь пока пусто. Напишите первое сообщение!</div>`;
    return;
  }
  box.innerHTML = currentRoomMsgs.map((m, i) => renderMsgItem(m, i)).join("");
  attachMessageInteractions();
  box.scrollTop = box.scrollHeight;
}

function insertEmoji(em) {
  const inp = document.getElementById("msgInput");
  const start = inp.selectionStart ?? inp.value.length;
  const end = inp.selectionEnd ?? inp.value.length;
  inp.value = inp.value.slice(0, start) + em + inp.value.slice(end);
  const pos = start + em.length;
  inp.focus();
  inp.setSelectionRange(pos, pos);
  autoResizeComposer();
  sendTypingSignal(false);
}

function clearAttachment() {
  pendingImageData = "";
  const fi = document.getElementById("fileInput");
  if (fi) fi.value = "";
  const prev = document.getElementById("attachPreview");
  if (prev) prev.classList.add("hidden");
  const thumb = document.getElementById("attachThumb");
  if (thumb) thumb.removeAttribute("src");
}

function compressImageFile(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const img = new Image();
      img.onload = () => {
        const maxSide = 900;
        let w = img.width;
        let h = img.height;
        if (w > maxSide || h > maxSide) {
          if (w > h) {
            h = Math.round((h * maxSide) / w);
            w = maxSide;
          } else {
            w = Math.round((w * maxSide) / h);
            h = maxSide;
          }
        }
        const canvas = document.createElement("canvas");
        canvas.width = w;
        canvas.height = h;
        const ctx = canvas.getContext("2d");
        ctx.drawImage(img, 0, 0, w, h);
        resolve(canvas.toDataURL("image/jpeg", 0.78));
      };
      img.onerror = reject;
      img.src = reader.result;
    };
    reader.onerror = reject;
    reader.readAsDataURL(file);
  });
}

async function onFileSelected(event) {
  const file = event.target.files && event.target.files[0];
  if (!file) return;
  try {
    const dataUrl = await compressImageFile(file);
    pendingImageData = dataUrl;
    document.getElementById("attachThumb").src = dataUrl;
    document.getElementById("attachName").textContent = file.name || "Картинка прикреплена";
    document.getElementById("attachPreview").classList.remove("hidden");
    document.getElementById("msgInput").focus();
  } catch (e) {
    clearAttachment();
  }
}

async function sendMsg() {
  if (!me || !activeChat) return;
  const inp = document.getElementById("msgInput");
  const text = inp.value.trim();
  const image = pendingImageData;
  const rep = replyTarget ? { ...replyTarget } : null;
  if (!text && !image) return;

  if (activeChat.type === "group") {
    const res = await api("/api/groups/send", {
      from: me.username,
      groupId: activeChat.id,
      text,
      image,
      replyTo: rep
    });
    if (!res.ok) {
      if (res.muted) syncRoomComposerLock(true);
      return;
    }
    inp.value = "";
    autoResizeComposer();
    clearReplyTarget();
    clearAttachment();
    inp.focus();
    await refreshMessages(true);
    await refreshChats();
    return;
  }

  inp.value = "";
  autoResizeComposer();
  clearReplyTarget();
  clearAttachment();
  inp.focus();

  await api("/api/send", { from: me.username, to: activeChat.username, text, image, replyTo: rep });
  await refreshMessages(true);
  await refreshChats();
}

async function setEmoji(em) {
  const res = await api("/api/emoji", { username: me.username, emoji: em });
  if (res.ok) {
    me.emoji = em;
    document.getElementById("myAva").textContent = em;
    document.getElementById("setOk").textContent = `Аватарка обновлена на ${em}`;
    document.getElementById("setErr").textContent = "";
    refreshChats();
  }
}

async function changeNick() {
  const nn = document.getElementById("newNickInput").value.trim();
  const res = await api("/api/nick", { oldNick: me.username, newNick: nn });
  if (!res.ok) {
    document.getElementById("setErr").textContent = res.error || "Ошибка";
    document.getElementById("setOk").textContent = "";
    return;
  }
  saveLocalUser(res.user);
  document.getElementById("myNick").innerHTML = nickLabelHtml(me.username, me.isOwner);
  document.getElementById("setErr").textContent = "";
  document.getElementById("setOk").textContent = "Никнейм изменён";
}

async function logout() {
  await api("/api/logout", {});
  saveLocalUser(null);
  activeChat = null;
  replyTarget = null;
  clearAttachment();
  showScreen("authScreen");
}

async function deleteAccount() {
  if (!me) return;
  await api("/api/delete", { username: me.username });
  saveLocalUser(null);
  activeChat = null;
  replyTarget = null;
  clearAttachment();
  showScreen("authScreen");
}

function startPolling() {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(() => {
    if (!me) return;
    refreshChats();
    if (activeChat) refreshMessages(false);
  }, 1800);
}

function esc(s) {
  return String(s || "").replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  }[c]));
}

applyCustom(loadCustom());
renderAvatarChoices();

(async function boot() {
  let savedNick = "";
  try { savedNick = localStorage.getItem("grusha_nick") || ""; } catch (e) {}
  const res = await api(`/api/me?nick=${encodeURIComponent(savedNick)}`);
  if (res.ok && res.user) {
    saveLocalUser(res.user);
    enterChats();
  } else {
    saveLocalUser(null);
    showScreen("authScreen");
  }
})();
</script>
</body>
</html>"""


def auth_response(user_dict):
    resp = make_response(jsonify({"ok": True, "user": format_user(user_dict)}))
    resp.set_cookie("grusha_user", user_dict["username"], max_age=30 * 24 * 3600, samesite="Lax")
    return resp


init_db()


@app.get("/")
def home():
    return render_template_string(HTML_PAGE)


@app.get("/api/me")
def get_me():
    nick = request.cookies.get("grusha_user") or (request.args.get("nick") or "").strip()
    if not nick:
        return jsonify({"ok": False})
    user = get_user(nick)
    if not user:
        return jsonify({"ok": False})
    return auth_response(user)


@app.post("/api/register")
def reg():
    d = request.get_json(force=True) or {}
    u = (d.get("username") or "").strip()
    p = d.get("password") or ""
    if len(u) < 2 or len(p) < 4:
        return jsonify({"ok": False, "error": "Ник от 2 символов, пароль от 4."})
    now = int(time.time() * 1000)

    if USE_PG:
        with get_pg_conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT username FROM users WHERE username = %s", (u,))
                if cur.fetchone():
                    return jsonify({"ok": False, "error": "Ник уже занят!"})
                cur.execute(
                    """
                    INSERT INTO users (username, password, emoji, created_at, last_nick_change_at)
                    VALUES (%s, %s, '🍐', %s, %s)
                    RETURNING *
                    """,
                    (u, p, now, now),
                )
                created = cur.fetchone()
        return auth_response(created)

    db = load_json_db()
    if u in db["users"]:
        return jsonify({"ok": False, "error": "Ник уже занят!"})
    db["users"][u] = {"username": u, "password": p, "emoji": "🍐", "createdAt": now, "lastNickChangeAt": now}
    save_json_db(db)
    return auth_response(db["users"][u])


@app.post("/api/login")
def login():
    d = request.get_json(force=True) or {}
    u = (d.get("username") or "").strip()
    p = d.get("password") or ""
    user = get_user(u)
    if not user or user["password"] != p:
        return jsonify({"ok": False, "error": "Неверный ник или пароль."})
    return auth_response(user)


@app.post("/api/logout")
def logout_api():
    resp = make_response(jsonify({"ok": True}))
    resp.delete_cookie("grusha_user")
    return resp


@app.post("/api/typing")
def typing_api():
    d = request.get_json(force=True) or {}
    f = (d.get("from") or "").strip()
    t = (d.get("to") or "").strip()
    gid = (d.get("groupId") or "").strip()
    if f and gid:
        TYPING_STATUS[(f, group_typing_key(gid))] = int(time.time() * 1000)
    elif f and t:
        TYPING_STATUS[(f, t)] = int(time.time() * 1000)
    return jsonify({"ok": True})


@app.get("/api/search")
def search():
    q = (request.args.get("q") or "").strip().lower()
    ex = request.args.get("exclude") or ""
    if not q:
        return jsonify({"ok": True, "users": []})

    escaped_q = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

    if USE_PG:
        with get_pg_conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    "SELECT username, emoji FROM users WHERE username <> %s AND LOWER(username) LIKE %s ESCAPE '\\' ORDER BY username ASC",
                    (ex, f"{escaped_q}%"),
                )
                rows = cur.fetchall()
        res = [{"username": r["username"], "emoji": r.get("emoji") or "🍐", "isOwner": is_owner_nick(r["username"])} for r in rows]
        return jsonify({"ok": True, "users": res})

    db = load_json_db()
    res = [
        {"username": k, "emoji": v.get("emoji", "🍐"), "isOwner": is_owner_nick(k)}
        for k, v in sorted(db["users"].items(), key=lambda x: x[0].lower())
        if k != ex and k.lower().startswith(q)
    ]
    return jsonify({"ok": True, "users": res})


@app.get("/api/chats")
def chats():
    u = request.args.get("user") or ""
    if USE_PG:
        with get_pg_conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT username, emoji FROM users")
                users_map = {r["username"]: (r.get("emoji") or "🍐") for r in cur.fetchall()}

                cur.execute(
                    """
                    SELECT DISTINCT ON (chat_key) chat_key, text, image, ts
                    FROM messages
                    WHERE chat_key LIKE %s OR chat_key LIKE %s
                    ORDER BY chat_key, id DESC
                    """,
                    (f"{u}::%", f"%::{u}"),
                )
                last_rows = cur.fetchall()

                cur.execute("SELECT * FROM groups ORDER BY created_at DESC")
                group_rows = cur.fetchall()

                group_last_map = {}
                cur.execute("""
                    SELECT DISTINCT ON (group_id) group_id, sender, text, image, ts
                    FROM group_messages
                    ORDER BY group_id, id DESC
                """)
                for gm in cur.fetchall():
                    group_last_map[gm["group_id"]] = gm

        out = []
        for r in last_rows:
            a, b = r["chat_key"].split("::")
            peer = b if a == u else (a if b == u else None)
            if peer and peer in users_map:
                preview_text = r.get("text") or ("📷 Картинка" if r.get("image") else "Сообщений пока нет")
                out.append({
                    "type": "direct",
                    "username": peer,
                    "emoji": users_map[peer],
                    "isOwner": is_owner_nick(peer),
                    "typing": is_peer_typing(peer, u),
                    "last": preview_text,
                    "ts": int(r.get("ts") or 0),
                })

        for gr in group_rows:
            g = format_group_row(gr)
            if u in g["members"]:
                gm = group_last_map.get(g["id"])
                if gm:
                    base_txt = gm.get("text") or ("📷 Картинка" if gm.get("image") else "Сообщений пока нет")
                    preview_text = f"@{gm['sender']}: {base_txt}"
                    ts_val = int(gm.get("ts") or g["createdAt"])
                else:
                    preview_text = "Группа создана"
                    ts_val = g["createdAt"]
                typers = get_group_typing_users(g["id"], u)
                out.append({
                    "type": "group",
                    "groupId": g["id"],
                    "title": g["title"],
                    "emoji": normalize_group_emoji(g["emoji"]),
                    "tripleBadge": g["tripleBadge"],
                    "typing": len(typers) > 0,
                    "typingText": ", ".join(f"@{t}" for t in typers),
                    "last": preview_text,
                    "ts": ts_val,
                })

        out.sort(key=lambda x: x["ts"], reverse=True)
        return jsonify({"ok": True, "chats": out})

    db = load_json_db()
    out = []
    for k, m in db["chats"].items():
        if not m:
            continue
        a, b = k.split("::")
        peer = b if a == u else (a if b == u else None)
        if peer and peer in db["users"]:
            last_msg = m[-1]
            preview_text = last_msg.get("text") or ("📷 Картинка" if last_msg.get("image") else "Сообщений пока нет")
            out.append({
                "type": "direct",
                "username": peer,
                "emoji": db["users"][peer].get("emoji", "🍐"),
                "isOwner": is_owner_nick(peer),
                "typing": is_peer_typing(peer, u),
                "last": preview_text,
                "ts": last_msg.get("ts", 0),
            })

    for gid, g in db["groups"].items():
        members = g.get("members") or []
        if u in members:
            g_msgs = db["group_msgs"].get(gid) or []
            if g_msgs:
                gm = g_msgs[-1]
                base_txt = gm.get("text") or ("📷 Картинка" if gm.get("image") else "Сообщений пока нет")
                preview_text = f"@{gm['from']}: {base_txt}"
                ts_val = int(gm.get("ts") or g.get("createdAt") or 0)
            else:
                preview_text = "Группа создана"
                ts_val = int(g.get("createdAt") or 0)
            typers = get_group_typing_users(gid, u)
            out.append({
                "type": "group",
                "groupId": gid,
                "title": g["title"],
                "emoji": normalize_group_emoji(g.get("emoji")),
                "tripleBadge": is_triple_badge_group(g.get("title")),
                "typing": len(typers) > 0,
                "typingText": ", ".join(f"@{t}" for t in typers),
                "last": preview_text,
                "ts": ts_val,
            })

    out.sort(key=lambda x: x["ts"], reverse=True)
    return jsonify({"ok": True, "chats": out})


@app.get("/api/msgs")
def msgs():
    a = request.args.get("a") or ""
    b = request.args.get("b") or ""
    ck = chat_key(a, b)
    peer_typing = is_peer_typing(b, a)
    if USE_PG:
        with get_pg_conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    "SELECT id, sender AS \"from\", text, image, ts, reply_to_json, reactions_json FROM messages WHERE chat_key = %s ORDER BY id ASC",
                    (ck,),
                )
                rows = cur.fetchall()
                cur.execute("SELECT emoji FROM users WHERE username = %s", (b,))
                peer_row = cur.fetchone()
        peer_emoji = (peer_row.get("emoji") if peer_row else None) or "🍐"
        formatted = [
            format_message_record(
                r["id"],
                r["from"],
                r.get("text") or "",
                r.get("image") or "",
                r.get("ts") or 0,
                parse_json_dict(r.get("reply_to_json")),
                parse_json_dict(r.get("reactions_json")),
            )
            for r in rows
        ]
        return jsonify({"ok": True, "msgs": formatted, "peerEmoji": peer_emoji, "peerTyping": peer_typing})

    db = load_json_db()
    peer_user = db["users"].get(b) or {}
    raw_list = db["chats"].get(ck, [])
    formatted = []
    for idx, m in enumerate(raw_list):
        msg_id = m.get("id") or f"d_{idx}_{m.get('ts', 0)}"
        formatted.append(
            format_message_record(
                msg_id,
                m.get("from", ""),
                m.get("text", ""),
                m.get("image", ""),
                m.get("ts", 0),
                m.get("replyTo"),
                m.get("reactions"),
            )
        )
    return jsonify({
        "ok": True,
        "msgs": formatted,
        "peerEmoji": peer_user.get("emoji") or "🍐",
        "peerTyping": peer_typing,
    })


@app.post("/api/send")
def send():
    d = request.get_json(force=True) or {}
    f = (d.get("from") or "").strip()
    t = (d.get("to") or "").strip()
    txt = (d.get("text") or "").strip()
    img = d.get("image") or ""
    rep = sanitize_reply_to(d.get("replyTo"))
    if img and not (img.startswith("data:image/") and len(img) <= MAX_IMAGE_LEN):
        img = ""
    if not f or not t or (not txt and not img):
        return jsonify({"ok": False})
    ts = int(time.time() * 1000)
    TYPING_STATUS.pop((f, t), None)

    if USE_PG:
        with get_pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO messages (chat_key, sender, text, image, ts, reply_to_json, reactions_json) VALUES (%s, %s, %s, %s, %s, %s, '{}')",
                    (chat_key(f, t), f, txt, img, ts, json.dumps(rep, ensure_ascii=False) if rep else ""),
                )
        return jsonify({"ok": True})

    msg_item = {
        "id": f"d_{uuid.uuid4().hex[:10]}",
        "from": f,
        "text": txt,
        "ts": ts,
        "reactions": {},
    }
    if img:
        msg_item["image"] = img
    if rep:
        msg_item["replyTo"] = rep
    db = load_json_db()
    db["chats"].setdefault(chat_key(f, t), []).append(msg_item)
    save_json_db(db)
    return jsonify({"ok": True})


@app.post("/api/react")
def react_api():
    d = request.get_json(force=True) or {}
    actor = (d.get("actor") or "").strip()
    emoji = (d.get("emoji") or "").strip()
    msg_id = d.get("msgId")
    chat_type = (d.get("chatType") or "direct").strip()

    if not actor or not emoji or msg_id is None or msg_id == "":
        return jsonify({"ok": False})

    def apply_toggle(reactions_dict):
        cur_map = normalize_reactions(reactions_dict)
        users = list(cur_map.get(emoji) or [])
        if actor in users:
            users.remove(actor)
        else:
            users.append(actor)
        if users:
            cur_map[emoji] = users
        else:
            cur_map.pop(emoji, None)
        return cur_map

    if chat_type == "group":
        gid = (d.get("groupId") or "").strip()
        g = get_group_by_id(gid)
        if not g or actor not in g["members"]:
            return jsonify({"ok": False})
        if USE_PG:
            with get_pg_conn() as conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute("SELECT reactions_json FROM group_messages WHERE id = %s AND group_id = %s", (int(msg_id), gid))
                    row = cur.fetchone()
                    if not row:
                        return jsonify({"ok": False})
                    updated = apply_toggle(parse_json_dict(row.get("reactions_json")))
                    cur.execute("UPDATE group_messages SET reactions_json = %s WHERE id = %s", (json.dumps(updated, ensure_ascii=False), int(msg_id)))
            return jsonify({"ok": True, "reactions": updated})

        db = load_json_db()
        g_list = db["group_msgs"].get(gid) or []
        for idx, m in enumerate(g_list):
            mid = m.get("id") or f"g_{idx}_{m.get('ts', 0)}"
            if str(mid) == str(msg_id):
                m["id"] = mid
                m["reactions"] = apply_toggle(m.get("reactions"))
                save_json_db(db)
                return jsonify({"ok": True, "reactions": m["reactions"]})
        return jsonify({"ok": False})

    peer = (d.get("peer") or "").strip()
    if not peer:
        return jsonify({"ok": False})
    ck = chat_key(actor, peer)
    if USE_PG:
        with get_pg_conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT reactions_json FROM messages WHERE id = %s AND chat_key = %s", (int(msg_id), ck))
                row = cur.fetchone()
                if not row:
                    return jsonify({"ok": False})
                updated = apply_toggle(parse_json_dict(row.get("reactions_json")))
                cur.execute("UPDATE messages SET reactions_json = %s WHERE id = %s", (json.dumps(updated, ensure_ascii=False), int(msg_id)))
        return jsonify({"ok": True, "reactions": updated})

    db = load_json_db()
    c_list = db["chats"].get(ck) or []
    for idx, m in enumerate(c_list):
        mid = m.get("id") or f"d_{idx}_{m.get('ts', 0)}"
        if str(mid) == str(msg_id):
            m["id"] = mid
            m["reactions"] = apply_toggle(m.get("reactions"))
            save_json_db(db)
            return jsonify({"ok": True, "reactions": m["reactions"]})
    return jsonify({"ok": False})


@app.post("/api/groups/create")
def create_group():
    d = request.get_json(force=True) or {}
    owner = (d.get("owner") or "").strip()
    title = (d.get("title") or "").strip()
    emoji = normalize_group_emoji(d.get("emoji"))
    raw_members = d.get("members") or []
    if not owner or not title:
        return jsonify({"ok": False, "error": "Укажите название группы."})
    if is_group_title_taken(title):
        return jsonify({"ok": False, "error": "Группа с таким названием уже существует!"})

    members_clean = []
    seen = set()
    for m in [owner] + [str(x).strip() for x in raw_members if isinstance(x, str)]:
        if m and m not in seen:
            seen.add(m)
            members_clean.append(m)

    gid = uuid.uuid4().hex[:12]
    now = int(time.time() * 1000)

    if USE_PG:
        with get_pg_conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    """
                    INSERT INTO groups (id, title, emoji, owner, members_json, muted_json, created_at)
                    VALUES (%s, %s, %s, %s, %s, '[]', %s)
                    RETURNING *
                    """,
                    (gid, title, emoji, owner, json.dumps(members_clean, ensure_ascii=False), now),
                )
                row = cur.fetchone()
        return jsonify({"ok": True, "group": format_group_row(row)})

    db = load_json_db()
    group_obj = {
        "id": gid,
        "title": title,
        "emoji": emoji,
        "owner": owner,
        "members": members_clean,
        "muted": [],
        "createdAt": now,
        "tripleBadge": is_triple_badge_group(title),
    }
    db["groups"][gid] = group_obj
    db["group_msgs"].setdefault(gid, [])
    save_json_db(db)
    return jsonify({"ok": True, "group": group_obj})


@app.post("/api/groups/update")
def group_update():
    d = request.get_json(force=True) or {}
    actor = (d.get("actor") or "").strip()
    gid = (d.get("groupId") or "").strip()
    title = (d.get("title") or "").strip()
    emoji = normalize_group_emoji(d.get("emoji"))

    g = get_group_by_id(gid)
    if not g or g["owner"] != actor:
        return jsonify({"ok": False, "error": "Только создатель группы может менять настройки."})
    if not title:
        return jsonify({"ok": False, "error": "Название не может быть пустым."})
    if is_group_title_taken(title, exclude_group_id=gid):
        return jsonify({"ok": False, "error": "Группа с таким названием уже существует!"})

    if USE_PG:
        with get_pg_conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    "UPDATE groups SET title = %s, emoji = %s WHERE id = %s RETURNING *",
                    (title, emoji, gid),
                )
                row = cur.fetchone()
        return jsonify({"ok": True, "group": format_group_row(row)})

    db = load_json_db()
    if gid in db["groups"]:
        db["groups"][gid]["title"] = title
        db["groups"][gid]["emoji"] = emoji
        save_json_db(db)
        return jsonify({"ok": True, "group": get_group_by_id(gid)})
    return jsonify({"ok": False, "error": "Группа не найдена."})


@app.get("/api/groups/info")
def group_info():
    gid = (request.args.get("groupId") or "").strip()
    u = (request.args.get("user") or "").strip()
    g = get_group_by_id(gid)
    if not g:
        return jsonify({"ok": False, "deleted": True})
    if u and u not in g["members"]:
        return jsonify({"ok": False, "kicked": True})
    return jsonify({"ok": True, "group": g})


@app.get("/api/groups/msgs")
def group_msgs():
    gid = (request.args.get("groupId") or "").strip()
    u = (request.args.get("user") or "").strip()
    g = get_group_by_id(gid)
    if not g:
        return jsonify({"ok": False, "deleted": True})
    if u and u not in g["members"]:
        return jsonify({"ok": False, "kicked": True})

    is_muted = u in set(g.get("muted") or [])
    typers = get_group_typing_users(gid, u)

    if USE_PG:
        with get_pg_conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    "SELECT id, sender AS \"from\", text, image, ts, reply_to_json, reactions_json FROM group_messages WHERE group_id = %s ORDER BY id ASC",
                    (gid,),
                )
                rows = cur.fetchall()
        formatted = [
            format_message_record(
                r["id"],
                r["from"],
                r.get("text") or "",
                r.get("image") or "",
                r.get("ts") or 0,
                parse_json_dict(r.get("reply_to_json")),
                parse_json_dict(r.get("reactions_json")),
            )
            for r in rows
        ]
        return jsonify({
            "ok": True,
            "msgs": formatted,
            "title": g["title"],
            "emoji": normalize_group_emoji(g["emoji"]),
            "tripleBadge": g["tripleBadge"],
            "isMuted": is_muted,
            "typingUsers": typers,
        })

    db = load_json_db()
    raw_list = db["group_msgs"].get(gid, [])
    formatted = []
    for idx, m in enumerate(raw_list):
        msg_id = m.get("id") or f"g_{idx}_{m.get('ts', 0)}"
        formatted.append(
            format_message_record(
                msg_id,
                m.get("from", ""),
                m.get("text", ""),
                m.get("image", ""),
                m.get("ts", 0),
                m.get("replyTo"),
                m.get("reactions"),
            )
        )
    return jsonify({
        "ok": True,
        "msgs": formatted,
        "title": g["title"],
        "emoji": normalize_group_emoji(g.get("emoji")),
        "tripleBadge": is_triple_badge_group(g.get("title")),
        "isMuted": is_muted,
        "typingUsers": typers,
    })


@app.post("/api/groups/send")
def group_send():
    d = request.get_json(force=True) or {}
    f = (d.get("from") or "").strip()
    gid = (d.get("groupId") or "").strip()
    txt = (d.get("text") or "").strip()
    img = d.get("image") or ""
    rep = sanitize_reply_to(d.get("replyTo"))
    if img and not (img.startswith("data:image/") and len(img) <= MAX_IMAGE_LEN):
        img = ""

    g = get_group_by_id(gid)
    if not g or f not in g["members"]:
        return jsonify({"ok": False})
    if f in set(g.get("muted") or []):
        return jsonify({"ok": False, "muted": True})
    if not txt and not img:
        return jsonify({"ok": False})

    ts = int(time.time() * 1000)
    TYPING_STATUS.pop((f, group_typing_key(gid)), None)

    if USE_PG:
        with get_pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO group_messages (group_id, sender, text, image, ts, reply_to_json, reactions_json) VALUES (%s, %s, %s, %s, %s, %s, '{}')",
                    (gid, f, txt, img, ts, json.dumps(rep, ensure_ascii=False) if rep else ""),
                )
        return jsonify({"ok": True})

    msg_item = {
        "id": f"g_{uuid.uuid4().hex[:10]}",
        "from": f,
        "text": txt,
        "ts": ts,
        "reactions": {},
    }
    if img:
        msg_item["image"] = img
    if rep:
        msg_item["replyTo"] = rep
    db = load_json_db()
    db["group_msgs"].setdefault(gid, []).append(msg_item)
    save_json_db(db)
    return jsonify({"ok": True})


@app.post("/api/groups/mute")
def group_mute():
    d = request.get_json(force=True) or {}
    actor = (d.get("actor") or "").strip()
    gid = (d.get("groupId") or "").strip()
    target = (d.get("target") or "").strip()
    g = get_group_by_id(gid)
    if not g or g["owner"] != actor or target == actor:
        return jsonify({"ok": False})

    muted = list(g.get("muted") or [])
    if target in muted:
        muted.remove(target)
    else:
        muted.append(target)

    if USE_PG:
        with get_pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE groups SET muted_json = %s WHERE id = %s", (json.dumps(muted, ensure_ascii=False), gid))
        return jsonify({"ok": True, "muted": muted})

    db = load_json_db()
    if gid in db["groups"]:
        db["groups"][gid]["muted"] = muted
        save_json_db(db)
    return jsonify({"ok": True, "muted": muted})


@app.post("/api/groups/kick")
def group_kick():
    d = request.get_json(force=True) or {}
    actor = (d.get("actor") or "").strip()
    gid = (d.get("groupId") or "").strip()
    target = (d.get("target") or "").strip()
    g = get_group_by_id(gid)
    if not g or g["owner"] != actor or target == actor:
        return jsonify({"ok": False})

    members = [m for m in (g.get("members") or []) if m != target]
    muted = [m for m in (g.get("muted") or []) if m != target]

    if USE_PG:
        with get_pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE groups SET members_json = %s, muted_json = %s WHERE id = %s",
                    (json.dumps(members, ensure_ascii=False), json.dumps(muted, ensure_ascii=False), gid),
                )
        return jsonify({"ok": True})

    db = load_json_db()
    if gid in db["groups"]:
        db["groups"][gid]["members"] = members
        db["groups"][gid]["muted"] = muted
        save_json_db(db)
    return jsonify({"ok": True})


@app.post("/api/groups/delete")
def group_delete():
    d = request.get_json(force=True) or {}
    actor = (d.get("actor") or "").strip()
    gid = (d.get("groupId") or "").strip()
    g = get_group_by_id(gid)
    if not g or g["owner"] != actor:
        return jsonify({"ok": False})

    if USE_PG:
        with get_pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM group_messages WHERE group_id = %s", (gid,))
                cur.execute("DELETE FROM groups WHERE id = %s", (gid,))
        return jsonify({"ok": True})

    db = load_json_db()
    db["groups"].pop(gid, None)
    db["group_msgs"].pop(gid, None)
    save_json_db(db)
    return jsonify({"ok": True})


@app.post("/api/emoji")
def emoji():
    d = request.get_json(force=True) or {}
    u, em = (d.get("username") or "").strip(), d.get("emoji") or "🍐"
    if USE_PG:
        with get_pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE users SET emoji = %s WHERE username = %s", (em, u))
        return jsonify({"ok": True})

    db = load_json_db()
    if u in db["users"]:
        db["users"][u]["emoji"] = em
        save_json_db(db)
    return jsonify({"ok": True})


@app.post("/api/nick")
def nick():
    d = request.get_json(force=True) or {}
    old_n, new_n = (d.get("oldNick") or "").strip(), (d.get("newNick") or "").strip()
    user = get_user(old_n)
    if not user:
        return jsonify({"ok": False, "error": "Аккаунт не найден."})
    now = int(time.time() * 1000)
    created_at = int(user.get("createdAt", user.get("created_at", now)) or now)
    last_change = int(user.get("lastNickChangeAt", user.get("last_nick_change_at", created_at)) or created_at)
    if now - last_change > SEVEN_DAYS_MS:
        return jsonify({"ok": False, "error": "7 дней прошло, сменить ник нельзя."})
    if len(new_n) < 2:
        return jsonify({"ok": False, "error": "Ник слишком короткий."})

    if USE_PG:
        with get_pg_conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT username FROM users WHERE username = %s", (new_n,))
                if cur.fetchone():
                    return jsonify({"ok": False, "error": "Ник уже занят!"})
                cur.execute(
                    """
                    INSERT INTO users (username, password, emoji, created_at, last_nick_change_at)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (new_n, user["password"], user.get("emoji") or "🍐", created_at, now),
                )
                cur.execute("SELECT chat_key FROM messages WHERE chat_key LIKE %s OR chat_key LIKE %s", (f"{old_n}::%", f"%::{old_n}"))
                keys = [r["chat_key"] for r in cur.fetchall()]
                for k in set(keys):
                    parts = [new_n if x == old_n else x for x in k.split("::")]
                    cur.execute("UPDATE messages SET chat_key = %s WHERE chat_key = %s", (chat_key(parts[0], parts[1]), k))
                cur.execute("UPDATE messages SET sender = %s WHERE sender = %s", (new_n, old_n))
                cur.execute("UPDATE group_messages SET sender = %s WHERE sender = %s", (new_n, old_n))
                cur.execute("DELETE FROM users WHERE username = %s", (old_n,))
                cur.execute("SELECT * FROM users WHERE username = %s", (new_n,))
                updated = cur.fetchone()
        return auth_response(updated)

    db = load_json_db()
    if new_n in db["users"]:
        return jsonify({"ok": False, "error": "Ник уже занят!"})
    db["users"][new_n] = {**user, "username": new_n, "lastNickChangeAt": now}
    del db["users"][old_n]
    new_chats = {}
    for k, m in db["chats"].items():
        parts = [new_n if x == old_n else x for x in k.split("::")]
        new_chats[chat_key(parts[0], parts[1])] = [{**x, "from": new_n if x["from"] == old_n else x["from"]} for x in m]
    db["chats"] = new_chats
    save_json_db(db)
    return auth_response(db["users"][new_n])


@app.post("/api/delete")
def delete():
    d = request.get_json(force=True) or {}
    u = (d.get("username") or "").strip()
    if USE_PG:
        with get_pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM messages WHERE chat_key LIKE %s OR chat_key LIKE %s", (f"{u}::%", f"%::{u}"))
                cur.execute("DELETE FROM users WHERE username = %s", (u,))
        resp = make_response(jsonify({"ok": True}))
        resp.delete_cookie("grusha_user")
        return resp

    db = load_json_db()
    db["users"].pop(u, None)
    db["chats"] = {k: v for k, v in db["chats"].items() if u not in k.split("::")}
    save_json_db(db)
    resp = make_response(jsonify({"ok": True}))
    resp.delete_cookie("grusha_user")
    return resp


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
