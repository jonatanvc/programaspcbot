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
user_search_cache = {}
worker_queue = asyncio.Queue()       
user_download_queue = asyncio.Queue() 
db_pool = None
application = None

# ----------------- TAREAS EN SEGUNDO PLANO -----------------

async def scheduled_scraper_task():
    while True:
        try:
            logger.info("Iniciando ciclo de scraping programado...")
            
            resultados = []
            
            # Alternar entre Modo Normal y Modo Caza-ISOs
            if not hasattr(scheduled_scraper_task, 'caza_isos_mode'):
                scheduled_scraper_task.caza_isos_mode = False
                
            if scheduled_scraper_task.caza_isos_mode:
                # MODO CAZA-PREMIUM
                from scraper import scrape_premium
                resultados.extend(await scrape_premium())
                scheduled_scraper_task.caza_isos_mode = False
            else:
                # MODO NORMAL
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
                
                # Ejecutamos el extractor de FileCR
                resultados.extend(await scrape_filecr(url_a_scrapear))
                scheduled_scraper_task.caza_isos_mode = True
            
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
            
        # Ejecución cada 1 hora para mantener un backlog saludable
        await asyncio.sleep(3600)

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
                    viewport={"width": 1366, "height": 768},
                    accept_downloads=True
                )
                if "getintopc.com" in prog['url_origen']:
                    from scraper import obtener_enlace_dinamico_getintopc
                    data = await obtener_enlace_dinamico_getintopc(context, prog['url_origen'], prog['titulo'])
                else:
                    data = await obtener_enlace_dinamico(context, prog['url_origen'], prog['titulo'])
            except Exception as e:
                logger.error(f"FCR-Worker: Error extrayendo link: {e}")
            finally:
                if context:
                    try: await context.close()
                    except: pass
                if browser:
                    try: await browser.close()
                    except: pass
                
        if data and "html_dump" in data:
            dump_path = f"dump_{prog['id']}.html"
            with open(dump_path, "w", encoding="utf-8") as f:
                f.write(data["html_dump"])
            try:
                from config import ADMIN_ID
                await application.bot.send_document(chat_id=ADMIN_ID, document=open(dump_path, "rb"), caption=f"⚠️ HTML DUMP de {prog['titulo']}")
                os.remove(dump_path)
            except Exception as e:
                logger.error(f"Error enviando HTML dump: {e}")
                
        if not data or not data.get("enlace"):
            logger.error(f"FCR-Worker: No se pudo obtener el enlace dinámico para {prog['titulo']}")
            await update_status(db_pool, prog['id'], 'Error')
            return False
            
        enlace_final = data["enlace"]
        
        # 2. Actualizar metadatos en memoria y base de datos
        if data.get('titulo'):
            prog['titulo'] = data['titulo']
            
        prog['version'] = data.get('version', prog.get('version', 'Latest'))
        prog['categoria'] = data.get('categoria', prog.get('categoria', 'Software'))
        prog['descripcion'] = data.get('descripcion', prog.get('descripcion', ''))
        prog['imagen_url'] = data.get('imagen_url', prog.get('imagen_url', ''))
        prog['fecha_actualizacion'] = data.get('fecha_actualizacion', prog.get('fecha_actualizacion', 'Reciente'))
        prog['languages'] = data.get('languages', prog.get('languages', 'Multilingual'))
        prog['publisher'] = data.get('publisher', prog.get('publisher', 'Unknown'))
        
        reqs = data.get('requisitos', prog.get('requisitos', ''))
        if reqs and 'OS:' in reqs or 'Windows' in reqs or 'RAM' in reqs:
            try:
                from deep_translator import GoogleTranslator
                t = GoogleTranslator(source='auto', target='es')
                reqs = await asyncio.to_thread(t.translate, reqs)
            except Exception as e:
                logger.error(f"Error traduciendo requisitos: {e}")
        prog['requisitos'] = reqs
        
        from database import update_program_metadata, delete_program
        try:
            await update_program_metadata(db_pool, prog['id'], prog['version'], prog['categoria'], prog['descripcion'], prog['imagen_url'], prog.get('titulo'), prog['fecha_actualizacion'], prog['requisitos'], prog['languages'], prog['publisher'])
        except Exception as e:
            if "UniqueViolationError" in str(type(e).__name__):
                logger.info(f"El programa {prog['titulo']} versión {prog['version']} ya existe en la base de datos. Es un duplicado. Limpiando registro fantasma...")
                await delete_program(db_pool, prog['id'])
                return False
            else:
                logger.error(f"FCR-Worker: Error actualizando metadatos: {e}")
                await update_status(db_pool, prog['id'], 'Error')
                return False
        
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
        from processor import download_file, split_file_if_needed, cleanup_files, safe_upload_file
        file_path = await download_file(enlace_final, original_filename)
        if not file_path:
            await update_status(db_pool, prog['id'], 'Error')
            from processor import DOWNLOAD_DIR
            failed_path = os.path.join(DOWNLOAD_DIR, original_filename)
            cleanup_files([failed_path, failed_path + ".aria2"])
            if prog.get('imagen_url') and prog['imagen_url'].startswith("temp_images/") and os.path.exists(prog['imagen_url']):
                try: os.remove(prog['imagen_url'])
                except: pass
            return False
            
        part_paths = await split_file_if_needed(file_path)
        if not part_paths:
            await update_status(db_pool, prog['id'], 'Error')
            cleanup_files([file_path])
            if prog.get('imagen_url') and prog['imagen_url'].startswith("temp_images/") and os.path.exists(prog['imagen_url']):
                try: os.remove(prog['imagen_url'])
                except: pass
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
            
        version_str = prog.get('version', 'Latest')
        archivos_count = len(part_paths)
        
        async with publish_lock:
            # Notificar al canal publico
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
                t = GoogleTranslator(source='auto', target='es')
                desc = await asyncio.wait_for(asyncio.to_thread(t.translate, desc), timeout=10.0)
            except Exception as e:
                logger.error(f"Error traduciendo descripción: {e}")

            # Formateo HTML solicitado
            texto = f"<b>{prog['titulo']}</b>\n\n"
            texto += f"⚙️ <b>Versión:</b> {version_str}\n"
            if fecha_str and fecha_str != 'Reciente':
                meses_en = {'January': 'Enero', 'February': 'Febrero', 'March': 'Marzo', 'April': 'Abril', 'May': 'Mayo', 'June': 'Junio', 'July': 'Julio', 'August': 'Agosto', 'September': 'Septiembre', 'October': 'Octubre', 'November': 'Noviembre', 'December': 'Diciembre'}
                for eng, esp in meses_en.items():
                    if eng in fecha_str:
                        fecha_str = fecha_str.replace(eng, esp)
                        break
                texto += f"📅 <b>Lanzamiento:</b> {fecha_str}\n"
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
            if not imagen_url or (not imagen_url.startswith("http") and not os.path.exists(imagen_url)):
                import urllib.parse
                safe_title = urllib.parse.quote(prog['titulo'].split(' ')[0])
                imagen_url = f"https://ui-avatars.com/api/?name={safe_title}&background=random&color=fff&size=256&font-size=0.4"
            
            # Si es una ruta local, abrirla para Telegram
            photo_to_send = open(imagen_url, 'rb') if os.path.exists(imagen_url) else imagen_url
            
            channel_msg_id = None
            try:
                channel_msg = await application.bot.send_photo(
                    chat_id=PUBLIC_CHANNEL_ID,
                    photo=photo_to_send,
                    caption=texto,
                    reply_markup=keyboard,
                    parse_mode='HTML'
                )
                channel_msg_id = channel_msg.message_id
            except Exception as e:
                logger.error(f"Error notificando al canal publico: {e}")
            finally:
                if hasattr(photo_to_send, 'close'):
                    photo_to_send.close()
                if imagen_url and imagen_url.startswith("temp_images/") and os.path.exists(imagen_url):
                    try:
                        os.remove(imagen_url)
                    except:
                        pass
    else:
        await update_status(db_pool, prog['id'], 'Error')
        cleanup_files(part_paths)
        if os.path.exists(file_path):
            cleanup_files([file_path])
        if prog.get('imagen_url') and prog['imagen_url'].startswith("temp_images/") and os.path.exists(prog['imagen_url']):
            try: os.remove(prog['imagen_url'])
            except: pass
        return None
        
    cleanup_files(part_paths)
    if os.path.exists(file_path):
        cleanup_files([file_path])
        
    return channel_msg_id if 'channel_msg_id' in locals() else True

