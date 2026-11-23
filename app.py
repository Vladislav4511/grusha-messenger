import json
import os
import time
import uuid
from flask import Flask, request, jsonify, render_template_string, make_response, send_from_directory
from werkzeug.utils import secure_filename

try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
except ImportError:
    psycopg2 = None
    RealDictCursor = None

app = Flask(__name__)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.path.join(BASE_DIR, "db.json")
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

DATABASE_URL = (os.environ.get("DATABASE_URL") or "").strip()
USE_PG = bool(DATABASE_URL and psycopg2 is not None)

SEVEN_DAYS_MS = 7 * 24 * 60 * 60 * 1000
TYPING_TTL_MS = 4500
CALL_TTL_MS = 60_000
ONLINE_TTL_MS = 25_000
OWNER_NICK = "владислав"
PHOTO_AVATAR_ALLOWED_NICKS = {"пъдпъдък"}
TRIPLE_BADGE_GROUP_TITLE = "ырка сквад"
MAX_IMAGE_LEN = 1_500_000
MAX_AVATAR_LEN = 1_500_000
MAX_VIDEO_BYTES = 25 * 1024 * 1024  # 25 МБ
DEFAULT_GROUP_ICON = "🦜"
DEFAULT_USER_EMOJI = "🍐"
VALID_CALL_ACCESS = ("all", "owner", "nobody")

app.config["MAX_CONTENT_LENGTH"] = MAX_VIDEO_BYTES + 2 * 1024 * 1024

TYPING_STATUS = {}
ACTIVE_CALLS = {}       # direct call_id -> dict
GROUP_CALLS = {}        # group_id -> dict
ONLINE_USERS = {}       # username -> last_seen_ms


def touch_user_online(username):
    u = (username or "").strip()
    if u:
        ONLINE_USERS[u] = int(time.time() * 1000)


def is_user_online(username):
    u = (username or "").strip()
    if not u:
        return False
    last_ts = int(ONLINE_USERS.get(u, 0) or 0)
    return (int(time.time() * 1000) - last_ts) <= ONLINE_TTL_MS


def can_use_photo_avatar(username):
    return (username or "").strip().lower() in PHOTO_AVATAR_ALLOWED_NICKS


def sanitize_avatar_photo(raw_photo, username=""):
    if not can_use_photo_avatar(username):
        return ""
    if not isinstance(raw_photo, str) or not raw_photo:
        return ""
    val = raw_photo.strip()
    if len(val) > MAX_AVATAR_LEN:
        return ""
    if val.startswith(("data:image/", "/uploads/")):
        if ".." in val:
            return ""
        return val
    return ""


def sanitize_call_access(val):
    v = (val or "").strip().lower()
    if v in VALID_CALL_ACCESS:
        return v
    return "all"


def sanitize_user_emoji(em):
    val = (em or "").strip()
    if not val:
        return DEFAULT_USER_EMOJI
    if val in ("👑", "💩"):
        return "🙂"
    return val


def sanitize_video_url(raw_vid):
    if not isinstance(raw_vid, str) or not raw_vid:
        return ""
    val = raw_vid.strip()
    if val.startswith("/uploads/") and ".." not in val:
        return val
    return ""


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
                        avatar_photo TEXT NOT NULL DEFAULT '',
                        created_at BIGINT NOT NULL,
                        last_nick_change_at BIGINT NOT NULL
                    )
                """)
                cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS avatar_photo TEXT NOT NULL DEFAULT ''")
                cur.execute("UPDATE users SET emoji = '🙂' WHERE emoji IN ('👑', '💩')")

                cur.execute("""
                    CREATE TABLE IF NOT EXISTS messages (
                        id SERIAL PRIMARY KEY,
                        chat_key TEXT NOT NULL,
                        sender TEXT NOT NULL,
                        text TEXT NOT NULL DEFAULT '',
                        image TEXT NOT NULL DEFAULT '',
                        video TEXT NOT NULL DEFAULT '',
                        ts BIGINT NOT NULL,
                        reply_to_json TEXT NOT NULL DEFAULT '',
                        reactions_json TEXT NOT NULL DEFAULT '{}'
                    )
                """)
                cur.execute("ALTER TABLE messages ADD COLUMN IF NOT EXISTS video TEXT NOT NULL DEFAULT ''")
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
                        call_access TEXT NOT NULL DEFAULT 'all',
                        created_at BIGINT NOT NULL
                    )
                """)
                cur.execute("ALTER TABLE groups ADD COLUMN IF NOT EXISTS call_access TEXT NOT NULL DEFAULT 'all'")
                cur.execute("UPDATE groups SET emoji = '🙂' WHERE emoji IN ('👑', '💩')")

                cur.execute("""
                    CREATE TABLE IF NOT EXISTS group_messages (
                        id SERIAL PRIMARY KEY,
                        group_id TEXT NOT NULL,
                        sender TEXT NOT NULL,
                        text TEXT NOT NULL DEFAULT '',
                        image TEXT NOT NULL DEFAULT '',
                        video TEXT NOT NULL DEFAULT '',
                        ts BIGINT NOT NULL,
                        reply_to_json TEXT NOT NULL DEFAULT '',
                        reactions_json TEXT NOT NULL DEFAULT '{}'
                    )
                """)
                cur.execute("ALTER TABLE group_messages ADD COLUMN IF NOT EXISTS video TEXT NOT NULL DEFAULT ''")
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
                if isinstance(u_obj, dict):
                    if u_obj.get("emoji") in ("👑", "💩"):
                        u_obj["emoji"] = "🙂"
                        changed = True
                    if "avatarPhoto" not in u_obj:
                        u_obj["avatarPhoto"] = ""
                        changed = True
            for _, g_obj in db.get("groups", {}).items():
                if isinstance(g_obj, dict):
                    if g_obj.get("emoji") in ("👑", "💩"):
                        g_obj["emoji"] = "🙂"
                        changed = True
                    if "callAccess" not in g_obj:
                        g_obj["callAccess"] = "all"
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


def format_duration_mmss(sec):
    s = max(0, int(sec))
    mins = s // 60
    rem = s % 60
    return f"{mins:02d}:{rem:02d}"


def append_direct_message(sender, peer, text):
    if not sender or not peer or not text:
        return
    ts = int(time.time() * 1000)
    if USE_PG:
        with get_pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO messages (chat_key, sender, text, image, video, ts, reply_to_json, reactions_json) VALUES (%s, %s, %s, '', '', %s, '', '{}')",
                    (chat_key(sender, peer), sender, text, ts),
                )
        return
    db = load_json_db()
    msg_item = {
        "id": f"d_{uuid.uuid4().hex[:10]}",
        "from": sender,
        "text": text,
        "ts": ts,
        "reactions": {},
    }
    db["chats"].setdefault(chat_key(sender, peer), []).append(msg_item)
    save_json_db(db)


def append_group_user_message(gid, sender, text):
    if not gid or not sender or not text:
        return
    ts = int(time.time() * 1000)
    if USE_PG:
        with get_pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO group_messages (group_id, sender, text, image, video, ts, reply_to_json, reactions_json) VALUES (%s, %s, %s, '', '', %s, '', '{}')",
                    (gid, sender, text, ts),
                )
        return
    db = load_json_db()
    msg_item = {
        "id": f"g_{uuid.uuid4().hex[:10]}",
        "from": sender,
        "text": text,
        "ts": ts,
        "reactions": {},
    }
    db["group_msgs"].setdefault(gid, []).append(msg_item)
    save_json_db(db)


def cleanup_stale_calls():
    now = int(time.time() * 1000)
    expired = []
    for cid, call in ACTIVE_CALLS.items():
        updated = int(call.get("updatedAt") or call.get("createdAt") or now)
        if call.get("status") == "ended" and (now - updated) > 5000:
            expired.append(cid)
        elif call.get("status") != "ended" and (now - updated) > CALL_TTL_MS:
            expired.append(cid)
    for cid in expired:
        ACTIVE_CALLS.pop(cid, None)

    stale_groups = []
    for gid, gc in GROUP_CALLS.items():
        updated = int(gc.get("updatedAt") or gc.get("createdAt") or now)
        if not gc.get("participants") or (now - updated) > CALL_TTL_MS:
            stale_groups.append(gid)
    for gid in stale_groups:
        GROUP_CALLS.pop(gid, None)


def end_user_calls(username, now_ms=None):
    if not username:
        return
    ts = now_ms if now_ms is not None else int(time.time() * 1000)
    for cid, c in ACTIVE_CALLS.items():
        if c.get("status") != "ended" and username in (c.get("caller"), c.get("callee")):
            c["status"] = "ended"
            c["endedBy"] = username
            c["updatedAt"] = ts


