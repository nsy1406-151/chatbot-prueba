from flask import Flask, request, send_from_directory

# ── Twilio: dejado comentado, se reactivará cuando se implemente
#    el modelo Tech Provider / ISV para nuevos clientes ──
# from twilio.twiml.messaging_response import MessagingResponse
# from twilio.rest import Client as TwilioClient

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

# ── Variables de Twilio: comentadas junto con el resto del código Twilio ──
# TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
# TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
# TWILIO_WHATSAPP_NUMBER = os.getenv("TWILIO_WHATSAPP_NUMBER", "whatsapp:+14155238886")

# ─────────────────────────────────────────
# CONFIGURACIÓN DE NEGOCIOS (multi-cliente)
# ─────────────────────────────────────────
# La clave es el "phone_number_id" de Meta — el ID interno del número de
# WhatsApp AL QUE le escribió el cliente (no el número del cliente).
NEGOCIOS_CONFIG = {
    "1290940880772557": {  # Urbana Style (número de pruebas actual)
        "archivo": "negocios/urbana_style.txt",
        "sheet_id": os.getenv("SHEET_ID"),
    },
    # "OTRO_PHONE_NUMBER_ID_AQUI": {
    #     "archivo": "negocios/otro_negocio.txt",
    #     "sheet_id": "otro_sheet_id_de_google",
    # },
}

NEGOCIO_DEFAULT = {
    "archivo": "negocio.txt",
    "sheet_id": os.getenv("SHEET_ID"),
}

# ─────────────────────────────────────────
# ESTADO DEL BOT
# ─────────────────────────────────────────
conversaciones = {}
pausados = set()
bot_activo = True
mensajes_procesados = set()
MAX_MENSAJES_PROCESADOS = 500

# Relación username de WhatsApp ↔ identificador real (número o BSUID).
# Permite usar el username directamente en comandos de admin
# (ej. "pausar julianp1406") en vez del código largo o el número.
USERNAME_A_ID = {}
ID_A_USERNAME = {}

def registrar_username(identificador, username):
    """Guarda la relación username ↔ identificador cuando llega un mensaje."""
    if not username:
        return
    USERNAME_A_ID[username.lower()] = identificador
    ID_A_USERNAME[identificador] = username

def resolver_identificador(texto):
    """Si el texto dado es un username conocido, lo traduce al identificador
    real. Si no, lo devuelve tal cual (asumiendo que ya es número o BSUID)."""
    return USERNAME_A_ID.get(texto.lower().strip(), texto.strip())

def formatear_identificador_cliente(identificador):
    """Da un formato legible al identificador del cliente para las
    notificaciones al admin, incluyendo el username si lo tenemos."""
    numero_limpio = identificador.replace("whatsapp:+", "").replace("whatsapp:", "")
    username = ID_A_USERNAME.get(identificador)
    es_bsuid = len(numero_limpio) > 2 and numero_limpio[2] == "." and numero_limpio[:2].isalpha()

    if es_bsuid:
        if username:
            return f"@{username} (no compartió su número — usa este username en los comandos)"
        return f"{numero_limpio} (usuario con username, no compartió su número)"

    if username:
        return f"+{numero_limpio} (@{username})"
    return f"+{numero_limpio}"

# ─────────────────────────────────────────
# GOOGLE SHEETS: LEER INVENTARIO
# ─────────────────────────────────────────
def obtener_inventario(sheet_id):
    """Lee el inventario desde Google Sheets en tiempo real."""
    if not sheet_id:
        return ""
    try:
        scope = [
            "https://spreadsheets.google.com/feeds",
            "https://www.googleapis.com/auth/drive"
        ]
        creds_json = json.loads(os.getenv("GOOGLE_CREDENTIALS"))
        creds = Credentials.from_service_account_info(creds_json, scopes=scope)
        gc = gspread.authorize(creds)
        hoja = gc.open_by_key(sheet_id).sheet1
        datos = hoja.get_all_records()
        logger.info(f"Google Sheets ({sheet_id[:8]}...): {len(datos)} filas leídas")

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
        logger.error(f"Error leyendo Google Sheets ({sheet_id}): {e}")
        return ""

