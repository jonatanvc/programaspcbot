import json
import asyncpg
from config import DATABASE_URL, logger

async def get_db_pool():
    try:
        pool = await asyncpg.create_pool(DATABASE_URL)
        logger.info("Conexión al pool de PostgreSQL establecida.")
        return pool
    except Exception as e:
        logger.error(f"Error crítico al conectar a PostgreSQL: {e}")
        raise

async def init_db(pool):
    query = """
    CREATE TABLE IF NOT EXISTS programas (
        id SERIAL PRIMARY KEY,
        titulo TEXT NOT NULL,
        version TEXT,
        categoria TEXT,
        url_origen TEXT NOT NULL,
        estado TEXT DEFAULT 'Pendiente',
        telegram_file_ids TEXT,
        descargas_count INTEGER DEFAULT 0,
        descripcion TEXT DEFAULT '',
        UNIQUE(titulo, version)
    );
    
    CREATE TABLE IF NOT EXISTS usuarios (
        telegram_id BIGINT PRIMARY KEY,
        fecha_registro TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """
    async with pool.acquire() as conn:
        await conn.execute(query)
        # Migracion en caliente para agregar la columna si la tabla ya existe
        try:
            await conn.execute("ALTER TABLE programas ADD COLUMN IF NOT EXISTS descripcion TEXT DEFAULT '';")
            await conn.execute("ALTER TABLE programas ADD COLUMN IF NOT EXISTS imagen_url TEXT DEFAULT '';")
            await conn.execute("ALTER TABLE programas ADD COLUMN IF NOT EXISTS fecha_actualizacion TEXT DEFAULT 'Reciente';")
            await conn.execute("ALTER TABLE programas ADD COLUMN IF NOT EXISTS publisher TEXT DEFAULT 'Unknown';")
            await conn.execute("ALTER TABLE programas ADD COLUMN IF NOT EXISTS languages TEXT DEFAULT 'Multilingual';")
        except Exception:
            pass
            
        # Creacion de indices para acelerar busquedas y filtros
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_programas_titulo ON programas(titulo);")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_programas_estado ON programas(estado);")
        
        logger.info("Tablas 'programas' y 'usuarios' e indices verificados/creados en PostgreSQL.")

async def register_user(pool, telegram_id):
    query = """
    INSERT INTO usuarios (telegram_id) VALUES ($1)
    ON CONFLICT (telegram_id) DO NOTHING;
    """
    async with pool.acquire() as conn:
        await conn.execute(query, telegram_id)

async def increment_download(pool, program_id):
    query = "UPDATE programas SET descargas_count = descargas_count + 1 WHERE id = $1;"
    async with pool.acquire() as conn:
        await conn.execute(query, int(program_id))

async def get_stats(pool):
    query_users = "SELECT COUNT(*) FROM usuarios;"
    query_downloads = "SELECT COALESCE(SUM(descargas_count), 0) FROM programas;"
    query_programs = "SELECT COUNT(*) FROM programas WHERE estado = 'Publicado';"
    query_pending = "SELECT COUNT(*) FROM programas WHERE estado = 'Pendiente';"
    
    async with pool.acquire() as conn:
        users = await conn.fetchval(query_users)
        downloads = await conn.fetchval(query_downloads)
        programs = await conn.fetchval(query_programs)
        pending = await conn.fetchval(query_pending)
        
    return {
        "usuarios_totales": users,
        "descargas_totales": downloads,
        "programas_publicados": programs,
        "cola_pendientes": pending
    }

async def insert_program(pool, titulo, version, categoria, url_origen, descripcion="", imagen_url="", fecha_actualizacion="Reciente", publisher="Unknown", languages="Multilingual"):
    query = """
    INSERT INTO programas (titulo, version, categoria, url_origen, descripcion, imagen_url, fecha_actualizacion, publisher, languages)
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
    ON CONFLICT (titulo, version) DO NOTHING
    RETURNING id;
    """
    async with pool.acquire() as conn:
        result = await conn.fetchval(query, titulo, version, categoria, url_origen, descripcion, imagen_url, fecha_actualizacion, publisher, languages)
        return result

