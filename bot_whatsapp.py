from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from openai import OpenAI
from dotenv import load_dotenv
import gspread
from google.oauth2.service_account import Credentials
import os
import requests as req
import logging
import json
# ─────────────────────────────────────────
# CONFIGURACIÓN INICIAL
# ─────────────────────────────────────────
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

load_dotenv()
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
app = Flask(__name__)

# ─────────────────────────────────────────
# VARIABLES DE CONFIGURACIÓN
# ─────────────────────────────────────────

# Número del administrador
NUMERO_ADMIN = os.getenv("NUMERO_ADMIN", "573152251406")

# Token de verificación para el webhook de Meta
VERIFY_TOKEN_META = os.getenv("VERIFY_TOKEN_META", "botdemo2026")

# Máximo de mensajes por conversación (para controlar costos)
MAX_MENSAJES = 20

# ID del Google Sheet 
SHEET_ID = os.getenv("SHEET_ID")

# ─────────────────────────────────────────
# ESTADO DEL BOT
# ─────────────────────────────────────────

# Historial de conversaciones por usuario
conversaciones = {}

# Números pausados (el bot no responde a estos)
pausados = set()

# Estado global del bot (True = activo, False = pausado para todos)
bot_activo = True


# ─────────────────────────────────────────
# GOOGLE SHEETS: LEER INVENTARIO
# ─────────────────────────────────────────

def obtener_inventario():
    """Lee el inventario desde Google Sheets en tiempo real."""
    try:
        scope = [
            "https://spreadsheets.google.com/feeds",
            "https://www.googleapis.com/auth/drive"
        ]
        creds_json = json.loads(os.getenv("GOOGLE_CREDENTIALS"))
        creds = Credentials.from_service_account_info(creds_json, scopes=scope)
        gc = gspread.authorize(creds)
        hoja = gc.open_by_key(SHEET_ID).sheet1
        datos = hoja.get_all_records()

        logger.info(f"Google Sheets: {len(datos)} filas leídas")

        if not datos:
            return ""

        inventario_texto = "INVENTARIO ACTUAL:\n"
        for item in datos:
            disponible = item.get("Disponible", 0)
            if isinstance(disponible, int):
                estado = f"✅ {disponible} unidades" if disponible > 0 else "❌ Agotado"
            else:
                estado = "✅ Disponible" if str(disponible).lower() == "sí" else "❌ Agotado"

            inventario_texto += f"- {item['Producto']} talla {item['Talla']}: {item['Precio']} — {estado}\n"

        return inventario_texto

    except Exception as e:
        logger.error(f"Error leyendo Google Sheets: {e}")
        return ""


# ─────────────────────────────────────────
# CARGA DE INFORMACIÓN DEL NEGOCIO
# ─────────────────────────────────────────

def cargar_info_negocio(numero=None):
    """
    Carga el negocio.txt correcto según el número de teléfono.
    Cuando tengas múltiples clientes, agrega sus números aquí.
    """
    negocios = {
        # "573001234567": "negocios/cliente1.txt",
        # "573009876543": "negocios/cliente2.txt",
    }
    archivo = negocios.get(numero, "negocio.txt")
    try:
        with open(archivo, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        with open("negocio.txt", "r", encoding="utf-8") as f:
            return f.read()


def crear_system_message(numero=None):
    """Crea el system message con la info del negocio e inventario actualizado."""
    info = cargar_info_negocio(numero)
    inventario = obtener_inventario()

    return {
        "role": "system",
        "content": f"""Eres el asistente virtual de este negocio.
Responde SOLO basándote en la siguiente información.
Si te preguntan algo que no está aquí, dilo amablemente y sugiere contactar directamente al negocio.
Si alguien pregunta quién desarrolló este bot o cómo pueden tener uno igual, menciona que fue desarrollado por Chatbots y da el número de WhatsApp +57 315 225 1406.
Responde siempre en el mismo idioma en que te escriben.
Sé amable, conciso y profesional.

INFORMACIÓN DEL NEGOCIO:
{info}

{inventario}
"""
    }


# ─────────────────────────────────────────
# FUNCIÓN CENTRAL: procesa cualquier mensaje
# ─────────────────────────────────────────

def procesar_mensaje(identificador, mensaje_usuario, es_admin):
    """
    Lógica central compartida entre WhatsApp (Twilio) y WhatsApp (Meta).
    Retorna el texto de respuesta, o None si el bot no debe responder.
    """
    global bot_activo

    # ── Comandos del administrador ──
    if es_admin:
        cmd = mensaje_usuario.lower().strip()

        if cmd.startswith("pausar "):
            id_pausar = mensaje_usuario[7:].strip()
            pausados.add(id_pausar)
            logger.info(f"Bot pausado para: {id_pausar}")
            return f"✅ Bot pausado para {id_pausar}"

        elif cmd.startswith("activar "):
            id_activar = mensaje_usuario[8:].strip()
            pausados.discard(id_activar)
            logger.info(f"Bot reactivado para: {id_activar}")
            return f"✅ Bot reactivado para {id_activar}"

        elif cmd == "lista":
            if pausados:
                return "📋 Conversaciones pausadas:\n" + "\n".join(pausados)
            return "✅ No hay conversaciones pausadas."

        elif cmd == "pausar todo":
            bot_activo = False
            logger.info("Bot pausado globalmente")
            return "⏸️ Bot pausado para todos los usuarios."

        elif cmd == "activar todo":
            bot_activo = True
            logger.info("Bot reactivado globalmente")
            return "▶️ Bot reactivado para todos los usuarios."

        elif cmd == "estado":
            estado = "✅ Activo" if bot_activo else "⏸️ Pausado globalmente"
            return (
                f"📊 Estado del bot:\n"
                f"• Estado global: {estado}\n"
                f"• Conversaciones activas: {len(conversaciones)}\n"
                f"• Usuarios pausados: {len(pausados)}"
            )

        elif cmd.startswith("borrar "):
            id_borrar = mensaje_usuario[7:].strip()
            if id_borrar in conversaciones:
                del conversaciones[id_borrar]
                return f"🗑️ Historial borrado para {id_borrar}"
            return f"No encontré conversación activa para {id_borrar}"

        elif cmd == "borrar todo":
            conversaciones.clear()
            return "🗑️ Todos los historiales borrados."

        elif cmd == "ayuda":
            return (
                "📖 Comandos disponibles:\n\n"
                "• *pausar [número]* — pausa el bot para ese usuario\n"
                "• *activar [número]* — reactiva el bot para ese usuario\n"
                "• *pausar todo* — pausa el bot para todos\n"
                "• *activar todo* — reactiva el bot para todos\n"
                "• *lista* — muestra usuarios pausados\n"
                "• *estado* — muestra el estado actual del bot\n"
                "• *borrar [número]* — borra el historial de un usuario\n"
                "• *borrar todo* — borra todos los historiales\n"
                "• *ayuda* — muestra este menú"
            )

    # ── Verificaciones antes de responder ──
    if not bot_activo:
        return None

    if identificador in pausados:
        return None

    # ── Lógica normal del chatbot ──
    if identificador not in conversaciones:
        conversaciones[identificador] = [crear_system_message(identificador)]

    conversaciones[identificador].append({"role": "user", "content": mensaje_usuario})

    # Limitar historial para controlar costos
    if len(conversaciones[identificador]) > MAX_MENSAJES + 1:
        conversaciones[identificador] = (
            [conversaciones[identificador][0]] +
            conversaciones[identificador][-MAX_MENSAJES:]
        )

    try:
        respuesta = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=conversaciones[identificador]
        )
        respuesta_texto = respuesta.choices[0].message.content
        conversaciones[identificador].append({"role": "assistant", "content": respuesta_texto})
        logger.info(f"Respuesta generada para {identificador[:8]}...")
        return respuesta_texto

    except Exception as e:
        logger.error(f"Error OpenAI: {e}")
        conversaciones[identificador].pop()
        return "Lo siento, tuve un problema técnico. Por favor intenta de nuevo en un momento. 🙏"


