# main.py — Bot de inactividad (Flask + pyTelegramBotAPI)
# Webhook directo + persistencia JSON + /backup + /ping (con aviso educado)
# TODOS los comandos solo para administradores.

import os
import time
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Tuple, Any

from flask import Flask, request
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

# ====== LOGGING ======
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
telebot.logger.setLevel(logging.WARNING)

# ====== CONFIG ======
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
WEBHOOK_BASE = os.getenv("WEBHOOK_URL", "").rstrip("/")
INACTIVITY_DAYS = int(os.getenv("INACTIVITY_DAYS", "14"))     # días para inactivo
SAFE_MODE = os.getenv("SAFE_MODE", "1") == "1"                # 1 = solo avisar, 0 = expulsar
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}
DATA_PATH = os.getenv("DATA_PATH", "data/activity.json").strip()

if not BOT_TOKEN:
    raise RuntimeError("Falta BOT_TOKEN")
if not WEBHOOK_BASE:
    raise RuntimeError("Falta WEBHOOK_URL")

# ====== APP/BOT ======
app = Flask(__name__)
bot = telebot.TeleBot(BOT_TOKEN)

# ====== PERSISTENCIA ACTIVIDAD ======
# {(chat_id, user_id): {"last_seen": datetime, "username": str}}
activity: Dict[Tuple[int, int], Dict[str, Any]] = {}

def _ensure_data_dir(path_str: str) -> Path:
    p = Path(path_str)
    if p.suffix:
        p.parent.mkdir(parents=True, exist_ok=True)
    else:
        p.mkdir(parents=True, exist_ok=True)
        p = p / "activity.json"
    return p

DATA_FILE = _ensure_data_dir(DATA_PATH)

def _dt_to_iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat() + "Z"

def _iso_to_dt(s: str) -> datetime:
    try:
        if s.endswith("Z"):
            s = s[:-1]
        return datetime.fromisoformat(s)
    except Exception:
        return datetime.utcnow()

def load_activity() -> None:
    global activity
    if not DATA_FILE.exists():
        logging.info("[DATA] No hay archivo de actividad, se creará en: %s", DATA_FILE)
        activity = {}
        return
    try:
        with DATA_FILE.open("r", encoding="utf-8") as f:
            raw = json.load(f)
        loaded: Dict[Tuple[int, int], Dict[str, Any]] = {}
        for key, val in raw.items():
            try:
                chat_s, user_s = key.split("|", 1)
                chat_id = int(chat_s)
                user_id = int(user_s)
                username = val.get("username", "") or ""
                last_seen_iso = val.get("last_seen")
                dt = _iso_to_dt(last_seen_iso) if last_seen_iso else datetime.utcnow()
                loaded[(chat_id, user_id)] = {"last_seen": dt, "username": username}
            except Exception:
                continue
        activity = loaded
        logging.info("[DATA] Actividad cargada: %s registros", len(activity))
    except Exception as e:
        logging.exception("[DATA] Error cargando actividad: %s", e)
        activity = {}

def save_activity() -> None:
    try:
        serializable: Dict[str, Dict[str, str]] = {}
        for (chat_id, user_id), data in activity.items():
            dt = data.get("last_seen")
            username = data.get("username", "") or ""
            iso = _dt_to_iso(dt) if isinstance(dt, datetime) else _dt_to_iso(datetime.utcnow())
            serializable[f"{chat_id}|{user_id}"] = {"last_seen": iso, "username": username}
        tmp = DATA_FILE.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(serializable, f, ensure_ascii=False, indent=2)
        tmp.replace(DATA_FILE)
        logging.info("[DATA] Actividad guardada (%s registros) en %s", len(serializable), DATA_FILE)
    except Exception as e:
        logging.exception("[DATA] Error guardando actividad: %s", e)

# ====== UTIL ======
def fmt_user(user_id, username):
    return "@{}".format(username) if username else "ID:{}".format(user_id)

def actualizar_actividad(chat_id, user_id, username):
    activity[(chat_id, user_id)] = {"last_seen": datetime.utcnow(), "username": username or ""}
    logging.info("[ACT] chat:%s user:%s @%s", chat_id, user_id, username or "")
    save_activity()

def es_grupo(chat_type: str) -> bool:
    return chat_type in ("group", "supergroup")

def es_mensaje_de_actividad(msg: Dict[str, Any]) -> bool:
    if "text" in msg and isinstance(msg["text"], str) and msg["text"].startswith("/"):
        return False
    for k in ("text", "photo", "video", "audio", "document", "sticker", "voice", "animation", "video_note"):
        if k in msg:
            return True
    return False

# ---- PERMISOS (UNIFICADO) ----
def es_admin_usuario(user_id: int) -> bool:
    """Admin declarado por variable de entorno ADMIN_IDS."""
    return user_id in ADMIN_IDS