# ─────────────────────────────────────────
# CARGA DE INFORMACIÓN DEL NEGOCIO
# ─────────────────────────────────────────
def obtener_config_negocio(phone_number_id):
    return NEGOCIOS_CONFIG.get(phone_number_id, NEGOCIO_DEFAULT)

def cargar_info_negocio(phone_number_id):
    config = obtener_config_negocio(phone_number_id)
    archivo = config["archivo"]
    try:
        with open(archivo, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        logger.error(f"No se encontró {archivo}, usando negocio.txt por defecto")
        with open("negocio.txt", "r", encoding="utf-8") as f:
            return f.read()

def crear_system_message(phone_number_id):
    """Crea el system message con info del negocio, inventario y flujo de compra.

    Toda la identidad del negocio (nombre, saludo, dirección, datos de pago)
    viene de su archivo negocio.txt correspondiente — así el mismo código
    sirve para cualquier cantidad de negocios sin tocar este prompt.
    """
    config = obtener_config_negocio(phone_number_id)
    info = cargar_info_negocio(phone_number_id)
    inventario = obtener_inventario(config["sheet_id"])

    return {
        "role": "system",
        "content": f"""Eres el asistente virtual oficial del negocio descrito a continuación.

Responde SOLO basándote en la siguiente información.
Si te preguntan algo que no está aquí, dilo amablemente y sugiere contactar directamente al negocio.
Si alguien pregunta quién desarrolló este bot o cómo pueden tener uno igual, menciona que fue desarrollado por Chatbots y da el número de WhatsApp +57 315 225 1406.
Responde siempre en el mismo idioma en que te escriben.
Sé amable, cercana y profesional — como una vendedora atenta del local.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
IDENTIDAD Y PRESENTACIÓN:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
- Usa el nombre del negocio tal como aparece en la información a continuación.
- Cuando alguien te salude por primera vez o pregunte quién eres, preséntate mencionando el nombre del negocio y ofrece ayuda con productos, precios, disponibilidad y pedidos.
- NUNCA vuelvas a presentarte ni a repetir el saludo inicial una vez la conversación ya está en curso — solo preséntate en el primerísimo mensaje del cliente.
- Si el cliente agradece o se despide después de un pedido confirmado (ej. "gracias", "listo", "perfecto"), responde con un cierre breve y amable como "¡Con gusto! Cualquier cosa me escribes 😊", sin reiniciar el flujo ni presentarte de nuevo.

INFORMACIÓN DEL NEGOCIO:
{info}

{inventario}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SOLICITUD DE ATENCIÓN HUMANA:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Si el cliente dice que quiere hablar con una persona real, un humano, atención personalizada,
hablar con alguien del equipo, o frases similares, responde EXACTAMENTE así:

"Entendido 😊 Le avisaré a nuestro equipo para que te contacte pronto.
¿Hay algo más en lo que pueda ayudarte mientras tanto?"

Y agrega al FINAL de tu respuesta, en una línea separada:
ATENCION_HUMANA_SOLICITADA

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PROCESO DE PEDIDO — SIGUE ESTOS PASOS EXACTAMENTE:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Cuando un cliente quiera comprar, sigue este proceso en orden:

PASO 1 - ACUMULAR PRODUCTOS:
- Anota cada producto que el cliente pida con su talla y cantidad
- Muestra la lista actualizada con precios después de cada producto agregado
- Pregunta: "¿Deseas agregar algo más o confirmamos el pedido?"
- IMPORTANTE: Solo acepta productos que estén en el inventario con unidades disponibles mayor a 0
- Si un producto está agotado, indícalo y sugiere alternativas disponibles

PASO 2 - CONFIRMAR LISTA:
- Cuando el cliente diga "confirmar", "listo", "eso es todo" o similar
- Muestra el resumen completo con subtotal

PASO 3 - TIPO DE ENTREGA:
- Pregunta cómo prefiere recibirlo, usando las opciones de entrega descritas en la información del negocio (recoger en el local o domicilio, con sus respectivos costos y dirección).

PASO 4 - DIRECCIÓN (solo si eligió domicilio):
- Pide la dirección completa de entrega, dentro de la zona de cobertura descrita en la información del negocio.

PASO 5 - DATOS DE PAGO:
- Muestra el total final y los datos de pago tal como aparecen en la información del negocio.
- Pide que envíen el comprobante de pago por el mismo chat.

- Al FINAL de ese mensaje, en una línea separada, agrega EXACTAMENTE esto:
PEDIDO_CONFIRMADO|[lista productos y cantidades]|[Domicilio o Recoger en local]|[dirección o N/A]|$[total con domicilio si aplica]

REGLAS IMPORTANTES:
- Nunca confirmes un pedido sin antes mostrar el total y los datos de pago
- Si el cliente cambia de opinión, actualiza el pedido sin problema
- Si hay algún producto que no está en el inventario, no lo agregues al pedido
- El domicilio solo aplica dentro de la zona de cobertura del negocio
- Cuando el cliente diga que ya envió el comprobante, responde amablemente que lo revisarán pronto
"""
    }

# ─────────────────────────────────────────
# NOTIFICACIONES AL ADMIN
# ─────────────────────────────────────────
def notificar_admin_texto(mensaje, phone_number_id):
    """Envía mensaje de texto al admin por Meta API, desde el mismo número
    de negocio donde ocurrió el evento."""
    try:
        enviar_mensaje_whatsapp(NUMERO_ADMIN, mensaje, phone_number_id)
        logger.info("Notificación texto enviada al admin por Meta API")
    except Exception as e:
        logger.error(f"Error notificando admin por Meta: {e}")

    # ── Respaldo por Twilio: comentado, se reactivará junto con el resto
    #    del código Twilio cuando se implemente el modelo Tech Provider/ISV ──
    # try:
    #     twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    #     twilio_client.messages.create(
    #         from_=TWILIO_WHATSAPP_NUMBER,
    #         to=f"whatsapp:+{NUMERO_ADMIN}",
    #         body=mensaje
    #     )
    #     logger.info("Notificación texto enviada al admin por Twilio")
    # except Exception as e:
    #     logger.error(f"Error notificando admin por Twilio: {e}")

def notificar_admin_imagen(image_id, numero_cliente, phone_number_id):
    """Reenvía imagen (comprobante de pago) al admin vía Meta API."""
    try:
        url_info = f"https://graph.facebook.com/v19.0/{image_id}"
        headers = {"Authorization": f"Bearer {os.getenv('WHATSAPP_ACCESS_TOKEN')}"}
        response = req.get(url_info, headers=headers)
        image_url = response.json().get("url")

        if not image_url:
            logger.error("No se pudo obtener la URL de la imagen")
            return

        cliente_display = formatear_identificador_cliente(numero_cliente)

        url_send = f"https://graph.facebook.com/v19.0/{phone_number_id}/messages"
        payload = {
            "messaging_product": "whatsapp",
            "to": NUMERO_ADMIN,
            "type": "image",
            "image": {
                "link": image_url,
                "caption": f"📸 *Comprobante de pago recibido*\n👤 Cliente: {cliente_display}"
            }
        }
        req.post(
            url_send,
            headers={
                "Authorization": f"Bearer {os.getenv('WHATSAPP_ACCESS_TOKEN')}",
                "Content-Type": "application/json"
            },
            json=payload
        )
        logger.info(f"Comprobante reenviado al admin desde cliente {numero_cliente}")
    except Exception as e:
        logger.error(f"Error reenviando comprobante: {e}")

# ─────────────────────────────────────────
# PROCESAMIENTO DE EVENTOS ESPECIALES
# ─────────────────────────────────────────
def procesar_respuesta(respuesta_texto, identificador, phone_number_id):
    """
    Procesa la respuesta del bot buscando eventos especiales:
    - Solicitudes de atención humana
    - Pedidos confirmados
    Retorna la respuesta limpia sin las líneas internas.
    """
    cliente_display = formatear_identificador_cliente(identificador)

    # ── Detectar solicitud de atención humana ──
    if "ATENCION_HUMANA_SOLICITADA" in respuesta_texto:
        respuesta_texto = respuesta_texto.replace("ATENCION_HUMANA_SOLICITADA", "").strip()
        mensaje_admin = (
            f"🙋 *Atención humana solicitada*\n\n"
            f"📱 Cliente: {cliente_display}\n\n"
            f"_El cliente quiere hablar con una persona del equipo._"
        )
        notificar_admin_texto(mensaje_admin, phone_number_id)
        logger.info(f"Atención humana solicitada por {identificador}")

    # ── Detectar pedido confirmado ──
    if "PEDIDO_CONFIRMADO|" in respuesta_texto:
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
                        f"📱 *Cliente:* {cliente_display}\n\n"
                        f"_Responde directamente al cliente para coordinar._"
                    )
                    notificar_admin_texto(mensaje_admin, phone_number_id)
                    logger.info(f"Pedido confirmado — notificación enviada al admin")
                except Exception as e:
                    logger.error(f"Error procesando pedido confirmado: {e}")
            else:
                respuesta_limpia.append(linea)
        respuesta_texto = "\n".join(respuesta_limpia).strip()

    return respuesta_texto

