"""Mandatory extra tests in the actual frozen dependency image; no skip fallback."""
import json
from pathlib import Path
import tempfile
import unittest
import pyarrow as pa
import pyarrow.parquet as pq
import input_profile_r05 as candidate


class NativeParquetTests(unittest.TestCase):
    def test_real_parquet_and_pqt_metadata_only(self):
        with tempfile.TemporaryDirectory(prefix="r05-native-parquet-") as raw:
            task = Path(raw)
            data = task / "environment/data"
            data.mkdir(parents=True)
            table = pa.table({"instrument_id": ["PRIVATE_ROW_A", "PRIVATE_ROW_B"], "owned_quantity": [3, 7]})
            for name in ("owned.parquet", "owned.pqt"):
                pq.write_table(table, data / name)
            text = candidate.run_profile_worker(task, 4096, lambda _: False)
            rows = json.loads(text[len(candidate.PREFIX):])
            self.assertEqual(len(rows), 2)
            self.assertTrue(all(row["rows_from_metadata"] == 2 for row in rows))
            self.assertTrue(all(row["fields"] == [{"name": "instrument_id", "type": "string"},
                                                 {"name": "owned_quantity", "type": "int64"}] for row in rows))
            self.assertNotIn("PRIVATE_ROW", text)
            self.assertNotIn("statistics", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
