import os
import time
import json
from datetime import datetime, timedelta
from threading import Thread
import telebot
from telebot import types
from flask import Flask

# ==================== CONFIGURACIÓN ====================

# Token de tu bot (lo pegarás desde BotFather)
BOT_TOKEN = os.environ.get('BOT_TOKEN', 'TU_TOKEN_AQUI')

# Días de inactividad antes de expulsar
DIAS_INACTIVIDAD = 28

# Días de aviso previo
DIAS_AVISO = 3

# Mensaje de aviso personalizado
MENSAJE_AVISO = """
👋 Hola {mention},

Llevas **{dias} días sin participar** en nuestro grupo de batería.

🥁 Un simple 👍, un "gracias" o cualquier mensaje bastan para seguir con nosotros.

⏰ Tienes {dias_restantes} días para interactuar, o serás eliminado automáticamente.

¡Esperamos leerte pronto!
"""

# Archivo para guardar datos
DATA_FILE = 'usuarios_actividad.json'

# ==================== INICIALIZACIÓN ====================

bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)

# Estructura de datos: {chat_id: {user_id: {'username': str, 'last_activity': timestamp, 'warned': bool}}}
usuarios_data = {}

# ==================== FUNCIONES AUXILIARES ====================

def cargar_datos():
    """Carga datos del archivo JSON"""
    global usuarios_data
    try:
        if os.path.exists(DATA_FILE):
            with open(DATA_FILE, 'r') as f:
                usuarios_data = json.load(f)
                print(f"✅ Datos cargados: {len(usuarios_data)} grupos")
        else:
            usuarios_data = {}
            print("📝 Archivo de datos creado")
    except Exception as e:
        print(f"❌ Error cargando datos: {e}")
        usuarios_data = {}

def guardar_datos():
    """Guarda datos en archivo JSON"""
    try:
        with open(DATA_FILE, 'w') as f:
            json.dump(usuarios_data, f, indent=2)
    except Exception as e:
        print(f"❌ Error guardando datos: {e}")

def actualizar_actividad(chat_id, user_id, username):
    """Actualiza la última actividad de un usuario"""
    chat_id = str(chat_id)
    user_id = str(user_id)
    
    if chat_id not in usuarios_data:
        usuarios_data[chat_id] = {}
    
    usuarios_data[chat_id][user_id] = {
        'username': username or 'Usuario',
        'last_activity': time.time(),
        'warned': False
    }
    guardar_datos()

def es_admin(chat_id, user_id):
    """Verifica si un usuario es administrador"""
    try:
        member = bot.get_chat_member(chat_id, user_id)
        return member.status in ['creator', 'administrator']
    except:
        return False

def formatear_mencion(user_id, username):
    """Formatea mención del usuario"""
    if username:
        return f"@{username}"
    else:
        return f"[Usuario](tg://user?id={user_id})"

# ==================== MONITOREO DE ACTIVIDAD ====================

@bot.message_handler(func=lambda message: True, content_types=['text', 'photo', 'video', 'audio', 'document', 'sticker', 'voice'])
def registrar_actividad(message):
    """Registra actividad de todos los usuarios"""
    if message.chat.type in ['group', 'supergroup']:
        actualizar_actividad(
            message.chat.id,
            message.from_user.id,
            message.from_user.username
        )

# ==================== COMANDOS ====================

@bot.message_handler(commands=['start', 'help'])
def cmd_start(message):
    """Comando de inicio"""
    if message.chat.type == 'private':
        bot.reply_to(message, 
            "👋 ¡Hola! Soy un bot que expulsa usuarios inactivos.\n\n"
            "📋 **Comandos disponibles:**\n"
            "/start - Muestra este mensaje\n"
            "/config - Ver configuración actual\n"
            "/stats - Ver estadísticas del grupo\n"
            "/check - Revisar usuarios inactivos ahora\n\n"
            "⚙️ **Añádeme a tu grupo** y hazme administrador con permisos de 'Banear usuarios'",
            parse_mode='Markdown'
        )
    else:
        if es_admin(message.chat.id, message.from_user.id):
            bot.reply_to(message,
                f"✅ Bot activado en este grupo\n\n"
                f"📊 Configuración:\n"
                f"• Días de inactividad: {DIAS_INACTIVIDAD}\n"
                f"• Aviso previo: {DIAS_AVISO} días\n"
                f"• Usuarios registrados: {len(usuarios_data.get(str(message.chat.id), {}))}\n\n"
                f"Usa /config para más detalles",
                parse_mode='Markdown'
            )

@bot.message_handler(commands=['config'])
def cmd_config(message):
    """Muestra configuración actual"""
    if message.chat.type not in ['group', 'supergroup']:
        bot.reply_to(message, "⚠️ Este comando solo funciona en grupos")
        return
    
    if not es_admin(message.chat.id, message.from_user.id):
        bot.reply_to(message, "⚠️ Solo los administradores pueden usar este comando")
        return
    
    chat_id = str(message.chat.id)
    total_usuarios = len(usuarios_data.get(chat_id, {}))
    
    config_msg = f"""
⚙️ **Configuración Actual**

📅 Días de inactividad: **{DIAS_INACTIVIDAD} días**
⏰ Aviso previo: **{DIAS_AVISO} días**
👥 Usuarios monitoreados: **{total_usuarios}**

📝 El bot está monitoreando la actividad de todos los usuarios.
Los usuarios inactivos recibirán un aviso {DIAS_AVISO} días antes de ser expulsados.

Comandos útiles:
/stats - Ver estadísticas detalladas
/check - Revisar usuarios inactivos ahora
"""
    bot.reply_to(message, config_msg, parse_mode='Markdown')

