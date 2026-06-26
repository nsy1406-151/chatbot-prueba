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