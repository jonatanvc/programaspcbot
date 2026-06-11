import asyncio
import os
import random
import shutil
import json
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from pyrogram.errors import FloodWait

from config import (
    API_ID, API_HASH, BOT_TOKEN, ADMIN_ID, 
    PUBLIC_CHANNEL_ID, PRIVATE_BACKUP_CHANNEL_ID, logger
)
from database import (
    get_db_pool, init_db, insert_program, program_exists, 
    get_pending_programs, update_status, update_file_ids, 
    get_program_by_id, search_programs, get_latest_programs,
    register_user, increment_download, get_stats, get_error_programs, delete_program
)
from scraper import scrape_majorgeeks, scrape_massgrave
from processor import check_disk_space, download_file, split_file_if_needed, cleanup_files

# Instancias Globales
app = Client(
    "distribucion_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN
)

task_queue = asyncio.Queue()  # Cola para descargar y subir (worker principal)
user_download_queue = asyncio.Queue()  # NUEVO: Cola para evitar Flood al enviar a usuarios
db_pool = None
user_states = {}

# --- WORKERS Y PROCESADORES (EN SEGUNDO PLANO) ---

async def send_admin_alert(message_text):
    try:
        if ADMIN_ID != 0:
            await app.send_message(chat_id=ADMIN_ID, text=f"⚠️ **ALERTA DEL SISTEMA**\n\n{message_text}")
    except Exception as e:
        pass

async def safe_upload_file(chat_id, file_path, caption=""):
    max_retries = 3
    for attempt in range(1, max_retries + 1):
        try:
            logger.info(f"Subiendo {file_path} a Telegram (Intento {attempt})...")
            await asyncio.sleep(random.uniform(3, 5))
            message = await app.send_document(
                chat_id=chat_id,
                document=file_path,
                caption=caption
            )
            return message.document.file_id
        except FloodWait as e:
            logger.warning(f"FloodWait detectado! Pausando {e.value}s...")
            await asyncio.sleep(e.value + 1)
        except Exception as e:
            logger.error(f"Error al subir: {e}")
            await asyncio.sleep(5)
    return None

async def process_program_task(program_record):
    """Descarga de la web, corta con 7z y lo sube al canal privado."""
    program_id = program_record['id']
    titulo = program_record['titulo']
    url = program_record['url_origen']
    
    logger.info(f"Iniciando procesamiento de ID {program_id}: {titulo}")
    await update_status(db_pool, program_id, 'Procesando')
    
    if not check_disk_space():
        await send_admin_alert(f"Espacio en disco insuficiente. Pausando.")
        await update_status(db_pool, program_id, 'Pendiente')
        await asyncio.sleep(600)
        return
        
    original_file = None
    files_to_upload = []
    
    try:
        file_extension = ".zip"
        if ".exe" in url.lower(): file_extension = ".exe"
        elif ".msi" in url.lower(): file_extension = ".msi"
        elif ".iso" in url.lower(): file_extension = ".iso"
        
        file_name = f"prog_{program_id}{file_extension}"
        original_file = await download_file(url, file_name)
        
        if not original_file:
            logger.error(f"Descarga fallida: {titulo}")
            await update_status(db_pool, program_id, 'Error')
            return
            
        files_to_upload = await split_file_if_needed(original_file)
        if not files_to_upload:
            await update_status(db_pool, program_id, 'Error')
            return
            
        uploaded_file_ids = []
        for f in files_to_upload:
            caption = f"ID: `{program_id}` | Parte: {os.path.basename(f)}\n{titulo}"
            file_id = await safe_upload_file(PRIVATE_BACKUP_CHANNEL_ID, f, caption)
            if file_id:
                uploaded_file_ids.append(file_id)
            else:
                break
                
        if len(uploaded_file_ids) != len(files_to_upload):
            await update_status(db_pool, program_id, 'Error')
            return
            
        await update_file_ids(db_pool, program_id, uploaded_file_ids)
        await update_status(db_pool, program_id, 'Publicado')
        
        bot_me = await app.get_me()
        peso_total_mb = sum(os.path.getsize(f) for f in files_to_upload) / (1024 * 1024)
        texto_post = f"**{titulo}**\n\nℹ️ {program_record['categoria']}\n📦 Total: {peso_total_mb:.2f} MB"
        reply_markup = InlineKeyboardMarkup([[InlineKeyboardButton("📥 Descargar", url=f"https://t.me/{bot_me.username}?start={program_id}")]])
        
        await app.send_message(chat_id=PUBLIC_CHANNEL_ID, text=texto_post, reply_markup=reply_markup)
        
    except Exception as e:
        logger.error(f"Error procesando {titulo}: {e}")
        await update_status(db_pool, program_id, 'Error')
    finally:
        files_to_clean = []
        if original_file: files_to_clean.append(original_file)
        if files_to_upload: files_to_clean.extend(files_to_upload)
        cleanup_files(list(set(files_to_clean)))

