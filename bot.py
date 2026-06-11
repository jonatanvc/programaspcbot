import asyncio
import os
import random
import shutil
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
    register_user, increment_download, get_stats
)
from scraper import scrape_majorgeeks, scrape_custom_isos
from processor import check_disk_space, download_file, split_file_if_needed, cleanup_files

# Instancia global del bot (se inicializará en main)
app = Client(
    "distribucion_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN
)

task_queue = asyncio.Queue()
db_pool = None

# Diccionario para controlar el estado de los usuarios (ej. si están esperando para buscar)
user_states = {}

# --- Funciones Core (Procesamiento) ---

async def send_admin_alert(message_text):
    try:
        if ADMIN_ID != 0:
            await app.send_message(chat_id=ADMIN_ID, text=f"⚠️ **ALERTA DEL SISTEMA**\n\n{message_text}")
    except Exception as e:
        logger.error(f"No se pudo enviar alerta al admin: {e}")

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
            logger.warning(f"FloodWait detectado! Pausando por {e.value} segundos...")
            await asyncio.sleep(e.value + 1)
        except Exception as e:
            logger.error(f"Error al subir archivo {file_path}: {e}")
            await asyncio.sleep(5)
            
    return None

async def process_program_task(program_record):
    program_id = program_record['id']
    titulo = program_record['titulo']
    url = program_record['url_origen']
    
    logger.info(f"Iniciando procesamiento de ID {program_id}: {titulo}")
    await update_status(db_pool, program_id, 'Procesando')
    
    if not check_disk_space():
        await send_admin_alert(f"Cola pausada temporalmente. Espacio en disco insuficiente en VPS.")
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
            logger.error(f"No se pudo descargar {titulo}")
            await update_status(db_pool, program_id, 'Error')
            await send_admin_alert(f"Error de descarga: {titulo}\nEnlace original puede estar caído: {url}")
            return
            
        files_to_upload = await split_file_if_needed(original_file)
        
        if not files_to_upload:
            logger.error(f"Error al procesar/dividir {titulo}")
            await update_status(db_pool, program_id, 'Error')
            return
            
        uploaded_file_ids = []
        for f in files_to_upload:
            caption = f"ID: `{program_id}` | Parte: {os.path.basename(f)}\n{titulo}"
            file_id = await safe_upload_file(PRIVATE_BACKUP_CHANNEL_ID, f, caption)
            if file_id:
                uploaded_file_ids.append(file_id)
            else:
                logger.error(f"Fallo final al subir {f}")
                
        if len(uploaded_file_ids) != len(files_to_upload):
            logger.error(f"No se subieron todas las partes para {titulo}.")
            await update_status(db_pool, program_id, 'Error')
            return
            
        await update_file_ids(db_pool, program_id, uploaded_file_ids)
        await update_status(db_pool, program_id, 'Publicado')
        
        bot_me = await app.get_me()
        bot_username = bot_me.username
        
        peso_total_mb = sum(os.path.getsize(f) for f in files_to_upload) / (1024 * 1024)
        texto_post = (
            f"**{titulo}**\n\n"
            f"ℹ️ {program_record['categoria']}\n"
            f"📦 Tamaño total: {peso_total_mb:.2f} MB\n"
        )
        
        reply_markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("📥 Descargar mediante el Bot", url=f"https://t.me/{bot_username}?start={program_id}")]
        ])
        
        await app.send_message(
            chat_id=PUBLIC_CHANNEL_ID,
            text=texto_post,
            reply_markup=reply_markup
        )
        logger.info(f"Publicado exitosamente ID {program_id} en canal público.")
        
    except Exception as e:
        logger.error(f"Excepción crítica procesando {titulo}: {e}")
        await update_status(db_pool, program_id, 'Error')
    finally:
        logger.info(f"Ejecutando limpieza para {titulo}...")
        files_to_clean = []
        if original_file: files_to_clean.append(original_file)
        if files_to_upload: files_to_clean.extend(files_to_upload)
        files_to_clean = list(set(files_to_clean))
        cleanup_files(files_to_clean)

async def worker_queue_processor():
    logger.info("Iniciando Worker de procesamiento de la cola.")
    while True:
        try:
            program_record = await task_queue.get()
            await process_program_task(program_record)
            task_queue.task_done()
        except Exception as e:
            logger.error(f"Error en el worker: {e}")
            await asyncio.sleep(5)

