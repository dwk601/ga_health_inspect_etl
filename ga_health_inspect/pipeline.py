import sys

from . import db, transform


def run(conn, source, run_id, options):
    if options.get('retry_report_errors'):
        return retry_report_errors(conn, source, run_id, options['max_reports'])
    cutoff = db.cutoff(conn, run_id, options)
    page = options['start_page']
    remaining = options['max_reports']
    while options['max_pages'] is None or page - options['start_page'] < options['max_pages']:
        facilities = source.search(page)
        if not facilities:
            return 'exhausted'
        fetched, remaining = fetch_page(conn, source, facilities, options['reports'], remaining, options['full'])
        ingest_page(conn, run_id, page, fetched)
        page += 1
        if cutoff:
            dates = [transform.facility(f, [0])['last_inspection_date'] for f in facilities]
            if all(date is not None and date < cutoff.isoformat() for date in dates):
                return 'caught_up'
    return 'bounded'


def fetch_report(source, url, template):
    try:
        page = source.report(url)
    except Exception as exc:
        print(f'Report fetch failed for {url}: {exc}', file=sys.stderr, flush=True)
        return None
    try:
        conversions = [0]
        fields, raw = transform.report(page, template, conversions)
        return fields, raw, conversions[0]
    except Exception as exc:
        return exc


def retry_report_errors(conn, source, run_id, limit):
    rows = conn.execute('''SELECT facility_id, source_id, printable_url, report_template
        FROM inspections WHERE report_error IS NOT NULL
        ORDER BY report_fetched_at ASC NULLS FIRST, facility_id, source_id LIMIT %s''', (limit,)).fetchall()
    for facility_id, inspection_id, url, template in rows:
        result = fetch_report(source, url, template)
        with conn.transaction():
            if isinstance(result, Exception):
                db.save_report_error(conn, facility_id, inspection_id, result)
                conn.execute('UPDATE etl_runs SET report_errors=report_errors+1 WHERE id=%s', (run_id,))
            elif result is not None:
                fields, raw, conversions = result
                db.save_report(conn, facility_id, inspection_id, fields, raw)
                conn.execute('''UPDATE etl_runs SET conversion_errors=conversion_errors+%s,
                    counters=jsonb_set(counters, '{parsed_reports}',
                    to_jsonb(COALESCE((counters->>'parsed_reports')::int,0)+1), true) WHERE id=%s''',
                             (conversions, run_id))
    return 'bounded'


def ingest_page(conn, run_id, page, fetched):
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
                    result = entry['report']
                    if isinstance(result, Exception):
                        report_errors += 1
                        db.save_report_error(conn, f['source_id'], i['source_id'], result)
                    else:
                        fields, raw_header, report_conversions = result
                        errors[0] += report_conversions
                        db.save_report(conn, f['source_id'], i['source_id'], fields, raw_header)
                        count_reports += 1
        conn.execute("""UPDATE etl_runs SET next_page=%s,
            counters=jsonb_build_object('facilities',COALESCE((counters->>'facilities')::int,0)+%s,
              'inspections',COALESCE((counters->>'inspections')::int,0)+%s,
              'violations',COALESCE((counters->>'violations')::int,0)+%s,
              'parsed_reports',COALESCE((counters->>'parsed_reports')::int,0)+%s,
              'skipped_facilities',COALESCE((counters->>'skipped_facilities')::int,0)+%s),
            conversion_errors=conversion_errors+%s, report_errors=report_errors+%s WHERE id=%s""",
            (page + 1, len(fetched), count_inspections, count_violations, count_reports,
             sum(item.get('skipped', False) for item in fetched), errors[0], report_errors, run_id))


def fetch_page(conn, source, facilities, reports, remaining, full):
    fetched = []
    for f in facilities:
        facility = transform.facility(f, [0])
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
        for raw in source.inspections(facility['source_id']):
            entry = {'inspection': raw}
            i = transform.inspection(raw, facility['source_id'], [0])
            if reports and (remaining is None or remaining > 0):
                stored_report = conn.execute('SELECT report_fields IS NOT NULL OR report_error IS NOT NULL FROM inspections WHERE facility_id=%s AND source_id=%s', (i['facility_id'], i['source_id'])).fetchone()
                if not (stored_report and stored_report[0]):
                    if remaining is not None:
                        remaining -= 1
                    result = fetch_report(source, i['printable_url'], i['report_template'])
                    if result is not None:
                        entry['report'] = result
            entries.append(entry)
        fetched.append({'facility': f, 'inspections': entries})
    return fetched, remaining
