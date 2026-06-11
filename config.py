import os
import logging
from dotenv import load_dotenv

# Cargar variables desde el archivo .env si existe
load_dotenv()

# Configuraciones de la API de Telegram (Pyrogram)
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "").strip().strip('"').strip("'")
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip().strip('"').strip("'")

# IDs de Telegram
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
PUBLIC_CHANNEL_ID = int(os.getenv("PUBLIC_CHANNEL_ID", "0"))
PRIVATE_BACKUP_CHANNEL_ID = int(os.getenv("PRIVATE_BACKUP_CHANNEL_ID", "0"))

# Base de datos PostgreSQL en Dokploy
# Usamos DB_URL en lugar de DATABASE_URL para evitar que Dokploy lo sobreescriba automáticamente
DATABASE_URL = os.getenv("DB_URL", "").strip().strip('"').strip("'")

# Print debug para verificar qué está leyendo realmente (Ocultando la contraseña)
if DATABASE_URL:
    try:
        # Intenta ocultar la contraseña en el log para seguridad
        safe_url = DATABASE_URL.split("@")[1] 
        print(f"DEBUG: Intentando conectar al host de BD: {safe_url.split('/')[0]}")
    except:
        print("DEBUG: DATABASE_URL tiene un formato no estándar.")
else:
    print("DEBUG: La variable DB_URL está VACÍA. Dokploy no la está pasando.")

# Configuración del servidor
MIN_DISK_SPACE_GB = int(os.getenv("MIN_DISK_SPACE_GB", "15"))

# Proxy para scraping (Opcional, formato: http://user:pass@ip:port)
PROXY_URL = os.getenv("PROXY_URL", "")

# Configuración del Logger
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("bot.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("SystemLogger")