async def program_exists(pool, titulo, version):
    query = "SELECT 1 FROM programas WHERE titulo = $1 AND version = $2;"
    async with pool.acquire() as conn:
        result = await conn.fetchval(query, titulo, version)
        return bool(result)

async def get_pending_programs(pool, limit=None):
    if limit:
        query = f"SELECT * FROM programas WHERE estado = 'Pendiente' ORDER BY id ASC LIMIT {int(limit)};"
    else:
        query = "SELECT * FROM programas WHERE estado = 'Pendiente' ORDER BY id ASC;"
        
    async with pool.acquire() as conn:
        records = await conn.fetch(query)
        # Convertir a lista de diccionarios para facilitar su uso
        return [dict(r) for r in records]

async def update_status(pool, program_id, status):
    query = "UPDATE programas SET estado = $1 WHERE id = $2;"
    async with pool.acquire() as conn:
        await conn.execute(query, status, program_id)

async def update_program_metadata(pool, program_id, version, categoria, descripcion, imagen_url):
    query = """
    UPDATE programas 
    SET version = $1, categoria = $2, descripcion = $3, imagen_url = $4
    WHERE id = $5;
    """
    async with pool.acquire() as conn:
        await conn.execute(query, version, categoria, descripcion, imagen_url, program_id)

async def update_file_ids(pool, program_id, file_ids):
    file_ids_json = json.dumps(file_ids)
    query = "UPDATE programas SET telegram_file_ids = $1 WHERE id = $2;"
    async with pool.acquire() as conn:
        await conn.execute(query, file_ids_json, program_id)

async def get_program_by_id(pool, program_id):
    query = "SELECT * FROM programas WHERE id = $1;"
    async with pool.acquire() as conn:
        record = await conn.fetchrow(query, int(program_id))
        return record

async def search_programs(pool, search_query):
    terms = search_query.strip().split()
    if not terms: return []
    
    conditions = []
    for i in range(len(terms)):
        conditions.append(f"titulo ILIKE ${i+1}")
        
    query = f"SELECT * FROM programas WHERE {' AND '.join(conditions)} ORDER BY id DESC LIMIT 10;"
    args = [f"%{term}%" for term in terms]
    
    async with pool.acquire() as conn:
        records = await conn.fetch(query, *args)
        return records

async def get_latest_programs(pool, limit=5):
    query = "SELECT * FROM programas ORDER BY id DESC LIMIT $1;"
    async with pool.acquire() as conn:
        records = await conn.fetch(query, limit)
        return records

# Nuevas funciones para manejo de errores (Panel Admin)
async def get_error_programs(pool):
    """Obtiene programas con estado 'Error'."""
    query = "SELECT * FROM programas WHERE estado = 'Error' ORDER BY id DESC LIMIT 10;"
    async with pool.acquire() as conn:
        records = await conn.fetch(query)
        return records

async def delete_program(pool, program_id):
    """Elimina un programa de la base de datos definitivamente."""
    query = "DELETE FROM programas WHERE id = $1;"
    async with pool.acquire() as conn:
        await conn.execute(query, int(program_id))

async def get_total_users(pool):
    """Obtiene el número total de usuarios registrados."""
    query = "SELECT COUNT(*) FROM usuarios;"
    async with pool.acquire() as conn:
        return await conn.fetchval(query)

async def get_all_users(pool):
    """Obtiene todos los IDs de usuarios para broadcasting."""
    query = "SELECT telegram_id FROM usuarios;"
    async with pool.acquire() as conn:
        records = await conn.fetch(query)
        return [r['telegram_id'] for r in records]

async def get_total_programs(pool):
    """Obtiene el total de programas por estado."""
    query = "SELECT estado, COUNT(*) as cantidad FROM programas GROUP BY estado;"
    async with pool.acquire() as conn:
        records = await conn.fetch(query)
        return {r['estado']: r['cantidad'] for r in records}
