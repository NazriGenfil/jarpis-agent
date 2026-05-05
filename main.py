import os
import asyncio
import json
import websockets
import aiohttp
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes

# --- LANGCHAIN & LANGGRAPH IMPORTS ---
from langchain_ollama import ChatOllama, OllamaEmbeddings
from langchain_core.messages import SystemMessage, HumanMessage
from langgraph.graph import START, StateGraph, MessagesState
from langchain_core.tools import tool
from langgraph.prebuilt import ToolNode, tools_condition

# --- MEMORY IMPORTS ---
import aiosqlite
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langchain_chroma import Chroma
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

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

# ================= SETUP VECTOR DB (LONG TERM MEMORY) =================
# 1. Inisialisasi Pustakawan (Embedding Model)
embeddings = OllamaEmbeddings(
    model="nomic-embed-text", 
    base_url=OLLAMA_BASE_URL
)

# 2. Buka Gudang Arsip (ChromaDB)
vector_store = Chroma(
    collection_name="jarvis_long_term",
    embedding_function=embeddings,
    persist_directory="/app/vector_data"
)

# ================= THE TOOLS =================
@tool
async def get_available_devices() -> str:
    """Narik SEMUA daftar entity_id dari Home Assistant."""
    async with aiohttp.ClientSession() as session:
        async with session.get(f"{HA_REST_URL}/states", headers=headers_ha) as response:
            if response.status != 200: return "Gagal ngambil data dari HA bos."
            states = await response.json()
            available_items = [f"- {i.get('attributes', {}).get('friendly_name', i['entity_id'])} (ID: {i['entity_id']}) -> Status: {i.get('state', 'unknown')}" for i in states if i['entity_id'].startswith(('light.', 'switch.', 'climate.'))]
            return "Daftar perangkat di rumah:\n" + "\n".join(available_items)

@tool
async def control_device(entity_id: str, action: str) -> str:
    """Ngeksekusi perintah (turn_on/turn_off) ke perangkat."""
    domain = entity_id.split('.')[0]
    url = f"{HA_REST_URL}/services/{domain}/{action}"
    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers_ha, json={"entity_id": entity_id}) as response:
            return f"Berhasil bos! {entity_id} udah di-{action}." if response.status == 200 else f"Gagal eksekusi. Status code: {response.status}"

@tool
def simpen_ingatan_jangka_panjang(fakta: str) -> str:
    """
    GUNAKAN TOOL INI JIKA bos ngasih tau fakta penting, preferensi, atau informasi personal yang harus diingat selamanya.
    Contoh: "Nama pacar gua Lia", "Gua ga suka suhu AC di bawah 20", "Proyek gua namanya Heimdall".
    """
    print(f"🧠 [VECTOR DB] Menyimpan kenangan baru: {fakta}")
    vector_store.add_texts(texts=[fakta])
    return "Fakta berhasil disimpan ke memori jangka panjang."

@tool
def ingat_masa_lalu(pertanyaan: str) -> str:
    """
    GUNAKAN TOOL INI JIKA bos nanya sesuatu tentang masa lalu, fakta tentang dirinya, atau lu butuh konteks tambahan yang ga ada di chat history.
    Contoh pertanyaan bos: "Tadi nama pacar gua siapa ya?", "Lu inget ga proyek gua apa?".
    """
    print(f"🧠 [VECTOR DB] Mencari kenangan terkait: {pertanyaan}")
    hasil_pencarian = vector_store.similarity_search(pertanyaan, k=3) # Ambil 3 ingatan paling mirip
    
    if not hasil_pencarian:
        return "Tidak ada ingatan yang cocok di memori jangka panjang."
    
    ingatan = "\n".join([f"- {doc.page_content}" for doc in hasil_pencarian])
    return f"Hasil dari memori jangka panjang:\n{ingatan}"
# --- Tambahin variabel global biar tool bisa akses scheduler & bot ---
scheduler = None
telegram_app = None