async def scheduled_scraper_task():
    while True:
        try:
            logger.info("Iniciando ciclo de scraping...")
            resultados = await scrape_majorgeeks()
            nuevos = 0
            for item in resultados:
                existe = await program_exists(db_pool, item['titulo'], item['version'])
                if not existe:
                    pid = await insert_program(
                        db_pool,
                        item['titulo'],
                        item['version'],
                        item['categoria'],
                        item['url_origen']
                    )
                    if pid:
                        nuevos += 1
            logger.info(f"Ciclo de scraping terminado. Se agregaron {nuevos} nuevos programas a la cola.")
            
            pendientes = await get_pending_programs(db_pool)
            for p in pendientes:
                await task_queue.put(p)
        except Exception as e:
            logger.error(f"Error en la tarea de scraping programada: {e}")
        await asyncio.sleep(7200)


# --- Interfaz de Usuario y Botones Inline ---

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
        [InlineKeyboardButton("📊 Estadísticas del Bot", callback_data="admin_stats")],
        [InlineKeyboardButton("💾 Espacio en Disco", callback_data="admin_disk")],
        [InlineKeyboardButton("⬅️ Volver al Menú", callback_data="menu_main")]
    ])

@app.on_message(filters.command("start") & filters.private)
async def start_command_handler(client, message):
    await register_user(db_pool, message.from_user.id)
    
    command_args = message.text.split(" ", 1)
    
    # Manejo del Deep Linking (botón del canal público)
    if len(command_args) > 1:
        program_id_str = command_args[1]
        if program_id_str.isdigit():
            program_id = int(program_id_str)
            program_record = await get_program_by_id(db_pool, program_id)
            
            if program_record and program_record['estado'] == 'Publicado':
                import json
                file_ids = []
                if program_record['telegram_file_ids']:
                    try: file_ids = json.loads(program_record['telegram_file_ids'])
                    except: pass
                
                if file_ids:
                    await message.reply_text(f"🚀 Preparando **{program_record['titulo']}**...")
                    # Incrementar estadísticas
                    await increment_download(db_pool, program_id)
                    
                    for i, fid in enumerate(file_ids, 1):
                        await asyncio.sleep(1)
                        await client.send_document(
                            chat_id=message.chat.id,
                            document=fid,
                            caption=f"Parte {i} de {len(file_ids)}" if len(file_ids) > 1 else ""
                        )
                    return
                else:
                    await message.reply_text("❌ Error: Archivos no disponibles.")
                    return
            else:
                await message.reply_text("❌ Programa no encontrado o aún no procesado.")
                return

    # Si es un /start normal, mostramos el Menú Principal
    user_states.pop(message.from_user.id, None) # Limpiar cualquier estado previo
    await message.reply_text(
        "👋 **¡Bienvenido al Sistema de Software Automatizado!**\n\n"
        "Desde aquí puedes buscar y descargar todos los programas de nuestra biblioteca, libres de juegos y publicidad.\n\n"
        "Selecciona una opción del menú inferior:",
        reply_markup=get_main_menu_keyboard(message.from_user.id)
    )

