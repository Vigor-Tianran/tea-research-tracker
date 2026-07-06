"""GitHub Actions 离线冒烟测试：不访问任何外部学术接口。"""

from __future__ import annotations

import py_compile
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import requests
import urllib3
import yaml

import main


ROOT = Path(__file__).resolve().parents[1]


class GitHubActionsSmokeTests(unittest.TestCase):
    def test_requirements_cover_direct_dependencies(self) -> None:
        requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8").lower()
        for package in ("requests", "pyyaml", "urllib3"):
            self.assertIn(package, requirements)
        self.assertTrue(requests.__version__)
        self.assertTrue(urllib3.__version__)
        self.assertTrue(yaml.__version__)

    def test_main_has_no_syntax_error(self) -> None:
        py_compile.compile(str(ROOT / "main.py"), doraise=True)

    def test_config_can_be_loaded(self) -> None:
        config = main.load_config(ROOT / "config.yaml")
        self.assertEqual(config["report"]["lookback_days"], 7)
        self.assertIn("茶树综合研究", config["categories"])
        self.assertTrue(config["sources"]["crossref"]["enabled"])

    def test_empty_search_creates_report_directory_and_report(self) -> None:
        config = main.load_config(ROOT / "config.yaml")
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            config_path = temp / "config.yaml"
            config_path.write_text(
                yaml.safe_dump(config, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )
            output_dir = temp / "new" / "reports"
            argv = [
                "main.py",
                "--config",
                str(config_path),
                "--date",
                "2026-07-06",
                "--output-dir",
                str(output_dir),
            ]
            with patch.object(main, "collect_papers", return_value=([], [])), patch.object(
                sys, "argv", argv
            ):
                self.assertEqual(main.main(), 0)

            report = output_dir / "weekly_report_2026-07-06.md"
            self.assertTrue(output_dir.is_dir())
            self.assertTrue(report.is_file())
            content = report.read_text(encoding="utf-8")
            self.assertIn("共去重获得 **0** 篇相关论文", content)
            self.assertIn("暂未检索到可确认的论文记录", content)

    def test_schedule_is_monday_9am_in_utc_plus_8(self) -> None:
        workflow_path = ROOT / ".github" / "workflows" / "weekly-tracker.yml"
        workflow = yaml.load(workflow_path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
        self.assertEqual(workflow["on"]["schedule"][0]["cron"], "0 1 * * 1")
        self.assertEqual(
            workflow["jobs"]["generate-weekly-report"]["env"]["TZ"],
            "Asia/Shanghai",
        )


if __name__ == "__main__":
    unittest.main()