def es_admin_en_este_chat(chat_id: int, chat_type: str, user_id: int) -> bool:
    """True si es admin/creator del grupo, o está en ADMIN_IDS (sirve también para privado)."""
    if es_admin_usuario(user_id):
        return True
    if chat_type in ("group", "supergroup"):
        try:
            m = bot.get_chat_member(chat_id, user_id)
            return getattr(m, "status", "") in ("administrator", "creator")
        except Exception:
            return False
    return False

def puede_expulsar(chat_id: int) -> bool:
    try:
        me = bot.get_me()
        member = bot.get_chat_member(chat_id, me.id)
        status = getattr(member, "status", "")
        can_restrict = getattr(member, "can_restrict_members", False)
        ok = (status in ("administrator", "creator")) and (can_restrict or status == "creator")
        logging.info("Permisos del bot en chat %s -> admin:%s restrict:%s", chat_id, status, can_restrict)
        return ok
    except Exception as e:
        logging.warning("puede_expulsar error: %s", e)
        return False

def expulsar_usuario(chat_id: int, user_id: int):
    try:
        bot.ban_chat_member(chat_id, user_id)
        bot.unban_chat_member(chat_id, user_id, only_if_banned=True)
        return True, None
    except Exception as e:
        return False, str(e)

# ====== HTTP ======
@app.route("/", methods=["GET"])
def health():
    return "Bot activo ✅", 200

@app.route("/webhook", methods=["POST"])
def webhook():
    if not request.is_json:
        logging.warning("Webhook sin JSON válido. Headers: %s", dict(request.headers))
        return "", 403

    data = request.get_json(silent=True) or {}
    logging.info("Webhook JSON recibido: %s", (str(data)[:700]))

    try:
        if "message" in data:
            handle_message(data["message"])
        elif "edited_message" in data:
            # handle_message(data["edited_message"], edited=True)  # si quieres contar editados
            pass
        elif "callback_query" in data:
            handle_callback(data["callback_query"])
        elif "chat_member" in data:
            handle_chat_member_update(data["chat_member"])
    except Exception as e:
        logging.exception("Error manejando update: %s", e)

    return "", 200

# ====== LÓGICA PRINCIPAL ======
def handle_message(msg: Dict[str, Any], edited: bool = False):
    chat = msg.get("chat", {}) or {}
    chat_id = chat.get("id")
    chat_type = chat.get("type", "private")

    from_user = msg.get("from", {}) or {}
    user_id = from_user.get("id")
    username = from_user.get("username") or ""

    text = msg.get("text") or ""

    # 1) Registrar actividad en grupos
    if es_grupo(chat_type) and es_mensaje_de_actividad(msg):
        actualizar_actividad(chat_id, user_id, username)

    # 2) Comandos (TODOS requieren ser admin)
    if isinstance(text, str) and text.startswith("/"):
        cmd = text.split()[0].split("@")[0].lower()  # soporta /cmd@TuBot

        # CORTAFUEGOS GLOBAL DE PERMISOS
        if not es_admin_en_este_chat(chat_id, chat_type, user_id):
            bot.send_message(chat_id, "⛔ Este comando es solo para administradores.")
            return

        if cmd in ("/start", "/help"):
            responder_start(chat_id, chat_type)
            return

        if cmd == "/config":
            responder_config(chat_id)
            return

        if cmd == "/scan":
            if not es_grupo(chat_type):
                bot.send_message(chat_id, "ℹ️ /scan se usa en grupos.")
                return
            if not puede_expulsar(chat_id):
                bot.send_message(chat_id, "⚠️ No tengo permisos de administrador para expulsar aquí.")
                return
            ejecutar_scan(chat_id)
            return

        if cmd == "/backup":
            try:
                if chat_type != "private":
                    bot.send_message(chat_id, "📦 Te envío el archivo por privado.")
                if DATA_FILE.exists():
                    with open(DATA_FILE, "rb") as f:
                        bot.send_document(user_id, f, caption=f"Backup de actividad ({DATA_FILE})")
                else:
                    bot.send_message(user_id, f"⚠️ No existe el archivo {DATA_FILE}. Escribe algo y vuelve a probar.")
            except Exception as e:
                bot.send_message(chat_id, "⚠️ No pude enviarte el backup por privado. Abre chat conmigo (/start) y repite. Error: {}".format(e))
            return

        if cmd == "/ping":
            if not es_grupo(chat_type):
                bot.send_message(chat_id, "ℹ️ /ping se usa en grupos.")
                return
            enviar_ping(chat_id)
            return

def responder_start(chat_id: int, chat_type: str):
    if chat_type == "private":
        bot.send_message(
            chat_id,
            "👋 ¡Hola! Funciono por webhook en Render.\n\n"
            "Comandos (solo admins):\n"
            "• /config — ver configuración\n"
            "• /scan — revisar inactividad ahora\n"
            "• /backup — descargar activity.json\n"
            "• /ping — publicar botón “Estoy activo”\n\n"
            "En grupos: añádeme y dame permisos de **banear**.",
            parse_mode="Markdown"
        )
    else:
        bot.send_message(chat_id, "✅ Bot operativo (comandos solo para administradores).")