# ─────────────────────────────────────────
# FUNCIÓN CENTRAL: procesa cualquier mensaje
# ─────────────────────────────────────────
def procesar_mensaje(identificador, mensaje_usuario, es_admin, phone_number_id):
    global bot_activo

    # ── Comandos del administrador ──
    if es_admin:
        cmd = mensaje_usuario.lower().strip()
        if cmd.startswith("pausar "):
            id_pausar = resolver_identificador(mensaje_usuario[7:])
            pausados.add(id_pausar)
            return f"✅ Bot pausado para {id_pausar}"
        elif cmd.startswith("activar "):
            id_activar = resolver_identificador(mensaje_usuario[8:])
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
            id_borrar = resolver_identificador(mensaje_usuario[7:])
            clave_borrar = f"{phone_number_id}:{id_borrar}"
            if clave_borrar in conversaciones:
                del conversaciones[clave_borrar]
                return f"🗑️ Historial borrado para {id_borrar}"
            return f"No encontré conversación activa para {id_borrar}"
        elif cmd == "borrar todo":
            conversaciones.clear()
            return "🗑️ Todos los historiales borrados."
        elif cmd == "ayuda":
            return (
                "📖 Comandos disponibles:\n\n"
                "• *pausar [número o @username]* — pausa el bot para ese usuario\n"
                "• *activar [número o @username]* — reactiva el bot para ese usuario\n"
                "• *pausar todo* — pausa el bot para todos\n"
                "• *activar todo* — reactiva el bot para todos\n"
                "• *lista* — muestra usuarios pausados\n"
                "• *estado* — muestra el estado actual del bot\n"
                "• *borrar [número o @username]* — borra el historial de un usuario\n"
                "• *borrar todo* — borra todos los historiales\n"
                "• *ayuda* — muestra este menú\n\n"
                "_Puedes usar el número, el username (sin @) o el código completo, según lo que te haya mostrado la notificación._"
            )

    # ── Verificaciones antes de responder ──
    if not bot_activo:
        return None
    if identificador in pausados:
        return None

    # ── Lógica normal del chatbot ──
    # La conversación se identifica por negocio + cliente, para que el mismo
    # cliente pueda hablar con dos negocios distintos sin mezclar historiales.
    clave_conversacion = f"{phone_number_id}:{identificador}"

    if clave_conversacion not in conversaciones:
        conversaciones[clave_conversacion] = [crear_system_message(phone_number_id)]
    else:
        conversaciones[clave_conversacion][0] = crear_system_message(phone_number_id)

    conversaciones[clave_conversacion].append({"role": "user", "content": mensaje_usuario})

    if len(conversaciones[clave_conversacion]) > MAX_MENSAJES + 1:
        conversaciones[clave_conversacion] = (
            [conversaciones[clave_conversacion][0]] +
            conversaciones[clave_conversacion][-MAX_MENSAJES:]
        )

    try:
        respuesta = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=conversaciones[clave_conversacion]
        )
        respuesta_texto = respuesta.choices[0].message.content

        respuesta_texto = procesar_respuesta(respuesta_texto, identificador, phone_number_id)

        conversaciones[clave_conversacion].append({"role": "assistant", "content": respuesta_texto})
        logger.info(f"Respuesta generada para {identificador[:8]}... (negocio {phone_number_id})")
        return respuesta_texto

    except Exception as e:
        logger.error(f"Error OpenAI: {e}")
        conversaciones[clave_conversacion].pop()
        return "Lo siento, tuve un problema técnico. Por favor intenta de nuevo en un momento. 🙏"

