"""GitHub Actions 离线冒烟测试：不访问任何外部学术接口。"""

from __future__ import annotations

import py_compile
import json
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
        for package in ("requests", "pyyaml", "urllib3", "openai"):
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
        self.assertTrue(config["openai"]["enabled"])
        self.assertEqual(config["openai"]["model"], "gpt-5.4-mini")

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
            self.assertIn("## 本周总体趋势", content)
            self.assertIn("## 下周建议继续追踪的关键词", content)

    def test_chinese_summary_uses_only_title_and_abstract_evidence(self) -> None:
        config = main.load_config(ROOT / "config.yaml")
        paper = main.Paper(
            title="Metabolomics reveals flavor formation in tea leaves",
            journal="Tea Science",
            year=2026,
            publication_date=main.date(2026, 7, 5),
            doi="10.1000/example",
            url="https://doi.org/10.1000/example",
            abstract=(
                "LC-MS metabolomics identified 120 metabolites in 30 samples. "
                "The results showed significant differences associated with tea flavor quality."
            ),
            categories={"茶树代谢组", "茶叶品质"},
            sources={"Crossref"},
            matched_keywords={"tea metabolomics"},
        )
        summary = main.summarize_paper(paper, main.date(2026, 7, 6), config)
        self.assertIn("代谢组", summary.study)
        self.assertIn("液相色谱-质谱", summary.methods)
        self.assertIn("120种代谢物", summary.conclusion)
        self.assertIn(summary.recommendation, {"高", "中", "低"})

        report = main.generate_report(
            [paper], [], config, main.date(2026, 6, 30), main.date(2026, 7, 6)
        )
        for field in (
            "期刊/年份", "DOI", "原文链接", "研究内容概括", "主要研究方法",
            "核心结论", "对我的研究启发", "推荐阅读等级",
        ):
            self.assertIn(field, report)

    def test_missing_abstract_is_cautious(self) -> None:
        config = main.load_config(ROOT / "config.yaml")
        paper = main.Paper(
            title="SSR markers for tea germplasm",
            categories={"SSR与分子标记"},
        )
        summary = main.summarize_paper(paper, main.date(2026, 7, 6), config)
        self.assertIn("摘要缺失，以下为基于标题的初步判断", summary.study)
        self.assertIn("标题不足以支持具体结论判断", summary.conclusion)
        self.assertEqual(summary.recommendation, "低")

    def test_pmc_full_text_extraction_excludes_references(self) -> None:
        root = main.ET.fromstring(
            """<article><body>
            <sec><title>Methods</title><p>LC-MS was used to profile metabolites.</p></sec>
            <sec><title>Results</title><p>Quality-associated compounds were identified.</p></sec>
            </body><ref-list><ref><mixed-citation>Should not appear</mixed-citation></ref></ref-list></article>"""
        )
        text = main.extract_pmc_body(root, 10000)
        self.assertIn("Methods", text)
        self.assertIn("LC-MS", text)
        self.assertIn("Results", text)
        self.assertNotIn("Should not appear", text)

    def test_openai_summary_uses_structured_output_and_full_text(self) -> None:
        class FakeResponses:
            def __init__(self) -> None:
                self.kwargs = None

            def create(self, **kwargs):
                self.kwargs = kwargs
                payload = {
                    "study": "比较茶树材料的代谢差异与品质性状。",
                    "methods": "采用 LC-MS 和多变量统计分析。",
                    "conclusion": "若干代谢物与品质差异相关。",
                    "inspiration": "可将候选代谢物用于种质资源品质评价，并开展独立材料验证。",
                    "recommendation": "高",
                }
                return type("Response", (), {"output_text": json.dumps(payload, ensure_ascii=False)})()

        class FakeClient:
            def __init__(self) -> None:
                self.responses = FakeResponses()

        paper = main.Paper(
            title="Tea metabolomics and quality",
            abstract="An abstract.",
            full_text="Methods: LC-MS. Results: metabolites differed among cultivars.",
        )
        client = FakeClient()
        summary = main.summarize_paper_with_openai(
            paper, client, "gpt-5.4-mini", {"max_input_characters": 30000}
        )
        self.assertEqual(summary.recommendation, "高")
        self.assertIn("PMC 开放获取全文", summary.study)
        self.assertEqual(summary.basis, "OpenAI 深度总结（PMC 公开全文）")
        self.assertEqual(
            client.responses.kwargs["text"]["format"]["type"], "json_schema"
        )
        self.assertTrue(client.responses.kwargs["text"]["format"]["strict"])

    def test_schedule_is_monday_9am_in_utc_plus_8(self) -> None:
        workflow_path = ROOT / ".github" / "workflows" / "weekly-tracker.yml"
        workflow = yaml.load(workflow_path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
        self.assertEqual(workflow["on"]["schedule"][0]["cron"], "0 1 * * 1")
        self.assertEqual(
            workflow["jobs"]["generate-weekly-report"]["env"]["TZ"],
            "Asia/Shanghai",
        )
        self.assertIn(
            "OPENAI_API_KEY",
            workflow["jobs"]["generate-weekly-report"]["env"],
        )


if __name__ == "__main__":
    unittest.main()
