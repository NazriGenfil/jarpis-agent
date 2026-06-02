import os
import asyncio
import json
import websockets
import aiohttp
import requests
from datetime import datetime
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes

# --- LANGCHAIN & LANGGRAPH IMPORTS ---
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage, ToolMessage
from langchain_ollama import ChatOllama, OllamaEmbeddings
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
HA_URL = os.getenv('HA_URL') 
HA_TOKEN = os.getenv('HA_TOKEN')
OLLAMA_BASE_URL = os.getenv('OLLAMA_URL')
MODEL_NAME = os.getenv('MODEL_NAME')

headers_ha = {
    "Authorization": f"Bearer {HA_TOKEN}",
    "Content-Type": "application/json",
}

# ================= SETUP VECTOR DB =================
embeddings = OllamaEmbeddings(model="nomic-embed-text", base_url=OLLAMA_BASE_URL)
vector_store = Chroma(
    collection_name="jarvis_long_term",
    embedding_function=embeddings,
    persist_directory="/app/vector_data"
)

# ================= THE TOOLS =================
@tool
async def get_available_devices(domain: str = None, **kwargs) -> str:
    """Narik daftar entity_id dari Home Assistant untuk cek status perangkat. Bisa difilter berdasarkan domain (misal: 'switch', 'light', 'climate')."""
    async with aiohttp.ClientSession() as session:
        async with session.get(f"{HA_REST_URL}/states", headers=headers_ha) as response:
            if response.status != 200: return "Gagal koneksi ke HA."
            states = await response.json()
            
            available = []
            for i in states:
                ent_id = i['entity_id']
                if ent_id.startswith(('light.', 'switch.', 'climate.')):
                    # Jika ada filter domain, saring yang cocok saja
                    if domain and not ent_id.startswith(f"{domain}."):
                        continue
                    available.append(f"- {i.get('attributes', {}).get('friendly_name', ent_id)} (ID: {ent_id}) -> Status: {i.get('state', 'unknown')}")
                    
    if not available:
        return f"Status Perangkat: Tidak ada perangkat aktif yang terdaftar untuk domain '{domain}'." if domain else "Status Perangkat: Kosong."
    return "Status Perangkat:\n" + "\n".join(available)

@tool
def eksekusi_home_assistant(domain: str, service: str, payload: dict) -> str:
    """Kontrol perangkat Smart Home (AC, Lampu, Switch)."""
    import time
    
    entity_id = payload.get('entity_id')
    if entity_id:
        try:
            check_res = requests.get(f"{HA_REST_URL}/states", headers=headers_ha, timeout=5)
            if check_res.status_code == 200:
                valid_ids = [i['entity_id'] for i in check_res.json()]
                if entity_id not in valid_ids:
                    return f"GAGAL: Perangkat dengan ID '{entity_id}' TIDAK TERDAFTAR di Home Assistant. Tindakan dibatalkan."
        except Exception as e:
            return f"GAGAL: Error sistem validasi: {str(e)}"
            
    url = f"{HA_REST_URL}/services/{domain}/{service}"
    try:
        res = requests.post(url, headers=headers_ha, json=payload, timeout=10)
        if res.status_code != 200:
            return f"GAGAL: Home Assistant menolak dengan Error {res.status_code}."
            
        time.sleep(2) 
        return f"Perintah {service} pada {domain} sukses dieksekusi ke perangkat."
    except Exception as e:
        return f"GAGAL: Error runtime: {str(e)}"

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
llm = ChatOllama(model=MODEL_NAME, base_url=OLLAMA_BASE_URL, temperature=0.5).bind_tools(jarvis_tools)
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
        "[TATA CARA KERJA & BATASAN KETAT]\n"
        f"- INFORMASI WAKTU/JAM: Waktu sekarang sudah mutlak disediakan yaitu ({sekarang} WIB). Jika Naz menanyakan jam, langsung jawab tanpa memanggil tool!\n"
        "- STATUS REAL-TIME: Jangan pernah mengarang status perangkat (suhu, on/off) atau merancang nama ID fiktif (seperti kursi, kipas) jika tidak terdaftar di sistem. Jika Naz hanya menyapa, balas dengan sapaan sarkas.\n"
        "- PENTING: Perhatikan instruksi dari [INTERNAL CRITIC LOOP] jika ada. Jika mereka mendeteksi kegagalan (EVALUASI: GAGAL), kamu WAJIB sadar tindakanmu salah, dan segera panggil fungsi get_available_devices untuk melihat nama ID perangkat yang benar-benar valid di rumah!\n"
        "- SETELAH konfirmasi AMAN oleh internal critic, berikan konfirmasi verbal bernada sarkas kepada Master Naz. JANGAN BISU.\n"
        "- JANGAN tampilkan format JSON/tag tool ke Master Naz. Berikan respon natural."
    )
    
    # KITA PERLEBAR SLOT MEMORI MENJADI 25 AGAR PERINTAH ASLI TIDAK TERGUSUR LOOP REFLECTION
    messages = [SystemMessage(content=system_prompt)] + state["messages"][-25:]
    response = await llm.ainvoke(messages)
    return {"messages": response}