def responder_config(chat_id: int):
    txt = (
        "⚙️ Configuración\n"
        "• Días de inactividad: {}\n"
        "• Modo seguro (no expulsa): {}\n"
        "• Requisitos:\n"
        "  - Bot administrador con permiso de banear.\n"
        "  - La privacidad del bot puede limitar lo que ve en grupos.\n"
    ).format(INACTIVITY_DAYS, "Sí" if SAFE_MODE else "No")
    bot.send_message(chat_id, txt)

def ejecutar_scan(chat_id: int):
    umbral = datetime.utcnow() - timedelta(days=INACTIVITY_DAYS)
    inactivos = []
    for (c_id, u_id), data in activity.items():
        if c_id != chat_id:
            continue
        last_seen = data.get("last_seen")
        if isinstance(last_seen, datetime) and last_seen < umbral:
            inactivos.append((u_id, data.get("username", ""), last_seen))

    if not inactivos:
        bot.send_message(chat_id, "✅ No hay inactivos según el registro actual.")
        return

    expulsados, fallidos = [], []
    for u_id, uname, last_seen in inactivos:
        if SAFE_MODE:
            bot.send_message(
                chat_id,
                "🔔 Usuario inactivo: {} (última actividad hace {} días)".format(
                    fmt_user(u_id, uname), (datetime.utcnow() - last_seen).days
                )
            )
        else:
            ok, err = expulsar_usuario(chat_id, u_id)
            if ok:
                expulsados.append((u_id, uname))
            else:
                fallidos.append((u_id, uname, err or "error"))

    save_activity()

    if SAFE_MODE:
        bot.send_message(chat_id, "🧪 Modo seguro activo: solo listado (no expulsados).")
    else:
        partes = ["🗑️ Expulsiones:"]
        if expulsados:
            partes.append("• Expulsados: " + ", ".join(fmt_user(u, n) for u, n in expulsados))
        if fallidos:
            partes.append("• Fallidos: " + ", ".join("{} ({})".format(fmt_user(u, n), e) for u, n, e in fallidos))
        bot.send_message(chat_id, "\n".join(partes))

# ====== /ping: botón + aviso educado ======
def enviar_ping(chat_id: int):
    text = (
        "🔎 *Pase de lista*\n"
        "Si sigues activo en el grupo, pulsa el botón para registrar tu actividad "
        "sin necesidad de escribir.\n\n"
        "ℹ️ *Aviso*: quienes no pulsen el botón en los próximos *28 días* "
        "podrán ser *baneados* por inactividad. Lo hacemos con cariño, solo para mantener "
        "el grupo ordenado. ¡Gracias por tu comprensión! 🙏"
    )
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("Estoy activo ✅", callback_data="ping:active"))
    try:
        msg = bot.send_message(chat_id, text, reply_markup=markup, parse_mode="Markdown")
        try:
            bot.pin_chat_message(chat_id, msg.message_id, disable_notification=True)
        except Exception:
            pass
    except Exception as e:
        logging.warning("enviar_ping error: %s", e)

def handle_callback(cb: Dict[str, Any]):
    try:
        data = cb.get("data", "")
        user = cb.get("from", {}) or {}
        user_id = user.get("id")
        username = user.get("username") or ""
        msg = cb.get("message", {}) or {}
        chat = msg.get("chat", {}) or {}
        chat_id = chat.get("id")

        if data == "ping:active" and chat_id and user_id:
            actualizar_actividad(chat_id, user_id, username)
            bot.answer_callback_query(cb.get("id"), text="¡Anotado! ✅")
    except Exception as e:
        logging.warning("handle_callback error: %s", e)

def handle_chat_member_update(cm: Dict[str, Any]):
    try:
        chat = cm.get("chat", {}) or {}
        chat_id = chat.get("id")
        new = cm.get("new_chat_member", {}) or {}
        user = new.get("user", {}) or {}
        user_id = user.get("id")
        username = user.get("username") or ""
        new_status = new.get("status")

        if not chat_id or not user_id:
            return

        if new_status in ("member", "administrator", "creator"):
            actualizar_actividad(chat_id, user_id, username)
        elif new_status in ("left", "kicked"):
            if (chat_id, user_id) in activity:
                del activity[(chat_id, user_id)]
                save_activity()
    except Exception as e:
        logging.warning("handle_chat_member_update error: %s", e)

# ====== WEBHOOK SETUP ======
def setup_webhook():
    try:
        bot.remove_webhook()
        time.sleep(1)
        url = f"{WEBHOOK_BASE}/webhook"
        bot.set_webhook(
            url=url,
            allowed_updates=["message", "edited_message", "callback_query", "chat_member"]
        )
        logging.info("[WEBHOOK] Configurado: %s", url)
    except Exception as e:
        logging.exception("[WEBHOOK] Error configurando webhook: %s", e)

# ====== ARRANQUE ======
def main():
    load_activity()
    setup_webhook()

main()
# No polling aquí; Render + gunicorn sirven Flask.
