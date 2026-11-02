import sys

from psycopg.types.json import Jsonb

from . import db, transform


MIN_EXHAUSTED_PAGE_RATIO = 0.9
CONVERSION_SAMPLE_LIMIT = 20


def run(conn, source, run_id, options):
    if options['retry_report_errors']:
        return retry_report_errors(conn, source, run_id, options['max_reports'])
    cutoff = db.cutoff(conn, run_id, options)
    page = options['start_page']
    remaining = options['max_reports']
    previous = conn.execute("""SELECT next_page FROM etl_runs
        WHERE status='exhausted' AND options->>'start_page'='0'
        ORDER BY started DESC, id DESC LIMIT 1""").fetchone()
    previous_end = previous[0] if previous else None
    invalid_ids = []
    status = 'bounded'
    while options['max_pages'] is None or page - options['start_page'] < options['max_pages']:
        facilities = source.search(page)
        if not facilities:
            if previous_end and page < previous_end * MIN_EXHAUSTED_PAGE_RATIO:
                raise RuntimeError(f'Unexpected empty page {page}; previous exhausted run reached {previous_end}')
            status = 'exhausted'
            break
        fetched, remaining, invalid = fetch_page(conn, source, facilities, options['reports'], remaining, options['full'])
        invalid_ids.extend(invalid)
        ingest_page(conn, run_id, page, fetched, len(invalid))
        page += 1
        if cutoff and not invalid:
            dates = [item['facility']['last_inspection_date'] for item in fetched]
            if all(date is not None and date < cutoff.isoformat() for date in dates):
                status = 'caught_up'
                break
    if invalid_ids:
        raise ValueError(f'{len(invalid_ids)} invalid facilities: {", ".join(invalid_ids[:5])}')
    return status


class ReportTransportError(Exception):
    pass


def fetch_report(source, url, template):
    try:
        page = source.report(url)
    except Exception as exc:
        print(f'Report fetch failed for {url}: {exc}', file=sys.stderr, flush=True)
        return ReportTransportError(str(exc))
    try:
        conversions = []
        fields, raw = transform.report(page, template, conversions)
        return fields, raw, conversions
    except Exception as exc:
        return exc


def save_report_result(conn, facility_id, inspection_id, result):
    if isinstance(result, ReportTransportError):
        conn.execute('UPDATE inspections SET report_fetched_at=now() WHERE facility_id=%s AND source_id=%s',
                     (facility_id, inspection_id))
        return 0, 1, []
    if isinstance(result, Exception):
        db.save_report_error(conn, facility_id, inspection_id, result)
        return 0, 1, []
    fields, raw, conversions = result
    db.save_report(conn, facility_id, inspection_id, fields, raw)
    return 1, 0, conversions


def update_report_counters(conn, run_id, parsed, failures, conversion_count):
    conn.execute('''UPDATE etl_runs SET report_errors=report_errors+%s,
        conversion_errors=conversion_errors+%s,
        counters=jsonb_set(counters, '{parsed_reports}',
        to_jsonb(COALESCE((counters->>'parsed_reports')::int,0)+%s), true) WHERE id=%s''',
                 (failures, conversion_count, parsed, run_id))


def record_conversions(conn, run_id, errors):
    if not errors:
        return
    samples = conn.execute("SELECT COALESCE(options->'conversion_samples', '[]'::jsonb) FROM etl_runs WHERE id=%s", (run_id,)).fetchone()[0]
    new_samples = errors[:max(0, CONVERSION_SAMPLE_LIMIT - len(samples))]
    if not new_samples:
        return
    for error in new_samples:
        samples.append(error)
        print(f'Conversion error: {error}', file=sys.stderr, flush=True)
    conn.execute("UPDATE etl_runs SET options=jsonb_set(options, '{conversion_samples}', %s) WHERE id=%s", (Jsonb(samples), run_id))


def contextualize(errors, facility_id, entity, inspection_id=None, violation_key=None):
    context = {'facility_id': facility_id, 'entity': entity}
    if inspection_id is not None:
        context['inspection_id'] = inspection_id
    if violation_key is not None:
        context['violation_key'] = violation_key
    return [dict(context, field=field, value=value, reason=reason) for field, value, reason in errors]


def retry_report_errors(conn, source, run_id, limit):
    rows = conn.execute('''SELECT facility_id, source_id, printable_url, report_template
        FROM inspections WHERE report_error IS NOT NULL
        ORDER BY report_fetched_at ASC NULLS FIRST, facility_id, source_id LIMIT %s''', (limit,)).fetchall()
    for facility_id, inspection_id, url, template in rows:
        result = fetch_report(source, url, template)
        with conn.transaction():
            parsed, failures, conversions = save_report_result(conn, facility_id, inspection_id, result)
            record_conversions(conn, run_id, contextualize(conversions, facility_id, 'report', inspection_id))
            update_report_counters(conn, run_id, parsed, failures, len(conversions))
    return 'bounded'


