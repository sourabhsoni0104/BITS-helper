import json
import math
import tempfile
import unittest
from pathlib import Path

from recommender.ingestion.snapshots import SnapshotError, load_snapshot, publish_snapshot


ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "data/synthetic/demo_snapshot.json"


class SnapshotRegressionTests(unittest.TestCase):
    def payload(self):
        return json.loads(FIXTURE.read_text(encoding="utf-8"))

    def rejected(self, edit):
        raw = self.payload()
        edit(raw)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "snapshot.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaises(SnapshotError):
                load_snapshot(path)

    def test_synthetic_requires_boolean(self):
        self.rejected(lambda raw: raw.update(synthetic="false"))

    def test_finite_nonnegative_numeric_fields(self):
        for value in (-1, math.inf, -math.inf, math.nan, "3", True):
            with self.subTest(value=value):
                self.rejected(lambda raw, value=value: raw["courses"][0].update(units=value))
        self.rejected(lambda raw: raw["policy_contexts"][0]["requirements"][0].update(required_value=-1))

    def test_bad_roots_and_nested_shapes_are_snapshot_errors(self):
        for malformed in ([], {"courses": "bad"}, {"courses": [None]}, {"offerings": [{}]}):
            with self.subTest(malformed=malformed), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "snapshot.json"
                path.write_text(json.dumps(malformed), encoding="utf-8")
                with self.assertRaises(SnapshotError):
                    load_snapshot(path)

    def test_prerequisite_types_and_programme_lists_are_checked(self):
        self.rejected(lambda raw: raw["offerings"][0].update(prerequisite={"op": "all_of", "conditions": "bad"}))
        self.rejected(lambda raw: raw["offerings"][0].update(prerequisite={"op": "minimum_semester", "value": True}))
        self.rejected(lambda raw: raw["offerings"][0]["categories"][0].update(programme_ids="BSC-SYNTH"))
        self.rejected(lambda raw: raw["policy_contexts"][0].update(programme_ids="BSC-SYNTH"))
        loaded = self.payload()
        loaded["offerings"][0]["prerequisite"] = {"op": "all_of", "conditions": []}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "snapshot.json"
            path.write_text(json.dumps(loaded), encoding="utf-8")
            self.assertEqual(load_snapshot(path).offerings[0].prerequisite["conditions"], [])

    def test_mandatory_courses_must_be_in_pool(self):
        self.rejected(lambda raw: raw["policy_contexts"][0]["requirements"][0].update(mandatory_course_ids=["NOT-IN-POOL"]))

    def test_publish_failure_preserves_existing_destination(self):
        with tempfile.TemporaryDirectory() as directory:
            candidate = Path(directory) / "candidate.json"
            destination = Path(directory) / "published.json"
            candidate.write_text('{"synthetic":"false"}', encoding="utf-8")
            destination.write_bytes(b"previous snapshot")
            with self.assertRaises(SnapshotError):
                publish_snapshot(candidate, destination)
            self.assertEqual(destination.read_bytes(), b"previous snapshot")


if __name__ == "__main__":
    unittest.main()
