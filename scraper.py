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
            # Extraer metadata de forma robusta con JavaScript
            metadata = await page.evaluate('''() => {
                let title = document.querySelector('h1') ? document.querySelector('h1').innerText.trim() : '';
                
                let desc = 'Sin descripción';
                let desc_tag = document.querySelector('article p') || document.querySelector('.article p') || document.querySelector('.post-wrap p');
                if (desc_tag) desc = desc_tag.innerText.trim();
                
                let cat = 'Software';
                let langs = 'Multilingual';
                let reqs = '';
                
                let next_data = document.getElementById('__NEXT_DATA__');
                if (next_data) {
                    try {
                        let json = JSON.parse(next_data.innerText);
                        let post = json.props?.pageProps?.post || {};
                        
                        // Categories
                        if (post.categories) {
                            if (post.categories.subCategory && post.categories.subCategory.name) {
                                cat = post.categories.subCategory.name;
                            } else if (post.categories.primary && post.categories.primary.name) {
                                cat = post.categories.primary.name;
                            }
                        }
                        
                        // Languages
                        if (post.languages && Array.isArray(post.languages) && post.languages.length > 0) {
                            let lang_names = post.languages.map(l => l.name);
                            langs = lang_names.join(', ');
                        }
                        
                        // Requirements (re-used later)
                        if (post.requirements) {
                            let tempDiv = document.createElement('div');
                            tempDiv.innerHTML = post.requirements;
                            let lis = tempDiv.querySelectorAll('li');
                            lis.forEach(li => li.innerText = '• ' + li.innerText);
                            reqs = tempDiv.innerText.replace('Technical Details and System Requirements', '').trim();
                        }
                    } catch(e) {}
                }
                
                // Fallbacks if not found
                if (cat === 'Software') {
                    let breadcrumbs = document.querySelectorAll('a.breadcrumb, a[class*="breadcrumb"]');
                    let cats = [];
                    breadcrumbs.forEach(b => {
                        let t = b.innerText.trim();
                        if (t && !['Home', 'Windows', 'Mac', 'Android'].includes(t)) {
                            cats.push(t);
                        }
                    });
                    if (cats.length > 0) cat = cats.join(', ');
                }
                if (v_tag) {
                    let m = v_tag.innerText.match(/\\d+(?:\\.\\d+)+/);
                    if (m) version = m[0];
                }
                if (version === 'Desconocida' && title) {
                    let m = title.match(/\\d+(?:\\.\\d+)+/);
                    if (m) version = m[0];
                }
                
                let img = '';
                let og_img = document.querySelector('meta[property="og:image"]');
                if (og_img) img = og_img.getAttribute('content');
                if (!img) {
                    let s_img = document.querySelector('img.slider-image, article img');
                    if (s_img) img = s_img.getAttribute('src');
                }
                
                let date = 'Reciente';
                let info_labels = document.querySelectorAll('span[class*="info_label"], div.label');
                info_labels.forEach(lbl => {
                    if (lbl.innerText.includes('Release Date') || lbl.innerText.includes('Update')) {
                        let val_span = lbl.nextElementSibling;
                        if (val_span && val_span.innerText.trim()) {
                            date = val_span.innerText.trim();
                        }
                    }
                });
                if (date === 'Reciente') {
                    let d_tag = document.querySelector('meta[property="article:modified_time"]');
                    if (d_tag) date = d_tag.getAttribute('content').split('T')[0];
                }
                
                if (!reqs) {
                    let h3s = document.querySelectorAll('h3, h2, h4, p strong');
                    h3s.forEach(h => {
                        if (h.innerText.includes('System Requirements') || h.innerText.includes('Technical Details')) {
                            let next = h.nextElementSibling || (h.parentElement && h.parentElement.nextElementSibling);
                            if (next && next.tagName === 'UL') {
                                let lis = next.querySelectorAll('li');
                                let r_arr = [];
                                lis.forEach(li => {
                                    if (li.innerText.includes('OS') || li.innerText.includes('RAM') || li.innerText.includes('Disk') || li.innerText.includes('Processor')) {
                                        r_arr.push('• ' + li.innerText.trim());
                                    }
                                });
                                if (r_arr.length > 0) reqs = r_arr.join('\\n');
                            }
                        }
                    });
                }
                
                return {title, desc, cat, version, img, date, reqs, langs};
            }''')
            
            titulo_real = metadata.get('title', '')
            # Limpiar el título quitándole la versión si la tiene al final
            if titulo_real and metadata.get('version', '') != 'Desconocida':
                titulo_real = titulo_real.replace(metadata['version'], '').strip()
                
            descripcion = metadata.get('desc', 'Sin descripción')
            categoria_real = metadata.get('cat', 'Software')
            version = metadata.get('version', 'Desconocida')
            imagen_url = metadata.get('img', '')
            fecha_actualizacion = metadata.get('date', 'Reciente')
            requisitos = metadata.get('reqs', '')
            idiomas = metadata.get('langs', 'Multilingual')
            
            # Limpiar textos de saltos de línea excesivos y prefijos SEO de FileCR
            if descripcion:
                import re
                descripcion = descripcion.strip()
                descripcion = re.sub(r'(?i)^(Free Download[^-]+-\s*)(?:(?:Offline Installer|Standalone Setup|Pre-Activated|macOS|for Mac)[^-]*-\s*)?', '', descripcion)
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
            
            # Intentar interceptar la descarga automática
            enlace_final = None
            try:
                import asyncio
                async with page.expect_download(timeout=30000) as download_info:
                    await boton_descarga.click()
                    # Esperar 20 segundos para que el JS inicie la descarga
                    await asyncio.sleep(20)
                    
                    # Si hay un botón de "Click here to download", presionarlo
                    btn_final = page.locator("a:has-text('Click here to download'), button:has-text('Download')").first
                    if await btn_final.count() > 0 and await btn_final.is_visible():
                        await btn_final.click()
                        
                download = await download_info.value
                enlace_final = download.url
                await download.cancel() # No descargar el archivo real con Playwright, solo queremos la URL
                from config import logger
                logger.info(f"FCR-Scraper: URL de descarga interceptada nativamente: {enlace_final}")
            except Exception as e:
                from config import logger
                logger.warning(f"FCR-Scraper: No se interceptó descarga automática ({e}). Cayendo a DOM extraction...")
                
            if not enlace_final:
                # Extraer el enlace usando JavaScript para buscar href, data-href, data-url
                enlace_final = await page.evaluate('''() => {
                    let allElements = document.querySelectorAll('a, button, div, span');
                    let posibles = [];
                    for (let el of allElements) {
                        let url = el.getAttribute('href') || el.getAttribute('data-href') || el.getAttribute('data-url');
                        if (url && !url.includes('filecr.com') && !url.startsWith('/') && !url.includes('t.me') && !url.includes('twitter') && !url.includes('youtube') && !url.includes('facebook') && !url.includes('reddit') && !url.includes('pinterest') && !url.includes('vk.com') && !url.includes('instagram') && !url.includes('trustpilot')) {
                            if (url.includes('?ref=') || url.includes('/download/') || url.includes('/d/') || url.match(/\.(zip|rar|iso|7z|exe|dmg|pkg)$/i)) {
                                return url;
                            }
                            posibles.push(url);
                        }
                    }
                    // Fallback: si no hay un match exacto, usamos el último enlace externo encontrado
                    if (posibles.length > 0) {
                        return posibles[posibles.length - 1];
                    }
                    return null;
                }''')

            if not enlace_final:
                html_dump = await page.content()
                from config import logger
                logger.warning(f"FCR-Scraper: No se encontró ningún enlace. Generando volcado HTML.")
                return {"html_dump": html_dump}
                    
            if enlace_final and (enlace_final.lower().endswith(".dmg") or enlace_final.lower().endswith(".pkg")):
                from config import logger
                logger.warning(f"FCR-Scraper: Archivo Mac detectado y descartado: {enlace_final}")
                return None

            # Extraer publisher (JSON)
            publisher = "Unknown"
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
                    "titulo": titulo_real,
                    "fecha_actualizacion": fecha_actualizacion,
                    "requisitos": requisitos,
                    "publisher": publisher,
                    "languages": idiomas
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
    
    # Obtener el índice de sitemaps para saber cuál es el último
    try:
        req_idx = urllib.request.Request("https://filecr.com/sitemap.xml", headers={'User-Agent': 'Mozilla/5.0'})
        xml_idx = urllib.request.urlopen(req_idx).read().decode('utf-8')
        sitemaps = re.findall(r'<loc>https://filecr.com/(post-sitemap\d+\.xml)</loc>', xml_idx)
        max_idx = len(sitemaps) if sitemaps else 20
    except Exception:
        max_idx = 20
        
    # Dar mayor peso a los sitemaps más recientes para mantener el contenido fresco
    if random.random() < 0.6 and max_idx >= 3:
        sitemap_idx = random.randint(max_idx - 2, max_idx)
    else:
        sitemap_idx = random.randint(1, max_idx)
        
    sitemap_url = f"https://filecr.com/post-sitemap{sitemap_idx}.xml"
    logger.info(f"FCR-Scraper: Evadiendo Cloudflare leyendo sitemap: {sitemap_url}")
    
    try:
        req = urllib.request.Request(sitemap_url, headers={'User-Agent': 'Mozilla/5.0'})
        xml_data = urllib.request.urlopen(req).read().decode('utf-8')
        
        # Extraer slugs de windows
        slugs = re.findall(r'<loc>https://filecr.com/windows/([^<]+)/</loc>', xml_data)
        
        if slugs:
            random.shuffle(slugs)
            slugs = slugs[:25]
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

