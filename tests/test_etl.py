import copy
import json
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest import mock
from zoneinfo import ZoneInfo

import psycopg
from scrapling.parser import Selector

from ga_health_inspect import db, gnr, pipeline, transform
from ga_health_inspect import __main__ as cli
from ga_health_inspect.pipeline import fetch_page, ingest_page
from ga_health_inspect.source import HTTPError, Source

FACILITY = {'id': 'YWJj', 'name': 'Example', 'mapAddress': '1 Main St', 'columns': {
    '0': '1 Main St', '1': 'Phone Number: 123', '2': 'Permit Type: Food Service',
    '3': 'Permit Number: F-1', '4': 'Last Inspection Score: 90', '5': 'Last Inspection Date: 01-02-2024'}}
INSPECTION = {'inspectionId': 42, 'facilityId': 'YWJj',
    'printablePath': '../../_templates/87/food_2015/_report_full.cfm?fsimID=42&rtype=food_2015',
    'columns': {'0': 'Date: 1-2-2024', '1': 'Inspection Purpose: Routine', '2': 'Score: 90', '3': 'Inspector: Person'},
    'violations': {'0': ['1. Item', 'Rule', 'Points: 2', 'Corrected during inspection?: Yes', 'Repeat: No', 'Inspector Notes: Fix']}}


class SourceTest(unittest.TestCase):
    def test_search_only_ends_on_persistently_empty_page(self):
        for bodies, expected in [([b'[]', b'[{"id": "A"}]'], [{'id': 'A'}]), ([b'[]'] * 4, [])]:
            responses = iter(bodies)
            source = Source()
            source.session = SimpleNamespace(get=lambda url, **kwargs: SimpleNamespace(status=200, body=next(responses)))
            with mock.patch('ga_health_inspect.source.time.sleep'):
                self.assertEqual(source.search(1525), expected)

    def test_report_headers(self):
        food = b'''<table><tr><td><b>Establishment Name:</b> Example</td></tr>
            <tr><td><b>Inspection Date:</b> 01/30/2026</td></tr></table>
            <div><script>document.location.href="login.cfm?attempt=</div>
            <div id="div_finalScore">100</div><div id="div_grade">A</div>'''
        pool = b'''<table><tr><td><b>Facility Name:</b> Example</td></tr>
            <tr><td><b>Inspection Date:</b> 01/30/2026</td></tr>%s</table>'''
        legacy = b'''<table><tr><td></td><td>&nbsp;Name of Facility</td><td>&nbsp;Address</td><td>City</td></tr>
            <tr><td></td><td>Example</td><td>1 Main</td><td>Atlanta</td></tr>
            <tr><td>&nbsp;Permit No.</td></tr><tr><td>P-1</td></tr>
            <tr><td></td><td>&nbsp;Date of Inspection</td></tr><tr><td></td><td>01/30/2026</td></tr></table>'''
        cases = [('food_2015', food, {'score': '100', 'grade': 'A'}),
                 ('food_2015', food.replace(b'>100</div>', b'> </div>'), {'score': None, 'grade': 'A'}),
                 ('pool_2017', pool % b'<tr><td class="ArialEighteen" rowspan="2"><strong></strong></td></tr>', {'score': None, 'grade': None}),
                 ('pool_2017', pool % b'<tr><td class="ArialEighteen" rowspan="2"><strong><html><hea</strong></td></tr>', {'score': None, 'grade': None}),
                 ('Pool', legacy, {'score': None, 'grade': None, 'address': '1 Main', 'city': 'Atlanta', 'permit_number': 'P-1'}),
                 ('pool_2017', pool % b'', None)]
        cases += [(template, b'<html>garbage</html>', None) for template in ('food_2015', 'pool_2017', 'Pool', 'Tourist2014')]
        for template, body, expected in cases:
            with self.subTest(template=template, body=body):
                source = Source()
                source.session = SimpleNamespace(get=lambda url, **kwargs: SimpleNamespace(status=200, body=body, encoding='utf-8'))
                page = source.report('https://example.test/r')
                if expected is None:
                    with self.assertRaises(ValueError):
                        transform.report(page, template, [])
                else:
                    fields, raw = transform.report(page, template, [])
                    self.assertEqual((fields['name'], fields['inspection_date']), ('Example', '2026-01-30'))
                    self.assertEqual({key: fields[key] for key in expected}, expected)
        source.session = SimpleNamespace(get=lambda url, **kwargs: SimpleNamespace(status=200, body=b'<title>Page Temporarily Unavailable</title>', encoding='utf-8'))
        with self.assertRaises(RuntimeError):
            source.report('https://example.test/r')

    def test_retry_cli_rejects_search_options(self):
        for option in (['--reports'], ['--full'], ['--start-page', '0'], ['--max-pages', '1'], ['--gnr']):
            with self.subTest(option=option), mock.patch('sys.argv', ['etl', '--retry-report-errors', *option]), mock.patch('sys.stderr'):
                with self.assertRaises(SystemExit) as error:
                    cli.main()
                self.assertEqual(error.exception.code, 2)

    def test_inspections_normalize_source_quirks(self):
        placeholder = {'printablePath': '', 'inspectionId': 0, 'facilityId': 'YWJj',
                       'columns': {'0': 'No Inspections To Display'}, 'violations': {}}
        other_facility = dict(INSPECTION, facilityId='other')
        for bodies in [[[placeholder, INSPECTION]], [[other_facility], [INSPECTION]]]:
            responses = iter(bodies)
            source = Source()
            source.session = SimpleNamespace(get=lambda url, **kwargs: SimpleNamespace(
                status=200, body=json.dumps(next(responses)).encode()))
            with mock.patch('ga_health_inspect.source.time.sleep'), mock.patch('sys.stderr'):
                self.assertEqual(source.inspections('YWJj'), [INSPECTION])

        source = Source()
        responses = iter([SimpleNamespace(status=429, body=b''),
                          SimpleNamespace(status=200, body=b'{'),
                          SimpleNamespace(status=200, body=b'{}'),
                          SimpleNamespace(status=200, body=json.dumps([INSPECTION]).encode())])
        source.session = SimpleNamespace(get=lambda url, **kwargs: next(responses))
        with mock.patch('ga_health_inspect.source.time.sleep'):
            self.assertEqual(source.inspections('YWJj'), [INSPECTION])
        responses = iter([other_facility] * 3)
        source.session = SimpleNamespace(get=lambda url, **kwargs: SimpleNamespace(
            status=200, body=json.dumps([next(responses)]).encode()))
        with mock.patch('ga_health_inspect.source.time.sleep'), mock.patch('sys.stderr'):
            with self.assertRaises(ValueError):
                source.inspections('YWJj')