@bot.message_handler(commands=['stats'])
def cmd_stats(message):
    """Muestra estadísticas del grupo"""
    if message.chat.type not in ['group', 'supergroup']:
        bot.reply_to(message, "⚠️ Este comando solo funciona en grupos")
        return
    
    if not es_admin(message.chat.id, message.from_user.id):
        bot.reply_to(message, "⚠️ Solo los administradores pueden usar este comando")
        return
    
    chat_id = str(message.chat.id)
    usuarios = usuarios_data.get(chat_id, {})
    
    if not usuarios:
        bot.reply_to(message, "📊 Aún no hay datos de actividad registrados.")
        return
    
    ahora = time.time()
    activos = 0
    advertidos = 0
    proximos_expulsar = 0
    
    for user_data in usuarios.values():
        dias_inactivo = (ahora - user_data['last_activity']) / 86400
        if dias_inactivo < DIAS_INACTIVIDAD - DIAS_AVISO:
            activos += 1
        elif dias_inactivo < DIAS_INACTIVIDAD:
            advertidos += 1
        else:
            proximos_expulsar += 1
    
    stats_msg = f"""
📊 **Estadísticas del Grupo**

👥 Total usuarios: **{len(usuarios)}**
✅ Activos: **{activos}**
⚠️ Advertidos: **{advertidos}**
🚫 Próximos a expulsar: **{proximos_expulsar}**

Última actualización: {datetime.now().strftime('%d/%m/%Y %H:%M')}
"""
    bot.reply_to(message, stats_msg, parse_mode='Markdown')

@bot.message_handler(commands=['check'])
def cmd_check(message):
    """Revisa manualmente usuarios inactivos"""
    if message.chat.type not in ['group', 'supergroup']:
        bot.reply_to(message, "⚠️ Este comando solo funciona en grupos")
        return
    
    if not es_admin(message.chat.id, message.from_user.id):
        bot.reply_to(message, "⚠️ Solo los administradores pueden usar este comando")
        return
    
    bot.reply_to(message, "🔍 Revisando usuarios inactivos...")
    revisar_inactivos()
    bot.send_message(message.chat.id, "✅ Revisión completada")

# ==================== REVISIÓN DE INACTIVIDAD ====================

def revisar_inactivos():
    """Revisa y procesa usuarios inactivos en todos los grupos"""
    ahora = time.time()
    
    for chat_id, usuarios in list(usuarios_data.items()):
        for user_id, data in list(usuarios.items()):
            dias_inactivo = (ahora - data['last_activity']) / 86400
            
            # Expulsar si superó el límite
            if dias_inactivo >= DIAS_INACTIVIDAD:
                try:
                    # Verificar que el usuario no sea admin
                    if not es_admin(int(chat_id), int(user_id)):
                        bot.kick_chat_member(int(chat_id), int(user_id))
                        bot.unban_chat_member(int(chat_id), int(user_id))  # Desbanear para que pueda volver
                        
                        # Notificar en el grupo
                        username = data['username']
                        bot.send_message(
                            int(chat_id),
                            f"🚫 Usuario {formatear_mencion(user_id, username)} ha sido expulsado por {DIAS_INACTIVIDAD} días de inactividad.",
                            parse_mode='Markdown'
                        )
                        
                        # Eliminar del registro
                        del usuarios_data[chat_id][user_id]
                        guardar_datos()
                        print(f"✅ Usuario {user_id} expulsado del chat {chat_id}")
                except Exception as e:
                    print(f"❌ Error expulsando usuario {user_id}: {e}")
            
            # Enviar aviso si está en periodo de aviso
            elif dias_inactivo >= (DIAS_INACTIVIDAD - DIAS_AVISO) and not data.get('warned', False):
                try:
                    username = data['username']
                    dias_restantes = DIAS_INACTIVIDAD - int(dias_inactivo)
                    
                    mensaje = MENSAJE_AVISO.format(
                        mention=formatear_mencion(user_id, username),
                        dias=int(dias_inactivo),
                        dias_restantes=dias_restantes
                    )
                    
                    bot.send_message(int(chat_id), mensaje, parse_mode='Markdown')
                    
                    # Marcar como advertido
                    usuarios_data[chat_id][user_id]['warned'] = True
                    guardar_datos()
                    print(f"⚠️ Aviso enviado a usuario {user_id} en chat {chat_id}")
                except Exception as e:
                    print(f"❌ Error enviando aviso a {user_id}: {e}")

def tarea_revision_periodica():
    """Tarea que revisa inactivos cada 6 horas"""
    while True:
        try:
            print(f"🔍 Iniciando revisión de inactividad - {datetime.now()}")
            revisar_inactivos()
            print("✅ Revisión completada")
        except Exception as e:
            print(f"❌ Error en revisión periódica: {e}")
        
        # Esperar 6 horas (21600 segundos)
        time.sleep(21600)

# ==================== SERVIDOR FLASK (KEEP ALIVE) ====================

@app.route('/')
def home():
    return "🤖 Bot de Inactividad Telegram está funcionando!"

def run_flask():
    app.run(host='0.0.0.0', port=8080)

# ==================== INICIO DEL BOT ====================

if __name__ == '__main__':
    print("🚀 Iniciando bot de inactividad...")
    
    # Cargar datos guardados
    cargar_datos()
    
    # Iniciar servidor Flask en thread separado (para keep alive)
    flask_thread = Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()
    print("✅ Servidor Flask iniciado")
    
    # Iniciar tarea de revisión periódica
    revision_thread = Thread(target=tarea_revision_periodica)
    revision_thread.daemon = True
    revision_thread.start()
    print("✅ Tarea de revisión periódica iniciada")
    
    # Iniciar bot
    print("✅ Bot iniciado y escuchando mensajes...")
    bot.infinity_polling()