async def scrape_premium():
    """
    Rastrea sitemaps buscando específicamente ISOs, Adobes, y sistemas operativos pesados.
    Se enfoca en los últimos sitemaps para conseguir las versiones más recientes.
    """
    from config import logger
    import urllib.request
    import re
    import random
    
    logger.info("Iniciando Módulo Caza-Premium (ISOs, Adobes, etc) de FileCR...")
    resultados = []
    
    # Obtener el índice de sitemaps para saber cuál es el último
    try:
        req_idx = urllib.request.Request("https://filecr.com/sitemap.xml", headers={'User-Agent': 'Mozilla/5.0'})
        xml_idx = urllib.request.urlopen(req_idx).read().decode('utf-8')
        sitemaps = re.findall(r'<loc>https://filecr.com/(post-sitemap\d+\.xml)</loc>', xml_idx)
        max_idx = len(sitemaps) if sitemaps else 20
    except Exception:
        max_idx = 20
        
    # Dar un 70% de probabilidad de buscar en los últimos 3 sitemaps (más recientes)
    if random.random() < 0.7 and max_idx >= 3:
        sitemap_idx = random.randint(max_idx - 2, max_idx)
    else:
        sitemap_idx = random.randint(1, max_idx)
        
    sitemap_url = f"https://filecr.com/post-sitemap{sitemap_idx}.xml"
    logger.info(f"FCR-PremiumHunter: Escaneando sitemap en busca de Software Premium: {sitemap_url}")
    
    premium_keywords = ['windows-11', 'windows-10', 'windows-server', 'microsoft-office', 'ubuntu', 'kali-linux', 'vmware', 'acronis', 'adobe', 'autodesk', 'corel', 'vegas']
    
    try:
        req = urllib.request.Request(sitemap_url, headers={'User-Agent': 'Mozilla/5.0'})
        xml_data = urllib.request.urlopen(req).read().decode('utf-8')
        
        slugs = re.findall(r'<loc>https://filecr.com/windows/([^<]+)/</loc>', xml_data)
        
        if slugs:
            # Filtrar solo aquellos slugs que contengan nuestras palabras clave premium
            premium_slugs = [slug for slug in slugs if any(kw in slug.lower() for kw in premium_keywords)]
            
            if not premium_slugs:
                logger.info(f"FCR-PremiumHunter: No se encontró software premium en este sitemap.")
            else:
                random.shuffle(premium_slugs)
                premium_slugs = premium_slugs[:10]  # Procesar max 10
                for slug in premium_slugs:
                    titulo = slug.replace('-', ' ').title()
                    url_post = f"https://filecr.com/windows/{slug}/"
                    logger.info(f"FCR-PremiumHunter: Añadiendo software premium a la cola: {titulo}")
                    resultados.append({
                        "titulo": titulo,
                        "version": "Latest",
                        "categoria": "Premium Software",
                        "url_origen": url_post,
                        "descripcion": "",
                        "imagen_url": "",
                        "fecha_actualizacion": "Reciente"
                    })
        else:
            logger.error("No se encontraron slugs de /windows/ en el sitemap premium.")
            
    except Exception as e:
        logger.error(f"Error al leer sitemap para ISOs {sitemap_url}: {e}")
        
    return resultados

# Funciones deprecadas mantenidas exclusivamente por consistencia de importaciones
async def scrape_github_releases():
    return []

async def scrape_massgrave():
    return []