class LifecycleTest(unittest.TestCase):
    @mock.patch('sys.stderr')
    def test_incremental_refresh(self, stderr):
        class Rollback(Exception):
            pass

        today = datetime.now(ZoneInfo('America/New_York')).date()
        facilities = []
        for name, days in [('A', 0), ('B', 7), ('C', 20), ('D', 30)]:
            f = copy.deepcopy(FACILITY)
            f.update(id=name, name=name)
            f['columns']['5'] = 'Last Inspection Date: ' + (today - timedelta(days=days)).strftime('%m-%d-%Y')
            facilities.append(f)

        class Source:
            def __init__(self):
                self.searches = []
                self.requests = []
                self.reports = []
                self.fail_report = False

            def search(self, page):
                self.searches.append(page)
                return [facilities[page]] if page < len(facilities) else []

            def inspections(self, facility_id):
                self.requests.append(facility_id)
                inspection = copy.deepcopy(INSPECTION)
                inspection['facilityId'] = facility_id
                return [inspection]

            def report(self, url):
                self.reports.append(url)
                if self.fail_report:
                    raise RuntimeError('network unavailable')
                return Selector('''<table><tr><td><b>Establishment Name:</b> Example</td></tr>
                    <tr><td><b>Inspection Date:</b> 01/02/2024</td></tr></table>
                    <div id="div_finalScore">90</div>''')

        options = dict(start_page=0, max_pages=1, full=False, overlap_days=14, reports=False, max_reports=None, retry_report_errors=False)
        with psycopg.connect(autocommit=True) as conn:
            try:
                with conn.transaction():
                    conn.execute('CREATE SCHEMA etl_incremental_test')
                    conn.execute('SET LOCAL search_path TO etl_incremental_test')
                    db.init(conn)
                    def execute(source):
                        run_id = db.start(conn, options)
                        status = pipeline.run(conn, source, run_id, options)
                        db.finish(conn, run_id, status)
                        return run_id, status

                    self.assertEqual(execute(Source())[1], 'bounded')
                    options['max_pages'] = None
                    source = Source()
                    self.assertEqual(execute(source)[1], 'exhausted')
                    self.assertEqual(source.searches, [0, 1, 2, 3, 4])
                    facilities[0]['name'] = 'A updated'
                    source = Source()
                    run_id, status = execute(source)
                    self.assertEqual(status, 'caught_up')
                    self.assertEqual(source.searches, [0, 1, 2])
                    self.assertEqual(source.requests, ['A'])
                    self.assertEqual(conn.execute("SELECT next_page, (counters->>'skipped_facilities')::int, options->>'cutoff' FROM etl_runs WHERE id=%s", (run_id,)).fetchone(),
                                     (3, 2, (today - timedelta(days=14)).isoformat()))
                    self.assertEqual(conn.execute("SELECT source_id, name FROM facilities WHERE source_id IN ('A', 'D') ORDER BY source_id").fetchall(), [('A', 'A updated'), ('D', 'D')])
                    conn.execute("UPDATE inspections SET report_error='bad report', report_fetched_at=now() WHERE facility_id='A'")
                    options.update(reports=True, max_pages=1, max_reports=1)
                    source = Source()
                    self.assertEqual(execute(source)[1], 'bounded')
                    self.assertEqual((source.requests, source.reports), ([], []))
                    facilities[0]['name'] = 'A changed again'
                    source = Source()
                    execute(source)
                    self.assertEqual((source.requests, source.reports), (['A'], []))
                    conn.execute("UPDATE inspections SET report_fetched_at=now()-interval '2 days' WHERE facility_id='A'")
                    conn.execute("UPDATE inspections SET report_error='newer error', report_fetched_at=now()-interval '1 day', printable_url=printable_url || '&facility=B' WHERE facility_id='B'")
                    options['retry_report_errors'] = True
                    source = Source()
                    retry_id, status = execute(source)
                    self.assertEqual((status, source.searches, source.requests, len(source.reports)), ('bounded', [], [], 1))
                    self.assertFalse(source.reports[0].endswith('&facility=B'))
                    self.assertEqual(conn.execute("SELECT report_error, report_fields->>'score' FROM inspections WHERE facility_id='A'").fetchone(), (None, '90'))
                    self.assertEqual(conn.execute("SELECT report_error FROM inspections WHERE facility_id='B'").fetchone()[0], 'newer error')
                    source = Source()
                    source.fail_report = True
                    with mock.patch('sys.stderr'):
                        retry_id, _ = execute(source)
                    self.assertEqual(conn.execute("SELECT report_error, report_fetched_at=now() FROM inspections WHERE facility_id='B'").fetchone(), ('newer error', True))
                    self.assertEqual(conn.execute('SELECT report_errors FROM etl_runs WHERE id=%s', (retry_id,)).fetchone()[0], 1)
                    conn.execute("UPDATE inspections SET report_error=NULL WHERE facility_id='B'")
                    options.update(retry_report_errors=False, start_page=1)
                    source = Source()
                    source.fail_report = True
                    with mock.patch('sys.stderr'):
                        failed_id, status = execute(source)
                    self.assertEqual(status, 'bounded')
                    self.assertEqual(conn.execute('SELECT report_errors FROM etl_runs WHERE id=%s', (failed_id,)).fetchone()[0], 1)
                    self.assertEqual(len(source.reports), 1)
                    self.assertEqual(conn.execute("SELECT report_error, report_fields FROM inspections WHERE facility_id='B'").fetchone(), (None, None))
                    source = Source()
                    source.fail_report = True
                    @contextmanager
                    def scoped_connect(**kwargs):
                        try:
                            yield conn
                        finally:
                            conn.execute('SELECT pg_advisory_unlock(%s)', (db.RUN_LOCK_KEY,))
                    source_context = mock.MagicMock()
                    source_context.__enter__.return_value = source
                    with mock.patch.object(cli.psycopg, 'connect', side_effect=scoped_connect), \
                         mock.patch.object(cli, 'Source', return_value=source_context), \
                         mock.patch('sys.argv', ['etl', '--retry-report-errors']), mock.patch('builtins.print') as output:
                        self.assertEqual(cli.main(), 0)
                    self.assertEqual(len(source.reports), 1)
                    self.assertIn('report_error_backlog=1', output.call_args.args[0])
                    source = Source()
                    execute(source)
                    self.assertEqual(len(source.reports), 1)
                    self.assertEqual(conn.execute("SELECT report_fields->>'score' FROM inspections WHERE facility_id='B'").fetchone()[0], '90')
                    options.update(start_page=0, max_pages=None, reports=False, full=True)
                    empty = Source()
                    empty.search = lambda page: []
                    run_id = db.start(conn, options)
                    with self.assertRaisesRegex(RuntimeError, 'Unexpected empty page'):
                        pipeline.run(conn, empty, run_id, options)
                    newer = db.start(conn, options)
                    conn.execute("UPDATE etl_runs SET status='exhausted', next_page=1 WHERE id=%s", (newer,))
                    run_id = db.start(conn, options)
                    empty.search = lambda page: [facilities[0]] if page == 0 else []
                    self.assertEqual(pipeline.run(conn, empty, run_id, options), 'exhausted')
                    raise Rollback()
            except Rollback:
                pass

    def test_run_reconciliation_and_invalid_facility(self):
        class Rollback(Exception):
            pass

        class TestSource:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def search(self, page):
                return [[dict(FACILITY, id='bad'), dict(FACILITY, id='good')],
                        [dict(FACILITY, id='later')], []][page]

            def inspections(self, facility_id):
                raw = copy.deepcopy(INSPECTION)
                raw['facilityId'] = facility_id
                if facility_id == 'bad':
                    raw['violations']['0'][0] = 123
                return [raw]

        with psycopg.connect(autocommit=True) as conn, psycopg.connect(autocommit=True) as lock_conn:
            try:
                with conn.transaction():
                    conn.execute('CREATE SCHEMA etl_run_test')
                    conn.execute('SET LOCAL search_path TO etl_run_test')
                    db.init(conn)

                    @contextmanager
                    def scoped_connect(**kwargs):
                        try:
                            yield conn
                        finally:
                            conn.execute('SELECT pg_advisory_unlock(%s)', (db.RUN_LOCK_KEY,))

                    with mock.patch.object(cli.psycopg, 'connect', side_effect=scoped_connect), \
                         mock.patch.object(cli, 'Source', TestSource), mock.patch('sys.argv', ['etl']), \
                         mock.patch('sys.stderr'):
                        self.assertEqual(cli.main(), 1)
                    status, error, count = conn.execute("SELECT status, error, (counters->>'invalid_facilities')::int FROM etl_runs ORDER BY id DESC LIMIT 1").fetchone()
                    self.assertEqual((status, count), ('failed', 1))
                    self.assertIn('bad', error)
                    self.assertEqual(conn.execute('SELECT source_id FROM facilities ORDER BY source_id').fetchall(), [('good',), ('later',)])
                    orphan = db.start(conn, {'start_page': 0})
                    self.assertIsNone(db.cutoff(conn, orphan, dict(start_page=0, full=False, overlap_days=14)))
                    self.assertTrue(lock_conn.execute('SELECT pg_try_advisory_lock(%s)', (db.RUN_LOCK_KEY,)).fetchone()[0])
                    with mock.patch.object(cli.psycopg, 'connect', side_effect=scoped_connect), \
                         mock.patch.object(cli, 'Source', TestSource), mock.patch('sys.argv', ['etl', '--retry-report-errors']), mock.patch('sys.stderr'):
                        self.assertEqual(cli.main(), 1)
                    self.assertEqual(conn.execute('SELECT count(*) FROM etl_runs').fetchone()[0], 2)
                    self.assertEqual(conn.execute('SELECT status FROM etl_runs WHERE id=%s', (orphan,)).fetchone()[0], 'running')
                    lock_conn.execute('SELECT pg_advisory_unlock(%s)', (db.RUN_LOCK_KEY,))
                    with mock.patch.object(cli.psycopg, 'connect', side_effect=scoped_connect), \
                         mock.patch.object(cli, 'Source', TestSource), mock.patch('sys.argv', ['etl', '--retry-report-errors']):
                        self.assertEqual(cli.main(), 0)
                    self.assertEqual(conn.execute('SELECT status, finished IS NOT NULL, error FROM etl_runs WHERE id=%s', (orphan,)).fetchone(),
                                     ('interrupted', True, 'Process ended without finishing'))
                    raise Rollback()
            except Rollback:
                pass

    def test_gnr_incremental_load(self):
        class Rollback(Exception):
            pass

        today = datetime.now(ZoneInfo('America/New_York')).date()
        day = (today - timedelta(days=2)).isoformat()
        rows = [{'permitID': f'P{i}', 'inspectionID': f'I{i}', 'inspectionDate': day + 'T00:00:00.000Z',
                 'establishmentName': f'Place {i} ', 'addressLine1': '1 Main St', 'addressLine2': '', 'city': 'Lilburn',
                 'state': 'GA', 'zip': '30047', 'score': 90, 'scoreDisplay': 'A', 'timein': '1899-12-30T11:05:00.000Z'}
                for i in range(26)]
        blocked = False
        page = b'''<h3 class="observations-header">Observations &amp; Corrective Actions</h3><p class="observations-text">
            4-2B: Mold &amp; dirt<br />Corrective Actions: Cleaned<br /><br />13A: Not posted<br /><br /></p>'''

        class Source:
            def __init__(self):
                self.days = []
                self.pages = []

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def post_json(self, url, body):
                data = body['data']
                self.days.append(data['filters']['date'].split(' to ')[0])
                found = rows if data['filters'] == dict(data['filters'], date=f'{day} to {day}', county='gcg') else []
                return found[data['start']:data['start'] + data['count']]

            def get(self, url):
                self.pages.append(url)
                if blocked:
                    raise HTTPError(403, url)
                return SimpleNamespace(body=page)

        with psycopg.connect(autocommit=True) as conn:
            try:
                with conn.transaction():
                    conn.execute('CREATE SCHEMA etl_gnr_test')
                    conn.execute('SET LOCAL search_path TO etl_gnr_test')
                    db.init(conn)

                    @contextmanager
                    def scoped_connect(**kwargs):
                        try:
                            yield conn
                        finally:
                            conn.execute('SELECT pg_advisory_unlock(%s)', (db.RUN_LOCK_KEY,))

                    def execute(exit_code=0):
                        source = Source()
                        with mock.patch.object(cli.psycopg, 'connect', side_effect=scoped_connect), \
                             mock.patch.object(cli, 'Source', return_value=source), \
                             mock.patch.object(gnr, 'FIRST_DATE', today - timedelta(days=5)), \
                             mock.patch('sys.argv', ['etl', '--gnr', '--reports', '--overlap-days', '1']), mock.patch('builtins.print'), \
                             mock.patch('sys.stderr'):
                            self.assertEqual(cli.main(), exit_code)
                        return source

                    source = execute()
                    self.assertEqual((source.days[0], len(source.pages)), ((today - timedelta(days=5)).isoformat(), 26))
                    self.assertEqual(conn.execute("SELECT name, address FROM facilities WHERE source_id='gnr:P25'").fetchone(),
                                     ('Place 25', '1 Main St\r\nLilburn, GA 30047'))
                    self.assertEqual(conn.execute("SELECT facility_id, inspection_date::text, score, grade, time_in::text, observations FROM gnr_inspections WHERE source_id='I0'").fetchone(),
                                     ('gnr:P0', day, 90, 'A', '11:05:00', ['4-2B: Mold & dirt\nCorrective Actions: Cleaned', '13A: Not posted']))
                    state_run = db.start(conn, {'start_page': 0})
                    self.assertIsNone(db.cutoff(conn, state_run, dict(start_page=0, full=False, overlap_days=14)))
                    rows[0]['score'] = 80
                    source = execute()
                    self.assertEqual((source.days[0], source.pages), ((today - timedelta(days=3)).isoformat(), [
                        'https://inspections.myhealthdepartment.com/gwinnett-ga/inspection/?inspectionID=I0']))
                    rows[0]['score'] = 70
                    blocked = True
                    self.assertEqual(len(execute(exit_code=1).pages), 1)
                    self.assertEqual(conn.execute("SELECT detail_fetched_at, detail_error FROM gnr_inspections WHERE source_id='I0'").fetchone(), (None, None))
                    raise Rollback()
            except Rollback:
                pass

    @mock.patch('sys.stderr')
    def test_ingestion_replay_and_invalid_page(self, stderr):
        class Rollback(Exception):
            pass

        page = Selector('''<table><tr><td><b>Establishment Name:</b> Example</td></tr>
            <tr><td><b>Inspection Date:</b> 01/02/2024</td></tr>
            <tr><td><b>Time In:</b></td><td>bad</td><td>:</td><td>30</td><td>AM</td></tr>
            <tr><td><b>Time Out:</b></td><td>10</td><td>:</td><td>30</td><td>AM</td></tr></table>
            <div id="div_finalScore">90</div>''')
        errors = []
        parsed, raw = transform.report(page, 'food_2015', errors)
        self.assertEqual((raw['time_in'], parsed['time_in'], parsed['time_out'], len(errors)),
                         ('bad:30 AM', None, '10:30:00', 1))

        facility = copy.deepcopy(FACILITY)
        inspection = copy.deepcopy(INSPECTION)
        fields = parsed
        source = SimpleNamespace(inspections=lambda facility_id: [inspection])
        with psycopg.connect(autocommit=True) as conn:
            try:
                with conn.transaction():
                    conn.execute('CREATE SCHEMA etl_lifecycle_test')
                    conn.execute('SET LOCAL search_path TO etl_lifecycle_test')
                    db.init(conn)
                    run = db.start(conn, {'start_page': 0})
                    fetched, _, _ = fetch_page(conn, source, [facility], False, None, True)
                    fetched[0]['inspections'][0]['report'] = (fields, raw, errors)
                    ingest_page(conn, run, 0, fetched, 0)
                    self.assertEqual(conn.execute('SELECT name, permit_number, last_inspection_date::text FROM facilities').fetchone(), ('Example', 'F-1', '2024-01-02'))
                    self.assertEqual(conn.execute('SELECT inspection_date::text, score, report_fields, report_raw FROM inspections').fetchone(), (None, 90, fields, raw))
                    self.assertEqual(conn.execute("SELECT (counters->>'parsed_reports')::int, conversion_errors FROM etl_runs WHERE id=%s", (run,)).fetchone(), (1, 2))
                    samples = conn.execute("SELECT options->'conversion_samples' FROM etl_runs WHERE id=%s", (run,)).fetchone()[0]
                    self.assertEqual(samples[0], {'facility_id': 'YWJj', 'inspection_id': 42, 'entity': 'inspection', 'field': 'inspection_date', 'value': '1-2-2024', 'reason': 'invalid date'})
                    self.assertEqual(samples[1], {'facility_id': 'YWJj', 'inspection_id': 42, 'entity': 'report', 'field': 'time_in', 'value': 'bad:30 AM', 'reason': 'invalid time'})
                    self.assertEqual(conn.execute('SELECT code_title, points, corrected, repeat, notes FROM violations').fetchone(), ('1. Item', 2, True, False, 'Fix'))
                    replay = copy.deepcopy(fetched)
                    del replay[0]['inspections'][0]['report']
                    ingest_page(conn, run, 0, replay, 0)
                    self.assertEqual(conn.execute('SELECT (SELECT count(*) FROM facilities), (SELECT count(*) FROM inspections), (SELECT count(*) FROM violations)').fetchone(), (1, 1, 1))
                    self.assertEqual(conn.execute('SELECT report_fields FROM inspections').fetchone()[0], fields)
                    old_hash = conn.execute('SELECT violations_hash FROM inspections').fetchone()[0]
                    inspection['violations']['0'][2] = 'Points: 3'
                    replay, _, _ = fetch_page(conn, source, [facility], False, None, True)
                    ingest_page(conn, run, 0, replay, 0)
                    new_hash = conn.execute('SELECT violations_hash FROM inspections').fetchone()[0]
                    self.assertNotEqual(old_hash, new_hash)
                    self.assertEqual(conn.execute('SELECT snapshot_hash, points FROM violations ORDER BY points').fetchall(), [(old_hash, 2), (new_hash, 3)])
                    self.assertEqual(conn.execute('SELECT snapshot_hash, points FROM current_violations').fetchall(), [(new_hash, 3)])
                    replay[0]['inspections'][0]['report'] = RuntimeError('x')
                    ingest_page(conn, run, 0, replay, 0)
                    self.assertEqual(conn.execute('SELECT report_error FROM inspections').fetchone()[0], 'RuntimeError: x')
                    self.assertEqual(conn.execute('SELECT report_errors FROM etl_runs WHERE id=%s', (run,)).fetchone()[0], 1)
                    replay[0]['inspections'][0]['report'] = (fields, raw, errors)
                    ingest_page(conn, run, 0, replay, 0)
                    self.assertIsNone(conn.execute('SELECT report_error FROM inspections').fetchone()[0])
                    inspection['violations'] = {str(i): ['Item', 'Rule', 'Points: bad'] for i in range(25)}
                    capped_run = db.start(conn, {'start_page': 0})
                    capped, _, _ = fetch_page(conn, source, [facility], False, None, True)
                    ingest_page(conn, capped_run, 0, capped, 0)
                    samples, count = conn.execute("SELECT options->'conversion_samples', conversion_errors FROM etl_runs WHERE id=%s", (capped_run,)).fetchone()
                    self.assertEqual((len(samples), count), (20, 26))
                    self.assertEqual(samples[1], {'facility_id': 'YWJj', 'inspection_id': 42, 'entity': 'violation', 'violation_key': '0', 'field': 'points', 'value': 'bad', 'reason': 'invalid number'})
                    raise Rollback()
            except Rollback:
                pass
