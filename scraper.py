import aiohttp
import re
import random
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
    return soup.get_text(separator="\n", strip=True)

async def fetch_json(url):
    proxy = PROXY_URL if PROXY_URL else None
    headers = {
        "User-Agent": "DistribucionBot/1.0",
        "Accept": "application/vnd.github.v3+json"
    }
    async with aiohttp.ClientSession(headers=headers) as session:
        try:
            async with session.get(url, proxy=proxy, timeout=15) as response:
                if response.status == 200:
                    return await response.json(content_type=None)
        except Exception as e:
            logger.error(f"Error HTTP obteniendo JSON de {url}: {e}")
    return None

async def fetch_content(url, is_rss=False):
    proxy = PROXY_URL if PROXY_URL else None
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko)"
    }
    async with aiohttp.ClientSession(headers=headers) as session:
        try:
            async with session.get(url, proxy=proxy, timeout=20) as response:
                if response.status == 200:
                    return await response.text()
        except Exception as e:
            logger.error(f"Error HTTP obteniendo {url}: {e}")
    return None

def extract_url_from_scoop(data):
    """Extrae la URL del JSON de Scoop, priorizando 64bit si existe."""
    url = None
    
    # 1. Intentar arquitectura 64bit
    arch = data.get('architecture', {})
    if '64bit' in arch and 'url' in arch['64bit']:
        url = arch['64bit']['url']
    elif '32bit' in arch and 'url' in arch['32bit']:
        url = arch['32bit']['url']
    elif 'url' in data:
        url = data['url']
        
    if isinstance(url, list) and len(url) > 0:
        url = url[0]
        
    if isinstance(url, str):
        # Aceptar ejecutables y comprimidos
        if url.endswith(".exe") or url.endswith(".msi") or url.endswith(".zip") or url.endswith(".7z"):
            return url
    return None

async def scrape_github_releases():
    """
    Rastrea el repositorio MUNDIAL de Scoop (Extras) para obtener programas aleatorios.
    """
    logger.info("Conectando al repositorio de Scoop Extras (Catálogo Masivo)...")
    tree_url = "https://api.github.com/repos/ScoopInstaller/Extras/git/trees/master?recursive=1"
    
    data = await fetch_json(tree_url)
    resultados = []
    
    if not data or "tree" not in data:
        logger.error("No se pudo obtener el árbol de archivos de Scoop.")
        return resultados

    # Filtrar solo archivos JSON dentro de la carpeta 'bucket'
    json_files = [f for f in data["tree"] if f["path"].startswith("bucket/") and f["path"].endswith(".json")]
    logger.info(f"Se encontraron {len(json_files)} programas disponibles en Scoop.")
    
    if not json_files:
        return resultados

    # Seleccionar 15 programas al azar para este ciclo
    seleccionados = random.sample(json_files, min(15, len(json_files)))
    
    for item in seleccionados:
        filename = item["path"].split("/")[-1].replace(".json", "")
        titulo = filename.replace("-", " ").title()
        
        raw_url = f"https://raw.githubusercontent.com/ScoopInstaller/Extras/master/{item['path']}"
        prog_data = await fetch_json(raw_url)
        
        if not prog_data:
            continue
            
        version = prog_data.get("version", "1.0")
        descripcion = prog_data.get("description", f"Utilidad para Windows: {titulo}")
        homepage = prog_data.get("homepage", "")
        
        titulo_completo = f"{titulo} {version}"
        
        if es_juego(titulo_completo, descripcion, homepage):
            continue
            
        download_url = extract_url_from_scoop(prog_data)
        
        if download_url:
            logger.info(f"Programa Random Encontrado: {titulo_completo} -> {download_url}")
            resultados.append({
                "titulo": titulo_completo,
                "version": str(version),
                "categoria": "Software para Windows",
                "url_origen": str(download_url),
                "descripcion": str(descripcion)[:500]
            })
            
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
        # EXTREMADAMENTE IMPORTANTE: Solo permitir archivos que terminen EXACTAMENTE en .iso
        if href.lower().split('?')[0].endswith('.iso'):
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
