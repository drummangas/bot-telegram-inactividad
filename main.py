# main.py
import os
import time
import threading
from datetime import datetime, timedelta

from flask import Flask, request
import telebot
from telebot import types

# ==================== CONFIG ====================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
WEBHOOK_BASE = os.getenv("WEBHOOK_URL", "").rstrip("/")  # p.ej. https://bot-telegram-inactividad.onrender.com
INACTIVITY_DAYS = int(os.getenv("INACTIVITY_DAYS", "14"))  # días sin actividad para considerar inactivo
SAFE_MODE = os.getenv("SAFE_MODE", "1") == "1"  # en modo seguro NO expulsa, solo avisa
ADMIN_IDS = {
    int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()
}

if not BOT_TOKEN:
    raise RuntimeError("Falta BOT_TOKEN en variables de entorno")

# ==================== APP / BOT ====================

app = Flask(__name__)
bot = telebot.TeleBot(BOT_TOKEN, parse_mode=None)  # parse_mode lo pondremos por mensaje si hace falta

# Memoria simple en RAM (se borra al reiniciar). Para persistencia real usar DB.
# activity[(chat_id, user_id)] = {"last_seen": datetime, "username": str}
activity = {}

# ==================== UTILIDADES ====================

def fmt_user_link(user_id: int, username: str | None) -> str:
    """
    Devuelve una mención clicable si hay username; si no, muestra el id.
    Nota: Para tg://user hace falta Markdown; usaremos texto simple por simplicidad.
    """
    if username:
        return f"@{username}"
    return f"ID:{user_id}"

def actualizar_actividad(chat_id: int, user_id: int, username: str | None):
    key = (chat_id, user_id)
    activity[key] = {
        "last_seen": datetime.utcnow(),
        "username": username or ""
    }

def dias_desde(dt: datetime) -> int:
    return (datetime.utcnow() - dt).days

def es_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS if ADMIN_IDS else True  # si no se define ADMIN_IDS, cualquiera puede usar /scan

def puede_expulsar(chat_id: int) -> bool:
    """
    Comprueba si el bot es admin con permiso para banear en ese chat.
    """
    try:
        me = bot.get_me()
        member = bot.get_chat_member(chat_id, me.id)
        status = getattr(member, "status", "")
        can_restrict = getattr(member, "can_restrict_members", False)
        # En supergrupos recientes, 'can_restrict_members' puede venir en ChatMemberAdministrator
        return status in ("administrator", "creator") and (can_restrict or status == "creator")
    except Exception:
        return False

def expulsar_usuario(chat_id: int, user_id: int):
    """
    Expulsa (ban + unban para permitir volver con enlace) a un usuario.
    """
    try:
        bot.ban_chat_member(chat_id, user_id)
        # desbanear para dejar la puerta abierta a volver con enlace de invitación
        bot.unban_chat_member(chat_id, user_id, only_if_banned=True)
        return True, None
    except Exception as e:
        return False, str(e)

# ==================== ENDPOINTS WEB ====================

@app.route("/", methods=["GET"])
def health():
    return "Bot activo ✅", 200

@app.route("/webhook", methods=["POST"])
def webhook():
    # Acepta application/json y application/json; charset=utf-8
    if request.is_json:
        json_string = request.get_data(as_text=True)
        update = telebot.types.Update.de_json(json_string)
        bot.process_new_updates([update])
        return "", 200
    return "", 403

# ==================== CONFIGURACIÓN DEL WEBHOOK ====================

def setup_webhook():
    try:
        bot.remove_webhook()
        time.sleep(1)
        if WEBHOOK_BASE:
            webhook_url = f"{WEBHOOK_BASE}/webhook"
            # Solo queremos updates de mensajes para este bot
            bot.set_webhook(url=webhook_url, allowed_updates=["message"])
            print(f"[WEBHOOK] Configurado: {webhook_url}")
        else:
            print("[WEBHOOK] WEBHOOK_URL no está configurado")
    except Exception as e:
        print(f"[WEBHOOK] Error configurando webhook: {e}")

# ==================== HANDLERS ====================

@bot.message_handler(commands=["start", "help"])
def cmd_start_help(message: types.Message):
    try:
        chat_type = getattr(message.chat, "type", "private")
        if chat_type == "private":
            bot.reply_to(
                message,
                "👋 ¡Hola! Estoy vivo.\n\n"
                "Puedo ayudar a detectar y expulsar usuarios inactivos en tus grupos.\n"
                "1) Añádeme al grupo\n"
                "2) Dame permisos de administrador (banear)\n"
                "3) Usa /config en el grupo para ver opciones\n\n"
                "Comandos:\n"
                "• /config – ver configuración\n"
                "• /scan – comprueba ahora la inactividad (si eres admin del bot)\n"
            )
        else:
            bot.reply_to(
                message,
                "✅ Bot operativo. Usa /config para ver la configuración y /scan para revisar inactividad."
            )
    except Exception:
        pass

