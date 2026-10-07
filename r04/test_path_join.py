import ast
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from reproduce_join import load, run

candidate = load(ROOT / "candidate" / "engine.py", "join_candidate")
original = load(ROOT / "engine_original.py", "join_original_controls")


class JoinRegression(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="agenthon-join-controls-")
        self.addCleanup(self.directory.cleanup)
        self.task = Path(self.directory.name) / "task"
        self.data = self.task / "environment" / "data"
        self.data.mkdir(parents=True)
        (self.data / "prices.csv").write_text("value\n1\n2\n3\n")
        self.instruction = "Read /app/data/prices.csv and write /output/result.json."

    def rewrite(self, code, instruction=None):
        return candidate.rewrite_legacy_input_literals(code, self.task, instruction or self.instruction)

    def test_original_end_to_end_failure_is_reproduced(self):
        result = run(original)
        self.assertEqual(result["status"], "FAILED")
        self.assertTrue(result["validated_legacy_directory_mapping"])
        self.assertFalse(result["program_transformed"])
        self.assertEqual(result["model_calls_are_fixed_local_stubs"], 2)
        self.assertIn("FileNotFoundError", result["fixed_error"])

    def test_corrected_end_to_end_publishes_exact_hand_computed_result(self):
        result = run(candidate)
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(result["actual_total"], 6.0)
        self.assertEqual(result["published_files"], ["result.json"])
        self.assertEqual(result["model_calls_are_fixed_local_stubs"], 1)
        self.assertEqual(result["real_house_calls"], 0)

    def test_six_standard_import_forms(self):
        forms = [
            ("import os", "os.path.join"),
            ("import os as runtime", "runtime.path.join"),
            ("import os.path", "os.path.join"),
            ("import os.path as p", "p.join"),
            ("from os import path as p", "p.join"),
            ("from os.path import join as j", "j"),
        ]
        for declaration, call in forms:
            with self.subTest(declaration=declaration):
                code = f"{declaration}\nx = {call}('/app/data', 'prices.csv')\n"
                changed = self.rewrite(code)
                self.assertNotEqual(changed, code)
                scope = {}
                exec(changed, scope)
                self.assertEqual(Path(scope["x"]), self.data / "prices.csv")

    def test_absolute_later_argument_obeys_standard_join_semantics(self):
        code = "import os\nx = os.path.join('discarded', '/app/data', 'prices.csv')\n"
        scope = {}
        exec(self.rewrite(code), scope)
        self.assertEqual(Path(scope["x"]), self.data / "prices.csv")

    def test_keyword_a(self):
        code = "import os\nx = os.path.join(a='/app/data')\n"
        scope = {}
        exec(self.rewrite(code), scope)
        self.assertEqual(Path(scope["x"]), self.data)

    def test_bare_app_composition_maps_validated_file_not_whole_app_directory(self):
        code = "import os\nx = os.path.join('/app', 'prices.csv')\n"
        scope = {}
        exec(self.rewrite(code, "Read /app/prices.csv and write /output/result.json."), scope)
        self.assertEqual(Path(scope["x"]), self.data / "prices.csv")

    def test_composed_docker_destination_maps_original_renamed_input(self):
        (self.task / "environment" / "Dockerfile").write_text(
            "FROM python:3.13\nCOPY data/prices.csv /app/renamed.csv\n")
        code = "import os\nx = os.path.join('/app', 'renamed.csv')\n"
        scope = {}
        exec(self.rewrite(code, "Read /app/renamed.csv and write /output/result.json."), scope)
        self.assertEqual(Path(scope["x"]), self.data / "prices.csv")

    def test_composed_escape_and_output_mount_unchanged(self):
        for code in (
            "import os\nx = os.path.join('/app/data', '..', 'checks')\n",
            "import os\nx = os.path.join('/app/output', 'result.json')\n",
        ):
            with self.subTest(code=code):
                self.assertEqual(self.rewrite(code), code)

    def test_unreferenced_absolute_path_not_rewritten(self):
        code = "import os\nx = os.path.join('/app/unmentioned', 'prices.csv')\n"
        self.assertEqual(self.rewrite(code), code)

    def test_custom_and_string_joins_unchanged(self):
        for code in (
            "def join(a, b): return a + b\nx = join('/app/data', 'prices.csv')\n",
            "x = ','.join(['/app/data', 'prices.csv'])\n",
            "x = custom.path.join('/app/data', 'prices.csv')\n",
        ):
            with self.subTest(code=code):
                self.assertEqual(self.rewrite(code), code)

    def test_rebound_alias_and_parameter_shadow_unchanged(self):
        for code in (
            "import os\nos = custom\nx = os.path.join('/app/data', 'prices.csv')\n",
            "import os\ndef f(os): return os.path.join('/app/data', 'prices.csv')\n",
            "from os.path import join\ndef join(a, b): return a + b\nx = join('/app/data', 'prices.csv')\n",
        ):
            with self.subTest(code=code):
                self.assertEqual(self.rewrite(code), code)

    def test_dynamic_or_mutated_namespace_unchanged(self):
        for code in (
            "import os\nos.path.join = custom\nx = os.path.join('/app/data', 'prices.csv')\n",
            "import os\nexec(source)\nx = os.path.join('/app/data', 'prices.csv')\n",
            "import os\nsetattr(os.path,'join',custom)\nx = os.path.join('/app/data', 'prices.csv')\n",
            "import os\nfrom custom import *\nx = os.path.join('/app/data', 'prices.csv')\n",
        ):
            with self.subTest(code=code):
                self.assertEqual(self.rewrite(code), code)

    def test_nested_import_shadow_unchanged(self):
        for code in (
            "import os\ndef f():\n    import custom as os\n    return os.path.join('/app/data','prices.csv')\n",
            "from os.path import join\ndef f():\n    from custom import join\n    return join('/app/data','prices.csv')\n",
        ):
            with self.subTest(code=code):
                self.assertEqual(self.rewrite(code), code)

    def test_exception_or_pattern_binding_shadow_unchanged(self):
        for code in (
            "import os\ntry: value()\nexcept Exception as os:\n    x = os.path.join('/app/data','prices.csv')\n",
            "import os\nmatch value:\n    case {'module': os}:\n        x = os.path.join('/app/data','prices.csv')\n",
        ):
            with self.subTest(code=code):
                self.assertEqual(self.rewrite(code), code)

    def test_duplicate_conflicting_import_not_rewritten(self):
        code = "import os as p\nimport custom as p\nx = p.path.join('/app/data', 'prices.csv')\n"
        self.assertEqual(self.rewrite(code), code)

    def test_relative_and_conditional_star_imports_not_standard_bindings(self):
        for code in (
            "from .os import path as p\nx = p.join('/app/data','prices.csv')\n",
            "from .os.path import join\nx = join('/app/data','prices.csv')\n",
            "import os\nif condition:\n    from custom import *\nx = os.path.join('/app/data','prices.csv')\n",
        ):
            with self.subTest(code=code):
                self.assertEqual(self.rewrite(code), code)

    def test_invalid_python_unchanged(self):
        code = "import os\nx = os.path.join('/app/data',\n"
        self.assertEqual(self.rewrite(code), code)

    def test_other_original_top_level_definitions_exact_ast(self):
        before = ast.parse((ROOT / "engine_original.py").read_text())
        after = ast.parse((ROOT / "candidate" / "engine.py").read_text())
        a = {n.name: ast.dump(n, include_attributes=False) for n in before.body if hasattr(n, "name")}
        b = {n.name: ast.dump(n, include_attributes=False) for n in after.body if hasattr(n, "name")}
        self.assertEqual(a.keys(), b.keys())
        self.assertEqual([name for name in a if a[name] != b[name]], ["rewrite_legacy_input_literals"])


if __name__ == "__main__":
    unittest.main()
