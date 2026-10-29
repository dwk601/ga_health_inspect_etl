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
    vals = [Jsonb(v) if k == 'raw' else v for k, v in data.items()]
    updates = [f'{k}=EXCLUDED.{k}' for k in columns if k not in keys]
    if table == 'facilities':
        updates.append('last_seen_at=now()')
    sql = f"INSERT INTO {table} ({','.join(columns)}) VALUES ({','.join(['%s'] * len(columns))}) ON CONFLICT ({','.join(keys)}) DO UPDATE SET {','.join(updates)}"
    conn.execute(sql, vals)


def save_report(conn, facility_id, inspection_id, fields, raw):
    conn.execute('UPDATE inspections SET report_fields=%s, report_raw=%s, report_fetched_at=now(), report_error=NULL WHERE facility_id=%s AND source_id=%s',
                 (Jsonb(fields), Jsonb(raw), facility_id, inspection_id))


def save_report_error(conn, facility_id, inspection_id, exc):
    conn.execute('UPDATE inspections SET report_error=%s WHERE facility_id=%s AND source_id=%s',
                 (f'{type(exc).__name__}: {exc}', facility_id, inspection_id))
