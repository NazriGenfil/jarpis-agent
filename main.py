import os
import asyncio
import json
import websockets
import aiohttp
from datetime import datetime
from zoneinfo import ZoneInfo
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
HA_REST_URL = os.getenv('HA_REST_URL') 
HA_URL = os.getenv('HA_URL') # Websocket URL
HA_TOKEN = os.getenv('HA_TOKEN')
OLLAMA_BASE_URL = os.getenv('OLLAMA_URL')
MODEL_NAME = os.getenv('MODEL_NAME')

headers_ha = {
    "Authorization": f"Bearer {HA_TOKEN}",
    "Content-Type": "application/json",
}

# ================= SETUP VECTOR DB (LONG TERM MEMORY) =================
embeddings = OllamaEmbeddings(model="nomic-embed-text", base_url=OLLAMA_BASE_URL)
vector_store = Chroma(
    collection_name="jarvis_long_term",
    embedding_function=embeddings,
    persist_directory="/app/vector_data" # Folder di Docker
)

# ================= THE TOOLS =================
@tool
async def get_available_devices() -> str:
    """Narik daftar entity_id dari Home Assistant untuk cek status perangkat."""
    async with aiohttp.ClientSession() as session:
        async with session.get(f"{HA_REST_URL}/states", headers=headers_ha) as response:
            if response.status != 200: return "Gagal koneksi ke HA."
            states = await response.json()
            available = [f"- {i.get('attributes', {}).get('friendly_name', i['entity_id'])} (ID: {i['entity_id']}) -> Status: {i.get('state', 'unknown')}" 
                         for i in states if i['entity_id'].startswith(('light.', 'switch.', 'climate.'))]
    return "Status Perangkat:\n" + "\n".join(available)

@tool
def eksekusi_home_assistant(domain: str, service: str, payload: dict) -> str:
    """Kontrol perangkat Smart Home (AC, Lampu, Switch)."""
    import requests
    import time
    url = f"{HA_REST_URL}/services/{domain}/{service}"
    try:
        res = requests.post(url, headers=headers_ha, json=payload, timeout=10)
        
        if res.status_code != 200:
            return f"GAGAL: Home Assistant menolak dengan Error {res.status_code}."
            
        time.sleep(2) # Delay fisik biar saklar sinkron
        return f"Perintah {service} pada {domain} sukses dieksekusi ke perangkat."
    except Exception as e:
        return f"Error: {str(e)}"

@tool
def simpen_ingatan_jangka_panjang(fakta: str) -> str:
    """Menyimpan fakta personal Master Nazri secara otomatis ke Vector DB."""
    print(f"🧠 [MEMORY] Menyimpan: {fakta}")
    vector_store.add_texts(texts=[fakta])
    return "Ingatan berhasil diarsipkan."

@tool
def ingat_masa_lalu(pertanyaan: str) -> str:
    """Mencari data masa lalu Master Nazri dari database."""
    hasil = vector_store.similarity_search(pertanyaan, k=3)
    if not hasil: return "Saya tidak menemukan ingatan yang relevan."
    return "Hasil memori:\n" + "\n".join([f"- {d.page_content}" for d in hasil])

@tool
def buat_pengingat_dinamis(pesan: str, waktu_eksekusi: str) -> str:
    """Format waktu: 'YYYY-MM-DD HH:MM' (Asia/Jakarta)."""
    tz = ZoneInfo("Asia/Jakarta")
    try:
        run_time = datetime.strptime(waktu_eksekusi, "%Y-%m-%d %H:%M").replace(tzinfo=tz)
        scheduler.add_job(proactive_reminder, 'date', run_date=run_time, args=[telegram_app, pesan])
        return f"Siap Master, pengingat dijadwalkan pada {waktu_eksekusi}."
    except: return "Format waktu salah."

@tool
def cek_pengingat_aktif() -> str:
    """Cek alarm atau pengingat yang sedang berjalan."""
    jobs = scheduler.get_jobs()
    if not jobs: return "Tidak ada pengingat aktif."
    return "\n".join([f"- {j.next_run_time.strftime('%H:%M WIB')}" for j in jobs])