@tool
def buat_pengingat_dinamis(pesan: str, menit_lagi: int) -> str:
    """
    GUNAKAN TOOL INI jika Bos minta diingetin sesuatu dalam waktu tertentu (misal: 30 menit lagi, 2 jam lagi).
    Contoh: 'Ingetin gua cek server 15 menit lagi ya'.
    """
    from datetime import datetime, timedelta
    
    run_time = datetime.now() + timedelta(minutes=menit_lagi)
    
    # Daftarin job baru ke scheduler (Sekali jalan / One-shot)
    prompt_rahasia = f"Ini adalah pengingat yang lu buat tadi: '{pesan}'. Sampaikan ke Bos dengan gaya asisten yang sigap."
    
    scheduler.add_job(
        proactive_reminder,
        'date',
        run_date=run_time,
        args=[telegram_app, prompt_rahasia]
    )
    
    return f"Siap Bos! Pengingat untuk '{pesan}' sudah gua set buat {menit_lagi} menit dari sekarang (sekitar jam {run_time.strftime('%H:%M')})."

# Update daftar tools lu
jarvis_tools = [get_available_devices, control_device, simpen_ingatan_jangka_panjang, ingat_masa_lalu, buat_pengingat_dinamis]

# ================= THE BRAIN =================
llm = ChatOllama(model=MODEL_NAME, base_url=OLLAMA_BASE_URL).bind_tools(jarvis_tools)

async def call_model(state: MessagesState):
    system_prompt = (
        "Lu adalah Jarvis, asisten AI cerdas untuk Bos Nazri. Jawab santai, cerdas, dan ala asisten pribadi yang loyal.\n\n"
        "KAPABILITAS LU SAAT INI (TOOLS):\n"
        "1. Memori: 'simpen_ingatan_jangka_panjang' & 'ingat_masa_lalu'.\n"
        "2. Smart Home: 'control_device' & 'get_available_devices'.\n"
        "3. Reminder Dinamis: Lu SEKARANG PUNYA tool 'buat_pengingat_dinamis'. Pake ini kalau bos minta diingetin sesuatu (misal: 'Vis, 10 menit lagi ingetin gua matiin kompor').\n"
        "ATURAN KESADARAN DIRI (SANGAT PENTING):\n"
        "Sebelum lu menjanjikan sesuatu ke bos (contoh: ngirim email, bikin alarm, bikin jadwal otomatis/cron job, ngakses kalender, dll), "
        "CEK DULU daftar tool yang lu punya di atas. Jika lu TIDAK PUNYA tool untuk melakukan tugas itu, JANGAN HALU atau berbohong.\n"
        "Lu harus jujur dan bilang: 'Bos, gua pengen banget bantu otomatisasi ini, tapi gua belum dikasih modul/tool-nya. "
        "Tolong update script gua dong Bos, buatin kodenya biar gua bisa ngelakuin itu!'"
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

jarvis_app = None

# ================= JEMBATAN TELEGRAM <-> LANGGRAPH =================
async def think_and_speak(prompt, thread_id="jarvis_main_thread"):
    config = {"configurable": {"thread_id": thread_id}}
    inputs = {"messages": [HumanMessage(content=prompt)]}
    result = await jarvis_app.ainvoke(inputs, config=config)
    return result["messages"][-1].content

# ================= THE EARS (TELEGRAM LISTENER) =================

# 1. Bikin fungsi background buat ngirim sinyal ngetik terus-terusan
async def keep_typing(chat_id, context):
    """Looping ngirim status 'typing' tiap 4 detik sampe di-cancel"""
    while True:
        try:
            await context.bot.send_chat_action(chat_id=chat_id, action='typing')
            await asyncio.sleep(4) # Kirim ulang sebelum limit 5 detiknya habis
        except asyncio.CancelledError:
            break # Berhenti kalau di-cancel

# 2. Update fungsi chat lu
async def handle_telegram_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.message.chat_id) != TELEGRAM_CHAT_ID: return
    
    user_message = update.message.text
    print(f"Bos ngetik: {user_message}")
    
    # Mulai looping "mengetik..." di background
    typing_task = asyncio.create_task(keep_typing(update.effective_chat.id, context))
    
    try:
        # Biarin Jarvis mikir (Bisa belasan detik)
        jarvis_reply = await think_and_speak(user_message)
        
        # Kirim balasan pas udah kelar
        await context.bot.send_message(chat_id=update.effective_chat.id, text=f"🤖 [JARVIS]\n{jarvis_reply}")
    finally:
        # PENTING: Matiin looping ngetiknya biar ga jalan terus selamanya!
        typing_task.cancel()

