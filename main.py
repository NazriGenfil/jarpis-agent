import os
import asyncio
import json
import websockets
import aiohttp
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes

# Load environment variables dari file .env
load_dotenv()

# ================= CONFIGURATION =================
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID = str(os.getenv('TELEGRAM_CHAT_ID')) # Pastikan string buat filter
HA_URL = os.getenv('HA_URL')
HA_TOKEN = os.getenv('HA_TOKEN')
OLLAMA_URL = os.getenv('OLLAMA_URL')
MODEL_NAME = os.getenv('MODEL_NAME')

if not all([TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, HA_TOKEN]):
    raise ValueError("Bro, file .env lu belum lengkap tuh. Cek lagi!")

# ================= THE BRAIN (OLLAMA) =================
async def think_and_speak(prompt, system_context="Lu adalah Jarvis, asisten AI cerdas untuk home automation. Jawab dengan gaya santai tapi sopan ke master lu."):
    async with aiohttp.ClientSession() as session:
        payload = {
            "model": MODEL_NAME,
            "prompt": prompt,
            "system": system_context,
            "stream": False
        }
        async with session.post(OLLAMA_URL, json=payload) as response:
            result = await response.json()
            return result['response']

# ================= THE EARS (TELEGRAM LISTENER) =================
async def handle_telegram_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Keamanan lapis pertama: Jarvis cuma mau ngobrol sama lu doang
    if str(update.message.chat_id) != TELEGRAM_CHAT_ID:
        return

    user_message = update.message.text
    print(f"Master Nazri bilang: {user_message}")

    # Kasih tau kalau Jarvis lagi mikir (biar lu ga nungguin kosong)
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action='typing')

    # Lempar chat lu ke Ollama
    jarvis_reply = await think_and_speak(f"Master berkata: {user_message}. Berikan balasan yang sesuai.")
    
    # Kirim balasan Ollama ke Telegram lu
    await context.bot.send_message(chat_id=update.effective_chat.id, text=f"🤖 [JARVIS]\n{jarvis_reply}")

# ================= THE EYES (HA WEBSOCKET) =================
async def monitor_home_assistant(application):
    # Biar ga error nunggu Telegram siap
    await asyncio.sleep(3) 
    
    try:
        async with websockets.connect(HA_URL) as websocket:
            await websocket.recv()
            await websocket.send(json.dumps({"type": "auth", "access_token": HA_TOKEN}))
            await websocket.recv()
            
            print("Jarvis: Mata nyala, siap melototin Home Assistant...")
            prompt0 = "Sistem jarvis sudah berhasil nyala. kasih tau kalau lu sudah menyala dan siap beroprasi. Bikin sapaan singkat 1 kalimat gaya Jarvis. jangan panggil 'master' panggil 'sir/bos'"
            jarvis_response0 = await think_and_speak(prompt0)
            await application.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=f"💡 [JARVIS]\n{jarvis_response0}")
            
            subscribe_msg = {"id": 1, "type": "subscribe_events", "event_type": "state_changed"}
            await websocket.send(json.dumps(subscribe_msg))

            while True:
                message = await websocket.recv()
                event_data = json.loads(message)
                
                # Logic proaktif (Mata) tetap jalan di background
                if event_data.get('type') == 'event':
                    event = event_data['event']
                    entity_id = event['data']['entity_id']
                    
                    if entity_id == 'switch.obk8c428848_1' and event['data']['new_state']['state'] == 'on':
                        prompt = "Lampu kamar baru nyala nih. Bikin sapaan singkat 1 kalimat gaya Jarvis."
                        jarvis_response = await think_and_speak(prompt)
                        await application.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=f"💡 [JARVIS]\n{jarvis_response}")
                        await asyncio.sleep(5)
    except Exception as e:
        print(f"WebSocket terputus: {e}. Coba reconnect nanti...")

# ================= MAIN LOOP =================
async def main():
    # Bikin aplikasi Telegram
    application = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    
    # Pasang Telinga (Listener text biasa)
    application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_telegram_chat))
    
    # Start bot Telegram secara async
    await application.initialize()
    await application.start()
    await application.updater.start_polling()
    
    print("Jarvis: Telinga udah online, coba chat gua di Telegram, Bro!")

    # Jalankan Mata (HA Websocket) berdampingan sama Telinga
    await monitor_home_assistant(application)
    
    # Biar script ga mati
    while True:
        await asyncio.sleep(3600)

if __name__ == "__main__":
    asyncio.run(main())