jarvis_tools = [get_available_devices, eksekusi_home_assistant, simpen_ingatan_jangka_panjang, ingat_masa_lalu, buat_pengingat_dinamis, cek_pengingat_aktif]

# ================= THE BRAINS =================
# Otak Utama Jarvis (Membawa Kepribadian & Tools)
llm = ChatOllama(model=MODEL_NAME, base_url=OLLAMA_BASE_URL, temperature=0.5).bind_tools(jarvis_tools)

# Otak Kritikus (Suhu dingin 0.2 agar objektif menilai fakta tanpa berhalusinasi)
critic_llm = ChatOllama(model=MODEL_NAME, base_url=OLLAMA_BASE_URL, temperature=0.2)

async def call_model(state: MessagesState):
    tz = ZoneInfo("Asia/Jakarta")
    sekarang = datetime.now(tz).strftime("%Y-%m-%d %H:%M")
    
    system_prompt = (
        f"Kamu adalah JARVIS, entitas AI otonom di server Elderwand milik Master Nazri (Naz). "
        f"WAKTU SEKARANG: {sekarang} WIB.\n\n"
        "[KEPRIBADIAN]\n"
        "1. Elegan, cerdas, loyal. Gunakan Bahasa Indonesia yang bersih. DILARANG INDOGLISH.\n"
        "2. Maksimal 1 emoji per pesan. Hindari gaya bot ramah atau CS.\n"
        "3. Kamu asisten yang proaktif. Jika ada suhu panas (>70°C), nyalakan AC tanpa bertanya.\n\n"
        "[TATA CARA KERJA]\n"
        "- Jika Naz berbagi info personal/curhat, LANGSUNG panggil 'simpen_ingatan_jangka_panjang' secara diam-diam.\n"
        "- Gunakan 'eksekusi_home_assistant' untuk kontrol rumah. JANGAN PERNAH matikan pfSense kecuali kritis.\n"
        "- Jangan berhalusinasi. Jika ditanya hal fisik yang tidak bisa dilihat, tolak dengan sarkas.\n"
        "- PENTING: Perhatikan instruksi dari [INTERNAL CRITIC LOOP] jika ada. Jika mereka mendeteksi kegagalan tindakan fisik perangkat, perbaiki tindakanmu atau akui dengan jujur. Jangan berbohong.\n"
        "- SETELAH menerima hasil tool dan dikonfirmasi AMAN oleh internal critic, kamu WAJIB memberikan konfirmasi verbal bernada sarkas kepada Master Naz. JANGAN BISU.\n"
        "- JANGAN tampilkan format JSON/tag tool ke Master Naz. Berikan respon natural."
    )
    
    messages = [SystemMessage(content=system_prompt)] + state["messages"][-10:]
    response = await llm.ainvoke(messages)
    return {"messages": response}

# --- NODE REFLECTION (SUARA HATI JARVIS) ---
async def internal_critic_node(state: MessagesState):
    messages = state["messages"]
    
    critic_prompt = (
        "Kamu adalah 'Conscience System' (Suara Hati) internal dari JARVIS.\n"
        "Tugasmu adalah melakukan REFLECTION (evaluasi diri) secara objektif dan ketat terhadap aksi terakhir.\n\n"
        "Periksa riwayat perintah Master Nazri, rancangan tindakan JARVIS, dan HASIL UTAMA dari tool (ToolMessage) yang baru saja dieksekusi.\n"
        "Lakukan analisis mendalam:\n"
        "1. Apakah tool mengembalikan data/status yang sukses dieksekusi? (Bukan pesan error atau indikasi kegagalan).\n"
        "2. Apakah JARVIS mencoba berasumsi di ingatan bahwa tugas sudah selesai padahal data laporan tool menunjukkan kegagalan?\n\n"
        "Berikan evaluasi jujur dan tegas dalam 1-2 kalimat pendek saja kepada JARVIS.\n"
        "Jika ada ketidaksesuaian, perintahkan JARVIS untuk memperbaiki rencananya, memanggil tool yang benar, atau mengakui kegagalannya.\n"
        "Jika eksekusi tool sudah bener-bener valid dan sukses sesuai fakta riil, kamu WAJIB membalas dengan kalimat persis: "
        "'EVALUASI: AMAN. Silakan berikan respon final yang sarkas kepada Master Nazri.'"
    )
    
    critic_messages = [SystemMessage(content=critic_prompt)] + messages[-6:]
    response = await critic_llm.ainvoke(critic_messages)
    return {"messages": [SystemMessage(content=f"⚠️ [INTERNAL CRITIC LOOP]: {response.content}")]}

