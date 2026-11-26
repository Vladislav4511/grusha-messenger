from functools import wraps
import json
import os
import sqlite3
from flask import Flask, jsonify, render_template_string, request, session
from werkzeug.security import check_password_hash, generate_password_hash

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get(
    "SECRET_KEY", "grusha_messenger_secret_key_2026"
)
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_HTTPONLY"] = True

DB_PATH = os.environ.get("DB_PATH", "grusha.db")


def ensure_db():
  db_path = DB_PATH
  parent = os.path.dirname(os.path.abspath(db_path))
  if parent and not os.path.exists(parent):
    os.makedirs(parent, exist_ok=True)

  conn = sqlite3.connect(db_path, timeout=15)
  conn.row_factory = sqlite3.Row
  conn.execute("PRAGMA foreign_keys = ON")
  conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            display_name TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            avatar TEXT NOT NULL DEFAULT '🍐'
        );

        CREATE TABLE IF NOT EXISTS chats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT NOT NULL CHECK(type IN ('private', 'group')),
            title TEXT,
            avatar TEXT NOT NULL DEFAULT '🍐',
            owner_id INTEGER,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS chat_members (
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            can_write INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY (chat_id, user_id),
            FOREIGN KEY (chat_id) REFERENCES chats(id) ON DELETE CASCADE,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            sender_id INTEGER NOT NULL,
            content TEXT NOT NULL,
            reply_to_id INTEGER,
            reactions TEXT NOT NULL DEFAULT '{}',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (chat_id) REFERENCES chats(id) ON DELETE CASCADE,
            FOREIGN KEY (sender_id) REFERENCES users(id) ON DELETE CASCADE
        );
    """)
  conn.commit()
  return conn


def login_required(fn):

  @wraps(fn)
  def wrapper(*args, **kwargs):
    if not session.get("user_id"):
      return jsonify({"error": "Требуется вход"}), 401
    return fn(*args, **kwargs)

  return wrapper


@app.get("/health")
def health():
  return jsonify({"ok": True})


# ---------------- AUTH & ACCOUNT ----------------


@app.post("/api/register")
def api_register():
  try:
    payload = request.get_json(silent=True) or {}
    username = str(payload.get("username", "")).strip().lower()
    if username.startswith("@"):
      username = username[1:].strip()
    display_name = str(payload.get("display_name", "")).strip()
    password = str(payload.get("password", ""))
    avatar = str(payload.get("avatar", "🍐")).strip() or "🍐"

    if not username or not display_name or not password:
      return jsonify({"error": "Заполните логин, имя и пароль"}), 400
    if len(username) < 2:
      return jsonify({"error": "Логин минимум 2 символа"}), 400
    if len(password) < 4:
      return jsonify({"error": "Пароль минимум 4 символа"}), 400

    conn = ensure_db()
    try:
      cur = conn.execute(
          "INSERT INTO users (username, display_name, password_hash, avatar)"
          " VALUES (?, ?, ?, ?)",
          (username, display_name, generate_password_hash(password), avatar),
      )
      conn.commit()
      session.clear()
      session["user_id"] = cur.lastrowid
      return jsonify({"ok": True, "user_id": cur.lastrowid})
    except sqlite3.IntegrityError:
      return jsonify({"error": "Такой логин уже занят"}), 400
    finally:
      conn.close()
  except Exception as exc:
    return jsonify({"error": f"Ошибка сервера при регистрации: {exc}"}), 500


@app.post("/api/login")
def api_login():
  try:
    payload = request.get_json(silent=True) or {}
    username = str(payload.get("username", "")).strip().lower()
    if username.startswith("@"):
      username = username[1:].strip()
    password = str(payload.get("password", ""))

    if not username or not password:
      return jsonify({"error": "Введите логин и пароль"}), 400

    conn = ensure_db()
    try:
      user = conn.execute(
          "SELECT * FROM users WHERE username = ?", (username,)
      ).fetchone()
      if not user or not check_password_hash(user["password_hash"], password):
        return jsonify({"error": "Неверный логин или пароль"}), 401
      session.clear()
      session["user_id"] = user["id"]
      return jsonify({"ok": True, "user_id": user["id"]})
    finally:
      conn.close()
  except Exception as exc:
    return jsonify({"error": f"Ошибка сервера при входе: {exc}"}), 500


@app.post("/api/logout")
def api_logout():
  session.clear()
  return jsonify({"ok": True})


@app.get("/api/me")
@login_required
def api_me_get():
  conn = ensure_db()
  try:
    user = conn.execute(
        "SELECT id, username, display_name, avatar FROM users WHERE id = ?",
        (session["user_id"],),
    ).fetchone()
    if not user:
      session.clear()
      return jsonify({"error": "Аккаунт не найден"}), 401
    return jsonify(dict(user))
  finally:
    conn.close()


@app.put("/api/me")
@login_required
def api_me_update():
  payload = request.get_json(silent=True) or {}
  display_name = str(payload.get("display_name", "")).strip()
  avatar = str(payload.get("avatar", "")).strip()

  conn = ensure_db()
  try:
    if display_name:
      conn.execute(
          "UPDATE users SET display_name = ? WHERE id = ?",
          (display_name, session["user_id"]),
      )
    if avatar:
      conn.execute(
          "UPDATE users SET avatar = ? WHERE id = ?",
          (avatar, session["user_id"]),
      )
    conn.commit()
    return jsonify({"ok": True})
  finally:
    conn.close()


@app.delete("/api/me")
@login_required
def api_me_delete():
  uid = session["user_id"]
  conn = ensure_db()
  try:
    priv = conn.execute(
        """
            SELECT c.id FROM chats c
            JOIN chat_members cm ON c.id = cm.chat_id
            WHERE c.type = 'private' AND cm.user_id = ?
        """,
        (uid,),
    ).fetchall()
    for row in priv:
      conn.execute("DELETE FROM chats WHERE id = ?", (row["id"],))
    conn.execute("DELETE FROM users WHERE id = ?", (uid,))
    conn.commit()
    session.clear()
    return jsonify({"ok": True})
  finally:
    conn.close()


# ---------------- USERS SEARCH ----------------


@app.get("/api/users")
@login_required
def api_users_search():
  q = request.args.get("q", "").strip().lower()
  if q.startswith("@"):
    q = q[1:].strip()
  uid = session["user_id"]

  conn = ensure_db()
  try:
    if q:
      rows = conn.execute(
          """
                SELECT id, username, display_name, avatar
                FROM users
                WHERE id != ? AND (LOWER(username) LIKE ? OR LOWER(display_name) LIKE ?)
                ORDER BY display_name ASC
                LIMIT 30
            """,
          (uid, f"%{q}%", f"%{q}%"),
      ).fetchall()
    else:
      rows = conn.execute(
          """
                SELECT id, username, display_name, avatar
                FROM users
                WHERE id != ?
                ORDER BY id DESC
                LIMIT 30
            """,
          (uid,),
      ).fetchall()
    return jsonify([dict(r) for r in rows])
  finally:
    conn.close()


# ---------------- CHATS & GROUPS ----------------


@app.get("/api/chats")
@login_required
def api_chats_list():
  uid = session["user_id"]
  conn = ensure_db()
  try:
    chats = conn.execute(
        """
            SELECT c.*, cm.can_write
            FROM chats c
            JOIN chat_members cm ON c.id = cm.chat_id
            WHERE cm.user_id = ?
            ORDER BY c.id DESC
        """,
        (uid,),
    ).fetchall()

    items = []
    for c in chats:
      item = dict(c)
      last = conn.execute(
          """
                SELECT m.content, m.created_at, u.display_name
                FROM messages m
                JOIN users u ON u.id = m.sender_id
                WHERE m.chat_id = ?
                ORDER BY m.id DESC
                LIMIT 1
            """,
          (c["id"],),
      ).fetchone()

      item["last_text"] = (
          f"{last['display_name']}: {last['content']}"
          if last
          else "Сообщений пока нет"
      )
      item["last_time"] = str(last["created_at"])[11:16] if last else ""

      if c["type"] == "private":
        peer = conn.execute(
            """
                    SELECT u.id, u.username, u.display_name, u.avatar
                    FROM users u
                    JOIN chat_members cm ON u.id = cm.user_id
                    WHERE cm.chat_id = ? AND u.id != ?
                """,
            (c["id"], uid),
        ).fetchone()
        if peer:
          item["title"] = peer["display_name"]
          item["avatar"] = peer["avatar"]
          item["subtitle"] = f"@{peer['username']}"
        else:
          item["title"] = "Удалённый пользователь"
          item["avatar"] = "👻"
          item["subtitle"] = ""
      else:
        cnt = conn.execute(
            "SELECT COUNT(*) AS n FROM chat_members WHERE chat_id = ?",
            (c["id"],),
        ).fetchone()["n"]
        item["subtitle"] = f"Группа • {cnt} участн."

      items.append(item)

    return jsonify(items)
  finally:
    conn.close()


@app.post("/api/chats/private")
@login_required
def api_create_private():
  uid = session["user_id"]
  peer_id = (request.get_json(silent=True) or {}).get("user_id")
  if not peer_id or int(peer_id) == uid:
    return jsonify({"error": "Неверный собеседник"}), 400
  peer_id = int(peer_id)

  conn = ensure_db()
  try:
    found = conn.execute(
        """
            SELECT c.id FROM chats c
            JOIN chat_members m1 ON c.id = m1.chat_id AND m1.user_id = ?
            JOIN chat_members m2 ON c.id = m2.chat_id AND m2.user_id = ?
            WHERE c.type = 'private'
        """,
        (uid, peer_id),
    ).fetchone()
    if found:
      return jsonify({"chat_id": found["id"]})

    cur = conn.execute("INSERT INTO chats (type) VALUES ('private')")
    cid = cur.lastrowid
    conn.execute(
        "INSERT INTO chat_members (chat_id, user_id, can_write) VALUES (?, ?, 1)",
        (cid, uid),
    )
    conn.execute(
        "INSERT INTO chat_members (chat_id, user_id, can_write) VALUES (?, ?, 1)",
        (cid, peer_id),
    )
    conn.commit()
    return jsonify({"chat_id": cid})
  finally:
    conn.close()


@app.post("/api/chats/group")
@login_required
def api_create_group():
  uid = session["user_id"]
  payload = request.get_json(silent=True) or {}
  title = str(payload.get("title", "")).strip()
  avatar = str(payload.get("avatar", "🍐")).strip() or "🍐"
  member_ids = payload.get("member_ids") or []

  if not title:
    return jsonify({"error": "Укажите название группы"}), 400

  conn = ensure_db()
  try:
    cur = conn.execute(
        "INSERT INTO chats (type, title, avatar, owner_id) VALUES ('group', ?,"
        " ?, ?)",
        (title, avatar, uid),
    )
    cid = cur.lastrowid
    members = {int(x) for x in member_ids if str(x).isdigit()}
    members.add(uid)
    for mid in members:
      conn.execute(
          "INSERT OR IGNORE INTO chat_members (chat_id, user_id, can_write)"
          " VALUES (?, ?, 1)",
          (cid, mid),
      )
    conn.commit()
    return jsonify({"chat_id": cid})
  finally:
    conn.close()


@app.get("/api/chats/<int:chat_id>")
@login_required
def api_chat_detail(chat_id):
  uid = session["user_id"]
  conn = ensure_db()
  try:
    chat = conn.execute(
        "SELECT * FROM chats WHERE id = ?", (chat_id,)
    ).fetchone()
    mem = conn.execute(
        "SELECT * FROM chat_members WHERE chat_id = ? AND user_id = ?",
        (chat_id, uid),
    ).fetchone()
    if not chat or not mem:
      return jsonify({"error": "Чат не найден"}), 404

    members_rows = conn.execute(
        """
            SELECT u.id, u.username, u.display_name, u.avatar, cm.can_write
            FROM users u
            JOIN chat_members cm ON u.id = cm.user_id
            WHERE cm.chat_id = ?
            ORDER BY CASE WHEN u.id = ? THEN 0 ELSE 1 END, u.display_name ASC
        """,
        (chat_id, chat["owner_id"] or -1),
    ).fetchall()

    info = dict(chat)
    info["my_can_write"] = bool(mem["can_write"])
    info["is_owner"] = chat["owner_id"] == uid
    info["members"] = [dict(m) for m in members_rows]

    if chat["type"] == "private":
      peer = next((m for m in info["members"] if m["id"] != uid), None)
      if peer:
        info["title"] = peer["display_name"]
        info["avatar"] = peer["avatar"]
        info["subtitle"] = f"@{peer['username']}"
      else:
        info["title"] = "Удалённый пользователь"
        info["avatar"] = "👻"
        info["subtitle"] = ""
    else:
      info["subtitle"] = f"{len(info['members'])} участн."

    return jsonify(info)
  finally:
    conn.close()


@app.put("/api/chats/<int:chat_id>")
@login_required
def api_chat_update(chat_id):
  uid = session["user_id"]
  conn = ensure_db()
  try:
    chat = conn.execute(
        "SELECT * FROM chats WHERE id = ?", (chat_id,)
    ).fetchone()
    if not chat or chat["type"] != "group" or chat["owner_id"] != uid:
      return jsonify({"error": "Только владелец группы может менять её"}), 403

    payload = request.get_json(silent=True) or {}
    title = str(payload.get("title", "")).strip()
    avatar = str(payload.get("avatar", "")).strip()

    if title:
      conn.execute(
          "UPDATE chats SET title = ? WHERE id = ?", (title, chat_id)
      )
    if avatar:
      conn.execute(
          "UPDATE chats SET avatar = ? WHERE id = ?", (avatar, chat_id)
      )
    conn.commit()
    return jsonify({"ok": True})
  finally:
    conn.close()


@app.delete("/api/chats/<int:chat_id>")
@login_required
def api_chat_delete(chat_id):
  uid = session["user_id"]
  conn = ensure_db()
  try:
    chat = conn.execute(
        "SELECT * FROM chats WHERE id = ?", (chat_id,)
    ).fetchone()
    mem = conn.execute(
        "SELECT 1 FROM chat_members WHERE chat_id = ? AND user_id = ?",
        (chat_id, uid),
    ).fetchone()
    if not chat or not mem:
      return jsonify({"error": "Чат не найден"}), 404

    if chat["type"] == "private" or chat["owner_id"] == uid:
      conn.execute("DELETE FROM chats WHERE id = ?", (chat_id,))
    else:
      conn.execute(
          "DELETE FROM chat_members WHERE chat_id = ? AND user_id = ?",
          (chat_id, uid),
      )
    conn.commit()
    return jsonify({"ok": True})
  finally:
    conn.close()


@app.post("/api/chats/<int:chat_id>/members")
@login_required
def api_group_member_add(chat_id):
  uid = session["user_id"]
  conn = ensure_db()
  try:
    chat = conn.execute(
        "SELECT * FROM chats WHERE id = ?", (chat_id,)
    ).fetchone()
    if not chat or chat["type"] != "group" or chat["owner_id"] != uid:
      return jsonify({"error": "Недостаточно прав"}), 403
    target_id = (request.get_json(silent=True) or {}).get("user_id")
    if target_id:
      conn.execute(
          "INSERT OR IGNORE INTO chat_members (chat_id, user_id, can_write)"
          " VALUES (?, ?, 1)",
          (chat_id, int(target_id)),
      )
      conn.commit()
    return jsonify({"ok": True})
  finally:
    conn.close()


@app.put("/api/chats/<int:chat_id>/members")
@login_required
def api_group_member_rights(chat_id):
  uid = session["user_id"]
  conn = ensure_db()
  try:
    chat = conn.execute(
        "SELECT * FROM chats WHERE id = ?", (chat_id,)
    ).fetchone()
    if not chat or chat["type"] != "group" or chat["owner_id"] != uid:
      return jsonify({"error": "Недостаточно прав"}), 403
    payload = request.get_json(silent=True) or {}
    target_id = payload.get("user_id")
    can_write = 1 if payload.get("can_write") else 0
    if target_id and int(target_id) != uid:
      conn.execute(
          "UPDATE chat_members SET can_write = ? WHERE chat_id = ? AND user_id"
          " = ?",
          (can_write, chat_id, int(target_id)),
      )
      conn.commit()
    return jsonify({"ok": True})
  finally:
    conn.close()


@app.delete("/api/chats/<int:chat_id>/members")
@login_required
def api_group_member_kick(chat_id):
  uid = session["user_id"]
  conn = ensure_db()
  try:
    chat = conn.execute(
        "SELECT * FROM chats WHERE id = ?", (chat_id,)
    ).fetchone()
    if not chat or chat["type"] != "group" or chat["owner_id"] != uid:
      return jsonify({"error": "Недостаточно прав"}), 403
    target_id = (request.get_json(silent=True) or {}).get("user_id")
    if target_id and int(target_id) != uid:
      conn.execute(
          "DELETE FROM chat_members WHERE chat_id = ? AND user_id = ?",
          (chat_id, int(target_id)),
      )
      conn.commit()
    return jsonify({"ok": True})
  finally:
    conn.close()


# ---------------- MESSAGES ----------------


@app.get("/api/chats/<int:chat_id>/messages")
@login_required
def api_messages_get(chat_id):
  uid = session["user_id"]
  conn = ensure_db()
  try:
    chat = conn.execute(
        "SELECT * FROM chats WHERE id = ?", (chat_id,)
    ).fetchone()
    mem = conn.execute(
        "SELECT * FROM chat_members WHERE chat_id = ? AND user_id = ?",
        (chat_id, uid),
    ).fetchone()
    if not chat or not mem:
      return jsonify({"error": "Нет доступа"}), 403

    rows = conn.execute(
        """
            SELECT m.*, u.display_name AS sender_name, u.avatar AS sender_avatar,
                   rm.content AS reply_content, ru.display_name AS reply_sender
            FROM messages m
            JOIN users u ON u.id = m.sender_id
            LEFT JOIN messages rm ON rm.id = m.reply_to_id
            LEFT JOIN users ru ON ru.id = rm.sender_id
            WHERE m.chat_id = ?
            ORDER BY m.id ASC
        """,
        (chat_id,),
    ).fetchall()

    out = []
    for r in rows:
      d = dict(r)
      try:
        d["reactions"] = json.loads(d["reactions"] or "{}")
      except Exception:
        d["reactions"] = {}
      d["is_mine"] = d["sender_id"] == uid
      d["can_delete"] = (
          (chat["owner_id"] == uid)
          if chat["type"] == "group"
          else (d["sender_id"] == uid)
      )
      d["time"] = str(d["created_at"])[11:16]
      out.append(d)
    return jsonify(out)
  finally:
    conn.close()


@app.post("/api/chats/<int:chat_id>/messages")
@login_required
def api_messages_send(chat_id):
  uid = session["user_id"]
  conn = ensure_db()
  try:
    chat = conn.execute(
        "SELECT * FROM chats WHERE id = ?", (chat_id,)
    ).fetchone()
    mem = conn.execute(
        "SELECT * FROM chat_members WHERE chat_id = ? AND user_id = ?",
        (chat_id, uid),
    ).fetchone()
    if not chat or not mem:
      return jsonify({"error": "Нет доступа"}), 403
    if not mem["can_write"]:
      return jsonify({"error": "Вам запрещено писать в этой группе"}), 403

    payload = request.get_json(silent=True) or {}
    content = str(payload.get("content", "")).strip()
    reply_to_id = payload.get("reply_to_id")
    if not content:
      return jsonify({"error": "Введите текст сообщения"}), 400

    conn.execute(
        "INSERT INTO messages (chat_id, sender_id, content, reply_to_id)"
        " VALUES (?, ?, ?, ?)",
        (chat_id, uid, content, reply_to_id),
    )
    conn.commit()
    return jsonify({"ok": True})
  finally:
    conn.close()


@app.delete("/api/messages/<int:msg_id>")
@login_required
def api_message_delete(msg_id):
  uid = session["user_id"]
  conn = ensure_db()
  try:
    msg = conn.execute(
        """
            SELECT m.*, c.type AS chat_type, c.owner_id
            FROM messages m
            JOIN chats c ON c.id = m.chat_id
            WHERE m.id = ?
        """,
        (msg_id,),
    ).fetchone()
    if not msg:
      return jsonify({"error": "Сообщение не найдено"}), 404

    if msg["chat_type"] == "group":
      if msg["owner_id"] != uid:
        return (
            jsonify({"error": "В группе удалять сообщения может только владелец"}),
            403,
        )
    else:
      if msg["sender_id"] != uid:
        return jsonify({"error": "Можно удалять только свои сообщения"}), 403

    conn.execute("DELETE FROM messages WHERE id = ?", (msg_id,))
    conn.commit()
    return jsonify({"ok": True})
  finally:
    conn.close()


@app.post("/api/messages/<int:msg_id>/react")
@login_required
def api_message_react(msg_id):
  uid = session["user_id"]
  emoji = str((request.get_json(silent=True) or {}).get("emoji", "")).strip()
  if not emoji:
    return jsonify({"error": "Выберите эмодзи"}), 400

  conn = ensure_db()
  try:
    msg = conn.execute(
        "SELECT * FROM messages WHERE id = ?", (msg_id,)
    ).fetchone()
    if not msg:
      return jsonify({"error": "Сообщение не найдено"}), 404
    mem = conn.execute(
        "SELECT 1 FROM chat_members WHERE chat_id = ? AND user_id = ?",
        (msg["chat_id"], uid),
    ).fetchone()
    if not mem:
      return jsonify({"error": "Нет доступа"}), 403

    try:
      reactions = json.loads(msg["reactions"] or "{}")
    except Exception:
      reactions = {}

    users = reactions.get(emoji, [])
    if uid in users:
      users.remove(uid)
    else:
      users.append(uid)

    if users:
      reactions[emoji] = users
    else:
      reactions.pop(emoji, None)

    conn.execute(
        "UPDATE messages SET reactions = ? WHERE id = ?",
        (json.dumps(reactions), msg_id),
    )
    conn.commit()
    return jsonify({"ok": True})
  finally:
    conn.close()


# ---------------- UI ----------------

PAGE_HTML = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<title>Груша — Мессенджер</title>
<style>
  :root {
    --bg: #0b1015;
    --surface: #131b24;
    --surface-2: #1b2633;
    --border: #263547;
    --pear: #a3e635;
    --pear-text: #0c1404;
    --pear-soft: rgba(163, 230, 53, 0.14);
    --text: #f3f7fb;
    --muted: #8fa1b5;
    --danger: #f87171;
    --danger-bg: rgba(248, 113, 113, 0.14);
    --msg-in: #19232f;
    --msg-out: #243814;
  }

  * {
    box-sizing: border-box;
    margin: 0;
    padding: 0;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    -webkit-tap-highlight-color: transparent;
  }

  html, body {
    height: 100%;
    background: var(--bg);
    color: var(--text);
    overflow: hidden;
  }

  button, input {
    font-family: inherit;
    outline: none;
  }

  /* AUTH VIEW */
  .auth-wrap {
    min-height: 100dvh;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 18px;
    background:
      radial-gradient(circle at 80% 10%, rgba(163, 230, 53, 0.16), transparent 45%),
      var(--bg);
  }

  .auth-box {
    width: 100%;
    max-width: 410px;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 24px;
    padding: 26px 22px;
    box-shadow: 0 24px 60px rgba(0, 0, 0, 0.55);
  }

  .logo-big {
    font-size: 48px;
    line-height: 1;
    text-align: center;
    margin-bottom: 8px;
  }

  .auth-title {
    text-align: center;
    font-size: 26px;
    font-weight: 800;
  }

  .auth-sub {
    text-align: center;
    color: var(--muted);
    font-size: 13px;
    margin-top: 4px;
    margin-bottom: 18px;
  }

  .mode-switch {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 6px;
    background: var(--bg);
    padding: 5px;
    border-radius: 14px;
    border: 1px solid var(--border);
    margin-bottom: 16px;
  }

  .mode-btn {
    border: none;
    background: transparent;
    color: var(--muted);
    padding: 10px;
    border-radius: 10px;
    font-size: 14px;
    font-weight: 700;
    cursor: pointer;
    transition: 0.15s;
  }

  .mode-btn.active {
    background: var(--pear);
    color: var(--pear-text);
  }

  .form-group {
    margin-bottom: 13px;
  }

  .form-label {
    display: block;
    font-size: 12px;
    font-weight: 700;
    color: var(--muted);
    margin-bottom: 6px;
  }

  .field {
    width: 100%;
    background: var(--bg);
    border: 1px solid var(--border);
    color: var(--text);
    padding: 12px 14px;
    border-radius: 12px;
    font-size: 15px;
  }

  .field:focus {
    border-color: var(--pear);
  }

  .error-box {
    min-height: 20px;
    color: var(--danger);
    font-size: 13px;
    font-weight: 600;
    text-align: center;
    margin-bottom: 10px;
  }

  .btn-primary {
    width: 100%;
    border: none;
    background: var(--pear);
    color: var(--pear-text);
    padding: 13px;
    border-radius: 12px;
    font-size: 15px;
    font-weight: 800;
    cursor: pointer;
  }

  .btn-primary:active {
    transform: scale(0.99);
  }

  .btn-ghost {
    width: 100%;
    border: 1px solid var(--border);
    background: var(--surface-2);
    color: var(--text);
    padding: 11px;
    border-radius: 12px;
    font-size: 14px;
    font-weight: 700;
    cursor: pointer;
  }

  .btn-danger {
    width: 100%;
    border: 1px solid rgba(248, 113, 113, 0.35);
    background: var(--danger-bg);
    color: var(--danger);
    padding: 11px;
    border-radius: 12px;
    font-size: 14px;
    font-weight: 700;
    cursor: pointer;
  }

  .emoji-palette {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    margin-top: 8px;
  }

  .emoji-choice {
    width: 36px;
    height: 36px;
    border-radius: 10px;
    border: 1px solid var(--border);
    background: var(--bg);
    font-size: 19px;
    cursor: pointer;
    display: flex;
    align-items: center;
    justify-content: center;
  }

  .emoji-choice.active {
    border-color: var(--pear);
    background: var(--pear-soft);
  }

  /* APP SHELL */
  .shell {
    display: none;
    width: 100%;
    height: 100dvh;
  }

  .shell.ready {
    display: flex;
  }

  .left-pane {
    width: 350px;
    flex-shrink: 0;
    background: var(--surface);
    border-right: 1px solid var(--border);
    display: flex;
    flex-direction: column;
  }

  .top-bar {
    padding: 12px 14px;
    border-bottom: 1px solid var(--border);
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
  }

  .profile-chip {
    display: flex;
    align-items: center;
    gap: 10px;
    background: transparent;
    border: none;
    color: var(--text);
    text-align: left;
    padding: 5px 8px;
    border-radius: 12px;
    cursor: pointer;
    flex: 1;
    min-width: 0;
  }

  .profile-chip:hover {
    background: var(--surface-2);
  }

  .ava {
    width: 42px;
    height: 42px;
    border-radius: 13px;
    background: var(--bg);
    border: 1px solid var(--border);
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 22px;
    flex-shrink: 0;
  }

  .chip-text {
    min-width: 0;
  }

  .chip-name {
    font-size: 15px;
    font-weight: 700;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .chip-sub {
    font-size: 12px;
    color: var(--pear);
  }

  .icon-action {
    width: 40px;
    height: 40px;
    border-radius: 12px;
    border: 1px solid var(--border);
    background: var(--bg);
    color: var(--text);
    font-size: 17px;
    cursor: pointer;
    display: flex;
    align-items: center;
    justify-content: center;
    flex-shrink: 0;
  }

  .icon-action:hover {
    border-color: var(--pear);
  }

  .search-wrap {
    padding: 10px 14px;
    border-bottom: 1px solid var(--border);
  }

  .list-scroll {
    flex: 1;
    overflow-y: auto;
    padding: 8px;
  }

  .list-heading {
    font-size: 11px;
    font-weight: 800;
    color: var(--muted);
    text-transform: uppercase;
    letter-spacing: 0.7px;
    padding: 8px 10px;
  }

  .list-row {
    display: flex;
    align-items: center;
    gap: 11px;
    padding: 10px;
    border-radius: 13px;
    cursor: pointer;
    margin-bottom: 3px;
  }

  .list-row:hover {
    background: var(--surface-2);
  }

  .list-row.selected {
    background: var(--pear-soft);
    border: 1px solid rgba(163, 230, 53, 0.35);
  }

  .row-body {
    flex: 1;
    min-width: 0;
  }

  .row-top {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 6px;
  }

  .row-title {
    font-size: 14px;
    font-weight: 700;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .row-time {
    font-size: 11px;
    color: var(--muted);
  }

  .row-sub {
    font-size: 12px;
    color: var(--muted);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    margin-top: 2px;
  }

  /* RIGHT PANE */
  .right-pane {
    flex: 1;
    display: flex;
    flex-direction: column;
    min-width: 0;
    background: var(--bg);
  }

  .welcome-pane {
    flex: 1;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    text-align: center;
    padding: 24px;
    color: var(--muted);
  }

  .chat-pane {
    display: none;
    flex-direction: column;
    height: 100%;
  }

  .chat-pane.open {
    display: flex;
  }

  .chat-top {
    height: 64px;
    padding: 0 14px;
    background: var(--surface);
    border-bottom: 1px solid var(--border);
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
  }

  .back-mobile {
    display: none;
  }

  .chat-title-btn {
    display: flex;
    align-items: center;
    gap: 10px;
    background: transparent;
    border: none;
    color: var(--text);
    text-align: left;
    padding: 5px 8px;
    border-radius: 12px;
    cursor: pointer;
    min-width: 0;
    flex: 1;
  }

  .chat-title-btn:hover {
    background: var(--surface-2);
  }

  .messages-feed {
    flex: 1;
    overflow-y: auto;
    padding: 16px;
    display: flex;
    flex-direction: column;
    gap: 10px;
  }

  .msg-line {
    display: flex;
    gap: 8px;
    max-width: 80%;
    align-items: flex-end;
  }

  .msg-line.mine {
    align-self: flex-end;
    flex-direction: row-reverse;
  }

  .msg-ava {
    width: 30px;
    height: 30px;
    border-radius: 10px;
    background: var(--surface);
    border: 1px solid var(--border);
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 16px;
    flex-shrink: 0;
  }

  .bubble {
    background: var(--msg-in);
    border: 1px solid var(--border);
    padding: 9px 12px;
    border-radius: 16px 16px 16px 4px;
    word-break: break-word;
  }

  .msg-line.mine .bubble {
    background: var(--msg-out);
    border-color: rgba(163, 230, 53, 0.28);
    border-radius: 16px 16px 4px 16px;
  }

  .bubble-author {
    font-size: 12px;
    font-weight: 700;
    color: var(--pear);
    margin-bottom: 3px;
  }

  .bubble-reply {
    background: rgba(0, 0, 0, 0.25);
    border-left: 3px solid var(--pear);
    padding: 4px 8px;
    border-radius: 6px;
    margin-bottom: 6px;
    font-size: 12px;
  }

  .bubble-reply b {
    display: block;
    color: var(--pear);
  }

  .bubble-reply span {
    color: var(--muted);
  }

  .bubble-text {
    font-size: 14px;
    line-height: 1.4;
    white-space: pre-wrap;
  }

  .bubble-time {
    font-size: 10px;
    color: var(--muted);
    text-align: right;
    margin-top: 4px;
  }

  .reactions-row {
    display: flex;
    flex-wrap: wrap;
    gap: 4px;
    margin-top: 6px;
  }

  .react-pill {
    border: 1px solid var(--border);
    background: rgba(255, 255, 255, 0.05);
    color: var(--text);
    border-radius: 999px;
    padding: 2px 7px;
    font-size: 12px;
    cursor: pointer;
  }

  .react-pill.mine {
    border-color: var(--pear);
    background: var(--pear-soft);
    color: var(--pear);
  }

  .msg-tools {
    display: flex;
    gap: 3px;
    flex-shrink: 0;
  }

  .tool-mini {
    border: 1px solid var(--border);
    background: var(--surface);
    color: var(--muted);
    border-radius: 8px;
    padding: 4px 6px;
    font-size: 12px;
    cursor: pointer;
  }

  .tool-mini:hover {
    color: var(--text);
    border-color: var(--pear);
  }

  .reply-banner {
    display: none;
    align-items: center;
    justify-content: space-between;
    padding: 8px 14px;
    background: var(--surface);
    border-top: 1px solid var(--border);
    font-size: 13px;
  }

  .reply-banner.show {
    display: flex;
  }

  .send-bar {
    padding: 10px 14px;
    background: var(--surface);
    border-top: 1px solid var(--border);
    display: flex;
    gap: 8px;
    align-items: center;
  }

  .send-bar .field {
    flex: 1;
  }

  .send-btn {
    width: 44px;
    height: 44px;
    border-radius: 12px;
    border: none;
    background: var(--pear);
    color: var(--pear-text);
    font-size: 18px;
    font-weight: 800;
    cursor: pointer;
    flex-shrink: 0;
  }

  .muted-bar {
    display: none;
    padding: 14px;
    text-align: center;
    background: var(--surface);
    border-top: 1px solid var(--border);
    color: var(--muted);
    font-size: 13px;
  }

  /* MODAL */
  .overlay {
    display: none;
    position: fixed;
    inset: 0;
    background: rgba(0, 0, 0, 0.72);
    z-index: 200;
    align-items: center;
    justify-content: center;
    padding: 16px;
  }

  .overlay.open {
    display: flex;
  }

  .dialog {
    width: 100%;
    max-width: 430px;
    max-height: 88dvh;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 20px;
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }

  .dialog-head {
    padding: 14px 18px;
    border-bottom: 1px solid var(--border);
    display: flex;
    align-items: center;
    justify-content: space-between;
    font-weight: 800;
    font-size: 16px;
  }

  .dialog-body {
    padding: 18px;
    overflow-y: auto;
  }

  .member-item {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    padding: 9px 0;
    border-bottom: 1px solid var(--border);
  }

  .member-item:last-child {
    border-bottom: none;
  }

  .small-btn {
    border: 1px solid var(--border);
    background: var(--surface-2);
    color: var(--text);
    border-radius: 8px;
    padding: 5px 8px;
    font-size: 12px;
    font-weight: 700;
    cursor: pointer;
  }

  .small-btn.warn {
    border-color: rgba(248, 113, 113, 0.35);
    background: var(--danger-bg);
    color: var(--danger);
  }

  .react-bar-popup {
    display: none;
    position: fixed;
    z-index: 300;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 999px;
    padding: 6px 10px;
    gap: 6px;
    box-shadow: 0 12px 30px rgba(0, 0, 0, 0.55);
  }

  .react-bar-popup.open {
    display: flex;
  }

  .react-bar-popup button {
    border: none;
    background: transparent;
    font-size: 20px;
    cursor: pointer;
  }

  @media (max-width: 768px) {
    .left-pane {
      width: 100%;
    }
    .right-pane {
      display: none;
      width: 100%;
    }
    .shell.mobile-chat .left-pane {
      display: none;
    }
    .shell.mobile-chat .right-pane {
      display: flex;
    }
    .back-mobile {
      display: flex;
    }
    .msg-line {
      max-width: 90%;
    }
  }
</style>
</head>
<body>

<div id="authView" class="auth-wrap">
  <div class="auth-box">
    <div class="logo-big">🍐</div>
    <div class="auth-title">Груша</div>
    <div class="auth-sub">Личные чаты и группы</div>

    <div class="mode-switch">
      <button type="button" id="modeLoginBtn" class="mode-btn active" onclick="setAuthMode('login')">Вход</button>
      <button type="button" id="modeRegBtn" class="mode-btn" onclick="setAuthMode('register')">Регистрация</button>
    </div>

    <div id="authError" class="error-box"></div>

    <div class="form-group">
      <label class="form-label">Логин</label>
      <input id="authUsername" class="field" placeholder="например: vlad_pear" autocapitalize="none" autocomplete="username">
    </div>

    <div id="regNameBlock" class="form-group" style="display:none;">
      <label class="form-label">Имя / никнейм</label>
      <input id="authDisplayName" class="field" placeholder="Как вас зовут?" autocomplete="name">
    </div>

    <div class="form-group">
      <label class="form-label">Пароль</label>
      <input id="authPassword" type="password" class="field" placeholder="Минимум 4 символа" autocomplete="current-password">
    </div>

    <div id="regAvatarBlock" class="form-group" style="display:none;">
      <label class="form-label">Аватарка эмодзи</label>
      <div id="regPalette" class="emoji-palette"></div>
    </div>

    <button type="button" id="authSubmitBtn" class="btn-primary" onclick="submitAuth()">Войти</button>
  </div>
</div>

<div id="appShell" class="shell">
  <aside class="left-pane">
    <div class="top-bar">
      <button type="button" class="profile-chip" onclick="openProfileDialog()">
        <div id="myAvatar" class="ava">🍐</div>
        <div class="chip-text">
          <div id="myName" class="chip-name">Пользователь</div>
          <div id="myLogin" class="chip-sub">@login</div>
        </div>
      </button>
      <button type="button" class="icon-action" title="Создать группу" onclick="openCreateGroupDialog()">👥+</button>
    </div>

    <div class="search-wrap">
      <input id="searchInput" class="field" placeholder="Поиск людей по имени или логину..." oninput="onSearchInput(this.value)">
    </div>

    <div id="sidebarList" class="list-scroll"></div>
  </aside>

  <main class="right-pane">
    <div id="welcomeView" class="welcome-pane">
      <div style="font-size:64px; margin-bottom:10px;">🍐</div>
      <div style="font-size:20px; font-weight:800; color:var(--text);">Мессенджер Груша</div>
      <div style="margin-top:6px; max-width:300px; font-size:14px;">Найдите человека в поиске слева или создайте группу.</div>
    </div>

    <div id="chatView" class="chat-pane">
      <div class="chat-top">
        <div style="display:flex; align-items:center; gap:8px; min-width:0; flex:1;">
          <button type="button" class="icon-action back-mobile" onclick="backToList()">←</button>
          <button type="button" class="chat-title-btn" onclick="openChatDialog()">
            <div id="activeChatAvatar" class="ava">🍐</div>
            <div style="min-width:0;">
              <div id="activeChatTitle" class="chip-name">Чат</div>
              <div id="activeChatSub" class="row-sub">Нажмите для настроек</div>
            </div>
          </button>
        </div>
        <button type="button" class="icon-action" title="Настройки" onclick="openChatDialog()">⚙️</button>
      </div>

      <div id="messagesFeed" class="messages-feed"></div>

      <div id="replyBanner" class="reply-banner">
        <div style="min-width:0;">
          <span id="replyAuthor" style="color:var(--pear); font-weight:700;"></span>:
          <span id="replyPreview" style="color:var(--muted);"></span>
        </div>
        <button type="button" style="border:none; background:transparent; color:var(--muted); font-size:18px; cursor:pointer;" onclick="clearReply()">✕</button>
      </div>

      <div id="sendBar" class="send-bar">
        <input id="msgInput" class="field" placeholder="Сообщение..." onkeydown="if(event.key==='Enter') sendChatMessage()">
        <button type="button" class="send-btn" onclick="sendChatMessage()">➤</button>
      </div>

      <div id="mutedBar" class="muted-bar">
        🔇 Владелец группы отключил для вас отправку сообщений
      </div>
    </div>
  </main>
</div>

<div id="dialogOverlay" class="overlay" onclick="if(event.target===this) closeDialog()">
  <div class="dialog">
    <div class="dialog-head">
      <span id="dialogTitle">Окно</span>
      <button type="button" style="border:none; background:transparent; color:var(--muted); font-size:20px; cursor:pointer;" onclick="closeDialog()">✕</button>
    </div>
    <div id="dialogBody" class="dialog-body"></div>
  </div>
</div>

<div id="reactPopup" class="react-bar-popup">
  <button type="button" onclick="pickReaction('❤️')">❤️</button>
  <button type="button" onclick="pickReaction('👍')">👍</button>
  <button type="button" onclick="pickReaction('😂')">😂</button>
  <button type="button" onclick="pickReaction('🔥')">🔥</button>
  <button type="button" onclick="pickReaction('😮')">😮</button>
  <button type="button" onclick="pickReaction('🍐')">🍐</button>
</div>

<script>
const EMOJI_LIST = ['🍐','😎','🦊','🐱','🐶','🐸','🐼','🦁','🤖','👾','🔥','⭐','💎','🎮','🎧','🍕','🍀','👑'];

let authMode = 'login';
let regAvatar = '🍐';
let me = null;
let activeChatId = null;
let activeChatData = null;
let replyTarget = null;
let reactMsgId = null;
let groupDraftMembers = new Set();
let lastMessagesSig = '';
let pollTimer = null;

function esc(v) {
  return String(v ?? '').replace(/[&<>"']/g, ch => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[ch]));
}

function escJs(v) {
  return esc(v).replace(/\n/g, ' ');
}

function buildPalette(elId, current, onPick) {
  const el = document.getElementById(elId);
  if (!el) return;
  el.innerHTML = '';
  EMOJI_LIST.forEach(em => {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'emoji-choice' + (em === current ? ' active' : '');
    b.textContent = em;
    b.onclick = () => {
      el.querySelectorAll('.emoji-choice').forEach(x => x.classList.remove('active'));
      b.classList.add('active');
      onPick(em);
    };
    el.appendChild(b);
  });
}

function setAuthMode(mode) {
  authMode = mode;
  document.getElementById('modeLoginBtn').classList.toggle('active', mode === 'login');
  document.getElementById('modeRegBtn').classList.toggle('active', mode === 'register');
  document.getElementById('regNameBlock').style.display = mode === 'register' ? 'block' : 'none';
  document.getElementById('regAvatarBlock').style.display = mode === 'register' ? 'block' : 'none';
  document.getElementById('authSubmitBtn').textContent = mode === 'login' ? 'Войти' : 'Зарегистрироваться';
  document.getElementById('authError').textContent = '';
  if (mode === 'register') {
    buildPalette('regPalette', regAvatar, em => { regAvatar = em; });
  }
}

async function submitAuth() {
  const errBox = document.getElementById('authError');
  errBox.textContent = '';

  const username = document.getElementById('authUsername').value.trim();
  const password = document.getElementById('authPassword').value;
  const display_name = document.getElementById('authDisplayName').value.trim();

  const endpoint = authMode === 'login' ? '/api/login' : '/api/register';
  const body = authMode === 'login'
    ? { username, password }
    : { username, password, display_name, avatar: regAvatar };

  try {
    const res = await fetch(endpoint, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      errBox.textContent = data.error || 'Не удалось выполнить запрос';
      return;
    }
    await boot();
  } catch (e) {
    errBox.textContent = 'Нет связи с сервером';
  }
}

async function boot() {
  try {
    const res = await fetch('/api/me');
    if (!res.ok) {
      me = null;
      document.getElementById('authView').style.display = 'flex';
      document.getElementById('appShell').classList.remove('ready');
      return;
    }
    me = await res.json();
    document.getElementById('authView').style.display = 'none';
    document.getElementById('appShell').classList.add('ready');
    document.getElementById('myAvatar').textContent = me.avatar;
    document.getElementById('myName').textContent = me.display_name;
    document.getElementById('myLogin').textContent = '@' + me.username;
    await refreshSidebar();
    if (!pollTimer) {
      pollTimer = setInterval(() => {
        if (!me) return;
        refreshSidebar();
        if (activeChatId) refreshChat(false);
      }, 1500);
    }
  } catch (e) {
    document.getElementById('authView').style.display = 'flex';
  }
}

async function refreshSidebar() {
  const q = document.getElementById('searchInput').value.trim();
  if (q) {
    await runUserSearch(q);
    return;
  }
  const res = await fetch('/api/chats');
  if (!res.ok) return;
  const chats = await res.json();
  const box = document.getElementById('sidebarList');
  if (!chats.length) {
    box.innerHTML = '<div style="text-align:center; color:var(--muted); padding:28px 14px; font-size:13px;">Чатов пока нет.<br>Найдите человека в поиске или создайте группу.</div>';
    return;
  }
  box.innerHTML = '<div class="list-heading">Чаты</div>' + chats.map(c => `
    <div class="list-row ${c.id === activeChatId ? 'selected' : ''}" onclick="selectChat(${c.id})">
      <div class="ava">${esc(c.avatar)}</div>
      <div class="row-body">
        <div class="row-top">
          <div class="row-title">${c.type === 'group' ? '👥 ' : ''}${esc(c.title)}</div>
          <div class="row-time">${esc(c.last_time)}</div>
        </div>
        <div class="row-sub">${esc(c.last_text)}</div>
      </div>
    </div>
  `).join('');
}

async function onSearchInput(val) {
  if (!val.trim()) {
    await refreshSidebar();
    return;
  }
  await runUserSearch(val.trim());
}

async function runUserSearch(q) {
  const res = await fetch('/api/users?q=' + encodeURIComponent(q));
  if (!res.ok) return;
  const users = await res.json();
  const box = document.getElementById('sidebarList');
  if (!users.length) {
    box.innerHTML = '<div style="text-align:center; color:var(--muted); padding:24px; font-size:13px;">Люди не найдены</div>';
    return;
  }
  box.innerHTML = '<div class="list-heading">Люди</div>' + users.map(u => `
    <div class="list-row" onclick="openPrivateWith(${u.id})">
      <div class="ava">${esc(u.avatar)}</div>
      <div class="row-body">
        <div class="row-title">${esc(u.display_name)}</div>
        <div class="row-sub">@${esc(u.username)}</div>
      </div>
    </div>
  `).join('');
}

async function openPrivateWith(userId) {
  const res = await fetch('/api/chats/private', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ user_id: userId })
  });
  const data = await res.json();
  if (!res.ok) return alert(data.error || 'Ошибка');
  document.getElementById('searchInput').value = '';
  await refreshSidebar();
  await selectChat(data.chat_id);
}

async function selectChat(chatId) {
  activeChatId = chatId;
  lastMessagesSig = '';
  clearReply();
  document.getElementById('welcomeView').style.display = 'none';
  document.getElementById('chatView').classList.add('open');
  document.getElementById('appShell').classList.add('mobile-chat');
  await refreshChat(true);
  await refreshSidebar();
}

function backToList() {
  document.getElementById('appShell').classList.remove('mobile-chat');
  activeChatId = null;
}

async function refreshChat(forceScroll) {
  if (!activeChatId) return;
  const infoRes = await fetch('/api/chats/' + activeChatId);
  if (!infoRes.ok) {
    activeChatId = null;
    document.getElementById('chatView').classList.remove('open');
    document.getElementById('welcomeView').style.display = 'flex';
    document.getElementById('appShell').classList.remove('mobile-chat');
    await refreshSidebar();
    return;
  }
  activeChatData = await infoRes.json();

  document.getElementById('activeChatAvatar').textContent = activeChatData.avatar;
  document.getElementById('activeChatTitle').textContent = activeChatData.title;
  document.getElementById('activeChatSub').textContent =
    activeChatData.type === 'group'
      ? `${activeChatData.subtitle} • нажмите для настроек`
      : (activeChatData.subtitle || 'Личный чат');

  document.getElementById('sendBar').style.display = activeChatData.my_can_write ? 'flex' : 'none';
  document.getElementById('mutedBar').style.display = activeChatData.my_can_write ? 'none' : 'block';

  const msgRes = await fetch('/api/chats/' + activeChatId + '/messages');
  if (!msgRes.ok) return;
  const msgs = await msgRes.json();
  const sig = JSON.stringify(msgs);
  if (sig === lastMessagesSig && !forceScroll) return;
  lastMessagesSig = sig;

  const feed = document.getElementById('messagesFeed');
  const nearBottom = feed.scrollHeight - feed.scrollTop - feed.clientHeight < 100;

  feed.innerHTML = msgs.map(m => {
    const reactHtml = Object.entries(m.reactions || {}).map(([em, ids]) => `
      <button type="button" class="react-pill ${ids.includes(me.id) ? 'mine' : ''}" onclick="toggleReaction(${m.id}, '${em}')">
        ${em} ${ids.length}
      </button>
    `).join('');

    const replyHtml = m.reply_to_id ? `
      <div class="bubble-reply">
        <b>${esc(m.reply_sender || 'Сообщение')}</b>
        <span>${esc(m.reply_content || 'Сообщение удалено')}</span>
      </div>
    ` : '';

    return `
      <div class="msg-line ${m.is_mine ? 'mine' : ''}">
        ${!m.is_mine ? `<div class="msg-ava">${esc(m.sender_avatar)}</div>` : ''}
        <div class="bubble">
          ${(!m.is_mine && activeChatData.type === 'group') ? `<div class="bubble-author">${esc(m.sender_name)}</div>` : ''}
          ${replyHtml}
          <div class="bubble-text">${esc(m.content)}</div>
          ${reactHtml ? `<div class="reactions-row">${reactHtml}</div>` : ''}
          <div class="bubble-time">${esc(m.time)}</div>
        </div>
        <div class="msg-tools">
          <button type="button" class="tool-mini" title="Реакция" onclick="openReactPopup(event, ${m.id})">😊</button>
          <button type="button" class="tool-mini" title="Ответить" onclick="prepareReply(${m.id}, '${escJs(m.sender_name)}', '${escJs(m.content)}')">↩️</button>
          ${m.can_delete ? `<button type="button" class="tool-mini" title="Удалить" onclick="removeMessage(${m.id})">🗑️</button>` : ''}
        </div>
      </div>
    `;
  }).join('');

  if (forceScroll || nearBottom) {
    feed.scrollTop = feed.scrollHeight;
  }
}

async function sendChatMessage() {
  const inp = document.getElementById('msgInput');
  const text = inp.value.trim();
  if (!text || !activeChatId) return;
  inp.value = '';
  const replyId = replyTarget ? replyTarget.id : null;
  clearReply();

  const res = await fetch('/api/chats/' + activeChatId + '/messages', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ content: text, reply_to_id: replyId })
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    alert(data.error || 'Не удалось отправить');
    return;
  }
  await refreshChat(true);
  await refreshSidebar();
}

function prepareReply(id, author, text) {
  replyTarget = { id, author, text };
  document.getElementById('replyAuthor').textContent = author;
  document.getElementById('replyPreview').textContent = text;
  document.getElementById('replyBanner').classList.add('show');
  document.getElementById('msgInput').focus();
}

function clearReply() {
  replyTarget = null;
  document.getElementById('replyBanner').classList.remove('show');
}

async function removeMessage(msgId) {
  if (!confirm('Удалить сообщение?')) return;
  const res = await fetch('/api/messages/' + msgId, { method: 'DELETE' });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    alert(data.error || 'Нельзя удалить');
    return;
  }
  lastMessagesSig = '';
  await refreshChat(false);
  await refreshSidebar();
}

function openReactPopup(ev, msgId) {
  ev.stopPropagation();
  reactMsgId = msgId;
  const pop = document.getElementById('reactPopup');
  pop.classList.add('open');
  const x = Math.min(window.innerWidth - 240, Math.max(12, ev.clientX - 90));
  const y = Math.max(12, ev.clientY - 48);
  pop.style.left = x + 'px';
  pop.style.top = y + 'px';
}

document.addEventListener('click', () => {
  document.getElementById('reactPopup').classList.remove('open');
});

async function pickReaction(emoji) {
  if (!reactMsgId) return;
  await toggleReaction(reactMsgId, emoji);
}

async function toggleReaction(msgId, emoji) {
  await fetch('/api/messages/' + msgId + '/react', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ emoji })
  });
  lastMessagesSig = '';
  await refreshChat(false);
}

function openDialog(title, html) {
  document.getElementById('dialogTitle').textContent = title;
  document.getElementById('dialogBody').innerHTML = html;
  document.getElementById('dialogOverlay').classList.add('open');
}

function closeDialog() {
  document.getElementById('dialogOverlay').classList.remove('open');
}

/* PROFILE DIALOG */
function openProfileDialog() {
  let picked = me.avatar;
  openDialog('Профиль', `
    <div class="form-group">
      <label class="form-label">Никнейм</label>
      <input id="profNameInp" class="field" value="${escJs(me.display_name)}">
    </div>
    <div class="form-group">
      <label class="form-label">Аватарка эмодзи</label>
      <input id="profAvaInp" class="field" value="${escJs(me.avatar)}" maxlength="4" style="text-align:center; font-size:22px;">
      <div id="profPalette" class="emoji-palette"></div>
    </div>
    <button type="button" class="btn-primary" style="margin-bottom:8px;" onclick="saveProfileDialog()">Сохранить</button>
    <button type="button" class="btn-ghost" style="margin-bottom:16px;" onclick="doLogout()">Выйти</button>
    <button type="button" class="btn-danger" onclick="doDeleteAccount()">Удалить аккаунт</button>
  `);
  buildPalette('profPalette', picked, em => {
    picked = em;
    document.getElementById('profAvaInp').value = em;
  });
}

async function saveProfileDialog() {
  const display_name = document.getElementById('profNameInp').value.trim();
  const avatar = document.getElementById('profAvaInp').value.trim();
  await fetch('/api/me', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ display_name, avatar })
  });
  closeDialog();
  await boot();
}

async function doLogout() {
  await fetch('/api/logout', { method: 'POST' });
  closeDialog();
  await boot();
}

async function doDeleteAccount() {
  if (!confirm('Удалить аккаунт без возможности восстановления?')) return;
  await fetch('/api/me', { method: 'DELETE' });
  closeDialog();
  await boot();
}

/* CREATE GROUP DIALOG */
function openCreateGroupDialog() {
  groupDraftMembers.clear();
  let gAva = '🍐';
  openDialog('Создать группу', `
    <div class="form-group">
      <label class="form-label">Название группы</label>
      <input id="grpTitleInp" class="field" placeholder="Название группы">
    </div>
    <div class="form-group">
      <label class="form-label">Эмодзи группы</label>
      <input id="grpAvaInp" class="field" value="🍐" maxlength="4" style="text-align:center; font-size:22px;">
      <div id="grpCreatePalette" class="emoji-palette"></div>
    </div>
    <div class="form-group">
      <label class="form-label">Участники</label>
      <input class="field" placeholder="Поиск людей..." oninput="loadGroupCandidates(this.value)" style="margin-bottom:8px;">
      <div id="grpCandidates" style="max-height:150px; overflow-y:auto;"></div>
    </div>
    <button type="button" class="btn-primary" onclick="submitCreateGroup()">Создать группу</button>
  `);
  buildPalette('grpCreatePalette', gAva, em => {
    gAva = em;
    document.getElementById('grpAvaInp').value = em;
  });
  loadGroupCandidates('');
}

async function loadGroupCandidates(q) {
  const res = await fetch('/api/users?q=' + encodeURIComponent(q));
  const users = await res.json();
  const box = document.getElementById('grpCandidates');
  box.innerHTML = users.map(u => {
    const checked = groupDraftMembers.has(u.id);
    return `
      <div class="member-item" style="cursor:pointer;" onclick="toggleDraftMember(${u.id}, this)">
        <span>${esc(u.avatar)} <b>${esc(u.display_name)}</b> <small style="color:var(--muted);">@${esc(u.username)}</small></span>
        <input type="checkbox" ${checked ? 'checked' : ''} style="width:18px; height:18px; accent-color:var(--pear);">
      </div>
    `;
  }).join('');
}

function toggleDraftMember(id, row) {
  const cb = row.querySelector('input');
  if (groupDraftMembers.has(id)) {
    groupDraftMembers.delete(id);
    cb.checked = false;
  } else {
    groupDraftMembers.add(id);
    cb.checked = true;
  }
}

async function submitCreateGroup() {
  const title = document.getElementById('grpTitleInp').value.trim();
  const avatar = document.getElementById('grpAvaInp').value.trim() || '🍐';
  if (!title) return alert('Введите название группы');
  const res = await fetch('/api/chats/group', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ title, avatar, member_ids: Array.from(groupDraftMembers) })
  });
  const data = await res.json();
  if (!res.ok) return alert(data.error || 'Ошибка');
  closeDialog();
  await refreshSidebar();
  await selectChat(data.chat_id);
}

/* CHAT / GROUP SETTINGS DIALOG */
async function openChatDialog() {
  if (!activeChatData) return;
  if (activeChatData.type === 'private') {
    openDialog('Чат', `
      <div style="text-align:center; margin-bottom:18px;">
        <div style="font-size:52px; margin-bottom:8px;">${esc(activeChatData.avatar)}</div>
        <div style="font-size:18px; font-weight:800;">${esc(activeChatData.title)}</div>
        <div style="color:var(--muted); font-size:13px;">${esc(activeChatData.subtitle || '')}</div>
      </div>
      <button type="button" class="btn-danger" onclick="deleteCurrentChat()">Удалить чат</button>
    `);
    return;
  }

  const owner = activeChatData.is_owner;
  const ownerSection = owner ? `
    <div class="form-group">
      <label class="form-label">Название группы</label>
      <input id="editGrpTitle" class="field" value="${escJs(activeChatData.title)}">
    </div>
    <div class="form-group">
      <label class="form-label">Аватарка эмодзи</label>
      <input id="editGrpAva" class="field" value="${escJs(activeChatData.avatar)}" maxlength="4" style="text-align:center; font-size:22px;">
      <div id="editGrpPalette" class="emoji-palette"></div>
    </div>
    <button type="button" class="btn-primary" style="margin-bottom:16px;" onclick="saveGroupMeta()">Сохранить название и эмодзи</button>
    <div class="form-group">
      <label class="form-label">Добавить участника</label>
      <input class="field" placeholder="Имя или логин..." oninput="findUsersForGroup(this.value)">
      <div id="addGroupUserList"></div>
    </div>
  ` : '';

  const membersHtml = activeChatData.members.map(m => {
    const isGrpOwner = m.id === activeChatData.owner_id;
    const controls = (owner && !isGrpOwner) ? `
      <div style="display:flex; gap:5px;">
        <button type="button" class="small-btn ${m.can_write ? '' : 'warn'}" onclick="setWritePerm(${m.id}, ${m.can_write ? 0 : 1})">
          ${m.can_write ? 'Пишет' : 'Без права писать'}
        </button>
        <button type="button" class="small-btn warn" onclick="kickFromGroup(${m.id})">Удалить</button>
      </div>
    ` : (isGrpOwner ? '<span style="font-size:11px; color:var(--pear); font-weight:700;">Владелец</span>' : '');

    return `
      <div class="member-item">
        <div style="min-width:0;">
          <div style="font-size:14px; font-weight:700;">${esc(m.avatar)} ${esc(m.display_name)}</div>
          <div style="font-size:12px; color:var(--muted);">@${esc(m.username)}</div>
        </div>
        ${controls}
      </div>
    `;
  }).join('');

  openDialog('Настройки группы', `
    ${ownerSection}
    <div class="list-heading" style="padding-left:0;">Участники (${activeChatData.members.length})</div>
    <div style="margin-bottom:16px;">${membersHtml}</div>
    <button type="button" class="btn-danger" onclick="deleteCurrentChat()">
      ${owner ? 'Удалить группу' : 'Выйти из группы'}
    </button>
  `);

  if (owner) {
    buildPalette('editGrpPalette', activeChatData.avatar, em => {
      document.getElementById('editGrpAva').value = em;
    });
  }
}

async function saveGroupMeta() {
  const title = document.getElementById('editGrpTitle').value.trim();
  const avatar = document.getElementById('editGrpAva').value.trim();
  await fetch('/api/chats/' + activeChatId, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ title, avatar })
  });
  lastMessagesSig = '';
  await refreshChat(false);
  await refreshSidebar();
  await openChatDialog();
}

async function findUsersForGroup(q) {
  const box = document.getElementById('addGroupUserList');
  if (!q.trim()) { box.innerHTML = ''; return; }
  const res = await fetch('/api/users?q=' + encodeURIComponent(q.trim()));
  const users = await res.json();
  const existing = new Set(activeChatData.members.map(x => x.id));
  box.innerHTML = users.filter(u => !existing.has(u.id)).map(u => `
    <div class="member-item">
      <span>${esc(u.avatar)} ${esc(u.display_name)} (@${esc(u.username)})</span>
      <button type="button" class="small-btn" onclick="inviteToGroup(${u.id})">+ Добавить</button>
    </div>
  `).join('');
}

async function inviteToGroup(userId) {
  await fetch('/api/chats/' + activeChatId + '/members', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ user_id: userId })
  });
  await refreshChat(false);
  await openChatDialog();
}

async function setWritePerm(userId, canWrite) {
  await fetch('/api/chats/' + activeChatId + '/members', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ user_id: userId, can_write: canWrite })
  });
  await refreshChat(false);
  await openChatDialog();
}

async function kickFromGroup(userId) {
  if (!confirm('Удалить участника из группы?')) return;
  await fetch('/api/chats/' + activeChatId + '/members', {
    method: 'DELETE',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ user_id: userId })
  });
  await refreshChat(false);
  await openChatDialog();
}

async function deleteCurrentChat() {
  if (!confirm('Подтвердить удаление?')) return;
  await fetch('/api/chats/' + activeChatId, { method: 'DELETE' });
  closeDialog();
  activeChatId = null;
  document.getElementById('chatView').classList.remove('open');
  document.getElementById('welcomeView').style.display = 'flex';
  document.getElementById('appShell').classList.remove('mobile-chat');
  await refreshSidebar();
}

boot();
</script>
</body>
</html>
"""


@app.get("/")
def index():
  return render_template_string(PAGE_HTML)


if __name__ == "__main__":
  port = int(os.environ.get("PORT", 5000))
  app.run(host="0.0.0.0", port=port)
