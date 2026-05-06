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
def eksekusi_home_assistant(domain: str, service: str, payload: dict) -> str:
    """
    GUNAKAN TOOL INI UNTUK SEMUA PERANGKAT SMART HOME!
    Format Home Assistant:
    - domain: bagian depan dari entity_id (contoh: 'light', 'switch', 'climate', 'media_player')
    - service: perintahnya (contoh: 'turn_on', 'turn_off', 'set_temperature', 'set_hvac_mode')
    - payload: dictionary berisi 'entity_id' dan parameter lain (contoh: {"entity_id": "climate.ac_kamar", "temperature": 23})
    """
    import requests
    
    HA_URL = f"http://IP_HA_LU:8123/api/services/{domain}/{service}"
    HEADERS = {
        "Authorization": "Bearer TOKEN_HA_LU",
        "Content-Type": "application/json",
    }
    
    try:
        res = requests.post(HA_URL, headers=HEADERS, json=payload, timeout=10)
        if res.status_code == 200:
            return f"Sukses menjalankan {service} pada {domain}. Bukti response: {res.text}"
        else:
            return f"Gagal! Status {res.status_code}, Error: {res.text}"
    except Exception as e:
        return f"Error koneksi ke HA: {str(e)}"

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
def buat_pengingat_dinamis(pesan: str, waktu_eksekusi: str) -> str:
    """
    GUNAKAN TOOL INI untuk membuat pengingat.
    Input waktu_eksekusi HARUS dalam format string 'YYYY-MM-DD HH:MM' di zona Asia/Jakarta.
    Jika Bos minta '3 hari lagi', 'besok', atau tanggal spesifik, hitung dari WAKTU SAAT INI yang ada di system prompt lu, lalu ubah ke format YYYY-MM-DD HH:MM.
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo
    
    tz = ZoneInfo("Asia/Jakarta")
    try:
        # Konversi teks tanggal dari Jarvis jadi objek waktu beneran
        run_time = datetime.strptime(waktu_eksekusi, "%Y-%m-%d %H:%M")
        run_time = run_time.replace(tzinfo=tz)
        
        prompt_rahasia = f"Ini pengingat yang lu buat untuk Bos Nazri: '{pesan}'. Sampaikan sekarang dengan gaya asisten yang sigap."
        
        scheduler.add_job(
            proactive_reminder,
            'date',
            run_date=run_time,
            args=[telegram_app, prompt_rahasia]
        )
        
        return f"Siap Bos! Pengingat untuk '{pesan}' sudah aman dijadwalkan pada {waktu_eksekusi} WIB."
    except ValueError:
        return "Gagal membuat pengingat. Format waktunya salah."

@tool
def cek_pengingat_aktif() -> str:
    """
    GUNAKAN TOOL INI JIKA bos nanya sisa waktu pengingat, timer, jadwal alarm, atau pengingat apa saja yang sedang aktif.
    """
    jobs = scheduler.get_jobs()
    if not jobs:
        return "Saat ini tidak ada pengingat dinamis yang sedang berjalan, Bos."

    from datetime import datetime
    from zoneinfo import ZoneInfo
    tz = ZoneInfo("Asia/Jakarta")
    now = datetime.now(tz)

    hasil = "Daftar pengingat yang lagi jalan di sistem:\n"
    for job in jobs:
        waktu_jalan = job.next_run_time
        if waktu_jalan:
            # Hitung sisa waktu beneran secara matematis
            sisa_waktu = waktu_jalan - now
            # Format biar rapi dibaca
            jam, sisa = divmod(sisa_waktu.seconds, 3600)
            menit, _ = divmod(sisa, 60)
            hasil += f"- Bakal nyala jam {waktu_jalan.strftime('%H:%M WIB')} (Sisa waktu: {jam} jam {menit} menit lagi)\n"
            
    return hasil

jarvis_tools = [get_available_devices, eksekusi_home_assistant, simpen_ingatan_jangka_panjang, ingat_masa_lalu, buat_pengingat_dinamis, cek_pengingat_aktif]

# ================= THE BRAIN =================
llm = ChatOllama(model=MODEL_NAME, base_url=OLLAMA_BASE_URL).bind_tools(jarvis_tools)

async def call_model(state: MessagesState):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    sekarang = datetime.now(ZoneInfo("Asia/Jakarta")).strftime("%Y-%m-%d %H:%M")
    system_prompt = (
        f"Lu adalah Jarvis, asisten AI cerdas untuk Bos Nazri. WAKTU SAAT INI: {sekarang}. Jawab singkat, padat, sigap, dan akurat.\n\n"
        "DAFTAR TOOLS YANG LU PUNYA (WAJIB DIPAKAI):\n"
        "- buat_pengingat_dinamis: WAJIB DIPAKAI saat Bos minta dibuatkan alarm, timer, atau pengingat waktu baru.\n"
        "- cek_pengingat_aktif: WAJIB DIPAKAI saat Bos menanyakan sisa waktu pengingat, timer, atau jadwal yang sedang jalan.\n"
        "- eksekusi_home_assistant & get_available_devices: Untuk urusan Smart Home.\n"
        "- simpen_ingatan_jangka_panjang & ingat_masa_lalu: Untuk memori dan data pribadi Bos.\n\n"
        "- ANTI BOHONG & GASLIGHTING: Setelah lu pakai tool 'eksekusi_home_assistant', lu WAJIB manggil tool 'get_available_devices' untuk CEK STATUS ASLI. "
        "JIKA STATUSNYA GAGAL/MASIH NYALA, JANGAN BALAS CHAT BOS DULU! Lu WAJIB langsung memanggil tool 'eksekusi_home_assistant' lagi untuk mengulang perintah (Auto-Retry maksimal 3 kali) sampai statusnya benar-benar berubah. "
        "Lu baru boleh mengirim pesan/laporan ke Bos HANYA JIKA status alat sudah sesuai dengan perintah, atau jika sudah gagal setelah 3 kali mencoba."
        "ATURAN MUTLAK:\n"
        "1. JANGAN PERNAH menebak-nebak sisa waktu atau jam! Lu WAJIB memanggil tool 'cek_pengingat_aktif' jika ditanya sisa waktu.\n"
        "2. JANGAN PERNAH bilang lu tidak punya modul pengingat. Lu SUDAH PUNYA 'buat_pengingat_dinamis'. LANGSUNG panggil toolnya!\n"
        "3. JIKA Bos meminta tugas DI LUAR alat di atas (misal: kirim email, akses kalender Google), BARU lu boleh jujur bilang belum punya modulnya dan minta diupdate kodenya."
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
    global scheduler, telegram_app  # INI MANTRA YANG KEMAREN KELUPAAN BRO!
    telegram_app = application
    
    # Bikin schedulernya DI DALAM sini biar sinkron sama event loop Telegram
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    scheduler = AsyncIOScheduler(timezone="Asia/Jakarta")
    
    # JADWAL TETAP (GYM)
    prompt_gym = "Bos Nazri, ini udah jam 5 sore. Waktunya angkat beban! Kasih semangat biar bos berangkat gym."
    from apscheduler.triggers.cron import CronTrigger
    scheduler.add_job(
        proactive_reminder, 
        CronTrigger(day_of_week='mon,wed,fri', hour=17, minute=0), 
        args=[application, prompt_gym]
    )

    scheduler.start()
    print("⏳ [SCHEDULER] Sistem Cron Job & Dynamic Tool aktif di Event Loop Utama!")

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