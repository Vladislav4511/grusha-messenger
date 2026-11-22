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
CALL_TTL_MS = 45_000
OWNER_NICK = "владислав"
TRIPLE_BADGE_GROUP_TITLE = "ырка сквад"
MAX_IMAGE_LEN = 1_500_000
DEFAULT_GROUP_ICON = "🦜"
DEFAULT_USER_EMOJI = "🍐"

TYPING_STATUS = {}
ACTIVE_CALLS = {}  # call_id -> dict
USER_ALERTS = {}   # username -> list of alert dicts


def sanitize_user_emoji(em):
    val = (em or "").strip()
    if not val:
        return DEFAULT_USER_EMOJI
    if val in ("👑", "💩"):
        return "🙂"
    return val


def push_user_alert(username, kind, title, body):
    u = (username or "").strip()
    if not u:
        return
    now = int(time.time() * 1000)
    USER_ALERTS.setdefault(u, []).append({
        "id": f"al_{uuid.uuid4().hex[:10]}",
        "kind": kind,
        "title": title,
        "body": body,
        "ts": now,
    })
    if len(USER_ALERTS[u]) > 50:
        USER_ALERTS[u] = USER_ALERTS[u][-50:]


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
                cur.execute("UPDATE users SET emoji = '🙂' WHERE emoji IN ('👑', '💩')")
                cur.execute("UPDATE groups SET emoji = '🙂' WHERE emoji IN ('👑', '💩')")

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
        else:
            db = load_json_db()
            changed = False
            for _, u_obj in db.get("users", {}).items():
                if isinstance(u_obj, dict) and u_obj.get("emoji") in ("👑", "💩"):
                    u_obj["emoji"] = "🙂"
                    changed = True
            for _, g_obj in db.get("groups", {}).items():
                if isinstance(g_obj, dict) and g_obj.get("emoji") in ("👑", "💩"):
                    g_obj["emoji"] = "🙂"
                    changed = True
            if changed:
                save_json_db(db)


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
    if val in ("👑", "💩"):
        return "🙂"
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


def cleanup_stale_calls():
    now = int(time.time() * 1000)
    expired = []
    for cid, call in ACTIVE_CALLS.items():
        updated = int(call.get("updatedAt") or call.get("createdAt") or now)
        if call.get("status") == "ended" and (now - updated) > 8000:
            expired.append(cid)
        elif (now - updated) > CALL_TTL_MS:
            expired.append(cid)
    for cid in expired:
        ACTIVE_CALLS.pop(cid, None)


def format_user(row):
    username = row["username"]
    created_at = int(row.get("createdAt", row.get("created_at", 0)) or 0)
    last_change = int(row.get("lastNickChangeAt", row.get("last_nick_change_at", created_at)) or created_at)
    return {
        "username": username,
        "emoji": sanitize_user_emoji(row.get("emoji")),
        "createdAt": created_at,
        "lastNickChangeAt": last_change,
        "isOwner": is_owner_nick(username),
    }


def get_user(username):
    if not username:
        return None
    if USE_PG:
        with get_pg_conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT * FROM users WHERE username = %s", (username,))
                row = cur.fetchone()
                if row and row.get("emoji") in ("👑", "💩"):
                    cur.execute("UPDATE users SET emoji = '🙂' WHERE username = %s", (username,))
                    row["emoji"] = "🙂"
                return row
    db = load_json_db()
    u = db["users"].get(username)
    if u and u.get("emoji") in ("👑", "💩"):
        u["emoji"] = "🙂"
        save_json_db(db)
    return u


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


