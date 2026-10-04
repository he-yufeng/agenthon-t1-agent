"""One loopback CLI smoke over r00, not semantic-review or House quality."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parent
BASE = ROOT.parent / "release_2026-10-02_r00/submission"
GOOD = '''import os, json
from pathlib import Path
task = Path(os.environ["QFBENCH_TASK_DIR"])
out = Path(os.environ["QFBENCH_AGENT_OUT"])
value = json.loads((task / "value.json").read_text())["value"]
(out / "answer.json").write_text(json.dumps({"value": 2 * value}))
'''


class BaselineNonThinkingCLI(unittest.TestCase):
    def test_original_baseline_cli_uses_one_request_without_review(self):
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                data = json.dumps({"choices":[{"message":{"content":GOOD}}]}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1",0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory(prefix="base-nonthinking-owned-") as directory:
                root = Path(directory)
                task = root / "task"
                task.mkdir()
                (task / "instruction.md").write_text(
                    "Read value.json. Write `/app/output/answer.json` containing the value multiplied by two.")
                (task / "value.json").write_text('{"value":7}')
                (task / "card.toml").write_text('[task]\nsplit="public-dev"\n[agent]\ntimeout_sec=1200\n')
                output = root / "output"
                env = {"PATH":os.defpath, "PYTHONDONTWRITEBYTECODE":"1",
                    "PYTHONPATH":str(BASE), "MODEL_ENDPOINT":f"http://127.0.0.1:{server.server_port}",
                    "MODEL_NAME":"synthetic-base-model", "MODEL_TOKEN":"SYNTHETIC_BASE_TOKEN_ONLY",
                    "AGENTHON_HTTP_TIMEOUT_SEC":"2", "AGENTHON_ENDPOINT_MAX_RETRIES":"0",
                    "AGENTHON_HOUSE_MAX_REQUESTS":"25", "AGENTHON_HOUSE_MAX_OUTPUT_TOKENS":"4000",
                    "QFBENCH_SEED":"17"}
                result = subprocess.run([sys.executable,"-B",str(ROOT / "agent.py"),"solve",
                    "--task-dir",str(task),"--out",str(output)],
                    env=env, capture_output=True, text=True, timeout=20)
                self.assertEqual(result.returncode,0,result.stderr)
                self.assertEqual(json.loads((output / "answer.json").read_text()),{"value":14})
                self.assertEqual(len(requests),1)
                self.assertEqual(requests[0]["chat_template_kwargs"],{"enable_thinking":False})
                self.assertEqual(set(requests[0]),{"model","messages","max_tokens","seed",
                                                   "temperature","chat_template_kwargs"})
                self.assertLessEqual(requests[0]["max_tokens"],4000)
                self.assertEqual(requests[0]["model"],"synthetic-base-model")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