# ================= THE EYES =================
async def monitor_home_assistant(application):
    await asyncio.sleep(3) 
    try:
        async with websockets.connect(HA_URL) as websocket:
            await websocket.recv()
            await websocket.send(json.dumps({"type": "auth", "access_token": HA_TOKEN}))
            await websocket.recv()
            
            prompt0 = "Sistem Jarvis (Vector DB Active) berhasil nyala. Bikin sapaan singkat."
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
                        prompt = "Lampu Bardi kamar dinyalain manual. Bikin notif."
                        jarvis_response = await think_and_speak(prompt, thread_id="system_alerts")
                        await application.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=f"💡 [JARVIS]\n{jarvis_response}")
                        await asyncio.sleep(5)
    except Exception as e:
        print(f"WebSocket HA putus bro: {e}")

# ================= THE MOUTH (PROACTIVE SENDER & SCHEDULER) =================
async def proactive_reminder(application, context_prompt):
    """
    Fungsi rahasia buat mancing Jarvis mikir dan ngomong duluan.
    Kita pancing dia pakai prompt rahasia di background.
    """
    print(f"⏰ [SCHEDULER] Trigger aktif: {context_prompt}")
    
    # Kita suruh Jarvis mikir di thread khusus 'system_alerts' biar ga ngerusak konteks chat lu yang lagi jalan
    jarvis_response = await think_and_speak(context_prompt, thread_id="system_alerts")
    
    # Jarvis ngirim chat duluan ke bos
    await application.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=f"💡 [JARVIS]\n{jarvis_response}")

def setup_scheduler(application):
    # Set zona waktu ke Jakarta (WIB) biar akurat
    scheduler = AsyncIOScheduler(timezone="Asia/Jakarta")
    
    # --- JADWAL 1: PENGINGAT GYM ---
    # Trigger: Setiap Senin, Rabu, Jumat jam 17:00 WIB
    prompt_gym = "Bos Nazri, ini udah jam 5 sore. Sesuai jadwal, ini waktunya rutinitas angkat beban. Buat kalimat motivasi singkat gaya Jarvis buat ngingetin bos berangkat ke gym sekarang."
    scheduler.add_job(
        proactive_reminder, 
        CronTrigger(day_of_week='mon,wed,fri', hour=17, minute=0), 
        args=[application, prompt_gym]
    )

    # --- JADWAL 2: CONTOH LAPORAN SERVER MALAM (Opsional, buat bayangan lu) ---
    # Trigger: Setiap hari jam 22:00 WIB
    prompt_server = "Ini jam 10 malam. Kasih sapaan singkat bilang kalau lu lagi mulai patroli ngecek keamanan Elderwand dan Nginx Proxy Manager."
    scheduler.add_job(
        proactive_reminder,
        CronTrigger(hour=22, minute=0),
        args=[application, prompt_server]
    )

    scheduler.start()
    print("⏳ [SCHEDULER] Sistem Cron Job & Proactive Messaging aktif!")

# ================= MAIN LOOP =================
async def main():
    global jarvis_app
    
    async with AsyncSqliteSaver.from_conn_string("/app/data/jarvis_memory.sqlite") as memory_saver:
        jarvis_app = workflow.compile(checkpointer=memory_saver)
        
        application = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
        application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_telegram_chat))
        
        await application.initialize()
        await application.start()
        await application.updater.start_polling()
        
        setup_scheduler(application)
        
        print("Jarvis: Vector DB Aktif! Siap mengingat masa lalu, Bos!")
        await monitor_home_assistant(application)
        
        while True:
            await asyncio.sleep(3600)

if __name__ == "__main__":
    asyncio.run(main())