@app.on_callback_query()
async def callback_handler(client, callback_query: CallbackQuery):
    data = callback_query.data
    user_id = callback_query.from_user.id
    
    # --- MENÚ PRINCIPAL ---
    if data == "menu_main":
        user_states.pop(user_id, None)
        await callback_query.message.edit_text(
            "Selecciona una opción del menú inferior:",
            reply_markup=get_main_menu_keyboard(user_id)
        )
        
    elif data == "menu_search":
        user_states[user_id] = "WAITING_SEARCH"
        await callback_query.message.edit_text(
            "🔍 **Búsqueda de Programas**\n\n"
            "Por favor, escribe en este chat el nombre del programa que estás buscando (Ej: `Chrome`, `Office`).",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancelar", callback_data="menu_main")]])
        )
        
    elif data == "menu_latest":
        ultimos = await get_latest_programs(db_pool, 5)
        if not ultimos:
            await callback_query.answer("No hay programas disponibles aún.", show_alert=True)
            return
            
        texto = "🆕 **Últimos 5 Programas Agregados:**"
        botones = []
        for p in ultimos:
            botones.append([InlineKeyboardButton(p['titulo'], callback_data=f"dl_{p['id']}")])
            
        botones.append([InlineKeyboardButton("⬅️ Volver al Menú", callback_data="menu_main")])
        await callback_query.message.edit_text(texto, reply_markup=InlineKeyboardMarkup(botones))
        
    elif data == "menu_help":
        texto = (
            "ℹ️ **Información del Bot**\n\n"
            "- Todos los programas son extraídos de fuentes confiables.\n"
            "- Las subidas están divididas en partes de 1.9GB si son ISOs grandes.\n"
            "- Navega usando los botones interactivos.\n"
        )
        await callback_query.message.edit_text(
            texto,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Volver al Menú", callback_data="menu_main")]])
        )
        
    # --- DESCARGA DESDE BOTÓN INLINE ---
    elif data.startswith("dl_"):
        program_id = int(data.split("_")[1])
        program_record = await get_program_by_id(db_pool, program_id)
        
        if program_record and program_record['estado'] == 'Publicado':
            import json
            file_ids = []
            if program_record['telegram_file_ids']:
                try: file_ids = json.loads(program_record['telegram_file_ids'])
                except: pass
            
            if file_ids:
                await callback_query.answer(f"Enviando {program_record['titulo']}...", show_alert=False)
                await increment_download(db_pool, program_id)
                for i, fid in enumerate(file_ids, 1):
                    await asyncio.sleep(1)
                    await client.send_document(
                        chat_id=user_id,
                        document=fid,
                        caption=f"Parte {i} de {len(file_ids)}" if len(file_ids) > 1 else ""
                    )
            else:
                await callback_query.answer("Archivos no disponibles.", show_alert=True)
        else:
            await callback_query.answer("Programa no encontrado.", show_alert=True)

    # --- PANEL DE ADMIN ---
    elif data == "menu_admin":
        if user_id != ADMIN_ID:
            await callback_query.answer("Acceso Denegado.", show_alert=True)
            return
        await callback_query.message.edit_text(
            "⚙️ **Panel de Administración**\nSelecciona una opción:",
            reply_markup=get_admin_keyboard()
        )
        
    elif data == "admin_stats":
        if user_id != ADMIN_ID: return
        stats = await get_stats(db_pool)
        texto = (
            "📊 **Estadísticas del Sistema:**\n\n"
            f"👥 **Usuarios Totales:** {stats['usuarios_totales']}\n"
            f"📥 **Descargas Totales:** {stats['descargas_totales']}\n"
            f"📦 **Programas Publicados:** {stats['programas_publicados']}\n"
            f"⏳ **En Cola (Pendientes):** {stats['cola_pendientes']}\n"
        )
        await callback_query.message.edit_text(
            texto,
            reply_markup=get_admin_keyboard()
        )
        
    elif data == "admin_disk":
        if user_id != ADMIN_ID: return
        total, used, free = shutil.disk_usage("/")
        texto = (
            "💾 **Almacenamiento del VPS:**\n\n"
            f"Total: {total / (1024**3):.2f} GB\n"
            f"Usado: {used / (1024**3):.2f} GB\n"
            f"Libre: {free / (1024**3):.2f} GB\n"
        )
        await callback_query.message.edit_text(
            texto,
            reply_markup=get_admin_keyboard()
        )


@app.on_message(filters.text & filters.private & ~filters.command("start"))
async def handle_user_text(client, message):
    user_id = message.from_user.id
    
    # Comprobar si el usuario estaba en estado de búsqueda
    if user_states.get(user_id) == "WAITING_SEARCH":
        search_query = message.text
        user_states.pop(user_id, None) # Limpiar estado
        
        resultados = await search_programs(db_pool, search_query)
        
        if not resultados:
            await message.reply_text(
                f"No se encontraron resultados para: `{search_query}`",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔄 Intentar otra búsqueda", callback_data="menu_search")],
                    [InlineKeyboardButton("⬅️ Menú Principal", callback_data="menu_main")]
                ])
            )
            return
            
        texto = f"🔍 **Resultados para:** `{search_query}`\nSelecciona para descargar:"
        botones = []
        for r in resultados:
            botones.append([InlineKeyboardButton(r['titulo'], callback_data=f"dl_{r['id']}")])
            
        botones.append([InlineKeyboardButton("⬅️ Volver al Menú", callback_data="menu_main")])
        await message.reply_text(texto, reply_markup=InlineKeyboardMarkup(botones))

async def main():
    global db_pool
    db_pool = await get_db_pool()
    await init_db(db_pool)
    await app.start()
    logger.info("Bot de Telegram iniciado con interfaz Inline.")
    asyncio.create_task(worker_queue_processor())
    asyncio.create_task(scheduled_scraper_task())
    try:
        while True: await asyncio.sleep(3600)
    except KeyboardInterrupt:
        logger.info("Apagando...")
    finally:
        await app.stop()
        if db_pool: await db_pool.close()

if __name__ == "__main__":
    if os.name == 'nt':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
