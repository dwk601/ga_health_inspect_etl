import copy
import unittest

import psycopg

from ga_health_inspect import db
from ga_health_inspect.pipeline import ingest_page


class LifecycleTest(unittest.TestCase):
    def test_ingestion_replay_and_invalid_page(self):
        class Rollback(Exception):
            pass

        facility = {'id': 'YWJj', 'name': 'Example', 'mapAddress': '1 Main St', 'columns': {
            '0': '1 Main St', '1': 'Phone Number: 123', '2': 'Permit Type: Food Service',
            '3': 'Permit Number: F-1', '4': 'Last Inspection Score: 90', '5': 'Last Inspection Date: 01-02-2024'}}
        inspection = {'inspectionId': 42, 'facilityId': 'YWJj',
            'printablePath': '../../_templates/87/food_2015/_report_full.cfm?fsimID=42&rtype=food_2015',
            'columns': {'0': 'Date: 01-02-2024', '1': 'Inspection Purpose: Routine', '2': 'Score: 90', '3': 'Inspector: Person'},
            'violations': {'0': ['1. Item', 'Rule', 'Points: 2', 'Corrected during inspection?: Yes', 'Repeat: No', 'Inspector Notes: Fix']}}
        fields = {'name': 'Example', 'inspection_date': '2024-01-02', 'score': '90', 'permit_number': 'F-1'}
        fetched = [{'facility': facility, 'inspections': [{'inspection': inspection, 'report': (fields, {'name': 'Example'}, 0)}]}]
        with psycopg.connect(autocommit=True) as conn:
            try:
                with conn.transaction():
                    conn.execute('CREATE SCHEMA etl_lifecycle_test')
                    conn.execute('SET LOCAL search_path TO etl_lifecycle_test')
                    db.init(conn)
                    run = db.start(conn, {'start_page': 0})
                    ingest_page(conn, run, 0, fetched)
                    self.assertEqual(conn.execute('SELECT name, permit_number, last_inspection_date::text FROM facilities').fetchone(), ('Example', 'F-1', '2024-01-02'))
                    self.assertEqual(conn.execute('SELECT inspection_date::text, score, report_fields FROM inspections').fetchone(), ('2024-01-02', 90, fields))
                    self.assertEqual(conn.execute('SELECT code_title, points, corrected, repeat, notes FROM violations').fetchone(), ('1. Item', 2, True, False, 'Fix'))
                    replay = copy.deepcopy(fetched)
                    del replay[0]['inspections'][0]['report']
                    ingest_page(conn, run, 0, replay)
                    self.assertEqual(conn.execute('SELECT (SELECT count(*) FROM facilities), (SELECT count(*) FROM inspections), (SELECT count(*) FROM violations)').fetchone(), (1, 1, 1))
                    self.assertEqual(conn.execute('SELECT report_fields FROM inspections').fetchone()[0], fields)
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