async def worker_queue_processor():
    import datetime
    # Configurar zona horaria UTC-4 (Venezuela/Caribe/Este)
    tz_caribe = datetime.timezone(datetime.timedelta(hours=-4))
    
    while True:
        try:
            now = datetime.datetime.now(tz_caribe)
            # Horario activo: 6:00 AM a 11:59 PM (12 AM)
            if now.hour < 6:
                # logger.debug(f"Fuera de horario (Actual: {now.strftime('%I:%M %p')}). Pausando subidas hasta las 6:00 AM...")
                await asyncio.sleep(600)
                continue
            progs = await get_pending_programs(db_pool, limit=1)
            if not progs:
                await asyncio.sleep(60)
                continue
            prog = progs[0]
            logger.info(f"Iniciando subida de {prog['titulo']} a Telegram (Modo goteo: 1 cada 12 min)...")
            import time
            start_time = time.time()
            try:
                await update_status(db_pool, prog['id'], 'Procesando')
                success = await process_single_program(prog)
            except Exception as e:
                logger.error(f"Error fatal procesando programa {prog['id']}: {e}")
                await update_status(db_pool, prog['id'], 'Error')
                success = False
            elapsed = time.time() - start_time
            is_priority = prog.get('estado') == 'Prioridad'
            sleep_time_calculated = max(10, 1200 - elapsed)
            
            if success:
                if is_priority:
                    logger.info(f"Programa prioritario enviado en {elapsed/60:.1f} min. Esperando {sleep_time_calculated/60:.1f} min para el próximo programa regular (pero atendiendo prioridades instantáneamente)...")
                else:
                    logger.info(f"Programa enviado con éxito en {elapsed/60:.1f} min. Esperando {sleep_time_calculated/60:.1f} min para mantener el goteo exacto de 3 por hora...")
            else:
                logger.info(f"Programa falló tras {elapsed/60:.1f} min.")
                requested_by = prog.get('requested_by')
                request_chat_id = prog.get('request_chat_id')
                status_msg_id = prog.get('status_msg_id')
                request_msg_id = prog.get('request_msg_id')
                if requested_by:
                    try:
                        if status_msg_id and request_chat_id:
                            try:
                                await application.bot.delete_message(chat_id=request_chat_id, message_id=status_msg_id)
                            except Exception as delete_e:
                                logger.error(f"Error eliminando mensaje anterior de estado (Error): {delete_e}")
                                
                        target_chat = request_chat_id if request_chat_id else requested_by
                        reply_to = request_msg_id if request_chat_id else None
                        
                        await application.bot.send_message(
                            chat_id=target_chat,
                            text=f"❌ <b>Error con tu pedido</b>\n\n<code>No pudimos descargar '{prog['titulo']}'. El servidor de origen no respondió o el enlace estaba roto.</code>",
                            parse_mode='HTML',
                            reply_to_message_id=reply_to
                        )
                    except Exception as e:
                        logger.error(f"Error notificando fallo al usuario: {e}")
            # Notificar si fue un pedido prioritario (sólo si fue éxito, el fallo se notifica arriba)
            if success:
                requested_by = prog.get('requested_by')
                request_chat_id = prog.get('request_chat_id')
                status_msg_id = prog.get('status_msg_id')
                request_msg_id = prog.get('request_msg_id')
                if requested_by:
                    try:
                        # Limpieza: eliminar el mensaje de 'Pedido Registrado' para no dejar basura en el chat
                        if status_msg_id and request_chat_id:
                            try:
                                await application.bot.delete_message(chat_id=request_chat_id, message_id=status_msg_id)
                            except Exception as e:
                                logger.error(f"No se pudo eliminar el mensaje de estado de pedido: {e}")
                                
                        target_chat = request_chat_id if request_chat_id else requested_by
                        reply_to = request_msg_id if request_chat_id else None
                        is_group = target_chat < 0
                        
                        if is_group and isinstance(success, int): # success is channel_msg_id
                            text_msg = f"🎉 <b>¡Tu pedido ya está listo!</b>\n\nEl programa <b>{prog['titulo']}</b> ha sido subido con éxito a nuestra base de datos. Haz clic en el botón abajo para descargarlo."
                            keyboard = InlineKeyboardMarkup([[
                                InlineKeyboardButton("📥 Descargar Ahora", url=f"https://t.me/{application.bot.username}?start={prog['id']}")
                            ]])
                            await application.bot.send_message(
                                chat_id=target_chat,
                                text=text_msg,
                                parse_mode='HTML',
                                reply_markup=keyboard,
                                reply_to_message_id=reply_to
                            )
                            logger.info(f"Notificación de grupo enviada a {target_chat} para el programa {prog['id']}")
                        else:
                            await application.bot.send_message(
                                chat_id=target_chat,
                                text=f"🎉 <b>¡Tu pedido ha sido procesado!</b>\n\nEl programa <b>{prog['titulo']}</b> ya está disponible en nuestra base de datos. Haz clic en el botón abajo para descargarlo de inmediato.",
                                parse_mode='HTML',
                                reply_markup=InlineKeyboardMarkup([[
                                    InlineKeyboardButton("📥 Descargar Ahora", url=f"https://t.me/{application.bot.username}?start={prog['id']}")
                                ]]),
                                reply_to_message_id=reply_to
                            )
                            logger.info(f"Notificación privada enviada al chat {target_chat} para el programa {prog['id']}")
                    except Exception as e:
                        logger.error(f"No se pudo enviar notificación de prioridad al chat {target_chat}: {e}")
            # Sueño interrumpible
            from database import count_priority_queue
            
            # Independientemente de si fue prioridad o no, si tuvimos éxito y subimos algo,
            # debemos esperar el intervalo completo para mantener el goteo en el canal público.
            
            for _ in range(int(sleep_time_calculated / 10)):
                await asyncio.sleep(10)
                priorities = await count_priority_queue(db_pool)
                if priorities > 0:
                    logger.info("¡Llegó un pedido prioritario! Interrumpiendo sueño para procesarlo al instante.")
                    break

        except Exception as master_e:
            logger.error(f"Fallo catastrófico en worker_queue_processor: {master_e}")
            await asyncio.sleep(60)


