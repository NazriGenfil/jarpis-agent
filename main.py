import os
import asyncio
import json
import websockets
import aiohttp
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes

# --- LANGCHAIN & LANGGRAPH IMPORTS ---
from langchain_ollama import ChatOllama
from langchain_core.messages import SystemMessage, HumanMessage
from langgraph.graph import START, StateGraph, MessagesState
from langchain_core.tools import tool
from langgraph.prebuilt import ToolNode, tools_condition

# IMPORT BARU: Versi Async dari SQLite
import aiosqlite
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

# Load environment variables
load_dotenv()

# ================= CONFIGURATION =================
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID = str(os.getenv('TELEGRAM_CHAT_ID'))
HA_URL = os.getenv('HA_URL') 
HA_REST_URL = os.getenv('HA_REST_URL') 
HA_TOKEN = os.getenv('HA_TOKEN')
OLLAMA_BASE_URL = os.getenv('OLLAMA_URL').replace('/api/generate', '') 
MODEL_NAME = os.getenv('MODEL_NAME')

if not all([TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, HA_TOKEN, HA_REST_URL]):
    raise ValueError("Bro, file .env lu belum lengkap tuh. Cek lagi!")

headers_ha = {
    "Authorization": f"Bearer {HA_TOKEN}",
    "Content-Type": "application/json",
}

# ================= THE TOOLS =================
@tool
async def get_available_devices() -> str:
    """Narik SEMUA daftar entity_id (lampu, AC, switch) dari Home Assistant biar Jarvis ga halu."""
    print("🔧 [TOOL] Jarvis lagi nge-scan seisi rumah...")
    async with aiohttp.ClientSession() as session:
        async with session.get(f"{HA_REST_URL}/states", headers=headers_ha) as response:
            if response.status != 200:
                return "Gagal ngambil data dari HA bos."
            
            states = await response.json()
            available_items = []
            for item in states:
                entity = item['entity_id']
                if entity.startswith(('light.', 'switch.', 'climate.')):
                    nama = item.get('attributes', {}).get('friendly_name', entity)
                    status = item.get('state', 'unknown')
                    available_items.append(f"- {nama} (ID: {entity}) -> Status: {status}")
            
            return "Daftar perangkat di rumah:\n" + "\n".join(available_items)

@tool
async def control_device(entity_id: str, action: str) -> str:
    """Ngeksekusi perintah (turn_on/turn_off) ke perangkat yang valid."""
    print(f"🔧 [TOOL] Jarvis mau ngeksekusi {action} ke {entity_id}...")
    domain = entity_id.split('.')[0]
    url = f"{HA_REST_URL}/services/{domain}/{action}"
    payload = {"entity_id": entity_id}
    
    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers_ha, json=payload) as response:
            if response.status == 200:
                return f"Berhasil bos! {entity_id} udah di-{action}."
            else:
                return f"Gagal eksekusi nih bos. Status code: {response.status}"

jarvis_tools = [get_available_devices, control_device]

# ================= THE BRAIN =================
llm = ChatOllama(model=MODEL_NAME, base_url=OLLAMA_BASE_URL).bind_tools(jarvis_tools)

async def call_model(state: MessagesState):
    system_prompt = (
        "Lu adalah Jarvis, asisten AI cerdas untuk home automation rumah Bos Nazri. "
        "Jawab santai. Kalau bos minta kontrol alat tapi ID-nya ga jelas, JANGAN ASAL TEBAK. "
        "Gunakan tool 'get_available_devices' buat ngecek daftar alat yang valid dulu. "
        "Kalau lu udah tau entity_id yang bener, langsung pake tool 'control_device'."
    )
    messages_to_process = state["messages"][-10:]
    messages = [SystemMessage(content=system_prompt)] + messages_to_process
    response = await llm.ainvoke(messages)
    return {"messages": response}

workflow = StateGraph(MessagesState)
workflow.add_node("agent", call_model)
workflow.add_node("tools", ToolNode(jarvis_tools))
workflow.add_edge(START, "agent")
workflow.add_conditional_edges("agent", tools_condition)
workflow.add_edge("tools", "agent")

# Global variable buat nampung app yg udah dicompile nanti di fungsi main()
jarvis_app = None

# ================= JEMBATAN TELEGRAM <-> LANGGRAPH =================
async def think_and_speak(prompt, thread_id="jarvis_main_thread"):
    config = {"configurable": {"thread_id": thread_id}}
    inputs = {"messages": [HumanMessage(content=prompt)]}
    result = await jarvis_app.ainvoke(inputs, config=config)
    return result["messages"][-1].content

# ================= THE EARS =================
async def handle_telegram_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.message.chat_id) != TELEGRAM_CHAT_ID:
        return
    user_message = update.message.text
    print(f"Bos Nazri ngetik: {user_message}")
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action='typing')
    jarvis_reply = await think_and_speak(user_message)
    await context.bot.send_message(chat_id=update.effective_chat.id, text=f"🤖 [JARVIS]\n{jarvis_reply}")

# ================= THE EYES =================
async def monitor_home_assistant(application):
    await asyncio.sleep(3) 
    try:
        async with websockets.connect(HA_URL) as websocket:
            await websocket.recv()
            await websocket.send(json.dumps({"type": "auth", "access_token": HA_TOKEN}))
            await websocket.recv()
            
            print("Jarvis: Mata websocket nyala...")
            
            prompt0 = "Sistem Jarvis (Versi AsyncSqliteSaver) sudah berhasil nyala. Bikin sapaan singkat."
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
                        prompt = "Lampu Bardi kamar dinyalain manual. Bikin notif singkat gaya Jarvis."
                        jarvis_response = await think_and_speak(prompt, thread_id="system_alerts")
                        await application.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=f"💡 [JARVIS]\n{jarvis_response}")
                        await asyncio.sleep(5)
    except Exception as e:
        print(f"WebSocket HA putus bro: {e}. Entar nyambung lagi...")

# ================= MAIN LOOP =================
async def main():
    global jarvis_app
    
    # KUNCI FIX NYA DI SINI: Pake AsyncSqliteSaver buat ngelola database pake aiosqlite
    async with AsyncSqliteSaver.from_conn_string("/app/data/jarvis_memory.sqlite") as memory_saver:
        # Compile graf di dalem context manager ini
        jarvis_app = workflow.compile(checkpointer=memory_saver)
        
        application = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
        application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_telegram_chat))
        
        await application.initialize()
        await application.start()
        await application.updater.start_polling()
        
        print("Jarvis: Siap diperintah! Ingatan Permanen (Async) aktif, Bos!")

        await monitor_home_assistant(application)
        
        while True:
            await asyncio.sleep(3600)

if __name__ == "__main__":
    asyncio.run(main())