# ─────────────────────────────────────────
# RUTA DE SALUD (para UptimeRobot)
# ─────────────────────────────────────────

@app.route("/", methods=["GET"])
def home():
    return "Bot activo", 200


# ─────────────────────────────────────────
# WHATSAPP VÍA TWILIO (sandbox/producción)
# ─────────────────────────────────────────

@app.route("/whatsapp", methods=["POST"])
def whatsapp_reply():
    numero = request.form.get("From")
    mensaje_usuario = request.form.get("Body")

    if not numero or not mensaje_usuario:
        return str(MessagingResponse())

    logger.info(f"Twilio - mensaje de {numero}: {mensaje_usuario[:50]}")
    es_admin = (numero == f"whatsapp:+{NUMERO_ADMIN}")

    respuesta_texto = procesar_mensaje(numero, mensaje_usuario, es_admin)

    resp = MessagingResponse()
    if respuesta_texto:
        resp.message(respuesta_texto)
    return str(resp)


# ─────────────────────────────────────────
# WHATSAPP VÍA META API (oficial)
# ─────────────────────────────────────────

@app.route("/whatsapp_meta", methods=["GET"])
def verificar_webhook_meta():
    """Meta llama a esta ruta para verificar el webhook al configurarlo."""
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    if mode == "subscribe" and token == VERIFY_TOKEN_META:
        logger.info("Webhook de Meta verificado correctamente")
        return challenge, 200
    return "Token inválido", 403


@app.route("/whatsapp_meta", methods=["POST"])
def whatsapp_meta_reply():
    """Recibe mensajes reales de WhatsApp vía Meta API."""
    datos = request.get_json()

    try:
        mensaje_evento = datos["entry"][0]["changes"][0]["value"]["messages"][0]
        numero = mensaje_evento["from"]

        if mensaje_evento.get("type") != "text":
            enviar_mensaje_whatsapp(numero, "Por el momento solo puedo responder mensajes de texto. 😊")
            return "OK", 200

        mensaje_usuario = mensaje_evento["text"]["body"]

    except (KeyError, IndexError):
        return "OK", 200

    logger.info(f"Meta - mensaje de {numero}: {mensaje_usuario[:50]}")
    es_admin = (numero == NUMERO_ADMIN)

    respuesta_texto = procesar_mensaje(numero, mensaje_usuario, es_admin)

    if respuesta_texto:
        enviar_mensaje_whatsapp(numero, respuesta_texto)

    return "OK", 200


def enviar_mensaje_whatsapp(numero_destino, texto):
    """Envía un mensaje de texto vía Meta WhatsApp API."""
    url = f"https://graph.facebook.com/v19.0/{os.getenv('WHATSAPP_PHONE_NUMBER_ID')}/messages"
    headers = {
        "Authorization": f"Bearer {os.getenv('WHATSAPP_ACCESS_TOKEN')}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": numero_destino,
        "type": "text",
        "text": {"body": texto}
    }
    response = req.post(url, headers=headers, json=payload)
    logger.info(f"Meta API response: {response.status_code}")


if __name__ == "__main__":
    app.run(port=5000)