async def user_queue_processor():
    while True:
        try:
            task = await user_download_queue.get()
            user_id = task['user_id']
            file_ids = task['file_ids']
            titulo = task['titulo']
            status_msg_id = task.get('status_msg_id')
            prog_id = task.get('prog_id')
            
            for i, fid in enumerate(file_ids, 1):
                try:
                    await asyncio.sleep(1.5)
                    user_caption = f"<b>{titulo}</b>"
                    if len(file_ids) > 1:
                        user_caption += f" Parte {i}"
                        
                    # Añadir requisitos si es la primera parte o el archivo único
                    if i == 1:
                        if task.get('requisitos'):
                            user_caption += f"\n\n🖥 <b>Requisitos del Sistema:</b>\n"
                            user_caption += f"<blockquote>{task['requisitos']}</blockquote>"
                        user_caption += "\n🔑 <b>Contraseña:</b> <code>123</code>"
                        
                    await application.bot.send_document(
                        chat_id=user_id,
                        document=fid,
                        caption=user_caption,
                        parse_mode='HTML'
                    )
                except Exception as e:
                    from telegram.error import Forbidden
                    if isinstance(e, Forbidden):
                        logger.warning(f"Usuario {user_id} bloqueó el bot. Abortando envío.")
                        break
                    logger.warning(f"Error enviando documento a {user_id}: {e}")
                    try:
                        await application.bot.send_message(
                            chat_id=user_id, 
                            text="❌ <b>Error:</b> El archivo no se encuentra disponible (posiblemente el bot se reinició o el ID expiró). Lo hemos marcado con error. Por favor, busca el programa nuevamente y solicítalo para que se vuelva a subir.", 
                            parse_mode='HTML'
                        )
                        if prog_id:
                            from database import update_status
                            # Use global db_pool directly
                            await update_status(db_pool, prog_id, 'Error')
                            await db_pool.execute("UPDATE programas SET telegram_file_ids = NULL WHERE id = $1", prog_id)
                    except Exception as inner_e:
                        logger.error(f"Error marcando programa {prog_id} como error: {inner_e}")
            
            # Borrar mensaje de status "extrayendo de base de datos..."
            if status_msg_id:
                try:
                    await application.bot.delete_message(chat_id=user_id, message_id=status_msg_id)
                except: pass
                    
            user_download_queue.task_done()
        except Exception as e:
            logger.error(f"Error en user_queue_processor: {e}")
            await asyncio.sleep(5)

# ----------------- HANDLERS PTB -----------------

async def start_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    lang = update.effective_user.language_code or 'es'
    await register_user(db_pool, user_id)
    
    # Textos por defecto en español
    msg_not_found = "❌ <b>¡El programa solicitado no existe en nuestra base de datos!</b> 🗄️\n\n<code>Asegúrese de usar los enlaces oficiales de nuestro canal.</code>"
    msg_processing = "⚠️ <b>¡El programa {titulo} aún se está extrayendo hacia la base de datos central!</b> 🗄️\n\n<blockquote>Aún no está listo para su descarga. Por favor, vuelva a intentarlo en un par de minutos.</blockquote>"
    msg_queued = "⏳ <b>¡Su programa está siendo extraído de la base de datos!</b> 🗄️\n\n<code>Por favor espere unos instantes mientras preparamos sus archivos...</code>"
    msg_welcome = "🤖 <b>¡Bienvenido al Gestor Automatizado de Software!</b>\n\n<code>Utilice los botones a continuación para explorar nuestra base de datos.</code>"
    
    # Traducciones rápidas usando diccionario si no es 'es'
    if not lang.startswith('es'):
        try:
            from deep_translator import GoogleTranslator
            t = GoogleTranslator(source='es', target=lang)
            msg_not_found = await asyncio.to_thread(t.translate, "El programa no existe en nuestra base de datos")
            msg_not_found = f"❌ <b>{msg_not_found}</b> 🗄️"
            msg_processing = await asyncio.to_thread(t.translate, "El programa todavía está en proceso de subida a la base de datos")
            msg_processing = f"⚠️ <b>{msg_processing}</b> 🗄️"
            msg_queued = await asyncio.to_thread(t.translate, "Su programa está siendo extraído de la base de datos")
            msg_queued = f"⏳ <b>{msg_queued}...</b> 🗄️"
            msg_welcome = await asyncio.to_thread(t.translate, "Bienvenido al gestor de software")
            msg_welcome = f"🤖 <b>{msg_welcome}</b>"
        except Exception as e:
            logger.error(f"Error traduciendo al idioma {lang}: {e}")

    args = context.args
    
    # Auto-delete user's command if possible
    try:
        await update.message.delete()
    except:
        pass
        
    if args and args[0].isdigit():
        prog_id = int(args[0])
        prog = await get_program_by_id(db_pool, prog_id)
        
        if not prog:
            await context.bot.send_message(chat_id=user_id, text=msg_not_found, parse_mode='HTML')
            return
            
        if prog['estado'] != 'Publicado' or not prog['telegram_file_ids']:
            await context.bot.send_message(chat_id=user_id, text=msg_processing.format(titulo=prog['titulo']), parse_mode='HTML')
            return
            
        import json
        try:
            file_ids_list = json.loads(prog['telegram_file_ids'])
        except Exception as e:
            logger.error(f"Error parseando telegram_file_ids para {prog_id}: {e}")
            await context.bot.send_message(chat_id=user_id, text="❌ <b>Error interno:</b> Los archivos de este programa están corruptos. Por favor, solicita el programa de nuevo.", parse_mode='HTML')
            from database import update_status
            await update_status(db_pool, prog_id, 'Error')
            await db_pool.execute("UPDATE programas SET telegram_file_ids = NULL WHERE id = $1", prog_id)
            return
            
        status_msg = await context.bot.send_message(chat_id=user_id, text=msg_queued, parse_mode='HTML')
        await increment_download(db_pool, prog_id)
        
        await user_download_queue.put({
            'user_id': user_id,
            'prog_id': prog_id,
            'file_ids': file_ids_list,
            'titulo': prog['titulo'],
            'requisitos': prog.get('requisitos', ''),
            'status_msg_id': status_msg.message_id
        })
        
        # Registrar o actualizar estadística de descargas del usuario
        try:
            from database import increment_user_download
            username = update.effective_user.username or ''
            if username:
                username = f"@{username}"
            else:
                username = update.effective_user.first_name or str(user_id)
            await increment_user_download(db_pool, user_id, username)
        except Exception as e:
            logger.error(f"Error registrando descarga del usuario {user_id}: {e}")
            
        return
        
    await show_main_menu(update, context, welcome_text=msg_welcome)