async def worker_queue_processor():
    """Worker 1: Procesa descargas de internet (Aria2) a Telegram."""
    while True:
        try:
            program_record = await task_queue.get()
            await process_program_task(program_record)
            task_queue.task_done()
        except Exception as e:
            logger.error(f"Error Worker1: {e}")
            await asyncio.sleep(5)

async def user_queue_processor():
    """Worker 2: Cola estricta de envíos a usuarios (Anti-Ban FloodWait)."""
    logger.info("Worker de Cola de Usuarios iniciado.")
    while True:
        try:
            # Obtiene la petición de la cola
            request = await user_download_queue.get()
            user_id = request['user_id']
            file_ids = request['file_ids']
            titulo = request['titulo']
            
            # Enviar los archivos al usuario uno por uno
            for i, fid in enumerate(file_ids, 1):
                try:
                    await asyncio.sleep(1.5) # Ritmo seguro para Telegram
                    await app.send_document(
                        chat_id=user_id,
                        document=fid,
                        caption=f"{titulo} (Parte {i}/{len(file_ids)})" if len(file_ids) > 1 else titulo
                    )
                except FloodWait as e:
                    logger.warning(f"FloodWait en worker usuarios. Pausa de {e.value}s")
                    await asyncio.sleep(e.value + 1)
                except Exception as e:
                    logger.error(f"Error enviando a usuario {user_id}: {e}")
                    
            user_download_queue.task_done()
            await asyncio.sleep(2) # Pausa entre usuarios distintos
        except Exception as e:
            logger.error(f"Error Worker2: {e}")
            await asyncio.sleep(5)

async def scheduled_scraper_task():
    while True:
        try:
            logger.info("Scraping en curso...")
            resultados = []
            resultados.extend(await scrape_majorgeeks())
            resultados.extend(await scrape_massgrave()) # Nuevo Scraper
            
            nuevos = 0
            for item in resultados:
                if not await program_exists(db_pool, item['titulo'], item['version']):
                    pid = await insert_program(db_pool, item['titulo'], item['version'], item['categoria'], item['url_origen'])
                    if pid: nuevos += 1
            logger.info(f"Scraping completado. {nuevos} agregados.")
            
            for p in await get_pending_programs(db_pool):
                await task_queue.put(p)
        except Exception as e:
            pass
        await asyncio.sleep(7200)

# --- INTERFAZ (BOTONES INLINE) ---

def get_main_menu_keyboard(user_id):
    buttons = [
        [InlineKeyboardButton("🔍 Buscar Programa", callback_data="menu_search")],
        [InlineKeyboardButton("🆕 Últimos Agregados", callback_data="menu_latest")],
        [InlineKeyboardButton("ℹ️ Ayuda / Info", callback_data="menu_help")]
    ]
    if user_id == ADMIN_ID:
        buttons.append([InlineKeyboardButton("⚙️ Panel de Admin", callback_data="menu_admin")])
    return InlineKeyboardMarkup(buttons)

