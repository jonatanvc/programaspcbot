import json
import asyncpg
from config import DATABASE_URL, logger

async def get_db_pool():
    """
    Crea y devuelve un pool de conexiones a la base de datos PostgreSQL.
    """
    try:
        pool = await asyncpg.create_pool(DATABASE_URL)
        logger.info("Conexión al pool de PostgreSQL establecida.")
        return pool
    except Exception as e:
        logger.error(f"Error crítico al conectar a PostgreSQL: {e}")
        raise

async def init_db(pool):
    """
    Inicializa las tablas en la base de datos.
    """
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
        UNIQUE(titulo, version)
    );
    
    CREATE TABLE IF NOT EXISTS usuarios (
        telegram_id BIGINT PRIMARY KEY,
        join_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """
    async with pool.acquire() as conn:
        await conn.execute(query)
        logger.info("Tablas 'programas' y 'usuarios' verificadas/creadas en PostgreSQL.")

async def register_user(pool, telegram_id):
    """
    Registra un usuario en la base de datos si no existe.
    """
    query = """
    INSERT INTO usuarios (telegram_id) VALUES ($1)
    ON CONFLICT (telegram_id) DO NOTHING;
    """
    async with pool.acquire() as conn:
        await conn.execute(query, telegram_id)

async def increment_download(pool, program_id):
    """
    Incrementa el contador de descargas de un programa.
    """
    query = "UPDATE programas SET descargas_count = descargas_count + 1 WHERE id = $1;"
    async with pool.acquire() as conn:
        await conn.execute(query, int(program_id))

async def get_stats(pool):
    """
    Obtiene las estadísticas para el panel de administración.
    """
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

async def insert_program(pool, titulo, version, categoria, url_origen):
    """
    Inserta un nuevo programa en estado 'Pendiente'.
    """
    query = """
    INSERT INTO programas (titulo, version, categoria, url_origen, estado)
    VALUES ($1, $2, $3, $4, 'Pendiente')
    ON CONFLICT (titulo, version) DO NOTHING
    RETURNING id;
    """
    async with pool.acquire() as conn:
        result = await conn.fetchval(query, titulo, version, categoria, url_origen)
        return result

async def program_exists(pool, titulo, version):
    """
    Verifica si un programa ya existe en la base de datos.
    """
    query = "SELECT 1 FROM programas WHERE titulo = $1 AND version = $2;"
    async with pool.acquire() as conn:
        result = await conn.fetchval(query, titulo, version)
        return bool(result)

async def get_pending_programs(pool):
    """
    Devuelve los programas que están en estado 'Pendiente'.
    """
    query = "SELECT * FROM programas WHERE estado = 'Pendiente' ORDER BY id ASC;"
    async with pool.acquire() as conn:
        records = await conn.fetch(query)
        return records

async def update_status(pool, program_id, status):
    """
    Actualiza el estado de un programa. Valores: 'Pendiente', 'Procesando', 'Publicado', 'Error'.
    """
    query = "UPDATE programas SET estado = $1 WHERE id = $2;"
    async with pool.acquire() as conn:
        await conn.execute(query, status, program_id)

async def update_file_ids(pool, program_id, file_ids):
    """
    Actualiza la lista de file_ids en Telegram, almacenados en formato JSON TEXT.
    """
    file_ids_json = json.dumps(file_ids)
    query = "UPDATE programas SET telegram_file_ids = $1 WHERE id = $2;"
    async with pool.acquire() as conn:
        await conn.execute(query, file_ids_json, program_id)

async def get_program_by_id(pool, program_id):
    """
    Devuelve un programa por su ID.
    """
    query = "SELECT * FROM programas WHERE id = $1;"
    async with pool.acquire() as conn:
        record = await conn.fetchrow(query, int(program_id))
        return record

async def search_programs(pool, search_query):
    """
    Busca programas por nombre usando ILIKE para coincidencias parciales.
    """
    query = "SELECT * FROM programas WHERE titulo ILIKE $1 AND estado = 'Publicado' ORDER BY id DESC LIMIT 10;"
    async with pool.acquire() as conn:
        records = await conn.fetch(query, f"%{search_query}%")
        return records

async def get_latest_programs(pool, limit=5):
    """
    Devuelve los N últimos programas publicados.
    """
    query = "SELECT * FROM programas WHERE estado = 'Publicado' ORDER BY id DESC LIMIT $1;"
    async with pool.acquire() as conn:
        records = await conn.fetch(query, limit)
        return records