async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, welcome_text=None):
    user_id = update.effective_user.id
    lang = update.effective_user.language_code or 'es'
    
    texto = welcome_text if welcome_text else "🤖 <b>¡Bienvenido al Gestor de Software!</b>\n\n<code>Selecciona una opción del menú para explorar nuestro catálogo de programas, todo listo para instalar y sin publicidad.</code>\n\n<i>¿Qué te gustaría hacer hoy?</i>"
    btn_search = "🔍 Buscar"
    btn_categories = "📂 Categorías"
    btn_latest = "🆕 Novedades"
    btn_top = "🔥 Tendencias"
    btn_top_users = "🏆 Leaderboard"
    btn_guide = "📖 Guía de Uso"
    btn_admin = "⚙️ Admin Panel"
    
    if not lang.startswith('es') and not welcome_text:
        try:
            from deep_translator import GoogleTranslator
            t = GoogleTranslator(source='es', target=lang)
            texto = await asyncio.to_thread(t.translate, "Bienvenido al Gestor de Software")
            texto = f"🤖 <b>{texto}</b>"
            btn_search = await asyncio.to_thread(t.translate, btn_search)
            btn_categories = await asyncio.to_thread(t.translate, btn_categories)
            btn_latest = await asyncio.to_thread(t.translate, btn_latest)
            btn_guide = await asyncio.to_thread(t.translate, btn_guide)
            btn_top = await asyncio.to_thread(t.translate, btn_top)
            btn_top_users = await asyncio.to_thread(t.translate, btn_top_users)
        except:
            pass

    keyboard = [
        [InlineKeyboardButton(btn_search, callback_data="search_prog"), InlineKeyboardButton(btn_categories, callback_data="menu_categories")],
        [InlineKeyboardButton(btn_latest, callback_data="latest_prog"), InlineKeyboardButton(btn_top, callback_data="top_prog")],
        [InlineKeyboardButton(btn_top_users, callback_data="top_users"), InlineKeyboardButton(btn_guide, callback_data="show_guide")]
    ]
    if ADMIN_ID != 0 and user_id == ADMIN_ID:
        keyboard.append([InlineKeyboardButton(btn_admin, callback_data="admin_panel")])
        
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    last_msg_id = user_states.get(f"{user_id}_menu_msg")
    
    if update.callback_query:
        try:
            await update.callback_query.edit_message_text(texto, reply_markup=reply_markup, parse_mode='HTML')
        except:
            pass
    elif last_msg_id:
        try:
            await context.bot.edit_message_text(chat_id=user_id, message_id=last_msg_id, text=texto, reply_markup=reply_markup, parse_mode='HTML')
        except:
            msg = await context.bot.send_message(chat_id=user_id, text=texto, reply_markup=reply_markup, parse_mode='HTML')
            user_states[f"{user_id}_menu_msg"] = msg.message_id
    else:
        msg = await context.bot.send_message(chat_id=user_id, text=texto, reply_markup=reply_markup, parse_mode='HTML')
        user_states[f"{user_id}_menu_msg"] = msg.message_id

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from telegram.error import BadRequest
    try:
        await _button_callback_impl(update, context)
    except BadRequest as e:
        if "Message is not modified" in str(e):
            pass
        else:
            logger.error(f"Telegram BadRequest in callback: {e}")
    except Exception as e:
        logger.error(f"Error in callback: {e}")

