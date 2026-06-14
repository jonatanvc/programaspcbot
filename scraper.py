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

async def obtener_enlace_dinamico(context, url_post, titulo="Software"):
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
        try:
            await page.goto(url_post, wait_until="domcontentloaded", timeout=40000)
        except Exception as e:
            from config import logger
            logger.warning(f"FCR-Scraper: Timeout al intentar cargar {url_post}. Detalle: {e}")
        
        # --- Extracción de metadatos solicitada por el usuario ---
        version = "Desconocida"
        descripcion = ""
        imagen_url = ""
        
        try:
            # Extraer versión usando regex desde el título si no se encuentra
            import re
            version = "Desconocida"
            match = re.search(r'\d+(?:\.\d+)+', url_post) # Try to find version in URL or title
            if match:
                version = match.group(0)
            else:
                version_tag = page.locator(".version, .app-version, h1").first
                if await version_tag.count() > 0:
                    t = await version_tag.inner_text()
                    m = re.search(r'\d+(?:\.\d+)+', t)
                    if m: version = m.group(0)

            descripcion = "Sin descripción"
            desc_tag = page.locator("article p, .article p, .post-wrap p").first
            if await desc_tag.count() > 0:
                descripcion = await desc_tag.inner_text()

            imagen_url = ""
            # Extraemos la imagen de las metas de OpenGraph (siempre confiable en React)
            img_tag = page.locator('meta[property="og:image"]').first
            if await img_tag.count() > 0:
                imagen_url = await img_tag.get_attribute("content")
            if not imagen_url:
                img_tag = page.locator("img.slider-image, article img").first
                if await img_tag.count() > 0:
                    imagen_url = await img_tag.get_attribute("src")
                
            # Limpiar textos de saltos de línea excesivos
            if descripcion:
                descripcion = descripcion.strip()
            if version:
                version = version.strip()
        except Exception as e:
            from config import logger
            logger.warning(f"No se pudieron extraer algunos metadatos en {url_post}: {e}")
        # -----------------------------------------------------------
        
        # Localizamos el botón que inicializa la secuencia de descarga (ahora usa <button> de NextJS)
        boton_descarga = page.locator("div[class^='version_download'] button, button.btn-primary.large, a.btn-download, a:has-text('Direct Download')").first
        if await boton_descarga.count() == 0:
            boton_descarga = page.locator("button:has-text('Download')").first

        if await boton_descarga.count() > 0:
            await boton_descarga.scroll_into_view_if_needed()
            await boton_descarga.click()
            
            # Espera explícita segura de 15 segundos para dar tiempo al contador dinámico
            import asyncio
            await asyncio.sleep(15)
            
            # Buscar todos los enlaces y filtrar el enlace resultante
            enlaces = await page.locator("a[href]").all()
            enlace_final = None
            
            for en in enlaces:
                try:
                    href = await en.get_attribute("href")
                    clase = await en.get_attribute("class") or ""
                    
                    # Ignorar enlaces basura o de navegación interna
                    if not href or href.startswith("/") or "how-to" in href or "login" in href:
                        continue
                        
                    if "filecr.com" not in href.lower():
                        if href.startswith("magnet:") or href.endswith((".zip", ".rar", ".iso", ".7z", ".exe")):
                            enlace_final = href
                            break
                        if "?ref=" in href.lower() or "download" in href.lower() or ".xyz" in href.lower():
                            enlace_final = href
                            break
                except Exception:
                    continue
                    
            if enlace_final and (enlace_final.lower().endswith(".dmg") or enlace_final.lower().endswith(".pkg")):
                from config import logger
                logger.warning(f"FCR-Scraper: Archivo Mac detectado y descartado: {enlace_final}")
                return None

            # Extraer tags para la categoría (AudioEffects, VideoEditor)
            tags_list = []
            tags_elements = await page.locator('meta[property="article:tag"]').all()
            for tag in tags_elements:
                content = await tag.get_attribute("content")
                if content:
                    tags_list.append(content.replace(" ", ""))
            categoria_real = ", ".join(tags_list) if tags_list else "Software"
            
            # Extraer fecha
            fecha_actualizacion = "Reciente"
            date_tag = page.locator('meta[property="article:modified_time"]').first
            if await date_tag.count() > 0:
                fecha_raw = await date_tag.get_attribute("content")
                # Intentaremos dar formato "June 12, 2026" luego o dejar "2026-06-12"
                if fecha_raw: fecha_actualizacion = fecha_raw.split("T")[0]

            # Extraer publisher (JSON)
            publisher = "Unknown"
            languages = "Multilingual" if "multilingual" in titulo.lower() else "English"
            try:
                import json
                script_tags = await page.locator('script[type="application/ld+json"]').all_inner_texts()
                for script_content in script_tags:
                    data_json = json.loads(script_content)
                    if data_json.get('@type') == 'SoftwareApplication':
                        pub = data_json.get('publisher')
                        if isinstance(pub, dict) and pub.get('name'):
                            publisher = pub['name']
                        if data_json.get('datePublished'):
                            # Overwrite with datePublished if preferred, but modified_time is ok.
                            pass
            except Exception:
                pass

            if enlace_final:
                return {
                    "enlace": enlace_final,
                    "version": version,
                    "descripcion": descripcion,
                    "imagen_url": imagen_url,
                    "categoria": categoria_real,
                    "fecha_actualizacion": fecha_actualizacion,
                    "publisher": publisher,
                    "languages": languages
                }
            else:
                from config import logger
                logger.warning(f"FCR-Scraper: No se detectó el enlace final en {url_post}")
                return None
    except Exception as e:
        from config import logger
        logger.error(f"Error procesando el enlace dinámico en {url_post}: {e}")
    finally:
        if page:
            await page.close()
    return None

async def scrape_filecr(url_apps="https://filecr.com/ms-windows/"):
    """
    Rastrea el catálogo principal de la sección Windows de FileCR evadiendo Cloudflare.
    """
    from config import logger
    logger.info(f"Iniciando Scraping automatizado en FileCR ({url_apps})...")
    resultados = []

    import urllib.request
    import re
    import random
    
    sitemap_idx = random.randint(1, 20)
    sitemap_url = f"https://filecr.com/post-sitemap{sitemap_idx}.xml"
    logger.info(f"FCR-Scraper: Evadiendo Cloudflare leyendo sitemap: {sitemap_url}")
    
    try:
        req = urllib.request.Request(sitemap_url, headers={'User-Agent': 'Mozilla/5.0'})
        xml_data = urllib.request.urlopen(req).read().decode('utf-8')
        
        # Extraer slugs de windows
        slugs = re.findall(r'<loc>https://filecr.com/windows/([^<]+)/</loc>', xml_data)
        
        if slugs:
            random.shuffle(slugs)
            slugs = slugs[:10]
            for slug in slugs:
                titulo = slug.replace('-', ' ').title()
                url_post = f"https://filecr.com/windows/{slug}/"
                
                if es_juego(titulo, "", "Windows Apps"):
                    continue
                    
                resultados.append({
                    "titulo": titulo,
                    "version": "Latest", 
                    "categoria": "Software",
                    "url_origen": url_post,
                    "descripcion": "",
                    "imagen_url": "",
                    "fecha_actualizacion": "Reciente"
                })
        else:
            logger.error("No se encontraron slugs de /windows/ en el sitemap.")
            
    except Exception as e:
        logger.error(f"Error al leer sitemap {sitemap_url}: {e}")
        
    return resultados

# Funciones deprecadas mantenidas exclusivamente por consistencia de importaciones
async def scrape_github_releases():
    return []

async def scrape_massgrave():
    return []