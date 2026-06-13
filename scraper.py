import asyncio
import re
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright
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

async def obtener_enlace_dinamico(context, url_post):
    """
    Interactúa de forma asíncrona con el DOM de la página para emular el clic,
    manejar el tiempo de espera del contador y capturar el enlace limpio,
    además de extraer los metadatos de la aplicación.
    """
    page = None
    try:
        page = await context.new_page()
        await page.set_extra_http_headers({
            "Accept-Language": "es-ES,es;q=0.9,en;q=0.8"
        })
        
        # Conectamos al artículo
        await page.goto(url_post, wait_until="domcontentloaded", timeout=40000)
        
        # --- Extracción de metadatos solicitada por el usuario ---
        version = "Desconocida"
        descripcion = ""
        imagen_url = ""
        
        try:
            if await page.locator(".version").count() > 0:
                version = await page.locator(".version").first.inner_text()
            
            if await page.locator(".description-content").count() > 0:
                descripcion = await page.locator(".description-content").first.inner_text()
            elif await page.locator(".entry-content").count() > 0:
                descripcion = await page.locator(".entry-content").first.inner_text()
                
            if await page.locator(".product-image img").count() > 0:
                imagen_url = await page.locator(".product-image img").first.get_attribute("src")
            elif await page.locator(".featured-media img").count() > 0:
                imagen_url = await page.locator(".featured-media img").first.get_attribute("src")
                
            # Limpiar textos de saltos de línea excesivos
            if descripcion:
                descripcion = descripcion.strip()
            if version:
                version = version.strip()
        except Exception as e:
            from config import logger
            logger.warning(f"No se pudieron extraer algunos metadatos en {url_post}: {e}")
        # -----------------------------------------------------------
        
        # Localizamos el botón que inicializa la secuencia de descarga
        boton_descarga = page.locator("a:has-text('Direct Download'), .download-btn, a.btn-download").first
        if await boton_descarga.count() == 0:
            boton_descarga = page.locator("a[href*='/download/']").first

        if await boton_descarga.count() > 0:
            await boton_descarga.scroll_into_view_if_needed()
            await boton_descarga.click()
            
            from config import logger
            logger.info(f"FCR-Scraper: Botón pulsado en {url_post}. Esperando renderizado del link final...")
            
            # Selector que busca el link final generado tras el contador (directo o torrent magnet)
            selector_enlace_final = "a[href*='filecr.com/download/file/'], a[href^='magnet:'], a.download-file-btn"
            
            # Espera explícita optimizada de hasta 18 segundos por el contador dinámico
            await page.wait_for_selector(selector_enlace_final, state="visible", timeout=18000)
            
            enlace_final = await page.locator(selector_enlace_final).first.get_attribute("href")
            return {
                "enlace": enlace_final,
                "version": version,
                "descripcion": descripcion,
                "imagen_url": imagen_url
            }
    except Exception as e:
        from config import logger
        logger.error(f"Error procesando el enlace dinámico en {url_post}: {e}")
    finally:
        if page:
            await page.close()
    return None

async def scrape_filecr():
    """
    Rastrea el catálogo principal de la sección Windows de FileCR evadiendo Cloudflare.
    """
    logger.info("Iniciando Scraping automatizado en FileCR (Sección Windows)...")
    url_apps = "https://filecr.com/en/category/windows/"
    resultados = []

    async with async_playwright() as p:
        browser = None
        context = None
        page = None
        try:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                viewport={"width": 1366, "height": 768}
            )
            
            page = await context.new_page()
            await page.goto(url_apps, wait_until="networkidle", timeout=40000)
            html_content = await page.content()
            soup = BeautifulSoup(html_content, "html.parser")
            
            # Identificamos los contenedores de las tarjetas de software
            items = soup.select("article, .product-card, .card-item")
            posts_a_procesar = []
            
            for item in items:
                link_tag = item.select_one("a[href*='/windows/']")
                title_tag = item.select_one("h2, h3, .title, .product-title")
                
                if link_tag and title_tag:
                    url_post = link_tag['href']
                    if not url_post.startswith("http"):
                        url_post = f"https://filecr.com{url_post}"
                        
                    titulo = title_tag.get_text(strip=True)
                    posts_a_procesar.append((titulo, url_post))
            
            # Limitamos el bucle a los 10 primeros posts para mantener un flujo sano de procesamiento
            posts_a_procesar = posts_a_procesar[:10]
            logger.info(f"Se detectaron {len(posts_a_procesar)} programas en el feed. Extrayendo enlaces finales...")

            for titulo, url_post in posts_a_procesar:
                if es_juego(titulo, "", "Windows Apps"):
                    continue
                
                # Accedemos asíncronamente a extraer el recurso descargable
                data = await obtener_enlace_dinamico(context, url_post)
                
                if data and data.get("enlace"):
                    desc = data.get("descripcion")
                    if not desc:
                        desc = f"Software útil para Windows: {titulo}. Paquete limpio verificado con medicina e instrucciones incluidas."
                        
                    resultados.append({
                        "titulo": titulo,
                        "version": data.get("version", "Latest"), 
                        "categoria": "Software para Windows",
                        "url_origen": data["enlace"],
                        "descripcion": desc,
                        "imagen_url": data.get("imagen_url", "")
                    })
                    logger.info(f"✅ Descarga indexada correctamente: {titulo} {data.get('version', '')}")
                    
                await asyncio.sleep(2) # Delay prudencial por post
                
        except Exception as e:
            logger.error(f"Error general en el ciclo del scraper de FileCR: {e}")
        finally:
            if page: await page.close()
            if context: await context.close()
            if browser: await browser.close()
            
    return resultados

# Funciones deprecadas mantenidas exclusivamente por consistencia de importaciones
async def scrape_github_releases():
    return []

async def scrape_massgrave():
    return []