async def _button_callback_impl(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    user_id = update.effective_user.id
    
    await query.answer()
    
    if data == "menu_main":
        user_states.pop(user_id, None)
        await show_main_menu(update, context)
        
    elif data == "menu_categories":
        keyboard = [
            [InlineKeyboardButton("🎨 Diseño y Foto", callback_data="cat_Design"), InlineKeyboardButton("💻 Sistemas", callback_data="cat_Operating")],
            [InlineKeyboardButton("🎬 Multimedia", callback_data="cat_Video"), InlineKeyboardButton("🛡️ Seguridad", callback_data="cat_Antivirus")],
            [InlineKeyboardButton("🧰 Utilidades", callback_data="cat_Utilities"), InlineKeyboardButton("🌐 Redes", callback_data="cat_Network")],
            [InlineKeyboardButton("🔙 Volver al Menú", callback_data="menu_main")]
        ]
        await query.edit_message_text("📂 <b>Selecciona una Categoría:</b> 🗄️\n\n<code>Explora nuestro catálogo por secciones:</code>", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
        
    elif data.startswith("cat_"):
        cat = data.split("_")[1]
        user_search_cache[user_id] = {'query': cat, 'type': 'category'}
        await render_category_results(update, context, user_id, cat, page=1)
        
    elif data.startswith("page_search_"):
        page = int(data.split("_")[2])
        query_text = user_search_cache.get(user_id, {}).get('query', '')
        if query_text:
            await render_search_results(update, context, user_id, query_text, page=page)
            
    elif data.startswith("page_cat_"):
        page = int(data.split("_")[2])
        cat = user_search_cache.get(user_id, {}).get('query', '')
        if cat:
            await render_category_results(update, context, user_id, cat, page=page)
            
    elif data.startswith("page_multi_"):
        parts = data.split("_")
        session_id = parts[2]
        page = int(parts[3])
        await render_multi_search_page(query, session_id, user_id, page=page, is_callback=True)
        
    elif data.startswith("cancel_multi_"):
        await query.message.delete()
        
    elif data.startswith("sel_src_"):
        # Format: sel_src_sessionID_idx
        parts = data.split("_")
        session_id = parts[2]
        idx = int(parts[3])
        
        cache_data = user_search_cache.get(session_id)
        if not cache_data:
            await query.answer("Sesión de búsqueda expirada.", show_alert=True)
            return
            
        cached_results = cache_data.get('results', [])
        if idx >= len(cached_results):
            return
            
        item = cached_results[idx]
        
        from database import insert_program, update_status, get_program_by_id, count_priority_queue, get_priority_queue_names
        
        req_msg_id = query.message.reply_to_message.message_id if query.message.reply_to_message else None
        req_chat_id = query.message.chat.id
        stat_msg_id = query.message.message_id

        if item['type'] == 'db':
            prog_id = item['data']['id']
            prog = await get_program_by_id(db_pool, prog_id)
            is_published_and_ready = prog['estado'] == 'Publicado' and prog.get('telegram_file_ids') and prog.get('telegram_file_ids') not in ['[]', '']
            
            if prog['estado'] in ['Pendiente', 'Error'] or (prog['estado'] == 'Publicado' and not is_published_and_ready):
                await db_pool.execute("UPDATE programas SET estado = 'Prioridad', requested_by = $1, request_msg_id = $2, request_chat_id = $3, status_msg_id = $4 WHERE id = $5;", user_id, req_msg_id, req_chat_id, stat_msg_id, prog_id)
                titulo = prog['titulo']
            elif is_published_and_ready:
                kb = [[InlineKeyboardButton(f"📥 Descargar '{prog['titulo']}'", url=f"https://t.me/{application.bot.username}?start={prog_id}")]]
                await query.edit_message_text(f"✅ <b>¡Programa Encontrado!</b>\n\nEste programa ya se encuentra en nuestro catálogo. Haz clic en el botón abajo para obtenerlo:", reply_markup=InlineKeyboardMarkup(kb), parse_mode='HTML')
                return
            else:
                titulo = prog['titulo']
                # Ya está procesando o en prioridad
                pass
        else:
            data_item = item['data']
            titulo = data_item['titulo']
            await insert_program(
                db_pool, 
                titulo=data_item['titulo'], 
                version=data_item['version'], 
                categoria=data_item['categoria'], 
                url_origen=data_item['url_origen'], 
                descripcion=data_item.get('descripcion', ''),
                imagen_url=data_item.get('imagen_url', ''),
                fecha_actualizacion=data_item.get('fecha_actualizacion', 'Reciente'),
                requisitos=data_item.get('requisitos', ''),
                languages=data_item.get('idiomas', 'Multilingual'),
                publisher=data_item.get('publisher', 'Unknown'),
                estado='Prioridad',
                requested_by=user_id,
                request_msg_id=req_msg_id,
                request_chat_id=req_chat_id,
                status_msg_id=stat_msg_id
            )
            
        cola = await count_priority_queue(db_pool)
        if cola <= 1:
            text_msg = f"✅ <b>¡Pedido Registrado!</b>\n\n<code>Hemos pausado todo para priorizar tu pedido: '{titulo}'. Lo estamos descargando y subiendo AHORA MISMO. Te enviaremos el link cuando esté listo.</code>"
        else:
            nombres_cola = await get_priority_queue_names(db_pool, 3)
            lista_nombres = "\n".join([f"• {n}" for n in nombres_cola])
            text_msg = f"✅ <b>¡Pedido Registrado!</b>\n\n<code>Hemos registrado tu pedido: '{titulo}'. Estás en la posición #{cola} de la cola prioritaria. Te enviaremos el link en cuanto esté listo.</code>\n\n<b>⏳ Procesando actualmente:</b>\n<i>{lista_nombres}</i>"
            
        await query.edit_message_text(text_msg, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver al Menú", callback_data="menu_main")]]), parse_mode='HTML')
        
    elif data.startswith("req_"):
        termino_original = data[4:]
        termino = termino_original.lower()
        require_os = False
        
        if "#os" in termino:
            require_os = True
            termino = termino.replace("#os", "").strip()
            
        for word in ["descargar", "quiero", "necesito", "programa", "buscar", "por favor", "el", "la", "un", "una"]:
            termino = termino.replace(word, "").strip()
            
        if not termino:
            termino = termino_original
            
        await query.edit_message_text(f"🔍 <b>Buscando '{termino.title()}'...</b>\n\n<code>Por favor espera unos segundos mientras revisamos nuestras fuentes de software...</code>", parse_mode='HTML')
        await process_multi_source_search(termino, query, query.message.chat.id, user_id, require_os)
        
    elif data == "search_prog":
        user_states[user_id] = "waiting_search"
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver al Menú", callback_data="menu_main")]])
        await query.edit_message_text("🔍 <b>¿Qué software necesitas hoy?</b> 🗄️\n\n<code>Escribe el nombre del programa a continuación para buscarlo en la Base de Datos...</code>", reply_markup=keyboard, parse_mode='HTML')
        
    elif data == "latest_prog":
        progs = await get_latest_programs(db_pool, 5)
        if not progs:
            await query.edit_message_text("❌ <b>¡Nuestra Base de Datos está vacía por ahora!</b> 🗄️\n\n<code>Pronto añadiremos nuevo software.</code>", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver", callback_data="menu_main")]]), parse_mode='HTML')
            return
            
        keyboard = []
        for p in progs:
            estado_icon = "✅" if p['estado'] == 'Publicado' else "⏳"
            keyboard.append([InlineKeyboardButton(f"{estado_icon} {p['titulo']}", url=f"https://t.me/{application.bot.username}?start={p['id']}")])
        keyboard.append([InlineKeyboardButton("🔙 Volver al Menú", callback_data="menu_main")])
        
        await query.edit_message_text("🆕 <b>Últimos 5 programas añadidos a la Base de Datos:</b> 🗄️", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
        
    elif data == "top_prog":
        from database import get_top_programs
        progs = await get_top_programs(db_pool, 10)
        if not progs:
            await query.edit_message_text("❌ <b>¡Aún no hay suficientes datos de descargas!</b> 🗄️", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver", callback_data="menu_main")]]), parse_mode='HTML')
            return
            
        keyboard = []
        for i, p in enumerate(progs, 1):
            keyboard.append([InlineKeyboardButton(f"{i}. {p['titulo']} ({p['descargas_count']} DLs)", url=f"https://t.me/{application.bot.username}?start={p['id']}")])
        keyboard.append([InlineKeyboardButton("🔙 Volver al Menú", callback_data="menu_main")])
        
        await query.edit_message_text("🔥 <b>Top 10 Programas Más Populares:</b> 🗄️", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
        
    elif data == "top_users":
        from database import get_top_users
        users = await get_top_users(db_pool, 10)
        if not users:
            await query.edit_message_text("❌ <b>¡Aún no hay descargas registradas!</b> 🗄️", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver", callback_data="menu_main")]]), parse_mode='HTML')
            return
            
        ranking_text = "🏆 <b>Top 10 Usuarios Más Activos:</b>\n\n"
        medallas = ["🥇", "🥈", "🥉", "🏅", "🏅", "🏅", "🏅", "🏅", "🏅", "🏅"]
        for i, u in enumerate(users):
            username_display = u['username'] if u['username'] else f"ID: {u['telegram_id']}"
            ranking_text += f"{medallas[i]} <b>{username_display}</b> - {u['descargas_count']} descargas\n"
            
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver al Menú", callback_data="menu_main")]])
        await query.edit_message_text(ranking_text, reply_markup=keyboard, parse_mode='HTML')
        
    elif data == "show_guide":
        texto = (
            "📖 <b>Guía Básica de Uso</b> 🤖\n\n"
            "¡Bienvenido! Usar este bot es muy sencillo:\n\n"
            "1️⃣ <b>Busca tu programa:</b> Toca en <i>'🔍 Buscar Programa'</i> y escribe el nombre de lo que necesitas (ej: <code>Photoshop</code> o <code>Windows</code>).\n"
            "2️⃣ <b>Selecciona tu programa:</b> Verás una lista de resultados, presiona el botón con el link directo.\n"
            "3️⃣ <b>Descarga Privada:</b> Al tocar el link, el bot te enviará todos los archivos ZIP directamente por mensaje privado. ¡Libre de publicidad!\n\n"
            "<i>💡 Consejo: A veces los archivos se dividen en partes (Parte 1, Parte 2). Descarga todas en una misma carpeta y descomprime la parte 1 para obtener todo.</i>"
        )
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver al Menú", callback_data="menu_main")]])
        await query.edit_message_text(texto, reply_markup=keyboard, parse_mode='HTML')
        
    elif data == "admin_panel":
        if user_id != ADMIN_ID: return
        stats = await get_stats(db_pool)
        texto = f"⚙️ <b>Panel de Administración Interno</b> 🗄️\n\n"
        texto += f"👥 <b>Usuarios Activos:</b> <code>{stats['usuarios_totales']}</code>\n"
        texto += f"📦 <b>Programas Publicados:</b> <code>{stats['programas_publicados']}</code>\n"
        texto += f"⏳ <b>En Cola Pendiente:</b> <code>{stats['cola_pendientes']}</code>\n"
        texto += f"🚀 <b>Descargas Servidas:</b> <code>{stats['descargas_totales']}</code>\n"
        texto += f"📝 <b>Peticiones Totales:</b> <code>{stats.get('peticiones_totales', 0)}</code>\n"
        texto += f"⚠️ <b>Programas Fallidos:</b> <code>{stats.get('programas_error', 0)}</code>"
        
        keyboard = [
            [InlineKeyboardButton("📢 Difusión Masiva", callback_data="admin_broadcast")],
            [InlineKeyboardButton("🔄 Gestionar Errores", callback_data="admin_errors")],
            [InlineKeyboardButton("🔙 Volver al Menú", callback_data="menu_main")]
        ]
        await query.edit_message_text(texto, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
        
    elif data == "admin_broadcast":
        if user_id != ADMIN_ID: return
        user_states[user_id] = "waiting_broadcast"
        await query.edit_message_text("📢 <b>Modo de Difusión Masiva Activado</b>\n\n<code>Escribe el mensaje que deseas enviar a TODOS los usuarios registrados. Si quieres incluir imágenes, envía un mensaje con foto y texto.\n\nEscribe 'cancelar' para salir.</code>", parse_mode='HTML', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Cancelar", callback_data="admin_panel")]]))
        
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

async def render_search_results(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id, query_text, page=1):
    limit = 10
    offset = (page - 1) * limit
    resultados = await search_programs(db_pool, query_text, limit=limit + 1, offset=offset)
    
    has_next = len(resultados) > limit
    if has_next:
        resultados = resultados[:limit]
        
    last_msg_id = user_states.get(f"{user_id}_menu_msg")
    
    if not resultados and page == 1:
        req_data = f"req_{query_text[:40]}"
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🙋‍♂️ ¡Pedir este programa al bot!", callback_data=req_data)],
            [InlineKeyboardButton("🔙 Volver al Menú", callback_data="menu_main")]
        ])
        error_text = f"❌ <b>No tenemos '{query_text}' en la base de datos... ¡TODAVÍA!</b>\n\n<code>Puedes usar el botón de abajo para que el bot lo busque en internet y lo suba automáticamente por ti con prioridad.</code>"
        if last_msg_id:
            try:
                await context.bot.edit_message_text(chat_id=user_id, message_id=last_msg_id, text=error_text, reply_markup=keyboard, parse_mode='HTML')
                return
            except: pass
        await context.bot.send_message(chat_id=user_id, text=error_text, reply_markup=keyboard, parse_mode='HTML')
        return
        
    keyboard = []
    for r in resultados:
        estado_icon = "✅" if r['estado'] == 'Publicado' else "⏳"
        keyboard.append([InlineKeyboardButton(f"{estado_icon} {r['titulo']}", url=f"https://t.me/{application.bot.username}?start={r['id']}")])
        
    nav_buttons = []
    if page > 1:
        nav_buttons.append(InlineKeyboardButton("⬅️ Anterior", callback_data=f"page_search_{page-1}"))
    if has_next:
        nav_buttons.append(InlineKeyboardButton("Siguiente ➡️", callback_data=f"page_search_{page+1}"))
        
    if nav_buttons:
        keyboard.append(nav_buttons)
        
    req_data = f"req_{query_text[:40]}"
    keyboard.append([InlineKeyboardButton("🙋‍♂️ ¿No está aquí? ¡Pídeselo al bot!", callback_data=req_data)])
    keyboard.append([InlineKeyboardButton("🔙 Volver al Menú", callback_data="menu_main")])
    
    success_text = f"🔍 <b>Resultados para '{query_text}' (Página {page}):</b> 🗄️\n\n<code>Seleccione un programa de la lista:</code>"
    if last_msg_id:
        try:
            await context.bot.edit_message_text(chat_id=user_id, message_id=last_msg_id, text=success_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
            return
        except: pass
    
    new_msg = await context.bot.send_message(chat_id=user_id, text=success_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
    user_states[f"{user_id}_menu_msg"] = new_msg.message_id

async def render_category_results(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id, category, page=1):
    limit = 10
    offset = (page - 1) * limit
    from database import search_programs_by_category
    resultados = await search_programs_by_category(db_pool, category, limit=limit + 1, offset=offset)
    
    has_next = len(resultados) > limit
    if has_next:
        resultados = resultados[:limit]
        
    last_msg_id = user_states.get(f"{user_id}_menu_msg")
    
    if not resultados and page == 1:
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver al Menú", callback_data="menu_categories")]])
        error_text = f"❌ <b>¡No hay programas en la categoría '{category}' todavía!</b> 🗄️"
        if last_msg_id:
            try:
                await context.bot.edit_message_text(chat_id=user_id, message_id=last_msg_id, text=error_text, reply_markup=keyboard, parse_mode='HTML')
                return
            except: pass
        await context.bot.send_message(chat_id=user_id, text=error_text, reply_markup=keyboard, parse_mode='HTML')
        return
        
    keyboard = []
    for r in resultados:
        estado_icon = "✅" if r['estado'] == 'Publicado' else "⏳"
        keyboard.append([InlineKeyboardButton(f"{estado_icon} {r['titulo']}", url=f"https://t.me/{application.bot.username}?start={r['id']}")])
        
    nav_buttons = []
    if page > 1:
        nav_buttons.append(InlineKeyboardButton("⬅️ Anterior", callback_data=f"page_cat_{page-1}"))
    if has_next:
        nav_buttons.append(InlineKeyboardButton("Siguiente ➡️", callback_data=f"page_cat_{page+1}"))
        
    if nav_buttons:
        keyboard.append(nav_buttons)
        
    keyboard.append([InlineKeyboardButton("🔙 Volver a Categorías", callback_data="menu_categories")])
    
    success_text = f"📂 <b>Categoría: {category} (Página {page}):</b> 🗄️\n\n<code>Seleccione un programa:</code>"
    if last_msg_id:
        try:
            await context.bot.edit_message_text(chat_id=user_id, message_id=last_msg_id, text=success_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
            return
        except: pass
    
    new_msg = await context.bot.send_message(chat_id=user_id, text=success_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
    user_states[f"{user_id}_menu_msg"] = new_msg.message_id

import uuid

async def process_multi_source_search(query_text, update_or_query, chat_id, user_id, require_os):
    from scraper import scrape_search_multiple_fcr, scrape_search_multiple_gpc
    from database import search_programs, insert_peticion
    
    # Obtener límite amplio para la paginación global (ej: 10 de cada fuente)
    db_results = await search_programs(db_pool, query_text, limit=10, require_os=require_os)
    fcr_results = await scrape_search_multiple_fcr(query_text, require_os=require_os, limit=10)
    gpc_results = await scrape_search_multiple_gpc(query_text, require_os=require_os, limit=10)
    
    if not db_results and not fcr_results and not gpc_results:
        await insert_peticion(db_pool, user_id, query_text)
        text = f"⚠️ <b>No encontrado</b>\n\n<code>Lamentablemente no encontramos '{query_text}' en nuestras fuentes. Lo hemos guardado en la lista de deseos para revisión manual.</code>"
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver al Menú", callback_data="menu_main")]])
        if hasattr(update_or_query, 'edit_message_text'):
            await update_or_query.edit_message_text(text, reply_markup=keyboard, parse_mode='HTML')
        else:
            await update_or_query.edit_text(text, parse_mode='HTML')
        return

    # Generate session ID for cache
    session_id = str(uuid.uuid4())[:8]
    
    # Flatten all results into a single list
    all_results = []
    
    for res in db_results:
        all_results.append({"type": "db", "data": dict(res)})
        
    for res in fcr_results:
        all_results.append({"type": "fcr", "data": res})
        
    for res in gpc_results:
        all_results.append({"type": "gpc", "data": res})
        
    user_search_cache[session_id] = {
        'query': query_text,
        'results': all_results
    }
    
    # Check if we should render inline (callback) or reply (command)
    is_callback = hasattr(update_or_query, 'edit_message_text')
    
    await render_multi_search_page(update_or_query, session_id, user_id, page=1, is_callback=is_callback)

async def render_multi_search_page(update_or_query, session_id, user_id, page=1, is_callback=True):
    cache_data = user_search_cache.get(session_id)
    if not cache_data:
        if is_callback:
            await update_or_query.answer("Sesión de búsqueda expirada.", show_alert=True)
        return
        
    all_results = cache_data['results']
    query_text = cache_data['query']
    
    per_page = 7
    total_pages = (len(all_results) + per_page - 1) // per_page
    if page < 1: page = 1
    if page > total_pages: page = total_pages
    
    start_idx = (page - 1) * per_page
    end_idx = start_idx + per_page
    page_results = all_results[start_idx:end_idx]
    
    keyboard = []
    
    for idx, item in enumerate(page_results):
        actual_idx = start_idx + idx
        if item['type'] == 'db':
            estado = item['data']['estado']
            estado_icon = "✅" if estado == 'Publicado' else "⏳"
            btn_text = f"{item['data']['titulo']} ({estado_icon})"
        else:
            btn_text = f"{item['data']['titulo']} {item['data']['version']}"
            
        # Limitar longitud para evitar errores de Telegram
        if len(btn_text) > 40:
            btn_text = btn_text[:37] + "..."
            
        keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"sel_src_{session_id}_{actual_idx}")])
        
    # Navigation buttons
    nav_buttons = []
    if page > 1:
        nav_buttons.append(InlineKeyboardButton("⬅️ Anterior", callback_data=f"page_multi_{session_id}_{page-1}"))
    if page < total_pages:
        nav_buttons.append(InlineKeyboardButton("Siguiente ➡️", callback_data=f"page_multi_{session_id}_{page+1}"))
        
    if nav_buttons:
        keyboard.append(nav_buttons)
        
    # Cancel button
    if is_callback:
        keyboard.append([InlineKeyboardButton("❌ Cancelar", callback_data=f"cancel_multi_{session_id}")])
        
    text = f"🔍 <b>Resultados para '{query_text.title()}' (Pág {page}/{total_pages}):</b>\n\n<code>Selecciona la versión exacta:</code>"
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    if is_callback:
        try:
            await update_or_query.edit_message_text(text, reply_markup=reply_markup, parse_mode='HTML')
        except:
            pass
    else:
        try:
            await update_or_query.edit_text(text, reply_markup=reply_markup, parse_mode='HTML')
        except:
            pass

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
        
    user_id = update.effective_user.id
    chat_type = update.message.chat.type
    
    # --- LÓGICA DE GRUPOS ---
    if chat_type in ['group', 'supergroup']:
        text = update.message.text
        if not text:
            return
            
        lower_text = text.lower().strip()
        if lower_text.startswith("#programa ") or lower_text.startswith("#os "):
            require_os = lower_text.startswith("#os ")
            
            termino = text.lower().replace("#programa", "").replace("#os", "").strip()
            termino_original = termino
            
            for word in ["descargar", "quiero", "necesito", "programa", "buscar", "por favor", "el", "la", "un", "una"]:
                termino = termino.replace(word, "").strip()
                
            if not termino:
                termino = termino_original
                
            msg = await update.message.reply_text(f"🔍 <b>Buscando '{termino.title()}'...</b>\n\n<code>Espera unos segundos...</code>", parse_mode='HTML', reply_to_message_id=update.message.message_id)
            await process_multi_source_search(termino, msg, update.message.chat.id, user_id, require_os)
            
        return
    # --- FIN LÓGICA DE GRUPOS ---
    
    if user_states.get(user_id) == "waiting_broadcast":
        user_states.pop(user_id, None)
        text = update.message.text or update.message.caption or ""
        if text.lower().strip() == 'cancelar':
            await context.bot.send_message(chat_id=user_id, text="❌ Difusión Masiva cancelada.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver al Panel", callback_data="admin_panel")]]))
            return
            
        from database import get_all_users
        users = await get_all_users(db_pool)
        
        status_msg = await context.bot.send_message(chat_id=user_id, text=f"🚀 <b>Iniciando envío masivo a <code>{len(users)}</code> usuarios...</b>", parse_mode='HTML')
        
        from telegram.error import Forbidden, BadRequest
        count = 0
        fallos = 0
        for uid in users:
            try:
                await update.message.copy(chat_id=uid)
                count += 1
                await asyncio.sleep(0.05) # Límite API Telegram
            except (Forbidden, BadRequest):
                fallos += 1
            except Exception as e:
                logger.warning(f"Error inesperado enviando broadcast a {uid}: {e}")
                fallos += 1
                await asyncio.sleep(0.05)
                
        await context.bot.edit_message_text(chat_id=user_id, message_id=status_msg.message_id, text=f"✅ <b>¡Broadcast Completado!</b>\n\n<code>Mensajes entregados: {count}</code>\n<code>Usuarios que bloquearon al bot: {fallos}</code>", parse_mode='HTML', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver al Panel", callback_data="admin_panel")]]))
        return
        
    if not update.message.text: return
    text = update.message.text
    
    try:
        await update.message.delete()
    except: pass
    
    if user_states.get(user_id) == "waiting_search":
        user_states.pop(user_id, None)
        user_search_cache[user_id] = {'query': text, 'type': 'search'}
        await render_search_results(update, context, user_id, text, page=1)

# ----------------- ADMIN HANDLERS -----------------

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    
    from database import get_total_users, get_total_programs
    import psutil
    
    users = await get_total_users(db_pool)
    progs = await get_total_programs(db_pool)
    
    total_progs = sum(progs.values())
    published = progs.get('Publicado', 0)
    
    cpu = psutil.cpu_percent()
    ram = psutil.virtual_memory().percent
    disk = psutil.disk_usage('/').percent
    
    # Auto-delete user's command if possible
    try:
        await update.message.delete()
    except: pass
    
    texto = f"📊 <b>Panel de Estadísticas de Servidor</b> 🗄️\n\n"
    texto += f"👥 <b>Usuarios Totales:</b> <code>{users}</code>\n"
    texto += f"📦 <b>Programas Totales:</b> <code>{total_progs} ({published} Publicados)</code>\n\n"
    texto += f"🖥️ <b>Estado del VPS:</b>\n"
    texto += f"<blockquote>CPU: {cpu}%\n"
    texto += f"RAM: {ram}%\n"
    texto += f"Disco: {disk}%</blockquote>\n"
    
    await context.bot.send_message(chat_id=update.effective_user.id, text=texto, parse_mode='HTML')

async def cola_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from database import count_priority_queue, get_priority_queue_names
    
    # Auto-delete user's command if possible to keep chat clean
    try:
        await update.message.delete()
    except: pass
    
    cola = await count_priority_queue(db_pool)
    if cola == 0:
        texto = "✅ <b>La cola de descargas está vacía.</b>\n\nNo hay programas pendientes en este momento. ¡Haz tu pedido con <code>#programa</code>!"
    else:
        nombres = await get_priority_queue_names(db_pool, limit=10)
        lista_nombres = "\\n".join([f"• <i>{n}</i>" for n in nombres])
        texto = f"⏳ <b>Estado de la Cola Prioritaria:</b>\n\n"
        texto += f"Hay <b>{cola}</b> pedido(s) en proceso actualmente:\n\n"
        texto += lista_nombres
        
        if cola > 10:
            texto += f"\\n... y {cola - 10} más."
            
    # Send message and optionally store its ID for auto-deletion if desired, 
    # but since it's a direct status check, we just send it.
    await context.bot.send_message(chat_id=update.effective_chat.id, text=texto, parse_mode='HTML')

async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    
    # Auto-delete user's command if possible
    try:
        await update.message.delete()
    except: pass
    
    if not context.args:
        await context.bot.send_message(chat_id=update.effective_user.id, text="❌ <b>Uso:</b> <code>/broadcast &lt;mensaje&gt;</code>", parse_mode='HTML')
        return
        
    mensaje = " ".join(context.args)
    from database import get_all_users
    users = await get_all_users(db_pool)
    
    status_msg = await context.bot.send_message(chat_id=update.effective_user.id, text=f"🚀 <b>Iniciando envío masivo a <code>{len(users)}</code> usuarios...</b>", parse_mode='HTML')
    
    count = 0
    for uid in users:
        try:
            await application.bot.send_message(chat_id=uid, text=mensaje)
            count += 1
            await asyncio.sleep(0.05) # Límite de 20 mensajes por segundo
        except Exception as e:
            pass # Ignorar usuarios que bloquearon al bot
            
    await context.bot.edit_message_text(chat_id=update.effective_user.id, message_id=status_msg.message_id, text=f"✅ <b>¡Broadcast Completado!</b>\n\n<code>Mensaje entregado con éxito a {count} usuarios.</code>", parse_mode='HTML')

# ----------------- INLINE QUERY REMOVIDO -----------------

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
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("cola", cola_command))
    application.add_handler(CommandHandler("broadcast", broadcast_command))
    application.add_handler(CallbackQueryHandler(button_callback))
    from telegram.ext import MessageHandler, filters
    application.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, message_handler))
    
    await application.initialize()
    await application.start()
    
    logger.info("Bot de Telegram iniciado en entorno asíncrono con Local API Server.")
    
    from processor import startup_cleanup_folders
    startup_cleanup_folders()
    
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