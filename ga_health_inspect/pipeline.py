import sys

from . import db, transform


MIN_EXHAUSTED_PAGE_RATIO = 0.9


def run(conn, source, run_id, options):
    if options['retry_report_errors']:
        return retry_report_errors(conn, source, run_id, options['max_reports'])
    cutoff = db.cutoff(conn, run_id, options)
    page = options['start_page']
    remaining = options['max_reports']
    furthest = conn.execute("SELECT max(next_page) FROM etl_runs WHERE status='exhausted' AND options->>'start_page'='0'").fetchone()[0]
    invalid = []
    status = 'bounded'
    while options['max_pages'] is None or page - options['start_page'] < options['max_pages']:
        facilities = source.search(page)
        if not facilities:
            if furthest and page < furthest * MIN_EXHAUSTED_PAGE_RATIO:
                raise RuntimeError(f'Unexpected empty page {page}; previous exhausted run reached {furthest}')
            status = 'exhausted'
            break
        fetched, remaining, skipped = fetch_page(conn, source, facilities, options['reports'], remaining, options['full'])
        invalid.extend(skipped)
        ingest_page(conn, run_id, page, fetched, len(skipped))
        page += 1
        if cutoff and not skipped:
            dates = [transform.facility(f, [0])['last_inspection_date'] for f in facilities]
            if all(date is not None and date < cutoff.isoformat() for date in dates):
                status = 'caught_up'
                break
    if invalid:
        raise ValueError(f'{len(invalid)} invalid facilities: {", ".join(invalid[:5])}')
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
        conversions = [0]
        fields, raw = transform.report(page, template, conversions)
        return fields, raw, conversions[0]
    except Exception as exc:
        return exc


def save_report_result(conn, facility_id, inspection_id, result):
    if isinstance(result, ReportTransportError):
        conn.execute('UPDATE inspections SET report_fetched_at=now() WHERE facility_id=%s AND source_id=%s',
                     (facility_id, inspection_id))
        return 0, 1, 0
    if isinstance(result, Exception):
        db.save_report_error(conn, facility_id, inspection_id, result)
        return 0, 1, 0
    fields, raw, conversions = result
    db.save_report(conn, facility_id, inspection_id, fields, raw)
    return 1, 0, conversions


def update_report_counters(conn, run_id, parsed, failures, conversions):
    conn.execute('''UPDATE etl_runs SET report_errors=report_errors+%s,
        conversion_errors=conversion_errors+%s,
        counters=jsonb_set(counters, '{parsed_reports}',
        to_jsonb(COALESCE((counters->>'parsed_reports')::int,0)+%s), true) WHERE id=%s''',
                 (failures, conversions, parsed, run_id))


def retry_report_errors(conn, source, run_id, limit):
    rows = conn.execute('''SELECT facility_id, source_id, printable_url, report_template
        FROM inspections WHERE report_error IS NOT NULL
        ORDER BY report_fetched_at ASC NULLS FIRST, facility_id, source_id LIMIT %s''', (limit,)).fetchall()
    for facility_id, inspection_id, url, template in rows:
        result = fetch_report(source, url, template)
        with conn.transaction():
            counts = save_report_result(conn, facility_id, inspection_id, result)
            update_report_counters(conn, run_id, *counts)
    return 'bounded'


def ingest_page(conn, run_id, page, fetched, invalid_count=0):
    errors = [0]
    count_inspections = count_violations = count_reports = report_errors = 0
    with conn.transaction():
        for item in fetched:
            f = transform.facility(item['facility'], errors)
            db.upsert(conn, 'facilities', f, ('source_id',))
            for entry in item['inspections']:
                raw = entry['inspection']
                i = transform.inspection(raw, f['source_id'], errors)
                db.upsert(conn, 'inspections', i, ('facility_id', 'source_id'))
                count_inspections += 1
                for key, value in raw['violations'].items():
                    v = transform.violation(key, value, errors)
                    v.update(facility_id=f['source_id'], inspection_id=i['source_id'], snapshot_hash=i['violations_hash'])
                    db.upsert(conn, 'violations', v, ('facility_id', 'inspection_id', 'snapshot_hash', 'source_key'))
                    count_violations += 1
                if 'report' in entry:
                    parsed, failures, conversions = save_report_result(conn, f['source_id'], i['source_id'], entry['report'])
                    count_reports += parsed
                    report_errors += failures
                    errors[0] += conversions
        conn.execute("""UPDATE etl_runs SET next_page=%s,
            counters=jsonb_build_object('facilities',COALESCE((counters->>'facilities')::int,0)+%s,
              'inspections',COALESCE((counters->>'inspections')::int,0)+%s,
              'violations',COALESCE((counters->>'violations')::int,0)+%s,
              'parsed_reports',COALESCE((counters->>'parsed_reports')::int,0),
              'skipped_facilities',COALESCE((counters->>'skipped_facilities')::int,0)+%s,
              'invalid_facilities',COALESCE((counters->>'invalid_facilities')::int,0)+%s) WHERE id=%s""",
            (page + 1, len(fetched), count_inspections, count_violations,
             sum(item.get('skipped', False) for item in fetched), invalid_count, run_id))
        update_report_counters(conn, run_id, count_reports, report_errors, errors[0])


def fetch_page(conn, source, facilities, reports, remaining, full):
    fetched = []
    invalid = []
    for f in facilities:
        try:
            facility = transform.facility(f, [0])
        except ValueError as exc:
            facility_id = str(f.get('id', '<missing>')) if isinstance(f, dict) else '<missing>'
            print(f'Invalid facility {facility_id}: {exc}', file=sys.stderr, flush=True)
            invalid.append(facility_id)
            continue
        entries = []
        stored = conn.execute('SELECT raw FROM facilities WHERE source_id = %s',
                              (facility['source_id'],)).fetchone()
        unchanged = not full and stored is not None and stored[0] == f
        needs_reports = (unchanged and reports and (remaining is None or remaining > 0)
                         and conn.execute('''SELECT EXISTS (SELECT 1 FROM inspections
                             WHERE facility_id = %s AND report_fields IS NULL AND report_error IS NULL)''',
                                          (facility['source_id'],)).fetchone()[0])
        if unchanged and not needs_reports:
            fetched.append({'facility': f, 'inspections': entries, 'skipped': True})
            continue
        try:
            inspections = source.inspections(facility['source_id'])
            for raw in inspections:
                transform.inspection(raw, facility['source_id'], [0])
                for key, value in raw['violations'].items():
                    transform.violation(key, value, [0])
        except ValueError as exc:
            print(f"Invalid facility {facility['source_id']}: {exc}", file=sys.stderr, flush=True)
            invalid.append(facility['source_id'])
            continue
        for raw in inspections:
            entry = {'inspection': raw}
            i = transform.inspection(raw, facility['source_id'], [0])
            if reports and (remaining is None or remaining > 0):
                stored_report = conn.execute('SELECT report_fields IS NOT NULL OR report_error IS NOT NULL FROM inspections WHERE facility_id=%s AND source_id=%s', (i['facility_id'], i['source_id'])).fetchone()
                if not (stored_report and stored_report[0]):
                    if remaining is not None:
                        remaining -= 1
                    entry['report'] = fetch_report(source, i['printable_url'], i['report_template'])
            entries.append(entry)
        fetched.append({'facility': f, 'inspections': entries})
    return fetched, remaining, invalid
