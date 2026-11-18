import csv
import hashlib
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / 'engine'))
import test_export_model_aq as fixture
from recover_spec_results import recover


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.case = fixture.ExportModelAQTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.case.prepare('original')
        self.root = self.case.root
        self.folder = self.root / 'aq_out/spec_guard_four_models/deepseek/run_01'
        self.target = self.folder / 'spec_check_results.json'
        self.rows = json.loads(self.target.read_text())
        for row, block in zip(self.rows, self.case.blocks):
            row.update(model_id='deepseek-test', assertion_text=block.text)
        self.target.unlink()
        summary = json.loads((self.folder / 'summary.json').read_text())
        summary.update(model_id='deepseek-test', final_aq=1, a0_count=3,
                       post_spec_qualification_yield_percent=100 / 3)
        (self.folder / 'summary.json').write_text(json.dumps(summary))

    def write_cache(self):
        folder = self.folder / 'cache'
        folder.mkdir()
        for index, row in enumerate(self.rows):
            (folder / f'{index}.json').write_text(json.dumps(row))

    def write_csv_traces(self, wrong_run=False):
        folder = self.folder / 'traces'
        folder.mkdir()
        records = []
        for index, original in enumerate(self.rows):
            row = dict(original)
            trace_file = f'deepseek/run_01/traces/{index}.json'
            trace = {'model_key': 'deepseek', 'run_number': 2 if wrong_run else 1,
                'assertion_file': row['assertion_file'], 'assertion_id': row['assertion_id'],
                'property_name': row['property_name'], 'requested_model': 'deepseek-test',
                'cache_fingerprint': row['fingerprint'], 'mapped_verdict': row['verdict'],
                'status': 'API_SUCCESS', 'request': {'payload': {'messages': [
                    {'role': 'user', 'content': row['assertion_text']}]}}}
            (folder / f'{index}.json').write_text(json.dumps(trace))
            row['trace_file'] = trace_file
            del row['fingerprint']
            del row['run_number']
            records.append(row)
        with (self.folder / 'spec_check_results.csv').open('w', encoding='utf-8-sig', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=list(records[0]))
            writer.writeheader()
            writer.writerows(records)

    def test_restores_exact_saved_cache_rows_without_overwriting(self):
        self.write_cache()
        result = recover(self.root, 'original', 'deepseek', 1)
        self.assertEqual(result['rows'], 2)
        restored = json.loads(self.target.read_text())
        self.assertEqual(restored, self.rows)
        before = self.target.read_bytes()
        with self.assertRaisesRegex(ValueError, 'already exists'):
            recover(self.root, 'original', 'deepseek', 1)
        self.assertEqual(self.target.read_bytes(), before)

    def test_recovers_csv_without_request_fingerprint_using_same_run_traces(self):
        self.write_csv_traces()
        result = recover(self.root, 'original', 'deepseek', 1)
        self.assertEqual(result['passed'], 1)
        restored = json.loads(self.target.read_text())
        self.assertEqual([r['fingerprint'] for r in restored], [r['fingerprint'] for r in self.rows])
        self.assertTrue(all(r['run_number'] == 1 for r in restored))

    def test_does_not_copy_another_run_or_invent_missing_evidence(self):
        with self.assertRaisesRegex(ValueError, 'No authentic same-run evidence'):
            recover(self.root, 'original', 'deepseek', 1)
        self.assertFalse(self.target.exists())
        self.write_csv_traces(wrong_run=True)
        with self.assertRaisesRegex(ValueError, 'No authentic same-run evidence'):
            recover(self.root, 'original', 'deepseek', 1)
        self.assertFalse(self.target.exists())

    def test_source_change_prevents_recovery(self):
        self.write_cache()
        source = self.root / 'assertions' / fixture.SOURCE_NAME
        source.write_text(source.read_text() + '\n// changed\n')
        with self.assertRaisesRegex(ValueError, 'source changed'):
            recover(self.root, 'original', 'deepseek', 1)
        self.assertFalse(self.target.exists())


if __name__ == '__main__':
    unittest.main()
