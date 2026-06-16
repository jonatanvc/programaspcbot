import asyncio
import os
import random
import sys
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

from config import (
    BOT_TOKEN, ADMIN_ID, PUBLIC_CHANNEL_ID, PRIVATE_BACKUP_CHANNEL_ID,
    logger, TELEGRAM_API_BASE_URL
)
from database import (
    get_db_pool, init_db, insert_program, program_exists, 
    get_pending_programs, update_status, update_file_ids, 
    get_program_by_id, search_programs, get_latest_programs,
    register_user, increment_download, get_stats, get_error_programs, delete_program
)
# Importación del nuevo módulo FileCR
from scraper import scrape_filecr
from processor import check_disk_space, download_file, split_file_if_needed, cleanup_files, safe_upload_file

user_states = {}
worker_queue = asyncio.Queue()       
user_download_queue = asyncio.Queue() 
db_pool = None
application = None

# ----------------- TAREAS EN SEGUNDO PLANO -----------------

async def scheduled_scraper_task():
    while True:
        try:
            logger.info("Iniciando ciclo de scraping programado...")
            
            # Rotar entre palabras clave para obtener software 100% al azar y evitar Cloudflare
            import random
            keywords = [
                "pdf", "video", "audio", "player", "converter", "editor", 
                "recovery", "data", "system", "manager", "network", "photo", 
                "driver", "antivirus", "office", "design", "3d", "animation", 
                "screen", "recorder", "studio", "internet", "browser", "vnc",
                "cleaner", "burner", "usb", "boot", "optimizer", "windows"
            ]
            
            url_a_scrapear = f"https://filecr.com/search/?query={random.choice(keywords)}"
            logger.info(f"Scraping aleatorio usando palabra clave: {url_a_scrapear}")
            
            resultados = []
            
            # Ejecutamos el extractor de FileCR
            resultados.extend(await scrape_filecr(url_a_scrapear))
            
            nuevos = 0
            for prog in resultados:
                if not await program_exists(db_pool, prog['titulo'], prog['version']):
                    await insert_program(
                        db_pool, 
                        prog['titulo'], 
                        prog['version'], 
                        prog.get('categoria', 'Software'), 
                        prog['url_origen'], 
                        prog.get('descripcion', ''),
                        prog.get('imagen_url', ''),
                        prog.get('fecha_actualizacion', 'Reciente'),
                        prog.get('publisher', 'Unknown'),
                        prog.get('languages', 'Multilingual')
                    )
                    nuevos += 1
                    
            logger.info(f"Ciclo terminado. {nuevos} nuevos programas inyectados a PostgreSQL.")
        except Exception as e:
            logger.error(f"Error en scheduled_scraper_task: {e}")
            
        # Ejecución cada 2 horas
        await asyncio.sleep(7200)

download_semaphore = asyncio.Semaphore(3)
publish_lock = asyncio.Lock()