# ─────────────────────────────────────────
# RUTA DE SALUD (para UptimeRobot)
# ─────────────────────────────────────────
@app.route("/", methods=["GET"])
def home():
    return "Bot activo", 200

# ─────────────────────────────────────────
# WHATSAPP VÍA TWILIO (sandbox/producción)
# ── Comentado: ya no se usa mientras el bot corre 100% sobre la Cloud API
#    de Meta. Se reactivará cuando se implemente el modelo Tech Provider/ISV
#    de Twilio para nuevos clientes. ──
# ─────────────────────────────────────────
# @app.route("/whatsapp", methods=["POST"])
# def whatsapp_reply():
#     numero = request.form.get("From")
#     mensaje_usuario = request.form.get("Body")
#
#     if not numero or not mensaje_usuario:
#         return str(MessagingResponse())
#
#     logger.info(f"Twilio - mensaje de {numero}: {mensaje_usuario[:50]}")
#
#     es_admin = (numero == f"whatsapp:+{NUMERO_ADMIN}")
#     respuesta_texto = procesar_mensaje(numero, mensaje_usuario, es_admin, None)
#
#     resp = MessagingResponse()
#     if respuesta_texto:
#         resp.message(respuesta_texto)
#     return str(resp)

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

@app.route('/privacy.html')
def privacy():
    return send_from_directory('.', 'privacy.html')