@bot.message_handler(commands=["config"])
def cmd_config(message: types.Message):
    try:
        chat_id = message.chat.id
        texto = (
            "⚙️ Configuración actual\n"
            f"• Días de inactividad: {INACTIVITY_DAYS}\n"
            f"• Modo seguro (no expulsa): {'Sí' if SAFE_MODE else 'No'}\n"
            "• Requisitos:\n"
            "  - El bot debe ser administrador con permiso para banear.\n"
            "  - La privacidad del bot afecta lo que puede leer en grupos.\n\n"
            "Comandos:\n"
            "• /scan – revisa ahora quién está inactivo\n"
        )
        bot.reply_to(message, texto)
    except Exception:
        pass

@bot.message_handler(commands=["scan"])
def cmd_scan(message: types.Message):
    try:
        user_id = message.from_user.id
        if not es_admin(user_id):
            bot.reply_to(message, "⛔ No tienes permiso para ejecutar /scan.")
            return

        chat = message.chat
        if chat.type not in ("group", "supergroup"):
            bot.reply_to(message, "ℹ️ /scan se usa en grupos.")
            return

        chat_id = chat.id
        if not puede_expulsar(chat_id):
            bot.reply_to(message, "⚠️ No tengo permisos de administrador para expulsar en este grupo.")
            return

        # Revisión sencilla: mira la tabla 'activity' y busca inactivos por chat
        umbral = datetime.utcnow() - timedelta(days=INACTIVITY_DAYS)
        inactivos = []
        for (c_id, u_id), data in activity.items():
            if c_id != chat_id:
                continue
            last_seen = data.get("last_seen")
            if last_seen and last_seen < umbral:
                inactivos.append((u_id, data.get("username", ""), last_seen))

        if not inactivos:
            bot.reply_to(message, "✅ No hay usuarios inactivos según el registro actual.")
            return

        # Procesar inactivos
        expulsados, fallidos = [], []
        for u_id, uname, last_seen in inactivos:
            if SAFE_MODE:
                # Solo avisar
                bot.send_message(
                    chat_id,
                    f"🔔 Usuario inactivo: {fmt_user_link(u_id, uname)} "
                    f"(última actividad hace {dias_desde(last_seen)} días)"
                )
            else:
                ok, err = expulsar_usuario(chat_id, u_id)
                if ok:
                    expulsados.append((u_id, uname))
                else:
                    fallidos.append((u_id, uname, err or "error desconocido"))

        # Resumen
        if SAFE_MODE:
            bot.reply_to(message, "🧪 Modo seguro activo: solo se han listado inactivos (no expulsados).")
        else:
            txt = "🗑️ Expulsiones completadas.\n"
            if expulsados:
                txt += "• Expulsados: " + ", ".join(fmt_user_link(u, n) for u, n in expulsados) + "\n"
            if fallidos:
                txt += "• Fallidos: " + ", ".join(f"{fmt_user_link(u, n)} ({e})" for u, n, e in fallidos)
            bot.reply_to(message, txt or "Sin resultados.")
    except Exception:
        pass

# ==================== MONITOREO DE ACTIVIDAD (no capta comandos) ====================

@bot.message_handler(
    func=lambda m: m.chat.type in ['group', 'supergroup'] and not (getattr(m, 'text', '') or '').startswith('/'),
    content_types=['text', 'photo', 'video', 'audio', 'document', 'sticker', 'voice']
)
def registrar_actividad(message: types.Message):
    try:
        actualizar_actividad(
            message.chat.id,
            message.from_user.id,
            getattr(message.from_user, "username", None)
        )
        # Si quieres ver logs de actividad, descomenta:
        # print(f"[ACTIVITY] chat:{message.chat.id} user:{message.from_user.id} {message.from_user.username} @ {datetime.utcnow()}")
    except Exception:
        pass

# ==================== TAREA PERIÓDICA (opcional) ====================

def tarea_periodica():
    """
    Hilo opcional para revisar inactividad cada X horas (aquí, 6 horas).
    Si prefieres hacerlo a mano, usa /scan y no inicies este hilo.
    """
    while True:
        try:
            # No hace nada automáticamente para evitar expulsiones no deseadas.
            # Puedes convertir esto en un aviso periódico si lo necesitas.
            time.sleep(6 * 3600)
        except Exception:
            time.sleep(60)

def iniciar_tareas():
    t = threading.Thread(target=tarea_periodica, daemon=True)
    t.start()

# ==================== ARRANQUE ====================

setup_webhook()
iniciar_tareas()

# Nota: En Render/Gunicorn se usa: gunicorn main:app --workers 1 --threads 4 --timeout 0
# El servidor Flask lo gestiona Gunicorn; NO llames bot.infinity_polling() aquí porque usas webhook.
