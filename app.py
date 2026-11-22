import json
import os
import time
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
MAX_IMAGE_LEN = 1_500_000

# In-memory карта статусов набора: {(sender, recipient): timestamp_ms}
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
                        ts BIGINT NOT NULL
                    )
                """)
                cur.execute("CREATE INDEX IF NOT EXISTS idx_messages_chat_key ON messages(chat_key)")
    else:
        if not os.path.exists(DB_FILE):
            with open(DB_FILE, "w", encoding="utf-8") as f:
                json.dump({"users": {}, "chats": {}}, f, ensure_ascii=False, indent=2)


def load_json_db():
    try:
        with open(DB_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"users": {}, "chats": {}}


def save_json_db(db):
    tmp_file = DB_FILE + ".tmp"
    with open(tmp_file, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)
    os.replace(tmp_file, DB_FILE)


def chat_key(a, b):
    return "::".join(sorted([a, b]))


def is_owner_nick(username):
    return (username or "").strip().lower() == OWNER_NICK


def is_peer_typing(peer, me_nick):
    ts = TYPING_STATUS.get((peer, me_nick), 0)
    return (int(time.time() * 1000) - ts) <= TYPING_TTL_MS


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
    --app-height: 100dvh;
  }
  * { box-sizing: border-box; }
  html, body {
    margin: 0;
    padding: 0;
    background: #101511;
    color: #f3f7f3;
    font-family: system-ui, -apple-system, sans-serif;
    height: var(--app-height);
    overflow: hidden;
  }
  .wrap {
    max-width: 480px;
    margin: 0 auto;
    height: var(--app-height);
    background: #171e18;
    display: flex;
    flex-direction: column;
    overflow: hidden;
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
  .brand { font-size: 20px; font-weight: 800; }
  .pad {
    padding: 14px;
    flex: 1;
    display: flex;
    flex-direction: column;
    overflow-y: auto;
    min-height: 0;
  }
  input[type="text"], input[type="password"], input:not([type="range"]):not([type="file"]), textarea {
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

  #roomScreen {
    flex: 1;
    display: flex;
    flex-direction: column;
    min-height: 0;
    overflow: hidden;
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
  .m {
    padding: 9px 12px;
    border-radius: 14px;
    max-width: 82%;
    word-break: break-word;
    white-space: pre-wrap;
    font-size: var(--msg-font-size);
    line-height: 1.35;
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

  .bottom-dock {
    flex-shrink: 0;
    background: #1b231c;
    border-top: 1px solid #28342a;
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
      <button class="sec" onclick="openSettings()">⚙️</button>
    </div>
    <div class="pad">
      <input id="searchInput" placeholder="Найти человека по никнейму..." autocomplete="off" autocorrect="off" autocapitalize="none" spellcheck="false" oninput="onSearchInput()">
      <div id="searchResults"></div>
      <div class="muted mt-12" style="font-weight:700;">Чаты</div>
      <div id="chatList"></div>
    </div>
  </div>

  <div id="roomScreen" class="hidden">
    <div class="top">
      <div style="display:flex; align-items:center; gap:10px; min-width:0;">
        <button class="sec" onclick="backToChats()">← Чаты</button>
        <div class="ava" id="peerAva">🍐</div>
        <div style="min-width:0;">
          <div class="chat-name" id="peerNick">@peer</div>
          <div class="typing-line" id="peerStatus"></div>
        </div>
      </div>
    </div>
    <div id="msgsBox" class="msgs"></div>
    <div class="bottom-dock">
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
      <div id="attachPreview" class="attach-preview hidden">
        <img id="attachThumb" alt="Превью">
        <div class="muted" id="attachName">Картинка прикреплена</div>
        <button type="button" class="attach-remove" onclick="clearAttachment()">Убрать</button>
      </div>
      <div class="composer">
        <input id="fileInput" type="file" accept="image/*" class="hidden" onchange="onFileSelected(event)">
        <button type="button" class="clip-btn" title="Прикрепить картинку" onclick="document.getElementById('fileInput').click()">📎</button>
        <textarea id="msgInput" rows="1" placeholder="Сообщение..." autocomplete="off" spellcheck="false" oninput="onComposerInput()" onkeydown="onComposerKeydown(event)"></textarea>
        <button type="button" onclick="sendMsg()">➤</button>
      </div>
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
  "🎧", "🚀", "🌵", "🍀", "🍓", "👑"
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
let activePeer = null;
let pendingImageData = "";
let lastRenderedMsgsJson = "";
let lastRenderedCustomJson = "";
let lastTypingPingTs = 0;
let pollTimer = null;

/* Подгоняем высоту под видимую область экрана при открытии экранной клавиатуры на телефоне */
function syncViewportHeight() {
  const vv = window.visualViewport;
  const h = vv ? vv.height : window.innerHeight;
  document.documentElement.style.setProperty("--app-height", `${Math.round(h)}px`);
  if (vv && vv.offsetTop > 0) {
    window.scrollTo(0, 0);
  }
  const box = document.getElementById("msgsBox");
  if (box && activePeer) {
    box.scrollTop = box.scrollHeight;
  }
}

if (window.visualViewport) {
  window.visualViewport.addEventListener("resize", syncViewportHeight);
  window.visualViewport.addEventListener("scroll", syncViewportHeight);
}
window.addEventListener("resize", syncViewportHeight);
syncViewportHeight();

function autoResizeComposer() {
  const inp = document.getElementById("msgInput");
  if (!inp) return;
  inp.style.height = "auto";
  const next = Math.min(inp.scrollHeight, 120);
  inp.style.height = `${Math.max(42, next)}px`;
}

function sendTypingSignal(force = false) {
  if (!me || !activePeer) return;
  const now = Date.now();
  if (!force && now - lastTypingPingTs < 1500) return;
  lastTypingPingTs = now;
  api("/api/typing", { from: me.username, to: activePeer.username }).catch(() => {});
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
  if (activePeer) {
    lastRenderedMsgsJson = "";
    refreshMessages(false);
  }
}

function resetCustomization() {
  try { localStorage.setItem("grusha_custom", JSON.stringify(DEFAULT_CUSTOM)); } catch (e) {}
  applyCustom(DEFAULT_CUSTOM);
  if (activePeer) {
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
    "button, input, textarea, select, a, .chat-card, .m, .msg-img, .em-btn, .clip-btn, .attach-preview, .top, .bottom-dock, .custom-box"
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
  ["loadingScreen", "authScreen", "chatsScreen", "roomScreen", "settingsScreen"].forEach(s => {
    document.getElementById(s).classList.toggle("hidden", s !== id);
  });
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
  activePeer = null;
  clearAttachment();
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
  clearAttachment();
  lastRenderedMsgsJson = "";
  showScreen("chatsScreen");
  refreshChats();
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

  if (activePeer) {
    const matched = res.chats.find(c => c.username === activePeer.username);
    if (matched && matched.emoji && matched.emoji !== activePeer.emoji) {
      activePeer.emoji = matched.emoji;
      const peerAvaEl = document.getElementById("peerAva");
      if (peerAvaEl) peerAvaEl.textContent = matched.emoji;
    }
  }

  if (!res.chats.length) {
    list.innerHTML = `<div class="muted mt-12">Чатов пока нет. Введи никнейм в поиске выше и открой диалог.</div>`;
    return;
  }
  list.innerHTML = res.chats.map(c => `
    <div class="chat-card" onclick="openChat('${esc(c.username)}', '${esc(c.emoji || '🍐')}', ${c.isOwner ? 'true' : 'false'})">
      <div class="ava">${c.emoji || '🍐'}</div>
      <div class="chat-info">
        <div class="chat-name">${nickLabelHtml(c.username, c.isOwner)}</div>
        <div class="chat-last ${c.typing ? 'typing-active' : ''}">${c.typing ? 'печатает...' : esc(c.last || 'Сообщений пока нет')}</div>
      </div>
    </div>
  `).join("");
}

async function openChat(username, emoji, isOwner = false) {
  activePeer = { username, emoji, isOwner };
  clearAttachment();
  lastRenderedMsgsJson = "";
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

function formatTextWithEmojis(rawText) {
  const safe = esc(rawText);
  return safe.replace(SINGLE_EMOJI_RE, match => `<span class="inline-emoji">${match}</span>`);
}

function renderMsgItem(m) {
  const cls = m.from === me.username ? "me" : "them";
  const text = (m.text || "").trim();
  const isEmojiOnly = !m.image && text.length > 0 && EMOJI_ONLY_RE.test(text);
  const bubbleExtra = isEmojiOnly ? " emoji-only-bubble" : "";

  const imgHtml = m.image
    ? `<img class="msg-img" src="${m.image}" alt="Картинка" onclick="window.open('${m.image}', '_blank')">`
    : "";

  const txtHtml = text
    ? `<div class="msg-txt">${formatTextWithEmojis(text)}</div>`
    : "";

  return `<div class="m ${cls}${bubbleExtra}">${imgHtml}${txtHtml}</div>`;
}

async function refreshMessages(forceScroll = false) {
  if (!me || !activePeer) return;
  const res = await api(`/api/msgs?a=${encodeURIComponent(me.username)}&b=${encodeURIComponent(activePeer.username)}`);
  if (!res.ok) return;

  if (res.peerEmoji && res.peerEmoji !== activePeer.emoji) {
    activePeer.emoji = res.peerEmoji;
    const peerAvaEl = document.getElementById("peerAva");
    if (peerAvaEl) peerAvaEl.textContent = res.peerEmoji;
  }

  const statusEl = document.getElementById("peerStatus");
  if (statusEl) {
    statusEl.textContent = res.peerTyping ? "печатает..." : "";
  }

  const customStamp = JSON.stringify(loadCustom());
  const json = JSON.stringify(res.msgs);
  if (json === lastRenderedMsgsJson && customStamp === lastRenderedCustomJson && !forceScroll) return;
  lastRenderedMsgsJson = json;
  lastRenderedCustomJson = customStamp;

  const box = document.getElementById("msgsBox");
  if (!box) return;
  if (!res.msgs.length) {
    box.innerHTML = `<div class="muted" style="margin:auto;">Здесь пока пусто. Напишите первое сообщение!</div>`;
    return;
  }
  box.innerHTML = res.msgs.map(renderMsgItem).join("");
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
  if (!me || !activePeer) return;
  const inp = document.getElementById("msgInput");
  const text = inp.value.trim();
  const image = pendingImageData;
  if (!text && !image) return;

  inp.value = "";
  autoResizeComposer();
  clearAttachment();
  inp.focus();

  await api("/api/send", { from: me.username, to: activePeer.username, text, image });
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
  activePeer = null;
  clearAttachment();
  showScreen("authScreen");
}

async function deleteAccount() {
  if (!me) return;
  await api("/api/delete", { username: me.username });
  saveLocalUser(null);
  activePeer = null;
  clearAttachment();
  showScreen("authScreen");
}

function startPolling() {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(() => {
    if (!me) return;
    refreshChats();
    if (activePeer) refreshMessages(false);
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
    if f and t:
        TYPING_STATUS[(f, t)] = int(time.time() * 1000)
    return jsonify({"ok": True})


@app.get("/api/search")
def search():
    q = (request.args.get("q") or "").strip().lower()
    ex = request.args.get("exclude") or ""
    if USE_PG:
        with get_pg_conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    "SELECT username, emoji FROM users WHERE username <> %s AND LOWER(username) LIKE %s",
                    (ex, f"%{q}%"),
                )
                rows = cur.fetchall()
        res = [{"username": r["username"], "emoji": r.get("emoji") or "🍐", "isOwner": is_owner_nick(r["username"])} for r in rows]
        return jsonify({"ok": True, "users": res})

    db = load_json_db()
    res = [
        {"username": k, "emoji": v.get("emoji", "🍐"), "isOwner": is_owner_nick(k)}
        for k, v in db["users"].items()
        if k != ex and q in k.lower()
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

        out = []
        for r in last_rows:
            a, b = r["chat_key"].split("::")
            peer = b if a == u else (a if b == u else None)
            if peer and peer in users_map:
                preview_text = r.get("text") or ("📷 Картинка" if r.get("image") else "Сообщений пока нет")
                out.append({
                    "username": peer,
                    "emoji": users_map[peer],
                    "isOwner": is_owner_nick(peer),
                    "typing": is_peer_typing(peer, u),
                    "last": preview_text,
                    "ts": int(r.get("ts") or 0),
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
                "username": peer,
                "emoji": db["users"][peer].get("emoji", "🍐"),
                "isOwner": is_owner_nick(peer),
                "typing": is_peer_typing(peer, u),
                "last": preview_text,
                "ts": last_msg.get("ts", 0),
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
                    "SELECT sender AS \"from\", text, image, ts FROM messages WHERE chat_key = %s ORDER BY id ASC",
                    (ck,),
                )
                rows = cur.fetchall()
                cur.execute("SELECT emoji FROM users WHERE username = %s", (b,))
                peer_row = cur.fetchone()
        peer_emoji = (peer_row.get("emoji") if peer_row else None) or "🍐"
        formatted = []
        for r in rows:
            item = {"from": r["from"], "text": r.get("text") or "", "ts": int(r.get("ts") or 0)}
            if r.get("image"):
                item["image"] = r["image"]
            formatted.append(item)
        return jsonify({"ok": True, "msgs": formatted, "peerEmoji": peer_emoji, "peerTyping": peer_typing})

    db = load_json_db()
    peer_user = db["users"].get(b) or {}
    return jsonify({
        "ok": True,
        "msgs": db["chats"].get(ck, []),
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
                    "INSERT INTO messages (chat_key, sender, text, image, ts) VALUES (%s, %s, %s, %s, %s)",
                    (chat_key(f, t), f, txt, img, ts),
                )
        return jsonify({"ok": True})

    msg_item = {"from": f, "text": txt, "ts": ts}
    if img:
        msg_item["image"] = img
    db = load_json_db()
    db["chats"].setdefault(chat_key(f, t), []).append(msg_item)
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