@app.route("/whatsapp_meta", methods=["POST"])
def whatsapp_meta_reply():
    datos = request.get_json()

    try:
        entrada = datos["entry"][0]["changes"][0]["value"]
        mensajes = entrada.get("messages", [])
        if not mensajes:
            return "OK", 200

        # ── Clave del enrutamiento multi-negocio: el número AL QUE escribieron ──
        phone_number_id = entrada["metadata"]["phone_number_id"]

        mensaje_evento = mensajes[0]

        # Soporta tanto números de teléfono normales ("from") como
        # identificadores BSUID de usuarios con username ("from_user_id")
        numero = mensaje_evento.get("from") or mensaje_evento.get("from_user_id")

        # ── Guarda la relación username ↔ identificador, si viene en el payload ──
        contactos = entrada.get("contacts", [])
        if contactos:
            username = contactos[0].get("username")
            registrar_username(numero, username)

        tipo = mensaje_evento.get("type", "text")

        # ── Manejo de imágenes (comprobantes de pago) ──
        if tipo == "image":
            image_id = mensaje_evento["image"]["id"]
            notificar_admin_imagen(image_id, numero, phone_number_id)
            enviar_mensaje_whatsapp(
                numero,
                "✅ ¡Recibimos tu comprobante de pago! Lo verificaremos y coordinaremos tu pedido pronto. ¡Gracias! 🙏",
                phone_number_id
            )
            return "OK", 200

        if tipo != "text":
            enviar_mensaje_whatsapp(
                numero,
                "Por el momento solo puedo responder mensajes de texto e imágenes. 😊",
                phone_number_id
            )
            return "OK", 200

        mensaje_usuario = mensaje_evento["text"]["body"]

    except (KeyError, IndexError) as e:
        logger.error(f"Error extrayendo mensaje: {e}")
        return "OK", 200

    logger.info(f"Meta - mensaje de {numero} a negocio {phone_number_id}: {mensaje_usuario[:50]}")

    es_admin = (numero == NUMERO_ADMIN)
    respuesta_texto = procesar_mensaje(numero, mensaje_usuario, es_admin, phone_number_id)

    if respuesta_texto:
        enviar_mensaje_whatsapp(numero, respuesta_texto, phone_number_id)

    return "OK", 200