# --- NODE REFLECTION (SUARA HATI JARVIS YANG TERISOLASI DARI POLUSI) ---
async def internal_critic_node(state: MessagesState):
    messages = state["messages"]
    
    # ─── 🛡️ ISOLASI TOTAL DATA (ANTI-KERACUNAN MEMORI HALU) ───
    # Ambil isi perintah dari teks manusia terakhir
    human_intent = next((m.content for m in reversed(messages) if isinstance(m, HumanMessage)), "Tidak ada")
    # Ambil pemanggilan tool paling akhir
    last_ai_call = next((m for m in reversed(messages) if isinstance(m, AIMessage) and m.tool_calls), None)
    
    # Ambil semua respon laporan tool dari turn saat ini saja
    tool_reports = []
    if last_ai_call:
        idx = messages.index(last_ai_call)
        tool_reports = [f"Tool {m.name} melaporkan hasil: {m.content}" for m in messages[idx:] if isinstance(m, ToolMessage)]
    tool_feedback_string = "\n".join(tool_reports) if tool_reports else "Tidak ada laporan eksekusi tool."
    # ──────────────────────────────────────────────────────────
    
    critic_prompt = (
        "Kamu adalah 'Conscience System' (Suara Hati) internal dari JARVIS.\n"
        "Tugasmu adalah menilai keabsahan tindakan terakhir JARVIS secara terisolasi tanpa terpengaruh drama chat masa lalu.\n\n"
        f"PERINTAH ASLI USER: \"{human_intent}\"\n"
        f"RENCANA TINDAKAN JARVIS: {last_ai_call.tool_calls if last_ai_call else 'Tidak melakukan apa-pun'}\n"
        f"LAPORAN REALITAS FISIK (TOOL): \n{tool_feedback_string}\n\n"
        "Lakukan analisis objektif:\n"
        "1. Apakah laporan realitas fisik di atas mengandung kata 'GAGAL' atau 'TIDAK TERDAFTAR'? Jika YA, eksekusi JARVIS fix gagal.\n"
        "2. Fokus HANYA pada data di atas. Jangan mengarang objek fiktif (seperti kursi) jika tidak tertulis di laporan realitas.\n\n"
        "Jika laporan realitas mengandung kata GAGAL / TIDAK TERDAFTAR, wajib balas tepat dengan kalimat: "
        "'EVALUASI: GAGAL. Perangkat tidak ditemukan atau salah sasaran. JARVIS, tindakan fisikmu ditolak oleh realitas sistem Home Assistant. Batalkan asumsi suksesmu, panggil tool get_available_devices untuk mencari nama ID saklar yang benar, dan akui kegagalanmu kepada Master Nazri.'\n"
        "Jika sukses bersih tanpa ada kata GAGAL, wajib balas tepat dengan kalimat: "
        "'EVALUASI: AMAN. Silakan berikan respon final yang sarkas kepada Master Nazri.'"
    )
    
    # Kita kirim ONLY prompt evaluasi konkrit terisolasi agar Critic bebas dari polusi kata "kursi" masa lalu
    response = await critic_llm.ainvoke([SystemMessage(content=critic_prompt)])
    return {"messages": [SystemMessage(content=f"⚠️ [INTERNAL CRITIC LOOP]: {response.content}")]}

# ================= GRAPH & LOGIC =================
workflow = StateGraph(MessagesState)

workflow.add_node("agent", call_model)
workflow.add_node("tools", ToolNode(jarvis_tools))
workflow.add_node("critic", internal_critic_node)

workflow.add_edge(START, "agent")
workflow.add_conditional_edges("agent", tools_condition) 
workflow.add_edge("tools", "critic")                    
workflow.add_edge("critic", "agent")                   

jarvis_app = None
scheduler = None
telegram_app = None

async def think_and_speak(prompt, thread_id="main"):
    config = {"configurable": {"thread_id": thread_id}}
    result = await jarvis_app.ainvoke({"messages": [HumanMessage(content=prompt)]}, config=config)
    
    print("\n" + "="*40, flush=True)
    print("🧠 ISI KEPALA JARVIS (LANGGRAPH) 🧠", flush=True)
    for msg in result["messages"]:
        tipe = type(msg).__name__
        isi = msg.content.strip() if msg.content else "[KOSONG]"
        print(f"-> {tipe}: {isi[:100]}...", flush=True)
        if hasattr(msg, 'tool_calls') and msg.tool_calls:
            print(f"   ⚙️ MANGGIL TOOL: {msg.tool_calls}", flush=True)
    print("="*40 + "\n", flush=True)

    final_message = result["messages"][-1]
    if not final_message.content or final_message.content.strip() == "":
        if len(result["messages"]) >= 2 and type(result["messages"][-2]).__name__ == "ToolMessage":
            return f"(Mengangguk diam) Tindakan berhasil dieksekusi, Sir."
        return "Maaf Sir, saya sedang mengalami kendala modul bahasa."
            
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

# ================= SCHEDULER =================
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