def format_user(row):
    username = row["username"]
    created_at = int(row.get("createdAt", row.get("created_at", 0)) or 0)
    last_change = int(row.get("lastNickChangeAt", row.get("last_nick_change_at", created_at)) or created_at)
    raw_photo = row.get("avatarPhoto", row.get("avatar_photo", ""))
    avatar_photo = sanitize_avatar_photo(raw_photo, username)
    return {
        "username": username,
        "emoji": sanitize_user_emoji(row.get("emoji")),
        "avatarPhoto": avatar_photo,
        "canPhotoAvatar": can_use_photo_avatar(username),
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
    has_video = bool(raw.get("hasVideo"))
    if not sender and not text and not has_image and not has_video:
        return None
    return {
        "from": sender,
        "text": text,
        "hasImage": has_image,
        "hasVideo": has_video,
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
                    "INSERT INTO group_messages (group_id, sender, text, image, video, ts, reply_to_json, reactions_json) VALUES (%s, %s, %s, '', '', %s, '', '{}')",
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
    raw_access = row.get("call_access") if isinstance(row, dict) else getattr(row, "call_access", "all")
    return {
        "id": row["id"],
        "title": title,
        "emoji": normalize_group_emoji(row.get("emoji")),
        "owner": row["owner"],
        "members": members,
        "muted": muted,
        "callAccess": sanitize_call_access(raw_access),
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
        "callAccess": sanitize_call_access(g.get("callAccess")),
        "createdAt": int(g.get("createdAt") or 0),
        "tripleBadge": is_triple_badge_group(title),
    }


def can_user_call_in_group(group_obj, username):
    if not group_obj or not username:
        return False
    if username not in (group_obj.get("members") or []):
        return False
    access = sanitize_call_access(group_obj.get("callAccess"))
    if access == "nobody":
        return False
    if access == "owner":
        return group_obj.get("owner") == username
    return True


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


def format_message_record(msg_id, sender, text, image, ts, reply_to_raw=None, reactions_raw=None, video=""):
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
    if video:
        item["video"] = video
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
    --check-blue: #2196f3;
    --check-ink: #ffffff;
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
    transition: transform 0.16s ease-out, max-width 0.2s ease, border-radius 0.2s ease;
  }

  /* Режим ПК: широкий двухколоночный макет */
  .wrap.device-pc {
    max-width: 1120px;
    width: 96vw;
    left: 50%;
    right: auto;
    transform: translateX(-50%) translateY(var(--kb-lift));
    border-left: 1px solid var(--border-col);
    border-right: 1px solid var(--border-col);
    box-shadow: 0 18px 48px rgba(0, 0, 0, 0.55);
  }
  .wrap.device-pc.desktop-split-active {
    flex-direction: row;
  }
  .wrap.device-pc.desktop-split-active > #chatsScreen {
    width: 350px;
    flex: none !important;
    border-right: 1px solid var(--border-col);
    display: flex !important;
  }
  .wrap.device-pc.desktop-split-active > #roomScreen {
    flex: 1;
    display: flex !important;
  }

  /* Режим Ноутбук: широкий макет с боковой панелью */
  .wrap.device-laptop {
    max-width: 960px;
    width: 94vw;
    left: 50%;
    right: auto;
    transform: translateX(-50%) translateY(var(--kb-lift));
    border-left: 1px solid var(--border-col);
    border-right: 1px solid var(--border-col);
    box-shadow: 0 14px 38px rgba(0, 0, 0, 0.48);
  }
  .wrap.device-laptop.desktop-split-active {
    flex-direction: row;
  }
  .wrap.device-laptop.desktop-split-active > #chatsScreen {
    width: 310px;
    flex: none !important;
    border-right: 1px solid var(--border-col);
    display: flex !important;
  }
  .wrap.device-laptop.desktop-split-active > #roomScreen {
    flex: 1;
    display: flex !important;
  }

  #chatsScreen {
    position: relative;
  }

  .splash-stage {
    flex: 1;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    padding: 24px 18px;
    text-align: center;
    background: radial-gradient(circle at 50% 42%, var(--accent-soft), transparent 68%);
  }
  .splash-trio {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 22px;
    margin-bottom: 22px;
  }
  .splash-side {
    font-size: 46px;
    line-height: 1;
    filter: drop-shadow(0 6px 14px rgba(0, 0, 0, 0.4));
    animation: splashFloatSide 2s ease-in-out infinite;
  }
  .splash-side.right {
    animation-delay: 0.35s;
  }
  .splash-pear-wrap {
    width: 112px;
    height: 112px;
    border-radius: 50%;
    background: var(--card-bg);
    border: 2px solid var(--accent);
    display: grid;
    place-items: center;
    font-size: 64px;
    line-height: 1;
    box-shadow: 0 12px 30px rgba(0, 0, 0, 0.5);
    animation: splashPearPulse 1.6s ease-in-out infinite;
  }
  .splash-title {
    font-size: 23px;
    font-weight: 800;
    color: var(--accent);
    letter-spacing: 0.2px;
  }
  .splash-sub {
    margin-top: 8px;
    font-size: 13px;
    color: var(--muted-col);
  }
  @keyframes splashPearPulse {
    0%, 100% { transform: scale(1); }
    50% { transform: scale(1.06); }
  }
  @keyframes splashFloatSide {
    0%, 100% { transform: translateY(0); }
    50% { transform: translateY(-6px); }
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
  button:disabled { opacity: 0.45; cursor: not-allowed; }
  .mt-8 { margin-top: 8px; }
  .mt-12 { margin-top: 12px; }
  .err { color: #ff7b7b; font-size: 13px; margin-top: 8px; min-height: 18px; }
  .ok { color: var(--accent); font-size: 13px; margin-top: 8px; }
  .muted { color: var(--muted-col); font-size: 13px; }
  .online-pill-yes {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    color: var(--accent);
    font-weight: 800;
    font-size: 14px;
  }
  .online-pill-no {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    color: var(--muted-col);
    font-weight: 700;
    font-size: 14px;
  }
  .typing-line {
    font-size: 12px;
    color: var(--accent);
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
    background: var(--card-bg);
    border: 1px solid var(--border-col);
    margin-top: 8px;
    cursor: pointer;
  }
  .chat-card.active-peer-card {
    border-color: var(--accent);
    background: var(--accent-soft);
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
    overflow: hidden;
  }
  .ava.msg-side-ava {
    width: 30px;
    height: 30px;
    font-size: 15px;
    align-self: flex-end;
    margin-bottom: 2px;
  }
  .ava img.ava-photo-img {
    width: 100%;
    height: 100%;
    object-fit: cover;
    display: block;
  }
  .ava-picker-row {
    display: flex;
    align-items: center;
    gap: 10px;
    margin-top: 8px;
    flex-wrap: wrap;
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
    background: var(--check-blue) !important;
    color: var(--check-ink) !important;
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
    position: absolute;
    right: 18px;
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
  .call-access-row {
    display: flex;
    gap: 6px;
    margin-top: 10px;
  }
  .call-access-btn {
    flex: 1;
    padding: 9px 8px;
    font-size: 12px;
    border-radius: 10px;
    background: var(--input-bg);
    color: var(--text-col);
    border: 1px solid var(--border-col);
  }
  .call-access-btn.active-mode {
    background: var(--accent);
    color: var(--accent-contrast);
    border-color: var(--accent);
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
  .group-call-banner {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
    padding: 9px 14px;
    background: var(--accent-soft);
    border-bottom: 1px solid var(--accent);
    flex-shrink: 0;
  }
  .group-call-banner-text {
    font-size: 13px;
    font-weight: 700;
    color: var(--text-col);
    min-width: 0;
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
  .msg-row {
    display: flex;
    align-items: flex-end;
    gap: 8px;
    width: 100%;
  }
  .msg-row.row-me {
    justify-content: flex-end;
  }
  .msg-row.row-them {
    justify-content: flex-start;
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
    padding: 9px 12px 7px;
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
  .row-them .m {
    max-width: calc(82% - 38px);
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
  .msg-video-card {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 10px 12px;
    border-radius: 12px;
    background: rgba(0, 0, 0, 0.24);
    border: 1px solid rgba(255, 255, 255, 0.16);
    cursor: pointer;
    max-width: 245px;
  }
  .msg-video-card.uploading {
    cursor: default;
    opacity: 0.92;
  }
  .msg-video-play {
    width: 36px;
    height: 36px;
    border-radius: 50%;
    background: var(--accent);
    color: var(--accent-contrast);
    display: grid;
    place-items: center;
    font-size: 14px;
    font-weight: 900;
    flex-shrink: 0;
  }
  .me .msg-video-play {
    background: var(--accent-contrast);
    color: var(--accent);
  }
  .msg-video-meta {
    min-width: 0;
    flex: 1;
  }
  .msg-video-title {
    font-size: 13px;
    font-weight: 700;
    line-height: 1.2;
  }
  .msg-video-sub {
    font-size: 11px;
    opacity: 0.85;
    margin-top: 2px;
  }
  .upload-progress-track {
    width: 100%;
    height: 5px;
    border-radius: 999px;
    background: rgba(0, 0, 0, 0.28);
    overflow: hidden;
    margin-top: 6px;
  }
  .upload-progress-fill {
    height: 100%;
    background: currentColor;
    border-radius: 999px;
    transition: width 0.12s linear;
  }
  .msg-txt { margin-top: 6px; font-size: var(--msg-font-size); white-space: pre-wrap; }
  .msg-txt:first-child { margin-top: 0; }
  .msg-footer-meta {
    display: flex;
    justify-content: flex-end;
    align-items: center;
    margin-top: 4px;
    font-size: 10px;
    line-height: 1;
    opacity: 0.72;
    font-weight: 600;
  }
  .emoji-only-bubble {
    background: transparent !important;
    border-color: transparent !important;
    padding: 2px 4px !important;
    font-size: var(--emoji-size) !important;
    line-height: 1.15 !important;
  }
  .emoji-only-bubble .msg-footer-meta {
    color: var(--muted-col);
    opacity: 0.9;
    margin-top: 2px;
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
  .reaction-authors-box {
    margin-top: 10px;
    padding: 8px 10px;
    border-radius: 10px;
    background: var(--input-bg);
    border: 1px solid var(--border-col);
    font-size: 12px;
  }
  .reaction-author-line {
    display: flex;
    align-items: baseline;
    gap: 6px;
    margin-top: 4px;
    color: var(--text-col);
  }
  .reaction-author-line:first-child {
    margin-top: 0;
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
    margin-top: 8px;
  }
  .reaction-pick-btn {
    background: var(--sec-btn-bg);
    color: var(--text-col);
    padding: 7px 10px;
    font-size: 20px;
    line-height: 1;
    border-radius: 10px;
  }

  .video-viewer-backdrop {
    position: fixed;
    inset: 0;
    background: rgba(6, 9, 7, 0.94);
    display: flex;
    flex-direction: column;
    justify-content: space-between;
    padding: 16px;
    z-index: 105;
  }
  .video-viewer-top {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
  }
  .video-viewer-stage {
    flex: 1;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    margin: 12px 0;
    min-height: 0;
  }
  .video-viewer-player {
    width: 100%;
    max-height: 75dvh;
    border-radius: 16px;
    background: #000;
    outline: none;
  }

  /* Звонок по умолчанию на весь экран (телефон) и в пределах окна на ПК/ноутбуке */
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
    padding: 18px 16px max(22px, env(safe-area-inset-bottom, 22px));
    z-index: 90;
    overflow: hidden;
  }
  .wrap.device-pc .call-overlay {
    left: 50%;
    right: auto;
    top: var(--app-top);
    height: var(--app-height);
    width: 96vw;
    max-width: 1120px;
    transform: translateX(-50%);
    border-left: 1px solid var(--border-col);
    border-right: 1px solid var(--border-col);
  }
  .wrap.device-laptop .call-overlay {
    left: 50%;
    right: auto;
    top: var(--app-top);
    height: var(--app-height);
    width: 94vw;
    max-width: 960px;
    transform: translateX(-50%);
    border-left: 1px solid var(--border-col);
    border-right: 1px solid var(--border-col);
  }
  .call-top-bar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
    z-index: 3;
  }
  .call-timer-pill {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 6px 14px;
    border-radius: 999px;
    background: var(--accent-soft);
    border: 1px solid var(--accent);
    color: var(--accent);
    font-weight: 800;
    font-size: 15px;
    letter-spacing: 0.5px;
  }
  .call-badge-pill {
    padding: 5px 11px;
    border-radius: 999px;
    background: var(--card-bg);
    border: 1px solid var(--border-col);
    color: var(--text-col);
    font-size: 12px;
    font-weight: 700;
  }
  .call-stage {
    position: relative;
    flex: 1;
    margin: 12px 0;
    border-radius: 22px;
    background: var(--surface-bg);
    border: 1px solid var(--border-col);
    overflow: hidden;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
  }
  .call-direct-wrap {
    position: relative;
    width: 100%;
    height: 100%;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
  }
  .call-remote-video {
    width: 100%;
    height: 100%;
    object-fit: contain;
    background: var(--input-bg);
    transform: none;
  }
  .call-avatar-fallback {
    width: 100%;
    height: 100%;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    background: var(--card-bg);
    cursor: pointer;
  }
  .call-local-pip {
    position: absolute;
    right: 12px;
    bottom: 12px;
    width: 148px;
    height: 112px;
    border-radius: 16px;
    overflow: hidden;
    border: 2px solid var(--accent);
    background: var(--input-bg);
    z-index: 4;
    box-shadow: 0 8px 24px rgba(0, 0, 0, 0.55);
  }
  .call-local-video {
    width: 100%;
    height: 100%;
    object-fit: cover;
    transform: none;
  }
  .call-group-grid {
    width: 100%;
    height: 100%;
    padding: 12px;
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(210px, 1fr));
    grid-auto-rows: minmax(180px, 1fr);
    gap: 10px;
    overflow-y: auto;
  }
  .call-tile {
    position: relative;
    height: 100%;
    min-height: 180px;
    border-radius: 16px;
    background: var(--card-bg);
    border: 1px solid var(--border-col);
    overflow: hidden;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    padding: 10px;
    cursor: pointer;
  }
  .call-tile.cam-active {
    justify-content: flex-end;
  }
  .call-tile-video {
    position: absolute;
    inset: 0;
    width: 100%;
    height: 100%;
    object-fit: contain;
    background: var(--input-bg);
    transform: none;
  }
  .call-tile-ava {
    width: 54px;
    height: 54px;
    border-radius: 50%;
    background: var(--input-bg);
    border: 1px solid var(--border-col);
    display: grid;
    place-items: center;
    font-size: 28px;
    z-index: 1;
    overflow: hidden;
  }
  .call-tile-ava img.ava-photo-img {
    width: 100%;
    height: 100%;
    object-fit: cover;
  }
  .call-tile.cam-active .call-tile-ava {
    display: none;
  }
  .call-tile-name {
    margin-top: 8px;
    font-size: 13px;
    font-weight: 700;
    z-index: 2;
    padding: 2px 8px;
    border-radius: 999px;
    background: rgba(14, 21, 16, 0.72);
    color: var(--text-col);
    max-width: 100%;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .call-tile.cam-active .call-tile-name {
    margin-top: 0;
  }
  .call-peer-ava {
    width: 82px;
    height: 82px;
    border-radius: 50%;
    background: var(--input-bg);
    border: 1px solid var(--border-col);
    display: grid;
    place-items: center;
    font-size: 42px;
    margin: 0 auto 10px;
    overflow: hidden;
  }
  .call-peer-ava img.ava-photo-img {
    width: 100%;
    height: 100%;
    object-fit: cover;
  }
  .call-peer-name {
    font-size: 21px;
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

  .vol-modal-backdrop {
    position: fixed;
    inset: 0;
    background: rgba(0, 0, 0, 0.65);
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 18px;
    z-index: 120;
  }
  .vol-modal-card {
    width: 100%;
    max-width: 320px;
    background: var(--header-bg);
    border: 1px solid var(--accent);
    border-radius: 16px;
    padding: 16px;
    box-shadow: 0 14px 36px rgba(0, 0, 0, 0.65);
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
  .attach-video-badge {
    width: 52px;
    height: 52px;
    border-radius: 8px;
    background: var(--card-bg);
    border: 1px solid var(--border-col);
    display: grid;
    place-items: center;
    font-size: 22px;
    flex-shrink: 0;
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
  .device-cycle-btn {
    width: 100%;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
    padding: 11px 14px;
    margin-top: 8px;
    border-radius: 12px;
    background: var(--input-bg);
    color: var(--text-col);
    border: 1px solid var(--accent);
    font-size: 14px;
    font-weight: 800;
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
  <audio id="remoteAudioEl" autoplay playsinline></audio>
  <div id="groupAudioSink" class="hidden"></div>

  <div id="loadingScreen" class="splash-stage">
    <div class="splash-trio">
      <div class="splash-side left" aria-label="Какашка">💩</div>
      <div class="splash-pear-wrap" aria-label="Груша">🍐</div>
      <div class="splash-side right" aria-label="Попугай">🦜</div>
    </div>
    <div class="splash-title">Мессенджер Груша</div>
    <div class="splash-sub">Загрузка...</div>
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
        <div class="chat-name" id="myNick">user</div>
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
          <div class="room-title-text" id="peerNick">peer</div>
          <div class="typing-line" id="peerStatus"></div>
        </div>
      </div>
      <div class="top-actions">
        <button type="button" id="btnCallRoom" class="mini-btn blue-btn hidden" title="Позвонить" onclick="startRoomCall()">📞</button>
      </div>
    </div>

    <div id="groupCallBanner" class="group-call-banner hidden">
      <div class="group-call-banner-text" id="groupCallBannerText">Идёт звонок в группе</div>
      <button type="button" id="btnJoinGroupCallBanner" class="mini-btn blue-btn" onclick="joinActiveGroupCall()">Войти в звонок</button>
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
            <div class="reply-bar-title" id="replyBarAuthor">Ответ user</div>
            <div class="reply-bar-text" id="replyBarText"></div>
          </div>
          <button type="button" class="mini-btn sec" onclick="clearReplyTarget()">✕</button>
        </div>
        <div id="attachPreview" class="attach-preview hidden">
          <img id="attachThumb" class="hidden" alt="Превью">
          <div id="attachVideoBadge" class="attach-video-badge hidden">🎬</div>
          <div class="muted" id="attachName">Файл прикреплён</div>
          <button type="button" class="attach-remove" onclick="clearAttachment()">Убрать</button>
        </div>
        <div class="composer">
          <input id="videoFileInput" type="file" accept="video/*" class="hidden" onchange="onFileSelected(event)">
          <input id="photoFileInput" type="file" accept="image/*" class="hidden" onchange="onFileSelected(event)">
          <button type="button" class="clip-btn" title="Прикрепить видео или фотографию" onclick="openAttachChoiceModal()">📎</button>
          <textarea id="msgInput" rows="1" placeholder="Сообщение..." autocomplete="off" spellcheck="false" onfocus="onComposerFocus()" onblur="onComposerBlur()" oninput="onComposerInput()" onkeydown="onComposerKeydown(event)"></textarea>
          <button type="button" onclick="sendMsg()">➤</button>
        </div>
      </div>
    </div>
  </div>

  <div id="attachChoiceModal" class="msg-menu-backdrop hidden" onclick="closeAttachChoiceModal(event)">
    <div class="msg-menu-card" onclick="event.stopPropagation()">
      <div style="font-weight:800; font-size:16px; margin-bottom:6px;">Что вы выберете</div>
      <div class="muted" style="margin-bottom:12px;">Выбери формат файла для отправки</div>
      <button type="button" class="blue-btn" style="width:100%;" onclick="chooseAttachKind('video')">🎬 Видео</button>
      <button type="button" class="sec mt-8" style="width:100%;" onclick="chooseAttachKind('photo')">📷 Фотографию</button>
      <button type="button" class="sec mt-12" style="width:100%;" onclick="closeAttachChoiceModal()">Отмена</button>
    </div>
  </div>

  <div id="msgActionModal" class="msg-menu-backdrop hidden" onclick="closeMsgActionModal(event)">
    <div class="msg-menu-card" onclick="event.stopPropagation()">
      <div class="msg-menu-preview" id="msgActionPreview">Сообщение</div>
      <button type="button" class="blue-btn" style="width:100%;" onclick="startReplyToSelectedMsg()">↩️ Ответить на сообщение</button>
      <button type="button" id="btnDeleteSelectedMsg" class="del mt-8 hidden" style="width:100%;" onclick="deleteSelectedMsg()">🗑️ Удалить сообщение</button>
      <div class="muted mt-12" style="font-weight:700;">Ответить реакцией:</div>
      <div id="msgReactionChoices" class="reaction-picker-grid"></div>
      <div id="msgReactionAuthorsBox" class="reaction-authors-box hidden"></div>
      <button type="button" class="sec mt-12" style="width:100%;" onclick="closeMsgActionModal()">Отмена</button>
    </div>
  </div>

  <div id="videoViewerModal" class="video-viewer-backdrop hidden" onclick="closeVideoViewer(event)">
    <div class="video-viewer-top" onclick="event.stopPropagation()">
      <strong id="videoViewerAuthor">user</strong>
      <button type="button" class="mini-btn del" title="Закрыть видео" onclick="closeVideoViewer()">✕</button>
    </div>
    <div class="video-viewer-stage" onclick="event.stopPropagation()">
      <div class="muted" id="videoViewerStatus" style="margin-bottom:8px;">Нажмите воспроизведение для загрузки видео</div>
      <video id="videoViewerEl" class="video-viewer-player" controls playsinline preload="none"></video>
    </div>
    <div style="text-align:center;" onclick="event.stopPropagation()">
      <button type="button" class="sec" style="width:100%; max-width:320px;" onclick="closeVideoViewer()">✕ Вернуться в чат</button>
    </div>
  </div>

  <div id="callOverlay" class="call-overlay hidden">
    <div class="call-top-bar">
      <div class="call-timer-pill" id="callTimerLabel">00:00</div>
      <div class="call-badge-pill" id="callModeBadge">Звонок</div>
    </div>

    <div class="call-stage">
      <div id="callDirectStage" class="call-direct-wrap">
        <video id="remoteVideoEl" class="call-remote-video hidden" autoplay playsinline onclick="openParticipantVolumeModal(callState && callState.peer)"></video>
        <div id="callAvatarFallback" class="call-avatar-fallback" onclick="openParticipantVolumeModal(callState && callState.peer)">
          <div class="call-peer-ava" id="callPeerAva">🙂</div>
          <div class="call-peer-name" id="callPeerName">peer</div>
          <div class="call-status-text" id="callStatusText">Вызов...</div>
        </div>
        <div class="call-local-pip hidden" id="callLocalPip">
          <video id="localVideoEl" class="call-local-video" autoplay muted playsinline></video>
        </div>
      </div>

      <div id="callGroupStage" class="call-group-grid hidden"></div>
    </div>

    <div class="call-actions-row" id="callIncomingControls">
      <button type="button" class="blue-btn" onclick="acceptIncomingCall()">Принять</button>
      <button type="button" class="del" onclick="hangupCall(true)">Отклонить</button>
    </div>
    <div class="call-actions-row hidden" id="callActiveControls">
      <button type="button" id="btnToggleMic" class="sec" onclick="toggleMicMute()">🎤 Микрофон вкл</button>
      <button type="button" id="btnToggleCam" class="sec" onclick="toggleCameraDirectly()">📷 Камера выкл</button>
      <button type="button" class="del" onclick="hangupCall(true)">Выйти</button>
    </div>
  </div>

  <div id="participantVolumeModal" class="vol-modal-backdrop hidden" onclick="closeParticipantVolumeModal(event)">
    <div class="vol-modal-card" onclick="event.stopPropagation()">
      <div style="font-weight:800; font-size:16px;" id="volModalTitle">Громкость user</div>
      <div class="muted mt-8">Максимум — 50%</div>
      <div class="slider-row">
        <div class="slider-head">
          <span>Громкость голоса</span>
          <strong id="volModalValue">50%</strong>
        </div>
        <input id="volModalRange" type="range" min="0" max="50" step="1" oninput="onParticipantVolumeSliderInput()">
      </div>
      <button type="button" class="sec mt-12" style="width:100%;" onclick="closeParticipantVolumeModal()">Готово</button>
    </div>
  </div>

  <div id="settingsScreen" class="hidden" style="flex:1; display:flex; flex-direction:column; min-height:0;">
    <div class="top">
      <button class="sec" onclick="backToChats()">← Назад</button>
      <strong>Настройки</strong>
      <span style="width:52px;"></span>
    </div>
    <div class="pad">
      <div style="font-weight:700;" id="avaSectionTitle">Аватарка (только эмодзи)</div>
      <div class="ava-picker-row">
        <div class="ava" id="settingsCurrentAva">🍐</div>
        <button type="button" id="btnToggleAvaPicker" class="sec" onclick="toggleAvatarPicker()">Выбрать эмодзи</button>
        <input id="avaPhotoInput" type="file" accept="image/*" class="hidden" onchange="onAvatarPhotoSelected(event)">
        <button type="button" id="btnUploadAvaPhoto" class="blue-btn hidden" onclick="triggerAvatarPhotoUpload()">📷 Поставить фото</button>
        <button type="button" id="btnClearAvaPhoto" class="sec hidden" onclick="clearAvatarPhoto()">Убрать фото</button>
      </div>
      <div id="avaPicker" class="hidden" style="display:flex; flex-wrap:wrap; gap:8px; margin-top:8px;"></div>

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
        <div class="muted mt-8">Настрой устройство, цвет сайта и размер элементов</div>

        <div class="mt-12" style="font-size:13px; font-weight:700;">Расположение устройства (по кругу)</div>
        <button type="button" id="btnCycleDevice" class="device-cycle-btn" onclick="cycleDeviceLayout()">
          <span id="deviceLayoutLabel">📱 Телефон</span>
          <span style="font-size:12px; opacity:0.8;">Нажми для смены →</span>
        </button>

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
const SPLASH_DURATION_MS = 1600;
const MAX_VIDEO_FILE_BYTES = 25 * 1024 * 1024;
const SESSION_CHAT_KEY = "grusha_session_active_chat";
const SESSION_CALL_KEY = "grusha_session_active_call";
const PHOTO_AVATAR_NICKS = new Set(["пъдпъдък"]);

const DEVICE_CYCLE = ["phone", "pc", "laptop"];
const DEVICE_LABELS = {
  phone: "📱 Телефон",
  pc: "🖥️ ПК",
  laptop: "💻 Ноутбук"
};

const AVATAR_EMOJIS = [
  "🍐", "😎", "🐱", "🐸", "🔥", "👾",
  "⚡", "🍕", "🦊", "🐼", "🤖", "👻",
  "🦄", "🐉", "🌙", "⭐", "💎", "🎮",
  "🎧", "🚀", "🌵", "🍀", "🍓", "🙂",
  "🐄", "🐗", "🔶", "🎂", "🥕", "🧸",
  "🙀", "🥺", "🤷‍♂️", "👅"
];

const GROUP_ICONS = [
  "🦜", "🍐", "🔥", "⚡", "🎮", "🚀",
  "🍕", "🙂", "💎", "🐗", "🐄", "🔶",
  "😎", "🐱", "🐸", "👾", "🦊", "🐼",
  "🤖", "👻", "🦄", "🐉", "🌙", "⭐"
];

const REACTION_EMOJIS = [
  "😀", "😂", "❤️", "🔥", "👍", "🍐", "🎉", "😭", "💩", "🤣", "🍕"
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
  theme: "green",
  device: "phone"
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
let currentScreenId = "loadingScreen";
let currentGroupOwner = "";
let currentGroupCallAccess = "all";
let pendingImageData = "";
let pendingVideoFile = null;
let pendingVideoName = "";
let pendingUploads = [];
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

let selectedGroupEmoji = "🦜";
let selectedGroupMembers = new Set();
let editGroupEmoji = "🦜";
let inviteGroupMembers = new Set();
let avaPickerOpen = false;

let composerFocused = false;
let baselineViewportHeight = window.visualViewport ? window.visualViewport.height : window.innerHeight;
let audioCtx = null;

/* Состояние звонков, камеры и громкости участников (макс. 50%) */
let callState = null;
let pc = null;
let localStream = null;
let remoteAudioStream = null;
let remoteVideoStream = null;
let remoteCamActive = false;
let micMuted = false;
let cameraEnabled = false;
let callTimerInterval = null;
let pendingOfferToApply = null;
let pendingRemoteCandidates = [];
let sentCandidateKeys = new Set();
let appliedCandidateKeys = new Set();
let renegotiating = false;
let needsRenegotiate = false;
let endingCallInProgress = false;

let groupPeers = new Map();
let activeGroupCallInfo = null;
let groupGridRenderSignature = "";
let lastServerMsgsCache = [];
let participantVolumes = new Map();
let volumeModalTargetUser = "";
let memberProfilesMap = new Map(); // nick -> { emoji, avatarPhoto }
let directPeerOnline = false;

function clampMax50(val) {
  const n = Number(val);
  if (Number.isNaN(n)) return 50;
  return Math.max(0, Math.min(50, Math.round(n)));
}

function getParticipantVolume(username) {
  if (!username) return 50;
  if (!participantVolumes.has(username)) return 50;
  return clampMax50(participantVolumes.get(username));
}

function applyParticipantVolumeToAudio(audioEl, username) {
  if (!audioEl) return;
  const vol50 = getParticipantVolume(username);
  audioEl.volume = Math.max(0, Math.min(1, vol50 / 50));
}

function openParticipantVolumeModal(username) {
  const nick = String(username || "").trim();
  if (!nick || !me || nick === me.username) return;
  volumeModalTargetUser = nick;
  playSound("click");
  const curVol = getParticipantVolume(nick);
  const titleEl = document.getElementById("volModalTitle");
  const valEl = document.getElementById("volModalValue");
  const rngEl = document.getElementById("volModalRange");
  if (titleEl) titleEl.textContent = `Громкость ${nick}`;
  if (valEl) valEl.textContent = `${curVol}%`;
  if (rngEl) {
    rngEl.max = "50";
    rngEl.value = String(curVol);
  }
  const modal = document.getElementById("participantVolumeModal");
  if (modal) modal.classList.remove("hidden");
}

function closeParticipantVolumeModal(event) {
  if (event && event.target && event.target.id !== "participantVolumeModal") return;
  volumeModalTargetUser = "";
  const modal = document.getElementById("participantVolumeModal");
  if (modal) modal.classList.add("hidden");
}

function onParticipantVolumeSliderInput() {
  if (!volumeModalTargetUser) return;
  const rngEl = document.getElementById("volModalRange");
  const valEl = document.getElementById("volModalValue");
  const clamped = clampMax50(rngEl ? rngEl.value : 50);
  if (rngEl) rngEl.value = String(clamped);
  if (valEl) valEl.textContent = `${clamped}%`;
  participantVolumes.set(volumeModalTargetUser, clamped);

  if (callState && callState.mode === "direct" && callState.peer === volumeModalTargetUser) {
    const aud = document.getElementById("remoteAudioEl");
    applyParticipantVolumeToAudio(aud, volumeModalTargetUser);
  }
  const peerEntry = groupPeers.get(volumeModalTargetUser);
  if (peerEntry && peerEntry.audioEl) {
    applyParticipantVolumeToAudio(peerEntry.audioEl, volumeModalTargetUser);
  }
}

function canCurrentUserUsePhotoAvatar() {
  const nick = (me && me.username) ? me.username.trim().toLowerCase() : "";
  return Boolean(nick && (PHOTO_AVATAR_NICKS.has(nick) || me.canPhotoAvatar));
}

function sanitizeAvatarPhotoClient(photo, username = "") {
  const targetNick = (username || (me && me.username) || "").trim().toLowerCase();
  const allowed = targetNick && (PHOTO_AVATAR_NICKS.has(targetNick) || (me && me.username && me.username.trim().toLowerCase() === targetNick && me.canPhotoAvatar));
  if (!allowed) return "";
  const s = String(photo || "").trim();
  if (!s || s.length > 1600000) return "";
  if (s.startsWith("data:image/") || s.startsWith("/uploads/")) {
    if (s.includes("..")) return "";
    return s;
  }
  return "";
}

function renderAvatarElement(el, emoji, photo = "", username = "") {
  if (!el) return;
  const cleanPhoto = sanitizeAvatarPhotoClient(photo, username);
  if (cleanPhoto) {
    el.innerHTML = `<img class="ava-photo-img" src="${escAttr(cleanPhoto)}" alt="Аватар">`;
  } else {
    el.textContent = sanitizeAvatarEmoji(emoji);
  }
}

function avatarHtml(emoji, photo = "", username = "") {
  const cleanPhoto = sanitizeAvatarPhotoClient(photo, username);
  if (cleanPhoto) {
    return `<img class="ava-photo-img" src="${escAttr(cleanPhoto)}" alt="Аватар">`;
  }
  return sanitizeAvatarEmoji(emoji);
}

function isDesktopLayoutActive() {
  const dev = loadCustom().device || "phone";
  return dev === "pc" || dev === "laptop";
}

function persistSessionState() {
  try {
    if (activeChat && me) {
      sessionStorage.setItem(SESSION_CHAT_KEY, JSON.stringify({
        ownerNick: me.username,
        chat: activeChat
      }));
    } else {
      sessionStorage.removeItem(SESSION_CHAT_KEY);
    }
  } catch (e) {}

  try {
    if (callState && me) {
      sessionStorage.setItem(SESSION_CALL_KEY, JSON.stringify({
        ownerNick: me.username,
        mode: callState.mode,
        callId: callState.callId || "",
        peer: callState.peer || "",
        peerEmoji: callState.peerEmoji || "🙂",
        peerPhoto: callState.peerPhoto || "",
        groupId: callState.groupId || "",
        groupTitle: callState.groupTitle || "",
        isCaller: Boolean(callState.isCaller),
        status: callState.status || "active",
        connectedAt: callState.connectedAt || null,
        micMuted: Boolean(micMuted),
        cameraEnabled: Boolean(cameraEnabled)
      }));
    } else {
      sessionStorage.removeItem(SESSION_CALL_KEY);
    }
  } catch (e) {}
}

function readSavedSessionChat(nick) {
  try {
    const raw = JSON.parse(sessionStorage.getItem(SESSION_CHAT_KEY) || "null");
    if (raw && raw.ownerNick === nick && raw.chat && raw.chat.type) {
      return raw.chat;
    }
  } catch (e) {}
  return null;
}

function readSavedSessionCall(nick) {
  try {
    const raw = JSON.parse(sessionStorage.getItem(SESSION_CALL_KEY) || "null");
    if (raw && raw.ownerNick === nick && raw.mode) {
      return raw;
    }
  } catch (e) {}
  return null;
}

function syncDesktopSplitClass() {
  const root = document.getElementById("appRoot");
  if (!root) return;
  const dev = loadCustom().device || "phone";
  const isWideDevice = dev === "pc" || dev === "laptop";
  const shouldSplit = Boolean(me && isWideDevice && activeChat && (currentScreenId === "roomScreen" || currentScreenId === "chatsScreen"));
  root.classList.toggle("desktop-split-active", shouldSplit);
}

function applyDeviceLayoutClass(dev) {
  const root = document.getElementById("appRoot");
  if (!root) return;
  const mode = DEVICE_CYCLE.includes(dev) ? dev : "phone";
  root.classList.remove("device-phone", "device-pc", "device-laptop");
  root.classList.add(`device-${mode}`);
  const lbl = document.getElementById("deviceLayoutLabel");
  if (lbl) {
    lbl.textContent = DEVICE_LABELS[mode] || DEVICE_LABELS.phone;
  }
  syncDesktopSplitClass();
}

function cycleDeviceLayout() {
  const cur = loadCustom();
  const curDev = DEVICE_CYCLE.includes(cur.device) ? cur.device : "phone";
  const nextIdx = (DEVICE_CYCLE.indexOf(curDev) + 1) % DEVICE_CYCLE.length;
  const nextDev = DEVICE_CYCLE[nextIdx];
  const nextCfg = { ...cur, device: nextDev };
  try { localStorage.setItem("grusha_custom", JSON.stringify(nextCfg)); } catch (e) {}
  applyCustom(nextCfg);
  playSound("react");
  if (activeChat) {
    lastRenderedMsgsJson = "";
    refreshMessages(true);
  }
}

function openAttachChoiceModal() {
  if (!me || !activeChat) return;
  playSound("click");
  const modal = document.getElementById("attachChoiceModal");
  if (modal) modal.classList.remove("hidden");
}

function closeAttachChoiceModal(event) {
  if (event && event.target && event.target.id !== "attachChoiceModal") return;
  const modal = document.getElementById("attachChoiceModal");
  if (modal) modal.classList.add("hidden");
}

function chooseAttachKind(kind) {
  closeAttachChoiceModal();
  playSound("click");
  if (kind === "video") {
    const vInp = document.getElementById("videoFileInput");
    if (vInp) {
      vInp.value = "";
      vInp.click();
    }
  } else {
    const pInp = document.getElementById("photoFileInput");
    if (pInp) {
      pInp.value = "";
      pInp.click();
    }
  }
}

function formatMessageTime(ts) {
  const num = Number(ts);
  if (!num || Number.isNaN(num)) return "";
  const d = new Date(num);
  const hh = String(d.getHours()).padStart(2, "0");
  const mm = String(d.getMinutes()).padStart(2, "0");
  return `${hh}:${mm}`;
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
  if (isDesktopLayoutActive()) return;
  try {
    const el = document.documentElement;
    if (!document.fullscreenElement && !document.webkitFullscreenElement) {
      if (el.requestFullscreen) el.requestFullscreen().catch(() => {});
      else if (el.webkitRequestFullscreen) el.webkitRequestFullscreen();
    }
  } catch (e) {}
}

function hardResetVideoElement(vidEl) {
  if (!vidEl) return;
  try { vidEl.pause(); } catch (e) {}
  try { vidEl.srcObject = null; } catch (e) {}
  try { vidEl.removeAttribute("src"); } catch (e) {}
  try { vidEl.load(); } catch (e) {}
  vidEl.classList.add("hidden");
}

function bindStreamToVideoElement(vidEl, stream, muted = true) {
  if (!vidEl || !stream) return;
  vidEl.muted = muted;
  vidEl.playsInline = true;
  vidEl.autoplay = true;
  if (vidEl.srcObject !== stream) {
    vidEl.srcObject = stream;
  }
  vidEl.classList.remove("hidden");
  const p = vidEl.play();
  if (p && typeof p.catch === "function") {
    p.catch(() => {});
  }
}

function playAudioSafe(audioEl, stream, peerNick = "") {
  if (!audioEl) return;
  audioEl.muted = false;
  applyParticipantVolumeToAudio(audioEl, peerNick || (callState && callState.peer) || "");
  audioEl.autoplay = true;
  audioEl.playsInline = true;
  if (stream && audioEl.srcObject !== stream) {
    audioEl.srcObject = stream;
  }
  if (audioEl.srcObject && audioEl.srcObject.getAudioTracks) {
    audioEl.srcObject.getAudioTracks().forEach(t => { t.enabled = true; });
  }
  const p = audioEl.play();
  if (p && typeof p.catch === "function") {
    p.catch(() => {});
  }
}

function unlockAllAudioOutputs() {
  getAudioContext();
  const aud = document.getElementById("remoteAudioEl");
  if (aud) {
    if (!aud.srcObject && remoteAudioStream) {
      aud.srcObject = remoteAudioStream;
    }
    if (aud.srcObject) {
      playAudioSafe(aud, aud.srcObject, callState && callState.peer);
    }
  }
  groupPeers.forEach((entry, peerNick) => {
    if (entry.audioStream) {
      entry.audioStream.getAudioTracks().forEach(t => { t.enabled = true; });
    }
    if (entry.audioEl) {
      playAudioSafe(entry.audioEl, entry.audioStream || entry.stream, peerNick);
    }
  });
}

function getCameraVideoTrack() {
  if (!cameraEnabled || !localStream) return null;
  return localStream.getVideoTracks().find(t => t.readyState === "live" && t.enabled) || null;
}

function syncDirectMediaElements() {
  const aud = document.getElementById("remoteAudioEl");
  const vid = document.getElementById("remoteVideoEl");
  const fallback = document.getElementById("callAvatarFallback");
  const locVid = document.getElementById("localVideoEl");
  const locPip = document.getElementById("callLocalPip");

  if (aud) {
    if (remoteAudioStream && remoteAudioStream.getAudioTracks().length > 0) {
      playAudioSafe(aud, remoteAudioStream, callState && callState.peer);
    } else if (aud.srcObject) {
      playAudioSafe(aud, aud.srcObject, callState && callState.peer);
    }
  }

  const hasLiveRemoteVideo = Boolean(
    remoteCamActive &&
    remoteVideoStream &&
    remoteVideoStream.getVideoTracks().some(t => t.readyState === "live" && t.enabled)
  );

  if (hasLiveRemoteVideo) {
    bindStreamToVideoElement(vid, remoteVideoStream, true);
    if (fallback) fallback.classList.add("hidden");
  } else {
    hardResetVideoElement(vid);
    if (fallback) fallback.classList.remove("hidden");
  }

  const camTrack = getCameraVideoTrack();
  if (camTrack && localStream) {
    if (locPip) locPip.classList.remove("hidden");
    bindStreamToVideoElement(locVid, localStream, true);
  } else {
    if (locPip) locPip.classList.add("hidden");
    hardResetVideoElement(locVid);
  }
}

document.addEventListener("pointerdown", () => {
  unlockAllAudioOutputs();
  if (callState && callState.mode === "direct") {
    syncDirectMediaElements();
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

function getCurrentCallDurationSeconds() {
  if (!callState || !callState.connectedAt) return 0;
  return Math.max(0, Math.floor((Date.now() - callState.connectedAt) / 1000));
}

function startCallDurationTicker() {
  stopCallDurationTicker();
  updateCallDurationLabel();
  callTimerInterval = setInterval(() => {
    updateCallDurationLabel();
    persistSessionState();
  }, 1000);
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
  el.textContent = formatCallDuration(getCurrentCallDurationSeconds());
}

function setCallStatusNote(text) {
  const statusEl = document.getElementById("callStatusText");
  if (statusEl) statusEl.textContent = text;
}

function updateCamButtonUi() {
  const btn = document.getElementById("btnToggleCam");
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
}

function syncGroupTileCameraState(tile, hasCam, stream, vidEl) {
  if (!tile) return;
  tile.classList.toggle("cam-active", Boolean(hasCam));
  if (hasCam && stream) {
    bindStreamToVideoElement(vidEl, stream, true);
  } else {
    hardResetVideoElement(vidEl);
  }
}

function renderGroupCallGrid() {
  const grid = document.getElementById("callGroupStage");
  if (!grid || !callState || callState.mode !== "group") return;

  const participants = Array.isArray(callState.participants) ? callState.participants : [];
  const camTrack = getCameraVideoTrack();
  const sig = participants.map(p => `${p.username}:${p.emoji}:${p.avatarPhoto || ""}:${p.camOn ? 1 : 0}`).join("|") + `|selfCam:${camTrack ? 1 : 0}`;
  if (sig === groupGridRenderSignature && grid.children.length === participants.length) {
    participants.forEach(p => {
      const tile = grid.querySelector(`[data-peer="${CSS.escape(p.username)}"]`);
      if (!tile) return;
      const vid = tile.querySelector("video");
      if (p.username === me.username) {
        syncGroupTileCameraState(tile, Boolean(camTrack && localStream), localStream, vid);
      } else {
        const entry = groupPeers.get(p.username);
        const hasRemoteVid = Boolean(p.camOn && entry && entry.videoTrack && entry.videoTrack.readyState === "live" && !entry.camOff);
        syncGroupTileCameraState(tile, hasRemoteVid, entry ? entry.videoStream : null, vid);
      }
    });
    return;
  }

  groupGridRenderSignature = sig;
  grid.innerHTML = participants.map(p => {
    const isSelf = p.username === me.username;
    const avaInner = avatarHtml(p.emoji || "🙂", p.avatarPhoto || "", p.username || "");
    const label = isSelf ? `${esc(p.username)} (вы)` : `${esc(p.username)}`;
    const clickAttr = isSelf ? "" : `onclick="openParticipantVolumeModal('${esc(p.username)}')"`;
    return `
      <div class="call-tile" data-peer="${esc(p.username)}" ${clickAttr}>
        <video class="call-tile-video hidden" autoplay playsinline muted></video>
        <div class="call-tile-ava">${avaInner}</div>
        <div class="call-tile-name">${label}</div>
      </div>
    `;
  }).join("");

  participants.forEach(p => {
    const tile = grid.querySelector(`[data-peer="${CSS.escape(p.username)}"]`);
    if (!tile) return;
    const vid = tile.querySelector("video");
    if (p.username === me.username) {
      syncGroupTileCameraState(tile, Boolean(camTrack && localStream), localStream, vid);
    } else {
      const entry = groupPeers.get(p.username);
      const hasRemoteVid = Boolean(p.camOn && entry && entry.videoTrack && entry.videoTrack.readyState === "live" && !entry.camOff);
      syncGroupTileCameraState(tile, hasRemoteVid, entry ? entry.videoStream : null, vid);
    }
  });
}

function showCallModalUi() {
  const overlay = document.getElementById("callOverlay");
  if (!overlay || !callState) return;
  overlay.classList.remove("hidden");

  const directStage = document.getElementById("callDirectStage");
  const groupStage = document.getElementById("callGroupStage");
  const modeBadge = document.getElementById("callModeBadge");
  const incBox = document.getElementById("callIncomingControls");
  const actBox = document.getElementById("callActiveControls");

  if (callState.mode === "group") {
    if (directStage) directStage.classList.add("hidden");
    if (groupStage) groupStage.classList.remove("hidden");
    if (incBox) incBox.classList.add("hidden");
    if (actBox) actBox.classList.remove("hidden");
    if (modeBadge) modeBadge.textContent = `Группа: ${callState.groupTitle || "Звонок"}`;
    renderGroupCallGrid();
  } else {
    if (directStage) directStage.classList.remove("hidden");
    if (groupStage) groupStage.classList.add("hidden");
    if (modeBadge) modeBadge.textContent = `${callState.peer}`;
    renderAvatarElement(
      document.getElementById("callPeerAva"),
      callState.peerEmoji || "🙂",
      callState.peerPhoto || "",
      callState.peer || ""
    );
    document.getElementById("callPeerName").textContent = `${callState.peer}`;

    if (callState.status === "ringing" && !callState.isCaller) {
      incBox.classList.remove("hidden");
      actBox.classList.add("hidden");
      setCallStatusNote(`Входящий звонок от ${callState.peer}`);
    } else if (callState.status === "ringing" && callState.isCaller) {
      incBox.classList.add("hidden");
      actBox.classList.remove("hidden");
      setCallStatusNote(`Звоним ${callState.peer}...`);
    } else {
      incBox.classList.add("hidden");
      actBox.classList.remove("hidden");
      if (!callState.iceNote) {
        setCallStatusNote("В звонке");
      }
    }
    syncDirectMediaElements();
  }

  updateCamButtonUi();
  updateCallDurationLabel();
  persistSessionState();
}

function hideCallModalUi() {
  const overlay = document.getElementById("callOverlay");
  if (overlay) overlay.classList.add("hidden");
  closeParticipantVolumeModal();
}

function cleanupGroupPeers() {
  groupPeers.forEach((entry) => {
    try {
      if (entry.pc) {
        entry.pc.ontrack = null;
        entry.pc.onicecandidate = null;
        entry.pc.onnegotiationneeded = null;
        entry.pc.close();
      }
    } catch (e) {}
    try {
      if (entry.audioEl) {
        entry.audioEl.pause();
        entry.audioEl.srcObject = null;
        entry.audioEl.remove();
      }
    } catch (e) {}
  });
  groupPeers.clear();
  groupGridRenderSignature = "";
  const sink = document.getElementById("groupAudioSink");
  if (sink) sink.innerHTML = "";
}

function forceResetCallUi() {
  stopCallDurationTicker();
  cleanupGroupPeers();

  if (pc) {
    try {
      pc.ontrack = null;
      pc.onicecandidate = null;
      pc.oniceconnectionstatechange = null;
      pc.close();
    } catch (e) {}
    pc = null;
  }
  if (localStream) {
    try {
      localStream.getTracks().forEach(t => {
        t.onended = null;
        try { t.stop(); } catch (e) {}
      });
    } catch (e) {}
    localStream = null;
  }
  if (remoteAudioStream) {
    try {
      remoteAudioStream.getTracks().forEach(t => {
        try { t.stop(); } catch (e) {}
      });
    } catch (e) {}
    remoteAudioStream = null;
  }
  if (remoteVideoStream) {
    try {
      remoteVideoStream.getTracks().forEach(t => {
        try { t.stop(); } catch (e) {}
      });
    } catch (e) {}
    remoteVideoStream = null;
  }
  remoteCamActive = false;

  const aud = document.getElementById("remoteAudioEl");
  if (aud) {
    try { aud.pause(); } catch (e) {}
    aud.srcObject = null;
  }
  hardResetVideoElement(document.getElementById("remoteVideoEl"));
  hardResetVideoElement(document.getElementById("localVideoEl"));
  const locPip = document.getElementById("callLocalPip");
  if (locPip) locPip.classList.add("hidden");
  const fallback = document.getElementById("callAvatarFallback");
  if (fallback) fallback.classList.remove("hidden");

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
  hideCallModalUi();
  persistSessionState();
}

async function ensureLocalAudioStream() {
  getAudioContext();
  if (localStream && localStream.getAudioTracks().length > 0) {
    localStream.getAudioTracks().forEach(t => {
      t.enabled = !micMuted;
      if (t.readyState === "ended") {
        localStream.removeTrack(t);
      }
    });
    if (localStream.getAudioTracks().length > 0) {
      return localStream;
    }
  }
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    throw new Error("Браузер не поддерживает доступ к микрофону");
  }
  let audioOnly = null;
  try {
    audioOnly = await navigator.mediaDevices.getUserMedia({
      audio: {
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true
      },
      video: false
    });
  } catch (e) {
    audioOnly = await navigator.mediaDevices.getUserMedia({ audio: true, video: false });
  }
  audioOnly.getAudioTracks().forEach(t => { t.enabled = !micMuted; });
  if (!localStream) {
    localStream = audioOnly;
  } else {
    audioOnly.getAudioTracks().forEach(t => localStream.addTrack(t));
  }
  return localStream;
}

async function acquireCameraTrackWithFallback() {
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    throw new Error("getUserMedia unavailable");
  }
  const constraintsList = [
    { video: { width: { ideal: 640 }, height: { ideal: 480 } }, audio: false },
    { video: { facingMode: "user" }, audio: false },
    { video: true, audio: false }
  ];
  let lastErr = null;
  for (const c of constraintsList) {
    try {
      const s = await navigator.mediaDevices.getUserMedia(c);
      const track = s.getVideoTracks()[0];
      if (track) return { stream: s, track };
      s.getTracks().forEach(t => { try { t.stop(); } catch (e) {} });
    } catch (e) {
      lastErr = e;
    }
  }
  throw lastErr || new Error("Camera unavailable");
}

async function flushRenegotiateIfReady() {
  if (!pc || !callState || callState.mode !== "direct") return;
  if (!needsRenegotiate || renegotiating) return;
  if (pc.signalingState !== "stable") return;

  renegotiating = true;
  needsRenegotiate = false;
  try {
    const offer = await pc.createOffer({ offerToReceiveAudio: true, offerToReceiveVideo: true });
    if (!pc || !callState || pc.signalingState !== "stable") return;
    await pc.setLocalDescription(offer);
    callState.lastAppliedOfferJson = JSON.stringify(pc.localDescription);
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

function stopLocalVideoTracksCompletely() {
  if (localStream) {
    localStream.getVideoTracks().forEach(t => {
      t.onended = null;
      try { t.enabled = false; } catch (e) {}
      try { t.stop(); } catch (e) {}
      localStream.removeTrack(t);
    });
  }
}

function stopRemoteVideoTracksCompletely() {
  remoteCamActive = false;
  if (remoteVideoStream) {
    remoteVideoStream.getVideoTracks().forEach(t => {
      try { t.enabled = false; } catch (e) {}
      try { t.stop(); } catch (e) {}
      remoteVideoStream.removeTrack(t);
    });
  }
  hardResetVideoElement(document.getElementById("remoteVideoEl"));
  const fallback = document.getElementById("callAvatarFallback");
  if (fallback) fallback.classList.remove("hidden");
}

function applyRemoteCameraState(isOn) {
  remoteCamActive = Boolean(isOn);
  if (!remoteCamActive) {
    stopRemoteVideoTracksCompletely();
  }
  syncDirectMediaElements();
}

async function pushCameraTrackToPeers(vTrack, isOn) {
  if (!callState) return;
  if (callState.mode === "group") {
    for (const [peerNick, entry] of groupPeers.entries()) {
      const sender = entry.pc.getSenders().find(s => s.track && s.track.kind === "video");
      if (sender) {
        await sender.replaceTrack(isOn ? vTrack : null).catch(() => {});
      } else if (isOn && vTrack && localStream) {
        entry.pc.addTrack(vTrack, localStream);
      }
      if (!isOn) {
        if (entry.videoTrack) {
          try { entry.videoTrack.stop(); } catch (e) {}
          entry.videoTrack = null;
        }
        if (entry.videoStream) {
          entry.videoStream.getVideoTracks().forEach(t => entry.videoStream.removeTrack(t));
        }
      }
      await sendGroupSignal(callState.groupId, me.username, peerNick, "cam-state", { camOn: Boolean(isOn) }).catch(() => {});
      if (isOn && entry.pc.signalingState === "stable") {
        try {
          const offer = await entry.pc.createOffer({ offerToReceiveAudio: true, offerToReceiveVideo: true });
          await entry.pc.setLocalDescription(offer);
          await sendGroupSignal(callState.groupId, me.username, peerNick, "offer", entry.pc.localDescription);
        } catch (e) {}
      }
    }
    await api("/api/group-call/media", {
      groupId: callState.groupId,
      user: me.username,
      camOn: Boolean(isOn)
    }).catch(() => {});
  } else if (pc) {
    const videoTransceiver = pc.getTransceivers().find(tr => tr.receiver && tr.receiver.track && tr.receiver.track.kind === "video");
    if (videoTransceiver && videoTransceiver.sender) {
      if (isOn) videoTransceiver.direction = "sendrecv";
      await videoTransceiver.sender.replaceTrack(isOn ? vTrack : null).catch(() => {});
    } else {
      const sender = pc.getSenders().find(s => s.track && s.track.kind === "video");
      if (sender) {
        await sender.replaceTrack(isOn ? vTrack : null).catch(() => {});
      } else if (isOn && vTrack && localStream) {
        pc.addTrack(vTrack, localStream);
      }
    }
    await api("/api/call/cam-state", {
      callId: callState.callId,
      from: me.username,
      camOn: Boolean(isOn)
    }).catch(() => {});
    scheduleRenegotiate();
  }
}

function disableCameraLocallyAndSync() {
  stopLocalVideoTracksCompletely();
  cameraEnabled = false;
  pushCameraTrackToPeers(null, false).catch(() => {});
  updateCamButtonUi();
  persistSessionState();
  if (callState && callState.mode === "group") {
    groupGridRenderSignature = "";
    renderGroupCallGrid();
  } else {
    syncDirectMediaElements();
  }
}

async function toggleCameraDirectly() {
  if (!callState) return;
  playSound("click");

  if (cameraEnabled) {
    disableCameraLocallyAndSync();
    return;
  }

  try {
    const { track: vTrack } = await acquireCameraTrackWithFallback();
    if (!vTrack || !callState) return;

    vTrack.enabled = true;
    vTrack.onended = () => {
      if (cameraEnabled) {
        disableCameraLocallyAndSync();
      }
    };

    stopLocalVideoTracksCompletely();
    if (!localStream) {
      localStream = new MediaStream([vTrack]);
    } else {
      localStream.addTrack(vTrack);
    }

    cameraEnabled = true;
    await pushCameraTrackToPeers(vTrack, true);
    updateCamButtonUi();
    persistSessionState();
    if (callState.mode === "group") {
      groupGridRenderSignature = "";
      renderGroupCallGrid();
    } else {
      syncDirectMediaElements();
    }
  } catch (err) {
    alert("Не удалось включить камеру. Проверь подключение камеры и разрешение в браузере.");
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
  if (!track || !callState) return;
  if (track.kind === "audio") {
    if (!remoteAudioStream) {
      remoteAudioStream = new MediaStream();
    }
    remoteAudioStream.getAudioTracks().filter(t => t.id !== track.id).forEach(t => remoteAudioStream.removeTrack(t));
    if (!remoteAudioStream.getAudioTracks().some(t => t.id === track.id)) {
      remoteAudioStream.addTrack(track);
    }
    track.enabled = true;
    const aud = document.getElementById("remoteAudioEl");
    if (aud) {
      playAudioSafe(aud, remoteAudioStream, callState.peer);
    }
    track.onunmute = () => {
      track.enabled = true;
      const a = document.getElementById("remoteAudioEl");
      if (a) playAudioSafe(a, remoteAudioStream, callState && callState.peer);
    };
    unlockAllAudioOutputs();
    return;
  }

  if (track.kind === "video") {
    if (!remoteVideoStream) {
      remoteVideoStream = new MediaStream();
    }
    remoteVideoStream.getVideoTracks().filter(t => t.id !== track.id).forEach(t => remoteVideoStream.removeTrack(t));
    if (!remoteVideoStream.getVideoTracks().some(t => t.id === track.id)) {
      remoteVideoStream.addTrack(track);
    }
    remoteCamActive = true;
    track.onunmute = () => {
      remoteCamActive = true;
      syncDirectMediaElements();
    };
    track.onmute = () => {
      applyRemoteCameraState(false);
    };
    track.onended = () => {
      applyRemoteCameraState(false);
    };
    syncDirectMediaElements();
  }
}

function createPeerConnectionForCall(cid) {
  const conn = new RTCPeerConnection(RTC_CONFIG);
  remoteAudioStream = new MediaStream();
  remoteVideoStream = new MediaStream();
  remoteCamActive = false;

  try {
    conn.addTransceiver("audio", { direction: "sendrecv" });
    conn.addTransceiver("video", { direction: "sendrecv" });
  } catch (e) {}

  const aud = document.getElementById("remoteAudioEl");
  if (aud) {
    aud.srcObject = remoteAudioStream;
    aud.muted = false;
    applyParticipantVolumeToAudio(aud, callState && callState.peer);
    aud.autoplay = true;
    aud.playsInline = true;
  }

  if (localStream) {
    localStream.getTracks().forEach(track => {
      if (track.kind === "video" && !cameraEnabled) return;
      if (track.kind === "audio") {
        track.enabled = !micMuted;
      }
      const existingSender = conn.getSenders().find(s => s.track && s.track.kind === track.kind);
      if (existingSender) {
        existingSender.replaceTrack(track).catch(() => {});
      } else {
        conn.addTrack(track, localStream);
      }
    });
  }

  conn.ontrack = (event) => {
    if (!callState || callState.callId !== cid) return;
    if (event.track) {
      attachTrackToRemoteStream(event.track);
    }
    if (event.streams && event.streams[0]) {
      event.streams[0].getTracks().forEach(t => attachTrackToRemoteStream(t));
    }
    syncDirectMediaElements();
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
      unlockAllAudioOutputs();
      syncDirectMediaElements();
    } else if (st === "checking") {
      callState.iceNote = "Установка соединения...";
      setCallStatusNote("Установка соединения...");
    } else if (st === "failed") {
      callState.iceNote = "Переподключение сети...";
      setCallStatusNote("Переподключение сети...");
      try {
        if (typeof conn.restartIce === "function") {
          conn.restartIce();
          scheduleRenegotiate();
        }
      } catch (e) {}
    }
  };

  return conn;
}

/* Групповой звонок (Mesh WebRTC) */
async function sendGroupSignal(groupId, fromUser, toUser, kind, payload) {
  await api("/api/group-call/signal", {
    groupId,
    from: fromUser,
    to: toUser,
    kind,
    payload
  });
}

function ensureGroupPeer(peerNick, shouldCreateOffer) {
  if (groupPeers.has(peerNick)) {
    return groupPeers.get(peerNick);
  }
  const peerConn = new RTCPeerConnection(RTC_CONFIG);
  const audioStream = new MediaStream();
  const videoStream = new MediaStream();
  const sink = document.getElementById("groupAudioSink");
  const audioEl = document.createElement("audio");
  audioEl.autoplay = true;
  audioEl.playsInline = true;
  audioEl.muted = false;
  applyParticipantVolumeToAudio(audioEl, peerNick);
  audioEl.srcObject = audioStream;
  if (sink) sink.appendChild(audioEl);

  const entry = {
    pc: peerConn,
    audioStream,
    videoStream,
    audioEl,
    videoTrack: null,
    pendingCands: [],
    appliedCands: new Set(),
    sentCands: new Set(),
    makingOffer: false,
    camOff: false
  };
  groupPeers.set(peerNick, entry);

  try {
    peerConn.addTransceiver("audio", { direction: "sendrecv" });
    peerConn.addTransceiver("video", { direction: "sendrecv" });
  } catch (e) {}

  const camTrack = getCameraVideoTrack();

  if (localStream) {
    localStream.getAudioTracks().forEach(track => {
      track.enabled = !micMuted;
      const existingSender = peerConn.getSenders().find(s => s.track && s.track.kind === "audio");
      if (existingSender) {
        existingSender.replaceTrack(track).catch(() => {});
      } else {
        peerConn.addTrack(track, localStream);
      }
    });
  }
  if (camTrack && localStream) {
    const existingVid = peerConn.getSenders().find(s => s.track && s.track.kind === "video");
    if (existingVid) {
      existingVid.replaceTrack(camTrack).catch(() => {});
    } else {
      peerConn.addTrack(camTrack, localStream);
    }
  }

  peerConn.ontrack = (event) => {
    const tr = event.track;
    if (!tr) return;
    if (tr.kind === "audio") {
      audioStream.getAudioTracks().filter(t => t.id !== tr.id).forEach(t => audioStream.removeTrack(t));
      audioStream.addTrack(tr);
      tr.enabled = true;
      playAudioSafe(audioEl, audioStream, peerNick);
    } else if (tr.kind === "video") {
      videoStream.getVideoTracks().filter(t => t.id !== tr.id).forEach(t => videoStream.removeTrack(t));
      videoStream.addTrack(tr);
      entry.videoTrack = tr;
      entry.camOff = false;
      tr.onmute = () => {
        entry.camOff = true;
        groupGridRenderSignature = "";
        renderGroupCallGrid();
      };
      tr.onended = () => {
        entry.videoTrack = null;
        entry.camOff = true;
        groupGridRenderSignature = "";
        renderGroupCallGrid();
      };
      groupGridRenderSignature = "";
      renderGroupCallGrid();
    }
  };

  peerConn.onicecandidate = (event) => {
    if (!event.candidate || !callState || callState.mode !== "group") return;
    const candJson = event.candidate.toJSON ? event.candidate.toJSON() : event.candidate;
    const key = JSON.stringify(candJson);
    if (entry.sentCands.has(key)) return;
    entry.sentCands.add(key);
    sendGroupSignal(callState.groupId, me.username, peerNick, "candidate", candJson).catch(() => {});
  };

  if (shouldCreateOffer) {
    (async () => {
      try {
        entry.makingOffer = true;
        const offer = await peerConn.createOffer({ offerToReceiveAudio: true, offerToReceiveVideo: true });
        if (!callState || callState.mode !== "group") return;
        await peerConn.setLocalDescription(offer);
        if (callState && callState.mode === "group") {
          await sendGroupSignal(callState.groupId, me.username, peerNick, "offer", peerConn.localDescription);
        }
      } catch (e) {} finally {
        entry.makingOffer = false;
      }
    })();
  }

  return entry;
}

async function handleGroupSignalItem(sigItem) {
  if (!callState || callState.mode !== "group" || !sigItem) return;
  const peerNick = sigItem.from;
  if (!peerNick || peerNick === me.username) return;

  const shouldOffer = me.username.localeCompare(peerNick) < 0;
  const entry = ensureGroupPeer(peerNick, false);

  if (sigItem.kind === "cam-state" && sigItem.payload) {
    const isCamOn = Boolean(sigItem.payload.camOn);
    if (!isCamOn) {
      entry.camOff = true;
      if (entry.videoTrack) {
        try { entry.videoTrack.stop(); } catch (e) {}
        entry.videoTrack = null;
      }
      if (entry.videoStream) {
        entry.videoStream.getVideoTracks().forEach(t => entry.videoStream.removeTrack(t));
      }
    } else {
      entry.camOff = false;
    }
    groupGridRenderSignature = "";
    renderGroupCallGrid();
    return;
  }

  if (sigItem.kind === "offer" && sigItem.payload) {
    const offerDesc = new RTCSessionDescription(sigItem.payload);
    const offerCollision = entry.makingOffer || entry.pc.signalingState !== "stable";
    const polite = !shouldOffer;
    if (offerCollision && !polite) {
      return;
    }
    try {
      if (offerCollision && polite) {
        await entry.pc.setLocalDescription({ type: "rollback" }).catch(() => {});
      }
      await entry.pc.setRemoteDescription(offerDesc);
      for (const c of entry.pendingCands) {
        await entry.pc.addIceCandidate(new RTCIceCandidate(c)).catch(() => {});
      }
      entry.pendingCands = [];
      const ans = await entry.pc.createAnswer({ offerToReceiveAudio: true, offerToReceiveVideo: true });
      await entry.pc.setLocalDescription(ans);
      await sendGroupSignal(callState.groupId, me.username, peerNick, "answer", entry.pc.localDescription);
      playAudioSafe(entry.audioEl, entry.audioStream, peerNick);
    } catch (e) {}
  } else if (sigItem.kind === "answer" && sigItem.payload) {
    if (entry.pc.signalingState === "have-local-offer") {
      try {
        await entry.pc.setRemoteDescription(new RTCSessionDescription(sigItem.payload));
        for (const c of entry.pendingCands) {
          await entry.pc.addIceCandidate(new RTCIceCandidate(c)).catch(() => {});
        }
        entry.pendingCands = [];
        playAudioSafe(entry.audioEl, entry.audioStream, peerNick);
      } catch (e) {}
    }
  } else if (sigItem.kind === "candidate" && sigItem.payload) {
    const key = JSON.stringify(sigItem.payload);
    if (entry.appliedCands.has(key)) return;
    entry.appliedCands.add(key);
    if (entry.pc.remoteDescription) {
      await entry.pc.addIceCandidate(new RTCIceCandidate(sigItem.payload)).catch(() => {});
    } else {
      entry.pendingCands.push(sigItem.payload);
    }
  }
}

async function restoreSessionCallIfAny() {
  if (!me || callState) return;
  const saved = readSavedSessionCall(me.username);
  if (!saved) return;

  micMuted = Boolean(saved.micMuted);
  cameraEnabled = false;
  const micBtn = document.getElementById("btnToggleMic");
  if (micBtn && micMuted) {
    micBtn.textContent = "🔇 Микрофон выкл";
    micBtn.classList.remove("sec");
    micBtn.classList.add("warn");
  }

  if (saved.mode === "group" && saved.groupId) {
    const gRes = await api(`/api/group-call/poll?groupId=${encodeURIComponent(saved.groupId)}&user=${encodeURIComponent(me.username)}`);
    if (!gRes || !gRes.ok || !gRes.call || !Array.isArray(gRes.call.participants) || !gRes.call.participants.length) {
      persistSessionState();
      return;
    }
    const joinRes = await api("/api/group-call/join", {
      groupId: saved.groupId,
      user: me.username,
      emoji: sanitizeAvatarEmoji(me.emoji),
      avatarPhoto: sanitizeAvatarPhotoClient(me.avatarPhoto, me.username),
      camOn: false
    });
    if (!joinRes || !joinRes.ok) {
      persistSessionState();
      return;
    }
    callState = {
      mode: "group",
      groupId: saved.groupId,
      groupTitle: saved.groupTitle || (activeChat && activeChat.id === saved.groupId ? activeChat.username : "Группа"),
      status: "active",
      connectedAt: joinRes.startedAt ? Number(joinRes.startedAt) : (saved.connectedAt || Date.now()),
      participants: joinRes.participants || []
    };
    showCallModalUi();
    startCallDurationTicker();
    try {
      await ensureLocalAudioStream();
    } catch (e) {}
    (callState.participants || []).forEach(p => {
      if (p.username && p.username !== me.username) {
        ensureGroupPeer(p.username, me.username.localeCompare(p.username) < 0);
      }
    });
    return;
  }

  if (saved.mode === "direct" && saved.callId) {
    const cRes = await api(`/api/call/poll?user=${encodeURIComponent(me.username)}&callId=${encodeURIComponent(saved.callId)}`);
    if (!cRes || !cRes.ok || cRes.ended || !cRes.call || cRes.call.status === "ended") {
      persistSessionState();
      return;
    }
    const c = cRes.call;
    const peerNick = saved.peer || (me.username === c.caller ? c.callee : c.caller);
    callState = {
      mode: "direct",
      callId: c.callId,
      peer: peerNick,
      peerEmoji: sanitizeAvatarEmoji(saved.peerEmoji || c.callerEmoji || "🙂"),
      peerPhoto: sanitizeAvatarPhotoClient(saved.peerPhoto || c.callerAvatarPhoto || "", peerNick),
      isCaller: me.username === c.caller,
      status: c.status || saved.status || "active",
      connectedAt: c.connectedAt ? Number(c.connectedAt) : (saved.connectedAt || Date.now()),
      iceNote: "Восстановление связи...",
      lastAppliedOfferJson: "",
      lastAppliedAnswerJson: ""
    };
    showCallModalUi();
    if (callState.status === "active" || callState.connectedAt) {
      startCallDurationTicker();
    }
    try {
      await ensureLocalAudioStream();
      pc = createPeerConnectionForCall(c.callId);
      if (c.offer && !callState.isCaller) {
        await pc.setRemoteDescription(new RTCSessionDescription(c.offer));
        callState.lastAppliedOfferJson = JSON.stringify(c.offer);
        const ans = await pc.createAnswer({ offerToReceiveAudio: true, offerToReceiveVideo: true });
        await pc.setLocalDescription(ans);
        callState.lastAppliedAnswerJson = JSON.stringify(pc.localDescription);
        await api("/api/call/answer", { callId: c.callId, from: me.username, answer: pc.localDescription });
      } else if (callState.isCaller) {
        const offer = await pc.createOffer({ offerToReceiveAudio: true, offerToReceiveVideo: true });
        await pc.setLocalDescription(offer);
        callState.lastAppliedOfferJson = JSON.stringify(pc.localDescription);
        await api("/api/call/offer", { callId: c.callId, from: me.username, offer: pc.localDescription });
      }
    } catch (e) {}
  }
}

function canCallInCurrentGroup() {
  if (!activeChat || activeChat.type !== "group" || !me) return false;
  if (currentGroupCallAccess === "nobody") return false;
  if (currentGroupCallAccess === "owner") return currentGroupOwner === me.username;
  return true;
}

function syncGroupCallPermissionUi() {
  const btnCall = document.getElementById("btnCallRoom");
  const btnJoinBanner = document.getElementById("btnJoinGroupCallBanner");
  if (!activeChat || activeChat.type !== "group") return;
  const allowed = canCallInCurrentGroup();
  if (btnCall) {
    btnCall.classList.remove("hidden");
    btnCall.disabled = !allowed;
    btnCall.title = allowed
      ? "Позвонить в группу"
      : (currentGroupCallAccess === "nobody" ? "Звонки в группе отключены" : "Звонить может только владелец группы");
  }
  if (btnJoinBanner) {
    btnJoinBanner.disabled = !allowed;
  }
}

async function startOrJoinGroupCall(groupId, groupTitle) {
  if (!me || !groupId || callState) return;
  if (!canCallInCurrentGroup()) {
    alert(
      currentGroupCallAccess === "nobody"
        ? "В этой группе звонки отключены настройками."
        : "Начинать или входить в звонок в этой группе может только владелец."
    );
    return;
  }

  playSound("join");
  requestPageFullscreen();
  cameraEnabled = false;
  micMuted = false;

  try {
    await ensureLocalAudioStream();
  } catch (err) {
    alert("Не удалось включить микрофон. Проверь разрешение микрофона и HTTPS.");
    return;
  }

  const res = await api("/api/group-call/join", {
    groupId,
    user: me.username,
    emoji: sanitizeAvatarEmoji(me.emoji),
    avatarPhoto: sanitizeAvatarPhotoClient(me.avatarPhoto, me.username),
    camOn: false
  });
  if (!res.ok) {
    forceResetCallUi();
    alert(res.error || "Не удалось войти в групповой звонок");
    return;
  }

  callState = {
    mode: "group",
    groupId,
    groupTitle: groupTitle || activeChat?.username || "Группа",
    status: "active",
    connectedAt: res.startedAt ? Number(res.startedAt) : Date.now(),
    participants: res.participants || []
  };

  showCallModalUi();
  startCallDurationTicker();
  unlockAllAudioOutputs();

  (callState.participants || []).forEach(p => {
    if (p.username && p.username !== me.username) {
      const shouldOffer = me.username.localeCompare(p.username) < 0;
      ensureGroupPeer(p.username, shouldOffer);
    }
  });
}

async function joinActiveGroupCall() {
  if (!activeChat || activeChat.type !== "group") return;
  await startOrJoinGroupCall(activeChat.id, activeChat.username);
}

async function startRoomCall() {
  if (!me || !activeChat || callState) return;
  if (activeChat.type === "group") {
    await startOrJoinGroupCall(activeChat.id, activeChat.username);
    return;
  }
  await startDirectCall();
}

async function startDirectCall() {
  if (!me || !activeChat || activeChat.type !== "direct" || callState) return;
  playSound("click");
  requestPageFullscreen();
  cameraEnabled = false;
  micMuted = false;

  try {
    await ensureLocalAudioStream();
  } catch (err) {
    alert("Не удалось включить микрофон. Проверь разрешение микрофона и HTTPS.");
    return;
  }

  const peer = activeChat.username;
  const peerEmoji = activeChat.emoji || "🙂";
  const peerPhoto = activeChat.avatarPhoto || "";
  const createRes = await api("/api/call/start", { caller: me.username, callee: peer });
  if (!createRes.ok || !createRes.callId) {
    forceResetCallUi();
    alert(createRes.error || "Не удалось начать звонок");
    return;
  }

  const cid = createRes.callId;
  callState = {
    mode: "direct",
    callId: cid,
    peer,
    peerEmoji,
    peerPhoto,
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
  if (!callState || callState.callId !== cid || !pc) return;
  await pc.setLocalDescription(offer);
  callState.lastAppliedOfferJson = JSON.stringify(pc.localDescription);
  await api("/api/call/offer", {
    callId: cid,
    from: me.username,
    offer: pc.localDescription
  });
}

async function acceptIncomingCall() {
  if (!me || !callState || callState.mode !== "direct" || callState.isCaller) return;
  const cid = callState.callId;
  playSound("join");
  unlockAllAudioOutputs();
  requestPageFullscreen();
  cameraEnabled = false;
  micMuted = false;

  try {
    await ensureLocalAudioStream();
  } catch (err) {
    alert("Не удалось включить микрофон. Проверь разрешение микрофона и HTTPS.");
    await hangupCall(true);
    return;
  }

  if (!callState || callState.callId !== cid) return;
  pc = createPeerConnectionForCall(cid);

  if (!pendingOfferToApply) {
    const latest = await api(`/api/call/poll?user=${encodeURIComponent(me.username)}&callId=${encodeURIComponent(cid)}`);
    if (latest && latest.ok && latest.call && latest.call.offer) {
      pendingOfferToApply = latest.call.offer;
    }
  }

  if (pendingOfferToApply) {
    await pc.setRemoteDescription(new RTCSessionDescription(pendingOfferToApply));
    callState.lastAppliedOfferJson = JSON.stringify(pendingOfferToApply);
    pendingOfferToApply = null;
    await flushPendingCandidates();
  }

  if (pc && pc.remoteDescription && callState && callState.callId === cid) {
    const answer = await pc.createAnswer({ offerToReceiveAudio: true, offerToReceiveVideo: true });
    if (!pc || !callState || callState.callId !== cid) return;
    await pc.setLocalDescription(answer);
    callState.lastAppliedAnswerJson = JSON.stringify(pc.localDescription);
    await api("/api/call/answer", {
      callId: cid,
      from: me.username,
      answer: pc.localDescription
    });
  }

  if (!callState || callState.callId !== cid) return;
  callState.status = "active";
  if (!callState.connectedAt) {
    callState.connectedAt = Date.now();
  }
  showCallModalUi();
  startCallDurationTicker();
  unlockAllAudioOutputs();
  syncDirectMediaElements();
}

function toggleMicMute() {
  micMuted = !micMuted;
  if (localStream) {
    localStream.getAudioTracks().forEach(track => {
      track.enabled = !micMuted;
    });
  }
  if (pc) {
    pc.getSenders().forEach(sender => {
      if (sender.track && sender.track.kind === "audio") {
        sender.track.enabled = !micMuted;
      }
    });
  }
  groupPeers.forEach(entry => {
    entry.pc.getSenders().forEach(sender => {
      if (sender.track && sender.track.kind === "audio") {
        sender.track.enabled = !micMuted;
      }
    });
  });
  unlockAllAudioOutputs();
  persistSessionState();
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
  if (endingCallInProgress) return;
  endingCallInProgress = true;

  const snap = callState ? { ...callState } : null;
  const durationStr = snap && snap.connectedAt ? formatCallDuration(getCurrentCallDurationSeconds()) : "00:00";
  const actorNick = me ? me.username : "";

  playSound("leave");
  forceResetCallUi();

  try {
    if (notifyServer && snap && actorNick) {
      if (snap.mode === "group") {
        await api("/api/group-call/leave", {
          groupId: snap.groupId,
          user: actorNick,
          duration: durationStr,
          wasConnected: Boolean(snap.connectedAt)
        });
        if (activeChat && activeChat.type === "group" && activeChat.id === snap.groupId) {
          await refreshMessages(true);
          await refreshChats();
        }
      } else if (snap.callId) {
        await api("/api/call/end", {
          callId: snap.callId,
          from: actorNick,
          duration: durationStr,
          wasConnected: Boolean(snap.connectedAt)
        });
        if (activeChat && activeChat.type === "direct" && activeChat.username === snap.peer) {
          await refreshMessages(true);
          await refreshChats();
        }
      }
    }
  } catch (e) {} finally {
    endingCallInProgress = false;
  }
}

function updateGroupCallBannerUi() {
  const banner = document.getElementById("groupCallBanner");
  const txt = document.getElementById("groupCallBannerText");
  if (!banner || !txt) return;

  if (!activeChat || activeChat.type !== "group" || (callState && callState.mode === "group" && callState.groupId === activeChat.id)) {
    banner.classList.add("hidden");
    return;
  }

  if (activeGroupCallInfo && Array.isArray(activeGroupCallInfo.participants) && activeGroupCallInfo.participants.length > 0) {
    const names = activeGroupCallInfo.participants.map(p => `${p.username}`).join(", ");
    txt.textContent = `📞 Идёт звонок (${names})`;
    banner.classList.remove("hidden");
    syncGroupCallPermissionUi();
  } else {
    banner.classList.add("hidden");
  }
}

async function pollCallStatus() {
  if (!me || endingCallInProgress) return;

  const watchGroupId = (callState && callState.mode === "group")
    ? callState.groupId
    : ((activeChat && activeChat.type === "group") ? activeChat.id : "");

  if (watchGroupId) {
    const gRes = await api(`/api/group-call/poll?groupId=${encodeURIComponent(watchGroupId)}&user=${encodeURIComponent(me.username)}`);
    if (gRes && gRes.ok) {
      activeGroupCallInfo = gRes.call || null;
      updateGroupCallBannerUi();

      if (callState && callState.mode === "group" && callState.groupId === watchGroupId) {
        if (!gRes.call || !Array.isArray(gRes.call.participants) || gRes.call.participants.length === 0) {
          playSound("leave");
          forceResetCallUi();
          return;
        }
        callState.participants = gRes.call.participants;
        if (!callState.connectedAt && gRes.call.startedAt) {
          callState.connectedAt = Number(gRes.call.startedAt);
          startCallDurationTicker();
        }

        const activeNicks = new Set(callState.participants.map(p => p.username));
        Array.from(groupPeers.keys()).forEach(peerNick => {
          if (!activeNicks.has(peerNick)) {
            const old = groupPeers.get(peerNick);
            try { old.pc.close(); } catch (e) {}
            try { old.audioEl.remove(); } catch (e) {}
            groupPeers.delete(peerNick);
          }
        });

        callState.participants.forEach(p => {
          if (p.username && p.username !== me.username) {
            const shouldOffer = me.username.localeCompare(p.username) < 0;
            ensureGroupPeer(p.username, shouldOffer);
            const entry = groupPeers.get(p.username);
            if (entry && !p.camOn) {
              entry.camOff = true;
              if (entry.videoTrack) {
                try { entry.videoTrack.stop(); } catch (e) {}
                entry.videoTrack = null;
              }
              if (entry.videoStream) {
                entry.videoStream.getVideoTracks().forEach(t => entry.videoStream.removeTrack(t));
              }
            } else if (entry && p.camOn) {
              entry.camOff = false;
            }
          }
        });

        const signals = Array.isArray(gRes.signals) ? gRes.signals : [];
        for (const sigItem of signals) {
          await handleGroupSignalItem(sigItem);
        }

        unlockAllAudioOutputs();
        showCallModalUi();
      }
    }
  } else {
    activeGroupCallInfo = null;
    updateGroupCallBannerUi();
  }

  if (callState && callState.mode === "group") {
    return;
  }

  const activeId = (callState && callState.mode === "direct") ? callState.callId : "";
  const res = await api(`/api/call/poll?user=${encodeURIComponent(me.username)}&callId=${encodeURIComponent(activeId)}`);
  if (!res || !res.ok) return;

  if (res.ended) {
    if (callState && callState.mode === "direct" && callState.callId === activeId) {
      playSound("leave");
      forceResetCallUi();
    }
    return;
  }

  const c = res.call;
  if (!c) {
    return;
  }

  if (c.status === "ended") {
    if (callState && callState.callId === c.callId) {
      playSound("leave");
      forceResetCallUi();
    }
    return;
  }

  if (me.username !== c.caller && me.username !== c.callee) {
    return;
  }

  if (!callState) {
    if (c.status === "ringing" && c.callee === me.username) {
      const peerUser = c.caller;
      playSound("receive");
      callState = {
        mode: "direct",
        callId: c.callId,
        peer: peerUser,
        peerEmoji: sanitizeAvatarEmoji(c.callerEmoji || "🙂"),
        peerPhoto: sanitizeAvatarPhotoClient(c.callerAvatarPhoto || "", peerUser),
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
    }
    return;
  }

  if (c.callId !== callState.callId) return;

  if (typeof c.peerCamOn === "boolean" && c.peerCamOn !== remoteCamActive) {
    applyRemoteCameraState(c.peerCamOn);
  }

  if (callState.isCaller && c.offer && !pc) {
    pc = createPeerConnectionForCall(c.callId);
  }

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
          if (!pc || !callState || callState.callId !== c.callId) return;
          await pc.setLocalDescription(ans);
          callState.lastAppliedAnswerJson = JSON.stringify(pc.localDescription);
          await api("/api/call/answer", {
            callId: c.callId,
            from: me.username,
            answer: pc.localDescription
          });
          unlockAllAudioOutputs();
          syncDirectMediaElements();
        } catch (e) {}
      } else if (!callState.isCaller && (!pc || !pc.remoteDescription)) {
        pendingOfferToApply = c.offer;
      }
    }
  }

  if (c.answer && pc && c.answerBy && c.answerBy !== me.username) {
    const remoteJson = JSON.stringify(c.answer);
    if (remoteJson !== callState.lastAppliedAnswerJson && pc.signalingState === "have-local-offer") {
      try {
        await pc.setRemoteDescription(new RTCSessionDescription(c.answer));
        callState.lastAppliedAnswerJson = remoteJson;
        await flushPendingCandidates();
        flushRenegotiateIfReady();
        unlockAllAudioOutputs();
        syncDirectMediaElements();
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
      unlockAllAudioOutputs();
      syncDirectMediaElements();
    }
  }

  if (!callState.isCaller && c.status === "active" && callState.status !== "active") {
    callState.status = "active";
    if (!callState.connectedAt) {
      callState.connectedAt = c.connectedAt ? Number(c.connectedAt) : Date.now();
    }
    showCallModalUi();
    startCallDurationTicker();
    unlockAllAudioOutputs();
    syncDirectMediaElements();
  }

  const incomingCands = Array.isArray(c.candidates) ? c.candidates : [];
  for (const item of incomingCands) {
    if (!item || item.from === me.username || !item.candidate) continue;
    await enqueueOrAddRemoteCandidate(item.candidate);
  }

  flushRenegotiateIfReady();

  if (callState && callState.status === "active") {
    unlockAllAudioOutputs();
    syncDirectMediaElements();
  }
}

function setKeyboardLift(px) {
  if (isDesktopLayoutActive()) {
    document.documentElement.style.setProperty("--kb-lift", "0px");
    return;
  }
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

  if (isDesktopLayoutActive()) {
    setKeyboardLift(0);
  } else {
    const viewportShrank = baselineViewportHeight - currentH > 90;
    if (viewportShrank || !composerFocused) {
      setKeyboardLift(0);
    }
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
  if (isDesktopLayoutActive()) {
    setKeyboardLift(0);
    return;
  }
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
  if (isDesktopLayoutActive()) {
    composerFocused = false;
    setKeyboardLift(0);
    return;
  }
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
    const deviceKey = DEVICE_CYCLE.includes(raw.device) ? raw.device : DEFAULT_CUSTOM.device;
    return {
      msgSize: Number(raw.msgSize) || DEFAULT_CUSTOM.msgSize,
      emojiSize: Number(raw.emojiSize) || DEFAULT_CUSTOM.emojiSize,
      photoSize: Number(raw.photoSize) || DEFAULT_CUSTOM.photoSize,
      theme: themeKey,
      device: deviceKey
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
  rootStyle.setProperty("--check-blue", "#2196f3");
  rootStyle.setProperty("--check-ink", "#ffffff");

  rootStyle.setProperty("--msg-font-size", `${cfg.msgSize}px`);
  rootStyle.setProperty("--emoji-size", `${cfg.emojiSize}px`);
  rootStyle.setProperty("--photo-max-size", `${cfg.photoSize}px`);

  applyThemeButtonsState(themeKey);
  applyDeviceLayoutClass(cfg.device || DEFAULT_CUSTOM.device);

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
    theme: cur.theme || DEFAULT_CUSTOM.theme,
    device: cur.device || DEFAULT_CUSTOM.device
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

function syncAvatarPickerUi() {
  const canPhoto = canCurrentUserUsePhotoAvatar();
  const titleEl = document.getElementById("avaSectionTitle");
  if (titleEl) {
    titleEl.textContent = canPhoto ? "Аватарка (эмодзи или фото)" : "Аватарка (только эмодзи)";
  }
  renderAvatarElement(
    document.getElementById("settingsCurrentAva"),
    me ? me.emoji : "🍐",
    me ? me.avatarPhoto : "",
    me ? me.username : ""
  );
  const box = document.getElementById("avaPicker");
  const btn = document.getElementById("btnToggleAvaPicker");
  const upBtn = document.getElementById("btnUploadAvaPhoto");
  const clrBtn = document.getElementById("btnClearAvaPhoto");

  if (box) box.classList.toggle("hidden", !avaPickerOpen);
  if (btn) btn.textContent = avaPickerOpen ? "Скрыть эмодзи" : "Выбрать эмодзи";
  if (upBtn) upBtn.classList.toggle("hidden", !canPhoto);
  if (clrBtn) {
    const hasPhoto = Boolean(canPhoto && me && me.avatarPhoto);
    clrBtn.classList.toggle("hidden", !hasPhoto);
  }
}

function toggleAvatarPicker() {
  avaPickerOpen = !avaPickerOpen;
  playSound("click");
  syncAvatarPickerUi();
}

function triggerAvatarPhotoUpload() {
  if (!canCurrentUserUsePhotoAvatar()) return;
  playSound("click");
  const inp = document.getElementById("avaPhotoInput");
  if (inp) {
    inp.value = "";
    inp.click();
  }
}

async function onAvatarPhotoSelected(event) {
  const file = event.target.files && event.target.files[0];
  if (!file || !canCurrentUserUsePhotoAvatar() || !me) return;
  try {
    const dataUrl = await compressImageFile(file);
    const res = await api("/api/avatar-photo", { username: me.username, avatarPhoto: dataUrl });
    if (!res.ok) {
      document.getElementById("setErr").textContent = res.error || "Не удалось загрузить фото";
      return;
    }
    playSound("react");
    me.avatarPhoto = sanitizeAvatarPhotoClient(res.avatarPhoto || dataUrl, me.username);
    renderAvatarElement(document.getElementById("myAva"), me.emoji, me.avatarPhoto, me.username);
    syncAvatarPickerUi();
    document.getElementById("setErr").textContent = "";
    document.getElementById("setOk").textContent = "Фотография на аватарке сохранена!";
    refreshChats();
  } catch (e) {
    document.getElementById("setErr").textContent = "Ошибка обработки изображения";
  }
}

async function clearAvatarPhoto() {
  if (!canCurrentUserUsePhotoAvatar() || !me) return;
  const res = await api("/api/avatar-photo", { username: me.username, avatarPhoto: "" });
  if (res.ok) {
    playSound("click");
    me.avatarPhoto = "";
    renderAvatarElement(document.getElementById("myAva"), me.emoji, "", me.username);
    syncAvatarPickerUi();
    document.getElementById("setOk").textContent = "Фото удалено, снова используется эмодзи";
    refreshChats();
  }
}

function isFullscreenNow() {
  return Boolean(document.fullscreenElement || document.webkitFullscreenElement);
}

async function toggleFullscreenByTap(e) {
  if (isDesktopLayoutActive()) return;
  if (!e.target.closest) return;
  const interactive = e.target.closest(
    "button, input, textarea, select, a, .chat-card, .member-pick, .group-member-row, .group-edit-card, .room-header-main, .msg-row, .m, .msg-img, .msg-video-card, .video-viewer-backdrop, .em-btn, .clip-btn, .attach-preview, .reply-bar, .msg-menu-card, .reaction-chip, .call-overlay, .top, .bottom-dock, .custom-box, .fab-create, .group-call-banner"
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
  return `${esc(username)}${badge}`;
}

async function api(url, body = null) {
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), 8000);
  try {
    const opts = body
      ? {
          method: "POST",
          credentials: "same-origin",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
          signal: controller.signal
        }
      : {
          credentials: "same-origin",
          signal: controller.signal
        };
    const res = await fetch(url, opts);
    return await res.json();
  } catch (e) {
    return { ok: false };
  } finally {
    clearTimeout(timeoutId);
  }
}

function saveLocalUser(user) {
  if (user) {
    user = {
      ...user,
      emoji: sanitizeAvatarEmoji(user.emoji),
      avatarPhoto: sanitizeAvatarPhotoClient(user.avatarPhoto, user.username)
    };
  }
  me = user;
  try {
    if (user) localStorage.setItem("grusha_nick", user.username);
    else localStorage.removeItem("grusha_nick");
  } catch (e) {}
}

function showScreen(id) {
  currentScreenId = id;
  composerFocused = false;
  setKeyboardLift(0);
  closeMsgActionModal();
  closeAttachChoiceModal();
  closeVideoViewer();
  ["loadingScreen", "authScreen", "chatsScreen", "createGroupScreen", "groupSettingsScreen", "directChatSettingsScreen", "searchScreen", "roomScreen", "settingsScreen"].forEach(s => {
    document.getElementById(s).classList.toggle("hidden", s !== id);
  });
  syncDesktopSplitClass();
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
  enterChats(false);
}

async function enterChats(restoreSaved = false) {
  activeChat = null;
  activeGroupCallInfo = null;
  currentGroupOwner = "";
  currentGroupCallAccess = "all";
  replyTarget = null;
  roomHistoryInitialized = false;
  clearAttachment();
  lastRenderedMsgsJson = "";
  renderAvatarElement(document.getElementById("myAva"), me.emoji, me.avatarPhoto, me.username);
  document.getElementById("myNick").innerHTML = nickLabelHtml(me.username, me.isOwner);
  showScreen("chatsScreen");
  await refreshChats();
  startPolling();

  if (restoreSaved && me) {
    const savedChat = readSavedSessionChat(me.username);
    if (savedChat) {
      if (savedChat.type === "group" && savedChat.id) {
        await openGroupChat(savedChat.id, savedChat.username, savedChat.emoji, savedChat.tripleBadge);
      } else if (savedChat.type === "direct" && savedChat.username) {
        await openDirectChat(savedChat.username, savedChat.emoji, savedChat.isOwner, savedChat.avatarPhoto || "");
      }
    }
    await restoreSessionCallIfAny();
  }
}

function backToChats() {
  playSound("click");
  activeChat = null;
  activeGroupCallInfo = null;
  currentGroupOwner = "";
  currentGroupCallAccess = "all";
  replyTarget = null;
  roomHistoryInitialized = false;
  clearAttachment();
  lastRenderedMsgsJson = "";
  persistSessionState();
  updateGroupCallBannerUi();
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

async function openDirectChatSettings() {
  if (!activeChat || activeChat.type !== "direct") return;
  playSound("click");
  showScreen("directChatSettingsScreen");
  renderDirectChatSettings();
  await refreshDirectPeerOnlineStatus();
}

async function refreshDirectPeerOnlineStatus() {
  if (!activeChat || activeChat.type !== "direct") return;
  const res = await api(`/api/user/online?username=${encodeURIComponent(activeChat.username)}`);
  if (res && res.ok) {
    directPeerOnline = Boolean(res.online);
    if (currentScreenId === "directChatSettingsScreen") {
      renderDirectChatSettings();
    }
  }
}

function renderDirectChatSettings() {
  const body = document.getElementById("directChatSettingsBody");
  if (!body || !activeChat || activeChat.type !== "direct") return;
  const statusHtml = directPeerOnline
    ? `<span class="online-pill-yes">● В сети</span>`
    : `<span class="online-pill-no">○ Не в сети</span>`;
  body.innerHTML = `
    <div style="display:flex; align-items:center; gap:12px;">
      <div class="ava" style="width:52px; height:52px; font-size:26px;">${avatarHtml(activeChat.emoji, activeChat.avatarPhoto, activeChat.username)}</div>
      <div>
        <div style="font-size:18px; font-weight:800;">${nickLabelHtml(activeChat.username, activeChat.isOwner)}</div>
        <div class="mt-8">${statusHtml}</div>
      </div>
    </div>

    <div class="group-edit-card">
      <div style="font-weight:700;">Статус пользователя</div>
      <div class="mt-8">${statusHtml}</div>
    </div>

    <div class="group-edit-card">
      <div style="font-weight:700; color:#ff8c8c;">Удаление чата</div>
      <div class="muted mt-8">История сообщений в этом личном чате будет удалена.</div>
      <button type="button" class="del mt-12" style="width:100%;" onclick="deleteDirectChat()">Удалить чат</button>
    </div>
  `;
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
    return `
      <div class="member-pick ${checked ? 'selected' : ''}" onclick="toggleGroupMember('${esc(u.username)}')">
        <input type="checkbox" ${checked ? 'checked' : ''} onclick="event.stopPropagation(); toggleGroupMember('${esc(u.username)}')">
        <div class="ava" style="width:36px; height:36px; font-size:18px;">${avatarHtml(u.emoji, u.avatarPhoto, u.username)}</div>
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

async function setGroupCallAccessMode(mode) {
  if (!activeChat || activeChat.type !== "group" || !me) return;
  if (!["all", "owner", "nobody"].includes(mode)) return;
  const res = await api("/api/groups/call-access", {
    actor: me.username,
    groupId: activeChat.id,
    callAccess: mode
  });
  if (res.ok) {
    playSound("react");
    currentGroupCallAccess = res.callAccess || mode;
    syncGroupCallPermissionUi();
    await renderGroupSettings();
  }
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
  currentGroupCallAccess = res.group.callAccess || currentGroupCallAccess;
  persistSessionState();
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

function callAccessLabel(mode) {
  if (mode === "owner") return "Только Владелец";
  if (mode === "nobody") return "Никто";
  return "Все";
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
  currentGroupOwner = g.owner || "";
  currentGroupCallAccess = g.callAccess || "all";
  syncGroupCallPermissionUi();
  const isGroupOwner = g.owner === me.username;
  const membersSet = new Set(g.members || []);
  const mutedSet = new Set(g.muted || []);
  editGroupEmoji = sanitizeAvatarEmoji(g.emoji || "🦜");

  activeChat.username = g.title;
  activeChat.emoji = editGroupEmoji;
  activeChat.tripleBadge = Boolean(g.tripleBadge);
  persistSessionState();
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

  const callAccessBoxHtml = `
    <div class="group-edit-card">
      <div style="font-weight:700;">📞 Кто может делать звонки</div>
      <div class="muted mt-8">Текущий режим: <strong>${callAccessLabel(currentGroupCallAccess)}</strong></div>
      ${
        isGroupOwner
          ? `
            <div class="call-access-row">
              <button type="button" class="call-access-btn ${currentGroupCallAccess === 'all' ? 'active-mode' : ''}" onclick="setGroupCallAccessMode('all')">Все</button>
              <button type="button" class="call-access-btn ${currentGroupCallAccess === 'owner' ? 'active-mode' : ''}" onclick="setGroupCallAccessMode('owner')">Только Владелец</button>
              <button type="button" class="call-access-btn ${currentGroupCallAccess === 'nobody' ? 'active-mode' : ''}" onclick="setGroupCallAccessMode('nobody')">Никто</button>
            </div>
          `
          : `<div class="muted mt-8">Менять это правило может только владелец группы.</div>`
      }
    </div>
  `;

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
              return `
                <div class="member-pick invite-pick ${checked ? 'selected' : ''}" data-nick="${esc(u.username)}" onclick="toggleInviteCandidate('${esc(u.username)}')">
                  <input type="checkbox" ${checked ? 'checked' : ''} onclick="event.stopPropagation(); toggleInviteCandidate('${esc(u.username)}')">
                  <div class="ava" style="width:34px; height:34px; font-size:17px;">${avatarHtml(u.emoji, u.avatarPhoto, u.username)}</div>
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

    ${callAccessBoxHtml}
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
  avaPickerOpen = false;
  renderAvatarChoices();
  syncAvatarPickerUi();
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
    const photo = sanitizeAvatarPhotoClient(u.avatarPhoto, u.username);
    return `
      <div class="chat-card" onclick="openDirectChat('${esc(u.username)}', '${esc(ava)}', ${u.isOwner ? 'true' : 'false'}, '${escAttr(photo)}')">
        <div class="ava">${avatarHtml(ava, photo, u.username)}</div>
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

  res.chats.forEach(c => {
    if (c.type !== "group" && c.username) {
      memberProfilesMap.set(c.username, {
        emoji: sanitizeAvatarEmoji(c.emoji),
        avatarPhoto: sanitizeAvatarPhotoClient(c.avatarPhoto, c.username)
      });
    }
  });

  if (activeChat) {
    if (activeChat.type === "direct") {
      const matched = res.chats.find(c => c.type !== "group" && c.username === activeChat.username);
      if (matched) {
        const cleanAva = sanitizeAvatarEmoji(matched.emoji || activeChat.emoji);
        const cleanPhoto = sanitizeAvatarPhotoClient(matched.avatarPhoto, matched.username);
        if (cleanAva !== activeChat.emoji || cleanPhoto !== (activeChat.avatarPhoto || "")) {
          activeChat.emoji = cleanAva;
          activeChat.avatarPhoto = cleanPhoto;
          persistSessionState();
          renderAvatarElement(document.getElementById("peerAva"), cleanAva, cleanPhoto, activeChat.username);
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
      persistSessionState();
      if (matchedGroup.owner) currentGroupOwner = matchedGroup.owner;
      if (matchedGroup.callAccess) currentGroupCallAccess = matchedGroup.callAccess;
      syncGroupCallPermissionUi();
      document.getElementById("peerAva").textContent = activeChat.emoji;
      document.getElementById("peerNick").innerHTML = groupTitleHtml(matchedGroup.title, activeChat.tripleBadge);
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
      const isSelected = activeChat && activeChat.type === "group" && activeChat.id === c.groupId;
      return `
        <div class="chat-card ${isSelected ? 'active-peer-card' : ''}" onclick="openGroupChat('${esc(c.groupId)}', '${esc(c.title)}', '${esc(gAva)}', ${c.tripleBadge ? 'true' : 'false'})">
          <div class="ava">${gAva}</div>
          <div class="chat-info">
            <div class="chat-name">${groupTitleHtml(c.title, c.tripleBadge)} <span class="muted" style="font-weight:500; font-size:12px;">• группа</span></div>
            <div class="chat-last ${typingLabel ? 'typing-active' : ''}">${typingLabel || esc(c.last || 'Сообщений пока нет')}</div>
          </div>
        </div>
      `;
    }
    const cleanAva = sanitizeAvatarEmoji(c.emoji);
    const cleanPhoto = sanitizeAvatarPhotoClient(c.avatarPhoto, c.username);
    const isSelected = activeChat && activeChat.type !== "group" && activeChat.username === c.username;
    return `
      <div class="chat-card ${isSelected ? 'active-peer-card' : ''}" onclick="openDirectChat('${esc(c.username)}', '${esc(cleanAva)}', ${c.isOwner ? 'true' : 'false'}, '${escAttr(cleanPhoto)}')">
        <div class="ava">${avatarHtml(cleanAva, cleanPhoto, c.username)}</div>
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
  if (!msg || msg.from === "system" || msg.isUploading) return;
  const cleanText = (msg.text || "").trim();
  const hasImg = Boolean(msg.image);
  const hasVid = Boolean(msg.video);
  replyTarget = {
    from: msg.from,
    text: cleanText,
    hasImage: hasImg,
    hasVideo: hasVid
  };
  const bar = document.getElementById("replyBar");
  const authorEl = document.getElementById("replyBarAuthor");
  const txtEl = document.getElementById("replyBarText");
  if (bar && authorEl && txtEl) {
    authorEl.textContent = `Ответ ${replyTarget.from}`;
    if (cleanText && hasVid) {
      txtEl.textContent = `🎬 ${cleanText}`;
    } else if (cleanText && hasImg) {
      txtEl.textContent = `📷 ${cleanText}`;
    } else if (cleanText) {
      txtEl.textContent = cleanText;
    } else if (hasVid) {
      txtEl.textContent = "🎬 Видео";
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

function openVideoViewer(msgIndex) {
  const msg = currentRoomMsgs[msgIndex];
  if (!msg || !msg.video || msg.isUploading) return;
  playSound("click");
  const modal = document.getElementById("videoViewerModal");
  const authorEl = document.getElementById("videoViewerAuthor");
  const statusEl = document.getElementById("videoViewerStatus");
  const vidEl = document.getElementById("videoViewerEl");
  if (!modal || !vidEl) return;

  if (authorEl) authorEl.textContent = `${msg.from || "пользователь"}`;
  if (statusEl) statusEl.textContent = "Загрузка видео...";

  vidEl.preload = "auto";
  vidEl.src = msg.video;
  vidEl.onloadeddata = () => {
    if (statusEl) statusEl.textContent = "Видео готово";
  };
  vidEl.onplaying = () => {
    if (statusEl) statusEl.textContent = "";
  };
  vidEl.onerror = () => {
    if (statusEl) statusEl.textContent = "Не удалось воспроизвести этот формат видео";
  };
  modal.classList.remove("hidden");
  vidEl.play().catch(() => {});
}

function closeVideoViewer(event) {
  if (event && event.target && event.target.id !== "videoViewerModal") return;
  const modal = document.getElementById("videoViewerModal");
  const vidEl = document.getElementById("videoViewerEl");
  if (vidEl) {
    try { vidEl.pause(); } catch (e) {}
    vidEl.removeAttribute("src");
    vidEl.preload = "none";
    try { vidEl.load(); } catch (e) {}
  }
  if (modal) modal.classList.add("hidden");
}

function canDeleteMessageInCurrentChat(msg) {
  if (!msg || msg.from === "system" || msg.isUploading || !activeChat || !me) return false;
  if (activeChat.type === "direct") return true;
  if (activeChat.type === "group") {
    return currentGroupOwner === me.username;
  }
  return false;
}

function openMsgActionModal(msgIndex) {
  const msg = currentRoomMsgs[msgIndex];
  if (!msg || msg.from === "system" || msg.isUploading) return;
  selectedMsgForAction = msg;
  playSound("click");
  const prev = document.getElementById("msgActionPreview");
  const cleanText = (msg.text || "").trim();
  const snippet = cleanText || (msg.video ? "🎬 Видео" : (msg.image ? "📷 Фотография" : "Сообщение"));
  if (prev) prev.textContent = `${msg.from}: ${snippet}`;

  const delBtn = document.getElementById("btnDeleteSelectedMsg");
  if (delBtn) {
    delBtn.classList.toggle("hidden", !canDeleteMessageInCurrentChat(msg));
  }

  const myReactions = msg.reactions || {};
  const grid = document.getElementById("msgReactionChoices");
  if (grid) {
    grid.innerHTML = REACTION_EMOJIS.map(em => {
      const users = myReactions[em] || [];
      const active = users.includes(me.username);
      return `<button type="button" class="reaction-pick-btn" style="${active ? 'outline:2px solid var(--accent); background:var(--accent-soft);' : ''}" onclick="pickReactionForSelected('${em}')">${em}</button>`;
    }).join("");
  }

  const authorsBox = document.getElementById("msgReactionAuthorsBox");
  if (authorsBox) {
    const activeKeys = Object.keys(myReactions).filter(k => Array.isArray(myReactions[k]) && myReactions[k].length > 0);
    if (activeKeys.length > 0) {
      authorsBox.classList.remove("hidden");
      authorsBox.innerHTML = `<div class="muted" style="font-weight:700;">Кто поставил реакции:</div>` + activeKeys.map(em => {
        const who = myReactions[em].map(u => u === me.username ? `${esc(u)} (вы)` : esc(u)).join(", ");
        return `<div class="reaction-author-line"><span>${em}</span><span>${who}</span></div>`;
      }).join("");
    } else {
      authorsBox.classList.add("hidden");
      authorsBox.innerHTML = "";
    }
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

async function deleteSelectedMsg() {
  const target = selectedMsgForAction;
  closeMsgActionModal();
  if (!target || !activeChat || !me) return;
  if (!canDeleteMessageInCurrentChat(target)) return;

  const payload = {
    actor: me.username,
    msgId: target.id,
    chatType: activeChat.type
  };
  if (activeChat.type === "group") {
    payload.groupId = activeChat.id;
  } else {
    payload.peer = activeChat.username;
  }

  const res = await api("/api/msgs/delete", payload);
  if (res.ok) {
    playSound("leave");
    lastRenderedMsgsJson = "";
    await refreshMessages(false);
    await refreshChats();
  }
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

async function openDirectChat(username, emoji, isOwner = false, avatarPhoto = "") {
  playSound("click");
  const cleanAva = sanitizeAvatarEmoji(emoji);
  const cleanPhoto = sanitizeAvatarPhotoClient(avatarPhoto, username);
  memberProfilesMap.set(username, { emoji: cleanAva, avatarPhoto: cleanPhoto });
  activeChat = { type: "direct", id: username, username, emoji: cleanAva, avatarPhoto: cleanPhoto, isOwner, tripleBadge: false };
  persistSessionState();
  activeGroupCallInfo = null;
  currentGroupOwner = "";
  currentGroupCallAccess = "all";
  replyTarget = null;
  roomHistoryInitialized = false;
  seenDirectMsgIds = new Set();
  clearReplyTarget();
  clearAttachment();
  lastRenderedMsgsJson = "";
  syncRoomComposerLock(false);
  updateGroupCallBannerUi();
  const headerMain = document.getElementById("roomHeaderMain");
  if (headerMain) headerMain.classList.add("clickable-header");
  const callBtn = document.getElementById("btnCallRoom");
  if (callBtn) {
    callBtn.classList.remove("hidden");
    callBtn.disabled = false;
    callBtn.title = "Позвонить";
  }
  renderAvatarElement(document.getElementById("peerAva"), cleanAva, cleanPhoto, username);
  document.getElementById("peerNick").innerHTML = nickLabelHtml(username, isOwner);
  document.getElementById("peerStatus").textContent = "";
  const inp = document.getElementById("msgInput");
  inp.value = "";
  autoResizeComposer();
  showScreen("roomScreen");
  await refreshMessages(true);
  await refreshChats();
  await refreshDirectPeerOnlineStatus();
}

async function openGroupChat(groupId, title, emoji, tripleBadge = false) {
  playSound("click");
  const badgeFlag = Boolean(tripleBadge) || isTripleBadgeGroupTitle(title);
  const gAva = sanitizeAvatarEmoji(emoji || "🦜");
  activeChat = { type: "group", id: groupId, username: title, emoji: gAva, isOwner: false, tripleBadge: badgeFlag };
  persistSessionState();
  activeGroupCallInfo = null;
  currentGroupOwner = "";
  currentGroupCallAccess = "all";
  replyTarget = null;
  roomHistoryInitialized = false;
  seenGroupMsgIds = new Set();
  clearReplyTarget();
  clearAttachment();
  lastRenderedMsgsJson = "";
  syncRoomComposerLock(false);
  updateGroupCallBannerUi();
  const headerMain = document.getElementById("roomHeaderMain");
  if (headerMain) headerMain.classList.add("clickable-header");
  syncGroupCallPermissionUi();
  document.getElementById("peerAva").textContent = gAva;
  document.getElementById("peerNick").innerHTML = groupTitleHtml(title, badgeFlag);
  document.getElementById("peerStatus").textContent = "";
  const inp = document.getElementById("msgInput");
  inp.value = "";
  autoResizeComposer();
  showScreen("roomScreen");
  await refreshMessages(true);
  await refreshChats();
  await pollCallStatus();
}

function formatTextWithEmojis(rawText) {
  const safe = esc(rawText);
  return safe.replace(SINGLE_EMOJI_RE, match => `<span class="inline-emoji">${match}</span>`);
}

function formatReplyPreviewText(rep) {
  if (!rep) return "Сообщение";
  const t = (rep.text || "").trim();
  if (t && rep.hasVideo) return `🎬 ${t}`;
  if (t && rep.hasImage) return `📷 ${t}`;
  if (t) return t;
  if (rep.hasVideo) return "🎬 Видео";
  if (rep.hasImage) return "📷 Фотография";
  return "Сообщение";
}

function getSenderAvatarData(senderNick) {
  if (senderNick === me.username) {
    return {
      emoji: sanitizeAvatarEmoji(me.emoji),
      photo: sanitizeAvatarPhotoClient(me.avatarPhoto, me.username)
    };
  }
  if (activeChat && activeChat.type === "direct" && senderNick === activeChat.username) {
    return {
      emoji: sanitizeAvatarEmoji(activeChat.emoji),
      photo: sanitizeAvatarPhotoClient(activeChat.avatarPhoto, senderNick)
    };
  }
  const cached = memberProfilesMap.get(senderNick);
  if (cached) {
    return {
      emoji: sanitizeAvatarEmoji(cached.emoji),
      photo: sanitizeAvatarPhotoClient(cached.avatarPhoto, senderNick)
    };
  }
  return { emoji: "🍐", photo: "" };
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
  const timeStr = formatMessageTime(m.ts);

  const isEmojiOnly = !m.image && !m.video && !m.isUploading && !hasReply && !hasReactions && text.length > 0 && EMOJI_ONLY_RE.test(text);
  const bubbleExtra = isEmojiOnly ? " emoji-only-bubble" : "";

  const senderHtml = (activeChat && activeChat.type === "group" && !isMe)
    ? `<div class="msg-sender">${esc(m.from)}</div>`
    : "";

  const replyHtml = m.replyTo
    ? `<div class="reply-quote"><div class="reply-quote-author">${esc(m.replyTo.from || "пользователь")}</div><div class="reply-quote-txt">${esc(formatReplyPreviewText(m.replyTo))}</div></div>`
    : "";

  const imgHtml = m.image
    ? `<img class="msg-img" src="${m.image}" alt="Картинка" onclick="if(suppressNextBubbleClick){suppressNextBubbleClick=false;return;}event.stopPropagation();window.open('${m.image}', '_blank')">`
    : "";

  let videoHtml = "";
  if (m.isUploading) {
    const pct = Math.max(0, Math.min(100, Math.round(Number(m.progress) || 0)));
    videoHtml = `
      <div class="msg-video-card uploading">
        <div class="msg-video-play">⏳</div>
        <div class="msg-video-meta">
          <div class="msg-video-title">Отправка видео: ${pct}%</div>
          <div class="msg-video-sub">${esc(m.videoName || "Видео")}</div>
          <div class="upload-progress-track"><div class="upload-progress-fill" style="width:${pct}%;"></div></div>
        </div>
      </div>
    `;
  } else if (m.video) {
    videoHtml = `<div class="msg-video-card" onclick="if(suppressNextBubbleClick){suppressNextBubbleClick=false;return;}event.stopPropagation();openVideoViewer(${index})"><div class="msg-video-play">▶</div><div class="msg-video-meta"><div class="msg-video-title">Видео</div><div class="msg-video-sub">Нажмите, чтобы загрузить и открыть</div></div></div>`;
  }

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

  const footerMetaHtml = m.isUploading
    ? `<div class="msg-footer-meta">${Math.max(0, Math.min(100, Math.round(Number(m.progress) || 0)))}%</div>`
    : (timeStr ? `<div class="msg-footer-meta">${esc(timeStr)}</div>` : "");

  const bubbleHtml = `<div class="m ${cls}${bubbleExtra}" data-msg-index="${index}">${senderHtml}${replyHtml}${imgHtml}${videoHtml}${txtHtml}${reactionsHtml}${footerMetaHtml}</div>`;

  if (isMe) {
    return `<div class="msg-row row-me">${bubbleHtml}</div>`;
  }

  const avaInfo = getSenderAvatarData(m.from);
  const sideAvaHtml = `<div class="ava msg-side-ava">${avatarHtml(avaInfo.emoji, avaInfo.photo, m.from)}</div>`;
  return `<div class="msg-row row-them">${sideAvaHtml}${bubbleHtml}</div>`;
}

function attachMessageInteractions() {
  const box = document.getElementById("msgsBox");
  if (!box) return;
  box.querySelectorAll(".m[data-msg-index]").forEach(el => {
    const idx = Number(el.getAttribute("data-msg-index"));
    bindLongPressToBubble(el, idx);
  });
}

function getActivePendingUploads() {
  if (!activeChat || !me) return [];
  return pendingUploads.filter(u =>
    u.chatType === activeChat.type &&
    u.chatTarget === (activeChat.type === "group" ? activeChat.id : activeChat.username)
  );
}

function renderRoomMessagesMerged(serverMsgs, forceScroll = false) {
  const localActive = getActivePendingUploads();
  currentRoomMsgs = [...serverMsgs, ...localActive];

  const customStamp = JSON.stringify(loadCustom());
  const json = JSON.stringify(currentRoomMsgs);
  if (json === lastRenderedMsgsJson && customStamp === lastRenderedCustomJson && !forceScroll) return;
  lastRenderedMsgsJson = json;
  lastRenderedCustomJson = customStamp;

  const box = document.getElementById("msgsBox");
  if (!box) return;
  if (!currentRoomMsgs.length) {
    const emptyText = (activeChat && activeChat.type === "group")
      ? "Группа создана! Напишите первое сообщение."
      : "Здесь пока пусто. Напишите первое сообщение!";
    box.innerHTML = `<div class="muted" style="margin:auto;">${emptyText}</div>`;
    return;
  }
  box.innerHTML = currentRoomMsgs.map((m, i) => renderMsgItem(m, i)).join("");
  attachMessageInteractions();
  box.scrollTop = box.scrollHeight;
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
    if (res.owner) currentGroupOwner = res.owner;
    if (res.callAccess) currentGroupCallAccess = res.callAccess;
    syncGroupCallPermissionUi();
    syncRoomComposerLock(Boolean(res.isMuted));
    activeChat.username = res.title || activeChat.username;
    activeChat.emoji = sanitizeAvatarEmoji(res.emoji || activeChat.emoji);
    activeChat.tripleBadge = Boolean(res.tripleBadge) || isTripleBadgeGroupTitle(activeChat.username);
    document.getElementById("peerAva").textContent = activeChat.emoji;
    document.getElementById("peerNick").innerHTML = groupTitleHtml(activeChat.username, activeChat.tripleBadge);

    if (res.memberProfiles && typeof res.memberProfiles === "object") {
      Object.entries(res.memberProfiles).forEach(([nick, prof]) => {
        if (nick && prof) {
          memberProfilesMap.set(nick, {
            emoji: sanitizeAvatarEmoji(prof.emoji),
            avatarPhoto: sanitizeAvatarPhotoClient(prof.avatarPhoto, nick)
          });
        }
      });
    }

    const statusEl = document.getElementById("peerStatus");
    if (statusEl) {
      const typers = res.typingUsers || [];
      if (typers.length) {
        const formattedTypers = typers.map(n => `${n}`).join(", ");
        statusEl.textContent = `${formattedTypers} печатает...`;
      } else {
        statusEl.textContent = "";
      }
    }

    lastServerMsgsCache = res.msgs || [];
    handleIncomingSounds(lastServerMsgsCache, true);
    renderRoomMessagesMerged(lastServerMsgsCache, forceScroll);
    return;
  }

  const res = await api(`/api/msgs?a=${encodeURIComponent(me.username)}&b=${encodeURIComponent(activeChat.username)}`);
  if (!res.ok) return;

  const cleanAva = sanitizeAvatarEmoji(res.peerEmoji || activeChat.emoji);
  const cleanPhoto = sanitizeAvatarPhotoClient(res.peerAvatarPhoto, activeChat.username);
  memberProfilesMap.set(activeChat.username, { emoji: cleanAva, avatarPhoto: cleanPhoto });
  if (cleanAva !== activeChat.emoji || cleanPhoto !== (activeChat.avatarPhoto || "")) {
    activeChat.emoji = cleanAva;
    activeChat.avatarPhoto = cleanPhoto;
    renderAvatarElement(document.getElementById("peerAva"), cleanAva, cleanPhoto, activeChat.username);
  }

  if (typeof res.peerOnline === "boolean" && res.peerOnline !== directPeerOnline) {
    directPeerOnline = res.peerOnline;
    if (currentScreenId === "directChatSettingsScreen") {
      renderDirectChatSettings();
    }
  }

  const statusEl = document.getElementById("peerStatus");
  if (statusEl) {
    statusEl.textContent = res.peerTyping ? "печатает..." : "";
  }

  lastServerMsgsCache = res.msgs || [];
  handleIncomingSounds(lastServerMsgsCache, false);
  renderRoomMessagesMerged(lastServerMsgsCache, forceScroll);
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
  pendingVideoFile = null;
  pendingVideoName = "";
  const vInp = document.getElementById("videoFileInput");
  if (vInp) vInp.value = "";
  const pInp = document.getElementById("photoFileInput");
  if (pInp) pInp.value = "";
  const prev = document.getElementById("attachPreview");
  if (prev) prev.classList.add("hidden");
  const thumb = document.getElementById("attachThumb");
  if (thumb) {
    thumb.removeAttribute("src");
    thumb.classList.add("hidden");
  }
  const vidBadge = document.getElementById("attachVideoBadge");
  if (vidBadge) vidBadge.classList.add("hidden");
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

  const isVideo = (file.type && file.type.startsWith("video/")) || /\.(mp4|webm|mov|m4v|3gp|mkv)$/i.test(file.name || "");
  if (isVideo) {
    if (file.size > MAX_VIDEO_FILE_BYTES) {
      alert("Видео слишком большое. Выбери ролик до 25 МБ.");
      clearAttachment();
      return;
    }
    pendingImageData = "";
    pendingVideoFile = file;
    pendingVideoName = file.name || "Видео";
    const thumb = document.getElementById("attachThumb");
    if (thumb) thumb.classList.add("hidden");
    const vidBadge = document.getElementById("attachVideoBadge");
    if (vidBadge) vidBadge.classList.remove("hidden");
    const sizeMb = (file.size / (1024 * 1024)).toFixed(1);
    document.getElementById("attachName").textContent = `🎬 ${pendingVideoName} (${sizeMb} МБ)`;
    document.getElementById("attachPreview").classList.remove("hidden");
    playSound("click");
    document.getElementById("msgInput").focus();
    return;
  }

  try {
    const dataUrl = await compressImageFile(file);
    pendingImageData = dataUrl;
    pendingVideoFile = null;
    pendingVideoName = "";
    const vidBadge = document.getElementById("attachVideoBadge");
    if (vidBadge) vidBadge.classList.add("hidden");
    const thumb = document.getElementById("attachThumb");
    if (thumb) {
      thumb.src = dataUrl;
      thumb.classList.remove("hidden");
    }
    document.getElementById("attachName").textContent = file.name || "Картинка прикреплена";
    document.getElementById("attachPreview").classList.remove("hidden");
    playSound("click");
    document.getElementById("msgInput").focus();
  } catch (e) {
    clearAttachment();
  }
}

function uploadVideoWithProgress(file, chatSnapshot, text, rep) {
  const uploadId = `up_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`;
  const item = {
    id: uploadId,
    from: me.username,
    text,
    ts: Date.now(),
    isUploading: true,
    progress: 0,
    videoName: file.name || "Видео",
    replyTo: rep,
    reactions: {},
    chatType: chatSnapshot.type,
    chatTarget: chatSnapshot.type === "group" ? chatSnapshot.id : chatSnapshot.username
  };
  pendingUploads.push(item);
  renderRoomMessagesMerged(lastServerMsgsCache, true);

  const formData = new FormData();
  formData.append("video", file);
  formData.append("from", me.username);
  formData.append("chatType", chatSnapshot.type);
  if (chatSnapshot.type === "group") {
    formData.append("groupId", chatSnapshot.id);
  } else {
    formData.append("to", chatSnapshot.username);
  }
  formData.append("text", text || "");
  if (rep) {
    formData.append("replyTo", JSON.stringify(rep));
  }

  const xhr = new XMLHttpRequest();
  xhr.open("POST", "/api/upload-video", true);
  xhr.withCredentials = true;

  xhr.upload.onprogress = (ev) => {
    if (ev.lengthComputable && ev.total > 0) {
      const pct = Math.min(99, Math.max(1, Math.round((ev.loaded / ev.total) * 100)));
      item.progress = pct;
      renderRoomMessagesMerged(lastServerMsgsCache, false);
    }
  };

  const finishUpload = async (ok, errMsg = "") => {
    if (ok) {
      item.progress = 100;
      renderRoomMessagesMerged(lastServerMsgsCache, false);
    }
    pendingUploads = pendingUploads.filter(x => x.id !== uploadId);
    if (!ok && errMsg) {
      alert(errMsg);
    }
    lastRenderedMsgsJson = "";
    await refreshMessages(true);
    await refreshChats();
  };

  xhr.onload = () => {
    try {
      const resp = JSON.parse(xhr.responseText || "{}");
      if (xhr.status >= 200 && xhr.status < 300 && resp.ok) {
        finishUpload(true);
      } else {
        finishUpload(false, resp.error || "Ошибка при загрузке видео");
      }
    } catch (e) {
      finishUpload(false, "Сервер вернул некорректный ответ при загрузке видео");
    }
  };

  xhr.onerror = () => {
    finishUpload(false, "Сбой сети при отправке видео");
  };

  xhr.send(formData);
}

async function sendMsg() {
  if (!me || !activeChat) return;
  const inp = document.getElementById("msgInput");
  const text = inp.value.trim();
  const image = pendingImageData;
  const videoFile = pendingVideoFile;
  const rep = replyTarget ? { ...replyTarget } : null;
  if (!text && !image && !videoFile) return;

  if (videoFile) {
    const chatSnap = { ...activeChat };
    playSound("send");
    inp.value = "";
    autoResizeComposer();
    clearReplyTarget();
    clearAttachment();
    inp.focus();
    uploadVideoWithProgress(videoFile, chatSnap, text, rep);
    return;
  }

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
    renderAvatarElement(document.getElementById("myAva"), me.emoji, me.avatarPhoto, me.username);
    syncAvatarPickerUi();
    refreshChats();
  }
}

async function changeNick() {
  const errEl = document.getElementById("setErr");
  const okEl = document.getElementById("setOk");
  errEl.textContent = "";
  okEl.textContent = "";
  const nextNick = document.getElementById("newNickInput").value.trim();
  if (!nextNick) {
    errEl.textContent = "Введите новый никнейм";
    return;
  }
  const res = await api("/api/change-nick", { oldUsername: me.username, newUsername: nextNick });
  if (!res.ok) {
    errEl.textContent = res.error || "Не удалось сменить никнейм";
    return;
  }
  playSound("react");
  saveLocalUser(res.user);
  renderAvatarElement(document.getElementById("myAva"), me.emoji, me.avatarPhoto, me.username);
  document.getElementById("myNick").innerHTML = nickLabelHtml(me.username, me.isOwner);
  syncAvatarPickerUi();
  const cd = getNickCooldownInfo(me);
  document.getElementById("nickHint").textContent = cd.canChange
    ? "Никнейм можно менять раз в 7 дней. Сейчас смена доступна!"
    : `Сменить никнейм можно раз в 7 дней. Осталось: ${cd.daysLeft} дн. ${cd.hoursLeft} ч.`;
  document.getElementById("newNickInput").disabled = !cd.canChange;
  document.getElementById("btnSaveNick").disabled = !cd.canChange;
  okEl.textContent = "Никнейм обновлён!";
  refreshChats();
}

async function logout() {
  playSound("leave");
  if (callState) await hangupCall(true);
  if (me) await api("/api/logout", { username: me.username });
  saveLocalUser(null);
  activeChat = null;
  activeGroupCallInfo = null;
  replyTarget = null;
  roomHistoryInitialized = false;
  clearAttachment();
  lastRenderedMsgsJson = "";
  persistSessionState();
  stopPolling();
  showScreen("authScreen");
}

async function deleteAccount() {
  if (!me) return;
  playSound("leave");
  const targetNick = me.username;
  if (callState) await hangupCall(false);
  const res = await api("/api/delete-account", { username: targetNick });
  if (!res.ok) {
    document.getElementById("setErr").textContent = res.error || "Не удалось удалить аккаунт";
    return;
  }
  saveLocalUser(null);
  activeChat = null;
  activeGroupCallInfo = null;
  replyTarget = null;
  roomHistoryInitialized = false;
  clearAttachment();
  lastRenderedMsgsJson = "";
  persistSessionState();
  stopPolling();
  showScreen("authScreen");
}

function startPolling() {
  stopPolling();
  pollTimer = setInterval(async () => {
    if (!me) return;
    if (activeChat && currentScreenId === "roomScreen") {
      await refreshMessages(false);
    }
    if (currentScreenId === "chatsScreen" || (activeChat && isDesktopLayoutActive())) {
      await refreshChats();
    }
    if (currentScreenId === "directChatSettingsScreen" && activeChat && activeChat.type === "direct") {
      await refreshDirectPeerOnlineStatus();
    }
    await pollCallStatus();
  }, 1400);
}

function stopPolling() {
  if (pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  }[c]));
}

function escAttr(s) {
  return String(s ?? "").replace(/\\/g, "\\\\").replace(/'/g, "\\'").replace(/"/g, "&quot;");
}

(async function init() {
  renderAvatarChoices();
  applyCustom(loadCustom());
  showScreen("loadingScreen");

  const splashDonePromise = new Promise(resolve => setTimeout(resolve, SPLASH_DURATION_MS));

  let savedNick = "";
  try { savedNick = localStorage.getItem("grusha_nick") || ""; } catch (e) {}
  let sessionUser = null;
  if (savedNick) {
    const res = await api(`/api/me?username=${encodeURIComponent(savedNick)}`);
    if (res.ok && res.user) sessionUser = res.user;
    else saveLocalUser(null);
  }

  await splashDonePromise;

  if (sessionUser) {
    saveLocalUser(sessionUser);
    await enterChats(true);
  } else {
    showScreen("authScreen");
  }
})();
</script>
</body>
</html>
"""


def get_or_create_direct_call(caller, callee):
    now = int(time.time() * 1000)
    for cid, c in ACTIVE_CALLS.items():
        if c.get("status") != "ended" and {c.get("caller"), c.get("callee")} == {caller, callee}:
            return c
    cid = f"call_{uuid.uuid4().hex[:12]}"
    caller_u = get_user(caller)
    caller_emoji = sanitize_user_emoji(caller_u.get("emoji")) if caller_u else "🙂"
    caller_photo = sanitize_avatar_photo(caller_u.get("avatarPhoto", caller_u.get("avatar_photo", "")), caller) if caller_u else ""
    c = {
        "callId": cid,
        "caller": caller,
        "callee": callee,
        "callerEmoji": caller_emoji,
        "callerAvatarPhoto": caller_photo,
        "status": "ringing",
        "offer": None,
        "offerBy": "",
        "answer": None,
        "answerBy": "",
        "callerCamOn": False,
        "calleeCamOn": False,
        "candidates": [],
        "createdAt": now,
        "updatedAt": now,
        "connectedAt": None,
        "endedBy": "",
        "logged": False,
    }
    ACTIVE_CALLS[cid] = c
    return c


def get_or_create_group_call(group_id):
    now = int(time.time() * 1000)
    gc = GROUP_CALLS.get(group_id)
    if not gc:
        gc = {
            "groupId": group_id,
            "participants": [],  # list of {"username": ..., "emoji": ..., "avatarPhoto": ..., "camOn": bool}
            "signals": [],       # list of {"id": ..., "from": ..., "to": ..., "kind": ..., "payload": ...}
            "createdAt": now,
            "startedAt": now,
            "updatedAt": now,
        }
        GROUP_CALLS[group_id] = gc
    return gc


@app.route("/")
def index():
    resp = make_response(render_template_string(HTML_PAGE))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    return resp


@app.route("/uploads/<path:filename>")
def serve_upload(filename):
    safe_name = os.path.basename(filename)
    return send_from_directory(UPLOAD_DIR, safe_name, conditional=True)


@app.route("/api/upload-video", methods=["POST"])
def api_upload_video():
    sender = (request.form.get("from") or "").strip()
    touch_user_online(sender)
    chat_type = (request.form.get("chatType") or "direct").strip()
    to_user = (request.form.get("to") or "").strip()
    group_id = (request.form.get("groupId") or "").strip()
    text = (request.form.get("text") or "").strip()
    raw_reply = request.form.get("replyTo") or ""
    rep = None
    if raw_reply:
        try:
            rep = sanitize_reply_to(json.loads(raw_reply))
        except Exception:
            rep = None

    file = request.files.get("video")
    if not sender or not get_user(sender):
        return jsonify({"ok": False, "error": "Аккаунт не найден"}), 400
    if not file or not file.filename:
        return jsonify({"ok": False, "error": "Видеофайл не выбран"}), 400

    orig_name = secure_filename(file.filename)
    ext = os.path.splitext(orig_name)[1].lower()
    if ext not in (".mp4", ".webm", ".mov", ".m4v", ".3gp", ".mkv"):
        ext = ".mp4"

    file.seek(0, os.SEEK_END)
    size_bytes = file.tell()
    file.seek(0)
    if size_bytes <= 0:
        return jsonify({"ok": False, "error": "Пустой видеофайл"}), 400
    if size_bytes > MAX_VIDEO_BYTES:
        return jsonify({"ok": False, "error": "Видео превышает лимит 25 МБ"}), 400

    stored_name = f"vid_{uuid.uuid4().hex[:14]}{ext}"
    save_path = os.path.join(UPLOAD_DIR, stored_name)
    file.save(save_path)
    video_url = f"/uploads/{stored_name}"
    ts = int(time.time() * 1000)

    if chat_type == "group":
        grp = get_group_by_id(group_id)
        if not grp or sender not in grp["members"]:
            return jsonify({"ok": False, "error": "Группа не найдена"}), 404
        if sender in grp["muted"] and sender != grp["owner"]:
            return jsonify({"ok": False, "error": "Вам запрещено писать в этой группе", "muted": True}), 403

        if USE_PG:
            with get_pg_conn() as conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute(
                        "INSERT INTO group_messages (group_id, sender, text, image, video, ts, reply_to_json, reactions_json) VALUES (%s, %s, %s, '', %s, %s, %s, '{}') RETURNING id",
                        (group_id, sender, text, video_url, ts, json.dumps(rep, ensure_ascii=False) if rep else ""),
                    )
                    new_id = cur.fetchone()["id"]
        else:
            db = load_json_db()
            msg_item = {
                "id": f"g_{uuid.uuid4().hex[:10]}",
                "from": sender,
                "text": text,
                "video": video_url,
                "ts": ts,
                "reactions": {},
            }
            if rep:
                msg_item["replyTo"] = rep
            db["group_msgs"].setdefault(group_id, []).append(msg_item)
            save_json_db(db)
            new_id = msg_item["id"]
        return jsonify({"ok": True, "id": new_id, "video": video_url})

    if not to_user or not get_user(to_user):
        return jsonify({"ok": False, "error": "Получатель не найден"}), 400

    if USE_PG:
        with get_pg_conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    "INSERT INTO messages (chat_key, sender, text, image, video, ts, reply_to_json, reactions_json) VALUES (%s, %s, %s, '', %s, %s, %s, '{}') RETURNING id",
                    (chat_key(sender, to_user), sender, text, video_url, ts, json.dumps(rep, ensure_ascii=False) if rep else ""),
                )
                new_id = cur.fetchone()["id"]
    else:
        db = load_json_db()
        msg_item = {
            "id": f"d_{uuid.uuid4().hex[:10]}",
            "from": sender,
            "text": text,
            "video": video_url,
            "ts": ts,
            "reactions": {},
        }
        if rep:
            msg_item["replyTo"] = rep
        db["chats"].setdefault(chat_key(sender, to_user), []).append(msg_item)
        save_json_db(db)
        new_id = msg_item["id"]

    return jsonify({"ok": True, "id": new_id, "video": video_url})


@app.route("/api/user/online")
def api_user_online():
    uname = (request.args.get("username") or "").strip()
    if not uname:
        return jsonify({"ok": False, "online": False})
    return jsonify({"ok": True, "username": uname, "online": is_user_online(uname)})


@app.route("/api/register", methods=["POST"])
def api_register():
    data = request.get_json(silent=True) or {}
    u = (data.get("username") or "").strip()
    p = data.get("password") or ""
    if len(u) < 2 or len(u) > 24:
        return jsonify({"ok": False, "error": "Никнейм должен быть от 2 до 24 символов"})
    if len(p) < 3:
        return jsonify({"ok": False, "error": "Пароль должен быть не короче 3 символов"})
    if get_user(u):
        return jsonify({"ok": False, "error": "Такой никнейм уже занят"})

    now = int(time.time() * 1000)
    if USE_PG:
        with get_pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO users (username, password, emoji, avatar_photo, created_at, last_nick_change_at) VALUES (%s, %s, '🍐', '', %s, %s)",
                    (u, p, now, now),
                )
    else:
        db = load_json_db()
        db["users"][u] = {
            "username": u,
            "password": p,
            "emoji": "🍐",
            "avatarPhoto": "",
            "createdAt": now,
            "lastNickChangeAt": now,
        }
        save_json_db(db)

    touch_user_online(u)
    return jsonify({"ok": True, "user": format_user(get_user(u))})


@app.route("/api/login", methods=["POST"])
def api_login():
    data = request.get_json(silent=True) or {}
    u = (data.get("username") or "").strip()
    p = data.get("password") or ""
    row = get_user(u)
    if not row or row["password"] != p:
        return jsonify({"ok": False, "error": "Неверный никнейм или пароль"})
    touch_user_online(u)
    return jsonify({"ok": True, "user": format_user(row)})


@app.route("/api/logout", methods=["POST"])
def api_logout():
    data = request.get_json(silent=True) or {}
    u = (data.get("username") or "").strip()
    if u:
        ONLINE_USERS.pop(u, None)
        TYPING_STATUS.pop(u, None)
        end_user_calls(u)
    return jsonify({"ok": True})


@app.route("/api/me")
def api_me():
    u = (request.args.get("username") or "").strip()
    row = get_user(u)
    if not row:
        return jsonify({"ok": False})
    touch_user_online(u)
    return jsonify({"ok": True, "user": format_user(row)})


@app.route("/api/emoji", methods=["POST"])
def api_emoji():
    data = request.get_json(silent=True) or {}
    u = (data.get("username") or "").strip()
    touch_user_online(u)
    em = sanitize_user_emoji(data.get("emoji"))
    if not get_user(u):
        return jsonify({"ok": False})
    if USE_PG:
        with get_pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE users SET emoji = %s WHERE username = %s", (em, u))
    else:
        db = load_json_db()
        db["users"][u]["emoji"] = em
        save_json_db(db)
    return jsonify({"ok": True, "emoji": em})


@app.route("/api/avatar-photo", methods=["POST"])
def api_avatar_photo():
    data = request.get_json(silent=True) or {}
    u = (data.get("username") or "").strip()
    touch_user_online(u)
    if not u or not get_user(u):
        return jsonify({"ok": False, "error": "Пользователь не найден"})
    if not can_use_photo_avatar(u):
        return jsonify({"ok": False, "error": "Фотография на аватарке недоступна для этого аккаунта"})

    raw_photo = data.get("avatarPhoto")
    if raw_photo is None:
        raw_photo = data.get("avatar_photo") or ""
    clean_photo = sanitize_avatar_photo(raw_photo, u)
    if raw_photo and not clean_photo:
        return jsonify({"ok": False, "error": "Некорректное изображение или слишком большой файл"})

    if USE_PG:
        with get_pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE users SET avatar_photo = %s WHERE username = %s", (clean_photo, u))
    else:
        db = load_json_db()
        db["users"][u]["avatarPhoto"] = clean_photo
        save_json_db(db)

    return jsonify({"ok": True, "avatarPhoto": clean_photo})


@app.route("/api/change-nick", methods=["POST"])
def api_change_nick():
    data = request.get_json(silent=True) or {}
    old_u = (data.get("oldUsername") or "").strip()
    touch_user_online(old_u)
    new_u = (data.get("newUsername") or "").strip()
    if len(new_u) < 2 or len(new_u) > 24:
        return jsonify({"ok": False, "error": "Никнейм должен быть от 2 до 24 символов"})
    row = get_user(old_u)
    if not row:
        return jsonify({"ok": False, "error": "Аккаунт не найден"})
    if old_u == new_u:
        return jsonify({"ok": True, "user": format_user(row)})
    if get_user(new_u):
        return jsonify({"ok": False, "error": "Этот никнейм уже занят"})

    now = int(time.time() * 1000)
    created_at = int(row.get("createdAt", row.get("created_at", now)) or now)
    last_change = int(row.get("lastNickChangeAt", row.get("last_nick_change_at", created_at)) or created_at)
    if last_change and (now - last_change) < SEVEN_DAYS_MS:
        rem = SEVEN_DAYS_MS - (now - last_change)
        days_left = rem // 86400000
        hours_left = max(1, (rem % 86400000 + 3599999) // 3600000)
        return jsonify({"ok": False, "error": f"Сменить никнейм можно раз в 7 дней. Осталось: {days_left} дн. {hours_left} ч."})

    em = sanitize_user_emoji(row.get("emoji"))
    av_photo = sanitize_avatar_photo(row.get("avatarPhoto", row.get("avatar_photo", "")), new_u)
    pw = row["password"]

    if USE_PG:
        with get_pg_conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    "INSERT INTO users (username, password, emoji, avatar_photo, created_at, last_nick_change_at) VALUES (%s, %s, %s, %s, %s, %s)",
                    (new_u, pw, em, av_photo, created_at, now),
                )
                cur.execute("UPDATE messages SET sender = %s WHERE sender = %s", (new_u, old_u))
                cur.execute("SELECT id, chat_key FROM messages WHERE chat_key LIKE %s", (f"%{old_u}%",))
                rows = cur.fetchall()
                for r in rows:
                    parts = r["chat_key"].split("::")
                    if old_u in parts:
                        new_key = chat_key(*[new_u if x == old_u else x for x in parts])
                        cur.execute("UPDATE messages SET chat_key = %s WHERE id = %s", (new_key, r["id"]))
                cur.execute("DELETE FROM users WHERE username = %s", (old_u,))
    else:
        db = load_json_db()
        db["users"][new_u] = {
            "username": new_u,
            "password": pw,
            "emoji": em,
            "avatarPhoto": av_photo,
            "createdAt": created_at,
            "lastNickChangeAt": now,
        }
        new_chats = {}
        for ck, msgs in db["chats"].items():
            parts = ck.split("::")
            updated_msgs = []
            for m in msgs:
                m_copy = dict(m)
                if m_copy.get("from") == old_u:
                    m_copy["from"] = new_u
                updated_msgs.append(m_copy)
            if old_u in parts:
                nk = chat_key(*[new_u if x == old_u else x for x in parts])
                new_chats.setdefault(nk, []).extend(updated_msgs)
            else:
                new_chats.setdefault(ck, []).extend(updated_msgs)
        db["chats"] = new_chats
        db["users"].pop(old_u, None)
        save_json_db(db)

    touch_user_online(new_u)
    return jsonify({"ok": True, "user": format_user(get_user(new_u))})


@app.route("/api/delete-account", methods=["POST"])
def api_delete_account():
    data = request.get_json(silent=True) or {}
    u = (data.get("username") or "").strip()
    if not u or not get_user(u):
        return jsonify({"ok": False, "error": "Аккаунт не найден"})

    ONLINE_USERS.pop(u, None)
    TYPING_STATUS.pop(u, None)
    end_user_calls(u)

    if USE_PG:
        with get_pg_conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("DELETE FROM messages WHERE sender = %s", (u,))
                cur.execute("SELECT id, chat_key FROM messages WHERE chat_key LIKE %s", (f"%{u}%",))
                for r in cur.fetchall():
                    parts = r["chat_key"].split("::")
                    if u in parts:
                        cur.execute("DELETE FROM messages WHERE id = %s", (r["id"],))
                cur.execute("DELETE FROM users WHERE username = %s", (u,))
    else:
        db = load_json_db()
        db["users"].pop(u, None)
        new_chats = {}
        for ck, msgs in db["chats"].items():
            parts = ck.split("::")
            if u in parts:
                continue
            filtered = [m for m in msgs if m.get("from") != u]
            new_chats[ck] = filtered
        db["chats"] = new_chats
        save_json_db(db)

    return jsonify({"ok": True})


@app.route("/api/search")
def api_search():
    q = (request.args.get("q") or "").strip().lower()
    ex = (request.args.get("exclude") or "").strip()
    touch_user_online(ex)
    if not q:
        return jsonify({"users": []})

    matched = []
    if USE_PG:
        with get_pg_conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT * FROM users WHERE LOWER(username) LIKE %s ORDER BY username LIMIT 30", (q + "%",))
                for r in cur.fetchall():
                    if r["username"] != ex:
                        matched.append(format_user(r))
    else:
        db = load_json_db()
        for uname, r in db["users"].items():
            if uname != ex and uname.lower().startswith(q):
                matched.append(format_user(r))
        matched.sort(key=lambda x: x["username"])

    return jsonify({"users": matched[:30]})


@app.route("/api/chats")
def api_chats():
    u = (request.args.get("user") or "").strip()
    touch_user_online(u)
    if not u:
        return jsonify({"ok": True, "chats": []})

    peers_last = {}
    if USE_PG:
        with get_pg_conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT chat_key, sender, text, image, video, ts FROM messages WHERE chat_key LIKE %s ORDER BY ts ASC, id ASC", (f"%{u}%",))
                for r in cur.fetchall():
                    parts = r["chat_key"].split("::")
                    if len(parts) == 2 and u in parts:
                        peer = parts[1] if parts[0] == u else parts[0]
                        if r["video"]:
                            preview = r["text"] or "🎬 Видео"
                        elif r["image"]:
                            preview = r["text"] or "📷 Картинка"
                        else:
                            preview = r["text"]
                        peers_last[peer] = {"last": preview, "ts": int(r["ts"] or 0)}
    else:
        db = load_json_db()
        for ck, msgs in db["chats"].items():
            parts = ck.split("::")
            if len(parts) == 2 and u in parts:
                peer = parts[1] if parts[0] == u else parts[0]
                if msgs:
                    last_m = msgs[-1]
                    if last_m.get("video"):
                        preview = last_m.get("text") or "🎬 Видео"
                    elif last_m.get("image"):
                        preview = last_m.get("text") or "📷 Картинка"
                    else:
                        preview = last_m.get("text", "")
                    peers_last[peer] = {"last": preview, "ts": int(last_m.get("ts") or 0)}

    items = []
    for peer, info in peers_last.items():
        urow = get_user(peer)
        em = sanitize_user_emoji(urow.get("emoji")) if urow else "🍐"
        av_photo = sanitize_avatar_photo(urow.get("avatarPhoto", urow.get("avatar_photo", "")), peer) if urow else ""
        items.append({
            "type": "direct",
            "username": peer,
            "emoji": em,
            "avatarPhoto": av_photo,
            "isOwner": is_owner_nick(peer),
            "last": info["last"],
            "ts": info["ts"],
            "typing": is_peer_typing(peer, u),
            "online": is_user_online(peer),
        })

    if USE_PG:
        with get_pg_conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT * FROM groups ORDER BY created_at DESC")
                g_rows = cur.fetchall()
        for gr in g_rows:
            g = format_group_row(gr)
            if u in g["members"]:
                gid = g["id"]
                last_txt = ""
                last_ts = g["createdAt"]
                with get_pg_conn() as conn:
                    with conn.cursor(cursor_factory=RealDictCursor) as cur:
                        cur.execute("SELECT sender, text, image, video, ts FROM group_messages WHERE group_id = %s ORDER BY ts DESC, id DESC LIMIT 1", (gid,))
                        m_row = cur.fetchone()
                        if m_row:
                            if m_row["video"]:
                                prev = m_row["text"] or "🎬 Видео"
                            elif m_row["image"]:
                                prev = m_row["text"] or "📷 Картинка"
                            else:
                                prev = m_row["text"]
                            last_txt = f"{m_row['sender']}: {prev}" if m_row["sender"] != "system" else prev
                            last_ts = int(m_row["ts"] or last_ts)
                typers = get_group_typing_users(gid, u)
                items.append({
                    "type": "group",
                    "groupId": gid,
                    "title": g["title"],
                    "emoji": g["emoji"],
                    "owner": g["owner"],
                    "callAccess": g["callAccess"],
                    "tripleBadge": g["tripleBadge"],
                    "last": last_txt,
                    "ts": last_ts,
                    "typingText": ", ".join(typers),
                })
    else:
        db = load_json_db()
        for gid, g in db["groups"].items():
            members = g.get("members") or []
            if u in members:
                msgs = db["group_msgs"].get(gid) or []
                last_txt = ""
                last_ts = int(g.get("createdAt") or 0)
                if msgs:
                    lm = msgs[-1]
                    if lm.get("video"):
                        prev = lm.get("text") or "🎬 Видео"
                    elif lm.get("image"):
                        prev = lm.get("text") or "📷 Картинка"
                    else:
                        prev = lm.get("text", "")
                    last_txt = f"{lm.get('from')}: {prev}" if lm.get("from") != "system" else prev
                    last_ts = int(lm.get("ts") or last_ts)
                typers = get_group_typing_users(gid, u)
                items.append({
                    "type": "group",
                    "groupId": gid,
                    "title": g["title"],
                    "emoji": normalize_group_emoji(g.get("emoji")),
                    "owner": g.get("owner", ""),
                    "callAccess": sanitize_call_access(g.get("callAccess")),
                    "tripleBadge": is_triple_badge_group(g["title"]),
                    "last": last_txt,
                    "ts": last_ts,
                    "typingText": ", ".join(typers),
                })

    items.sort(key=lambda x: x["ts"], reverse=True)
    return jsonify({"ok": True, "chats": items})


@app.route("/api/chats/delete", methods=["POST"])
def api_delete_chat():
    data = request.get_json(silent=True) or {}
    a = (data.get("a") or "").strip()
    touch_user_online(a)
    b = (data.get("b") or "").strip()
    if not a or not b:
        return jsonify({"ok": False})
    ck = chat_key(a, b)
    if USE_PG:
        with get_pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM messages WHERE chat_key = %s", (ck,))
    else:
        db = load_json_db()
        db["chats"].pop(ck, None)
        save_json_db(db)
    return jsonify({"ok": True})


@app.route("/api/groups/create", methods=["POST"])
def api_create_group():
    data = request.get_json(silent=True) or {}
    owner = (data.get("owner") or "").strip()
    touch_user_online(owner)
    title = (data.get("title") or "").strip()
    emoji = normalize_group_emoji(data.get("emoji"))
    raw_members = data.get("members") or []
    if not owner or not get_user(owner):
        return jsonify({"ok": False, "error": "Аккаунт владельца не найден"})
    if not title or len(title) > 40:
        return jsonify({"ok": False, "error": "Название группы должно быть от 1 до 40 символов"})
    if is_group_title_taken(title):
        return jsonify({"ok": False, "error": "Группа с таким названием уже существует. Придумай другое название."})

    members = [owner]
    for m in raw_members:
        nick = str(m or "").strip()
        if nick and nick not in members and get_user(nick):
            members.append(nick)

    gid = f"grp_{uuid.uuid4().hex[:10]}"
    now = int(time.time() * 1000)

    if USE_PG:
        with get_pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO groups (id, title, emoji, owner, members_json, muted_json, call_access, created_at) VALUES (%s, %s, %s, %s, %s, '[]', 'all', %s)",
                    (gid, title, emoji, owner, json.dumps(members, ensure_ascii=False), now),
                )
    else:
        db = load_json_db()
        db["groups"][gid] = {
            "id": gid,
            "title": title,
            "emoji": emoji,
            "owner": owner,
            "members": members,
            "muted": [],
            "callAccess": "all",
            "createdAt": now,
        }
        db["group_msgs"].setdefault(gid, [])
        save_json_db(db)

    return jsonify({"ok": True, "group": get_group_by_id(gid)})


@app.route("/api/groups/call-access", methods=["POST"])
def api_group_call_access():
    data = request.get_json(silent=True) or {}
    actor = (data.get("actor") or "").strip()
    touch_user_online(actor)
    gid = (data.get("groupId") or "").strip()
    mode = sanitize_call_access(data.get("callAccess"))
    grp = get_group_by_id(gid)
    if not grp:
        return jsonify({"ok": False, "error": "Группа не найдена"}), 404
    if actor != grp["owner"]:
        return jsonify({"ok": False, "error": "Менять настройки звонков может только владелец"}), 403

    if USE_PG:
        with get_pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE groups SET call_access = %s WHERE id = %s", (mode, gid))
    else:
        db = load_json_db()
        if gid in db["groups"]:
            db["groups"][gid]["callAccess"] = mode
            save_json_db(db)

    return jsonify({"ok": True, "callAccess": mode})


@app.route("/api/groups/update", methods=["POST"])
def api_update_group():
    data = request.get_json(silent=True) or {}
    actor = (data.get("actor") or "").strip()
    touch_user_online(actor)
    gid = (data.get("groupId") or "").strip()
    new_title = (data.get("title") or "").strip()
    new_emoji = normalize_group_emoji(data.get("emoji"))

    grp = get_group_by_id(gid)
    if not grp:
        return jsonify({"ok": False, "error": "Группа не найдена"}), 404
    if actor != grp["owner"]:
        return jsonify({"ok": False, "error": "Изменять группу может только владелец"}), 403
    if not new_title or len(new_title) > 40:
        return jsonify({"ok": False, "error": "Название группы должно быть от 1 до 40 символов"})
    if is_group_title_taken(new_title, exclude_group_id=gid):
        return jsonify({"ok": False, "error": "Группа с таким названием уже существует. Придумай другое название."})

    if USE_PG:
        with get_pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE groups SET title = %s, emoji = %s WHERE id = %s", (new_title, new_emoji, gid))
    else:
        db = load_json_db()
        if gid in db["groups"]:
            db["groups"][gid]["title"] = new_title
            db["groups"][gid]["emoji"] = new_emoji
            save_json_db(db)

    return jsonify({"ok": True, "group": get_group_by_id(gid)})


@app.route("/api/groups/add-members", methods=["POST"])
def api_group_add_members():
    data = request.get_json(silent=True) or {}
    actor = (data.get("actor") or "").strip()
    touch_user_online(actor)
    gid = (data.get("groupId") or "").strip()
    raw_members = data.get("members") or []

    grp = get_group_by_id(gid)
    if not grp:
        return jsonify({"ok": False, "error": "Группа не найдена"}), 404
    if actor != grp["owner"]:
        return jsonify({"ok": False, "error": "Добавлять участников может только владелец"}), 403

    members = list(grp["members"])
    added = []
    for m in raw_members:
        nick = str(m or "").strip()
        if nick and nick not in members and get_user(nick):
            members.append(nick)
            added.append(nick)

    if not added:
        return jsonify({"ok": False, "error": "Нет новых пользователей для добавления"})

    if USE_PG:
        with get_pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE groups SET members_json = %s WHERE id = %s", (json.dumps(members, ensure_ascii=False), gid))
    else:
        db = load_json_db()
        if gid in db["groups"]:
            db["groups"][gid]["members"] = members
            save_json_db(db)

    for nick in added:
        append_group_system_message(gid, f"{nick} зашел", "join")

    return jsonify({"ok": True, "added": added, "group": get_group_by_id(gid)})


@app.route("/api/groups/info")
def api_group_info():
    gid = (request.args.get("groupId") or "").strip()
    user = (request.args.get("user") or "").strip()
    touch_user_online(user)
    grp = get_group_by_id(gid)
    if not grp:
        return jsonify({"ok": False, "deleted": True})
    if user and user not in grp["members"]:
        return jsonify({"ok": False, "kicked": True})
    return jsonify({"ok": True, "group": grp})


@app.route("/api/groups/leave", methods=["POST"])
def api_group_leave():
    data = request.get_json(silent=True) or {}
    actor = (data.get("actor") or "").strip()
    touch_user_online(actor)
    gid = (data.get("groupId") or "").strip()
    grp = get_group_by_id(gid)
    if not grp:
        return jsonify({"ok": False, "error": "Группа не найдена"}), 404
    if actor == grp["owner"]:
        return jsonify({"ok": False, "error": "Владелец не может выйти из своей группы, он может её удалить"}), 400
    if actor not in grp["members"]:
        return jsonify({"ok": True})

    members = [m for m in grp["members"] if m != actor]
    muted = [m for m in grp["muted"] if m != actor]

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

    append_group_system_message(gid, f"{actor} вышел", "leave")
    return jsonify({"ok": True})


@app.route("/api/groups/mute", methods=["POST"])
def api_group_mute():
    data = request.get_json(silent=True) or {}
    actor = (data.get("actor") or "").strip()
    touch_user_online(actor)
    gid = (data.get("groupId") or "").strip()
    target = (data.get("target") or "").strip()
    grp = get_group_by_id(gid)
    if not grp:
        return jsonify({"ok": False, "error": "Группа не найдена"}), 404
    if actor != grp["owner"]:
        return jsonify({"ok": False, "error": "Только владелец может управлять правами"}), 403
    if target == grp["owner"] or target not in grp["members"]:
        return jsonify({"ok": False, "error": "Некорректный участник"}), 400

    muted = list(grp["muted"])
    if target in muted:
        muted.remove(target)
        is_muted = False
    else:
        muted.append(target)
        is_muted = True

    if USE_PG:
        with get_pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE groups SET muted_json = %s WHERE id = %s", (json.dumps(muted, ensure_ascii=False), gid))
    else:
        db = load_json_db()
        if gid in db["groups"]:
            db["groups"][gid]["muted"] = muted
            save_json_db(db)

    return jsonify({"ok": True, "muted": is_muted})


@app.route("/api/groups/kick", methods=["POST"])
def api_group_kick():
    data = request.get_json(silent=True) or {}
    actor = (data.get("actor") or "").strip()
    touch_user_online(actor)
    gid = (data.get("groupId") or "").strip()
    target = (data.get("target") or "").strip()
    grp = get_group_by_id(gid)
    if not grp:
        return jsonify({"ok": False, "error": "Группа не найдена"}), 404
    if actor != grp["owner"]:
        return jsonify({"ok": False, "error": "Только владелец может удалять участников"}), 403
    if target == grp["owner"]:
        return jsonify({"ok": False, "error": "Нельзя удалить владельца"}), 400

    members = [m for m in grp["members"] if m != target]
    muted = [m for m in grp["muted"] if m != target]

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

    append_group_system_message(gid, f"{target} вышел", "leave")
    return jsonify({"ok": True})


@app.route("/api/groups/delete", methods=["POST"])
def api_group_delete():
    data = request.get_json(silent=True) or {}
    actor = (data.get("actor") or "").strip()
    touch_user_online(actor)
    gid = (data.get("groupId") or "").strip()
    grp = get_group_by_id(gid)
    if not grp:
        return jsonify({"ok": False, "error": "Группа не найдена"}), 404
    if actor != grp["owner"]:
        return jsonify({"ok": False, "error": "Удалить группу может только владелец"}), 403

    GROUP_CALLS.pop(gid, None)
    if USE_PG:
        with get_pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM group_messages WHERE group_id = %s", (gid,))
                cur.execute("DELETE FROM groups WHERE id = %s", (gid,))
    else:
        db = load_json_db()
        db["groups"].pop(gid, None)
        db["group_msgs"].pop(gid, None)
        save_json_db(db)

    return jsonify({"ok": True})


@app.route("/api/groups/msgs")
def api_group_msgs():
    gid = (request.args.get("groupId") or "").strip()
    user = (request.args.get("user") or "").strip()
    touch_user_online(user)
    grp = get_group_by_id(gid)
    if not grp:
        return jsonify({"ok": False, "deleted": True})
    if user and user not in grp["members"]:
        return jsonify({"ok": False, "kicked": True})

    member_profiles = {}
    for m_nick in grp["members"]:
        urow = get_user(m_nick)
        if urow:
            member_profiles[m_nick] = {
                "emoji": sanitize_user_emoji(urow.get("emoji")),
                "avatarPhoto": sanitize_avatar_photo(urow.get("avatarPhoto", urow.get("avatar_photo", "")), m_nick),
            }

    msgs = []
    if USE_PG:
        with get_pg_conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    "SELECT id, sender, text, image, video, ts, reply_to_json, reactions_json FROM group_messages WHERE group_id = %s ORDER BY ts ASC, id ASC",
                    (gid,),
                )
                for r in cur.fetchall():
                    msgs.append(format_message_record(
                        r["id"],
                        r["sender"],
                        r["text"],
                        r["image"],
                        r["ts"],
                        parse_json_dict(r.get("reply_to_json")),
                        parse_json_dict(r.get("reactions_json")),
                        r.get("video") or "",
                    ))
    else:
        db = load_json_db()
        for raw in (db["group_msgs"].get(gid) or []):
            msgs.append(format_message_record(
                raw.get("id"),
                raw.get("from", ""),
                raw.get("text", ""),
                raw.get("image", ""),
                raw.get("ts", 0),
                raw.get("replyTo"),
                raw.get("reactions"),
                raw.get("video", ""),
            ))

    return jsonify({
        "ok": True,
        "msgs": msgs,
        "title": grp["title"],
        "emoji": grp["emoji"],
        "owner": grp["owner"],
        "callAccess": grp["callAccess"],
        "tripleBadge": grp["tripleBadge"],
        "isMuted": (user in grp["muted"]) and (user != grp["owner"]),
        "typingUsers": get_group_typing_users(gid, user),
        "memberProfiles": member_profiles,
    })


@app.route("/api/groups/send", methods=["POST"])
def api_group_send():
    data = request.get_json(silent=True) or {}
    sender = (data.get("from") or "").strip()
    touch_user_online(sender)
    gid = (data.get("groupId") or "").strip()
    text = (data.get("text") or "").strip()
    image = (data.get("image") or "").strip()
    rep = sanitize_reply_to(data.get("replyTo"))

    grp = get_group_by_id(gid)
    if not grp or sender not in grp["members"]:
        return jsonify({"ok": False, "error": "Группа не найдена"}), 404
    if sender in grp["muted"] and sender != grp["owner"]:
        return jsonify({"ok": False, "error": "Вам запрещено писать в этой группе", "muted": True}), 403
    if not text and not image:
        return jsonify({"ok": False})
    if len(image) > MAX_IMAGE_LEN:
        return jsonify({"ok": False, "error": "Картинка слишком большая"})

    TYPING_STATUS.pop((sender, group_typing_key(gid)), None)
    ts = int(time.time() * 1000)

    if USE_PG:
        with get_pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO group_messages (group_id, sender, text, image, video, ts, reply_to_json, reactions_json) VALUES (%s, %s, %s, %s, '', %s, %s, '{}')",
                    (gid, sender, text, image, ts, json.dumps(rep, ensure_ascii=False) if rep else ""),
                )
    else:
        db = load_json_db()
        item = {
            "id": f"g_{uuid.uuid4().hex[:10]}",
            "from": sender,
            "text": text,
            "ts": ts,
            "reactions": {},
        }
        if image:
            item["image"] = image
        if rep:
            item["replyTo"] = rep
        db["group_msgs"].setdefault(gid, []).append(item)
        save_json_db(db)

    return jsonify({"ok": True})


@app.route("/api/typing", methods=["POST"])
def api_typing():
    data = request.get_json(silent=True) or {}
    frm = (data.get("from") or "").strip()
    touch_user_online(frm)
    to = (data.get("to") or "").strip()
    gid = (data.get("groupId") or "").strip()
    if frm and gid:
        TYPING_STATUS[(frm, group_typing_key(gid))] = int(time.time() * 1000)
        return jsonify({"ok": True})
    if frm and to:
        TYPING_STATUS[(frm, to)] = int(time.time() * 1000)
    return jsonify({"ok": True})


@app.route("/api/msgs")
def api_msgs():
    a = (request.args.get("a") or "").strip()
    touch_user_online(a)
    b = (request.args.get("b") or "").strip()
    if not a or not b:
        return jsonify({"ok": True, "msgs": [], "peerEmoji": "🍐", "peerAvatarPhoto": "", "peerTyping": False, "peerOnline": False})

    ck = chat_key(a, b)
    msgs = []
    if USE_PG:
        with get_pg_conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    "SELECT id, sender, text, image, video, ts, reply_to_json, reactions_json FROM messages WHERE chat_key = %s ORDER BY ts ASC, id ASC",
                    (ck,),
                )
                for r in cur.fetchall():
                    msgs.append(format_message_record(
                        r["id"],
                        r["sender"],
                        r["text"],
                        r["image"],
                        r["ts"],
                        parse_json_dict(r.get("reply_to_json")),
                        parse_json_dict(r.get("reactions_json")),
                        r.get("video") or "",
                    ))
    else:
        db = load_json_db()
        for raw in (db["chats"].get(ck) or []):
            msgs.append(format_message_record(
                raw.get("id"),
                raw.get("from", ""),
                raw.get("text", ""),
                raw.get("image", ""),
                raw.get("ts", 0),
                raw.get("replyTo"),
                raw.get("reactions"),
                raw.get("video", ""),
            ))

    urow = get_user(b)
    peer_emoji = sanitize_user_emoji(urow.get("emoji")) if urow else "🍐"
    peer_photo = sanitize_avatar_photo(urow.get("avatarPhoto", urow.get("avatar_photo", "")), b) if urow else ""
    return jsonify({
        "ok": True,
        "msgs": msgs,
        "peerEmoji": peer_emoji,
        "peerAvatarPhoto": peer_photo,
        "peerTyping": is_peer_typing(b, a),
        "peerOnline": is_user_online(b),
    })


@app.route("/api/send", methods=["POST"])
def api_send():
    data = request.get_json(silent=True) or {}
    frm = (data.get("from") or "").strip()
    touch_user_online(frm)
    to = (data.get("to") or "").strip()
    text = (data.get("text") or "").strip()
    image = (data.get("image") or "").strip()
    rep = sanitize_reply_to(data.get("replyTo"))
    if not frm or not to or (not text and not image):
        return jsonify({"ok": False})
    if len(image) > MAX_IMAGE_LEN:
        return jsonify({"ok": False, "error": "Картинка слишком большая"})

    TYPING_STATUS.pop((frm, to), None)
    ck = chat_key(frm, to)
    ts = int(time.time() * 1000)

    if USE_PG:
        with get_pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO messages (chat_key, sender, text, image, video, ts, reply_to_json, reactions_json) VALUES (%s, %s, %s, %s, '', %s, %s, '{}')",
                    (ck, frm, text, image, ts, json.dumps(rep, ensure_ascii=False) if rep else ""),
                )
    else:
        db = load_json_db()
        item = {
            "id": f"d_{uuid.uuid4().hex[:10]}",
            "from": frm,
            "text": text,
            "ts": ts,
            "reactions": {},
        }
        if image:
            item["image"] = image
        if rep:
            item["replyTo"] = rep
        db["chats"].setdefault(ck, []).append(item)
        save_json_db(db)

    return jsonify({"ok": True})


@app.route("/api/msgs/delete", methods=["POST"])
def api_delete_msg():
    data = request.get_json(silent=True) or {}
    actor = (data.get("actor") or "").strip()
    touch_user_online(actor)
    msg_id = data.get("msgId")
    chat_type = (data.get("chatType") or "direct").strip()
    gid = (data.get("groupId") or "").strip()
    peer = (data.get("peer") or "").strip()

    if not actor or msg_id is None:
        return jsonify({"ok": False})

    if chat_type == "group":
        grp = get_group_by_id(gid)
        if not grp or actor != grp["owner"]:
            return jsonify({"ok": False, "error": "Удалять сообщения в группе может только владелец"}), 403
        if USE_PG:
            with get_pg_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM group_messages WHERE id = %s AND group_id = %s", (int(msg_id), gid))
        else:
            db = load_json_db()
            arr = db["group_msgs"].get(gid) or []
            db["group_msgs"][gid] = [m for m in arr if str(m.get("id")) != str(msg_id)]
            save_json_db(db)
        return jsonify({"ok": True})

    if not peer:
        return jsonify({"ok": False})
    ck = chat_key(actor, peer)
    if USE_PG:
        with get_pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM messages WHERE id = %s AND chat_key = %s", (int(msg_id), ck))
    else:
        db = load_json_db()
        arr = db["chats"].get(ck) or []
        db["chats"][ck] = [m for m in arr if str(m.get("id")) != str(msg_id)]
        save_json_db(db)
    return jsonify({"ok": True})


@app.route("/api/react", methods=["POST"])
def api_react():
    data = request.get_json(silent=True) or {}
    actor = (data.get("actor") or "").strip()
    touch_user_online(actor)
    msg_id = data.get("msgId")
    emoji = str(data.get("emoji") or "").strip()
    chat_type = (data.get("chatType") or "direct").strip()
    gid = (data.get("groupId") or "").strip()
    peer = (data.get("peer") or "").strip()

    if not actor or msg_id is None or not emoji:
        return jsonify({"ok": False})

    def toggle_on_dict(raw_dict):
        rx = normalize_reactions(raw_dict)
        users_list = list(rx.get(emoji) or [])
        if actor in users_list:
            users_list.remove(actor)
        else:
            users_list.append(actor)
        if users_list:
            rx[emoji] = users_list
        else:
            rx.pop(emoji, None)
        return rx

    if chat_type == "group":
        grp = get_group_by_id(gid)
        if not grp or actor not in grp["members"]:
            return jsonify({"ok": False})
        if USE_PG:
            with get_pg_conn() as conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute("SELECT reactions_json FROM group_messages WHERE id = %s AND group_id = %s", (int(msg_id), gid))
                    row = cur.fetchone()
                    if not row:
                        return jsonify({"ok": False})
                    updated = toggle_on_dict(parse_json_dict(row.get("reactions_json")))
                    cur.execute("UPDATE group_messages SET reactions_json = %s WHERE id = %s", (json.dumps(updated, ensure_ascii=False), int(msg_id)))
        else:
            db = load_json_db()
            arr = db["group_msgs"].get(gid) or []
            for m in arr:
                if str(m.get("id")) == str(msg_id):
                    m["reactions"] = toggle_on_dict(m.get("reactions"))
                    break
            save_json_db(db)
        return jsonify({"ok": True})

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
                updated = toggle_on_dict(parse_json_dict(row.get("reactions_json")))
                cur.execute("UPDATE messages SET reactions_json = %s WHERE id = %s", (json.dumps(updated, ensure_ascii=False), int(msg_id)))
    else:
        db = load_json_db()
        arr = db["chats"].get(ck) or []
        for m in arr:
            if str(m.get("id")) == str(msg_id):
                m["reactions"] = toggle_on_dict(m.get("reactions"))
                break
        save_json_db(db)
    return jsonify({"ok": True})


@app.route("/api/call/start", methods=["POST"])
def api_call_start():
    cleanup_stale_calls()
    data = request.get_json(silent=True) or {}
    caller = (data.get("caller") or "").strip()
    touch_user_online(caller)
    callee = (data.get("callee") or "").strip()
    if not caller or not callee or caller == callee:
        return jsonify({"ok": False, "error": "Некорректный вызов"})
    if not get_user(caller) or not get_user(callee):
        return jsonify({"ok": False, "error": "Пользователь не найден"})

    c = get_or_create_direct_call(caller, callee)
    return jsonify({"ok": True, "callId": c["callId"], "call": c})


@app.route("/api/call/offer", methods=["POST"])
def api_call_offer():
    cleanup_stale_calls()
    data = request.get_json(silent=True) or {}
    cid = (data.get("callId") or "").strip()
    frm = (data.get("from") or "").strip()
    touch_user_online(frm)
    offer = data.get("offer")
    c = ACTIVE_CALLS.get(cid)
    if not c or c.get("status") == "ended":
        return jsonify({"ok": False, "ended": True})
    c["offer"] = offer
    c["offerBy"] = frm
    c["updatedAt"] = int(time.time() * 1000)
    return jsonify({"ok": True})


@app.route("/api/call/answer", methods=["POST"])
def api_call_answer():
    cleanup_stale_calls()
    data = request.get_json(silent=True) or {}
    cid = (data.get("callId") or "").strip()
    frm = (data.get("from") or "").strip()
    touch_user_online(frm)
    answer = data.get("answer")
    c = ACTIVE_CALLS.get(cid)
    if not c or c.get("status") == "ended":
        return jsonify({"ok": False, "ended": True})
    now = int(time.time() * 1000)
    c["answer"] = answer
    c["answerBy"] = frm
    c["status"] = "active"
    if not c.get("connectedAt"):
        c["connectedAt"] = now
    c["updatedAt"] = now
    return jsonify({"ok": True})


@app.route("/api/call/cam-state", methods=["POST"])
def api_call_cam_state():
    cleanup_stale_calls()
    data = request.get_json(silent=True) or {}
    cid = (data.get("callId") or "").strip()
    frm = (data.get("from") or "").strip()
    touch_user_online(frm)
    cam_on = bool(data.get("camOn"))
    c = ACTIVE_CALLS.get(cid)
    if not c or c.get("status") == "ended":
        return jsonify({"ok": False, "ended": True})
    if frm == c.get("caller"):
        c["callerCamOn"] = cam_on
    elif frm == c.get("callee"):
        c["calleeCamOn"] = cam_on
    c["updatedAt"] = int(time.time() * 1000)
    return jsonify({"ok": True})


@app.route("/api/call/candidate", methods=["POST"])
def api_call_candidate():
    cleanup_stale_calls()
    data = request.get_json(silent=True) or {}
    cid = (data.get("callId") or "").strip()
    frm = (data.get("from") or "").strip()
    touch_user_online(frm)
    cand = data.get("candidate")
    c = ACTIVE_CALLS.get(cid)
    if not c or c.get("status") == "ended":
        return jsonify({"ok": False, "ended": True})
    if cand:
        c["candidates"].append({"from": frm, "candidate": cand})
        c["updatedAt"] = int(time.time() * 1000)
    return jsonify({"ok": True})


@app.route("/api/call/end", methods=["POST"])
def api_call_end():
    cleanup_stale_calls()
    data = request.get_json(silent=True) or {}
    cid = (data.get("callId") or "").strip()
    frm = (data.get("from") or "").strip()
    touch_user_online(frm)
    duration_str = (data.get("duration") or "00:00").strip()
    was_connected = bool(data.get("wasConnected"))

    c = ACTIVE_CALLS.get(cid)
    if not c:
        return jsonify({"ok": True, "ended": True})

    now = int(time.time() * 1000)
    already_logged = bool(c.get("logged"))
    c["status"] = "ended"
    c["endedBy"] = frm
    c["updatedAt"] = now
    c["logged"] = True

    if not already_logged and frm:
        peer = c["callee"] if frm == c["caller"] else c["caller"]
        if was_connected or c.get("connectedAt"):
            if not duration_str or duration_str == "00:00":
                sec = max(1, (now - int(c.get("connectedAt") or now)) // 1000)
                duration_str = format_duration_mmss(sec)
            append_direct_message(frm, peer, f"📞 Звонок завершён • длительность {duration_str}")
        else:
            append_direct_message(frm, peer, "📞 Звонок отклонён / пропущен")

    return jsonify({"ok": True, "ended": True})


@app.route("/api/call/poll")
def api_call_poll():
    cleanup_stale_calls()
    user = (request.args.get("user") or "").strip()
    touch_user_online(user)
    cid = (request.args.get("callId") or "").strip()
    if not user:
        return jsonify({"ok": False})

    now = int(time.time() * 1000)
    if cid:
        c = ACTIVE_CALLS.get(cid)
        if not c:
            return jsonify({"ok": True, "ended": True})
        c["updatedAt"] = now
        out = dict(c)
        out["peerCamOn"] = bool(c.get("calleeCamOn") if user == c.get("caller") else c.get("callerCamOn"))
        return jsonify({"ok": True, "ended": c.get("status") == "ended", "call": out})

    for _, c in ACTIVE_CALLS.items():
        if c.get("status") != "ended" and c.get("callee") == user:
            c["updatedAt"] = now
            out = dict(c)
            out["peerCamOn"] = bool(c.get("callerCamOn"))
            return jsonify({"ok": True, "ended": False, "call": out})

    return jsonify({"ok": True, "ended": False, "call": None})


@app.route("/api/group-call/join", methods=["POST"])
def api_group_call_join():
    cleanup_stale_calls()
    data = request.get_json(silent=True) or {}
    gid = (data.get("groupId") or "").strip()
    user = (data.get("user") or "").strip()
    touch_user_online(user)
    emoji = sanitize_user_emoji(data.get("emoji"))
    avatar_photo = sanitize_avatar_photo(data.get("avatarPhoto"), user)
    cam_on = bool(data.get("camOn"))

    grp = get_group_by_id(gid)
    if not grp or user not in grp["members"]:
        return jsonify({"ok": False, "error": "Группа не найдена"}), 404
    if not can_user_call_in_group(grp, user):
        if grp["callAccess"] == "nobody":
            return jsonify({"ok": False, "error": "Звонки в этой группе отключены"}), 403
        return jsonify({"ok": False, "error": "Звонить в этой группе может только владелец"}), 403

    gc = get_or_create_group_call(gid)
    now = int(time.time() * 1000)
    gc["updatedAt"] = now

    found = False
    for p in gc["participants"]:
        if p["username"] == user:
            p["emoji"] = emoji
            p["avatarPhoto"] = avatar_photo
            p["camOn"] = cam_on
            p["lastSeen"] = now
            found = True
            break
    if not found:
        if not gc["participants"]:
            gc["startedAt"] = now
        gc["participants"].append({
            "username": user,
            "emoji": emoji,
            "avatarPhoto": avatar_photo,
            "camOn": cam_on,
            "lastSeen": now,
        })

    return jsonify({
        "ok": True,
        "startedAt": gc["startedAt"],
        "participants": gc["participants"],
    })


@app.route("/api/group-call/media", methods=["POST"])
def api_group_call_media():
    cleanup_stale_calls()
    data = request.get_json(silent=True) or {}
    gid = (data.get("groupId") or "").strip()
    user = (data.get("user") or "").strip()
    touch_user_online(user)
    cam_on = bool(data.get("camOn"))
    gc = GROUP_CALLS.get(gid)
    if not gc:
        return jsonify({"ok": True})
    now = int(time.time() * 1000)
    gc["updatedAt"] = now
    for p in gc["participants"]:
        if p["username"] == user:
            p["camOn"] = cam_on
            p["lastSeen"] = now
            break
    return jsonify({"ok": True})


@app.route("/api/group-call/signal", methods=["POST"])
def api_group_call_signal():
    cleanup_stale_calls()
    data = request.get_json(silent=True) or {}
    gid = (data.get("groupId") or "").strip()
    frm = (data.get("from") or "").strip()
    touch_user_online(frm)
    to = (data.get("to") or "").strip()
    kind = (data.get("kind") or "").strip()
    payload = data.get("payload")

    gc = GROUP_CALLS.get(gid)
    if not gc or not frm or not to or not kind:
        return jsonify({"ok": False})

    gc["updatedAt"] = int(time.time() * 1000)
    gc["signals"].append({
        "id": uuid.uuid4().hex[:10],
        "from": frm,
        "to": to,
        "kind": kind,
        "payload": payload,
    })
    if len(gc["signals"]) > 300:
        gc["signals"] = gc["signals"][-300:]
    return jsonify({"ok": True})


@app.route("/api/group-call/leave", methods=["POST"])
def api_group_call_leave():
    cleanup_stale_calls()
    data = request.get_json(silent=True) or {}
    gid = (data.get("groupId") or "").strip()
    user = (data.get("user") or "").strip()
    touch_user_online(user)
    duration_str = (data.get("duration") or "00:00").strip()
    was_connected = bool(data.get("wasConnected"))

    gc = GROUP_CALLS.get(gid)
    if not gc:
        return jsonify({"ok": True})

    now = int(time.time() * 1000)
    gc["participants"] = [p for p in gc["participants"] if p["username"] != user]
    gc["signals"] = [s for s in gc["signals"] if s.get("from") != user and s.get("to") != user]
    gc["updatedAt"] = now

    if was_connected and user:
        if not duration_str or duration_str == "00:00":
            sec = max(1, (now - int(gc.get("startedAt") or now)) // 1000)
            duration_str = format_duration_mmss(sec)
        append_group_user_message(gid, user, f"📞 Вышел из звонка • длительность {duration_str}")

    if not gc["participants"]:
        GROUP_CALLS.pop(gid, None)

    return jsonify({"ok": True})


@app.route("/api/group-call/poll")
def api_group_call_poll():
    cleanup_stale_calls()
    gid = (request.args.get("groupId") or "").strip()
    user = (request.args.get("user") or "").strip()
    touch_user_online(user)
    gc = GROUP_CALLS.get(gid)
    if not gc:
        return jsonify({"ok": True, "call": None, "signals": []})

    now = int(time.time() * 1000)
    alive_parts = []
    for p in gc["participants"]:
        if (now - int(p.get("lastSeen") or now)) <= CALL_TTL_MS:
            if p["username"] == user:
                p["lastSeen"] = now
            alive_parts.append(p)
    gc["participants"] = alive_parts
    if not gc["participants"]:
        GROUP_CALLS.pop(gid, None)
        return jsonify({"ok": True, "call": None, "signals": []})

    my_signals = []
    remaining_signals = []
    for s in gc["signals"]:
        if s.get("to") == user:
            my_signals.append(s)
        else:
            remaining_signals.append(s)
    gc["signals"] = remaining_signals

    return jsonify({
        "ok": True,
        "call": {
            "groupId": gid,
            "startedAt": gc["startedAt"],
            "participants": gc["participants"],
        },
        "signals": my_signals,
    })


init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
