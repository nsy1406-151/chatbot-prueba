from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from openai import OpenAI
from dotenv import load_dotenv
import os
import requests as req

load_dotenv()
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
app = Flask(__name__)

# Cargar info del negocio
with open("negocio.txt", "r", encoding="utf-8") as archivo:
    info_negocio = archivo.read()

system_message = {
    "role": "system",
    "content": f"""Eres el asistente virtual de Solo Medias.
    Responde SOLO basándote en la siguiente información.
    Si te preguntan algo que no está aquí, dilo amablemente y sugiere contactar directamente al negocio.

    INFORMACIÓN DEL NEGOCIO:
    {info_negocio}
    """
}

# Historial por usuario (compartido entre canales)
conversaciones = {}

# Números pausados (el bot no responde a estos)
pausados = set()

# Número admin de tu mamá (sin el +, ej: 573001234567)
NUMERO_ADMIN = os.getenv("NUMERO_ADMIN", "57XXXXXXXXXX")

# Token de verificación para el webhook de Meta
VERIFY_TOKEN_META = "botdemo2026"


# ─────────────────────────────────────────
# FUNCIÓN CENTRAL: procesa cualquier mensaje
# ─────────────────────────────────────────
def procesar_mensaje(identificador, mensaje_usuario, es_admin):
    """
    Lógica central compartida entre WhatsApp (Twilio), WhatsApp (Meta) e Instagram.
    Recibe el identificador del usuario, el mensaje, y si es admin.
    Retorna el texto de respuesta, o None si el bot no debe responder.
    """

    # Comandos especiales del admin
    if es_admin:
        if mensaje_usuario.lower().startswith("pausar "):
            id_pausar = mensaje_usuario[7:].strip()
            pausados.add(id_pausar)
            return f"Bot pausado para {id_pausar}"
        elif mensaje_usuario.lower().startswith("activar "):
            id_activar = mensaje_usuario[8:].strip()
            pausados.discard(id_activar)
            return f"Bot reactivado para {id_activar}"
        elif mensaje_usuario.lower() == "lista":
            if pausados:
                return "Conversaciones pausadas:\n" + "\n".join(pausados)
            return "No hay conversaciones pausadas."

    # Si el número está pausado, no responder
    if identificador in pausados:
        return None

    # Lógica normal del chatbot
    if identificador not in conversaciones:
        conversaciones[identificador] = [system_message]

    conversaciones[identificador].append({"role": "user", "content": mensaje_usuario})

    respuesta = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=conversaciones[identificador]
    )

    respuesta_texto = respuesta.choices[0].message.content
    conversaciones[identificador].append({"role": "assistant", "content": respuesta_texto})

    return respuesta_texto


# ─────────────────────────────────────────
# RUTA DE SALUD (para UptimeRobot)
# ─────────────────────────────────────────
@app.route("/", methods=["GET"])
def home():
    return "Bot activo", 200


# ─────────────────────────────────────────
# WHATSAPP VÍA TWILIO (sandbox actual)
# ─────────────────────────────────────────
@app.route("/whatsapp", methods=["POST"])
def whatsapp_reply():
    numero = request.form.get("From")  # ej: "whatsapp:+57301..."
    mensaje_usuario = request.form.get("Body")
    es_admin = (numero == f"whatsapp:+{NUMERO_ADMIN}")

    respuesta_texto = procesar_mensaje(numero, mensaje_usuario, es_admin)

    resp = MessagingResponse()
    if respuesta_texto:
        resp.message(respuesta_texto)
    return str(resp)


# ─────────────────────────────────────────
# WHATSAPP VÍA META API (nuevo, oficial)
# ─────────────────────────────────────────
@app.route("/whatsapp_meta", methods=["GET"])
def verificar_webhook_meta():
    """Meta llama a esta ruta para verificar el webhook al configurarlo."""
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    if mode == "subscribe" and token == VERIFY_TOKEN_META:
        return challenge, 200
    return "Token inválido", 403


@app.route("/whatsapp_meta", methods=["POST"])
def whatsapp_meta_reply():
    """Recibe mensajes reales de WhatsApp vía Meta API."""
    datos = request.get_json()

    try:
        mensaje_evento = datos["entry"][0]["changes"][0]["value"]["messages"][0]
        numero = mensaje_evento["from"]  # ej: "573001234567"
        mensaje_usuario = mensaje_evento["text"]["body"]
    except (KeyError, IndexError):
        return "OK", 200  # ignora eventos que no son mensajes de texto

    es_admin = (numero == NUMERO_ADMIN)
    respuesta_texto = procesar_mensaje(numero, mensaje_usuario, es_admin)

    if respuesta_texto:
        enviar_mensaje_whatsapp(numero, respuesta_texto)

    return "OK", 200


def enviar_mensaje_whatsapp(numero_destino, texto):
    """Envía un mensaje de texto via Meta WhatsApp API."""
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
    req.post(url, headers=headers, json=payload)


if __name__ == "__main__":
    app.run(port=5000)
