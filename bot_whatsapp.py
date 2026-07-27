from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from twilio.rest import Client as TwilioClient
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

NUMERO_ADMIN = os.getenv("NUMERO_ADMIN", "573152251406")
VERIFY_TOKEN_META = os.getenv("VERIFY_TOKEN_META", "botdemo2026")
MAX_MENSAJES = 20
SHEET_ID = os.getenv("SHEET_ID")

# Credenciales de Twilio (para notificar al admin en modo sandbox)
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_WHATSAPP_NUMBER = os.getenv("TWILIO_WHATSAPP_NUMBER", "whatsapp:+14155238886")

# ─────────────────────────────────────────
# ESTADO DEL BOT
# ─────────────────────────────────────────

conversaciones = {}
pausados = set()
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
    negocios = {
        # "573001234567": "negocios/cliente1.txt",
    }
    archivo = negocios.get(numero, "negocio.txt")
    try:
        with open(archivo, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        with open("negocio.txt", "r", encoding="utf-8") as f:
            return f.read()


def crear_system_message(numero=None):
    """Crea el system message con info del negocio, inventario y flujo de compra."""
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

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PROCESO DE PEDIDO — SIGUE ESTOS PASOS EXACTAMENTE:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Cuando un cliente quiera comprar, sigue este proceso en orden:

PASO 1 - ACUMULAR PRODUCTOS:
- Anota cada producto que el cliente pida con su talla y cantidad
- Muestra la lista actualizada con precios después de cada producto agregado
- Pregunta: "¿Deseas agregar algo más o confirmamos el pedido?"
- IMPORTANTE: Solo acepta productos que estén en el inventario y con unidades disponibles (mayor a 0)
- Si un producto está agotado, indícalo y sugiere alternativas disponibles

PASO 2 - CONFIRMAR LISTA:
- Cuando el cliente diga "confirmar", "listo", "eso es todo" o similar
- Muestra el resumen completo con subtotal
- Ejemplo:
  "📝 Tu pedido:
  • 2x Boxer talla M — $50.000
  • 1x Pijama talla S — $30.000
  Subtotal: $80.000"

PASO 3 - TIPO DE ENTREGA:
- Pregunta exactamente esto:
  "¿Cómo prefieres recibirlo?
  🏪 *Recoger en el local* — Av. 4 # 11-15, Edificio Benur Local 4, Centro (gratis)
  🛵 *Domicilio* — $5.000 adicional"

PASO 4 - DIRECCIÓN (solo si eligió domicilio):
- Pide la dirección completa de entrega
- Confirma que es dentro de Cúcuta

PASO 5 - DATOS DE PAGO:
- Muestra el total final y los datos de pago:

  "✅ *Pedido confirmado*

  📦 Resumen:
  [lista de productos con precios]
  [costo domicilio si aplica]
  ━━━━━━━━━━━━━━
  💰 *Total: $[total]*

  💳 *Datos de pago:*
  🏦 Bancolombia ahorros: 497-000195-85
  👤 Titular: Luis Felipe Parra Granados
  📱 Bre-B / Llave: 0073865313

  Por favor envía tu comprobante de pago por este mismo chat 📸
  ¡Gracias por tu compra en Solo Medias y Algo Más! 🛍️"

- Al FINAL de ese mensaje, en una línea separada, agrega EXACTAMENTE esto (el sistema lo usa internamente y no lo ve el cliente):
PEDIDO_CONFIRMADO|[lista productos y cantidades]|[Domicilio o Recoger en local]|[dirección o N/A]|$[total con domicilio si aplica]

REGLAS IMPORTANTES:
- Nunca confirmes un pedido sin antes mostrar el total y los datos de pago
- Si el cliente cambia de opinión, actualiza el pedido sin problema
- Si hay algún producto que no está en el inventario, no lo agregues al pedido
- El domicilio solo aplica dentro de Cúcuta
- Sé paciente si el cliente tiene dudas durante el proceso
"""
    }


# ─────────────────────────────────────────
# NOTIFICACIÓN AL ADMIN
# ─────────────────────────────────────────

def notificar_admin(mensaje, canal="ambos"):
    """Envía notificación al admin por WhatsApp (Meta y/o Twilio)."""

    # Intentar por Meta API
    if canal in ("meta", "ambos"):
        try:
            enviar_mensaje_whatsapp(NUMERO_ADMIN, mensaje)
            logger.info("Notificación enviada al admin por Meta API")
        except Exception as e:
            logger.error(f"Error notificando admin por Meta: {e}")

    # Intentar por Twilio
    if canal in ("twilio", "ambos"):
        try:
            twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
            twilio_client.messages.create(
                from_=TWILIO_WHATSAPP_NUMBER,
                to=f"whatsapp:+{NUMERO_ADMIN}",
                body=mensaje
            )
            logger.info("Notificación enviada al admin por Twilio")
        except Exception as e:
            logger.error(f"Error notificando admin por Twilio: {e}")


def procesar_pedido_confirmado(respuesta_texto, identificador):
    """
    Detecta si hay un pedido confirmado en la respuesta,
    extrae el resumen y notifica al admin.
    Retorna la respuesta limpia (sin la línea interna).
    """
    if "PEDIDO_CONFIRMADO|" not in respuesta_texto:
        return respuesta_texto

    lineas = respuesta_texto.split("\n")
    respuesta_limpia = []

    for linea in lineas:
        if "PEDIDO_CONFIRMADO|" in linea:
            try:
                partes = linea.replace("PEDIDO_CONFIRMADO|", "").split("|")
                resumen = partes[0].strip() if len(partes) > 0 else "Sin detalle"
                entrega = partes[1].strip() if len(partes) > 1 else "No especificado"
                direccion = partes[2].strip() if len(partes) > 2 else "N/A"
                total = partes[3].strip() if len(partes) > 3 else "No especificado"

                mensaje_admin = (
                    f"🛒 *NUEVO PEDIDO*\n\n"
                    f"📦 *Productos:*\n{resumen}\n\n"
                    f"🚚 *Entrega:* {entrega}\n"
                    f"📍 *Dirección:* {direccion}\n"
                    f"💰 *Total:* {total}\n\n"
                    f"📱 *Cliente:* +{identificador.replace('whatsapp:+', '').replace('whatsapp:', '')}\n\n"
                    f"_Responde directamente al cliente para coordinar._"
                )

                notificar_admin(mensaje_admin, canal="ambos")
                logger.info(f"Pedido confirmado — notificación enviada al admin")

            except Exception as e:
                logger.error(f"Error procesando pedido confirmado: {e}")
        else:
            respuesta_limpia.append(linea)

    return "\n".join(respuesta_limpia).strip()


# ─────────────────────────────────────────
# FUNCIÓN CENTRAL: procesa cualquier mensaje
# ─────────────────────────────────────────

def procesar_mensaje(identificador, mensaje_usuario, es_admin):
    global bot_activo

    # ── Comandos del administrador ──
    if es_admin:
        cmd = mensaje_usuario.lower().strip()

        if cmd.startswith("pausar "):
            id_pausar = mensaje_usuario[7:].strip()
            pausados.add(id_pausar)
            return f"✅ Bot pausado para {id_pausar}"

        elif cmd.startswith("activar "):
            id_activar = mensaje_usuario[8:].strip()
            pausados.discard(id_activar)
            return f"✅ Bot reactivado para {id_activar}"

        elif cmd == "lista":
            if pausados:
                return "📋 Conversaciones pausadas:\n" + "\n".join(pausados)
            return "✅ No hay conversaciones pausadas."

        elif cmd == "pausar todo":
            bot_activo = False
            return "⏸️ Bot pausado para todos los usuarios."

        elif cmd == "activar todo":
            bot_activo = True
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
    else:
        conversaciones[identificador][0] = crear_system_message(identificador)

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

        # Detectar y procesar pedidos confirmados
        respuesta_texto = procesar_pedido_confirmado(respuesta_texto, identificador)

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
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    if mode == "subscribe" and token == VERIFY_TOKEN_META:
        logger.info("Webhook de Meta verificado correctamente")
        return challenge, 200
    return "Token inválido", 403


@app.route("/whatsapp_meta", methods=["POST"])
def whatsapp_meta_reply():
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
