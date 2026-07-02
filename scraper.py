import asyncio
import re
import os
import io
import aiohttp
from PIL import Image

async def download_and_crop_image(url: str) -> str:
    """Descarga una imagen, recorta la mitad inferior (marca de agua GetIntoPC) y la guarda localmente."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}) as resp:
                if resp.status != 200:
                    return ""
                img_data = await resp.read()
        
        def process_img(data):
            img = Image.open(io.BytesIO(data))
            # GetIntoPC pone una gran marca de agua amarilla en la parte inferior.
            # Recortaremos manteniendo el 82% superior de la imagen para evitar cortar contenido útil.
            width, height = img.size
            cropped = img.crop((0, 0, width, int(height * 0.82)))
            
            os.makedirs("temp_images", exist_ok=True)
            filename = f"temp_images/{hash(url)}.jpg"
            # Si es RGBA (PNG transparente), convertir a RGB
            if cropped.mode in ('RGBA', 'P'):
                cropped = cropped.convert('RGB')
            cropped.save(filename, "JPEG", quality=85)
            return filename
            
        return await asyncio.to_thread(process_img, img_data)
    except Exception as e:
        from config import logger
        logger.error(f"Error procesando imagen: {e}")
        return ""
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright
from config import logger, PROXY_URL

BANNED_WORDS = [
    "games", "gaming", "arcade", "action", "rpg", "steam", "nintendo",
    "game", "playstation", "xbox", "emulators", "emulator", "roms", "rom"
]
BANNED_WORDS_REGEX = [re.compile(rf'\b{word}\b') for word in BANNED_WORDS]

def es_juego(titulo, descripcion, categoria):
    texto_a_revisar = f"{titulo} {descripcion} {categoria}".lower()
    for i, pattern in enumerate(BANNED_WORDS_REGEX):
        if pattern.search(texto_a_revisar):
            logger.info(f"Filtro Anti-Juego activado. Bloqueado por palabra: {BANNED_WORDS[i]} | Título: {titulo}")
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
            return None # Early return para no procesar sobre un DOM vacío/roto
        
        # --- Extracción de metadatos solicitada por el usuario ---
        version = "Desconocida"
        descripcion = ""
        imagen_url = ""
        categoria_real = "Software"
        fecha_actualizacion = "Reciente"
        requisitos = ""
        idiomas = "Multilingual"
        titulo_real = titulo
        
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
                
                let version = 'Desconocida';
                let v_tag = document.querySelector('.version') || document.querySelector('.app-version');
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
            titulo_real = metadata.get('title', titulo)
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
        
        # Localizamos el botón que inicializa la secuencia de descarga
        boton_descarga = page.locator("div[class^='version_download'] button, button.btn-primary.large, a.btn-download, a:has-text('Direct Download')").first
        if await boton_descarga.count() == 0:
            boton_descarga = page.locator("button:has-text('Download')").first

        if await boton_descarga.count() > 0:
            await boton_descarga.scroll_into_view_if_needed()
            
            # Prevenir popups incontrolables
            await page.evaluate("() => { document.querySelectorAll('form, a').forEach(e => e.removeAttribute('target')); }")
            
            enlace_final = None
            try:
                import asyncio
                # Primero intentamos atrapar un nuevo tab
                async with context.expect_page(timeout=10000) as new_page_info:
                    await boton_descarga.click()
                new_page = await new_page_info.value
                await new_page.wait_for_load_state("domcontentloaded")
                enlace_final = new_page.url
                await new_page.close()
                from config import logger
                logger.info(f"FCR-Scraper: Nuevo tab interceptado a posible página intermedia: {enlace_final}")
            except:
                try:
                    import asyncio
                    # Intentamos atrapar una navegación a una página intermedia en la misma pestaña
                    async with page.expect_navigation(timeout=10000):
                        await boton_descarga.click()
                    enlace_final = page.url
                    from config import logger
                    logger.info(f"FCR-Scraper: Navegación interceptada a posible página intermedia: {enlace_final}")
                except:
                    # Si no hubo navegación, intentamos interceptar un archivo directamente
                    try:
                        import asyncio
                        async with page.expect_download(timeout=20000) as download_info:
                            await boton_descarga.click()
                            await asyncio.sleep(15)
                            
                            btn_final = page.locator("a:has-text('Click here to download'), button:has-text('Download')").first
                            if await btn_final.count() > 0 and await btn_final.is_visible():
                                await btn_final.click()
                                
                        download = await download_info.value
                        enlace_final = download.url
                        await download.cancel()
                        from config import logger
                        logger.info(f"FCR-Scraper: URL de descarga interceptada nativamente: {enlace_final}")
                    except Exception as e:
                        from config import logger
                        logger.warning(f"FCR-Scraper: No se interceptó descarga automática ({e}). Cayendo a DOM extraction...")
                
            if not enlace_final or enlace_final == url_post:
                # Extraer el enlace usando JavaScript. Buscamos primero en el botón, luego en enlaces
                enlace_final = await page.evaluate('''() => {
                    // Try to get href of the download button itself using standard CSS
                    let btn = document.querySelector("div[class^='version_download'] button, button.btn-primary.large, a.btn-download");
                    if (!btn) {
                        let btns = Array.from(document.querySelectorAll("button, a"));
                        btn = btns.find(b => b.innerText && b.innerText.toLowerCase().includes('download') && (b.tagName === 'BUTTON' || b.className.includes('btn')));
                    }
                    if (btn && btn.closest('a') && btn.closest('a').href) return btn.closest('a').href;
                    if (btn && btn.getAttribute('href')) return btn.getAttribute('href');
                    
                    let allElements = document.querySelectorAll('a, button, div, span');
                    let valid_urls = [];
                    for (let el of allElements) {
                        let url = el.getAttribute('href') || el.getAttribute('data-href') || el.getAttribute('data-url');
                        if (url && !url.startsWith('#') && !url.startsWith('javascript:') && !url.includes('filecr.com') && !url.startsWith('/') && !url.includes('t.me') && !url.includes('twitter') && !url.includes('youtube') && !url.includes('facebook') && !url.includes('reddit') && !url.includes('pinterest') && !url.includes('vk.com') && !url.includes('instagram') && !url.includes('trustpilot')) {
                            if (url.startsWith('http') || url.startsWith('//')) {
                                // Prefer urls that look like intermediate pages or downloads
                                if (url.includes('?ref=') || url.includes('/download/') || url.includes('/d/') || url.match(/\.(zip|rar|iso|7z|exe|dmg|pkg)$/i) || url.includes('apkteal') || url.includes('veryapk') || url.includes('anygame')) {
                                    return url;
                                }
                                valid_urls.push(url);
                            }
                        }
                    }
                    return valid_urls.length > 0 ? valid_urls[valid_urls.length - 1] : null;
                }''')

            if enlace_final:
                import re
                if not re.search(r'\.(zip|rar|iso|7z|exe|dmg|pkg|msi)$', enlace_final, re.I) and "mega.nz" not in enlace_final and "drive.google.com" not in enlace_final:
                    from config import logger
                    logger.info(f"FCR-Scraper: URL parece ser una intermedia ({enlace_final}). Intentando resolverla con Playwright...")
                    try:
                        import asyncio
                        await page.goto(enlace_final, timeout=45000, wait_until="domcontentloaded")
                        await asyncio.sleep(15)
                        
                        async with page.expect_download(timeout=45000) as download_info:
                            btn_intermedio = page.locator("a:has-text('Click here to download'), a:has-text('Get Link'), button:has-text('Download'), a:has-text('Download'), a.download-btn, button.download-btn, .dl-btn").first
                            if await btn_intermedio.count() > 0 and await btn_intermedio.is_visible():
                                await btn_intermedio.click()
                            await asyncio.sleep(10)
                            
                        download = await download_info.value
                        enlace_final = download.url
                        await download.cancel()
                        logger.info(f"FCR-Scraper: URL real obtenida desde intermedia: {enlace_final}")
                    except Exception as e:
                        logger.warning(f"FCR-Scraper: No se pudo resolver la página intermedia ({e}). Se devolverá la original.")
            
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

    import httpx
    import re
    import random
    
    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            # Obtener el índice de sitemaps para saber cuál es el último
            try:
                resp_idx = await client.get("https://filecr.com/sitemap.xml", headers={'User-Agent': 'Mozilla/5.0'})
                xml_idx = resp_idx.text
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
                resp = await client.get(sitemap_url, headers={'User-Agent': 'Mozilla/5.0'})
                xml_data = resp.text
                
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
                
    except Exception as general_e:
        logger.error(f"Error general en scrape_filecr: {general_e}")
        
    return resultados

async def scrape_premium():
    """
    Rastrea sitemaps buscando específicamente ISOs, Adobes, y sistemas operativos pesados.
    Se enfoca en los últimos sitemaps para conseguir las versiones más recientes.
    """
    from config import logger
    import httpx
    import re
    import random
    
    logger.info("Iniciando Módulo Caza-Premium (ISOs, Adobes, etc) de FileCR...")
    resultados = []
    
    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            # Obtener el índice de sitemaps para saber cuál es el último
            try:
                resp_idx = await client.get("https://filecr.com/sitemap.xml", headers={'User-Agent': 'Mozilla/5.0'})
                xml_idx = resp_idx.text
                sitemaps = re.findall(r'<loc>https://filecr.com/(post-sitemap\d+\.xml)</loc>', xml_idx)
                max_idx = len(sitemaps) if sitemaps else 20
            except Exception:
                max_idx = 20
                
            premium_keywords = ['windows-11', 'windows-10', 'windows-server', 'microsoft-office', 'ubuntu', 'kali-linux', 'vmware', 'acronis', 'adobe', 'autodesk', 'corel', 'vegas', 'antivirus', 'kaspersky', 'eset']
            
            # Escanear los últimos 5 sitemaps (los más recientes) para tener mayor probabilidad de encontrar ISOs y actualizaciones de Adobe
            start_idx = max(1, max_idx - 4)
            premium_slugs_totales = []
            
            for sitemap_idx in range(start_idx, max_idx + 1):
                sitemap_url = f"https://filecr.com/post-sitemap{sitemap_idx}.xml"
                try:
                    resp = await client.get(sitemap_url, headers={'User-Agent': 'Mozilla/5.0'})
                    xml_data = resp.text
                    slugs = re.findall(r'<loc>https://filecr.com/windows/([^<]+)/</loc>', xml_data)
                    if slugs:
                        premium_slugs = [slug for slug in slugs if any(kw in slug.lower() for kw in premium_keywords)]
                        premium_slugs_totales.extend(premium_slugs)
                except Exception as e:
                    logger.error(f"Error al leer sitemap para ISOs {sitemap_url}: {e}")
                    
            if not premium_slugs_totales:
                logger.info("FCR-PremiumHunter: No se encontró software premium en los últimos sitemaps.")
            else:
                # Remover duplicados
                premium_slugs_totales = list(set(premium_slugs_totales))
                random.shuffle(premium_slugs_totales)
                premium_slugs_totales = premium_slugs_totales[:15]  # Procesar max 15
                
                for slug in premium_slugs_totales:
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
    except Exception as general_e:
        logger.error(f"Error general en scrape_premium: {general_e}")
            
    return resultados

async def scrape_search(query, require_os=False):
    """
    Busca un programa en FileCR usando Playwright.
    Retorna el primer resultado que no sea un juego.
    Si require_os es True, filtra estrictamente por categorías de Sistemas Operativos.
    """
    from config import logger
    from playwright.async_api import async_playwright
    import urllib.parse
    
    logger.info(f"FCR-Search: Buscando '{query}' (Requiere OS: {require_os})...")
    url = f"https://filecr.com/search/?query={urllib.parse.quote(query)}"
    
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context()
            page = await context.new_page()
            
            await page.set_extra_http_headers({
                "Accept-Language": "es-ES,es;q=0.9,en;q=0.8"
            })
            
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            
            # Extraer resultados
            resultados = await page.evaluate('''() => {
                let items = [];
                let next_data = document.getElementById('__NEXT_DATA__');
                if (next_data) {
                    try {
                        let json = JSON.parse(next_data.innerText);
                        let posts = json.props?.pageProps?.posts || [];
                        for (let p of posts) {
                            items.push({
                                titulo: p.title || '',
                                url_origen: p.slug ? "https://filecr.com/windows/" + p.slug + "/" : "",
                                categoria: p.categories?.primary?.name || "Software",
                                version: "Latest",
                                descripcion: "",
                                imagen_url: "",
                                fecha_actualizacion: "Reciente"
                            });
                        }
                    } catch(e) {}
                }
                
                if (items.length === 0) {
                    // Fallback DOM extraction
                    let cards = document.querySelectorAll('article');
                    cards.forEach(c => {
                        let a = c.querySelector('a');
                        let t = c.querySelector('h3, h2');
                        let cat = c.querySelector('.category') ? c.querySelector('.category').innerText : "Software";
                        if (a && t && a.href.includes('/windows/')) {
                            items.push({
                                titulo: t.innerText.trim(),
                                url_origen: a.href,
                                categoria: cat.trim(),
                                version: "Latest",
                                descripcion: "",
                                imagen_url: "",
                                fecha_actualizacion: "Reciente"
                            });
                        }
                    });
                }
                return items;
            }''')
            
            await browser.close()
            
            for item in resultados:
                if item['url_origen'] and '/windows/' in item['url_origen']:
                    if not es_juego(item['titulo'], "", item['categoria']):
                        if require_os:
                            cat_lower = item['categoria'].lower()
                            if "operating system" in cat_lower or "os" in cat_lower or "windows" in cat_lower:
                                return item
                            else:
                                continue # No es OS, saltar al siguiente
                        return item
                        
    except Exception as e:
        logger.error(f"Error en scrape_search_fcr: {e}")
        
    return None

async def scrape_search_multiple_fcr(query, require_os=False, limit=3):
    """Retorna una lista de múltiples resultados desde FileCR."""
    import urllib.parse
    url = f"https://filecr.com/search/?query={urllib.parse.quote(query)}"
    
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
            page = await context.new_page()
            await page.set_extra_http_headers({"Accept-Language": "es-ES,es;q=0.9,en;q=0.8"})
            
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            
            resultados = await page.evaluate('''() => {
                let items = [];
                let next_data = document.getElementById('__NEXT_DATA__');
                if (next_data) {
                    try {
                        let json = JSON.parse(next_data.innerText);
                        let posts = json.props?.pageProps?.posts || [];
                        for (let p of posts) {
                            items.push({
                                titulo: p.title || '',
                                url_origen: p.slug ? "https://filecr.com/windows/" + p.slug + "/" : "",
                                categoria: p.categories?.primary?.name || "Software",
                                version: "Latest"
                            });
                        }
                    } catch(e) {}
                }
                
                if (items.length === 0) {
                    let cards = document.querySelectorAll('article');
                    cards.forEach(c => {
                        let a = c.querySelector('a');
                        let t = c.querySelector('h3, h2');
                        let cat = c.querySelector('.category') ? c.querySelector('.category').innerText : "Software";
                        if (a && t && a.href.includes('/windows/')) {
                            items.push({
                                titulo: t.innerText.trim(),
                                url_origen: a.href,
                                categoria: cat.trim(),
                                version: "Latest"
                            });
                        }
                    });
                }
                return items;
            }''')
            
            await browser.close()
            
            valid_items = []
            for item in resultados:
                if len(valid_items) >= limit: break
                if item['url_origen'] and '/windows/' in item['url_origen']:
                    if not es_juego(item['titulo'], "", item['categoria']):
                        if require_os:
                            cat_lower = item['categoria'].lower()
                            if not ("operating system" in cat_lower or "os" in cat_lower or "windows" in cat_lower):
                                continue
                        valid_items.append(item)
            return valid_items
            
    except Exception as e:
        logger.error(f"Error en scrape_search_multiple_fcr: {e}")
        
    return []

# Funciones deprecadas mantenidas exclusivamente por consistencia de importaciones
async def scrape_github_releases():
    return []

async def scrape_massgrave():
    return []

# --- GETINTOPC IMPLEMENTATION ---

async def scrape_search_getintopc(query, require_os=False):
    res = await scrape_search_multiple_gpc(query, require_os, limit=1)
    return res[0] if res else None

async def scrape_search_multiple_gpc(query, require_os=False, limit=3):
    import urllib.parse
    url = f"https://getintopc.com/?s={urllib.parse.quote(query)}"
    
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
            page = await context.new_page()
            
            await page.goto(url, wait_until="domcontentloaded", timeout=40000)
            
            items = await page.evaluate('''() => {
                let posts = document.querySelectorAll('.post');
                let results = [];
                for(let p of posts) {
                    let titleNode = p.querySelector('.post-title a') || p.querySelector('.title a');
                    if(!titleNode) continue;
                    let titulo = titleNode.innerText.trim();
                    let url_origen = titleNode.href;
                    
                    let imgNode = p.querySelector('img');
                    let imagen_url = imgNode ? imgNode.src : "";
                    
                    let categoria = "Software";
                    let descNode = p.querySelector('.post-content p, .entry-content p, .post p');
                    let descripcion = descNode ? descNode.innerText.trim() : "";
                    
                    results.push({titulo, url_origen, imagen_url, categoria, descripcion, fecha_actualizacion: "Reciente"});
                }
                return results;
            }''')
            
            await browser.close()
            
            if not items:
                return []
                
            valid_items = []
            for item in items:
                if len(valid_items) >= limit: break
                titulo_lower = item['titulo'].lower()
                if "mac" in titulo_lower:
                    continue
                if es_juego(item['titulo'], item['descripcion'], item['categoria']):
                    continue
                
                match = re.search(r'\b(20\d{2}|v?\d+\.\d+(\.\d+)?)\b', item['titulo'])
                version = match.group(1) if match else "Latest"
                item['version'] = version
                
                if require_os:
                    if "windows" not in titulo_lower and "linux" not in titulo_lower and "ubuntu" not in titulo_lower and "iso" not in titulo_lower:
                        continue
                
                valid_items.append(item)
            return valid_items
            
    except Exception as e:
        logger.error(f"GetIntoPC-Scraper Error de búsqueda múltiple: {e}")
        return []

async def obtener_enlace_dinamico_getintopc(context, url_post, titulo="Software"):
    page = None
    try:
        page = await context.new_page()
        await page.goto(url_post, wait_until="domcontentloaded", timeout=40000)
        
        metadata = await page.evaluate('''() => {
            let data = { requisitos: "", descripcion: "", publisher: "GetIntoPC", imagen_url: "", categoria: "Software" };
            
            // Buscar la primera imagen del artículo
            let img = document.querySelector('.post-content img, .entry-content img');
            if (img) data.imagen_url = img.src;
            
            // Descripción limpia (Solo el primer párrafo válido)
            let p_tags = document.querySelectorAll('.post-content p, .entry-content p');
            for(let i=0; i<p_tags.length; i++) {
                let text = p_tags[i].innerText.trim();
                let lowerText = text.toLowerCase();
                // Ignorar parrafos de spam o muy cortos
                if(text.length > 40 && !lowerText.includes('click on below button') && !lowerText.includes('password') && !lowerText.includes('prior to start') && !lowerText.includes('below are some noticeable')) {
                    // Saltar la primera frase genérica de GetIntoPC si hay más párrafos
                    if (i === 0 && lowerText.includes('standalone setup of') && p_tags.length > 1) continue;
                    
                    // Remover oraciones tipo "You can also download X Free Download"
                    text = text.replace(/You can also download.*?(?:Free Download|latest version)\.?/gi, '').trim();
                    if (text.length > 40) {
                        data.descripcion = text;
                        break;
                    }
                }
            }
            
            // Requisitos
            let req_header = Array.from(document.querySelectorAll('h3, h2, h4')).find(el => el.innerText.toLowerCase().includes('requirements') || el.innerText.toLowerCase().includes('system'));
            if(req_header) {
                let curr = req_header.nextElementSibling;
                let req_text = "";
                // Look for UL lists first
                while(curr && curr.tagName !== 'H2' && curr.tagName !== 'H3' && curr.tagName !== 'H4') {
                    if (curr.tagName === 'UL') {
                        req_text += curr.innerText.trim() + "\\n";
                    }
                    curr = curr.nextElementSibling;
                }
                
                if(req_text.trim()) {
                    data.requisitos = req_text.trim();
                } else {
                    // Fallback to P if no UL found
                    curr = req_header.nextElementSibling;
                    while(curr && curr.tagName !== 'H2' && curr.tagName !== 'H3' && curr.tagName !== 'H4') {
                        if (curr.tagName === 'P' && curr.innerText.length > 20 && !curr.innerText.toLowerCase().includes('before you start')) {
                            req_text += curr.innerText.trim() + "\\n";
                        }
                        curr = curr.nextElementSibling;
                    }
                    data.requisitos = req_text.trim();
                }
            }
            
            // Categoría
            let breadcrumbs = document.querySelectorAll('a[rel="category tag"]');
            if(breadcrumbs.length > 0) {
                let cats = [];
                for(let b of breadcrumbs) {
                    let catText = b.innerText.trim();
                    cats.push(catText);
                }
                if(cats.length > 0) data.categoria = cats.join(', ');
            }
            
            // Version (del título)
            let pageTitle = document.title || document.querySelector('h1')?.innerText || "";
            // Usa el objecto RegExp para evitar problemas de parseo de backslash con el string de python
            let versionMatch = pageTitle.match(new RegExp("\\\\b(20\\\\d{2}|v?\\\\d+\\\\.\\\\d+(\\\\.\\\\d+)?)\\\\b"));
            data.version = versionMatch ? versionMatch[1] : "Latest";
            
            return data;
        }''')
        
        enlace_final = None
        
        btn_download = page.locator("input[type='submit'][value*='Download'], input[type='image'][src*='download'], button:has-text('Download'), a:has-text('Download Full Setup'), a:has-text('Download Full')").first
        if await btn_download.count() > 0:
            # Eliminar target=_blank para evitar popups
            await page.evaluate("() => { document.querySelectorAll('form, a').forEach(e => e.removeAttribute('target')); }")
            
            try:
                # Aumentamos el timeout a 120s para soportar cuentas regresivas de GetIntoPC
                async with page.expect_download(timeout=120000) as download_info:
                    await btn_download.click()
                download = await download_info.value
                enlace_final = download.url
                await download.cancel()
            except Exception as wait_e:
                logger.warning(f"GetIntoPC-Scraper Timeout esperando descarga: {wait_e}")
                
        if not enlace_final:
            logger.error("GetIntoPC-Scraper: Falló la extracción del enlace.")
            
        logger.info(f"GetIntoPC-Scraper: URL Final interceptada: {enlace_final}")
        
        imagen_final = metadata.get('imagen_url', '')
        if imagen_final:
            logger.info(f"GetIntoPC-Scraper: Procesando imagen para quitar marca de agua: {imagen_final}")
            cropped_img = await download_and_crop_image(imagen_final)
            if cropped_img:
                imagen_final = cropped_img
                
        match = re.search(r'\b(20\d{2}|v?\d+\.\d+(\.\d+)?)\b', titulo)
        version_ext = match.group(1) if match else "Latest"
        
        categoria = metadata.get('categoria', '')
        if not categoria or categoria == 'Software':
            try:
                parts = [p.replace('-', ' ').title() for p in url_post.strip('/').split('/') if p not in ['softwares', 'http:', 'https:', 'getintopc.com', '']]
                if len(parts) >= 2:
                    categoria = parts[-2]
                elif len(parts) == 1:
                    categoria = parts[0]
                else:
                    categoria = 'Software'
            except:
                categoria = 'Software'
                
        return {
            'enlace': enlace_final,
            'titulo': titulo,
            'version': version_ext,
            'categoria': categoria,
            'descripcion': metadata.get('descripcion', ''),
            'imagen_url': imagen_final,
            'fecha_actualizacion': "Reciente",
            'requisitos': metadata.get('requisitos', ''),
            'idiomas': "Multilingual",
            'publisher': metadata.get('publisher', 'GetIntoPC')
        }
    except Exception as e:
        logger.error(f"GetIntoPC-Scraper Error extrayendo link: {e}")
        return None
    finally:
        if page:
            await page.close()