def ingest_page(conn, run_id, page, fetched, invalid_count):
    errors = []
    count_inspections = count_violations = count_reports = report_errors = 0
    with conn.transaction():
        for item in fetched:
            f = item['facility']
            errors.extend(item['conversion_errors'])
            db.upsert(conn, 'facilities', f, ('source_id',))
            for entry in item['inspections']:
                i = entry['inspection']
                errors.extend(entry['conversion_errors'])
                db.upsert(conn, 'inspections', i, ('facility_id', 'source_id'))
                count_inspections += 1
                for v in entry['violations']:
                    v.update(facility_id=f['source_id'], inspection_id=i['source_id'], snapshot_hash=i['violations_hash'])
                    db.upsert(conn, 'violations', v, ('facility_id', 'inspection_id', 'snapshot_hash', 'source_key'))
                    count_violations += 1
                if 'report' in entry:
                    parsed, failures, conversions = save_report_result(conn, f['source_id'], i['source_id'], entry['report'])
                    count_reports += parsed
                    report_errors += failures
                    errors.extend(contextualize(conversions, f['source_id'], 'report', i['source_id']))
        conn.execute("""UPDATE etl_runs SET next_page=%s,
            counters=jsonb_build_object('facilities',COALESCE((counters->>'facilities')::int,0)+%s,
              'inspections',COALESCE((counters->>'inspections')::int,0)+%s,
              'violations',COALESCE((counters->>'violations')::int,0)+%s,
              'parsed_reports',COALESCE((counters->>'parsed_reports')::int,0),
              'skipped_facilities',COALESCE((counters->>'skipped_facilities')::int,0)+%s,
              'invalid_facilities',COALESCE((counters->>'invalid_facilities')::int,0)+%s) WHERE id=%s""",
            (page + 1, len(fetched), count_inspections, count_violations,
             sum(item.get('skipped', False) for item in fetched), invalid_count, run_id))
        record_conversions(conn, run_id, errors)
        update_report_counters(conn, run_id, count_reports, report_errors, len(errors))


def fetch_page(conn, source, facilities, reports, remaining, full):
    fetched = []
    invalid = []
    for f in facilities:
        conversions = []
        try:
            facility = transform.facility(f, conversions)
        except ValueError as exc:
            facility_id = str(f.get('id', '<missing>')) if isinstance(f, dict) else '<missing>'
            print(f'Invalid facility {facility_id}: {exc}', file=sys.stderr, flush=True)
            invalid.append(facility_id)
            continue
        errors = contextualize(conversions, facility['source_id'], 'facility')
        entries = []
        stored = conn.execute('SELECT raw FROM facilities WHERE source_id = %s',
                              (facility['source_id'],)).fetchone()
        unchanged = not full and stored is not None and stored[0] == f
        needs_reports = (unchanged and reports and (remaining is None or remaining > 0)
                         and conn.execute('''SELECT EXISTS (SELECT 1 FROM inspections
                             WHERE facility_id = %s AND report_fields IS NULL AND report_error IS NULL)''',
                                          (facility['source_id'],)).fetchone()[0])
        if unchanged and not needs_reports:
            fetched.append({'facility': facility, 'inspections': entries, 'skipped': True, 'conversion_errors': errors})
            continue
        try:
            inspections = source.inspections(facility['source_id'])
            for raw in inspections:
                conversions = []
                inspection = transform.inspection(raw, facility['source_id'], conversions)
                inspection_errors = contextualize(conversions, facility['source_id'], 'inspection', inspection['source_id'])
                violations = []
                for key, value in raw['violations'].items():
                    conversions = []
                    violations.append(transform.violation(key, value, conversions))
                    inspection_errors.extend(contextualize(conversions, facility['source_id'], 'violation', inspection['source_id'], key))
                entries.append({'inspection': inspection, 'violations': violations, 'conversion_errors': inspection_errors})
        except ValueError as exc:
            print(f"Invalid facility {facility['source_id']}: {exc}", file=sys.stderr, flush=True)
            invalid.append(facility['source_id'])
            continue
        for entry in entries:
            i = entry['inspection']
            if reports and (remaining is None or remaining > 0):
                stored_report = conn.execute('SELECT report_fields IS NOT NULL OR report_error IS NOT NULL FROM inspections WHERE facility_id=%s AND source_id=%s', (i['facility_id'], i['source_id'])).fetchone()
                if not (stored_report and stored_report[0]):
                    if remaining is not None:
                        remaining -= 1
                    entry['report'] = fetch_report(source, i['printable_url'], i['report_template'])
        fetched.append({'facility': facility, 'inspections': entries, 'conversion_errors': errors})
    return fetched, remaining, invalid
