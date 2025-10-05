# main.py — Bot de inactividad para Telegram (Flask + pyTelegramBotAPI) compatible con Python 3.8

import os
import time
import threading
from datetime import datetime, timedelta
from typing import Optional, Dict, Tuple

from flask import Flask, request
import telebot
from telebot import types

# ========= CONFIG =========
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
WEBHOOK_BASE = os.getenv("WEBHOOK_URL", "").rstrip("/")  # p.ej. https://bot-telegram-inactividad.onrender.com
INACTIVITY_DAYS = int(os.getenv("INACTIVITY_DAYS", "14"))  # días sin actividad
SAFE_MODE = os.getenv("SAFE_MODE", "1") == "1"             # 1 = no expulsa, solo avisa
ADMIN_IDS = set()
if os.getenv("ADMIN_IDS", ""):
    for x in os.getenv("ADMIN_IDS").split(","):
        x = x.strip()
        if x.isdigit():
            ADMIN_IDS.add(int(x))

if not BOT_TOKEN:
    raise RuntimeError("Falta BOT_TOKEN en variables de entorno")
if not WEBHOOK_BASE:
    raise RuntimeError("Falta WEBHOOK_URL en variables de entorno")

# ========= APP/BOT =========
app = Flask(__name__)
bot = telebot.TeleBot(BOT_TOKEN)  # sin parse_mode global

# Memoria en RAM: {(chat_id, user_id): {"last_seen": datetime, "username": str}}
activity = {}  # type: Dict[Tuple[int, int], Dict[str, object]]

# ========= UTIL =========
def fmt_user(user_id, username):
    return "@{}".format(username) if username else "ID:{}".format(user_id)

def actualizar_actividad(chat_id, user_id, username):
    activity[(chat_id, user_id)] = {
        "last_seen": datetime.utcnow(),
        "username": username or ""
    }

def dias_desde(dt):
    return (datetime.utcnow() - dt).days

def es_admin(user_id):
    # Si no configuras ADMIN_IDS, cualquiera puede ejecutar /scan
    return (user_id in ADMIN_IDS) if ADMIN_IDS else True

def puede_expulsar(chat_id):
    try:
        me = bot.get_me()
        member = bot.get_chat_member(chat_id, me.id)
        status = getattr(member, "status", "")
        can_restrict = getattr(member, "can_restrict_members", False)
        return (status in ("administrator", "creator")) and (can_restrict or status == "creator")
    except Exception:
        return False

def expulsar_usuario(chat_id, user_id):
    try:
        bot.ban_chat_member(chat_id, user_id)
        bot.unban_chat_member(chat_id, user_id, only_if_banned=True)
        return True, None
    except Exception as e:
        return False, str(e)

# ========= HTTP =========
@app.route("/", methods=["GET"])
def health():
    return "Bot activo ✅", 200

@app.route("/webhook", methods=["POST"])
def webhook():
    if request.is_json:  # acepta application/json; charset=utf-8
        json_string = request.get_data(as_text=True)
        update = telebot.types.Update.de_json(json_string)
        bot.process_new_updates([update])
        return "", 200
    return "", 403

# ========= WEBHOOK SETUP =========
def setup_webhook():
    try:
        bot.remove_webhook()
        time.sleep(1)
        url = "{}/webhook".format(WEBHOOK_BASE)
        bot.set_webhook(url=url, allowed_updates=["message"])
        print("[WEBHOOK] Configurado:", url)
    except Exception as e:
        print("[WEBHOOK] Error configurando webhook:", e)

# ========= HANDLERS =========
@bot.message_handler(commands=["start", "help"])
def cmd_start_help(message):
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
                    fmt_user(u_id, uname), dias_desde(last_seen)
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
            txt += "• Fallidos: " + ", ".join("{} ({})".format(fmt_user(u, n), e) for u, n, e in fallidos)
        bot.reply_to(message, txt or "Sin resultados.")

# Registrar actividad en grupos SIN atrapar comandos
@bot.message_handler(
    func=lambda m: m.chat.type in ('group', 'supergroup') and not ((getattr(m, 'text', '') or '').startswith('/')),
    content_types=['text', 'photo', 'video', 'audio', 'document', 'sticker', 'voice']
)
def registrar_actividad(message):
    try:
        actualizar_actividad(
            message.chat.id,
            message.from_user.id,
            getattr(message.from_user, "username", None)
        )
    except Exception:
        pass

# ========= TAREA PERIÓDICA (opcional, desactivada por defecto) =========
def tarea_periodica():
    # Plantilla para añadir avisos/expulsiones automáticas si lo quieres en el futuro.
    while True:
        time.sleep(6 * 3600)

def iniciar_tareas():
    t = threading.Thread(target=tarea_periodica, daemon=True)
    t.start()

# ========= ARRANQUE =========
setup_webhook()
iniciar_tareas()
# No usar polling: Render + webhook + gunicorn se encargan del server.
