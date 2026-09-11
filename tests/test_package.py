import json
from pathlib import Path
import re
import subprocess
import unittest
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
DOCUMENTS = [ROOT / "README.md", ROOT / "ROADMAP.md", ROOT / "skills/feishu-agent-loop/SKILL.md"]


class PackageTests(unittest.TestCase):
    def test_plugin_versions_and_names_agree(self):
        plugin = json.loads((ROOT / ".claude-plugin/plugin.json").read_text())
        market = json.loads((ROOT / ".claude-plugin/marketplace.json").read_text())
        self.assertEqual(plugin["version"], market["metadata"]["version"])
        self.assertEqual(plugin["version"], market["plugins"][0]["version"])
        self.assertEqual(plugin["name"], market["plugins"][0]["name"])
        self.assertEqual(market["plugins"][0]["source"], "./")

    def test_bundled_skill_includes_required_assets(self):
        for relative in ("LICENSE", "skills/feishu-agent-loop/scripts/events.py",
                         "skills/feishu-agent-loop/scripts/listen.sh",
                         "skills/feishu-agent-loop/examples/routing.example.json",
                         ".github/workflows/tests.yml"):
            with self.subTest(path=relative):
                self.assertTrue((ROOT / relative).is_file())

    def test_local_markdown_links_exist_and_stay_in_repo(self):
        for document in DOCUMENTS:
            for target in re.findall(r"\[[^\]]+\]\(([^)]+)\)", document.read_text()):
                parsed = urlsplit(target)
                if parsed.scheme or not parsed.path:
                    continue
                path = (document.parent / unquote(parsed.path)).resolve()
                with self.subTest(document=document.name, target=target):
                    self.assertTrue(path.is_relative_to(ROOT))
                    self.assertTrue(path.exists())

    def test_bash_examples_parse_without_executing(self):
        for document in DOCUMENTS:
            examples = re.findall(r"^```bash\n(.*?)^```", document.read_text(), flags=re.MULTILINE | re.DOTALL)
            for index, example in enumerate(examples):
                with self.subTest(document=document.name, example=index):
                    result = subprocess.run(["bash", "-n"], input=example, text=True,
                                            capture_output=True, timeout=5)
                    self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