def append_group_system_message(gid, text, event_kind=""):
    ts = int(time.time() * 1000)
    if USE_PG:
        with get_pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO group_messages (group_id, sender, text, image, ts, reply_to_json, reactions_json) VALUES (%s, %s, %s, '', %s, '', '{}')",
                    (gid, "system", text, ts),
                )
        return
    db = load_json_db()
    item = {
        "id": f"g_{uuid.uuid4().hex[:10]}",
        "from": "system",
        "text": text,
        "ts": ts,
        "reactions": {},
    }
    if event_kind:
        item["eventKind"] = event_kind
    db["group_msgs"].setdefault(gid, []).append(item)
    save_json_db(db)


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
    txt_val = text or ""
    event_kind = ""
    if sender == "system":
        if txt_val.endswith("зашел"):
            event_kind = "join"
        elif txt_val.endswith("вышел"):
            event_kind = "leave"
    item = {
        "id": msg_id,
        "from": sender,
        "text": txt_val,
        "ts": int(ts or 0),
        "reactions": normalize_reactions(reactions_raw),
    }
    if event_kind:
        item["eventKind"] = event_kind
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

    --outer-bg: #101511;
    --surface-bg: #171e18;
    --header-bg: #1f2920;
    --dock-bg: #1b231c;
    --card-bg: #202a21;
    --card-alt-bg: #243026;
    --input-bg: #101511;
    --border-col: #2b382d;
    --text-col: #f3f7f3;
    --muted-col: #96a598;
    --sec-btn-bg: #263228;
    --sec-btn-text: #eef5ee;
    --accent: #9be15d;
    --accent-contrast: #101511;
    --accent-soft: rgba(155, 225, 93, 0.18);
  }
  * { box-sizing: border-box; }
  html, body {
    margin: 0;
    padding: 0;
    background: var(--outer-bg);
    color: var(--text-col);
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
    background: var(--surface-bg);
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
    background: var(--header-bg);
    border-bottom: 1px solid var(--border-col);
    gap: 8px;
    flex-shrink: 0;
  }
  .top-actions {
    display: flex;
    align-items: center;
    gap: 8px;
    flex-shrink: 0;
  }
  .brand { font-size: 20px; font-weight: 800; color: var(--accent); }
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
    border: 1px solid var(--border-col);
    background: var(--input-bg);
    color: var(--text-col);
    font-size: 15px;
    font-family: inherit;
    outline: none;
  }
  input:focus, textarea:focus { border-color: var(--accent); }
  input[type="range"] {
    width: 100%;
    accent-color: var(--accent);
    cursor: pointer;
  }
  button {
    padding: 10px 14px;
    border-radius: 10px;
    border: none;
    background: var(--accent);
    color: var(--accent-contrast);
    font-weight: 700;
    font-size: 14px;
    cursor: pointer;
    flex-shrink: 0;
  }
  button.sec { background: var(--sec-btn-bg); color: var(--sec-btn-text); }
  button.del { background: #b93838; color: #fff; }
  button.warn { background: #c88a1e; color: #101511; }
  button.blue-btn { background: var(--accent); color: var(--accent-contrast); }
  button:disabled { opacity: 0.48; cursor: not-allowed; }
  .mt-8 { margin-top: 8px; }
  .mt-12 { margin-top: 12px; }
  .err { color: #ff7b7b; font-size: 13px; margin-top: 8px; min-height: 18px; }
  .ok { color: var(--accent); font-size: 13px; margin-top: 8px; }
  .muted { color: var(--muted-col); font-size: 13px; }
  .typing-line {
    font-size: 12px;
    color: var(--accent);
    min-height: 16px;
    line-height: 1.2;
  }
  .typing-line:empty {
    display: none;
  }
  .inapp-toast-banner {
    margin-bottom: 10px;
    padding: 10px 12px;
    border-radius: 12px;
    background: var(--card-alt-bg);
    border: 1px solid var(--accent);
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
    font-size: 13px;
  }
  .chat-card {
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 11px;
    border-radius: 12px;
    background: var(--card-bg);
    border: 1px solid var(--border-col);
    margin-top: 8px;
    cursor: pointer;
  }
  .ava {
    width: 44px;
    height: 44px;
    border-radius: 50%;
    background: var(--input-bg);
    border: 1px solid var(--border-col);
    display: grid;
    place-items: center;
    font-size: 22px;
    flex-shrink: 0;
  }
  .chat-info { min-width: 0; flex: 1; }
  .chat-name { font-weight: 700; font-size: 15px; display: inline-flex; align-items: center; gap: 5px; }
  .chat-last { color: var(--muted-col); font-size: 13px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; margin-top: 2px; }
  .chat-last.typing-active { color: var(--accent); font-weight: 600; }
  .verified-badge {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 17px;
    height: 17px;
    border-radius: 50%;
    background: var(--accent);
    color: var(--accent-contrast);
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

  .fab-create {
    position: fixed;
    right: max(18px, calc(50% - 220px));
    bottom: 22px;
    width: 54px;
    height: 54px;
    border-radius: 50%;
    background: var(--accent);
    color: var(--accent-contrast);
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

  .member-pick {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 10px 12px;
    border-radius: 12px;
    background: var(--card-bg);
    margin-top: 8px;
    cursor: pointer;
    border: 1px solid var(--border-col);
  }
  .member-pick.selected {
    border-color: var(--accent);
    background: var(--accent-soft);
  }
  .member-pick input[type="checkbox"] {
    width: 18px;
    height: 18px;
    accent-color: var(--accent);
    cursor: pointer;
    flex-shrink: 0;
  }

  .group-edit-card {
    padding: 14px;
    border-radius: 14px;
    background: var(--card-bg);
    border: 1px solid var(--border-col);
    margin-top: 14px;
  }
  .group-member-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    padding: 10px 12px;
    border-radius: 12px;
    background: var(--card-bg);
    border: 1px solid var(--border-col);
    margin-top: 8px;
  }
  .role-badge-owner {
    display: inline-block;
    padding: 2px 8px;
    border-radius: 999px;
    background: var(--accent-soft);
    color: var(--accent);
    font-size: 12px;
    font-weight: 700;
    margin-top: 3px;
  }
  .role-badge-member {
    display: inline-block;
    padding: 2px 8px;
    border-radius: 999px;
    background: var(--sec-btn-bg);
    color: var(--muted-col);
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
    background: var(--sec-btn-bg);
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
  .sys-msg {
    align-self: center;
    background: var(--card-bg);
    border: 1px solid var(--border-col);
    color: var(--muted-col);
    font-size: 12px;
    padding: 4px 12px;
    border-radius: 999px;
    max-width: 90%;
    text-align: center;
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
  .me { align-self: flex-end; background: var(--accent); color: var(--accent-contrast); font-weight: 500; }
  .them { align-self: flex-start; background: var(--card-alt-bg); border: 1px solid var(--border-col); color: var(--text-col); }
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
    border-color: transparent !important;
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
    background: rgba(0, 0, 0, 0.25);
    color: inherit;
    border: 1px solid rgba(255, 255, 255, 0.16);
    cursor: pointer;
  }
  .reaction-chip.reacted-by-me {
    background: var(--accent-soft);
    border-color: var(--accent);
  }

  .msg-menu-backdrop {
    position: fixed;
    inset: 0;
    background: rgba(0, 0, 0, 0.6);
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 18px;
    z-index: 60;
  }
  .msg-menu-card {
    width: 100%;
    max-width: 340px;
    background: var(--header-bg);
    border: 1px solid var(--border-col);
    border-radius: 16px;
    padding: 14px;
    box-shadow: 0 12px 32px rgba(0, 0, 0, 0.6);
  }
  .msg-menu-preview {
    font-size: 12px;
    color: var(--muted-col);
    background: var(--input-bg);
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
    background: var(--sec-btn-bg);
    color: var(--text-col);
    padding: 7px 10px;
    font-size: 20px;
    line-height: 1;
    border-radius: 10px;
  }

  /* Полноэкранный звонок с видео и звуком */
  .call-overlay {
    position: fixed;
    inset: 0;
    width: 100vw;
    height: 100dvh;
    background: var(--outer-bg);
    color: var(--text-col);
    display: flex;
    flex-direction: column;
    justify-content: space-between;
    padding: 20px 18px max(24px, env(safe-area-inset-bottom, 24px));
    z-index: 90;
    overflow: hidden;
  }
  .call-top-bar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
    z-index: 3;
  }
  .call-stage {
    position: relative;
    flex: 1;
    margin: 14px 0;
    border-radius: 20px;
    background: var(--surface-bg);
    border: 1px solid var(--border-col);
    overflow: hidden;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
  }
  .call-remote-video {
    width: 100%;
    height: 100%;
    object-fit: cover;
    background: var(--input-bg);
  }
  .call-avatar-fallback {
    position: absolute;
    inset: 0;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    background: var(--card-bg);
    z-index: 1;
  }
  .call-avatar-fallback.hidden-cam {
    display: none;
  }
  .call-local-pip {
    position: absolute;
    right: 12px;
    bottom: 12px;
    width: 106px;
    height: 148px;
    border-radius: 14px;
    overflow: hidden;
    border: 2px solid var(--accent);
    background: var(--input-bg);
    z-index: 2;
    box-shadow: 0 8px 24px rgba(0, 0, 0, 0.55);
  }
  .call-local-video {
    width: 100%;
    height: 100%;
    object-fit: cover;
    transform: scaleX(-1);
  }
  .call-timer-pill {
    display: inline-block;
    padding: 5px 13px;
    border-radius: 999px;
    background: var(--accent-soft);
    color: var(--accent);
    font-weight: 800;
    font-size: 15px;
    letter-spacing: 0.5px;
  }
  .call-peer-ava {
    width: 78px;
    height: 78px;
    border-radius: 50%;
    background: var(--input-bg);
    border: 1px solid var(--border-col);
    display: grid;
    place-items: center;
    font-size: 40px;
    margin: 0 auto 10px;
  }
  .call-peer-name {
    font-size: 20px;
    font-weight: 800;
    text-align: center;
  }
  .call-status-text {
    font-size: 13px;
    color: var(--muted-col);
    margin-top: 6px;
    min-height: 20px;
    text-align: center;
  }
  .call-actions-row {
    display: flex;
    gap: 10px;
    justify-content: center;
    align-items: center;
    flex-wrap: wrap;
    z-index: 3;
  }

  .reply-bar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    padding: 7px 12px;
    background: var(--card-bg);
    border-top: 1px solid var(--border-col);
    font-size: 12px;
  }
  .reply-bar-info {
    min-width: 0;
    flex: 1;
    border-left: 3px solid var(--accent);
    padding-left: 8px;
  }
  .reply-bar-title {
    font-weight: 700;
    color: var(--accent);
  }
  .reply-bar-text {
    color: var(--muted-col);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .bottom-dock {
    flex-shrink: 0;
    background: var(--dock-bg);
    border-top: 1px solid var(--border-col);
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
    background: var(--dock-bg);
    overflow-x: auto;
  }
  .em-btn {
    background: var(--sec-btn-bg);
    color: var(--sec-btn-text);
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
    background: var(--header-bg);
    border-top: 1px solid var(--border-col);
  }
  .attach-preview img {
    width: 52px;
    height: 52px;
    object-fit: cover;
    border-radius: 8px;
    border: 1px solid var(--border-col);
  }
  .attach-remove { padding: 6px 10px; font-size: 12px; border-radius: 8px; background: #b93838; color: #fff; margin-left: auto; }
  .composer {
    display: flex;
    align-items: flex-end;
    gap: 6px;
    padding: 8px 12px 10px;
    background: var(--dock-bg);
  }
  .clip-btn {
    background: var(--sec-btn-bg);
    color: var(--sec-btn-text);
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
    background: var(--card-bg);
    border: 1px solid var(--border-col);
  }
  .theme-swatches {
    display: grid;
    grid-template-columns: repeat(2, 1fr);
    gap: 8px;
    margin-top: 8px;
  }
  .theme-btn {
    display: flex;
    align-items: center;
    gap: 8px;
    width: 100%;
    padding: 9px 11px;
    border-radius: 10px;
    background: var(--input-bg);
    color: var(--text-col);
    border: 1px solid var(--border-col);
    font-size: 13px;
    font-weight: 700;
  }
  .theme-btn.active-theme {
    border-color: var(--accent);
    background: var(--accent-soft);
  }
  .swatch-dot {
    width: 16px;
    height: 16px;
    border-radius: 50%;
    flex-shrink: 0;
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
      <div id="inAppToastBox"></div>
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

  <div id="directChatSettingsScreen" class="hidden" style="flex:1; display:flex; flex-direction:column; min-height:0;">
    <div class="top">
      <button class="sec" onclick="backToRoomFromDirectSettings()">← В чат</button>
      <strong>Настройки чата</strong>
      <span style="width:52px;"></span>
    </div>
    <div class="pad" id="directChatSettingsBody"></div>
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
      <div class="top-actions">
        <button type="button" id="btnCallDirect" class="mini-btn blue-btn hidden" title="Позвонить" onclick="startDirectCall()">📞</button>
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

  <!-- Полноэкранный звонок -->
  <div id="callOverlay" class="call-overlay hidden">
    <div class="call-top-bar">
      <div class="call-timer-pill" id="callTimerLabel">00:00</div>
      <div class="muted" id="callPeerTopLabel">@peer</div>
    </div>

    <div class="call-stage">
      <video id="remoteVideoEl" class="call-remote-video" autoplay playsinline></video>
      <div id="callAvatarFallback" class="call-avatar-fallback">
        <div class="call-peer-ava" id="callPeerAva">🙂</div>
        <div class="call-peer-name" id="callPeerName">@peer</div>
        <div class="call-status-text" id="callStatusText">Вызов...</div>
      </div>
      <div class="call-local-pip hidden" id="callLocalPip">
        <video id="localVideoEl" class="call-local-video" autoplay muted playsinline></video>
      </div>
    </div>

    <div class="call-actions-row" id="callIncomingControls">
      <button type="button" class="blue-btn" onclick="acceptIncomingCall()">Принять</button>
      <button type="button" class="del" onclick="hangupCall(true)">Отклонить</button>
    </div>
    <div class="call-actions-row hidden" id="callActiveControls">
      <button type="button" id="btnToggleMic" class="sec" onclick="toggleMicMute()">🎤 Микрофон вкл</button>
      <button type="button" id="btnToggleCam" class="sec" onclick="toggleCamera()">📷 Камера выкл</button>
      <button type="button" class="del" onclick="hangupCall(true)">Выйти</button>
    </div>
  </div>

  <div id="settingsScreen" class="hidden" style="flex:1; display:flex; flex-direction:column; min-height:0;">
    <div class="top">
      <button class="sec" onclick="backToChats()">← Назад</button>
      <strong>Настройки</strong>
      <span style="width:52px;"></span>
    </div>
    <div class="pad">
      <div class="group-edit-card" style="margin-top:0;">
        <div style="font-weight:700;">🔔 Уведомления браузера</div>
        <div class="muted mt-8" id="notifPermStatusText">Проверка статуса уведомлений...</div>
        <button type="button" class="blue-btn mt-12" style="width:100%;" id="btnAskNotifPerm" onclick="requestNotificationPermissionFromUser()">Включить уведомления браузера</button>
      </div>

      <div style="font-weight:700; margin-top:18px;">Аватарка (только эмодзи)</div>
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
        <div class="muted mt-8">Настрой цвет сайта и размер элементов</div>

        <div class="mt-12" style="font-size:13px; font-weight:700;">Цвет сайта</div>
        <div class="theme-swatches" id="themePicker">
          <button type="button" class="theme-btn" data-theme="green" onclick="setSiteTheme('green')">
            <span class="swatch-dot" style="background:#9be15d;"></span>Зелёный
          </button>
          <button type="button" class="theme-btn" data-theme="red" onclick="setSiteTheme('red')">
            <span class="swatch-dot" style="background:#ff5c5c;"></span>Красный
          </button>
          <button type="button" class="theme-btn" data-theme="yellow" onclick="setSiteTheme('yellow')">
            <span class="swatch-dot" style="background:#ffd43b;"></span>Жёлтый
          </button>
          <button type="button" class="theme-btn" data-theme="blue" onclick="setSiteTheme('blue')">
            <span class="swatch-dot" style="background:#38a9ff;"></span>Синий
          </button>
        </div>

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
const SEVEN_DAYS_MS = 7 * 24 * 60 * 60 * 1000;

const AVATAR_EMOJIS = [
  "🍐", "😎", "🐱", "🐸", "🔥", "👾",
  "⚡", "🍕", "🦊", "🐼", "🤖", "👻",
  "🦄", "🐉", "🌙", "⭐", "💎", "🎮",
  "🎧", "🚀", "🌵", "🍀", "🍓", "🙂",
  "🐄", "🐗", "🔶"
];

const GROUP_ICONS = [
  "🦜", "🍐", "🔥", "⚡", "🎮", "🚀",
  "🍕", "🙂", "💎", "🐗", "🐄", "🔶"
];

const REACTION_EMOJIS = [
  "😀", "😂", "❤️", "🔥", "👍", "🍐", "🎉", "😭", "💩", "🤣"
];

const SITE_THEMES = {
  green: {
    outerBg: "#0d140e",
    surfaceBg: "#142116",
    headerBg: "#1c2e1f",
    dockBg: "#17261a",
    cardBg: "#1d3021",
    cardAltBg: "#233a27",
    inputBg: "#0e1710",
    borderCol: "#2d4a32",
    textCol: "#f2fbf3",
    mutedCol: "#9bc2a2",
    secBtnBg: "#253d29",
    secBtnText: "#eef9ef",
    accent: "#9be15d",
    contrast: "#0e1710",
    soft: "rgba(155, 225, 93, 0.2)"
  },
  red: {
    outerBg: "#170c0d",
    surfaceBg: "#241214",
    headerBg: "#33171b",
    dockBg: "#291417",
    cardBg: "#38191e",
    cardAltBg: "#441e24",
    inputBg: "#180c0e",
    borderCol: "#59262e",
    textCol: "#fff3f4",
    mutedCol: "#d49ca3",
    secBtnBg: "#471f26",
    secBtnText: "#fff0f1",
    accent: "#ff5c5c",
    contrast: "#1c080a",
    soft: "rgba(255, 92, 92, 0.22)"
  },
  yellow: {
    outerBg: "#171409",
    surfaceBg: "#241f0e",
    headerBg: "#332b12",
    dockBg: "#29230f",
    cardBg: "#382f14",
    cardAltBg: "#453917",
    inputBg: "#181509",
    borderCol: "#5c4d1e",
    textCol: "#fffbef",
    mutedCol: "#d6c58b",
    secBtnBg: "#473b18",
    secBtnText: "#fff9e6",
    accent: "#ffd43b",
    contrast: "#191404",
    soft: "rgba(255, 212, 59, 0.22)"
  },
  blue: {
    outerBg: "#0a121a",
    surfaceBg: "#101d2b",
    headerBg: "#16293d",
    dockBg: "#122132",
    cardBg: "#182e45",
    cardAltBg: "#1d3752",
    inputBg: "#0b141e",
    borderCol: "#25476b",
    textCol: "#f1f8ff",
    mutedCol: "#96bddf",
    secBtnBg: "#1f3a57",
    secBtnText: "#eef7ff",
    accent: "#38a9ff",
    contrast: "#07131e",
    soft: "rgba(56, 169, 255, 0.22)"
  }
};

const DEFAULT_CUSTOM = {
  msgSize: 15,
  emojiSize: 24,
  photoSize: 240,
  theme: "green"
};

const RTC_CONFIG = {
  iceServers: [
    { urls: "stun:stun.l.google.com:19302" },
    { urls: "stun:stun1.l.google.com:19302" },
    { urls: "stun:stun2.l.google.com:19302" },
    { urls: "stun:stun.services.mozilla.com" }
  ]
};

const EMOJI_ONLY_RE = /^(?:[\p{Emoji_Presentation}\p{Extended_Pictographic}]\uFE0F?(?:\u200D[\p{Emoji_Presentation}\p{Extended_Pictographic}]\uFE0F?)*|\s)+$/u;
const SINGLE_EMOJI_RE = /([\p{Emoji_Presentation}\p{Extended_Pictographic}]\uFE0F?(?:\u200D[\p{Emoji_Presentation}\p{Extended_Pictographic}]\uFE0F?)*)/gu;

let me = null;
let authTab = "login";
let activeChat = null;
let pendingImageData = "";
let replyTarget = null;
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
let seenGroupMsgIds = new Set();
let seenDirectMsgIds = new Set();
let roomHistoryInitialized = false;
let notifiedDirectMsgIds = new Set();
let directNotifInitDone = false;
let seenAlertIds = new Set();
let autoPermPromptTried = false;

let selectedGroupEmoji = "🦜";
let selectedGroupMembers = new Set();
let editGroupEmoji = "🦜";
let inviteGroupMembers = new Set();

let composerFocused = false;
let baselineViewportHeight = window.visualViewport ? window.visualViewport.height : window.innerHeight;
let audioCtx = null;

/* Состояние звонка 1 на 1 (аудио + камера) */
let callState = null;
let pc = null;
let localStream = null;
let remoteStream = null;
let micMuted = false;
let cameraEnabled = false;
let callTimerInterval = null;
let pendingOfferToApply = null;
let pendingRemoteCandidates = [];
let sentCandidateKeys = new Set();
let appliedCandidateKeys = new Set();
let answerSent = false;
let announcedIncomingCallId = "";
let renegotiating = false;
let needsRenegotiate = false;

function loadMutedDirectPeers() {
  try {
    const raw = JSON.parse(localStorage.getItem("grusha_muted_direct") || "[]");
    return new Set(Array.isArray(raw) ? raw.map(String) : []);
  } catch (e) {
    return new Set();
  }
}

function saveMutedDirectPeers(setObj) {
  try {
    localStorage.setItem("grusha_muted_direct", JSON.stringify(Array.from(setObj)));
  } catch (e) {}
}

function isDirectPeerMuted(peer) {
  if (!peer) return false;
  return loadMutedDirectPeers().has(String(peer));
}

function toggleDirectPeerMuted(peer) {
  if (!peer) return false;
  const setObj = loadMutedDirectPeers();
  const key = String(peer);
  let nowMuted = false;
  if (setObj.has(key)) {
    setObj.delete(key);
    nowMuted = false;
  } else {
    setObj.add(key);
    nowMuted = true;
  }
  saveMutedDirectPeers(setObj);
  return nowMuted;
}

function refreshNotificationUiStatus() {
  const txt = document.getElementById("notifPermStatusText");
  const btn = document.getElementById("btnAskNotifPerm");
  if (!txt || !btn) return;
  if (!("Notification" in window)) {
    txt.textContent = "Этот браузер не поддерживает системные уведомления.";
    btn.classList.add("hidden");
    return;
  }
  const perm = Notification.permission;
  if (perm === "granted") {
    txt.textContent = "Уведомления разрешены: придут для личных сообщений, звонков и групп.";
    btn.classList.add("hidden");
  } else if (perm === "denied") {
    txt.textContent = "Уведомления запрещены в настройках браузера для этого сайта. Разреши их в значке 🔒 в адресной строке.";
    btn.classList.remove("hidden");
  } else {
    txt.textContent = "Нажми кнопку ниже, чтобы браузер спросил разрешение на уведомления.";
    btn.classList.remove("hidden");
  }
}

async function requestNotificationPermissionFromUser() {
  if (!("Notification" in window)) {
    alert("Браузер не поддерживает уведомления.");
    return;
  }
  try {
    const perm = await Notification.requestPermission();
    refreshNotificationUiStatus();
    if (perm === "granted") {
      playSound("react");
      showBrowserNativeNotification("🍐 Груша", "Уведомления включены!");
    } else if (perm === "denied") {
      showInAppAlert("Уведомления заблокированы", "Включи уведомления для сайта в настройках браузера (иконка замочка/настроек в адресной строке).");
    }
  } catch (e) {
    refreshNotificationUiStatus();
  }
}

function tryAutoRequestNotificationOnce() {
  if (autoPermPromptTried) return;
  autoPermPromptTried = true;
  if (!("Notification" in window)) return;
  if (Notification.permission === "default") {
    try {
      const p = Notification.requestPermission();
      if (p && typeof p.then === "function") {
        p.then(() => refreshNotificationUiStatus()).catch(() => {});
      }
    } catch (e) {}
  }
  refreshNotificationUiStatus();
}

function showBrowserNativeNotification(title, body) {
  try {
    if (!("Notification" in window) || Notification.permission !== "granted") {
      return;
    }
    const opts = {
      body: body || "",
      icon: "data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'><text y='50' font-size='52'>🍐</text></svg>",
      tag: `grusha_${Date.now()}`,
      renotify: true
    };
    if (navigator.serviceWorker && navigator.serviceWorker.getRegistration) {
      navigator.serviceWorker.getRegistration().then(reg => {
        if (reg && reg.showNotification) {
          reg.showNotification(title, opts).catch(() => {
            try { new Notification(title, opts); } catch (err) {}
          });
        } else {
          try { new Notification(title, opts); } catch (err) {}
        }
      }).catch(() => {
        try { new Notification(title, opts); } catch (err) {}
      });
    } else {
      new Notification(title, opts);
    }
  } catch (e) {}
}

function showInAppAlert(title, body) {
  const box = document.getElementById("inAppToastBox");
  if (!box) return;
  box.innerHTML = `
    <div class="inapp-toast-banner">
      <div style="min-width:0;">
        <div style="font-weight:800;">${esc(title)}</div>
        <div class="muted" style="color:var(--text-col);">${esc(body)}</div>
      </div>
      <button type="button" class="mini-btn sec" onclick="this.parentElement.remove()">✕</button>
    </div>
  `;
}

function triggerSystemNotification(title, body, { allowInApp = true, isDirectMessage = false, peer = "" } = {}) {
  if (isDirectMessage && peer && isDirectPeerMuted(peer)) {
    return;
  }
  if (allowInApp) {
    showInAppAlert(title, body);
  }
  showBrowserNativeNotification(title, body);
}

function getAudioContext() {
  try {
    const Ctx = window.AudioContext || window.webkitAudioContext;
    if (!Ctx) return null;
    if (!audioCtx) audioCtx = new Ctx();
    if (audioCtx.state === "suspended") {
      audioCtx.resume().catch(() => {});
    }
    return audioCtx;
  } catch (e) {
    return null;
  }
}

function playTone(freq, duration = 0.09, type = "sine", delay = 0, gainPeak = 0.11, endFreq = null) {
  const ctx = getAudioContext();
  if (!ctx) return;
  try {
    const start = ctx.currentTime + delay;
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.type = type;
    osc.frequency.setValueAtTime(freq, start);
    if (endFreq !== null) {
      osc.frequency.exponentialRampToValueAtTime(Math.max(40, endFreq), start + duration);
    }
    gain.gain.setValueAtTime(0.0001, start);
    gain.gain.linearRampToValueAtTime(gainPeak, start + 0.012);
    gain.gain.exponentialRampToValueAtTime(0.0001, start + duration);
    osc.connect(gain);
    gain.connect(ctx.destination);
    osc.start(start);
    osc.stop(start + duration + 0.02);
  } catch (e) {}
}

function playSound(kind) {
  switch (kind) {
    case "send":
      playTone(540, 0.075, "triangle", 0, 0.11, 780);
      break;
    case "receive":
      playTone(620, 0.08, "sine", 0, 0.11, 820);
      playTone(840, 0.1, "sine", 0.08, 0.11, 1020);
      break;
    case "react":
      playTone(700, 0.06, "sine", 0, 0.1, 980);
      break;
    case "join":
      playTone(440, 0.08, "triangle", 0, 0.11, 587);
      playTone(659, 0.11, "triangle", 0.08, 0.11, 880);
      break;
    case "leave":
      playTone(620, 0.09, "triangle", 0, 0.11, 440);
      playTone(420, 0.12, "sine", 0.08, 0.1, 310);
      break;
    case "click":
      playTone(480, 0.035, "sine", 0, 0.05, 560);
      break;
    default:
      break;
  }
}

function requestPageFullscreen() {
  try {
    const el = document.documentElement;
    if (!document.fullscreenElement && !document.webkitFullscreenElement) {
      if (el.requestFullscreen) el.requestFullscreen().catch(() => {});
      else if (el.webkitRequestFullscreen) el.webkitRequestFullscreen();
    }
  } catch (e) {}
}

function syncRemoteMediaElements() {
  const vid = document.getElementById("remoteVideoEl");
  const fallback = document.getElementById("callAvatarFallback");
  if (!vid) return;
  vid.muted = false;
  vid.volume = 1.0;

  if (remoteStream && vid.srcObject !== remoteStream) {
    vid.srcObject = remoteStream;
  }

  const videoTracks = remoteStream ? remoteStream.getVideoTracks() : [];
  const hasLiveVideo = videoTracks.some(t => t.readyState === "live" && t.enabled && !t.muted);

  if (fallback) {
    fallback.classList.toggle("hidden-cam", hasLiveVideo);
  }
  const p = vid.play();
  if (p && typeof p.catch === "function") {
    p.catch(() => {});
  }
}

document.addEventListener("pointerdown", () => {
  getAudioContext();
  if ("Notification" in window && Notification.permission === "default" && me) {
    tryAutoRequestNotificationOnce();
  }
  if (remoteStream) {
    syncRemoteMediaElements();
  }
}, { passive: true });

function sanitizeAvatarEmoji(em) {
  const s = String(em || "").trim();
  if (!s) return "🍐";
  if (s === "👑" || s === "💩") return "🙂";
  return s;
}

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

function formatCallDuration(sec) {
  const s = Math.max(0, Math.floor(sec));
  const mins = Math.floor(s / 60);
  const rem = s % 60;
  return `${String(mins).padStart(2, "0")}:${String(rem).padStart(2, "0")}`;
}

function startCallDurationTicker() {
  stopCallDurationTicker();
  updateCallDurationLabel();
  callTimerInterval = setInterval(updateCallDurationLabel, 1000);
}

function stopCallDurationTicker() {
  if (callTimerInterval) {
    clearInterval(callTimerInterval);
    callTimerInterval = null;
  }
}

function updateCallDurationLabel() {
  const el = document.getElementById("callTimerLabel");
  if (!el) return;
  if (!callState || !callState.connectedAt) {
    el.textContent = "00:00";
    return;
  }
  const elapsedSec = Math.floor((Date.now() - callState.connectedAt) / 1000);
  el.textContent = formatCallDuration(elapsedSec);
}

function setCallStatusNote(text) {
  const statusEl = document.getElementById("callStatusText");
  if (statusEl) statusEl.textContent = text;
}

function updateCamButtonUi() {
  const btn = document.getElementById("btnToggleCam");
  const pip = document.getElementById("callLocalPip");
  if (btn) {
    if (cameraEnabled) {
      btn.textContent = "📷 Камера вкл";
      btn.classList.remove("sec");
      btn.classList.add("blue-btn");
    } else {
      btn.textContent = "📷 Камера выкл";
      btn.classList.remove("blue-btn");
      btn.classList.add("sec");
    }
  }
  if (pip) {
    pip.classList.toggle("hidden", !cameraEnabled);
  }
}

function showCallModalUi() {
  const overlay = document.getElementById("callOverlay");
  if (!overlay || !callState) return;
  overlay.classList.remove("hidden");
  document.getElementById("callPeerAva").textContent = sanitizeAvatarEmoji(callState.peerEmoji || "🙂");
  document.getElementById("callPeerName").textContent = `@${callState.peer}`;
  document.getElementById("callPeerTopLabel").textContent = `@${callState.peer}`;

  const incBox = document.getElementById("callIncomingControls");
  const actBox = document.getElementById("callActiveControls");

  if (callState.status === "ringing" && !callState.isCaller) {
    incBox.classList.remove("hidden");
    actBox.classList.add("hidden");
    setCallStatusNote(`Входящий звонок от @${callState.peer}`);
  } else if (callState.status === "ringing" && callState.isCaller) {
    incBox.classList.add("hidden");
    actBox.classList.remove("hidden");
    setCallStatusNote(`Звоним @${callState.peer}...`);
  } else {
    incBox.classList.add("hidden");
    actBox.classList.remove("hidden");
    if (!callState.iceNote) {
      setCallStatusNote("Соединение...");
    }
  }
  updateCamButtonUi();
  updateCallDurationLabel();
  syncRemoteMediaElements();
}

function hideCallModalUi() {
  const overlay = document.getElementById("callOverlay");
  if (overlay) overlay.classList.add("hidden");
}

function cleanupLocalCallResources() {
  stopCallDurationTicker();
  if (pc) {
    try { pc.close(); } catch (e) {}
    pc = null;
  }
  if (localStream) {
    try {
      localStream.getTracks().forEach(t => t.stop());
    } catch (e) {}
    localStream = null;
  }
  if (remoteStream) {
    try {
      remoteStream.getTracks().forEach(t => t.stop());
    } catch (e) {}
    remoteStream = null;
  }
  const remVid = document.getElementById("remoteVideoEl");
  if (remVid) remVid.srcObject = null;
  const locVid = document.getElementById("localVideoEl");
  if (locVid) locVid.srcObject = null;
  const fallback = document.getElementById("callAvatarFallback");
  if (fallback) fallback.classList.remove("hidden-cam");

  micMuted = false;
  cameraEnabled = false;
  renegotiating = false;
  needsRenegotiate = false;
  const micBtn = document.getElementById("btnToggleMic");
  if (micBtn) {
    micBtn.textContent = "🎤 Микрофон вкл";
    micBtn.classList.remove("warn");
    micBtn.classList.add("sec");
  }
  updateCamButtonUi();

  callState = null;
  pendingOfferToApply = null;
  pendingRemoteCandidates = [];
  sentCandidateKeys = new Set();
  appliedCandidateKeys = new Set();
  answerSent = false;
  announcedIncomingCallId = "";
  hideCallModalUi();
}

async function ensureLocalAudioStream() {
  if (localStream && localStream.getAudioTracks().length > 0) {
    localStream.getAudioTracks().forEach(t => { t.enabled = !micMuted; });
    return localStream;
  }
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    throw new Error("Браузер не поддерживает доступ к микрофону");
  }
  const audioOnly = await navigator.mediaDevices.getUserMedia({
    audio: {
      echoCancellation: true,
      noiseSuppression: true,
      autoGainControl: true
    },
    video: false
  });
  if (!localStream) {
    localStream = audioOnly;
  } else {
    audioOnly.getAudioTracks().forEach(t => localStream.addTrack(t));
  }
  return localStream;
}

async function flushRenegotiateIfReady() {
  if (!pc || !callState) return;
  if (!needsRenegotiate || renegotiating) return;
  if (pc.signalingState !== "stable") return;

  renegotiating = true;
  needsRenegotiate = false;
  try {
    const offer = await pc.createOffer({ offerToReceiveAudio: true, offerToReceiveVideo: true });
    await pc.setLocalDescription(offer);
    await api("/api/call/offer", {
      callId: callState.callId,
      from: me.username,
      offer: pc.localDescription
    });
  } catch (e) {} finally {
    renegotiating = false;
    if (needsRenegotiate) {
      flushRenegotiateIfReady();
    }
  }
}

function scheduleRenegotiate() {
  needsRenegotiate = true;
  flushRenegotiateIfReady();
}

async function toggleCamera() {
  if (!callState) return;
  playSound("click");

  if (!cameraEnabled) {
    try {
      if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
        alert("Камера недоступна в этом браузере.");
        return;
      }
      const camStream = await navigator.mediaDevices.getUserMedia({
        video: { facingMode: "user", width: { ideal: 640 }, height: { ideal: 480 } },
        audio: false
      });
      const vTrack = camStream.getVideoTracks()[0];
      if (!vTrack) return;

      if (!localStream) {
        localStream = new MediaStream([vTrack]);
      } else {
        localStream.getVideoTracks().forEach(t => {
          t.stop();
          localStream.removeTrack(t);
        });
        localStream.addTrack(vTrack);
      }

      const locVid = document.getElementById("localVideoEl");
      if (locVid) {
        locVid.srcObject = localStream;
        locVid.play().catch(() => {});
      }

      if (pc) {
        const videoTransceiver = pc.getTransceivers().find(tr => tr.receiver && tr.receiver.track && tr.receiver.track.kind === "video");
        if (videoTransceiver && videoTransceiver.sender) {
          videoTransceiver.direction = "sendrecv";
          await videoTransceiver.sender.replaceTrack(vTrack);
        } else {
          const sender = pc.getSenders().find(s => s.track && s.track.kind === "video");
          if (sender) {
            await sender.replaceTrack(vTrack);
          } else {
            pc.addTrack(vTrack, localStream);
          }
        }
        scheduleRenegotiate();
      }

      cameraEnabled = true;
      updateCamButtonUi();
    } catch (err) {
      alert("Не удалось включить камеру. Разреши доступ к камере и проверь HTTPS.");
    }
  } else {
    if (localStream) {
      localStream.getVideoTracks().forEach(t => {
        t.stop();
        localStream.removeTrack(t);
      });
    }
    const locVid = document.getElementById("localVideoEl");
    if (locVid) locVid.srcObject = null;

    if (pc) {
      const sender = pc.getSenders().find(s => s.track && s.track.kind === "video");
      if (sender) {
        try { await sender.replaceTrack(null); } catch (e) {}
      }
      scheduleRenegotiate();
    }

    cameraEnabled = false;
    updateCamButtonUi();
  }
}

async function flushPendingCandidates() {
  if (!pc || !pc.remoteDescription || !pendingRemoteCandidates.length) return;
  const queue = [...pendingRemoteCandidates];
  pendingRemoteCandidates = [];
  for (const candObj of queue) {
    try {
      await pc.addIceCandidate(new RTCIceCandidate(candObj));
    } catch (e) {}
  }
}

async function enqueueOrAddRemoteCandidate(candObj) {
  if (!candObj) return;
  const key = JSON.stringify(candObj);
  if (appliedCandidateKeys.has(key)) return;
  appliedCandidateKeys.add(key);

  if (pc && pc.remoteDescription) {
    try {
      await pc.addIceCandidate(new RTCIceCandidate(candObj));
    } catch (e) {}
  } else {
    pendingRemoteCandidates.push(candObj);
  }
}

function attachTrackToRemoteStream(track) {
  if (!track) return;
  if (!remoteStream) {
    remoteStream = new MediaStream();
  }
  const existing = remoteStream.getTracks().find(t => t.id === track.id || t.kind === track.kind);
  if (existing && existing.id !== track.id) {
    remoteStream.removeTrack(existing);
  }
  if (!remoteStream.getTracks().some(t => t.id === track.id)) {
    remoteStream.addTrack(track);
  }
  track.onunmute = () => syncRemoteMediaElements();
  track.onmute = () => syncRemoteMediaElements();
  track.onended = () => syncRemoteMediaElements();
  syncRemoteMediaElements();
}

function createPeerConnectionForCall(cid) {
  const conn = new RTCPeerConnection(RTC_CONFIG);
  remoteStream = new MediaStream();
  const vid = document.getElementById("remoteVideoEl");
  if (vid) {
    vid.srcObject = remoteStream;
    vid.muted = false;
    vid.volume = 1.0;
  }

  try {
    conn.addTransceiver("audio", { direction: "sendrecv" });
    conn.addTransceiver("video", { direction: "sendrecv" });
  } catch (e) {}

  if (localStream) {
    localStream.getTracks().forEach(track => {
      const existingSender = conn.getSenders().find(s => s.track && s.track.kind === track.kind);
      if (existingSender) {
        existingSender.replaceTrack(track).catch(() => {});
      } else {
        conn.addTrack(track, localStream);
      }
    });
  }

  conn.ontrack = (event) => {
    if (event.track) {
      attachTrackToRemoteStream(event.track);
    }
    if (event.streams && event.streams[0]) {
      event.streams[0].getTracks().forEach(t => attachTrackToRemoteStream(t));
    }
    syncRemoteMediaElements();
  };

  conn.onicecandidate = (event) => {
    if (!event.candidate || !callState || callState.callId !== cid) return;
    const candJson = event.candidate.toJSON ? event.candidate.toJSON() : event.candidate;
    const key = JSON.stringify(candJson);
    if (sentCandidateKeys.has(key)) return;
    sentCandidateKeys.add(key);
    api("/api/call/candidate", {
      callId: cid,
      from: me.username,
      candidate: candJson
    }).catch(() => {});
  };

  conn.oniceconnectionstatechange = () => {
    if (!callState || callState.callId !== cid) return;
    const st = conn.iceConnectionState;
    if (st === "connected" || st === "completed") {
      callState.iceNote = "Связь установлена";
      setCallStatusNote("Связь установлена");
      syncRemoteMediaElements();
    } else if (st === "checking") {
      callState.iceNote = "Установка соединения...";
      setCallStatusNote("Установка соединения...");
    } else if (st === "failed") {
      callState.iceNote = "Нет прямого маршрута между сетями";
      setCallStatusNote("Нет прямого маршрута между сетями");
    } else if (st === "disconnected") {
      callState.iceNote = "Связь прерывалась...";
      setCallStatusNote("Связь прерывалась...");
    }
  };

  return conn;
}

async function startDirectCall() {
  if (!me || !activeChat || activeChat.type !== "direct") return;
  if (callState) return;
  playSound("click");
  requestPageFullscreen();

  try {
    await ensureLocalAudioStream();
  } catch (err) {
    alert("Не удалось включить микрофон. Проверь разрешение микрофона и HTTPS.");
    return;
  }

  const peer = activeChat.username;
  const peerEmoji = activeChat.emoji || "🙂";
  const createRes = await api("/api/call/start", { caller: me.username, callee: peer });
  if (!createRes.ok || !createRes.callId) {
    cleanupLocalCallResources();
    alert(createRes.error || "Не удалось начать звонок");
    return;
  }

  const cid = createRes.callId;
  callState = {
    callId: cid,
    peer,
    peerEmoji,
    isCaller: true,
    status: "ringing",
    connectedAt: null,
    iceNote: "",
    lastAppliedOfferJson: "",
    lastAppliedAnswerJson: ""
  };
  showCallModalUi();

  pc = createPeerConnectionForCall(cid);
  const offer = await pc.createOffer({ offerToReceiveAudio: true, offerToReceiveVideo: true });
  await pc.setLocalDescription(offer);
  callState.lastAppliedOfferJson = JSON.stringify(pc.localDescription);
  await api("/api/call/offer", {
    callId: cid,
    from: me.username,
    offer: pc.localDescription
  });
}

async function acceptIncomingCall() {
  if (!me || !callState || callState.isCaller) return;
  playSound("join");
  requestPageFullscreen();

  try {
    await ensureLocalAudioStream();
  } catch (err) {
    alert("Не удалось включить микрофон. Проверь разрешение микрофона и HTTPS.");
    await hangupCall(true);
    return;
  }

  const cid = callState.callId;
  pc = createPeerConnectionForCall(cid);

  if (pendingOfferToApply) {
    await pc.setRemoteDescription(new RTCSessionDescription(pendingOfferToApply));
    callState.lastAppliedOfferJson = JSON.stringify(pendingOfferToApply);
    pendingOfferToApply = null;
    await flushPendingCandidates();
  }

  if (pc.remoteDescription) {
    const answer = await pc.createAnswer({ offerToReceiveAudio: true, offerToReceiveVideo: true });
    await pc.setLocalDescription(answer);
    callState.lastAppliedAnswerJson = JSON.stringify(pc.localDescription);
    answerSent = true;
    await api("/api/call/answer", {
      callId: cid,
      from: me.username,
      answer: pc.localDescription
    });
  }

  callState.status = "active";
  if (!callState.connectedAt) {
    callState.connectedAt = Date.now();
  }
  showCallModalUi();
  startCallDurationTicker();
  syncRemoteMediaElements();
}

function toggleMicMute() {
  if (!localStream) return;
  micMuted = !micMuted;
  localStream.getAudioTracks().forEach(track => {
    track.enabled = !micMuted;
  });
  playSound("click");
  const btn = document.getElementById("btnToggleMic");
  if (btn) {
    if (micMuted) {
      btn.textContent = "🔇 Микрофон выкл";
      btn.classList.remove("sec");
      btn.classList.add("warn");
    } else {
      btn.textContent = "🎤 Микрофон вкл";
      btn.classList.remove("warn");
      btn.classList.add("sec");
    }
  }
}

async function hangupCall(notifyServer = true) {
  const cid = callState ? callState.callId : null;
  playSound("leave");
  cleanupLocalCallResources();
  if (notifyServer && cid && me) {
    api("/api/call/end", { callId: cid, from: me.username }).catch(() => {});
  }
}

async function pollCallStatus() {
  if (!me) return;
  const activeId = callState ? callState.callId : "";
  const res = await api(`/api/call/poll?user=${encodeURIComponent(me.username)}&callId=${encodeURIComponent(activeId)}`);
  if (!res || !res.ok) return;

  if (res.ended) {
    if (callState) {
      playSound("leave");
      cleanupLocalCallResources();
    }
    return;
  }

  const c = res.call;
  if (!c) return;

  if (!callState) {
    if (c.status === "ringing" && c.callee === me.username) {
      const peerUser = c.caller;
      playSound("receive");
      callState = {
        callId: c.callId,
        peer: peerUser,
        peerEmoji: sanitizeAvatarEmoji(c.callerEmoji || "🙂"),
        isCaller: false,
        status: "ringing",
        connectedAt: null,
        iceNote: "",
        lastAppliedOfferJson: "",
        lastAppliedAnswerJson: ""
      };
      if (c.offer) {
        pendingOfferToApply = c.offer;
      }
      showCallModalUi();
      if (announcedIncomingCallId !== c.callId) {
        announcedIncomingCallId = c.callId;
        triggerSystemNotification(
          "Вас пригласили в звонок",
          `@${peerUser} вызывает вас на звонок`,
          { allowInApp: true, isDirectMessage: false }
        );
      }
    }
    return;
  }

  if (c.callId !== callState.callId) return;

  if (callState.isCaller && c.offer && !pc) {
    pc = createPeerConnectionForCall(c.callId);
  }

  /* Обработка новых офферов (включая ренегациацию при включении/выключении камеры) */
  if (c.offer) {
    const offerJson = JSON.stringify(c.offer);
    const isNewOfferFromPeer = c.offerBy && c.offerBy !== me.username && offerJson !== callState.lastAppliedOfferJson;
    if (isNewOfferFromPeer) {
      if (pc && pc.signalingState === "stable") {
        try {
          await pc.setRemoteDescription(new RTCSessionDescription(c.offer));
          callState.lastAppliedOfferJson = offerJson;
          await flushPendingCandidates();
          const ans = await pc.createAnswer({ offerToReceiveAudio: true, offerToReceiveVideo: true });
          await pc.setLocalDescription(ans);
          callState.lastAppliedAnswerJson = JSON.stringify(pc.localDescription);
          await api("/api/call/answer", {
            callId: c.callId,
            from: me.username,
            answer: pc.localDescription
          });
          syncRemoteMediaElements();
        } catch (e) {}
      } else if (!callState.isCaller && (!pc || !pc.remoteDescription)) {
        pendingOfferToApply = c.offer;
      }
    }
  }

  /* Обработка ответа на наш оффер (первый ответ или ответ на ренегациацию камеры) */
  if (c.answer && pc && c.answerBy && c.answerBy !== me.username) {
    const remoteJson = JSON.stringify(c.answer);
    if (remoteJson !== callState.lastAppliedAnswerJson && pc.signalingState === "have-local-offer") {
      try {
        await pc.setRemoteDescription(new RTCSessionDescription(c.answer));
        callState.lastAppliedAnswerJson = remoteJson;
        await flushPendingCandidates();
        flushRenegotiateIfReady();
        syncRemoteMediaElements();
      } catch (e) {}
    }
    if (callState.isCaller && callState.status !== "active") {
      callState.status = "active";
      if (!callState.connectedAt) {
        callState.connectedAt = c.connectedAt ? Number(c.connectedAt) : Date.now();
      }
      playSound("join");
      showCallModalUi();
      startCallDurationTicker();
      syncRemoteMediaElements();
    }
  }

  if (!callState.isCaller && c.status === "active" && callState.status !== "active") {
    callState.status = "active";
    if (!callState.connectedAt) {
      callState.connectedAt = c.connectedAt ? Number(c.connectedAt) : Date.now();
    }
    showCallModalUi();
    startCallDurationTicker();
    syncRemoteMediaElements();
  }

  const incomingCands = Array.isArray(c.candidates) ? c.candidates : [];
  for (const item of incomingCands) {
    if (!item || item.from === me.username || !item.candidate) continue;
    await enqueueOrAddRemoteCandidate(item.candidate);
  }

  flushRenegotiateIfReady();

  if (callState && callState.status === "active") {
    syncRemoteMediaElements();
  }
}

async function pollUserAlerts() {
  if (!me) return;
  const res = await api(`/api/alerts?user=${encodeURIComponent(me.username)}`);
  if (!res || !res.ok || !Array.isArray(res.alerts)) return;

  for (const al of res.alerts) {
    if (!al || !al.id || seenAlertIds.has(al.id)) continue;
    seenAlertIds.add(al.id);
    if (al.kind === "kicked") {
      playSound("leave");
      triggerSystemNotification("Вас удалили", al.body || "Вас удалили из группы", { allowInApp: true });
      if (activeChat && activeChat.type === "group" && (!al.groupId || activeChat.id === al.groupId)) {
        backToChats();
      }
    } else if (al.kind === "added_to_group") {
      playSound("join");
      triggerSystemNotification("Вас добавили в группу", al.body || "Вас добавили в новую группу", { allowInApp: true });
      refreshChats();
    }
  }
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
    const themeKey = SITE_THEMES[raw.theme] ? raw.theme : DEFAULT_CUSTOM.theme;
    return {
      msgSize: Number(raw.msgSize) || DEFAULT_CUSTOM.msgSize,
      emojiSize: Number(raw.emojiSize) || DEFAULT_CUSTOM.emojiSize,
      photoSize: Number(raw.photoSize) || DEFAULT_CUSTOM.photoSize,
      theme: themeKey
    };
  } catch (e) {
    return { ...DEFAULT_CUSTOM };
  }
}

function applyThemeButtonsState(themeKey) {
  document.querySelectorAll("#themePicker .theme-btn").forEach(btn => {
    btn.classList.toggle("active-theme", btn.getAttribute("data-theme") === themeKey);
  });
}

function applyCustom(cfg) {
  const themeKey = SITE_THEMES[cfg.theme] ? cfg.theme : DEFAULT_CUSTOM.theme;
  const palette = SITE_THEMES[themeKey];
  const rootStyle = document.documentElement.style;
  rootStyle.setProperty("--outer-bg", palette.outerBg);
  rootStyle.setProperty("--surface-bg", palette.surfaceBg);
  rootStyle.setProperty("--header-bg", palette.headerBg);
  rootStyle.setProperty("--dock-bg", palette.dockBg);
  rootStyle.setProperty("--card-bg", palette.cardBg);
  rootStyle.setProperty("--card-alt-bg", palette.cardAltBg);
  rootStyle.setProperty("--input-bg", palette.inputBg);
  rootStyle.setProperty("--border-col", palette.borderCol);
  rootStyle.setProperty("--text-col", palette.textCol);
  rootStyle.setProperty("--muted-col", palette.mutedCol);
  rootStyle.setProperty("--sec-btn-bg", palette.secBtnBg);
  rootStyle.setProperty("--sec-btn-text", palette.secBtnText);
  rootStyle.setProperty("--accent", palette.accent);
  rootStyle.setProperty("--accent-contrast", palette.contrast);
  rootStyle.setProperty("--accent-soft", palette.soft);

  rootStyle.setProperty("--msg-font-size", `${cfg.msgSize}px`);
  rootStyle.setProperty("--emoji-size", `${cfg.emojiSize}px`);
  rootStyle.setProperty("--photo-max-size", `${cfg.photoSize}px`);

  applyThemeButtonsState(themeKey);

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

function setSiteTheme(themeKey) {
  if (!SITE_THEMES[themeKey]) return;
  const cur = loadCustom();
  const next = { ...cur, theme: themeKey };
  try { localStorage.setItem("grusha_custom", JSON.stringify(next)); } catch (e) {}
  applyCustom(next);
  playSound("click");
}

function updateCustomization() {
  const cur = loadCustom();
  const cfg = {
    msgSize: Number(document.getElementById("msgSizeRange").value) || DEFAULT_CUSTOM.msgSize,
    emojiSize: Number(document.getElementById("emojiSizeRange").value) || DEFAULT_CUSTOM.emojiSize,
    photoSize: Number(document.getElementById("photoSizeRange").value) || DEFAULT_CUSTOM.photoSize,
    theme: cur.theme || DEFAULT_CUSTOM.theme
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
  playSound("click");
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
    "button, input, textarea, select, a, .chat-card, .member-pick, .group-member-row, .group-edit-card, .room-header-main, .m, .msg-img, .em-btn, .clip-btn, .attach-preview, .reply-bar, .msg-menu-card, .reaction-chip, .call-overlay, .top, .bottom-dock, .custom-box, .fab-create"
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
  if (user) {
    user = { ...user, emoji: sanitizeAvatarEmoji(user.emoji) };
  }
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
  ["loadingScreen", "authScreen", "chatsScreen", "createGroupScreen", "groupSettingsScreen", "directChatSettingsScreen", "searchScreen", "roomScreen", "settingsScreen"].forEach(s => {
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
  playSound("join");
  enterChats();
}

function enterChats() {
  activeChat = null;
  replyTarget = null;
  roomHistoryInitialized = false;
  clearAttachment();
  lastRenderedMsgsJson = "";
  document.getElementById("myAva").textContent = sanitizeAvatarEmoji(me.emoji);
  document.getElementById("myNick").innerHTML = nickLabelHtml(me.username, me.isOwner);
  refreshNotificationUiStatus();
  if ("Notification" in window && Notification.permission === "default") {
    showInAppAlert("Включить уведомления?", "Разреши уведомления для личных сообщений и звонков.");
  }
  showScreen("chatsScreen");
  refreshChats();
  pollUserAlerts();
  startPolling();
}

function backToChats() {
  playSound("click");
  activeChat = null;
  replyTarget = null;
  roomHistoryInitialized = false;
  clearAttachment();
  lastRenderedMsgsJson = "";
  showScreen("chatsScreen");
  refreshChats();
}

function backToRoomFromGroupSettings() {
  playSound("click");
  if (!activeChat) {
    backToChats();
    return;
  }
  showScreen("roomScreen");
  refreshMessages(false);
}

function backToRoomFromDirectSettings() {
  playSound("click");
  if (!activeChat) {
    backToChats();
    return;
  }
  showScreen("roomScreen");
  refreshMessages(false);
}

function openDirectChatSettings() {
  if (!activeChat || activeChat.type !== "direct") return;
  playSound("click");
  showScreen("directChatSettingsScreen");
  renderDirectChatSettings();
}

function renderDirectChatSettings() {
  const body = document.getElementById("directChatSettingsBody");
  if (!body || !activeChat || activeChat.type !== "direct") return;
  const muted = isDirectPeerMuted(activeChat.username);
  body.innerHTML = `
    <div style="display:flex; align-items:center; gap:12px;">
      <div class="ava" style="width:52px; height:52px; font-size:26px;">${sanitizeAvatarEmoji(activeChat.emoji)}</div>
      <div>
        <div style="font-size:18px; font-weight:800;">${nickLabelHtml(activeChat.username, activeChat.isOwner)}</div>
        <div class="muted">Личный чат</div>
      </div>
    </div>

    <div class="group-edit-card">
      <div style="font-weight:700;">Уведомления о сообщениях</div>
      <div class="muted mt-8">
        ${muted
          ? "Уведомления от этого пользователя сейчас отключены."
          : "Уведомления от этого пользователя включены (если разрешены в браузере)."}
      </div>
      <button type="button" class="${muted ? 'ok' : 'warn'} mt-12" style="width:100%;" onclick="toggleMuteCurrentDirectChat()">
        ${muted ? "Включить уведомления" : "Отключить уведомления"}
      </button>
    </div>

    <div class="group-edit-card">
      <div style="font-weight:700; color:#ff8c8c;">Удаление чата</div>
      <div class="muted mt-8">История сообщений в этом личном чате будет удалена.</div>
      <button type="button" class="del mt-12" style="width:100%;" onclick="deleteDirectChat()">Удалить чат</button>
    </div>
  `;
}

function toggleMuteCurrentDirectChat() {
  if (!activeChat || activeChat.type !== "direct") return;
  playSound("click");
  toggleDirectPeerMuted(activeChat.username);
  renderDirectChatSettings();
}

function selectGroupEmoji(em) {
  selectedGroupEmoji = sanitizeAvatarEmoji(em);
  document.getElementById("selectedGroupIconLabel").textContent = selectedGroupEmoji;
  playSound("click");
  renderGroupIconPicker();
}

function renderGroupIconPicker() {
  const box = document.getElementById("groupIconPicker");
  if (!box) return;
  box.innerHTML = GROUP_ICONS.map(em => {
    const cls = em === selectedGroupEmoji ? "em-btn" : "em-btn sec";
    return `<button type="button" class="${cls}" style="${em === selectedGroupEmoji ? 'outline:2px solid var(--accent);' : ''}" onclick="selectGroupEmoji('${em}')">${em}</button>`;
  }).join("");
}

function toggleGroupMember(nick) {
  if (selectedGroupMembers.has(nick)) selectedGroupMembers.delete(nick);
  else selectedGroupMembers.add(nick);
  playSound("click");
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
    const ava = sanitizeAvatarEmoji(u.emoji);
    return `
      <div class="member-pick ${checked ? 'selected' : ''}" onclick="toggleGroupMember('${esc(u.username)}')">
        <input type="checkbox" ${checked ? 'checked' : ''} onclick="event.stopPropagation(); toggleGroupMember('${esc(u.username)}')">
        <div class="ava" style="width:36px; height:36px; font-size:18px;">${ava}</div>
        <div class="chat-name">${nickLabelHtml(u.username, u.isOwner)}</div>
      </div>
    `;
  }).join("");
}

async function openCreateGroup() {
  playSound("click");
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
  playSound("join");
  await openGroupChat(res.group.id, res.group.title, res.group.emoji, res.group.tripleBadge);
}

function onRoomTitleClick() {
  if (!activeChat) return;
  if (activeChat.type === "group") {
    openGroupSettings();
  } else if (activeChat.type === "direct") {
    openDirectChatSettings();
  }
}

async function openGroupSettings() {
  if (!activeChat || activeChat.type !== "group") return;
  playSound("click");
  inviteGroupMembers = new Set();
  showScreen("groupSettingsScreen");
  await renderGroupSettings();
}

function selectEditGroupEmoji(em) {
  editGroupEmoji = sanitizeAvatarEmoji(em);
  const lbl = document.getElementById("editGroupIconLabel");
  if (lbl) lbl.textContent = editGroupEmoji;
  playSound("click");
  renderEditGroupIconPicker();
}

function renderEditGroupIconPicker() {
  const box = document.getElementById("editGroupIconPicker");
  if (!box) return;
  box.innerHTML = GROUP_ICONS.map(em => {
    const cls = em === editGroupEmoji ? "em-btn" : "em-btn sec";
    return `<button type="button" class="${cls}" style="${em === editGroupEmoji ? 'outline:2px solid var(--accent);' : ''}" onclick="selectEditGroupEmoji('${em}')">${em}</button>`;
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
  playSound("react");
  activeChat.username = res.group.title;
  activeChat.emoji = sanitizeAvatarEmoji(res.group.emoji);
  activeChat.tripleBadge = Boolean(res.group.tripleBadge);
  document.getElementById("peerAva").textContent = activeChat.emoji;
  document.getElementById("peerNick").innerHTML = groupTitleHtml(res.group.title, activeChat.tripleBadge);
  await renderGroupSettings();
  const okAfter = document.getElementById("editGroupOk");
  if (okAfter) okAfter.textContent = "Название и иконка обновлены!";
  await refreshChats();
}

function toggleInviteCandidate(nick) {
  if (inviteGroupMembers.has(nick)) inviteGroupMembers.delete(nick);
  else inviteGroupMembers.add(nick);
  playSound("click");
  const el = document.querySelector(`.invite-pick[data-nick="${CSS.escape(nick)}"]`);
  if (el) {
    const checked = inviteGroupMembers.has(nick);
    el.classList.toggle("selected", checked);
    const cb = el.querySelector('input[type="checkbox"]');
    if (cb) cb.checked = checked;
  }
}

async function submitInviteMembers() {
  if (!activeChat || activeChat.type !== "group") return;
  const errEl = document.getElementById("inviteGroupErr");
  const list = Array.from(inviteGroupMembers);
  if (!list.length) {
    if (errEl) errEl.textContent = "Выберите хотя бы одного пользователя для добавления";
    return;
  }
  if (errEl) errEl.textContent = "";
  const res = await api("/api/groups/add-members", {
    actor: me.username,
    groupId: activeChat.id,
    members: list
  });
  if (!res.ok) {
    if (errEl) errEl.textContent = res.error || "Не удалось добавить участников";
    return;
  }
  playSound("join");
  inviteGroupMembers = new Set();
  await renderGroupSettings();
  await refreshMessages(false);
  await refreshChats();
}

async function renderGroupSettings() {
  const body = document.getElementById("groupSettingsBody");
  if (!body || !activeChat || activeChat.type !== "group") return;

  body.innerHTML = `<div class="muted">Загрузка настроек группы...</div>`;

  const res = await api(`/api/groups/info?groupId=${encodeURIComponent(activeChat.id)}&user=${encodeURIComponent(me.username)}`);
  if (!res.ok || !res.group) {
    body.innerHTML = `<div class="muted">Группа не найдена, удалена или вы вышли из неё.</div><button class="sec mt-12" onclick="backToChats()">← К чатам</button>`;
    return;
  }

  const g = res.group;
  const isGroupOwner = g.owner === me.username;
  const membersSet = new Set(g.members || []);
  const mutedSet = new Set(g.muted || []);
  editGroupEmoji = sanitizeAvatarEmoji(g.emoji || "🦜");

  activeChat.username = g.title;
  activeChat.emoji = editGroupEmoji;
  activeChat.tripleBadge = Boolean(g.tripleBadge);
  document.getElementById("peerAva").textContent = editGroupEmoji;
  document.getElementById("peerNick").innerHTML = groupTitleHtml(g.title, activeChat.tripleBadge);

  let inviteCandidates = [];
  if (isGroupOwner) {
    const chatsRes = await api(`/api/chats?user=${encodeURIComponent(me.username)}`);
    if (chatsRes.ok && Array.isArray(chatsRes.chats)) {
      inviteCandidates = chatsRes.chats.filter(c => c.type !== "group" && !membersSet.has(c.username));
    }
    inviteGroupMembers = new Set(Array.from(inviteGroupMembers).filter(n => !membersSet.has(n)));
  }

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

  const ownerInviteHtml = isGroupOwner ? `
    <div class="group-edit-card">
      <div style="font-weight:700;">Добавить участников</div>
      ${
        inviteCandidates.length
          ? inviteCandidates.map(u => {
              const checked = inviteGroupMembers.has(u.username);
              const ava = sanitizeAvatarEmoji(u.emoji);
              return `
                <div class="member-pick invite-pick ${checked ? 'selected' : ''}" data-nick="${esc(u.username)}" onclick="toggleInviteCandidate('${esc(u.username)}')">
                  <input type="checkbox" ${checked ? 'checked' : ''} onclick="event.stopPropagation(); toggleInviteCandidate('${esc(u.username)}')">
                  <div class="ava" style="width:34px; height:34px; font-size:17px;">${ava}</div>
                  <div class="chat-name">${nickLabelHtml(u.username, u.isOwner)}</div>
                </div>
              `;
            }).join("") +
            `<div id="inviteGroupErr" class="err"></div><button type="button" class="blue-btn mt-8" style="width:100%;" onclick="submitInviteMembers()">Добавить выбранных</button>`
          : `<div class="muted mt-8">Нет других собеседников из личных чатов для добавления.</div>`
      }
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
      roleBadgeHtml = `<span class="role-badge-owner">Владелец</span>`;
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

  const actionZoneHtml = isGroupOwner
    ? `
      <div style="margin-top:22px; padding-top:14px; border-top:1px solid var(--border-col);">
        <div style="font-weight:700; color:#ff8c8c;">Управление группой (только владелец)</div>
        <button type="button" class="del mt-8" style="width:100%;" onclick="deleteCurrentGroup()">Удалить группу</button>
      </div>
    `
    : `
      <div style="margin-top:22px; padding-top:14px; border-top:1px solid var(--border-col);">
        <button type="button" class="del" style="width:100%;" onclick="leaveCurrentGroup()">Выйти из группы</button>
      </div>
    `;

  body.innerHTML = `
    <div style="display:flex; align-items:center; gap:12px;">
      <div class="ava" style="width:52px; height:52px; font-size:26px;">${editGroupEmoji}</div>
      <div>
        <div style="font-size:18px; font-weight:800; display:flex; align-items:center; gap:5px;">${groupTitleHtml(g.title, activeChat.tripleBadge)}</div>
        <div class="muted">Владелец: ${nickLabelHtml(g.owner, isSystemOwnerNick(g.owner))}</div>
      </div>
    </div>

    ${ownerEditMetaHtml}
    ${ownerInviteHtml}

    <div style="font-weight:700; margin-top:18px;">Участники (${orderedMembers.length})</div>
    <div id="groupMembersList">${membersHtml}</div>
    ${actionZoneHtml}
  `;

  if (isGroupOwner) {
    renderEditGroupIconPicker();
  }
}

async function leaveCurrentGroup() {
  if (!activeChat || activeChat.type !== "group" || !me) return;
  const res = await api("/api/groups/leave", {
    actor: me.username,
    groupId: activeChat.id
  });
  if (res.ok) {
    playSound("leave");
    backToChats();
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
    playSound("click");
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
    playSound("leave");
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
    playSound("leave");
    backToChats();
  }
}

async function deleteDirectChat() {
  if (!activeChat || activeChat.type !== "direct" || !me) return;
  const res = await api("/api/chats/delete", {
    a: me.username,
    b: activeChat.username
  });
  if (res.ok) {
    playSound("leave");
    backToChats();
  }
}

function getNickCooldownInfo(userObj) {
  const now = Date.now();
  const baseTs = Number(userObj.lastNickChangeAt || userObj.createdAt || 0);
  if (!baseTs) {
    return { canChange: true, daysLeft: 0, hoursLeft: 0 };
  }
  const elapsed = now - baseTs;
  const remaining = SEVEN_DAYS_MS - elapsed;
  if (remaining <= 0) {
    return { canChange: true, daysLeft: 0, hoursLeft: 0 };
  }
  const daysLeft = Math.floor(remaining / 86400000);
  const hoursLeft = Math.max(1, Math.ceil((remaining % 86400000) / 3600000));
  return { canChange: false, daysLeft, hoursLeft };
}

function openSettings() {
  playSound("click");
  refreshNotificationUiStatus();
  renderAvatarChoices();
  applyCustom(loadCustom());
  document.getElementById("setErr").textContent = "";
  document.getElementById("setOk").textContent = "";
  const cd = getNickCooldownInfo(me);
  document.getElementById("nickHint").textContent = cd.canChange
    ? "Никнейм можно менять раз в 7 дней. Сейчас смена доступна!"
    : `Сменить никнейм можно раз в 7 дней. Осталось: ${cd.daysLeft} дн. ${cd.hoursLeft} ч.`;
  const inp = document.getElementById("newNickInput");
  inp.value = me.username;
  inp.disabled = !cd.canChange;
  document.getElementById("btnSaveNick").disabled = !cd.canChange;
  showScreen("settingsScreen");
}

function openSearch() {
  playSound("click");
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
  box.innerHTML = res.users.map(u => {
    const ava = sanitizeAvatarEmoji(u.emoji);
    return `
      <div class="chat-card" onclick="openDirectChat('${esc(u.username)}', '${esc(ava)}', ${u.isOwner ? 'true' : 'false'})">
        <div class="ava">${ava}</div>
        <div class="chat-info">
          <div class="chat-name">${nickLabelHtml(u.username, u.isOwner)}</div>
          <div class="chat-last">Нажми, чтобы открыть чат</div>
        </div>
      </div>
    `;
  }).join("");
}

async function refreshChats() {
  if (!me) return;
  const res = await api(`/api/chats?user=${encodeURIComponent(me.username)}`);
  const list = document.getElementById("chatList");
  if (!list || !res.ok) return;

  if (activeChat) {
    if (activeChat.type === "direct") {
      const matched = res.chats.find(c => c.type !== "group" && c.username === activeChat.username);
      if (matched && matched.emoji) {
        const cleanAva = sanitizeAvatarEmoji(matched.emoji);
        if (cleanAva !== activeChat.emoji) {
          activeChat.emoji = cleanAva;
          const peerAvaEl = document.getElementById("peerAva");
          if (peerAvaEl) peerAvaEl.textContent = cleanAva;
        }
      }
    } else if (activeChat.type === "group") {
      const matchedGroup = res.chats.find(c => c.type === "group" && c.groupId === activeChat.id);
      if (!matchedGroup) {
        backToChats();
        return;
      }
      activeChat.username = matchedGroup.title;
      activeChat.emoji = sanitizeAvatarEmoji(matchedGroup.emoji);
      activeChat.tripleBadge = Boolean(matchedGroup.tripleBadge);
      document.getElementById("peerAva").textContent = activeChat.emoji;
      document.getElementById("peerNick").innerHTML = groupTitleHtml(matchedGroup.title, activeChat.tripleBadge);
    }
  }

  /* Браузерные уведомления только о новых личных сообщениях (не в группах) */
  const directItems = res.chats.filter(c => c.type !== "group" && c.lastMsgId && c.lastSender && c.lastSender !== me.username);
  if (!directNotifInitDone) {
    directNotifInitDone = true;
    directItems.forEach(c => notifiedDirectMsgIds.add(String(c.lastMsgId)));
  } else {
    for (const c of directItems) {
      const mid = String(c.lastMsgId);
      if (!notifiedDirectMsgIds.has(mid)) {
        notifiedDirectMsgIds.add(mid);
        const viewingThisChat = !document.hidden && activeChat && activeChat.type === "direct" && activeChat.username === c.username;
        if (!viewingThisChat) {
          triggerSystemNotification(
            `Сообщение от @${c.username}`,
            c.last || "Новое сообщение",
            { allowInApp: true, isDirectMessage: true, peer: c.username }
          );
        }
      }
    }
  }

  if (!res.chats.length) {
    list.innerHTML = `<div class="muted mt-12">Чатов пока нет. Нажми на 🔍 в правом верхнем углу или на ✏️ внизу.</div>`;
    return;
  }
  list.innerHTML = res.chats.map(c => {
    if (c.type === "group") {
      const gAva = sanitizeAvatarEmoji(c.emoji || "🦜");
      const typingLabel = c.typingText ? `${esc(c.typingText)} печатает...` : "";
      return `
        <div class="chat-card" onclick="openGroupChat('${esc(c.groupId)}', '${esc(c.title)}', '${esc(gAva)}', ${c.tripleBadge ? 'true' : 'false'})">
          <div class="ava">${gAva}</div>
          <div class="chat-info">
            <div class="chat-name">${groupTitleHtml(c.title, c.tripleBadge)} <span class="muted" style="font-weight:500; font-size:12px;">• группа</span></div>
            <div class="chat-last ${typingLabel ? 'typing-active' : ''}">${typingLabel || esc(c.last || 'Сообщений пока нет')}</div>
          </div>
        </div>
      `;
    }
    const cleanAva = sanitizeAvatarEmoji(c.emoji);
    const mutedTag = isDirectPeerMuted(c.username) ? ` <span class="muted" style="font-size:11px;">🔇</span>` : "";
    return `
      <div class="chat-card" onclick="openDirectChat('${esc(c.username)}', '${esc(cleanAva)}', ${c.isOwner ? 'true' : 'false'})">
        <div class="ava">${cleanAva}</div>
        <div class="chat-info">
          <div class="chat-name">${nickLabelHtml(c.username, c.isOwner)}${mutedTag}</div>
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
  if (!msg || msg.from === "system") return;
  const cleanText = (msg.text || "").trim();
  const hasImg = Boolean(msg.image);
  replyTarget = {
    from: msg.from,
    text: cleanText,
    hasImage: hasImg
  };
  const bar = document.getElementById("replyBar");
  const authorEl = document.getElementById("replyBarAuthor");
  const txtEl = document.getElementById("replyBarText");
  if (bar && authorEl && txtEl) {
    authorEl.textContent = `Ответ @${replyTarget.from}`;
    if (cleanText && hasImg) {
      txtEl.textContent = `📷 ${cleanText}`;
    } else if (cleanText) {
      txtEl.textContent = cleanText;
    } else if (hasImg) {
      txtEl.textContent = "📷 Фотография";
    } else {
      txtEl.textContent = "Сообщение";
    }
    bar.classList.remove("hidden");
  }
  playSound("click");
  const inp = document.getElementById("msgInput");
  if (inp) inp.focus();
}

function openMsgActionModal(msgIndex) {
  const msg = currentRoomMsgs[msgIndex];
  if (!msg || msg.from === "system") return;
  selectedMsgForAction = msg;
  playSound("click");
  const prev = document.getElementById("msgActionPreview");
  const cleanText = (msg.text || "").trim();
  const snippet = cleanText || (msg.image ? "📷 Фотография" : "Сообщение");
  if (prev) prev.textContent = `@${msg.from}: ${snippet}`;

  const grid = document.getElementById("msgReactionChoices");
  if (grid) {
    const myReactions = msg.reactions || {};
    grid.innerHTML = REACTION_EMOJIS.map(em => {
      const users = myReactions[em] || [];
      const active = users.includes(me.username);
      return `<button type="button" class="reaction-pick-btn" style="${active ? 'outline:2px solid var(--accent); background:var(--accent-soft);' : ''}" onclick="pickReactionForSelected('${em}')">${em}</button>`;
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
    playSound("react");
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
  playSound("click");
  const cleanAva = sanitizeAvatarEmoji(emoji);
  activeChat = { type: "direct", id: username, username, emoji: cleanAva, isOwner, tripleBadge: false };
  replyTarget = null;
  roomHistoryInitialized = false;
  seenDirectMsgIds = new Set();
  clearReplyTarget();
  clearAttachment();
  lastRenderedMsgsJson = "";
  syncRoomComposerLock(false);
  const headerMain = document.getElementById("roomHeaderMain");
  if (headerMain) headerMain.classList.add("clickable-header");
  const callBtn = document.getElementById("btnCallDirect");
  if (callBtn) callBtn.classList.remove("hidden");
  document.getElementById("peerAva").textContent = cleanAva;
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
  playSound("click");
  const badgeFlag = Boolean(tripleBadge) || isTripleBadgeGroupTitle(title);
  const gAva = sanitizeAvatarEmoji(emoji || "🦜");
  activeChat = { type: "group", id: groupId, username: title, emoji: gAva, isOwner: false, tripleBadge: badgeFlag };
  replyTarget = null;
  roomHistoryInitialized = false;
  seenGroupMsgIds = new Set();
  clearReplyTarget();
  clearAttachment();
  lastRenderedMsgsJson = "";
  syncRoomComposerLock(false);
  const headerMain = document.getElementById("roomHeaderMain");
  if (headerMain) headerMain.classList.add("clickable-header");
  const callBtn = document.getElementById("btnCallDirect");
  if (callBtn) callBtn.classList.add("hidden");
  document.getElementById("peerAva").textContent = gAva;
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

function formatReplyPreviewText(rep) {
  if (!rep) return "Сообщение";
  const t = (rep.text || "").trim();
  if (t && rep.hasImage) return `📷 ${t}`;
  if (t) return t;
  if (rep.hasImage) return "📷 Фотография";
  return "Сообщение";
}

function renderMsgItem(m, index) {
  if (m.from === "system") {
    return `<div class="sys-msg">${esc(m.text || "")}</div>`;
  }
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
    ? `<div class="reply-quote"><div class="reply-quote-author">@${esc(m.replyTo.from || "пользователь")}</div><div class="reply-quote-txt">${esc(formatReplyPreviewText(m.replyTo))}</div></div>`
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
        return `<span class="reaction-chip ${reactedByMe ? 'reacted-by-me' : ''}" onclick="event.stopPropagation(); toggleMessageReaction(${JSON.stringify(m.id)}, '${em}')">${em} <span>${users.length}</span></span>`;
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

function handleIncomingSounds(msgsList, isGroup) {
  const seenSet = isGroup ? seenGroupMsgIds : seenDirectMsgIds;
  const currentIds = new Set(msgsList.map(m => String(m.id)));

  if (!roomHistoryInitialized) {
    roomHistoryInitialized = true;
    seenSet.clear();
    currentIds.forEach(id => seenSet.add(id));
    return;
  }

  let playedSound = false;
  for (const m of msgsList) {
    const mid = String(m.id);
    if (!seenSet.has(mid)) {
      seenSet.add(mid);
      if (playedSound) continue;
      if (m.from === "system") {
        const txt = String(m.text || "");
        if (m.eventKind === "join" || txt.endsWith("зашел")) {
          playSound("join");
          playedSound = true;
        } else if (m.eventKind === "leave" || txt.endsWith("вышел")) {
          playSound("leave");
          playedSound = true;
        }
      } else if (m.from && m.from !== me.username) {
        playSound("receive");
        playedSound = true;
      }
    }
  }
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
    activeChat.emoji = sanitizeAvatarEmoji(res.emoji || activeChat.emoji);
    activeChat.tripleBadge = Boolean(res.tripleBadge) || isTripleBadgeGroupTitle(activeChat.username);
    document.getElementById("peerAva").textContent = activeChat.emoji;
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
    handleIncomingSounds(currentRoomMsgs, true);

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

  if (res.peerEmoji) {
    const cleanAva = sanitizeAvatarEmoji(res.peerEmoji);
    if (cleanAva !== activeChat.emoji) {
      activeChat.emoji = cleanAva;
      const peerAvaEl = document.getElementById("peerAva");
      if (peerAvaEl) peerAvaEl.textContent = cleanAva;
    }
  }

  const statusEl = document.getElementById("peerStatus");
  if (statusEl) {
    statusEl.textContent = res.peerTyping ? "печатает..." : "";
  }

  currentRoomMsgs = res.msgs || [];
  handleIncomingSounds(currentRoomMsgs, false);

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
  playSound("click");
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
    playSound("click");
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
    playSound("send");
    inp.value = "";
    autoResizeComposer();
    clearReplyTarget();
    clearAttachment();
    inp.focus();
    await refreshMessages(true);
    await refreshChats();
    return;
  }

  playSound("send");
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
  const cleanEm = sanitizeAvatarEmoji(em);
  const res = await api("/api/emoji", { username: me.username, emoji: cleanEm });
  if (res.ok) {
    playSound("react");
    me.emoji = cleanEm;
    document.getElementById("myAva").textContent = cleanEm;
    document.getElementById("setOk").textContent = `Аватарка обновлена на ${cleanEm}`;
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
  playSound("react");
  saveLocalUser(res.user);
  document.getElementById("myNick").innerHTML = nickLabelHtml(me.username, me.isOwner);
  document.getElementById("setErr").textContent = "";
  document.getElementById("setOk").textContent = "Никнейм изменён (следующая смена через 7 дней)";
  const cd = getNickCooldownInfo(me);
  document.getElementById("nickHint").textContent = `Сменить никнейм можно раз в 7 дней. Осталось: ${cd.daysLeft} дн. ${cd.hoursLeft} ч.`;
  const inp = document.getElementById("newNickInput");
  inp.disabled = !cd.canChange;
  document.getElementById("btnSaveNick").disabled = !cd.canChange;
}

async function logout() {
  playSound("leave");
  await hangupCall(true);
  await api("/api/logout", {});
  saveLocalUser(null);
  activeChat = null;
  replyTarget = null;
  roomHistoryInitialized = false;
  clearAttachment();
  showScreen("authScreen");
}

async function deleteAccount() {
  if (!me) return;
  playSound("leave");
  await hangupCall(true);
  await api("/api/delete", { username: me.username });
  saveLocalUser(null);
  activeChat = null;
  replyTarget = null;
  roomHistoryInitialized = false;
  clearAttachment();
  showScreen("authScreen");
}

function startPolling() {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(() => {
    if (!me) return;
    refreshChats();
    if (activeChat) refreshMessages(false);
    pollCallStatus();
    pollUserAlerts();
  }, 1200);
}

function esc(s) {
  return String(s || "").replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  }[c]));
}

applyCustom(loadCustom());
renderAvatarChoices();
refreshNotificationUiStatus();

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


@app.get("/api/alerts")
def alerts_api():
    user = (request.args.get("user") or "").strip()
    if not user:
        return jsonify({"ok": False, "alerts": []})
    items = list(USER_ALERTS.get(user) or [])
    USER_ALERTS[user] = []
    return jsonify({"ok": True, "alerts": items})


@app.post("/api/call/start")
def call_start():
    cleanup_stale_calls()
    d = request.get_json(force=True) or {}
    caller = (d.get("caller") or "").strip()
    callee = (d.get("callee") or "").strip()
    if not caller or not callee or caller == callee:
        return jsonify({"ok": False, "error": "Некорректный собеседник."})

    for cid, c in ACTIVE_CALLS.items():
        if c.get("status") != "ended" and (callee in (c.get("caller"), c.get("callee")) or caller in (c.get("caller"), c.get("callee"))):
            return jsonify({"ok": False, "error": "Пользователь сейчас занят или уже в звонке."})

    caller_user = get_user(caller) or {}
    caller_emoji = sanitize_user_emoji(caller_user.get("emoji"))
    now = int(time.time() * 1000)
    cid = f"call_{uuid.uuid4().hex[:12]}"
    ACTIVE_CALLS[cid] = {
        "callId": cid,
        "caller": caller,
        "callee": callee,
        "callerEmoji": caller_emoji,
        "status": "ringing",
        "offer": None,
        "offerBy": "",
        "answer": None,
        "answerBy": "",
        "candidates": [],
        "createdAt": now,
        "updatedAt": now,
        "connectedAt": None,
    }
    return jsonify({"ok": True, "callId": cid})


@app.post("/api/call/offer")
def call_offer():
    cleanup_stale_calls()
    d = request.get_json(force=True) or {}
    cid = (d.get("callId") or "").strip()
    from_u = (d.get("from") or "").strip()
    offer = d.get("offer")
    c = ACTIVE_CALLS.get(cid)
    if not c or from_u not in (c.get("caller"), c.get("callee")):
        return jsonify({"ok": False})
    c["offer"] = offer
    c["offerBy"] = from_u
    c["updatedAt"] = int(time.time() * 1000)
    return jsonify({"ok": True})


@app.post("/api/call/answer")
def call_answer():
    cleanup_stale_calls()
    d = request.get_json(force=True) or {}
    cid = (d.get("callId") or "").strip()
    from_u = (d.get("from") or "").strip()
    answer = d.get("answer")
    c = ACTIVE_CALLS.get(cid)
    if not c or from_u not in (c.get("caller"), c.get("callee")):
        return jsonify({"ok": False})
    now = int(time.time() * 1000)
    c["answer"] = answer
    c["answerBy"] = from_u
    c["status"] = "active"
    if not c.get("connectedAt"):
        c["connectedAt"] = now
    c["updatedAt"] = now
    return jsonify({"ok": True, "connectedAt": c["connectedAt"]})


@app.post("/api/call/candidate")
def call_candidate():
    cleanup_stale_calls()
    d = request.get_json(force=True) or {}
    cid = (d.get("callId") or "").strip()
    from_u = (d.get("from") or "").strip()
    cand = d.get("candidate")
    c = ACTIVE_CALLS.get(cid)
    if not c or from_u not in (c.get("caller"), c.get("callee")) or not cand:
        return jsonify({"ok": False})
    c.setdefault("candidates", []).append({"from": from_u, "candidate": cand})
    c["updatedAt"] = int(time.time() * 1000)
    return jsonify({"ok": True})


@app.post("/api/call/end")
def call_end():
    d = request.get_json(force=True) or {}
    cid = (d.get("callId") or "").strip()
    from_u = (d.get("from") or "").strip()
    c = ACTIVE_CALLS.get(cid)
    if c and (not from_u or from_u in (c.get("caller"), c.get("callee"))):
        c["status"] = "ended"
        c["updatedAt"] = int(time.time() * 1000)
    return jsonify({"ok": True})


@app.get("/api/call/poll")
def call_poll():
    cleanup_stale_calls()
    user = (request.args.get("user") or "").strip()
    active_cid = (request.args.get("callId") or "").strip()
    if not user:
        return jsonify({"ok": False})

    if active_cid:
        c = ACTIVE_CALLS.get(active_cid)
        if not c or c.get("status") == "ended":
            return jsonify({"ok": True, "ended": True})
        c["updatedAt"] = int(time.time() * 1000)
        return jsonify({"ok": True, "call": c})

    for cid, c in ACTIVE_CALLS.items():
        if c.get("status") == "ringing" and c.get("callee") == user:
            return jsonify({"ok": True, "call": c})

    return jsonify({"ok": True, "call": None})


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
        res = [{"username": r["username"], "emoji": sanitize_user_emoji(r.get("emoji")), "isOwner": is_owner_nick(r["username"])} for r in rows]
        return jsonify({"ok": True, "users": res})

    db = load_json_db()
    res = [
        {"username": k, "emoji": sanitize_user_emoji(v.get("emoji")), "isOwner": is_owner_nick(k)}
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
                users_map = {r["username"]: sanitize_user_emoji(r.get("emoji")) for r in cur.fetchall()}

                cur.execute(
                    """
                    SELECT DISTINCT ON (chat_key) id, chat_key, sender, text, image, ts
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
                    "lastMsgId": r.get("id"),
                    "lastSender": r.get("sender"),
                    "ts": int(r.get("ts") or 0),
                })

        for gr in group_rows:
            g = format_group_row(gr)
            if u in g["members"]:
                gm = group_last_map.get(g["id"])
                if gm:
                    base_txt = gm.get("text") or ("📷 Картинка" if gm.get("image") else "Сообщений пока нет")
                    if gm.get("sender") == "system":
                        preview_text = base_txt
                    else:
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
                "emoji": sanitize_user_emoji(db["users"][peer].get("emoji")),
                "isOwner": is_owner_nick(peer),
                "typing": is_peer_typing(peer, u),
                "last": preview_text,
                "lastMsgId": last_msg.get("id") or f"d_{len(m)-1}_{last_msg.get('ts', 0)}",
                "lastSender": last_msg.get("from"),
                "ts": last_msg.get("ts", 0),
            })

    for gid, g in db["groups"].items():
        members = g.get("members") or []
        if u in members:
            g_msgs = db["group_msgs"].get(gid) or []
            if g_msgs:
                gm = g_msgs[-1]
                base_txt = gm.get("text") or ("📷 Картинка" if gm.get("image") else "Сообщений пока нет")
                if gm.get("from") == "system":
                    preview_text = base_txt
                else:
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


@app.post("/api/chats/delete")
def delete_direct_chat_api():
    d = request.get_json(force=True) or {}
    a = (d.get("a") or "").strip()
    b = (d.get("b") or "").strip()
    if not a or not b:
        return jsonify({"ok": False})
    ck = chat_key(a, b)
    if USE_PG:
        with get_pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM messages WHERE chat_key = %s", (ck,))
        return jsonify({"ok": True})
    db = load_json_db()
    db["chats"].pop(ck, None)
    save_json_db(db)
    return jsonify({"ok": True})


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
        peer_emoji = sanitize_user_emoji(peer_row.get("emoji") if peer_row else None)
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
        "peerEmoji": sanitize_user_emoji(peer_user.get("emoji")),
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
        for m_nick in members_clean:
            if m_nick != owner:
                push_user_alert(m_nick, "added_to_group", "Вас добавили в группу", f"@{owner} добавил вас в группу «{title}»")
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
    for m_nick in members_clean:
        if m_nick != owner:
            push_user_alert(m_nick, "added_to_group", "Вас добавили в группу", f"@{owner} добавил вас в группу «{title}»")
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


@app.post("/api/groups/add-members")
def group_add_members():
    d = request.get_json(force=True) or {}
    actor = (d.get("actor") or "").strip()
    gid = (d.get("groupId") or "").strip()
    raw_members = d.get("members") or []

    g = get_group_by_id(gid)
    if not g or g["owner"] != actor:
        return jsonify({"ok": False, "error": "Только владелец группы может добавлять участников."})

    members = list(g.get("members") or [])
    existing_set = set(members)
    added = []
    for m in raw_members:
        nick = str(m or "").strip()
        if nick and nick not in existing_set:
            existing_set.add(nick)
            members.append(nick)
            added.append(nick)

    if not added:
        return jsonify({"ok": False, "error": "Нет новых участников для добавления."})

    if USE_PG:
        with get_pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE groups SET members_json = %s WHERE id = %s",
                    (json.dumps(members, ensure_ascii=False), gid),
                )
    else:
        db = load_json_db()
        if gid in db["groups"]:
            db["groups"][gid]["members"] = members
            save_json_db(db)

    for nick in added:
        append_group_system_message(gid, f'"{nick}" зашел', "join")
        push_user_alert(nick, "added_to_group", "Вас добавили в группу", f"@{actor} добавил вас в группу «{g['title']}»")

    return jsonify({"ok": True, "added": added, "group": get_group_by_id(gid)})


@app.post("/api/groups/leave")
def group_leave():
    d = request.get_json(force=True) or {}
    actor = (d.get("actor") or "").strip()
    gid = (d.get("groupId") or "").strip()

    g = get_group_by_id(gid)
    if not g or actor not in (g.get("members") or []):
        return jsonify({"ok": False})
    if g["owner"] == actor:
        return jsonify({"ok": False, "error": "Владелец не может просто выйти — используйте «Удалить группу»."})

    members = [m for m in (g.get("members") or []) if m != actor]
    muted = [m for m in (g.get("muted") or []) if m != actor]

    if USE_PG:
        with get_pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE groups SET members_json = %s, muted_json = %s WHERE id = %s",
                    (json.dumps(members, ensure_ascii=False), json.dumps(muted, ensure_ascii=False), gid),
                )
    else:
        db = load_json_db()
        if gid in db["groups"]:
            db["groups"][gid]["members"] = members
            db["groups"][gid]["muted"] = muted
            save_json_db(db)

    append_group_system_message(gid, f'"{actor}" вышел', "leave")
    return jsonify({"ok": True})


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
    else:
        db = load_json_db()
        if gid in db["groups"]:
            db["groups"][gid]["members"] = members
            db["groups"][gid]["muted"] = muted
            save_json_db(db)

    append_group_system_message(gid, f'"{target}" вышел', "leave")
    push_user_alert(target, "kicked", "Вас удалили", f"Вас удалили из группы «{g['title']}»")
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
    u = (d.get("username") or "").strip()
    em = sanitize_user_emoji(d.get("emoji"))
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
    if now - last_change < SEVEN_DAYS_MS:
        rem_days = max(0, (SEVEN_DAYS_MS - (now - last_change)) // 86400000)
        return jsonify({"ok": False, "error": f"Никнейм можно менять раз в 7 дней. Подождите ещё ~{rem_days} дн."})
    if len(new_n) < 2:
        return jsonify({"ok": False, "error": "Ник слишком короткий."})

    clean_emoji = sanitize_user_emoji(user.get("emoji"))

    if USE_PG:
        with get_pg_conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT username FROM users WHERE username = %s AND username <> %s", (new_n, old_n))
                if cur.fetchone():
                    return jsonify({"ok": False, "error": "Ник уже занят!"})
                cur.execute(
                    """
                    INSERT INTO users (username, password, emoji, created_at, last_nick_change_at)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (username) DO UPDATE
                    SET password = EXCLUDED.password,
                        emoji = EXCLUDED.emoji,
                        last_nick_change_at = EXCLUDED.last_nick_change_at
                    """,
                    (new_n, user["password"], clean_emoji, created_at, now),
                )
                if new_n != old_n:
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
    if new_n != old_n and new_n in db["users"]:
        return jsonify({"ok": False, "error": "Ник уже занят!"})
    db["users"][new_n] = {**user, "username": new_n, "emoji": clean_emoji, "createdAt": created_at, "lastNickChangeAt": now}
    if new_n != old_n:
        db["users"].pop(old_n, None)
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
