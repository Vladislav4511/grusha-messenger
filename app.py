import json
import os
import time
from flask import Flask, request, jsonify, render_template_string, make_response

app = Flask(__name__)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.path.join(BASE_DIR, "db.json")
SEVEN_DAYS_MS = 7 * 24 * 60 * 60 * 1000
OWNER_NICK = "владислав"


def load_db():
    if not os.path.exists(DB_FILE):
        return {"users": {}, "chats": {}}
    try:
        with open(DB_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"users": {}, "chats": {}}


def save_db(db):
    tmp_file = DB_FILE + ".tmp"
    with open(tmp_file, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)
    os.replace(tmp_file, DB_FILE)


def chat_key(a, b):
    return "::".join(sorted([a, b]))


def is_owner_nick(username):
    return (username or "").strip().lower() == OWNER_NICK


def safe_user(u):
    data = {k: v for k, v in u.items() if k != "password"}
    data["isOwner"] = is_owner_nick(data.get("username"))
    return data


HTML_PAGE = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Груша</title>
<style>
  * { box-sizing: border-box; }
  body { margin: 0; font-family: system-ui, -apple-system, sans-serif; background: #101511; color: #f3f7f3; }
  .wrap { max-width: 480px; margin: 0 auto; min-height: 100vh; background: #171e18; display: flex; flex-direction: column; }
  .top { display: flex; align-items: center; justify-content: space-between; padding: 12px 14px; background: #1f2920; border-bottom: 1px solid #2b382d; gap: 8px; }
  .brand { font-size: 20px; font-weight: 800; }
  .pad { padding: 14px; flex: 1; display: flex; flex-direction: column; }
  input { width: 100%; padding: 11px 12px; border-radius: 10px; border: 1px solid #2f3d31; background: #101511; color: #fff; font-size: 15px; outline: none; }
  input:focus { border-color: #9be15d; }
  button { padding: 10px 14px; border-radius: 10px; border: none; background: #9be15d; color: #101511; font-weight: 700; font-size: 14px; cursor: pointer; }
  button.sec { background: #263228; color: #eef5ee; }
  button.del { background: #b93838; color: #fff; }
  .mt-8 { margin-top: 8px; }
  .mt-12 { margin-top: 12px; }
  .err { color: #ff7b7b; font-size: 13px; margin-top: 8px; min-height: 18px; }
  .ok { color: #9be15d; font-size: 13px; margin-top: 8px; }
  .muted { color: #96a598; font-size: 13px; }
  .chat-card { display: flex; align-items: center; gap: 12px; padding: 11px; border-radius: 12px; background: #202a21; margin-top: 8px; cursor: pointer; }
  .ava { width: 44px; height: 44px; border-radius: 50%; background: #141a15; display: grid; place-items: center; font-size: 22px; flex-shrink: 0; }
  .chat-info { min-width: 0; flex: 1; }
  .chat-name { font-weight: 700; font-size: 15px; display: inline-flex; align-items: center; gap: 5px; }
  .chat-last { color: #9aa89c; font-size: 13px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; margin-top: 2px; }
  .verified-badge { display: inline-flex; align-items: center; justify-content: center; width: 17px; height: 17px; border-radius: 50%; background: #2da9ff; color: #ffffff; font-size: 11px; font-weight: 900; line-height: 1; flex-shrink: 0; }
  .msgs { flex: 1; overflow-y: auto; display: flex; flex-direction: column; gap: 8px; padding: 12px 14px; max-height: 62vh; }
  .m { padding: 9px 12px; border-radius: 14px; max-width: 80%; word-break: break-word; font-size: 15px; line-height: 1.35; }
  .me { align-self: flex-end; background: #9be15d; color: #101511; font-weight: 500; }
  .them { align-self: flex-start; background: #243026; color: #f3f7f3; }
  .em-row { display: flex; gap: 6px; padding: 6px 14px; background: #1b231c; border-top: 1px solid #28342a; overflow-x: auto; }
  .em-btn { background: #263228; color: #fff; padding: 6px 9px; font-size: 18px; border-radius: 8px; flex-shrink: 0; }
  .composer { display: flex; gap: 8px; padding: 10px 14px 14px; background: #1b231c; }
  .composer input { flex: 1; }
  .hidden { display: none !important; }
</style>
</head>
<body>
<div class="wrap">
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

  <div id="chatsScreen" class="hidden" style="flex:1; display:flex; flex-direction:column;">
    <div class="top">
      <div style="display:flex; align-items:center; gap:10px; min-width:0;">
        <div class="ava" id="myAva">🍐</div>
        <div class="chat-name" id="myNick">@user</div>
      </div>
      <button class="sec" onclick="openSettings()">⚙️</button>
    </div>
    <div class="pad">
      <input id="searchInput" placeholder="Найти человека по никнейму..." autocomplete="off" autocorrect="off" autocapitalize="none" spellcheck="false" oninput="onSearchInput()">
      <div id="searchResults"></div>
      <div class="muted mt-12" style="font-weight:700;">Чаты</div>
      <div id="chatList"></div>
    </div>
  </div>

  <div id="roomScreen" class="hidden" style="flex:1; display:flex; flex-direction:column;">
    <div class="top">
      <div style="display:flex; align-items:center; gap:10px; min-width:0;">
        <button class="sec" onclick="backToChats()">← Чаты</button>
        <div class="ava" id="peerAva">🍐</div>
        <div class="chat-name" id="peerNick">@peer</div>
      </div>
    </div>
    <div id="msgsBox" class="msgs"></div>
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
    <div class="composer">
      <input id="msgInput" placeholder="Сообщение..." autocomplete="off" spellcheck="false" onkeydown="if(event.key==='Enter'){event.preventDefault();sendMsg();}">
      <button type="button" onclick="sendMsg()">➤</button>
    </div>
  </div>

  <div id="settingsScreen" class="hidden" style="flex:1; display:flex; flex-direction:column;">
    <div class="top">
      <button class="sec" onclick="backToChats()">← Назад</button>
      <strong>Настройки</strong>
      <span style="width:52px;"></span>
    </div>
    <div class="pad">
      <div style="font-weight:700;">Аватарка (только эмодзи)</div>
      <div style="display:flex; flex-wrap:wrap; gap:8px; margin-top:8px;">
        <button type="button" class="em-btn" onclick="setEmoji('🍐')">🍐</button>
        <button type="button" class="em-btn" onclick="setEmoji('😎')">😎</button>
        <button type="button" class="em-btn" onclick="setEmoji('🐱')">🐱</button>
        <button type="button" class="em-btn" onclick="setEmoji('🐸')">🐸</button>
        <button type="button" class="em-btn" onclick="setEmoji('🔥')">🔥</button>
        <button type="button" class="em-btn" onclick="setEmoji('👾')">👾</button>
        <button type="button" class="em-btn" onclick="setEmoji('⚡')">⚡</button>
        <button type="button" class="em-btn" onclick="setEmoji('🍕')">🍕</button>
      </div>

      <div style="font-weight:700; margin-top:18px;">Изменить никнейм</div>
      <div id="nickHint" class="muted mt-8"></div>
      <input id="newNickInput" class="mt-8" autocomplete="off" autocorrect="off" autocapitalize="none" spellcheck="false">
      <button class="mt-8" style="width:100%;" id="btnSaveNick" onclick="changeNick()">Сохранить никнейм</button>

      <div id="setErr" class="err"></div>
      <div id="setOk" class="ok"></div>

      <button class="sec mt-12" style="width:100%;" onclick="logout()">Выйти из аккаунта</button>
      <button class="del mt-8" style="width:100%;" onclick="deleteAccount()">Удалить аккаунт</button>
    </div>
  </div>
</div>

<script>
let me = null;
let authTab = "login";
let activePeer = null;
let lastRenderedMsgsJson = "";
let pollTimer = null;

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
  ["loadingScreen", "authScreen", "chatsScreen", "roomScreen", "settingsScreen"].forEach(s => {
    document.getElementById(s).classList.toggle("hidden", s !== id);
  });
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
  activePeer = null;
  lastRenderedMsgsJson = "";
  document.getElementById("myAva").textContent = me.emoji || "🍐";
  document.getElementById("myNick").innerHTML = nickLabelHtml(me.username, me.isOwner);
  document.getElementById("searchInput").value = "";
  document.getElementById("searchResults").innerHTML = "";
  showScreen("chatsScreen");
  refreshChats();
  startPolling();
}

function backToChats() {
  activePeer = null;
  lastRenderedMsgsJson = "";
  showScreen("chatsScreen");
  refreshChats();
}

function openSettings() {
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

async function onSearchInput() {
  const q = document.getElementById("searchInput").value.trim();
  const box = document.getElementById("searchResults");
  if (!q) {
    box.innerHTML = "";
    return;
  }
  const res = await api(`/api/search?q=${encodeURIComponent(q)}&exclude=${encodeURIComponent(me.username)}`);
  if (!res.users || !res.users.length) {
    box.innerHTML = `<div class="muted mt-8">Пользователь не найден</div>`;
    return;
  }
  box.innerHTML = res.users.map(u => `
    <div class="chat-card" onclick="openChat('${esc(u.username)}', '${esc(u.emoji || '🍐')}', ${u.isOwner ? 'true' : 'false'})">
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
  if (!res.chats.length) {
    list.innerHTML = `<div class="muted mt-12">Чатов пока нет. Введи никнейм в поиске выше и открой диалог.</div>`;
    return;
  }
  list.innerHTML = res.chats.map(c => `
    <div class="chat-card" onclick="openChat('${esc(c.username)}', '${esc(c.emoji || '🍐')}', ${c.isOwner ? 'true' : 'false'})">
      <div class="ava">${c.emoji || '🍐'}</div>
      <div class="chat-info">
        <div class="chat-name">${nickLabelHtml(c.username, c.isOwner)}</div>
        <div class="chat-last">${esc(c.last || 'Сообщений пока нет')}</div>
      </div>
    </div>
  `).join("");
}

async function openChat(username, emoji, isOwner = false) {
  activePeer = { username, emoji, isOwner };
  lastRenderedMsgsJson = "";
  document.getElementById("peerAva").textContent = emoji || "🍐";
  document.getElementById("peerNick").innerHTML = nickLabelHtml(username, isOwner);
  document.getElementById("msgInput").value = "";
  showScreen("roomScreen");
  await refreshMessages(true);
}

async function refreshMessages(forceScroll = false) {
  if (!me || !activePeer) return;
  const res = await api(`/api/msgs?a=${encodeURIComponent(me.username)}&b=${encodeURIComponent(activePeer.username)}`);
  if (!res.ok) return;
  const json = JSON.stringify(res.msgs);
  if (json === lastRenderedMsgsJson && !forceScroll) return;
  lastRenderedMsgsJson = json;

  const box = document.getElementById("msgsBox");
  if (!box) return;
  if (!res.msgs.length) {
    box.innerHTML = `<div class="muted" style="margin:auto;">Здесь пока пусто. Напишите первое сообщение!</div>`;
    return;
  }
  box.innerHTML = res.msgs.map(m => `
    <div class="m ${m.from === me.username ? 'me' : 'them'}">${esc(m.text)}</div>
  `).join("");
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
}

async function sendMsg() {
  if (!me || !activePeer) return;
  const inp = document.getElementById("msgInput");
  const text = inp.value.trim();
  if (!text) return;
  inp.value = "";
  inp.focus();
  await api("/api/send", { from: me.username, to: activePeer.username, text });
  await refreshMessages(true);
  await refreshChats();
}

async function setEmoji(em) {
  const res = await api("/api/emoji", { username: me.username, emoji: em });
  if (res.ok) {
    me.emoji = em;
    document.getElementById("myAva").textContent = em;
    document.getElementById("setOk").textContent = "Аватарка обновлена";
    document.getElementById("setErr").textContent = "";
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
  activePeer = null;
  showScreen("authScreen");
}

async function deleteAccount() {
  if (!me) return;
  await api("/api/delete", { username: me.username });
  saveLocalUser(null);
  activePeer = null;
  showScreen("authScreen");
}

function startPolling() {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(() => {
    if (!me) return;
    refreshChats();
    if (activePeer) refreshMessages(false);
  }, 2500);
}

function esc(s) {
  return String(s || "").replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  }[c]));
}

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
    resp = make_response(jsonify({"ok": True, "user": safe_user(user_dict)}))
    resp.set_cookie("grusha_user", user_dict["username"], max_age=30 * 24 * 3600, samesite="Lax")
    return resp


@app.get("/")
def home():
    return render_template_string(HTML_PAGE)


@app.get("/api/me")
def get_me():
    nick = request.cookies.get("grusha_user") or (request.args.get("nick") or "").strip()
    if not nick:
        return jsonify({"ok": False})
    db = load_db()
    user = db["users"].get(nick)
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
    db = load_db()
    if u in db["users"]:
        return jsonify({"ok": False, "error": "Ник уже занят!"})
    now = int(time.time() * 1000)
    db["users"][u] = {"username": u, "password": p, "emoji": "🍐", "createdAt": now, "lastNickChangeAt": now}
    save_db(db)
    return auth_response(db["users"][u])


@app.post("/api/login")
def login():
    d = request.get_json(force=True) or {}
    u = (d.get("username") or "").strip()
    p = d.get("password") or ""
    db = load_db()
    user = db["users"].get(u)
    if not user or user["password"] != p:
        return jsonify({"ok": False, "error": "Неверный ник или пароль."})
    return auth_response(user)


@app.post("/api/logout")
def logout_api():
    resp = make_response(jsonify({"ok": True}))
    resp.delete_cookie("grusha_user")
    return resp


@app.get("/api/search")
def search():
    q = (request.args.get("q") or "").strip().lower()
    ex = request.args.get("exclude") or ""
    db = load_db()
    res = [
        {
            "username": k,
            "emoji": v.get("emoji", "🍐"),
            "isOwner": is_owner_nick(k),
        }
        for k, v in db["users"].items()
        if k != ex and q in k.lower()
    ]
    return jsonify({"ok": True, "users": res})


@app.get("/api/chats")
def chats():
    u = request.args.get("user") or ""
    db = load_db()
    out = []
    for k, m in db["chats"].items():
        if not m:
            continue
        a, b = k.split("::")
        peer = b if a == u else (a if b == u else None)
        if peer and peer in db["users"]:
            out.append({
                "username": peer,
                "emoji": db["users"][peer].get("emoji", "🍐"),
                "isOwner": is_owner_nick(peer),
                "last": m[-1]["text"],
                "ts": m[-1].get("ts", 0),
            })
    out.sort(key=lambda x: x["ts"], reverse=True)
    return jsonify({"ok": True, "chats": out})


@app.get("/api/msgs")
def msgs():
    a = request.args.get("a") or ""
    b = request.args.get("b") or ""
    db = load_db()
    return jsonify({"ok": True, "msgs": db["chats"].get(chat_key(a, b), [])})


@app.post("/api/send")
def send():
    d = request.get_json(force=True) or {}
    f, t, txt = (d.get("from") or "").strip(), (d.get("to") or "").strip(), (d.get("text") or "").strip()
    if not txt:
        return jsonify({"ok": False})
    db = load_db()
    db["chats"].setdefault(chat_key(f, t), []).append({"from": f, "text": txt, "ts": int(time.time() * 1000)})
    save_db(db)
    return jsonify({"ok": True})


@app.post("/api/emoji")
def emoji():
    d = request.get_json(force=True) or {}
    u, em = d.get("username") or "", d.get("emoji") or "🍐"
    db = load_db()
    if u in db["users"]:
        db["users"][u]["emoji"] = em
        save_db(db)
    return jsonify({"ok": True})


@app.post("/api/nick")
def nick():
    d = request.get_json(force=True) or {}
    old_n, new_n = (d.get("oldNick") or "").strip(), (d.get("newNick") or "").strip()
    db = load_db()
    user = db["users"].get(old_n)
    if not user:
        return jsonify({"ok": False, "error": "Аккаунт не найден."})
    now = int(time.time() * 1000)
    if now - (user.get("lastNickChangeAt") or user.get("createdAt") or now) > SEVEN_DAYS_MS:
        return jsonify({"ok": False, "error": "7 дней прошло, сменить ник нельзя."})
    if len(new_n) < 2:
        return jsonify({"ok": False, "error": "Ник слишком короткий."})
    if new_n in db["users"]:
        return jsonify({"ok": False, "error": "Ник уже занят!"})
    db["users"][new_n] = {**user, "username": new_n, "lastNickChangeAt": now}
    del db["users"][old_n]
    new_chats = {}
    for k, m in db["chats"].items():
        parts = [new_n if x == old_n else x for x in k.split("::")]
        new_chats[chat_key(parts[0], parts[1])] = [{**x, "from": new_n if x["from"] == old_n else x["from"]} for x in m]
    db["chats"] = new_chats
    save_db(db)
    return auth_response(db["users"][new_n])


@app.post("/api/delete")
def delete():
    d = request.get_json(force=True) or {}
    u = d.get("username") or ""
    db = load_db()
    db["users"].pop(u, None)
    db["chats"] = {k: v for k, v in db["chats"].items() if u not in k.split("::")}
    save_db(db)
    resp = make_response(jsonify({"ok": True}))
    resp.delete_cookie("grusha_user")
    return resp


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