async def process_single_program(prog):
    async with download_semaphore:
        logger.info(f"Iniciando procesamiento concurrente de ID {prog['id']}: {prog['titulo']}")
        
        # 1. Extraer enlace fresco y metadatos usando Playwright
        from scraper import obtener_enlace_dinamico
        from playwright.async_api import async_playwright
        
        data = None
        logger.info(f"FCR-Worker: Obteniendo enlace fresco para {prog['url_origen']}...")
        async with async_playwright() as p:
            browser = None
            context = None
            try:
                browser = await p.chromium.launch(headless=True)
                context = await browser.new_context(
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                    viewport={"width": 1366, "height": 768}
                )
                data = await obtener_enlace_dinamico(context, prog['url_origen'], prog['titulo'])
            except Exception as e:
                logger.error(f"FCR-Worker: Error extrayendo link: {e}")
            finally:
                if context: await context.close()
                if browser: await browser.close()
                
        if not data or not data.get("enlace"):
            logger.error(f"FCR-Worker: No se pudo obtener el enlace dinámico para {prog['titulo']}")
            await update_status(db_pool, prog['id'], 'Error')
            return False
            
        enlace_final = data["enlace"]
        
        # 2. Actualizar metadatos en memoria y base de datos
        prog['version'] = data.get('version', prog.get('version', 'Latest'))
        prog['categoria'] = data.get('categoria', prog.get('categoria', 'Software'))
        prog['descripcion'] = data.get('descripcion', prog.get('descripcion', ''))
        prog['imagen_url'] = data.get('imagen_url', prog.get('imagen_url', ''))
        
        from database import update_program_metadata
        await update_program_metadata(db_pool, prog['id'], prog['version'], prog['categoria'], prog['descripcion'], prog['imagen_url'])
        
        import urllib.parse
        ext = ".zip"
        # Tratar de extraer la extensión del enlace
        parts = urllib.parse.unquote(enlace_final.split('/')[-1].split('?')[0].split('#')[0]).split('.')
        if len(parts) > 1 and len(parts[-1]) <= 4:
            ext = f".{parts[-1]}"
            
        original_filename = f"{prog['titulo'].replace(' ', '_')}{ext}"
        # Limpiar caracteres inválidos
        import re
        original_filename = re.sub(r'[\\/*?:"<>|]', "", original_filename)
            
        # 3. Descargar el archivo con el enlace fresco
        file_path = await download_file(enlace_final, original_filename)
        if not file_path:
            await update_status(db_pool, prog['id'], 'Error')
            from processor import DOWNLOAD_DIR
            failed_path = os.path.join(DOWNLOAD_DIR, original_filename)
            cleanup_files([failed_path, failed_path + ".aria2"])
            return False
            
        part_paths = await split_file_if_needed(file_path)
        if not part_paths:
            await update_status(db_pool, prog['id'], 'Error')
            cleanup_files([file_path])
            return False
        
    # Fuera del semáforo para no bloquear descargas durante el upload
    file_ids = []
    for i, part in enumerate(part_paths, 1):
        if len(part_paths) > 1:
            part_caption = f"<b>{prog['titulo']}</b> Parte {i}"
        else:
            part_caption = f"<b>{prog['titulo']}</b>"
            
        file_id = await safe_upload_file(application.bot, PRIVATE_BACKUP_CHANNEL_ID, part, part_caption)
        if file_id:
            file_ids.append(file_id)
            
    if len(file_ids) == len(part_paths):
        await update_file_ids(db_pool, prog['id'], file_ids)
        await update_status(db_pool, prog['id'], 'Publicado')
        
        if os.path.exists(file_path):
            size_bytes = os.path.getsize(file_path)
            if size_bytes >= 1024 * 1024 * 1024:
                size_str = f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"
            else:
                size_str = f"{size_bytes / (1024 * 1024):.2f} MB"
        else:
            size_str = "Desconocido"
            
        archivos_count = len(part_paths)
        
        if PUBLIC_CHANNEL_ID != 0:
            version_str = prog.get('version', 'Desconocida')
            fecha_str = prog.get('fecha_actualizacion', 'Reciente')
            pub_str = prog.get('publisher', 'Unknown')
            lang_str = prog.get('languages', 'Multilingual')
            cat_str = prog.get('categoria', 'Software')

            # Traducción de la descripción al español
            desc = prog.get('descripcion', 'Utilidad distribuida para Windows.')
            if len(desc) > 350:
                desc = desc[:347] + "..."
                
            try:
                from deep_translator import GoogleTranslator
                desc = GoogleTranslator(source='auto', target='es').translate(desc)
            except Exception as e:
                logger.error(f"Error traduciendo descripción: {e}")

            # Formateo HTML solicitado
            texto = f"<b>{prog['titulo']}</b>\n\n"
            texto += f"⚙️ <b>Versión:</b> {version_str}\n"
            texto += f"🌍 <b>Idiomas:</b> {lang_str}\n"
            texto += f"📂 <b>Categorías:</b> {cat_str}\n\n"
            texto += f"📝 <b>Descripción:</b>\n"
            texto += f"<blockquote>{desc}</blockquote>\n\n"
            texto += f"💾 <b>{size_str}</b> | 📚 <b>{archivos_count} Archivos</b>"
            
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("📥 Descargar Archivos", url=f"https://t.me/{application.bot.username}?start={prog['id']}")
            ]])
            
            # Usar la imagen extraida de la BD o generar avatar si no hay
            imagen_url = prog.get('imagen_url')
            if not imagen_url or not imagen_url.startswith("http"):
                import urllib.parse
                safe_title = urllib.parse.quote(prog['titulo'].split(' ')[0])
                imagen_url = f"https://ui-avatars.com/api/?name={safe_title}&background=random&color=fff&size=256&font-size=0.4"
            
            try:
                await application.bot.send_photo(
                    chat_id=PUBLIC_CHANNEL_ID,
                    photo=imagen_url,
                    caption=texto,
                    reply_markup=keyboard,
                    parse_mode='HTML'
                )
            except Exception as e:
                logger.error(f"Error notificando al canal publico: {e}")
    else:
        await update_status(db_pool, prog['id'], 'Error')
        cleanup_files(part_paths)
        if os.path.exists(file_path):
            cleanup_files([file_path])
        return False
        
    cleanup_files(part_paths)
    if os.path.exists(file_path):
        cleanup_files([file_path])
        
    return True