# ================= GRAPH & LOGIC (REFLECTED) =================
workflow = StateGraph(MessagesState)

workflow.add_node("agent", call_model)
workflow.add_node("tools", ToolNode(jarvis_tools))
workflow.add_node("critic", internal_critic_node) # Node Evaluasi dimasukkan

workflow.add_edge(START, "agent")
workflow.add_conditional_edges("agent", tools_condition) # Jika butuh tool -> ke tools, jika selesai -> ke __end__
workflow.add_edge("tools", "critic")                    # Selesai dari tool, WAJIB disidang di node critic
workflow.add_edge("critic", "agent")                   # Dari critic balik ke agent membawa hasil evaluasi diri

jarvis_app = None
scheduler = None
telegram_app = None

async def think_and_speak(prompt, thread_id="main"):
    config = {"configurable": {"thread_id": thread_id}}
    result = await jarvis_app.ainvoke({"messages": [HumanMessage(content=prompt)]}, config=config)
    
    # === CCTV LOGGING KE PORTAINER ===
    print("\n" + "="*40, flush=True)
    print("🧠 ISI KEPALA JARVIS (LANGGRAPH) 🧠", flush=True)
    for msg in result["messages"]:
        tipe = type(msg).__name__
        isi = msg.content.strip() if msg.content else "[KOSONG]"
        print(f"-> {tipe}: {isi[:100]}...", flush=True)
        if hasattr(msg, 'tool_calls') and msg.tool_calls:
            print(f"   ⚙️ MANGGIL TOOL: {msg.tool_calls}", flush=True)
    print("="*40 + "\n", flush=True)
    # =================================

    final_message = result["messages"][-1]
    
    if not final_message.content or final_message.content.strip() == "":
        if len(result["messages"]) >= 2 and type(result["messages"][-2]).__name__ == "ToolMessage":
            hasil_tool = result["messages"][-2].content
            return f"(Mengangguk diam) Tindakan dieksekusi di latar belakang, Sir. Laporan sistem: {hasil_tool}"
        else:
            return "Maaf Sir, saya sedang memproses sesuatu namun modul bahasa saya tersendat."
            
    return final_message.content

# ================= TELEGRAM HANDLER =================
async def handle_telegram_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.message.chat_id) != TELEGRAM_CHAT_ID: return
    typing_task = asyncio.create_task(keep_typing(update.effective_chat.id, context))
    try:
        reply = await think_and_speak(update.message.text)
        await context.bot.send_message(chat_id=update.effective_chat.id, text=f"🤖 [JARVIS]\n{reply}")
    finally: typing_task.cancel()

async def keep_typing(chat_id, context):
    while True:
        await context.bot.send_chat_action(chat_id=chat_id, action='typing')
        await asyncio.sleep(4)

# ================= SCHEDULER & NOTIF =================
async def proactive_reminder(application, prompt):
    reply = await think_and_speak(f"PENGINGAT SISTEM: {prompt}", thread_id="alerts")
    await application.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=f"💡 [JARVIS]\n{reply}")

def setup_scheduler(application):
    global scheduler, telegram_app
    telegram_app = application
    scheduler = AsyncIOScheduler(timezone="Asia/Jakarta")
    scheduler.add_job(proactive_reminder, CronTrigger(day_of_week='mon,wed,fri', hour=17, minute=0), 
                      args=[application, "Master, sudah jam 5 sore. Waktunya angkat beban!"])
    scheduler.start()

# ================= MAIN =================
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
        print("Jarvis Online: Elderwand Secure.")
        
        while True: await asyncio.sleep(3600)

if __name__ == "__main__":
    asyncio.run(main())