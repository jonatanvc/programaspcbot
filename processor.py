import os
import shutil
import asyncio
import subprocess
import random
from config import logger, MIN_DISK_SPACE_GB

DOWNLOAD_DIR = "./downloads"

def check_disk_space():
    try:
        total, used, free = shutil.disk_usage("/")
        free_gb = free / (1024 ** 3)
        if free_gb < MIN_DISK_SPACE_GB:
            logger.warning(f"Espacio en disco insuficiente: {free_gb:.2f} GB libres. Mínimo requerido: {MIN_DISK_SPACE_GB} GB.")
            return False
        return True
    except Exception as e:
        logger.error(f"Error al verificar espacio en disco: {e}")
        return False

async def download_file(url, file_name):
    """
    Descarga un archivo desde la URL usando aria2c para máxima velocidad multihilo.
    """
    if not os.path.exists(DOWNLOAD_DIR):
        os.makedirs(DOWNLOAD_DIR)
        
    file_path = os.path.join(DOWNLOAD_DIR, file_name)
    
    max_retries = 3
    for attempt in range(1, max_retries + 1):
        try:
            logger.info(f"Intentando descargar {url} con Aria2 (Intento {attempt}/{max_retries})")
            
            # Comando Aria2 mejorado para evitar bloqueos: 
            # --check-certificate=false (evita errores SSL en URLs estaticas como las de MS)
            # --user-agent (simula navegador)
            # --auto-file-renaming=false (evita crear archivos repetidos .1 si falla)
            # Optimización: Reducido de 16 a 4 conexiones para no saturar CPU/Red del VPS
            # Anti-Congelamiento: --timeout=60 y --max-tries=3 para evitar que Aria2 se quede bloqueado
            # Docker DNS Fix: Forzamos el uso de DNS de Google/Cloudflare por si el VPS bloquea el dominio .xyz
            cmd = f'aria2c -x 4 -s 4 -k 1M --check-certificate=false -U "Mozilla/5.0 (Windows NT 10.0; Win64; x64)" --auto-file-renaming=false --async-dns=true --async-dns-server=8.8.8.8,1.1.1.1 --timeout=60 --max-tries=3 -d "{DOWNLOAD_DIR}" -o "{file_name}" "{url}"'
            
            process = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=3600.0)
            except asyncio.TimeoutError:
                logger.error(f"Timeout de 60 minutos alcanzado en Aria2 para {url}. Matando proceso zombi.")
                try: process.kill()
                except: pass
                continue
            
            if process.returncode == 0 and os.path.exists(file_path):
                # Validar que no descargó una página HTML de Cloudflare o Error 404
                try:
                    with open(file_path, 'rb') as f:
                        header = f.read(1024).decode('utf-8', errors='ignore').lower()
                        if "<html" in header or "<!doctype html" in header or "<body" in header:
                            logger.error(f"Falsa descarga. El archivo es una página HTML (posible bloqueo).")
                            os.remove(file_path)
                            continue
                except:
                    pass
                
                logger.info(f"Descarga exitosa: {file_path}")
                return file_path
            else:
                logger.warning(f"Fallo la descarga con aria2c. Código: {process.returncode}. {stderr.decode()}")
        except Exception as e:
            logger.error(f"Excepción durante la descarga de {url}: {e}")
        
        await asyncio.sleep(5)
        
    logger.warning("Fallo Aria2c después de todos los intentos. Cayendo a fallback asíncrono con httpx...")
    try:
        import httpx
        
        async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
            headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
            async with client.stream('GET', url, headers=headers) as response:
                response.raise_for_status()
                with open(file_path, 'wb') as out_file:
                    async for chunk in response.aiter_bytes(chunk_size=8192):
                        out_file.write(chunk)
        
        # Check size to ensure it wasn't a tiny HTML file
        if os.path.getsize(file_path) > 10240:
            # Check headers just like we do for aria2c
            try:
                with open(file_path, 'rb') as f:
                    header = f.read(1024).decode('utf-8', errors='ignore').lower()
                    if "<html" in header or "<!doctype html" in header or "<body" in header:
                        logger.error("Fallback httpx descargó una página HTML falsa (Cloudflare/Error). Descartando...")
                        os.remove(file_path)
                        return None
            except Exception:
                pass
            
            logger.info(f"Fallback httpx exitoso: {file_path}")
            return file_path
        else:
            logger.error("Fallback httpx descargó un archivo demasiado pequeño (posible bloqueo).")
            os.remove(file_path)
    except Exception as py_err:
        logger.error(f"Fallback asíncrono de httpx también falló: {py_err}")

    return None