def enviar_mensaje_whatsapp(numero_destino, texto, phone_number_id):
    """Envía un mensaje de texto vía Meta WhatsApp API, DESDE el mismo
    número de negocio (phone_number_id) donde llegó el mensaje original.

    Detecta automáticamente si numero_destino es un número de teléfono
    normal o un BSUID (identificador de usuario con username activado,
    formato "XX.numeros") y usa el campo correcto ("to" o "recipient").
    """
    url = f"https://graph.facebook.com/v19.0/{phone_number_id}/messages"
    headers = {
        "Authorization": f"Bearer {os.getenv('WHATSAPP_ACCESS_TOKEN')}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "type": "text",
        "text": {"body": texto}
    }

    if len(numero_destino) > 2 and numero_destino[2] == "." and numero_destino[:2].isalpha():
        payload["recipient"] = numero_destino
    else:
        payload["to"] = numero_destino

    response = req.post(url, headers=headers, json=payload)
    logger.info(f"Meta API response: {response.status_code}")

# ─────────────────────────────────────────
# INSTAGRAM VÍA META API (oficial)
# ─────────────────────────────────────────
INSTAGRAM_ACCOUNT_ID = "17841443710781118"  # chatbots.co

@app.route("/instagram", methods=["GET"])
def verificar_webhook_instagram():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    if mode == "subscribe" and token == VERIFY_TOKEN_META:
        logger.info("Webhook de Instagram verificado correctamente")
        return challenge, 200
    return "Token inválido", 403

@app.route("/instagram", methods=["POST"])
def instagram_reply():
    datos = request.get_json()

    try:
        entrada = datos["entry"][0]
        mensajeria = entrada.get("messaging", [])
        if not mensajeria:
            return "OK", 200

        evento = mensajeria[0]
        numero = evento["sender"]["id"]

        # Ignorar cualquier evento que no sea un mensaje real (echos,
        # confirmaciones de entrega/lectura, reacciones, etc.) — responder
        # a esos causa un bucle infinito de notificaciones.
        if "message" not in evento or evento["message"].get("is_echo"):
            return "OK", 200

        # Ignorar mensajes duplicados (Meta reintenta si tarda la respuesta)
        mid = evento["message"].get("mid")
        if mid:
            if mid in mensajes_procesados:
                logger.info(f"Mensaje duplicado ignorado: {mid}")
                return "OK", 200
            mensajes_procesados.add(mid)
            if len(mensajes_procesados) > MAX_MENSAJES_PROCESADOS:
                mensajes_procesados.pop()

        mensaje_usuario = evento["message"].get("text")
        if not mensaje_usuario:
            # Es un mensaje real (imagen, sticker, etc.) pero sin texto —
            # aquí sí responde, una sola vez, porque es un mensaje nuevo.
            enviar_mensaje_instagram(
                numero,
                "Por el momento solo puedo responder mensajes de texto. 😊"
            )
            return "OK", 200

        mensaje_usuario = evento["message"].get("text")
        if not mensaje_usuario:
            # Es un mensaje real (imagen, sticker, etc.) pero sin texto —
            # aquí sí responde, una sola vez, porque es un mensaje nuevo.
            enviar_mensaje_instagram(
                numero,
                "Por el momento solo puedo responder mensajes de texto. 😊"
            )
            return "OK", 200

    except (KeyError, IndexError) as e:
        logger.error(f"Error extrayendo mensaje de Instagram: {e}")
        return "OK", 200

    logger.info(f"Instagram - mensaje de {numero}: {mensaje_usuario[:50]}")

    es_admin = (numero == NUMERO_ADMIN)
    respuesta_texto = procesar_mensaje(numero, mensaje_usuario, es_admin, INSTAGRAM_ACCOUNT_ID)

    if respuesta_texto:
        enviar_mensaje_instagram(numero, respuesta_texto)

    return "OK", 200

def enviar_mensaje_instagram(destinatario_id, texto):
    """Envía un mensaje de texto vía Instagram API (con Instagram Login)."""
    url = f"https://graph.instagram.com/v21.0/{INSTAGRAM_ACCOUNT_ID}/messages"
    headers = {
        "Authorization": f"Bearer {os.getenv('INSTAGRAM_ACCESS_TOKEN')}",
        "Content-Type": "application/json"
    }
    payload = {
        "recipient": {"id": destinatario_id},
        "message": {"text": texto}
    }
    response = req.post(url, headers=headers, json=payload)
    logger.info(f"Instagram API response: {response.status_code}")

if __name__ == "__main__":
    app.run(port=5000)
