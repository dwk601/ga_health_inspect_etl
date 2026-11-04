"""GNR Public Health (Gwinnett, Newton, Rockdale) publishes inspections outside the statewide portal."""
import html
import json
import re
import sys
from datetime import date, datetime, timedelta
from urllib.parse import quote
from zoneinfo import ZoneInfo

from psycopg.types.json import Jsonb
from scrapling.parser import Selector

from . import db

SITE = 'https://inspections.myhealthdepartment.com/'
JURISDICTION = 'gwinnett-ga'
COUNTIES = ('gcg', 'nch', 'rch')
FIRST_DATE = date(2024, 10, 1)  # The site returns nothing earlier.
# Search returns at most 25 rows per county per call, rejects offsets above 200,
# and lists only each permit's latest inspection within the date filter, so scan one day at a time.
PAGE_SIZE = 25
MAX_START = 200
FACILITY_PREFIX = 'gnr:'
REQUIRED_FIELDS = ('permitID', 'inspectionID', 'inspectionDate', 'establishmentName')


def run(conn, source, run_id, options):
    day = start_date(conn, options)
    conn.execute("UPDATE etl_runs SET options=jsonb_set(options, '{cutoff}', %s) WHERE id=%s",
                 (Jsonb(day.isoformat()), run_id))
    today = datetime.now(ZoneInfo('America/New_York')).date()
    while day <= today:
        rows = [row for county in COUNTIES for row in search_day(source, county, day)]
        with conn.transaction():
            for row in rows:
                ingest(conn, row)
            add_counters(conn, run_id, facilities=len({row['permitID'] for row in rows}), inspections=len(rows))
        day += timedelta(days=1)
    if options['reports']:
        fetch_details(conn, source, run_id, options['max_reports'])
    return 'exhausted' if options['full'] else 'caught_up'


def start_date(conn, options):
    latest = conn.execute('SELECT max(inspection_date) FROM gnr_inspections').fetchone()[0]
    if options['full'] or latest is None:
        return FIRST_DATE
    return max(FIRST_DATE, latest - timedelta(days=options['overlap_days']))


def search_day(source, county, day):
    rows = []
    for start in range(0, MAX_START + 1, PAGE_SIZE):
        page = source.post_json(SITE, {'task': 'searchInspections', 'data': {
            'path': JURISDICTION, 'programName': '', 'start': start, 'count': PAGE_SIZE, 'searchStr': '',
            'searchQueryOverride': None, 'lat': 0, 'lng': 0, 'sort': {},
            'filters': {'date': f'{day} to {day}', 'county': county, 'purpose': '', 'city': '', 'score': '', 'letterGrade': ''}}})
        rows.extend(page)
        if len(page) < PAGE_SIZE:
            return rows
    raise RuntimeError(f'More than {len(rows)} GNR inspections for {county} on {day}; search offset limit reached')


def ingest(conn, row):
    validate(row)
    db.upsert(conn, 'facilities', facility(row), ('source_id',))
    data = inspection(row)
    stored = conn.execute('SELECT raw FROM gnr_inspections WHERE source_id=%s', (data['source_id'],)).fetchone()
    if stored and stored[0] != row:
        data['detail_fetched_at'] = None
    db.upsert(conn, 'gnr_inspections', data, ('source_id',))


def validate(row):
    if not isinstance(row, dict) or not all(isinstance(row.get(k), str) and row[k] for k in REQUIRED_FIELDS):
        raise ValueError(f'Invalid GNR inspection row: {json.dumps(row)[:300]}')
    date.fromisoformat(row['inspectionDate'][:10])


def facility(row):
    street = ' '.join(part.strip() for part in (row.get('addressLine1'), row.get('addressLine2')) if part and part.strip())
    return dict(source_id=FACILITY_PREFIX + row['permitID'], name=row['establishmentName'].strip(),
                address=f"{street}\r\n{row.get('city')}, {row.get('state')} {row.get('zip')}",
                permit_type=row.get('permitType'), last_inspection_date=row['inspectionDate'][:10],
                last_inspection_score=row.get('score'), raw=row)


def inspection(row):
    time_in = re.search(r'T(\d\d:\d\d:\d\d)', row.get('timein') or '')
    return dict(source_id=row['inspectionID'], facility_id=FACILITY_PREFIX + row['permitID'],
                inspection_date=row['inspectionDate'][:10], inspection_type=row.get('inspectionType'),
                purpose=row.get('purpose'), score=row.get('score'), grade=row.get('scoreDisplay') or None,
                time_in=time_in.group(1) if time_in else None, comments=row.get('comments'), raw=row)


def fetch_details(conn, source, run_id, limit):
    pending = conn.execute('''SELECT source_id FROM gnr_inspections WHERE detail_fetched_at IS NULL OR detail_error IS NOT NULL
        ORDER BY detail_error NULLS FIRST, inspection_date DESC LIMIT %s''', (limit,)).fetchall()
    for (inspection_id,) in pending:
        url = f'{SITE}{JURISDICTION}/inspection/?inspectionID={quote(inspection_id)}'
        try:
            items, error = observations(source.get(url).body), None
        except Exception as exc:
            print(f'GNR inspection page failed for {url}: {exc}', file=sys.stderr, flush=True)
            items, error = None, f'{type(exc).__name__}: {exc}'
        with conn.transaction():
            conn.execute('''UPDATE gnr_inspections SET observations=COALESCE(%s, observations), detail_error=%s,
                detail_fetched_at=now() WHERE source_id=%s''', (None if items is None else Jsonb(items), error, inspection_id))
            conn.execute('UPDATE etl_runs SET report_errors=report_errors+%s WHERE id=%s', (int(error is not None), run_id))
            add_counters(conn, run_id, parsed_reports=int(error is None))


def observations(body):
    nodes = Selector(body).xpath('//h3[normalize-space(.)="Observations & Corrective Actions"]/following-sibling::p[1]')
    if not nodes:
        raise ValueError('Inspection page not recognized')
    content = re.sub(r'^<p[^>]*>|</p>$', '', nodes[0].html_content.strip())
    items = []
    for item in re.split(r'(?:<br\s*/?>\s*){2,}', content):
        lines = html.unescape(re.sub(r'<[^>]+>', '', re.sub(r'<br\s*/?>', '\n', item))).split('\n')
        text = '\n'.join(line.strip() for line in lines if line.strip())
        if text:
            items.append(text)
    return items


def add_counters(conn, run_id, **counts):
    for key, value in counts.items():
        conn.execute("UPDATE etl_runs SET counters=jsonb_set(counters, ARRAY[%s], to_jsonb(COALESCE((counters->>%s)::int, 0) + %s)) WHERE id=%s",
                     (key, key, value, run_id))
