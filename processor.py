import os
import shutil
import asyncio
import subprocess
from config import logger, MIN_DISK_SPACE_GB

DOWNLOAD_DIR = "./downloads"

def check_disk_space():
    """
    Verifica el espacio en disco en el VPS.
    Devuelve True si hay espacio suficiente, False si está por debajo del mínimo (ej. 15GB).
    """
    try:
        total, used, free = shutil.disk_usage("/")
        free_gb = free / (1024 ** 3)
        if free_gb < MIN_DISK_SPACE_GB:
            logger.warning(f"Espacio en disco insuficiente: {free_gb:.2f} GB libres. Mínimo requerido: {MIN_DISK_SPACE_GB} GB.")
            return False
        return True
    except Exception as e:
        logger.error(f"Error al verificar espacio en disco: {e}")
        return False # Ante la duda, evitamos saturar

async def download_file(url, file_name):
    """
    Descarga un archivo desde la URL usando wget o curl (mediante subprocess para simplicidad con archivos grandes)
    o aiohttp. Usaremos subprocess con curl para mayor robustez en descargas grandes.
    Retorna la ruta del archivo descargado o None en caso de error.
    """
    if not os.path.exists(DOWNLOAD_DIR):
        os.makedirs(DOWNLOAD_DIR)
        
    file_path = os.path.join(DOWNLOAD_DIR, file_name)
    
    # Lógica de reintentos
    max_retries = 3
    for attempt in range(1, max_retries + 1):
        try:
            logger.info(f"Intentando descargar {url} (Intento {attempt}/{max_retries})")
            # Ejecutamos curl de manera síncrona pero sin bloquear el event loop usando run_in_executor
            # O mejor, ejecutamos el comando asíncronamente
            process = await asyncio.create_subprocess_shell(
                f'curl -L "{url}" -o "{file_path}" -s',
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await process.communicate()
            
            if process.returncode == 0 and os.path.exists(file_path):
                logger.info(f"Descarga exitosa: {file_path}")
                return file_path
            else:
                logger.warning(f"Fallo la descarga con curl: {stderr.decode()}")
        except Exception as e:
            logger.error(f"Excepción durante la descarga de {url}: {e}")
        
        await asyncio.sleep(5) # Esperar antes del próximo intento
        
    return None

async def split_file_if_needed(file_path):
    """
    Verifica el tamaño del archivo. Si es mayor a 2GB (aprox. 2000000000 bytes),
    lo divide con 7z en partes de 1900MB.
    Retorna una lista de rutas de archivos a subir.
    """
    MAX_SIZE = 1.95 * 1024 * 1024 * 1024 # 1.95 GB para estar seguros (Telegram limite es 2GB)
    file_size = os.path.getsize(file_path)
    
    if file_size < MAX_SIZE:
        return [file_path]
    
    logger.info(f"Archivo {file_path} supera los 2GB ({file_size / (1024**3):.2f} GB). Iniciando división con 7z...")
    
    base_name = os.path.basename(file_path)
    # Crear un subdirectorio temporal para las partes
    split_dir = os.path.join(DOWNLOAD_DIR, f"{base_name}_parts")
    if not os.path.exists(split_dir):
        os.makedirs(split_dir)
        
    # Archivo base de salida para 7z
    out_path = os.path.join(split_dir, base_name + ".7z")
    
    try:
        # 7z a -v1900m <archivo_salida> <archivo_entrada>
        cmd = f'7z a -v1900m "{out_path}" "{file_path}"'
        process = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await process.communicate()
        
        if process.returncode != 0:
            logger.error(f"Error al dividir archivo con 7z: {stderr.decode()}")
            return []
            
        # Recopilar todas las partes generadas (.7z.001, .7z.002, etc.)
        partes = []
        for file in sorted(os.listdir(split_dir)):
            partes.append(os.path.join(split_dir, file))
            
        logger.info(f"División exitosa. {len(partes)} partes generadas.")
        return partes
        
    except Exception as e:
        logger.error(f"Excepción al ejecutar 7z: {e}")
        return []

def cleanup_files(file_paths):
    """
    Elimina los archivos especificados del disco permanentemente.
    Recibe una lista de rutas (archivos originales o divididos) o una sola ruta.
    """
    if isinstance(file_paths, str):
        file_paths = [file_paths]
        
    for path in file_paths:
        try:
            if os.path.isfile(path):
                os.remove(path)
                logger.info(f"Limpieza (Garbage Collector): Archivo {path} eliminado.")
            elif os.path.isdir(path):
                shutil.rmtree(path)
                logger.info(f"Limpieza (Garbage Collector): Directorio {path} eliminado.")
        except Exception as e:
            logger.error(f"Error al intentar eliminar {path}: {e}")
            
    # Intentar limpiar también directorios padres vacíos si se crearon partes
    for path in file_paths:
        parent_dir = os.path.dirname(path)
        if parent_dir and parent_dir.endswith("_parts"):
            try:
                if os.path.exists(parent_dir) and not os.listdir(parent_dir):
                    os.rmdir(parent_dir)
                    logger.info(f"Limpieza: Directorio temporal vacío {parent_dir} eliminado.")
            except Exception:
                pass
