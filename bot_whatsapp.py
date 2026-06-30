from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from openai import OpenAI
from dotenv import load_dotenv
import os

load_dotenv()
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
app = Flask(__name__)

with open("negocio.txt", "r", encoding="utf-8") as archivo:
    info_negocio = archivo.read()

system_message = {
    "role": "system",
    "content": f"""Eres el asistente virtual de este negocio. 
    Responde SOLO basándote en la siguiente información.
    Si te preguntan algo que no está aquí, dilo amablemente.

    INFORMACIÓN DEL NEGOCIO:
    {info_negocio}
    """
}

# Diccionario: cada número de teléfono tiene su propio historial
conversaciones = {}

VERIFY_TOKEN_META = "solomedias2026"  # el mismo que pondrás en Meta

@app.route("/whatsapp_meta", methods=["GET"])
def verificar_webhook_meta():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    
    if mode == "subscribe" and token == VERIFY_TOKEN_META:
        return challenge, 200
    return "Token inválido", 403

@app.route("/whatsapp_meta", methods=["POST"])
def whatsapp_meta_reply():
    datos = request.get_json()
    
    try:
        mensaje_evento = datos["entry"][0]["changes"][0]["value"]["messages"][0]
        numero = mensaje_evento["from"]
        mensaje_usuario = mensaje_evento["text"]["body"]
    except (KeyError, IndexError):
        return "OK", 200
    
    es_admin = (numero == "57XXXXXXXXXX")  # tu número de mamá sin el +
    
    respuesta_texto = procesar_mensaje(numero, mensaje_usuario, es_admin, canal="whatsapp_meta")
    
    if respuesta_texto:
        enviar_mensaje_whatsapp(numero, respuesta_texto)
    
    return "OK", 200


def enviar_mensaje_whatsapp(numero_destino, texto):
    import requests
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
    requests.post(url, headers=headers, json=payload)


@app.route("/", methods=["GET"])
def home():
    return "Bot activo", 200

@app.route("/whatsapp", methods=["POST"])
def whatsapp_reply():
    numero = request.form.get("From")  # ej: "whatsapp:+57301..."
    mensaje_usuario = request.form.get("Body")

    # Si es la primera vez que escribe este número, le creamos historial nuevo
    if numero not in conversaciones:
        conversaciones[numero] = [system_message]

    conversaciones[numero].append({"role": "user", "content": mensaje_usuario})

    respuesta = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=conversaciones[numero]
    )

    respuesta_texto = respuesta.choices[0].message.content
    conversaciones[numero].append({"role": "assistant", "content": respuesta_texto})

    resp = MessagingResponse()
    resp.message(respuesta_texto)
    return str(resp)

if __name__ == "__main__":
    app.run(port=5000)
