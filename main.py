# main.py — Render + Flask + pyTelegramBotAPI (con logs de diagnóstico)

import os
import time
import threading
import logging
from datetime import datetime, timedelta

from flask import Flask, request
import telebot
from telebot import types

# ====== LOGGING ======
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
telebot.logger.setLevel(logging.DEBUG)  # logs verbosos de la librería

# ====== CONFIG ======
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
WEBHOOK_BASE = os.getenv("WEBHOOK_URL", "").rstrip("/")
INACTIVITY_DAYS = int(os.getenv("INACTIVITY_DAYS", "14"))
SAFE_MODE = os.getenv("SAFE_MODE", "1") == "1"
ADMIN_IDS = {
    int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()
}

if not BOT_TOKEN:
    raise RuntimeError("Falta BOT_TOKEN")
if not WEBHOOK_BASE:
    raise RuntimeError("Falta WEBHOOK_URL")

# ====== APP/BOT ======
app = Flask(__name__)
bot = telebot.TeleBot(BOT_TOKEN)  # sin parse_mode global

# Memoria simple de actividad
activity = {}  # {(chat_id, user_id): {"last_seen": datetime, "username": str}}

def fmt_user(user_id, username):
    return "@{}".format(username) if username else "ID:{}".format(user_id)

def actualizar_actividad(chat_id, user_id, username):
    activity[(chat_id, user_id)] = {
        "last_seen": datetime.utcnow(),
        "username": username or ""
    }

def es_admin(user_id):
    return (user_id in ADMIN_IDS) if ADMIN_IDS else True

def puede_expulsar(chat_id):
    try:
        me = bot.get_me()
        member = bot.get_chat_member(chat_id, me.id)
        status = getattr(member, "status", "")
        can_restrict = getattr(member, "can_restrict_members", False)
        return (status in ("administrator", "creator")) and (can_restrict or status == "creator")
    except Exception as e:
        logging.warning("puede_expulsar error: %s", e)
        return False

def expulsar_usuario(chat_id, user_id):
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
    # Acepta JSON con/ sin charset
    if not request.is_json:
        logging.warning("Webhook recibido sin JSON válido. Headers: %s", dict(request.headers))
        return "", 403

    json_string = request.get_data(as_text=True)
    logging.info("Webhook JSON recibido: %s", json_string[:500])  # truncamos por si es largo

    try:
        update = telebot.types.Update.de_json(json_string)
        bot.process_new_updates([update])
        logging.info("Update procesado correctamente.")
    except Exception as e:
        logging.exception("Error procesando update: %s", e)
        # Devolvemos 200 igualmente para que Telegram no reintente en bucle
        return "", 200

    return "", 200

# ====== WEBHOOK SETUP ======
def setup_webhook():
    try:
        bot.remove_webhook()
        time.sleep(1)
        url = f"{WEBHOOK_BASE}/webhook"
        bot.set_webhook(url=url, allowed_updates=["message"])
        logging.info("[WEBHOOK] Configurado: %s", url)
    except Exception as e:
        logging.exception("[WEBHOOK] Error configurando webhook: %s", e)

# ====== HANDLERS ======
@bot.message_handler(commands=["start", "help"])
def cmd_start_help(message):
    logging.info("/start|/help de %s en chat %s", message.from_user.id, message.chat.id)
    chat_type = getattr(message.chat, "type", "private")
    if chat_type == "private":
        bot.reply_to(
            message,
            "👋 ¡Hola! Funciono por webhook en Render.\n\n"
            "Comandos:\n"
            "• /config — ver configuración\n"
            "• /scan — revisar inactividad ahora (si tienes permiso)\n"
            "En grupos: hazme admin (banear) para poder expulsar."
        )
    else:
        bot.reply_to(message, "✅ Bot operativo. Usa /config y /scan.")

@bot.message_handler(commands=["config"])
def cmd_config(message):
    logging.info("/config en chat %s por %s", message.chat.id, message.from_user.id)
    txt = (
        "⚙️ Configuración\n"
        "• Días de inactividad: {}\n"
        "• Modo seguro (no expulsa): {}\n"
        "• Requisitos:\n"
        "  - Bot administrador con permiso de banear.\n"
        "  - La privacidad del bot puede limitar lo que ve en grupos.\n"
    ).format(INACTIVITY_DAYS, "Sí" if SAFE_MODE else "No")
    bot.reply_to(message, txt)

@bot.message_handler(commands=["scan"])
def cmd_scan(message):
    logging.info("/scan en chat %s por %s", message.chat.id, message.from_user.id)
    user_id = message.from_user.id
    if not es_admin(user_id):
        bot.reply_to(message, "⛔ No tienes permiso para /scan.")
        return

    if message.chat.type not in ("group", "supergroup"):
        bot.reply_to(message, "ℹ️ /scan se usa en grupos.")
        return

    chat_id = message.chat.id
    if not puede_expulsar(chat_id):
        bot.reply_to(message, "⚠️ No tengo permisos de administrador para expulsar aquí.")
        return

    umbral = datetime.utcnow() - timedelta(days=INACTIVITY_DAYS)
    inactivos = []
    for (c_id, u_id), data in activity.items():
        if c_id != chat_id:
            continue
        last_seen = data.get("last_seen")
        if isinstance(last_seen, datetime) and last_seen < umbral:
            inactivos.append((u_id, data.get("username", ""), last_seen))

    if not inactivos:
        bot.reply_to(message, "✅ No hay inactivos según el registro actual.")
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

    if SAFE_MODE:
        bot.reply_to(message, "🧪 Modo seguro activo: solo listado (no expulsados).")
    else:
        txt = "🗑️ Expulsiones:\n"
        if expulsados:
            txt += "• Expulsados: " + ", ".join(fmt_user(u, n) for u, n in expulsados) + "\n"
        if fallidos:
            txt += "• Fallidos
