from pathlib import Path
from psycopg.types.json import Jsonb


def init(conn):
    with conn.transaction():
        conn.execute((Path(__file__).parent.parent / 'schema.sql').read_text())


def start(conn, options):
    with conn.transaction():
        return conn.execute('INSERT INTO etl_runs (status, options, next_page) VALUES (%s,%s,%s) RETURNING id',
                            ('running', Jsonb(options), options['start_page'])).fetchone()[0]


def finish(conn, run_id, status, error=None):
    with conn.transaction():
        conn.execute('UPDATE etl_runs SET status=%s, finished=now(), error=%s WHERE id=%s', (status, error, run_id))


def upsert(conn, table, data, keys):
    columns = list(data)
    vals = [Jsonb(v) if k in ('raw', 'report_fields', 'report_raw') and v is not None else v for k, v in data.items()]
    updates = [f'{k}=EXCLUDED.{k}' for k in columns if k not in keys]
    if table == 'facilities':
        updates.append('last_seen_at=now()')
    sql = f"INSERT INTO {table} ({','.join(columns)}) VALUES ({','.join(['%s'] * len(columns))}) ON CONFLICT ({','.join(keys)}) DO UPDATE SET {','.join(updates)}"
    conn.execute(sql, vals)
