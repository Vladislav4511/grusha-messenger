from functools import wraps
import json
import os
import sqlite3
from flask import Flask, jsonify, render_template_string, request, session
from werkzeug.security import check_password_hash, generate_password_hash

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "grusha_super_secret_key_2026")
DB_PATH = os.environ.get("DB_PATH", "grusha.db")


def get_db():
  db_dir = os.path.dirname(os.path.abspath(DB_PATH))
  if db_dir and not os.path.exists(db_dir):
    os.makedirs(db_dir, exist_ok=True)
  conn = sqlite3.connect(DB_PATH)
  conn.row_factory = sqlite3.Row
  conn.execute("PRAGMA foreign_keys = ON")
  return conn


def init_db():
  conn = get_db()
  conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            display_name TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            avatar TEXT DEFAULT '🍐'
        );

        CREATE TABLE IF NOT EXISTS chats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT NOT NULL CHECK(type IN ('private', 'group')),
            title TEXT,
            avatar TEXT DEFAULT '🍐',
            owner_id INTEGER,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS chat_members (
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            can_write INTEGER DEFAULT 1,
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
            reactions TEXT DEFAULT '{}',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (chat_id) REFERENCES chats(id) ON DELETE CASCADE,
            FOREIGN KEY (sender_id) REFERENCES users(id) ON DELETE CASCADE
        );
    """)
  conn.commit()
  conn.close()


init_db()


def login_required(f):

  @wraps(f)
  def decorated(*args, **kwargs):
    if "user_id" not in session:
      return jsonify({"error": "Не авторизован"}), 401
    return f(*args, **kwargs)

  return decorated


@app.route("/health")
def health():
  return jsonify({"status": "ok"})


# --- AUTH & PROFILE API ---


@app.route("/api/register", methods=["POST"])
def register():
  try:
    init_db()
    data = request.get_json(silent=True) or {}
    username = str(data.get("username", "")).strip().lower()
    display_name = str(data.get("display_name", "")).strip()
    password = str(data.get("password", ""))
    avatar = str(data.get("avatar", "🍐")).strip() or "🍐"

    if not username or not display_name or not password:
      return jsonify({"error": "Заполните все поля"}), 400
    if len(username) < 3:
      return jsonify({"error": "Логин минимум 3 символа"}), 400

    conn = get_db()
    try:
      cur = conn.execute(
          "INSERT INTO users (username, display_name, password_hash, avatar)"
          " VALUES (?, ?, ?, ?)",
          (username, display_name, generate_password_hash(password), avatar),
      )
      conn.commit()
      session["user_id"] = cur.lastrowid
      return jsonify({"ok": True})
    except sqlite3.IntegrityError:
      return jsonify({"error": "Этот логин уже занят"}), 400
    finally:
      conn.close()
  except Exception as e:
    return jsonify({"error": f"Ошибка сервера: {e}"}), 500


@app.route("/api/login", methods=["POST"])
def login():
  try:
    init_db()
    data = request.get_json(silent=True) or {}
    username = str(data.get("username", "")).strip().lower()
    password = str(data.get("password", ""))
    conn = get_db()
    user = conn.execute(
        "SELECT * FROM users WHERE username = ?", (username,)
    ).fetchone()
    conn.close()
    if not user or not check_password_hash(user["password_hash"], password):
      return jsonify({"error": "Неверный логин или пароль"}), 400
    session["user_id"] = user["id"]
    return jsonify({"ok": True})
  except Exception as e:
    return jsonify({"error": f"Ошибка сервера: {e}"}), 500


@app.route("/api/logout", methods=["POST"])
def logout():
  session.clear()
  return jsonify({"ok": True})


@app.route("/api/me", methods=["GET", "PUT", "DELETE"])
@login_required
def me():
  uid = session["user_id"]
  conn = get_db()
  if request.method == "GET":
    user = conn.execute(
        "SELECT id, username, display_name, avatar FROM users WHERE id = ?",
        (uid,),
    ).fetchone()
    conn.close()
    if not user:
      session.clear()
      return jsonify({"error": "Пользователь не найден"}), 401
    return jsonify(dict(user))

  if request.method == "PUT":
    data = request.get_json(silent=True) or {}
    display_name = str(data.get("display_name", "")).strip()
    avatar = str(data.get("avatar", "")).strip()
    if display_name:
      conn.execute(
          "UPDATE users SET display_name = ? WHERE id = ?", (display_name, uid)
      )
    if avatar:
      conn.execute("UPDATE users SET avatar = ? WHERE id = ?", (avatar, uid))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})

  if request.method == "DELETE":
    private_chats = conn.execute(
        """
            SELECT c.id FROM chats c
            JOIN chat_members cm ON c.id = cm.chat_id
            WHERE c.type = 'private' AND cm.user_id = ?
        """,
        (uid,),
    ).fetchall()
    for pc in private_chats:
      conn.execute("DELETE FROM chats WHERE id = ?", (pc["id"],))
    conn.execute("DELETE FROM users WHERE id = ?", (uid,))
    conn.commit()
    conn.close()
    session.clear()
    return jsonify({"ok": True})


# --- USERS SEARCH ---


@app.route("/api/users/search")
@login_required
def search_users():
  q = request.args.get("q", "").strip().lower()
  uid = session["user_id"]
  conn = get_db()
  if q:
    rows = conn.execute(
        """
            SELECT id, username, display_name, avatar FROM users
            WHERE id != ? AND (LOWER(username) LIKE ? OR LOWER(display_name) LIKE ?)
            LIMIT 25
        """,
        (uid, f"%{q}%", f"%{q}%"),
    ).fetchall()
  else:
    rows = conn.execute(
        """
            SELECT id, username, display_name, avatar FROM users
            WHERE id != ? ORDER BY id DESC LIMIT 20
        """,
        (uid,),
    ).fetchall()
  conn.close()
  return jsonify([dict(r) for r in rows])


# --- CHATS API ---


@app.route("/api/chats", methods=["GET"])
@login_required
def get_chats():
  uid = session["user_id"]
  conn = get_db()
  chats = conn.execute(
      """
        SELECT c.*, cm.can_write FROM chats c
        JOIN chat_members cm ON c.id = cm.chat_id
        WHERE cm.user_id = ?
        ORDER BY c.id DESC
    """,
      (uid,),
  ).fetchall()

  result = []
  for c in chats:
    c_dict = dict(c)
    last_msg = conn.execute(
        """
            SELECT m.content, u.display_name, m.created_at
            FROM messages m
            JOIN users u ON m.sender_id = u.id
            WHERE m.chat_id = ? ORDER BY m.id DESC LIMIT 1
        """,
        (c["id"],),
    ).fetchone()

    c_dict["last_message"] = (
        f"{last_msg['display_name']}: {last_msg['content']}"
        if last_msg
        else "Нет сообщений"
    )
    c_dict["last_time"] = (
        str(last_msg["created_at"])[11:16] if last_msg else ""
    )

    if c["type"] == "private":
      other = conn.execute(
          """
                SELECT u.id, u.username, u.display_name, u.avatar
                FROM users u
                JOIN chat_members cm ON u.id = cm.user_id
                WHERE cm.chat_id = ? AND u.id != ?
            """,
          (c["id"], uid),
      ).fetchone()
      if other:
        c_dict["title"] = other["display_name"]
        c_dict["avatar"] = other["avatar"]
        c_dict["subtitle"] = f"@{other['username']}"
      else:
        c_dict["title"] = "Удалённый аккаунт"
        c_dict["avatar"] = "👻"
        c_dict["subtitle"] = ""
    else:
      count = conn.execute(
          "SELECT COUNT(*) as cnt FROM chat_members WHERE chat_id = ?",
          (c["id"],),
      ).fetchone()["cnt"]
      c_dict["subtitle"] = f"{count} участн."
    result.append(c_dict)

  conn.close()
  return jsonify(result)


@app.route("/api/chats/private", methods=["POST"])
@login_required
def create_or_get_private():
  uid = session["user_id"]
  target_id = (request.get_json(silent=True) or {}).get("user_id")
  if not target_id or target_id == uid:
    return jsonify({"error": "Некорректный пользователь"}), 400

  conn = get_db()
  existing = conn.execute(
      """
        SELECT c.id FROM chats c
        JOIN chat_members cm1 ON c.id = cm1.chat_id AND cm1.user_id = ?
        JOIN chat_members cm2 ON c.id = cm2.chat_id AND cm2.user_id = ?
        WHERE c.type = 'private'
    """,
      (uid, target_id),
  ).fetchone()

  if existing:
    conn.close()
    return jsonify({"chat_id": existing["id"]})

  cur = conn.execute("INSERT INTO chats (type) VALUES ('private')")
  chat_id = cur.lastrowid
  conn.execute(
      "INSERT INTO chat_members (chat_id, user_id, can_write) VALUES (?, ?, 1)",
      (chat_id, uid),
  )
  conn.execute(
      "INSERT INTO chat_members (chat_id, user_id, can_write) VALUES (?, ?, 1)",
      (chat_id, target_id),
  )
  conn.commit()
  conn.close()
  return jsonify({"chat_id": chat_id})


@app.route("/api/chats/group", methods=["POST"])
@login_required
def create_group():
  uid = session["user_id"]
  data = request.get_json(silent=True) or {}
  title = str(data.get("title", "")).strip()
  avatar = str(data.get("avatar", "🍐")).strip() or "🍐"
  member_ids = data.get("member_ids", [])

  if not title:
    return jsonify({"error": "Введите название группы"}), 400

  conn = get_db()
  cur = conn.execute(
      "INSERT INTO chats (type, title, avatar, owner_id) VALUES ('group', ?, ?,"
      " ?)",
      (title, avatar, uid),
  )
  chat_id = cur.lastrowid

  all_members = set(member_ids)
  all_members.add(uid)
  for mid in all_members:
    conn.execute(
        "INSERT OR IGNORE INTO chat_members (chat_id, user_id, can_write)"
        " VALUES (?, ?, 1)",
        (chat_id, mid),
    )

  conn.commit()
  conn.close()
  return jsonify({"chat_id": chat_id})


@app.route("/api/chats/<int:chat_id>", methods=["GET", "PUT", "DELETE"])
@login_required
def manage_chat(chat_id):
  uid = session["user_id"]
  conn = get_db()
  chat = conn.execute("SELECT * FROM chats WHERE id = ?", (chat_id,)).fetchone()
  member = conn.execute(
      "SELECT * FROM chat_members WHERE chat_id = ? AND user_id = ?",
      (chat_id, uid),
  ).fetchone()

  if not chat or not member:
    conn.close()
    return jsonify({"error": "Чат не найден"}), 404

  if request.method == "GET":
    members = conn.execute(
        """
            SELECT u.id, u.username, u.display_name, u.avatar, cm.can_write
            FROM users u
            JOIN chat_members cm ON u.id = cm.user_id
            WHERE cm.chat_id = ?
        """,
        (chat_id,),
    ).fetchall()
    info = dict(chat)
    info["my_can_write"] = member["can_write"]
    info["is_owner"] = chat["owner_id"] == uid
    info["members"] = [dict(m) for m in members]
    if chat["type"] == "private":
      other = next((m for m in info["members"] if m["id"] != uid), None)
      if other:
        info["title"] = other["display_name"]
        info["avatar"] = other["avatar"]
        info["subtitle"] = f"@{other['username']}"
    conn.close()
    return jsonify(info)

  if request.method == "PUT":
    if chat["type"] != "group" or chat["owner_id"] != uid:
      conn.close()
      return (
          jsonify({"error": "Только владелец может менять настройки группы"}),
          403,
      )
    data = request.get_json(silent=True) or {}
    if "title" in data and str(data["title"]).strip():
      conn.execute(
          "UPDATE chats SET title = ? WHERE id = ?",
          (str(data["title"]).strip(), chat_id),
      )
    if "avatar" in data and str(data["avatar"]).strip():
      conn.execute(
          "UPDATE chats SET avatar = ? WHERE id = ?",
          (str(data["avatar"]).strip(), chat_id),
      )
    conn.commit()
    conn.close()
    return jsonify({"ok": True})

  if request.method == "DELETE":
    if chat["type"] == "private" or chat["owner_id"] == uid:
      conn.execute("DELETE FROM chats WHERE id = ?", (chat_id,))
    else:
      conn.execute(
          "DELETE FROM chat_members WHERE chat_id = ? AND user_id = ?",
          (chat_id, uid),
      )
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/chats/<int:chat_id>/members", methods=["POST", "PUT", "DELETE"])
@login_required
def manage_group_members(chat_id):
  uid = session["user_id"]
  conn = get_db()
  chat = conn.execute("SELECT * FROM chats WHERE id = ?", (chat_id,)).fetchone()
  if not chat or chat["type"] != "group" or chat["owner_id"] != uid:
    conn.close()
    return jsonify({"error": "Недостаточно прав"}), 403

  data = request.get_json(silent=True) or {}
  target_id = data.get("user_id")

  if request.method == "POST":
    if target_id:
      conn.execute(
          "INSERT OR IGNORE INTO chat_members (chat_id, user_id, can_write)"
          " VALUES (?, ?, 1)",
          (chat_id, target_id),
      )
  elif request.method == "PUT":
    can_write = 1 if data.get("can_write") else 0
    if target_id and target_id != uid:
      conn.execute(
          "UPDATE chat_members SET can_write = ? WHERE chat_id = ? AND user_id"
          " = ?",
          (can_write, chat_id, target_id),
      )
  elif request.method == "DELETE":
    if target_id and target_id != uid:
      conn.execute(
          "DELETE FROM chat_members WHERE chat_id = ? AND user_id = ?",
          (chat_id, target_id),
      )

  conn.commit()
  conn.close()
  return jsonify({"ok": True})


# --- MESSAGES API ---


@app.route("/api/chats/<int:chat_id>/messages", methods=["GET", "POST"])
@login_required
def messages_api(chat_id):
  uid = session["user_id"]
  conn = get_db()
  chat = conn.execute("SELECT * FROM chats WHERE id = ?", (chat_id,)).fetchone()
  member = conn.execute(
      "SELECT * FROM chat_members WHERE chat_id = ? AND user_id = ?",
      (chat_id, uid),
  ).fetchone()
  if not chat or not member:
    conn.close()
    return jsonify({"error": "Нет доступа к чату"}), 403

  if request.method == "GET":
    rows = conn.execute(
        """
            SELECT m.*, u.display_name as sender_name, u.avatar as sender_avatar,
                   rm.content as reply_content, ru.display_name as reply_sender
            FROM messages m
            JOIN users u ON m.sender_id = u.id
            LEFT JOIN messages rm ON m.reply_to_id = rm.id
            LEFT JOIN users ru ON rm.sender_id = ru.id
            WHERE m.chat_id = ?
            ORDER BY m.id ASC
        """,
        (chat_id,),
    ).fetchall()

    msgs = []
    for r in rows:
      d = dict(r)
      d["reactions"] = json.loads(d["reactions"] or "{}")
      d["is_mine"] = d["sender_id"] == uid
      if chat["type"] == "group":
        d["can_delete"] = chat["owner_id"] == uid
      else:
        d["can_delete"] = d["sender_id"] == uid
      d["time"] = str(d["created_at"])[11:16]
      msgs.append(d)
    conn.close()
    return jsonify(msgs)

  if request.method == "POST":
    if not member["can_write"]:
      conn.close()
      return (
          jsonify({"error": "Владелец запретил вам писать в этой группе"}),
          403,
      )
    data = request.get_json(silent=True) or {}
    content = str(data.get("content", "")).strip()
    reply_to_id = data.get("reply_to_id")
    if not content:
      conn.close()
      return jsonify({"error": "Пустое сообщение"}), 400

    conn.execute(
        "INSERT INTO messages (chat_id, sender_id, content, reply_to_id)"
        " VALUES (?, ?, ?, ?)",
        (chat_id, uid, content, reply_to_id),
    )
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/messages/<int:msg_id>", methods=["DELETE"])
@login_required
def delete_message(msg_id):
  uid = session["user_id"]
  conn = get_db()
  msg = conn.execute(
      """
        SELECT m.*, c.type as chat_type, c.owner_id
        FROM messages m
        JOIN chats c ON m.chat_id = c.id
        WHERE m.id = ?
    """,
      (msg_id,),
  ).fetchone()

  if not msg:
    conn.close()
    return jsonify({"error": "Сообщение не найдено"}), 404

  if msg["chat_type"] == "group":
    if msg["owner_id"] != uid:
      conn.close()
      return (
          jsonify({"error": "В группах удалять сообщения может только владелец"}),
          403,
      )
  else:
    if msg["sender_id"] != uid:
      conn.close()
      return jsonify({"error": "Нельзя удалить чужое сообщение"}), 403

  conn.execute("DELETE FROM messages WHERE id = ?", (msg_id,))
  conn.commit()
  conn.close()
  return jsonify({"ok": True})


@app.route("/api/messages/<int:msg_id>/react", methods=["POST"])
@login_required
def react_message(msg_id):
  uid = session["user_id"]
  emoji = str((request.get_json(silent=True) or {}).get("emoji", "")).strip()
  if not emoji:
    return jsonify({"error": "Нет эмодзи"}), 400

  conn = get_db()
  msg = conn.execute(
      "SELECT * FROM messages WHERE id = ?", (msg_id,)
  ).fetchone()
  if not msg:
    conn.close()
    return jsonify({"error": "Сообщение не найдено"}), 404

  member = conn.execute(
      "SELECT 1 FROM chat_members WHERE chat_id = ? AND user_id = ?",
      (msg["chat_id"], uid),
  ).fetchone()
  if not member:
    conn.close()
    return jsonify({"error": "Нет доступа"}), 403

  reactions = json.loads(msg["reactions"] or "{}")
  users_list = reactions.get(emoji, [])
  if uid in users_list:
    users_list.remove(uid)
  else:
    users_list.append(uid)

  if users_list:
    reactions[emoji] = users_list
  else:
    reactions.pop(emoji, None)

  conn.execute(
      "UPDATE messages SET reactions = ? WHERE id = ?",
      (json.dumps(reactions), msg_id),
  )
  conn.commit()
  conn.close()
  return jsonify({"ok": True})


# --- FRONTEND HTML/CSS/JS ---

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<title>Груша — Мессенджер</title>
<style>
  :root {
    --bg: #0e1318;
    --panel: #161d24;
    --panel-hover: #1e2731;
    --border: #25303c;
    --accent: #a3e635;
    --accent-dark: #65a30d;
    --accent-glow: rgba(163, 230, 53, 0.16);
    --text: #f1f5f9;
    --muted: #94a3b8;
    --bubble-in: #1e2731;
    --bubble-out: #283b18;
    --danger: #ef4444;
    --radius: 16px;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Inter, sans-serif; }
  body { background: var(--bg); color: var(--text); height: 100dvh; overflow: hidden; display: flex; }

  ::-webkit-scrollbar { width: 5px; height: 5px; }
  ::-webkit-scrollbar-thumb { background: #2e3b49; border-radius: 99px; }

  #auth-screen {
    position: fixed; inset: 0; background: radial-gradient(circle at top right, #1f2e16, var(--bg) 60%);
    display: flex; align-items: center; justify-content: center; z-index: 100; padding: 20px; overflow-y: auto;
  }
  .auth-card {
    background: var(--panel); border: 1px solid var(--border); border-radius: 24px;
    padding: 28px; width: 100%; max-width: 400px; box-shadow: 0 24px 60px rgba(0,0,0,0.55);
  }
  .brand-logo { font-size: 46px; text-align: center; margin-bottom: 6px; }
  .brand-title { font-size: 26px; font-weight: 800; text-align: center; letter-spacing: -0.5px; }
  .brand-sub { text-align: center; color: var(--muted); font-size: 14px; margin-bottom: 20px; }
  .tabs { display: flex; background: var(--bg); border-radius: 12px; padding: 4px; margin-bottom: 18px; gap: 4px; }
  .tab {
    flex: 1; text-align: center; padding: 10px 8px; border-radius: 9px; cursor: pointer;
    font-weight: 700; font-size: 14px; color: var(--muted); background: transparent;
    transition: .2s; touch-action: manipulation;
  }
  .tab.active { background: var(--panel-hover) !important; color: var(--accent); }

  input, button { font-size: 15px; outline: none; border: none; }
  .field { margin-bottom: 14px; }
  .field label { display: block; font-size: 12px; color: var(--muted); margin-bottom: 6px; font-weight: 600; }
  .input {
    width: 100%; background: var(--bg); border: 1px solid var(--border); color: var(--text);
    padding: 12px 14px; border-radius: 12px; transition: .2s;
  }
  .input:focus { border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-glow); }
  .btn {
    width: 100%; padding: 13px; border-radius: 12px; font-weight: 700; cursor: pointer;
    background: var(--accent); color: #0f1705; transition: .2s; touch-action: manipulation;
  }
  .btn:hover { filter: brightness(1.08); transform: translateY(-1px); }
  .btn-secondary { background: var(--panel-hover); color: var(--text); border: 1px solid var(--border); }
  .btn-danger { background: rgba(239,68,68,0.15); color: var(--danger); border: 1px solid rgba(239,68,68,0.3); }
  .btn-danger:hover { background: var(--danger); color: #fff; }
  .err-msg { color: var(--danger); font-size: 13px; text-align: center; margin-bottom: 12px; min-height: 18px; font-weight: 600; }

  .emoji-grid { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 6px; }
  .emoji-opt {
    width: 38px; height: 38px; display: flex; align-items: center; justify-content: center;
    background: var(--bg); border: 1px solid var(--border); border-radius: 10px;
    font-size: 20px; cursor: pointer; transition: .15s; touch-action: manipulation;
  }
  .emoji-opt:hover, .emoji-opt.selected { border-color: var(--accent); background: var(--accent-glow); transform: scale(1.08); }

  #app { display: flex; width: 100%; height: 100%; }
  .sidebar {
    width: 350px; background: var(--panel); border-right: 1px solid var(--border);
    display: flex; flex-direction: column; flex-shrink: 0;
  }
  .sidebar-top {
    padding: 14px 16px; display: flex; align-items: center; justify-content: space-between;
    border-bottom: 1px solid var(--border); gap: 10px;
  }
  .me-pill {
    display: flex; align-items: center; gap: 10px; cursor: pointer; padding: 6px 10px;
    border-radius: 14px; transition: .2s; flex: 1; min-width: 0;
  }
  .me-pill:hover { background: var(--panel-hover); }
  .avatar {
    width: 44px; height: 44px; border-radius: 14px; background: var(--bg);
    border: 1px solid var(--border); display: flex; align-items: center; justify-content: center;
    font-size: 23px; flex-shrink: 0;
  }
  .me-info { min-width: 0; }
  .me-name { font-weight: 700; font-size: 15px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .me-sub { font-size: 12px; color: var(--accent); }
  .icon-btn {
    width: 40px; height: 40px; border-radius: 12px; background: var(--bg);
    border: 1px solid var(--border); color: var(--text); font-size: 18px;
    cursor: pointer; display: flex; align-items: center; justify-content: center; flex-shrink: 0; transition: .2s;
  }
  .icon-btn:hover { border-color: var(--accent); color: var(--accent); }

  .search-box { padding: 12px 16px; border-bottom: 1px solid var(--border); position: relative; }
  .search-box input { padding-left: 36px; border-radius: 12px; }
  .search-icon { position: absolute; left: 28px; top: 24px; color: var(--muted); font-size: 14px; }

  .chat-list { flex: 1; overflow-y: auto; padding: 8px; }
  .section-label { font-size: 11px; text-transform: uppercase; letter-spacing: 0.8px; color: var(--muted); padding: 8px 10px; font-weight: 700; }
  .chat-item {
    display: flex; align-items: center; gap: 12px; padding: 10px 12px; border-radius: 14px;
    cursor: pointer; transition: .15s; margin-bottom: 2px;
  }
  .chat-item:hover { background: var(--panel-hover); }
  .chat-item.active { background: var(--accent-glow); border: 1px solid rgba(163, 230, 53, 0.28); }
  .chat-meta { flex: 1; min-width: 0; }
  .chat-row { display: flex; justify-content: space-between; align-items: center; margin-bottom: 3px; }
  .chat-title { font-weight: 600; font-size: 15px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .chat-time { font-size: 11px; color: var(--muted); }
  .chat-preview { font-size: 13px; color: var(--muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }

  .main { flex: 1; display: flex; flex-direction: column; background: var(--bg); position: relative; min-width: 0; }
  .empty-state {
    flex: 1; display: flex; flex-direction: column; align-items: center; justify-content: center;
    color: var(--muted); text-align: center; padding: 24px;
  }
  .empty-state .big-pear { font-size: 68px; margin-bottom: 12px; filter: drop-shadow(0 10px 25px rgba(163,230,53,0.2)); }

  .chat-header {
    height: 68px; padding: 0 18px; background: var(--panel); border-bottom: 1px solid var(--border);
    display: flex; align-items: center; justify-content: space-between; gap: 12px;
  }
  .header-info { display: flex; align-items: center; gap: 12px; cursor: pointer; padding: 6px 10px; border-radius: 14px; transition: .2s; min-width: 0; }
  .header-info:hover { background: var(--panel-hover); }
  .back-btn { display: none; }

  .messages-box {
    flex: 1; overflow-y: auto; padding: 20px; display: flex; flex-direction: column; gap: 10px;
    background-image: radial-gradient(rgba(163, 230, 53, 0.04) 1px, transparent 1px);
    background-size: 24px 24px;
  }
  .msg-row { display: flex; gap: 10px; max-width: 78%; align-items: flex-end; }
  .msg-row.mine { align-self: flex-end; flex-direction: row-reverse; }
  .msg-avatar { width: 32px; height: 32px; border-radius: 10px; background: var(--panel); display: flex; align-items: center; justify-content: center; font-size: 17px; flex-shrink: 0; }
  .msg-bubble {
    background: var(--bubble-in); border: 1px solid var(--border); padding: 10px 14px;
    border-radius: 18px 18px 18px 4px; position: relative; word-wrap: break-word;
  }
  .msg-row.mine .msg-bubble {
    background: var(--bubble-out); border-color: rgba(163, 230, 53, 0.25);
    border-radius: 18px 18px 4px 18px;
  }
  .msg-sender { font-size: 12px; font-weight: 700; color: var(--accent); margin-bottom: 4px; }
  .reply-quote {
    background: rgba(0,0,0,0.28); border-left: 3px solid var(--accent); padding: 5px 9px;
    border-radius: 6px; margin-bottom: 6px; font-size: 12px; cursor: pointer;
  }
  .reply-quote strong { color: var(--accent); display: block; }
  .reply-quote span { color: var(--muted); display: -webkit-box; -webkit-line-clamp: 1; -webkit-box-orient: vertical; overflow: hidden; }
  .msg-text { font-size: 15px; line-height: 1.42; white-space: pre-wrap; }
  .msg-footer { display: flex; align-items: center; justify-content: flex-end; gap: 8px; margin-top: 5px; }
  .msg-time { font-size: 11px; color: var(--muted); }

  .reactions-bar { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 6px; }
  .reaction-chip {
    background: rgba(255,255,255,0.06); border: 1px solid var(--border); border-radius: 99px;
    padding: 2px 8px; font-size: 12px; cursor: pointer; display: flex; align-items: center; gap: 4px; transition: .15s;
  }
  .reaction-chip.active { background: var(--accent-glow); border-color: var(--accent); color: var(--accent); }
  .msg-actions {
    opacity: 0; transition: .15s; display: flex; gap: 4px; align-items: center;
  }
  .msg-row:hover .msg-actions { opacity: 1; }
  .act-btn {
    background: var(--panel); border: 1px solid var(--border); color: var(--muted);
    border-radius: 8px; padding: 4px 7px; font-size: 12px; cursor: pointer;
  }
  .act-btn:hover { color: var(--text); border-color: var(--accent); }
  .act-btn.del:hover { color: var(--danger); border-color: var(--danger); }

  .reply-bar {
    background: var(--panel); border-top: 1px solid var(--border); padding: 8px 18px;
    display: flex; align-items: center; justify-content: space-between; font-size: 13px;
  }
  .composer {
    padding: 12px 16px; background: var(--panel); border-top: 1px solid var(--border);
    display: flex; gap: 10px; align-items: center;
  }
  .composer input { flex: 1; border-radius: 14px; }
  .send-btn {
    width: 46px; height: 46px; border-radius: 14px; background: var(--accent); color: #0f1705;
    font-size: 19px; font-weight: bold; cursor: pointer; display: flex; align-items: center; justify-content: center;
  }
  .readonly-banner {
    padding: 15px; text-align: center; background: var(--panel); border-top: 1px solid var(--border);
    color: var(--muted); font-size: 14px;
  }

  .modal-backdrop {
    position: fixed; inset: 0; background: rgba(0,0,0,0.7); backdrop-filter: blur(4px);
    display: none; align-items: center; justify-content: center; z-index: 200; padding: 16px;
  }
  .modal-backdrop.open { display: flex; }
  .modal {
    background: var(--panel); border: 1px solid var(--border); border-radius: 22px;
    width: 100%; max-width: 440px; max-height: 88vh; display: flex; flex-direction: column;
    overflow: hidden; box-shadow: 0 20px 50px rgba(0,0,0,0.6);
  }
  .modal-head {
    padding: 16px 20px; border-bottom: 1px solid var(--border);
    display: flex; justify-content: space-between; align-items: center; font-weight: 700; font-size: 17px;
  }
  .modal-body { padding: 20px; overflow-y: auto; }
  .member-row {
    display: flex; align-items: center; justify-content: space-between; padding: 10px 0;
    border-bottom: 1px solid var(--border); gap: 10px;
  }
  .member-row:last-child { border-bottom: none; }
  .badge { font-size: 11px; padding: 2px 7px; border-radius: 6px; background: var(--accent-glow); color: var(--accent); font-weight: 600; }
  .mini-btn { padding: 5px 9px; border-radius: 8px; font-size: 12px; cursor: pointer; font-weight: 600; }

  .react-popover {
    position: fixed; background: var(--panel); border: 1px solid var(--border); border-radius: 99px;
    padding: 6px 10px; display: none; gap: 6px; z-index: 300; box-shadow: 0 10px 30px rgba(0,0,0,0.5);
  }
  .react-popover span { font-size: 20px; cursor: pointer; transition: .15s; }
  .react-popover span:hover { transform: scale(1.25); }

  @media (max-width: 768px) {
    .sidebar { width: 100%; }
    .main { display: none; width: 100%; }
    body.chat-open .sidebar { display: none; }
    body.chat-open .main { display: flex; }
    .back-btn { display: flex; }
    .msg-row { max-width: 88%; }
    .msg-actions { opacity: 1; }
  }
</style>
</head>
<body>

<div id="auth-screen">
  <div class="auth-card">
    <div class="brand-logo">🍐</div>
    <div class="brand-title">Груша</div>
    <div class="brand-sub">Свежий и быстрый мессенджер</div>
    <div class="tabs">
      <button type="button" class="tab active" id="tab-login" onclick="switchAuthTab('login')">Вход</button>
      <button type="button" class="tab" id="tab-reg" onclick="switchAuthTab('reg')">Регистрация</button>
    </div>
    <div class="err-msg" id="auth-err"></div>
    <div class="field">
      <label>Логин (@username)</label>
      <input class="input" id="auth-user" placeholder="например: alex_pear" autocomplete="username">
    </div>
    <div class="field" id="reg-name-wrap" style="display:none;">
      <label>Ваше имя (никнейм)</label>
      <input class="input" id="auth-name" placeholder="Как вас подписать?" autocomplete="name">
    </div>
    <div class="field">
      <label>Пароль</label>
      <input class="input" type="password" id="auth-pass" placeholder="••••••••" autocomplete="current-password">
    </div>
    <div class="field" id="reg-avatar-wrap" style="display:none;">
      <label>Выберите эмодзи-аватар</label>
      <div class="emoji-grid" id="reg-emojis"></div>
    </div>
    <button type="button" class="btn" id="auth-submit" onclick="submitAuth()">Войти в Грушу</button>
  </div>
</div>

<div id="app" style="display:none;">
  <div class="sidebar">
    <div class="sidebar-top">
      <div class="me-pill" onclick="openProfileModal()">
        <div class="avatar" id="me-avatar">🍐</div>
        <div class="me-info">
          <div class="me-name" id="me-name">Загрузка...</div>
          <div class="me-sub" id="me-username">@user</div>
        </div>
      </div>
      <button type="button" class="icon-btn" title="Создать группу" onclick="openCreateGroupModal()">👥+</button>
    </div>

    <div class="search-box">
      <span class="search-icon">🔍</span>
      <input class="input" id="search-input" placeholder="Поиск людей или чатов..." oninput="handleSearch(this.value)">
    </div>

    <div class="chat-list" id="sidebar-list"></div>
  </div>

  <div class="main">
    <div class="empty-state" id="empty-state">
      <div class="big-pear">🍐</div>
      <h2>Добро пожаловать в Грушу</h2>
      <p style="margin-top:6px; max-width:320px;">Найдите друзей через поиск слева или создайте новую группу для общения.</p>
    </div>

    <div id="active-chat-wrap" style="display:none; flex-direction:column; height:100%;">
      <div class="chat-header">
        <div style="display:flex; align-items:center; gap:8px; min-width:0; flex:1;">
          <button type="button" class="icon-btn back-btn" onclick="closeMobileChat()">←</button>
          <div class="header-info" onclick="openChatSettings()">
            <div class="avatar" id="chat-avatar">🍐</div>
            <div style="min-width:0;">
              <div class="chat-title" id="chat-title">Название</div>
              <div class="chat-preview" id="chat-subtitle">Нажмите для настроек</div>
            </div>
          </div>
        </div>
        <button type="button" class="icon-btn" title="Настройки чата" onclick="openChatSettings()">⚙️</button>
      </div>

      <div class="messages-box" id="messages-box"></div>

      <div class="reply-bar" id="reply-bar" style="display:none;">
        <div>
          <span style="color:var(--accent); font-weight:700;" id="reply-to-name">Ответ</span>:
          <span style="color:var(--muted);" id="reply-to-text">текст</span>
        </div>
        <button type="button" style="background:none; color:var(--muted); cursor:pointer; font-size:18px;" onclick="cancelReply()">✕</button>
      </div>

      <div class="composer" id="composer">
        <input class="input" id="msg-input" placeholder="Написать сообщение..." onkeydown="if(event.key==='Enter') sendMessage()">
        <button type="button" class="send-btn" onclick="sendMessage()">➤</button>
      </div>
      <div class="readonly-banner" id="readonly-banner" style="display:none;">
        🔇 Владелец группы ограничил вам отправку сообщений
      </div>
    </div>
  </div>
</div>

<div class="modal-backdrop" id="profile-modal">
  <div class="modal">
    <div class="modal-head">
      <span>Настройки профиля</span>
      <button type="button" style="background:none; color:var(--muted); cursor:pointer; font-size:20px;" onclick="closeModal('profile-modal')">✕</button>
    </div>
    <div class="modal-body">
      <div class="field">
        <label>Никнейм</label>
        <input class="input" id="edit-display-name">
      </div>
      <div class="field">
        <label>Аватарка эмодзи (выберите или впишите свой)</label>
        <input class="input" id="edit-avatar" maxlength="4" style="font-size:22px; text-align:center; margin-bottom:8px;">
        <div class="emoji-grid" id="profile-emojis"></div>
      </div>
      <button type="button" class="btn" style="margin-bottom:10px;" onclick="saveProfile()">Сохранить изменения</button>
      <button type="button" class="btn btn-secondary" style="margin-bottom:20px;" onclick="logout()">Выйти из аккаунта</button>
      <hr style="border:none; border-top:1px solid var(--border); margin-bottom:16px;">
      <button type="button" class="btn btn-danger" onclick="deleteAccount()">🗑️ Удалить аккаунт навсегда</button>
    </div>
  </div>
</div>

<div class="modal-backdrop" id="group-create-modal">
  <div class="modal">
    <div class="modal-head">
      <span>Новая группа</span>
      <button type="button" style="background:none; color:var(--muted); cursor:pointer; font-size:20px;" onclick="closeModal('group-create-modal')">✕</button>
    </div>
    <div class="modal-body">
      <div class="field">
        <label>Название группы</label>
        <input class="input" id="new-group-title" placeholder="Например: Клуб любителей груш">
      </div>
      <div class="field">
        <label>Эмодзи-аватар группы</label>
        <input class="input" id="new-group-avatar" value="🍐" maxlength="4" style="font-size:22px; text-align:center; margin-bottom:8px;">
        <div class="emoji-grid" id="group-create-emojis"></div>
      </div>
      <div class="field">
        <label>Добавить участников</label>
        <input class="input" placeholder="Поиск людей..." oninput="searchForGroupCreate(this.value)" style="margin-bottom:8px;">
        <div id="group-create-users" style="max-height:160px; overflow-y:auto;"></div>
      </div>
      <button type="button" class="btn" onclick="createGroup()">Создать группу</button>
    </div>
  </div>
</div>

<div class="modal-backdrop" id="chat-settings-modal">
  <div class="modal">
    <div class="modal-head">
      <span id="settings-modal-title">Настройки чата</span>
      <button type="button" style="background:none; color:var(--muted); cursor:pointer; font-size:20px;" onclick="closeModal('chat-settings-modal')">✕</button>
    </div>
    <div class="modal-body" id="chat-settings-body"></div>
  </div>
</div>

<div class="react-popover" id="react-popover">
  <span onclick="sendReaction('❤️')">❤️</span>
  <span onclick="sendReaction('👍')">👍</span>
  <span onclick="sendReaction('😂')">😂</span>
  <span onclick="sendReaction('😮')">😮</span>
  <span onclick="sendReaction('😢')">😢</span>
  <span onclick="sendReaction('🔥')">🔥</span>
  <span onclick="sendReaction('🍐')">🍐</span>
</div>

<script>
const EMOJIS = ['🍐','😎','🦊','🐱','🐼','🐸','🦁','🐵','🤖','👽','👻','🔥','💎','🚀','🌈','🍕','🎮','🎧','👑','🍀'];
let authMode = 'login';
let selectedRegEmoji = '🍐';
let currentUser = null;
let currentChatId = null;
let currentChatInfo = null;
let replyToMsg = null;
let reactTargetMsgId = null;
let selectedGroupMembers = new Set();
let lastRenderedHash = '';

function renderEmojiPicker(containerId, onSelect, currentVal = '🍐') {
  const box = document.getElementById(containerId);
  if (!box) return;
  box.innerHTML = '';
  EMOJIS.forEach(em => {
    const el = document.createElement('button');
    el.type = 'button';
    el.className = 'emoji-opt' + (em === currentVal ? ' selected' : '');
    el.textContent = em;
    el.onclick = () => {
      box.querySelectorAll('.emoji-opt').forEach(x => x.classList.remove('selected'));
      el.classList.add('selected');
      onSelect(em);
    };
    box.appendChild(el);
  });
}

function switchAuthTab(mode) {
  authMode = mode;
  document.getElementById('tab-login').classList.toggle('active', mode === 'login');
  document.getElementById('tab-reg').classList.toggle('active', mode === 'reg');
  document.getElementById('reg-name-wrap').style.display = mode === 'reg' ? 'block' : 'none';
  document.getElementById('reg-avatar-wrap').style.display = mode === 'reg' ? 'block' : 'none';
  document.getElementById('auth-submit').textContent = mode === 'login' ? 'Войти в Грушу' : 'Создать аккаунт';
  document.getElementById('auth-err').textContent = '';
  if (mode === 'reg') {
    renderEmojiPicker('reg-emojis', em => selectedRegEmoji = em, selectedRegEmoji);
  }
}

async function submitAuth() {
  const errEl = document.getElementById('auth-err');
  errEl.textContent = '';
  const username = document.getElementById('auth-user').value;
  const password = document.getElementById('auth-pass').value;
  const display_name = document.getElementById('auth-name').value;
  const url = authMode === 'login' ? '/api/login' : '/api/register';

  try {
    const res = await fetch(url, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({username, password, display_name, avatar: selectedRegEmoji})
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      errEl.textContent = data.error || `Ошибка (${res.status})`;
      return;
    }
    initApp();
  } catch (e) {
    errEl.textContent = 'Ошибка сети при подключении к серверу';
  }
}

async function initApp() {
  try {
    const res = await fetch('/api/me');
    if (!res.ok) {
      document.getElementById('auth-screen').style.display = 'flex';
      document.getElementById('app').style.display = 'none';
      return;
    }
    currentUser = await res.json();
    document.getElementById('auth-screen').style.display = 'none';
    document.getElementById('app').style.display = 'flex';
    document.getElementById('me-avatar').textContent = currentUser.avatar;
    document.getElementById('me-name').textContent = currentUser.display_name;
    document.getElementById('me-username').textContent = '@' + currentUser.username;
    loadChats();
  } catch (e) {
    document.getElementById('auth-screen').style.display = 'flex';
  }
}

async function loadChats() {
  if (document.getElementById('search-input').value.trim()) return;
  const res = await fetch('/api/chats');
  if (!res.ok) return;
  const chats = await res.json();
  const list = document.getElementById('sidebar-list');
  if (!chats.length) {
    list.innerHTML = `<div style="text-align:center; color:var(--muted); padding:30px 15px; font-size:14px;">Пока нет чатов.<br>Введите имя в поиске выше, чтобы найти людей!</div>`;
    return;
  }
  list.innerHTML = `<div class="section-label">Ваши чаты</div>` + chats.map(c => `
    <div class="chat-item ${c.id === currentChatId ? 'active' : ''}" onclick="openChat(${c.id})">
      <div class="avatar">${esc(c.avatar)}</div>
      <div class="chat-meta">
        <div class="chat-row">
          <div class="chat-title">${c.type === 'group' ? '👥 ' : ''}${esc(c.title)}</div>
          <div class="chat-time">${esc(c.last_time)}</div>
        </div>
        <div class="chat-preview">${esc(c.last_message)}</div>
      </div>
    </div>
  `).join('');
}

async function handleSearch(q) {
  q = q.trim();
  if (!q) {
    loadChats();
    return;
  }
  const res = await fetch('/api/users/search?q=' + encodeURIComponent(q));
  const users = await res.json();
  const list = document.getElementById('sidebar-list');
  if (!users.length) {
    list.innerHTML = `<div style="text-align:center; color:var(--muted); padding:25px;">Никого не найдено</div>`;
    return;
  }
  list.innerHTML = `<div class="section-label">Найденные люди</div>` + users.map(u => `
    <div class="chat-item" onclick="startPrivateChat(${u.id})">
      <div class="avatar">${esc(u.avatar)}</div>
      <div class="chat-meta">
        <div class="chat-title">${esc(u.display_name)}</div>
        <div class="chat-preview">@${esc(u.username)}</div>
      </div>
    </div>
  `).join('');
}

async function startPrivateChat(userId) {
  const res = await fetch('/api/chats/private', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({user_id: userId})
  });
  const data = await res.json();
  document.getElementById('search-input').value = '';
  await loadChats();
  openChat(data.chat_id);
}

async function openChat(chatId) {
  currentChatId = chatId;
  lastRenderedHash = '';
  cancelReply();
  document.body.classList.add('chat-open');
  document.getElementById('empty-state').style.display = 'none';
  document.getElementById('active-chat-wrap').style.display = 'flex';
  await refreshActiveChat(true);
  loadChats();
}

function closeMobileChat() {
  document.body.classList.remove('chat-open');
  currentChatId = null;
}

async function refreshActiveChat(scrollBottom = false) {
  if (!currentChatId) return;
  const infoRes = await fetch(`/api/chats/${currentChatId}`);
  if (!infoRes.ok) {
    closeMobileChat();
    loadChats();
    return;
  }
  currentChatInfo = await infoRes.json();
  document.getElementById('chat-avatar').textContent = currentChatInfo.avatar;
  document.getElementById('chat-title').textContent = currentChatInfo.title;
  document.getElementById('chat-subtitle').textContent =
    currentChatInfo.type === 'group'
      ? `${currentChatInfo.members.length} участн. • нажмите для управления`
      : (currentChatInfo.subtitle || 'Личный чат');

  if (currentChatInfo.my_can_write) {
    document.getElementById('composer').style.display = 'flex';
    document.getElementById('readonly-banner').style.display = 'none';
  } else {
    document.getElementById('composer').style.display = 'none';
    document.getElementById('readonly-banner').style.display = 'block';
  }

  const msgRes = await fetch(`/api/chats/${currentChatId}/messages`);
  const msgs = await msgRes.json();
  const hash = JSON.stringify(msgs);
  if (hash === lastRenderedHash) return;
  lastRenderedHash = hash;

  const box = document.getElementById('messages-box');
  const wasAtBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 90;

  box.innerHTML = msgs.map(m => {
    const reactionsHtml = Object.entries(m.reactions || {}).map(([em, users]) => `
      <div class="reaction-chip ${users.includes(currentUser.id) ? 'active' : ''}" onclick="toggleReaction(${m.id}, '${em}')">
        ${em} <span>${users.length}</span>
      </div>
    `).join('');

    const replyHtml = m.reply_to_id ? `
      <div class="reply-quote">
        <strong>${esc(m.reply_sender || 'Сообщение')}</strong>
        <span>${esc(m.reply_content || 'Удалено')}</span>
      </div>
    ` : '';

    return `
      <div class="msg-row ${m.is_mine ? 'mine' : ''}">
        ${!m.is_mine ? `<div class="msg-avatar">${esc(m.sender_avatar)}</div>` : ''}
        <div class="msg-bubble">
          ${(!m.is_mine && currentChatInfo.type === 'group') ? `<div class="msg-sender">${esc(m.sender_name)}</div>` : ''}
          ${replyHtml}
          <div class="msg-text">${esc(m.content)}</div>
          ${reactionsHtml ? `<div class="reactions-bar">${reactionsHtml}</div>` : ''}
          <div class="msg-footer">
            <span class="msg-time">${m.time}</span>
          </div>
        </div>
        <div class="msg-actions">
          <button type="button" class="act-btn" title="Реакция" onclick="openReactPopover(event, ${m.id})">😊</button>
          <button type="button" class="act-btn" title="Ответить" onclick="setReply(${m.id}, '${escAttr(m.sender_name)}', '${escAttr(m.content)}')">↩️</button>
          ${m.can_delete ? `<button type="button" class="act-btn del" title="Удалить" onclick="deleteMessage(${m.id})">🗑️</button>` : ''}
        </div>
      </div>
    `;
  }).join('');

  if (scrollBottom || wasAtBottom) {
    box.scrollTop = box.scrollHeight;
  }
}

async function sendMessage() {
  const input = document.getElementById('msg-input');
  const content = input.value.trim();
  if (!content || !currentChatId) return;
  input.value = '';
  const payload = { content, reply_to_id: replyToMsg ? replyToMsg.id : null };
  cancelReply();
  const res = await fetch(`/api/chats/${currentChatId}/messages`, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload)
  });
  if (!res.ok) {
    const err = await res.json();
    alert(err.error || 'Ошибка отправки');
  }
  await refreshActiveChat(true);
  loadChats();
}

function setReply(id, sender, text) {
  replyToMsg = { id, sender, text };
  document.getElementById('reply-bar').style.display = 'flex';
  document.getElementById('reply-to-name').textContent = sender;
  document.getElementById('reply-to-text').textContent = text;
  document.getElementById('msg-input').focus();
}

function cancelReply() {
  replyToMsg = null;
  document.getElementById('reply-bar').style.display = 'none';
}

async function deleteMessage(msgId) {
  if (!confirm('Удалить это сообщение?')) return;
  const res = await fetch(`/api/messages/${msgId}`, { method: 'DELETE' });
  if (!res.ok) {
    const d = await res.json();
    alert(d.error || 'Нельзя удалить');
  }
  refreshActiveChat(false);
  loadChats();
}

function openReactPopover(e, msgId) {
  e.stopPropagation();
  reactTargetMsgId = msgId;
  const pop = document.getElementById('react-popover');
  pop.style.display = 'flex';
  pop.style.left = Math.min(window.innerWidth - 260, Math.max(10, e.clientX - 100)) + 'px';
  pop.style.top = Math.max(10, e.clientY - 50) + 'px';
}

document.addEventListener('click', () => {
  document.getElementById('react-popover').style.display = 'none';
});

async function sendReaction(emoji) {
  if (!reactTargetMsgId) return;
  await toggleReaction(reactTargetMsgId, emoji);
}

async function toggleReaction(msgId, emoji) {
  await fetch(`/api/messages/${msgId}/react`, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ emoji })
  });
  refreshActiveChat(false);
}

function openProfileModal() {
  document.getElementById('edit-display-name').value = currentUser.display_name;
  document.getElementById('edit-avatar').value = currentUser.avatar;
  renderEmojiPicker('profile-emojis', em => {
    document.getElementById('edit-avatar').value = em;
  }, currentUser.avatar);
  openModal('profile-modal');
}

async function saveProfile() {
  const display_name = document.getElementById('edit-display-name').value.trim();
  const avatar = document.getElementById('edit-avatar').value.trim();
  await fetch('/api/me', {
    method: 'PUT',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ display_name, avatar })
  });
  closeModal('profile-modal');
  initApp();
}

async function logout() {
  await fetch('/api/logout', { method: 'POST' });
  location.reload();
}

async function deleteAccount() {
  if (!confirm('Точно удалить аккаунт? Все ваши данные и личные чаты будут стёрты безвозвратно.')) return;
  await fetch('/api/me', { method: 'DELETE' });
  location.reload();
}

function openCreateGroupModal() {
  selectedGroupMembers.clear();
  document.getElementById('new-group-title').value = '';
  document.getElementById('new-group-avatar').value = '🍐';
  renderEmojiPicker('group-create-emojis', em => {
    document.getElementById('new-group-avatar').value = em;
  }, '🍐');
  searchForGroupCreate('');
  openModal('group-create-modal');
}

async function searchForGroupCreate(q) {
  const res = await fetch('/api/users/search?q=' + encodeURIComponent(q));
  const users = await res.json();
  const box = document.getElementById('group-create-users');
  box.innerHTML = users.map(u => {
    const checked = selectedGroupMembers.has(u.id);
    return `
      <div class="member-row" style="cursor:pointer;" onclick="toggleGroupMemberSelect(${u.id}, this)">
        <div style="display:flex; align-items:center; gap:10px;">
          <div class="avatar" style="width:36px; height:36px; font-size:18px;">${esc(u.avatar)}</div>
          <div>
            <div style="font-weight:600; font-size:14px;">${esc(u.display_name)}</div>
            <div style="font-size:12px; color:var(--muted);">@${esc(u.username)}</div>
          </div>
        </div>
        <input type="checkbox" ${checked ? 'checked' : ''} style="width:18px; height:18px; accent-color:var(--accent);">
      </div>
    `;
  }).join('');
}

function toggleGroupMemberSelect(uid, rowEl) {
  const cb = rowEl.querySelector('input');
  if (selectedGroupMembers.has(uid)) {
    selectedGroupMembers.delete(uid);
    cb.checked = false;
  } else {
    selectedGroupMembers.add(uid);
    cb.checked = true;
  }
}

async function createGroup() {
  const title = document.getElementById('new-group-title').value.trim();
  const avatar = document.getElementById('new-group-avatar').value.trim() || '🍐';
  if (!title) return alert('Введите название группы');
  const res = await fetch('/api/chats/group', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ title, avatar, member_ids: Array.from(selectedGroupMembers) })
  });
  const data = await res.json();
  if (!res.ok) return alert(data.error);
  closeModal('group-create-modal');
  await loadChats();
  openChat(data.chat_id);
}

async function openChatSettings() {
  if (!currentChatInfo) return;
  const body = document.getElementById('chat-settings-body');
  if (currentChatInfo.type === 'private') {
    document.getElementById('settings-modal-title').textContent = 'Личный чат';
    body.innerHTML = `
      <div style="text-align:center; margin-bottom:20px;">
        <div style="font-size:56px; margin-bottom:8px;">${esc(currentChatInfo.avatar)}</div>
        <h3>${esc(currentChatInfo.title)}</h3>
        <p style="color:var(--muted); font-size:14px;">${esc(currentChatInfo.subtitle || '')}</p>
      </div>
      <button type="button" class="btn btn-danger" onclick="deleteCurrentChat()">🗑️ Удалить чат</button>
    `;
  } else {
    document.getElementById('settings-modal-title').textContent = 'Управление группой';
    const isOwner = currentChatInfo.is_owner;
    let ownerControls = '';
    if (isOwner) {
      ownerControls = `
        <div class="field">
          <label>Название группы</label>
          <input class="input" id="grp-edit-title" value="${escAttr(currentChatInfo.title)}">
        </div>
        <div class="field">
          <label>Аватарка эмодзи</label>
          <input class="input" id="grp-edit-avatar" value="${escAttr(currentChatInfo.avatar)}" maxlength="4" style="font-size:22px; text-align:center; margin-bottom:8px;">
          <div class="emoji-grid" id="grp-settings-emojis"></div>
        </div>
        <button type="button" class="btn" style="margin-bottom:18px;" onclick="saveGroupInfo()">Сохранить оформление</button>
        <div class="field">
          <label>Добавить участника (поиск по логину)</label>
          <input class="input" placeholder="Введите имя или логин..." oninput="searchAddMember(this.value)">
          <div id="grp-add-results"></div>
        </div>
      `;
    }

    const membersHtml = currentChatInfo.members.map(m => {
      const mIsOwner = m.id === currentChatInfo.owner_id;
      let btns = '';
      if (isOwner && !mIsOwner) {
        btns = `
          <div style="display:flex; gap:6px;">
            <button type="button" class="mini-btn ${m.can_write ? 'btn-secondary' : 'btn-danger'}"
              onclick="toggleMemberWrite(${m.id}, ${m.can_write ? 0 : 1})">
              ${m.can_write ? '✍️ Пишет' : '🔇 Без звука'}
            </button>
            <button type="button" class="mini-btn btn-danger" onclick="kickMember(${m.id})">✕</button>
          </div>
        `;
      }
      return `
        <div class="member-row">
          <div style="display:flex; align-items:center; gap:10px; min-width:0;">
            <div class="avatar" style="width:36px; height:36px; font-size:18px;">${esc(m.avatar)}</div>
            <div style="min-width:0;">
              <div style="font-weight:600; font-size:14px;">
                ${esc(m.display_name)} ${mIsOwner ? '<span class="badge">Владелец</span>' : ''}
              </div>
              <div style="font-size:12px; color:var(--muted);">@${esc(m.username)}</div>
            </div>
          </div>
          ${btns}
        </div>
      `;
    }).join('');

    body.innerHTML = `
      ${ownerControls}
      <div class="section-label" style="padding-left:0;">Участники (${currentChatInfo.members.length})</div>
      <div style="margin-bottom:18px;">${membersHtml}</div>
      <button type="button" class="btn btn-danger" onclick="deleteCurrentChat()">
        ${isOwner ? '🗑️ Удалить группу для всех' : '🚪 Покинуть группу'}
      </button>
    `;

    if (isOwner) {
      renderEmojiPicker('grp-settings-emojis', em => {
        document.getElementById('grp-edit-avatar').value = em;
      }, currentChatInfo.avatar);
    }
  }
  openModal('chat-settings-modal');
}

async function saveGroupInfo() {
  const title = document.getElementById('grp-edit-title').value.trim();
  const avatar = document.getElementById('grp-edit-avatar').value.trim();
  await fetch(`/api/chats/${currentChatId}`, {
    method: 'PUT',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ title, avatar })
  });
  await refreshActiveChat(false);
  loadChats();
  openChatSettings();
}

async function searchAddMember(q) {
  q = q.trim();
  const box = document.getElementById('grp-add-results');
  if (!q) { box.innerHTML = ''; return; }
  const res = await fetch('/api/users/search?q=' + encodeURIComponent(q));
  const users = await res.json();
  const existingIds = new Set(currentChatInfo.members.map(m => m.id));
  const filtered = users.filter(u => !existingIds.has(u.id));
  box.innerHTML = filtered.map(u => `
    <div class="member-row">
      <span>${esc(u.avatar)} ${esc(u.display_name)} (@${esc(u.username)})</span>
      <button type="button" class="mini-btn btn" style="width:auto;" onclick="addMemberToGroup(${u.id})">+ Добавить</button>
    </div>
  `).join('');
}

async function addMemberToGroup(userId) {
  await fetch(`/api/chats/${currentChatId}/members`, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ user_id: userId })
  });
  await refreshActiveChat(false);
  openChatSettings();
}

async function toggleMemberWrite(userId, canWrite) {
  await fetch(`/api/chats/${currentChatId}/members`, {
    method: 'PUT',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ user_id: userId, can_write: canWrite })
  });
  await refreshActiveChat(false);
  openChatSettings();
}

async function kickMember(userId) {
  if (!confirm('Удалить участника из группы?')) return;
  await fetch(`/api/chats/${currentChatId}/members`, {
    method: 'DELETE',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ user_id: userId })
  });
  await refreshActiveChat(false);
  openChatSettings();
}

async function deleteCurrentChat() {
  if (!confirm('Вы уверены?')) return;
  await fetch(`/api/chats/${currentChatId}`, { method: 'DELETE' });
  closeModal('chat-settings-modal');
  closeMobileChat();
  document.getElementById('active-chat-wrap').style.display = 'none';
  document.getElementById('empty-state').style.display = 'flex';
  loadChats();
}

function openModal(id) { document.getElementById(id).classList.add('open'); }
function closeModal(id) { document.getElementById(id).classList.remove('open'); }
function esc(s) { return String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function escAttr(s) { return esc(s).replace(/\n/g, ' '); }

initApp();
setInterval(() => {
  if (currentUser) {
    loadChats();
    if (currentChatId) refreshActiveChat(false);
  }
}, 1500);
</script>
</body>
</html>
"""


@app.route("/")
def index():
  return render_template_string(HTML_TEMPLATE)


if __name__ == "__main__":
  port = int(os.environ.get("PORT", 5000))
  app.run(host="0.0.0.0", port=port)
