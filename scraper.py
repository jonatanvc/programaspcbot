import aiohttp
import feedparser
import re
from bs4 import BeautifulSoup
from config import logger

# Palabras clave prohibidas (filtro estricto anti-juegos)
BANNED_WORDS = [
    "games", "gaming", "arcade", "action", "rpg", "steam", "nintendo",
    "game", "playstation", "xbox", "emulators", "emulator", "roms", "rom"
]

def es_juego(titulo, descripcion, categoria):
    """
    Verifica si el contenido contiene palabras prohibidas relacionadas con juegos.
    Devuelve True si es un juego (debe ser descartado).
    """
    texto_a_revisar = f"{titulo} {descripcion} {categoria}".lower()
    for word in BANNED_WORDS:
        # Busca la palabra clave con límites de palabra para evitar falsos positivos
        # (ej. no bloquear "actionable" si la palabra prohibida es "action", aunque aquí preferimos ser estrictos).
        if re.search(rf'\b{word}\b', texto_a_revisar):
            logger.info(f"Filtro Anti-Juego activado. Bloqueado por palabra: {word} | Título: {titulo}")
            return True
    return False

def clean_html(raw_html):
    """
    Limpia el código HTML de una descripción para devolver texto plano.
    """
    if not raw_html:
        return ""
    soup = BeautifulSoup(raw_html, "html.parser")
    # Eliminar enlaces y texto inútil
    for a in soup.findAll('a'):
        a.unwrap()
    text = soup.get_text(separator="\n", strip=True)
    return text

async def fetch_rss(url):
    """
    Obtiene el contenido de un Feed RSS.
    """
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, timeout=15) as response:
                if response.status == 200:
                    content = await response.text()
                    return feedparser.parse(content)
        except Exception as e:
            logger.error(f"Error al leer RSS {url}: {e}")
    return None

async def scrape_majorgeeks():
    """
    Realiza el scraping del RSS de MajorGeeks, filtra juegos y devuelve una lista de diccionarios con la data.
    """
    rss_url = "https://www.majorgeeks.com/files/rss"
    feed = await fetch_rss(rss_url)
    
    resultados = []
    if not feed or not hasattr(feed, 'entries'):
        return resultados

    for entry in feed.entries:
        titulo_completo = entry.title if hasattr(entry, 'title') else "Sin Titulo"
        descripcion_html = entry.description if hasattr(entry, 'description') else ""
        link = entry.link if hasattr(entry, 'link') else ""
        categoria = entry.category if hasattr(entry, 'category') else ""
        
        # Intentar extraer la versión del título (usualmente MajorGeeks pone "Programa x.x.x")
        version = ""
        # Limpieza básica
        descripcion_limpia = clean_html(descripcion_html)
        
        if not es_juego(titulo_completo, descripcion_limpia, categoria):
            resultados.append({
                "titulo": titulo_completo,
                "version": version,
                "categoria": categoria,
                "url_origen": link,
                "descripcion": descripcion_limpia[:500] + "..." if len(descripcion_limpia) > 500 else descripcion_limpia
            })
            
    return resultados

# Aquí se pueden agregar otras funciones para Bob Pony o Massgrave mediante scraping clásico con aiohttp y BeautifulSoup.
async def scrape_custom_isos():
    """
    Lugar reservado para scraping de ISOs.
    Devuelve lista vacía por ahora.
    """
    return []