async def worker_queue_processor():
    while True:
        progs = await get_pending_programs(db_pool, limit=1)
        if not progs:
            await asyncio.sleep(60)
            continue
            
        prog = progs[0]
            
        logger.info(f"Iniciando subida de {prog['titulo']} a Telegram (Modo goteo: 1 cada 12 min)...")
        
        import time
        start_time = time.time()
        
        await update_status(db_pool, prog['id'], 'Procesando')
        success = await process_single_program(prog)
            
        elapsed = time.time() - start_time
        # Queremos exactamente 5 por hora = 1 cada 12 minutos (720 segundos)
        # Descontamos el tiempo que tardó en descargar/subir para mantener la frecuencia exacta
        sleep_time = max(10, 720 - elapsed)
        
        if success:
            logger.info(f"Programa enviado con éxito en {elapsed/60:.1f} min. Esperando {sleep_time/60:.1f} min para mantener el goteo exacto de 5 por hora...")
            await asyncio.sleep(sleep_time)
        else:
            logger.warning(f"Fallo al procesar {prog['titulo']}. Saltando al siguiente en 10s...")
            await asyncio.sleep(10)

async def user_queue_processor():
    while True:
        try:
            task = await user_download_queue.get()
            user_id = task['user_id']
            file_ids = task['file_ids']
            titulo = task['titulo']
            
            for i, fid in enumerate(file_ids, 1):
                try:
                    await asyncio.sleep(1.5)
                    if len(file_ids) > 1:
                        user_caption = f"<b>{titulo}</b> Parte {i}"
                    else:
                        user_caption = f"<b>{titulo}</b>"
                        
                    await application.bot.send_document(
                        chat_id=user_id,
                        document=fid,
                        caption=user_caption,
                        parse_mode='HTML'
                    )
                except Exception as e:
                    logger.warning(f"Error enviando documento a {user_id}: {e}")
            
            user_download_queue.task_done()
        except Exception as e:
            logger.error(f"Error en user_queue_processor: {e}")
            await asyncio.sleep(5)

# ----------------- HANDLERS PTB -----------------

async def start_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    await register_user(db_pool, user_id)
    
    args = context.args
    if args and args[0].isdigit():
        prog_id = int(args[0])
        prog = await get_program_by_id(db_pool, prog_id)
        
        if not prog:
            await update.message.reply_text("❌ Este programa no existe en la base de datos.")
            return
            
        if prog['estado'] != 'Publicado' or not prog['telegram_file_ids']:
            await update.message.reply_text(f"⏳ {prog['titulo']} todavía está en proceso de subida. ¡Prueba en unos minutos!", parse_mode='Markdown')
            return
            
        import json
        file_ids_list = json.loads(prog['telegram_file_ids'])
        await update.message.reply_text("✅ Descarga encolada. El bot te enviará los archivos comprimidos automáticamente en breves instantes...")
        await increment_download(db_pool, prog_id)
        
        await user_download_queue.put({
            'user_id': user_id,
            'file_ids': file_ids_list,
            'titulo': prog['titulo']
        })
        return
        
    await show_main_menu(update, context)

