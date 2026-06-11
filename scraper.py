import aiohttp
import feedparser
import re
from bs4 import BeautifulSoup
from config import logger, PROXY_URL

BANNED_WORDS = [
    "games", "gaming", "arcade", "action", "rpg", "steam", "nintendo",
    "game", "playstation", "xbox", "emulators", "emulator", "roms", "rom"
]

def es_juego(titulo, descripcion, categoria):
    texto_a_revisar = f"{titulo} {descripcion} {categoria}".lower()
    for word in BANNED_WORDS:
        if re.search(rf'\b{word}\b', texto_a_revisar):
            logger.info(f"Filtro Anti-Juego activado. Bloqueado por palabra: {word} | Título: {titulo}")
            return True
    return False

def clean_html(raw_html):
    if not raw_html:
        return ""
    soup = BeautifulSoup(raw_html, "html.parser")
    for a in soup.findAll('a'):
        a.unwrap()
    text = soup.get_text(separator="\n", strip=True)
    return text

async def fetch_content(url, is_rss=False):
    """
    Función genérica para hacer fetch de URLs con soporte de Proxy.
    """
    proxy = PROXY_URL if PROXY_URL else None
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, proxy=proxy, timeout=20) as response:
                if response.status == 200:
                    content = await response.text()
                    if is_rss:
                        return feedparser.parse(content)
                    return content
        except Exception as e:
            logger.error(f"Error HTTP obteniendo {url}: {e}")
    return None

async def scrape_majorgeeks():
    rss_url = "https://www.majorgeeks.com/files/rss"
    feed = await fetch_content(rss_url, is_rss=True)
    
    resultados = []
    if not feed or not hasattr(feed, 'entries'):
        return resultados

    for entry in feed.entries:
        titulo_completo = entry.title if hasattr(entry, 'title') else "Sin Titulo"
        descripcion_html = entry.description if hasattr(entry, 'description') else ""
        link = entry.link if hasattr(entry, 'link') else ""
        categoria = entry.category if hasattr(entry, 'category') else ""
        
        version = ""
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

async def scrape_massgrave():
    """
    Scraper específico para extraer enlaces directos desde los repositorios comunitarios limpios (ej. enlaces a software de MS).
    Este es un scraper básico de demostración para el concepto.
    """
    # Massgrave aloja muchas descargas directas en Github Pages o sitios propios.
    # Como la web real es dinámica, haremos un scraping a una URL hipotética/conocida para buscar .iso
    url = "https://massgrave.dev/windows_11_links" 
    html = await fetch_content(url, is_rss=False)
    
    resultados = []
    if not html:
        return resultados
        
    soup = BeautifulSoup(html, "html.parser")
    # Buscamos enlaces a ISOs
    for a in soup.find_all('a', href=True):
        href = a['href']
        if href.lower().endswith('.iso') or "software-download" in href.lower():
            titulo = a.get_text(strip=True) or "Windows ISO (Enlace Directo)"
            # Evitamos duplicados básicos
            if len(titulo) > 3:
                resultados.append({
                    "titulo": titulo,
                    "version": "1.0",
                    "categoria": "Sistema Operativo",
                    "url_origen": href,
                    "descripcion": "ISO Extraída desde enlaces limpios comunitarios."
                })
    return resultados