def get_admin_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Estadísticas", callback_data="admin_stats")],
        [InlineKeyboardButton("💾 Almacenamiento", callback_data="admin_disk")],
        [InlineKeyboardButton("🔄 Gestionar Errores", callback_data="admin_errors")],
        [InlineKeyboardButton("⬅️ Volver", callback_data="menu_main")]
    ])

@app.on_message(filters.command("start") & filters.private)
async def start_command_handler(client, message):
    await register_user(db_pool, message.from_user.id)
    command_args = message.text.split(" ", 1)
    
    # Deep Link Handler
    if len(command_args) > 1 and command_args[1].isdigit():
        program_id = int(command_args[1])
        program_record = await get_program_by_id(db_pool, program_id)
        
        if program_record and program_record['estado'] == 'Publicado':
            file_ids = []
            if program_record['telegram_file_ids']:
                try: file_ids = json.loads(program_record['telegram_file_ids'])
                except: pass
            
            if file_ids:
                # Agregar a la cola de usuarios en vez de enviar directo
                await user_download_queue.put({
                    'user_id': message.chat.id,
                    'file_ids': file_ids,
                    'titulo': program_record['titulo']
                })
                await increment_download(db_pool, program_id)
                await message.reply_text(f"⏳ **{program_record['titulo']}** añadido a la cola de envíos.\nRecibirás los archivos en breve...")
                return
        await message.reply_text("❌ Archivos no disponibles.")
        return

    # Menú Principal
    user_states.pop(message.from_user.id, None)
    await message.reply_text(
        "👋 **¡Bienvenido al Bot de Software!**\n\nNavega usando los botones:",
        reply_markup=get_main_menu_keyboard(message.from_user.id)
    )

