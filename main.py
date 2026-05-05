import os
import asyncio
import json
import websockets
import aiohttp
from dotenv import load_dotenv
from telegram import Bot

# Load environment variables dari file .env
load_dotenv()

# ================= CONFIGURATION =================
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')
HA_URL = os.getenv('HA_URL')
HA_TOKEN = os.getenv('HA_TOKEN')
OLLAMA_URL = os.getenv('OLLAMA_URL')
MODEL_NAME = os.getenv('MODEL_NAME')

# Pengecekan biar lu nggak lupa ngisi .env
if not all([TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, HA_TOKEN]):
    raise ValueError("Bro, file .env lu belum lengkap tuh. Cek lagi!")

bot = Bot(token=TELEGRAM_BOT_TOKEN)

# ================= THE BRAIN (OLLAMA) =================
async def think_and_speak(prompt, system_context="Lu adalah Jarvis, asisten AI cerdas untuk home automation."):
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

# ================= THE EYES (HA WEBSOCKET - ACTIVE LISTENER) =================
async def monitor_home_assistant():
    async with websockets.connect(HA_URL) as websocket:
        # Auth ke Home Assistant
        await websocket.recv()
        await websocket.send(json.dumps({"type": "auth", "access_token": HA_TOKEN}))
        await websocket.recv()
        
        print("Jarvis: Mata nyala, siap melototin Home Assistant...")
        
        # Subscribe ke event
        subscribe_msg = {"id": 1, "type": "subscribe_events", "event_type": "state_changed"}
        await websocket.send(json.dumps(subscribe_msg))

        while True:
            message = await websocket.recv()
            event_data = json.loads(message)
            
            # Logic proaktif lu taruh di sini
            if event_data.get('type') == 'event':
                event = event_data['event']
                entity_id = event['data']['entity_id']
                
                # Skenario dummy buat test
                if entity_id == 'switch.obk8c428848_1' and event['data']['new_state']['state'] == 'on':
                    prompt = "Lampu kamar baru nyala nih. Bikin sapaan singkat 1 kalimat gaya Jarvis."
                    jarvis_response = await think_and_speak(prompt)
                    await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=f"💡 [JARVIS]\n{jarvis_response}")
                    await asyncio.sleep(5)

# ================= MAIN LOOP =================
async def main():
    await asyncio.gather(
        monitor_home_assistant()
    )

if __name__ == "__main__":
    asyncio.run(main())