async def safe_upload_file(bot, chat_id, file_path, caption=""):
    """
    Sube un archivo utilizando PTB y el Local Bot API Server.
    Utiliza el prefijo file:// para transferencias instantáneas (Zero Network Overhead)
    """
    from telegram.error import RetryAfter, TimedOut, NetworkError
    
    max_retries = 3
    for attempt in range(1, max_retries + 1):
        try:
            logger.info(f"Subiendo {file_path} a Telegram (Intento {attempt})...")
            await asyncio.sleep(random.uniform(3, 5))
            
            # Al usar Local Bot API con volúmenes compartidos, podemos pasar la ruta absoluta con file://
            # Aseguramos que la ruta coincida con el volumen montado en el docker-compose (/downloads)
            # En Dokploy, el contenedor del bot mapea /app/downloads. El API server mapea /downloads.
            # Por lo tanto, si file_path es './downloads/file.zip', lo convertimos a '/downloads/file.zip'
            
            api_server_path = os.path.abspath(file_path).replace("/app/downloads", "/downloads").replace("\\", "/")
            if "downloads" in file_path and not api_server_path.startswith("/downloads"):
                 # Fallback para windows local testing
                 filename = os.path.basename(file_path)
                 api_server_path = f"/downloads/{filename}"
                 
            local_url = f"file://{api_server_path}"
            
            message = await bot.send_document(
                chat_id=chat_id,
                document=local_url,
                caption=caption,
                parse_mode='HTML',
                read_timeout=600,
                write_timeout=600,
                connect_timeout=600,
                pool_timeout=600
            )
            return message.document.file_id
        except RetryAfter as e:
            logger.warning(f"FloodWait detectado! Pausando {e.retry_after}s...")
            await asyncio.sleep(e.retry_after + 1)
        except (TimedOut, NetworkError) as e:
            logger.warning(f"Error de red en intento {attempt}: {e}")
            await asyncio.sleep(10)
        except Exception as e:
            logger.error(f"Error desconocido subiendo {file_path}: {e}")
            await asyncio.sleep(10)
            
    logger.error(f"Fallo definitivo al subir {file_path} tras {max_retries} intentos.")
    return None

async def split_file_if_needed(file_path):
    MAX_SIZE = 1.95 * 1024 * 1024 * 1024 # 1.95 GB limit
    file_size = os.path.getsize(file_path)
    
    if file_size < MAX_SIZE:
        return [file_path]
    
    logger.info(f"Archivo {file_path} supera los 2GB ({file_size / (1024**3):.2f} GB). Iniciando división con 7z...")
    
    base_name = os.path.basename(file_path)
    split_dir = os.path.join(DOWNLOAD_DIR, f"{base_name}_parts")
    if not os.path.exists(split_dir):
        os.makedirs(split_dir)
        
    out_path = os.path.join(split_dir, base_name + ".7z")
    
    try:
        # Añadimos -mx=0 (Store) para evitar compresión inútil que satura el CPU y RAM por horas
        cmd = f'7z a -mx=0 -v1900m "{out_path}" "{file_path}"'
        process = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=1800.0)
        except asyncio.TimeoutError:
            logger.error(f"Timeout de 30 minutos alcanzado en 7z para {file_path}. Matando proceso zombi.")
            try: process.kill()
            except: pass
            return []
        
        if process.returncode != 0:
            logger.error(f"Error al dividir archivo con 7z: {stderr.decode()}")
            return []
            
        partes = []
        for i, file in enumerate(sorted(os.listdir(split_dir)), 1):
            old_path = os.path.join(split_dir, file)
            ext = file.split('.')[-1] # Extrae .001, .002
            clean_base = base_name.rsplit('.', 1)[0] if '.' in base_name else base_name
            new_name = f"{clean_base}_Parte_{i}.7z.{ext}"
            new_path = os.path.join(split_dir, new_name)
            os.rename(old_path, new_path)
            partes.append(new_path)
            
        logger.info(f"División exitosa. {len(partes)} partes renombradas y generadas.")
        return partes
        
    except Exception as e:
        logger.error(f"Excepción al ejecutar 7z: {e}")
        return []

def cleanup_files(file_paths):
    if isinstance(file_paths, str):
        file_paths = [file_paths]
        
    for path in file_paths:
        try:
            if os.path.isfile(path):
                os.remove(path)
                logger.info(f"Limpieza: Archivo {path} eliminado.")
            elif os.path.isdir(path):
                shutil.rmtree(path)
                logger.info(f"Limpieza: Directorio {path} eliminado.")
        except Exception as e:
            logger.error(f"Error al intentar eliminar {path}: {e}")
            
    for path in file_paths:
        parent_dir = os.path.dirname(path)
        if parent_dir and parent_dir.endswith("_parts"):
            try:
                if os.path.exists(parent_dir) and not os.listdir(parent_dir):
                    os.rmdir(parent_dir)
                    logger.info(f"Limpieza: Directorio temporal vacío {parent_dir} eliminado.")
            except Exception:
                pass

def startup_cleanup_folders():
    folders_to_clean = [DOWNLOAD_DIR, "temp_images"]
    
    # Limpiar carpetas específicas
    for folder in folders_to_clean:
        if os.path.exists(folder):
            try:
                # Borrar la carpeta entera y recrearla
                shutil.rmtree(folder)
                logger.info(f"Limpieza de inicio: Directorio {folder} purgado completamente.")
            except Exception as e:
                logger.error(f"Limpieza de inicio: Error purgando {folder}: {e}")
        os.makedirs(folder, exist_ok=True)
        
    # Limpiar directorios de _parts sueltos en el directorio raíz o en DOWNLOAD_DIR
    try:
        for item in os.listdir("."):
            if item.endswith("_parts") and os.path.isdir(item):
                shutil.rmtree(item)
                logger.info(f"Limpieza de inicio: Directorio de partes {item} purgado.")
    except:
        pass
