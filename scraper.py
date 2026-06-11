import aiohttp
import feedparser
import re
from bs4 import BeautifulSoup
from urllib.parse import urljoin
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
    return soup.get_text(separator="\n", strip=True)

async def fetch_content(url, is_rss=False):
    proxy = PROXY_URL if PROXY_URL else None
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    async with aiohttp.ClientSession(headers=headers) as session:
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

async def extract_majorgeeks_direct_link(details_url):
    """
    Entra a la página de detalles, busca el mirror de descarga,
    entra al mirror y extrae la URL final del archivo .exe o .zip.
    """
    try:
        html = await fetch_content(details_url)
        if not html: return None
        
        soup = BeautifulSoup(html, "html.parser")
        mirror_url = None
        
        # 1. Buscar el botón "Download@MajorGeeks"
        for a in soup.find_all('a', href=True):
            if '/mg/getmirror/' in a['href']:
                mirror_url = urljoin("https://www.majorgeeks.com", a['href'])
                break
                
        if not mirror_url:
            return None
            
        # 2. Entrar a la página del Mirror
        mirror_html = await fetch_content(mirror_url)
        if not mirror_html: return None
        
        mirror_soup = BeautifulSoup(mirror_html, "html.parser")
        
        # 3. Extraer el link directo desde la etiqueta <meta http-equiv="Refresh">
        meta_refresh = mirror_soup.find("meta", attrs={"http-equiv": re.compile("refresh", re.I)})
        if meta_refresh and meta_refresh.get("content"):
            # El contenido suele ser: "5;url=https://archivos.com/programa.exe"
            content = meta_refresh["content"]
            match = re.search(r'url=([^"\'>]+)', content, re.I)
            if match:
                direct_url = match.group(1).strip()
                # A veces es un enlace relativo
                if direct_url.startswith("/"):
                    direct_url = urljoin("https://www.majorgeeks.com", direct_url)
                return direct_url
                
        # Fallback: a veces proporcionan un enlace clickeable si no redirige
        click_here = mirror_soup.find('a', text=re.compile("click here", re.I))
        if click_here and click_here.get('href'):
            return click_here['href']
            
    except Exception as e:
        logger.error(f"Error extrayendo enlace directo de {details_url}: {e}")
        
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
        link_detalles = entry.link if hasattr(entry, 'link') else ""
        categoria = entry.category if hasattr(entry, 'category') else ""
        
        version = ""
        descripcion_limpia = clean_html(descripcion_html)
        
        if not es_juego(titulo_completo, descripcion_limpia, categoria):
            logger.info(f"Rastreando enlace directo para: {titulo_completo}")
            url_real_descarga = await extract_majorgeeks_direct_link(link_detalles)
            
            if url_real_descarga:
                resultados.append({
                    "titulo": titulo_completo,
                    "version": version,
                    "categoria": categoria,
                    "url_origen": url_real_descarga,
                    "descripcion": descripcion_limpia[:500] + "..." if len(descripcion_limpia) > 500 else descripcion_limpia
                })
            else:
                logger.warning(f"Se ignoró '{titulo_completo}' porque no se pudo obtener un enlace directo.")
            
    return resultados

async def scrape_massgrave():
    url = "https://massgrave.dev/windows_11_links" 
    html = await fetch_content(url, is_rss=False)
    
    resultados = []
    if not html:
        return resultados
        
    soup = BeautifulSoup(html, "html.parser")
    for a in soup.find_all('a', href=True):
        href = a['href']
        if href.lower().endswith('.iso') or "software-download" in href.lower():
            titulo = a.get_text(strip=True) or "Windows ISO (Enlace Directo)"
            if len(titulo) > 3:
                resultados.append({
                    "titulo": titulo,
                    "version": "1.0",
                    "categoria": "Sistema Operativo",
                    "url_origen": href,
                    "descripcion": "ISO Extraída desde enlaces limpios comunitarios."
                })
    return resultados