async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = "👋 ¡Bienvenido al Gestor Automatizado de Software!\n\nUtiliza los botones para interactuar de forma inmediata:"
    keyboard = [
        [InlineKeyboardButton("🔍 Buscar Programa", callback_data="search_prog")],
        [InlineKeyboardButton("🆕 Últimos Añadidos", callback_data="latest_prog")],
    ]
    if ADMIN_ID != 0 and update.effective_user.id == ADMIN_ID:
        keyboard.append([InlineKeyboardButton("⚙️ Panel de Control Admin", callback_data="admin_panel")])
        
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    if update.callback_query:
        await update.callback_query.edit_message_text(texto, reply_markup=reply_markup)
    else:
        await update.message.reply_text(texto, reply_markup=reply_markup)

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    user_id = update.effective_user.id
    
    await query.answer()
    
    if data == "menu_main":
        user_states.pop(user_id, None)
        await show_main_menu(update, context)
        
    elif data == "search_prog":
        user_states[user_id] = "waiting_search"
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver al Menú", callback_data="menu_main")]])
        await query.edit_message_text("🔍 Escribe las palabras clave del programa que deseas buscar:", reply_markup=keyboard)
        
    elif data == "latest_prog":
        progs = await get_latest_programs(db_pool, 5)
        if not progs:
            await query.edit_message_text("No hay software disponible actualmente en el índice.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver", callback_data="menu_main")]]))
            return
            
        keyboard = []
        for p in progs:
            estado_icon = "✅" if p['estado'] == 'Publicado' else "⏳"
            keyboard.append([InlineKeyboardButton(f"{estado_icon} {p['titulo']}", url=f"https://t.me/{application.bot.username}?start={p['id']}")])
        keyboard.append([InlineKeyboardButton("🔙 Volver al Menú", callback_data="menu_main")])
        
        await query.edit_message_text("🆕 Últimos 5 programas procesados:", reply_markup=InlineKeyboardMarkup(keyboard))
        
    elif data == "admin_panel":
        if user_id != ADMIN_ID: return
        stats = await get_stats(db_pool)
        texto = f"⚙️ Panel de Administración Interno\n\nUsuarios Activos: {stats['usuarios_totales']}\nProgramas Publicados: {stats['programas_publicados']}\nEn Cola Pendiente: {stats['cola_pendientes']}\nDescargas Servidas: {stats['descargas_totales']}"
        keyboard = [
            [InlineKeyboardButton("🔄 Gestionar Errores", callback_data="admin_errors")],
            [InlineKeyboardButton("🔙 Volver al Menú", callback_data="menu_main")]
        ]
        await query.edit_message_text(texto, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
        
    elif data == "admin_errors":
        if user_id != ADMIN_ID: return
        errores = await get_error_programs(db_pool)
        if not errores:
            await query.edit_message_text("✅ Limpio. No existen registros marcados con fallos.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver", callback_data="admin_panel")]]))
        return
            
        for e in errores:
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Reintentar", callback_data=f"retry_{e['id']}"),
                 InlineKeyboardButton("🗑️ Forzar Borrado", callback_data=f"del_{e['id']}")]
            ])
            await context.bot.send_message(chat_id=user_id, text=f"⚠️ Falla crítica: {e['titulo']}\nOrigen: {e['url_origen']}", reply_markup=kb)
            
    elif data.startswith("retry_"):
        if user_id != ADMIN_ID: return
        pid = int(data.split("_")[1])
        await update_status(db_pool, pid, "Pendiente")
        await query.edit_message_text("✅ Registro restablecido a 'Pendiente' de forma exitosa.")
        
    elif data.startswith("del_"):
        if user_id != ADMIN_ID: return
        pid = int(data.split("_")[1])
        await delete_program(db_pool, pid)
        await query.edit_message_text("🗑️ Elemento purgado de la base de datos.")

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
        
    user_id = update.effective_user.id
    text = update.message.text
    
    if user_states.get(user_id) == "waiting_search":
        resultados = await search_programs(db_pool, text)
        user_states.pop(user_id, None)
        
        if not resultados:
            keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver al Menú", callback_data="menu_main")]])
            await update.message.reply_text("❌ No se encontraron coincidencias locales con tu búsqueda.", reply_markup=keyboard)
            return
            
        keyboard = []
        for r in resultados:
            estado_icon = "✅" if r['estado'] == 'Publicado' else "⏳"
            keyboard.append([InlineKeyboardButton(f"{estado_icon} {r['titulo']}", url=f"https://t.me/{application.bot.username}?start={r['id']}")])
        keyboard.append([InlineKeyboardButton("🔙 Volver al Menú", callback_data="menu_main")])
        
        await update.message.reply_text("🔍 Coincidencias en el índice local:", reply_markup=InlineKeyboardMarkup(keyboard))

# ----------------- INICIO DE APLICACIÓN -----------------

async def main():
    global db_pool, application
    
    db_pool = await get_db_pool()
    await init_db(db_pool)
    
    application = Application.builder() \
        .token(BOT_TOKEN) \
        .base_url(TELEGRAM_API_BASE_URL) \
        .base_file_url(TELEGRAM_API_BASE_URL.replace("/bot", "/file/bot")) \
        .local_mode(True) \
        .build()
        
    application.add_handler(CommandHandler("start", start_command_handler))
    application.add_handler(CallbackQueryHandler(button_callback))
    from telegram.ext import MessageHandler, filters
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
    
    await application.initialize()
    await application.start()
    
    logger.info("Bot de Telegram iniciado en entorno asíncrono con Local API Server.")
    
    asyncio.create_task(worker_queue_processor())
    asyncio.create_task(user_queue_processor())
    asyncio.create_task(scheduled_scraper_task())
    
    await application.updater.start_polling()
    
    try:
        while True:
            await asyncio.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        await application.updater.stop()
        await application.stop()
        await application.shutdown()
        if db_pool:
            await db_pool.close()

if __name__ == "__main__":
    if os.name == 'nt':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())