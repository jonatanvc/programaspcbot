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
from scraper import scrape_github_releases, scrape_massgrave
from processor import check_disk_space, download_file, split_file_if_needed, cleanup_files, safe_upload_file

# Diccionarios de estado
user_states = {}

# Colas asíncronas
worker_queue = asyncio.Queue()       
user_download_queue = asyncio.Queue() 

# Base de datos pool
db_pool = None

# Global PTB Application
application = None

# ----------------- TAREAS EN SEGUNDO PLANO -----------------

async def scheduled_scraper_task():
    while True:
        try:
            logger.info("Scraping en curso...")
            resultados = []
            resultados.extend(await scrape_github_releases())
            resultados.extend(await scrape_massgrave()) 
            
            nuevos = 0
            for prog in resultados:
                if not await program_exists(db_pool, prog['titulo'], prog['version']):
                    await insert_program(db_pool, prog['titulo'], prog['version'], prog.get('categoria', 'Software'), prog['url_origen'])
                    nuevos += 1
                    
            logger.info(f"Scraping completado. {nuevos} agregados.")
        except Exception as e:
            logger.error(f"Error en scraper task: {e}")
            
        await asyncio.sleep(7200)

async def worker_queue_processor():
    while True:
        prog = await get_pending_programs(db_pool)
        if not prog:
            await asyncio.sleep(10)
            continue
            
        prog = prog[0]
        logger.info(f"Iniciando procesamiento de ID {prog['id']}: {prog['titulo']}")
        await update_status(db_pool, prog['id'], 'downloading')
        
        file_path = await download_file(prog['url_origen'], f"prog_{prog['id']}.zip")
        if not file_path:
            await update_status(db_pool, prog['id'], 'error')
            continue
            
        part_paths = await split_file_if_needed(file_path)
        
        file_ids = []
        for part in part_paths:
            file_id = await safe_upload_file(application.bot, PRIVATE_BACKUP_CHANNEL_ID, part, f"📦 {prog['titulo']}")
            if file_id:
                file_ids.append(file_id)
                
        if len(file_ids) == len(part_paths):
            await update_file_ids(db_pool, prog['id'], file_ids)
            await update_status(db_pool, prog['id'], 'ready')
            
            # Notificar al canal publico
            if PUBLIC_CHANNEL_ID != 0:
                texto = f"🌟 **Nuevo Programa Disponible**\n\n"
                texto += f"📌 **Título:** {prog['titulo']}\n"
                texto += f"🏷️ **Categoría:** {prog['categoria']}\n"
                texto += f"📝 **Descripción:** {prog['descripcion'][:300]}...\n\n"
                texto += f"🔗 Da clic al botón abajo para descargarlo de forma segura."
                
                keyboard = InlineKeyboardMarkup([[
                    InlineKeyboardButton("📥 Descargar Ahora", url=f"https://t.me/{application.bot.username}?start={prog['id']}")
                ]])
                
                try:
                    await application.bot.send_message(
                        chat_id=PUBLIC_CHANNEL_ID,
                        text=texto,
                        reply_markup=keyboard,
                        parse_mode='Markdown'
                    )
                except Exception as e:
                    logger.error(f"Error notificando al canal publico: {e}")
        else:
            await update_status(db_pool, prog['id'], 'error')
            
        cleanup_files(part_paths)
        if os.path.exists(file_path):
            cleanup_files([file_path])

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
                    await application.bot.send_document(
                        chat_id=user_id,
                        document=fid,
                        caption=f"{titulo} (Parte {i}/{len(file_ids)})" if len(file_ids) > 1 else titulo
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
        
        if not prog or prog['status'] != 'ready' or not prog['telegram_file_ids']:
            await update.message.reply_text("❌ Este archivo no está disponible o sigue procesándose.")
            return
            
        await update.message.reply_text("✅ Tu descarga ha sido encolada. Recibirás los archivos en breve...")
        await increment_download(db_pool, prog_id)
        
        await user_download_queue.put({
            'user_id': user_id,
            'file_ids': prog['telegram_file_ids'],
            'titulo': prog['titulo']
        })
        return
        
    await show_main_menu(update, context)

async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = "👋 ¡Hola! Soy el Gestor de Distribución de Software.\n\nElige una opción:"
    keyboard = [
        [InlineKeyboardButton("🔍 Buscar Programa", callback_data="search_prog")],
        [InlineKeyboardButton("🆕 Últimos Añadidos", callback_data="latest_prog")],
    ]
    if ADMIN_ID != 0 and update.effective_user.id == ADMIN_ID:
        keyboard.append([InlineKeyboardButton("⚙️ Panel de Admin", callback_data="admin_panel")])
        
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
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver", callback_data="menu_main")]])
        await query.edit_message_text("🔍 Escribe el nombre del programa que buscas:", reply_markup=keyboard)
        
    elif data == "latest_prog":
        progs = await get_latest_programs(db_pool, 5)
        if not progs:
            await query.edit_message_text("Todavía no hay programas disponibles.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver", callback_data="menu_main")]]))
            return
            
        keyboard = []
        for p in progs:
            keyboard.append([InlineKeyboardButton(p['titulo'], url=f"https://t.me/{application.bot.username}?start={p['id']}")])
        keyboard.append([InlineKeyboardButton("🔙 Volver", callback_data="menu_main")])
        
        await query.edit_message_text("🆕 Últimos programas subidos:", reply_markup=InlineKeyboardMarkup(keyboard))
        
    elif data == "admin_panel":
        if user_id != ADMIN_ID: return
        stats = await get_stats(db_pool)
        texto = f"⚙️ **Panel de Admin**\n\nUsuarios: {stats['usuarios']}\nProgramas (Listos): {stats['programas_ready']}\nProgramas (Error): {stats['programas_error']}\nDescargas Totales: {stats['descargas']}"
        keyboard = [
            [InlineKeyboardButton("🔄 Gestionar Errores", callback_data="admin_errors")],
            [InlineKeyboardButton("🔙 Volver", callback_data="menu_main")]
        ]
        await query.edit_message_text(texto, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
        
    elif data == "admin_errors":
        if user_id != ADMIN_ID: return
        errores = await get_error_programs(db_pool)
        if not errores:
            await query.edit_message_text("✅ No hay programas con estado de error.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver", callback_data="admin_panel")]]))
            return
            
        for e in errores:
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Reintentar", callback_data=f"retry_{e['id']}"),
                 InlineKeyboardButton("🗑️ Borrar", callback_data=f"del_{e['id']}")]
            ])
            await context.bot.send_message(chat_id=user_id, text=f"⚠️ Falla: {e['titulo']}\nOrigen: {e['url_origen']}", reply_markup=kb)
            
    elif data.startswith("retry_"):
        if user_id != ADMIN_ID: return
        pid = int(data.split("_")[1])
        await update_status(db_pool, pid, "pending")
        await query.edit_message_text("✅ Puesto en cola nuevamente.")
        
    elif data.startswith("del_"):
        if user_id != ADMIN_ID: return
        pid = int(data.split("_")[1])
        await delete_program(db_pool, pid)
        await query.edit_message_text("🗑️ Programa eliminado de la BD.")

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
        
    user_id = update.effective_user.id
    text = update.message.text
    
    if user_states.get(user_id) == "waiting_search":
        resultados = await search_programs(db_pool, text)
        user_states.pop(user_id, None)
        
        if not resultados:
            keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver", callback_data="menu_main")]])
            await update.message.reply_text("❌ No se encontraron resultados.", reply_markup=keyboard)
            return
            
        keyboard = []
        for r in resultados:
            keyboard.append([InlineKeyboardButton(f"{r['titulo']} ({r['categoria']})", url=f"https://t.me/{application.bot.username}?start={r['id']}")])
        keyboard.append([InlineKeyboardButton("🔙 Volver al Menú", callback_data="menu_main")])
        
        await update.message.reply_text("🔍 Resultados de búsqueda:", reply_markup=InlineKeyboardMarkup(keyboard))

# ----------------- INICIO -----------------

async def main():
    global db_pool, application
    
    db_pool = await get_db_pool()
    await init_db(db_pool)
    
    # Construir PTB Application apuntando al Local Bot API Server
    application = Application.builder() \
        .token(BOT_TOKEN) \
        .base_url(TELEGRAM_API_BASE_URL) \
        .base_file_url(TELEGRAM_API_BASE_URL.replace("/bot", "/file/bot")) \
        .build()
        
    application.add_handler(CommandHandler("start", start_command_handler))
    application.add_handler(CallbackQueryHandler(button_callback))
    from telegram.ext import MessageHandler, filters
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
    
    # Inicializar PTB
    await application.initialize()
    await application.start()
    
    logger.info("Bot de Telegram iniciado (Nivel Enterprise, Modo PTB + Local API).")
    
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
