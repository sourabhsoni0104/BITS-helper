from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from recommender.ingestion.handouts import HandoutEntry
from recommender.ingestion.review import build_review_bundle, publish_review, save_review
from recommender.ingestion.snapshots import SnapshotError


class ReviewWorkflowTests(unittest.TestCase):
    def test_extraction_stays_unverified_and_does_not_guess_absent_properties(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / 'corpus.sqlite'
            with sqlite3.connect(database) as db:
                db.executescript('CREATE TABLE documents(document_id TEXT,primary_file_name TEXT,content_sha256 TEXT);'
                    'CREATE TABLE sources(file_name TEXT,document_id TEXT);'
                    'CREATE TABLE pages(document_id TEXT,page_number INTEGER,text TEXT);')
                db.execute('INSERT INTO documents VALUES (?,?,?)', ('D1','test.pdf','hash'))
                db.execute('INSERT INTO sources VALUES (?,?)', ('test.pdf','D1'))
                db.execute('INSERT INTO pages VALUES (?,?,?)', ('D1',1,'Course No: CS F213\nCourse title: Programming\nNo marks for attendance.'))
            db.close()
            entry = HandoutEntry(1,'123','CS F213','Programming','today',
                'https://academic.bits-pilani.ac.in/Faculty/Course_Handouts/Handout_Files/Current_Handouts/test.pdf')
            with patch('recommender.ingestion.review.parse_handout_index',return_value=(entry,)):
                bundle=build_review_bundle(database,root/'index.html',root/'draft.json')
            self.assertEqual(bundle['status'],'needs_review')
            offering=bundle['snapshot']['offerings'][0]
            self.assertIsNone(offering['prerequisite'])
            self.assertIsNone(offering['availability']['value'])
            self.assertIsNone(offering['handout_facts']['attendance_required']['value'])
            self.assertTrue(all(e['verification_status']=='needs_review' for e in bundle['snapshot']['evidence']))
            active=root/'active.json'
            active.write_text('old valid data',encoding='utf-8')
            with self.assertRaises(SnapshotError):
                publish_review(root/'draft.json',active,'reviewer')
            self.assertEqual(active.read_text(),'old valid data')

    def test_reviewed_publication_retains_previous_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            snapshot=json.loads((Path(__file__).resolve().parents[2]/'data/synthetic/demo_snapshot.json').read_text())
            
            snapshot['synthetic']=False
            draft=root/'draft.json'
            save_review({'status':'needs_review'},snapshot,draft,'tester')
            active=root/'active.json'
            active.write_text(json.dumps(snapshot))
            result=publish_review(draft,active,'tester')
            self.assertEqual(result['dataset_version'],snapshot['dataset_version'])
            self.assertEqual(len(list((root/'history').glob('*.json'))),1)
            self.assertEqual(json.loads(draft.read_text())['status'],'published')


if __name__=='__main__':
    unittest.main()