@app.on_callback_query()
async def callback_handler(client, callback_query: CallbackQuery):
    data = callback_query.data
    user_id = callback_query.from_user.id
    
    if data == "menu_main":
        user_states.pop(user_id, None)
        await callback_query.message.edit_text("Navega usando los botones:", reply_markup=get_main_menu_keyboard(user_id))
        
    elif data == "menu_search":
        user_states[user_id] = "WAITING_SEARCH"
        await callback_query.message.edit_text("🔍 Escribe el nombre del programa:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancelar", callback_data="menu_main")]]))
        
    elif data == "menu_latest":
        ultimos = await get_latest_programs(db_pool, 5)
        if not ultimos:
            await callback_query.answer("Vacio.", show_alert=True)
            return
        botones = [[InlineKeyboardButton(p['titulo'], callback_data=f"dl_{p['id']}")] for p in ultimos]
        botones.append([InlineKeyboardButton("⬅️ Volver", callback_data="menu_main")])
        await callback_query.message.edit_text("🆕 **Últimos Agregados:**", reply_markup=InlineKeyboardMarkup(botones))
        
    elif data == "menu_help":
        await callback_query.message.edit_text("ℹ️ **Información**\nTodo automatizado y limpio.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Volver", callback_data="menu_main")]]))
        
    elif data.startswith("dl_"):
        program_id = int(data.split("_")[1])
        program_record = await get_program_by_id(db_pool, program_id)
        if program_record and program_record['estado'] == 'Publicado':
            file_ids = json.loads(program_record['telegram_file_ids'] or "[]")
            if file_ids:
                await user_download_queue.put({
                    'user_id': user_id,
                    'file_ids': file_ids,
                    'titulo': program_record['titulo']
                })
                await increment_download(db_pool, program_id)
                await callback_query.answer("Añadido a la cola. Se enviará en breve...", show_alert=True)
            else:
                await callback_query.answer("Error.", show_alert=True)
        else:
            await callback_query.answer("No encontrado.", show_alert=True)

    # --- ADMIN ---
    elif data == "menu_admin" and user_id == ADMIN_ID:
        await callback_query.message.edit_text("⚙️ **Panel de Admin**", reply_markup=get_admin_keyboard())
        
    elif data == "admin_stats" and user_id == ADMIN_ID:
        stats = await get_stats(db_pool)
        texto = f"📊 **Estadísticas:**\n👥 Usuarios: {stats['usuarios_totales']}\n📥 Descargas: {stats['descargas_totales']}\n📦 Publicados: {stats['programas_publicados']}\n⏳ Pendientes: {stats['cola_pendientes']}\n🧑‍💻 Usuarios en cola espera: {user_download_queue.qsize()}"
        await callback_query.message.edit_text(texto, reply_markup=get_admin_keyboard())
        
    elif data == "admin_disk" and user_id == ADMIN_ID:
        t, u, f = shutil.disk_usage("/")
        texto = f"💾 **Disco:**\nTotal: {t/(1024**3):.2f} GB\nLibre: {f/(1024**3):.2f} GB"
        await callback_query.message.edit_text(texto, reply_markup=get_admin_keyboard())
        
    elif data == "admin_errors" and user_id == ADMIN_ID:
        errores = await get_error_programs(db_pool)
        if not errores:
            await callback_query.answer("No hay programas con error.", show_alert=True)
            return
        botones = []
        for e in errores:
            # Opción para reintentar (retry_)
            botones.append([InlineKeyboardButton(f"🔄 Reintentar {e['titulo'][:15]}", callback_data=f"retry_{e['id']}")])
            # Opción para borrar definitivamente (del_)
            botones.append([InlineKeyboardButton(f"🗑 Borrar {e['titulo'][:15]}", callback_data=f"del_{e['id']}")])
        botones.append([InlineKeyboardButton("⬅️ Volver", callback_data="menu_admin")])
        await callback_query.message.edit_text("⚠️ **Programas con Error:**\nSelecciona una acción:", reply_markup=InlineKeyboardMarkup(botones))
        
    elif data.startswith("retry_") and user_id == ADMIN_ID:
        pid = int(data.split("_")[1])
        await update_status(db_pool, pid, 'Pendiente')
        p = await get_program_by_id(db_pool, pid)
        await task_queue.put(p) # Re-encolar
        await callback_query.answer("Programa enviado a la cola nuevamente.", show_alert=True)
        await callback_query.message.edit_text("⚙️ **Panel de Admin**", reply_markup=get_admin_keyboard())
        
    elif data.startswith("del_") and user_id == ADMIN_ID:
        pid = int(data.split("_")[1])
        await delete_program(db_pool, pid)
        await callback_query.answer("Programa eliminado.", show_alert=True)
        await callback_query.message.edit_text("⚙️ **Panel de Admin**", reply_markup=get_admin_keyboard())

@app.on_message(filters.text & filters.private & ~filters.command("start"))
async def handle_user_text(client, message):
    user_id = message.from_user.id
    if user_states.get(user_id) == "WAITING_SEARCH":
        search_query = message.text
        user_states.pop(user_id, None)
        resultados = await search_programs(db_pool, search_query)
        if not resultados:
            await message.reply_text("No hay resultados.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Menú Principal", callback_data="menu_main")]]))
            return
        botones = [[InlineKeyboardButton(r['titulo'], callback_data=f"dl_{r['id']}")] for r in resultados]
        botones.append([InlineKeyboardButton("⬅️ Volver", callback_data="menu_main")])
        await message.reply_text("🔍 **Resultados:**", reply_markup=InlineKeyboardMarkup(botones))

async def main():
    global db_pool
    db_pool = await get_db_pool()
    await init_db(db_pool)
    await app.start()
    logger.info("Bot de Telegram iniciado (Nivel Enterprise).")
    
    # Iniciar los 3 procesos principales
    asyncio.create_task(worker_queue_processor())
    asyncio.create_task(user_queue_processor()) # <--- NUEVO
    asyncio.create_task(scheduled_scraper_task())
    
    try:
        while True: await asyncio.sleep(3600)
    except KeyboardInterrupt: pass
    finally:
        await app.stop()
        if db_pool: await db_pool.close()

if __name__ == "__main__":
    if os.name == 'nt':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
