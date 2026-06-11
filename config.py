import os
import logging
from dotenv import load_dotenv

# Cargar variables desde el archivo .env si existe
load_dotenv()

# Configuraciones de la API de Telegram (Pyrogram)
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")

# IDs de Telegram
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
PUBLIC_CHANNEL_ID = int(os.getenv("PUBLIC_CHANNEL_ID", "0"))
PRIVATE_BACKUP_CHANNEL_ID = int(os.getenv("PRIVATE_BACKUP_CHANNEL_ID", "0"))

# Base de datos PostgreSQL en Dokploy
DATABASE_URL = os.getenv("DATABASE_URL", "")

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
