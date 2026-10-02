from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BASE = (
    "python:3.13-slim@"
    "sha256:16f75ad0fbc6c4883a8afd63b2d700c3cf68ccffc1aaeca5304ca0a3a908451f"
)


class ContainerContractTests(unittest.TestCase):
    def test_image_contract_is_pinned_and_explicit(self):
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.assertRegex(dockerfile, rf"(?m)^FROM {re.escape(BASE)}$")
        self.assertIn('LABEL qfbench2.interface_version="2.0"', dockerfile)
        self.assertIn('ENTRYPOINT ["python", "/agent/agent.py"]', dockerfile)
        self.assertIn(
            'CMD ["solve", "--task-dir", "/input", "--out", "/app/output"]',
            dockerfile,
        )
        self.assertIn("COPY agent.py engine.py /agent/", dockerfile)
        self.assertIn("requirements-sandbox.lock.sha256", dockerfile)
        self.assertIn("sha256sum -c ../requirements-sandbox.lock.sha256", dockerfile)
        self.assertIn("pip download --only-binary=:all: --no-deps", dockerfile)
        self.assertIn("pip install --no-index --no-deps --find-links=.", dockerfile)
        self.assertIn("python -m pip check", dockerfile)

    def test_context_is_an_allowlist(self):
        rules = [
            line.strip()
            for line in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        self.assertEqual(
            rules,
            [
                "**",
                "!Dockerfile",
                "!.dockerignore",
                "!agent.py",
                "!engine.py",
                "!base/",
                "base/**",
                "!base/requirements-sandbox.lock",
                "!base/requirements-sandbox.lock.sha256",
                "!base/smoke_imports.py",
            ],
        )
        self.assertFalse(any("tests" in rule or "checks" in rule for rule in rules[1:]))


if __name__ == "__main__":
    unittest.main()
