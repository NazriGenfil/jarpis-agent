import os
import asyncio
import json
import websockets
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes

# --- LANGCHAIN & LANGGRAPH IMPORTS ---
from langchain_ollama import ChatOllama
from langchain_core.messages import SystemMessage, HumanMessage, RemoveMessage
from langgraph.graph import START, StateGraph, MessagesState
from langgraph.checkpoint.memory import MemorySaver

# Load environment variables dari file .env
load_dotenv()

# ================= CONFIGURATION =================
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID = str(os.getenv('TELEGRAM_CHAT_ID'))
HA_URL = os.getenv('HA_URL')
HA_TOKEN = os.getenv('HA_TOKEN')
OLLAMA_BASE_URL = os.getenv('OLLAMA_URL').replace('/api/generate', '') # Langchain butuh base url aja
MODEL_NAME = os.getenv('MODEL_NAME')

if not all([TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, HA_TOKEN]):
    raise ValueError("Bro, file .env lu belum lengkap tuh. Cek lagi!")

# ================= THE BRAIN (LANGGRAPH + OLLAMA) =================
# 1. Setup LLM
llm = ChatOllama(model=MODEL_NAME, base_url=OLLAMA_BASE_URL)

# 2. Setup Node untuk LangGraph
async def call_model(state: MessagesState):
    # System Prompt (Long Term Memory bisa diselipkan di sini nanti)
    system_prompt = (
        "Lu adalah Jarvis, asisten AI cerdas untuk home automation. "
        "Jawab dengan gaya santai tapi sopan. Jangan sebut pengguna Master, gunakan kata sir/bos."
    )
    
    # Ambil pesan-pesan terakhir, batasi misal 10 pesan terakhir biar VRAM aman (Sliding Window)
    # LangGraph menyimpan semua history, kita filter sebelum masuk ke model
    messages_to_process = state["messages"][-10:]
    
    # Gabung System Prompt + History Chat
    messages = [SystemMessage(content=system_prompt)] + messages_to_process
    
    # Invoke model Ollama
    response = await llm.ainvoke(messages)
    return {"messages": response}

# 3. Setup Graph Workflow
workflow = StateGraph(MessagesState)
workflow.add_node("agent", call_model)
workflow.add_edge(START, "agent")

# 4. Setup Checkpointer (Buat nyimpen ingatan per sesi/thread)
memory_saver = MemorySaver()
jarvis_app = workflow.compile(checkpointer=memory_saver)

# 5. Fungsi Jembatan buat Telegram/HA
async def think_and_speak(prompt, thread_id="jarvis_main_thread"):
    # Config ini yang bikin Jarvis inget, thread_id diibaratkan "ID Ruang Chat"
    config = {"configurable": {"thread_id": thread_id}}
    
    inputs = {"messages": [HumanMessage(content=prompt)]}
    
    # Jalankan graf secara asynchronous
    result = await jarvis_app.ainvoke(inputs, config=config)
    
    # Ambil pesan terakhir dari AI
    return result["messages"][-1].content


# ================= THE EARS (TELEGRAM LISTENER) =================
async def handle_telegram_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.message.chat_id) != TELEGRAM_CHAT_ID:
        return

    user_message = update.message.text
    print(f"Bos Nazri bilang: {user_message}")

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action='typing')

    # Lempar ke Graf Jarvis (otomatis masuk ke memory 'jarvis_main_thread')
    jarvis_reply = await think_and_speak(user_message)
    
    await context.bot.send_message(chat_id=update.effective_chat.id, text=f"🤖 [JARVIS]\n{jarvis_reply}")

# ================= THE EYES (HA WEBSOCKET) =================
async def monitor_home_assistant(application):
    await asyncio.sleep(3) 
    try:
        async with websockets.connect(HA_URL) as websocket:
            await websocket.recv()
            await websocket.send(json.dumps({"type": "auth", "access_token": HA_TOKEN}))
            await websocket.recv()
            
            print("Jarvis: Mata nyala, siap melototin Home Assistant...")
            
            # Sapaan awal pas server nyala (pake thread terpisah biar ga ngotorin chat history utama)
            prompt0 = "Sistem jarvis sudah berhasil nyala. kasih tau kalau lu sudah menyala dan siap beroprasi. Bikin sapaan singkat 1 kalimat gaya Jarvis."
            jarvis_response0 = await think_and_speak(prompt0, thread_id="system_alerts")
            await application.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=f"💡 [JARVIS]\n{jarvis_response0}")
            
            subscribe_msg = {"id": 1, "type": "subscribe_events", "event_type": "state_changed"}
            await websocket.send(json.dumps(subscribe_msg))

            while True:
                message = await websocket.recv()
                event_data = json.loads(message)
                
                if event_data.get('type') == 'event':
                    event = event_data['event']
                    entity_id = event['data']['entity_id']
                    
                    if entity_id == 'switch.obk8c428848_1' and event['data']['new_state']['state'] == 'on':
                        prompt = "Lampu kamar baru nyala nih. Bikin sapaan singkat 1 kalimat."
                        # Notifikasi otomatis juga pake thread terpisah biar ga ganggu ingatan ngobrol lu
                        jarvis_response = await think_and_speak(prompt, thread_id="system_alerts")
                        await application.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=f"💡 [JARVIS]\n{jarvis_response}")
                        await asyncio.sleep(5)
    except Exception as e:
        print(f"WebSocket terputus: {e}. Coba reconnect nanti...")

# ================= MAIN LOOP =================
async def main():
    application = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_telegram_chat))
    
    await application.initialize()
    await application.start()
    await application.updater.start_polling()
    
    print("Jarvis: Telinga & Memori LangGraph udah online, Bos!")

    await monitor_home_assistant(application)
    
    while True:
        await asyncio.sleep(3600)

if __name__ == "__main__":
    asyncio.run(main())