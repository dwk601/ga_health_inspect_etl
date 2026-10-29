from . import db, transform


def ingest_page(conn, run_id, page, fetched):
    errors = [0]
    count_inspections = count_violations = report_errors = 0
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
                        conn.execute('UPDATE inspections SET report_error=%s WHERE facility_id=%s AND source_id=%s', (str(result), f['source_id'], i['source_id']))
                    else:
                        fields, raw_header, report_conversions = result
                        errors[0] += report_conversions
                        from psycopg.types.json import Jsonb
                        conn.execute('UPDATE inspections SET report_fields=%s, report_raw=%s, report_fetched_at=now(), report_error=NULL WHERE facility_id=%s AND source_id=%s', (Jsonb(fields), Jsonb(raw_header), f['source_id'], i['source_id']))
        conn.execute("""UPDATE etl_runs SET next_page=%s,
            counters=jsonb_build_object('facilities',COALESCE((counters->>'facilities')::int,0)+%s,
              'inspections',COALESCE((counters->>'inspections')::int,0)+%s,
              'violations',COALESCE((counters->>'violations')::int,0)+%s),
            conversion_errors=conversion_errors+%s, report_errors=report_errors+%s WHERE id=%s""",
            (page + 1, len(fetched), count_inspections, count_violations, errors[0], report_errors, run_id))


def fetch_page(conn, source, facilities, reports, remaining):
    fetched = []
    for f in facilities:
        errors = [0]
        facility = transform.facility(f, errors)
        entries = []
        for raw in source.inspections(facility['source_id']):
            i = transform.inspection(raw, facility['source_id'], errors)
            entry = {'inspection': raw}
            stored = conn.execute('SELECT report_fields IS NOT NULL FROM inspections WHERE facility_id=%s AND source_id=%s', (i['facility_id'], i['source_id'])).fetchone()
            if reports and (remaining is None or remaining > 0) and not (stored and stored[0]):
                if remaining is not None:
                    remaining -= 1
                try:
                    page = source.get(i['printable_url'])
                    # Parse before ingest to make failures non-fatal to the page.
                    report_conversions = [0]
                    fields, raw_header = transform.report(page, i['report_template'], report_conversions)
                    entry['report'] = (fields, raw_header, report_conversions[0])
                except Exception as exc:
                    entry['report'] = exc
            entries.append(entry)
        fetched.append({'facility': f, 'inspections': entries})
    return fetched, remaining
