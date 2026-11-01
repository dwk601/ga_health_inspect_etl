import copy
import json
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest import mock
from zoneinfo import ZoneInfo

import psycopg
from scrapling.parser import Selector

from ga_health_inspect import db, pipeline, transform
from ga_health_inspect.pipeline import ingest_page
from ga_health_inspect.source import Source

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


class LifecycleTest(unittest.TestCase):
    def test_incremental_refresh(self):
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

            def search(self, page):
                self.searches.append(page)
                return [facilities[page]] if page < len(facilities) else []

            def inspections(self, facility_id):
                self.requests.append(facility_id)
                inspection = copy.deepcopy(INSPECTION)
                inspection['facilityId'] = facility_id
                return [inspection]

            def get(self, url):
                raise AssertionError('reports are disabled')

        options = dict(start_page=0, max_pages=1, full=False, overlap_days=14, reports=False, max_reports=None)
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
                    raise Rollback()
            except Rollback:
                pass

    def test_ingestion_replay_and_invalid_page(self):
        class Rollback(Exception):
            pass

        page = Selector('''<table><tr><td><b>Establishment Name:</b> Example</td></tr>
            <tr><td><b>Inspection Date:</b> 01/02/2024</td></tr>
            <tr><td><b>Time In:</b></td><td>bad</td><td>:</td><td>30</td><td>AM</td></tr>
            <tr><td><b>Time Out:</b></td><td>10</td><td>:</td><td>30</td><td>AM</td></tr></table>
            <div id="div_finalScore">90</div>''')
        errors = [0]
        parsed, raw = transform.report(page, 'food_2015', errors)
        self.assertEqual((raw['time_in'], parsed['time_in'], parsed['time_out'], errors[0]),
                         ('bad:30 AM', None, '10:30:00', 1))

        facility = copy.deepcopy(FACILITY)
        inspection = copy.deepcopy(INSPECTION)
        fields = parsed
        fetched = [{'facility': facility, 'inspections': [{'inspection': inspection, 'report': (fields, raw, errors[0])}]}]
        with psycopg.connect(autocommit=True) as conn:
            try:
                with conn.transaction():
                    conn.execute('CREATE SCHEMA etl_lifecycle_test')
                    conn.execute('SET LOCAL search_path TO etl_lifecycle_test')
                    db.init(conn)
                    run = db.start(conn, {'start_page': 0})
                    ingest_page(conn, run, 0, fetched)
                    self.assertEqual(conn.execute('SELECT name, permit_number, last_inspection_date::text FROM facilities').fetchone(), ('Example', 'F-1', '2024-01-02'))
                    self.assertEqual(conn.execute('SELECT inspection_date::text, score, report_fields, report_raw FROM inspections').fetchone(), (None, 90, fields, raw))
                    self.assertEqual(conn.execute("SELECT (counters->>'parsed_reports')::int, conversion_errors FROM etl_runs WHERE id=%s", (run,)).fetchone(), (1, 2))
                    self.assertEqual(conn.execute('SELECT code_title, points, corrected, repeat, notes FROM violations').fetchone(), ('1. Item', 2, True, False, 'Fix'))
                    replay = copy.deepcopy(fetched)
                    del replay[0]['inspections'][0]['report']
                    ingest_page(conn, run, 0, replay)
                    self.assertEqual(conn.execute('SELECT (SELECT count(*) FROM facilities), (SELECT count(*) FROM inspections), (SELECT count(*) FROM violations)').fetchone(), (1, 1, 1))
                    self.assertEqual(conn.execute('SELECT report_fields FROM inspections').fetchone()[0], fields)
                    old_hash = conn.execute('SELECT violations_hash FROM inspections').fetchone()[0]
                    replay[0]['inspections'][0]['inspection']['violations']['0'][2] = 'Points: 3'
                    ingest_page(conn, run, 0, replay)
                    new_hash = conn.execute('SELECT violations_hash FROM inspections').fetchone()[0]
                    self.assertNotEqual(old_hash, new_hash)
                    self.assertEqual(conn.execute('SELECT snapshot_hash, points FROM violations ORDER BY points').fetchall(), [(old_hash, 2), (new_hash, 3)])
                    replay[0]['inspections'][0]['report'] = RuntimeError('x')
                    ingest_page(conn, run, 0, replay)
                    self.assertEqual(conn.execute('SELECT report_error FROM inspections').fetchone()[0], 'RuntimeError: x')
                    self.assertEqual(conn.execute('SELECT report_errors FROM etl_runs WHERE id=%s', (run,)).fetchone()[0], 1)
                    replay[0]['inspections'][0]['report'] = (fields, raw, errors[0])
                    ingest_page(conn, run, 0, replay)
                    self.assertIsNone(conn.execute('SELECT report_error FROM inspections').fetchone()[0])
                    invalid = copy.deepcopy(replay)
                    invalid[0]['facility']['name'] = 'Must roll back'
                    invalid[0]['inspections'][0]['inspection']['inspectionId'] = None
                    with self.assertRaises(ValueError):
                        ingest_page(conn, run, 1, invalid)
                    self.assertEqual(conn.execute('SELECT next_page FROM etl_runs WHERE id=%s', (run,)).fetchone()[0], 1)
                    self.assertEqual(conn.execute('SELECT count(*) FROM inspections').fetchone()[0], 1)
                    self.assertEqual(conn.execute('SELECT name FROM facilities').fetchone()[0], 'Example')
                    raise Rollback()
            except Rollback:
                pass
