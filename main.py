#!/usr/bin/env python3
"""茶叶方向每周学术进展追踪程序。

论文元数据来自公开学术接口。若配置 OpenAI API，程序会优先结合 PMC
开放获取全文生成结构化中文总结；无法取得全文时降级到摘要或保守规则。
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import logging
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable
from zoneinfo import ZoneInfo

import requests
import yaml
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:
    from openai import OpenAI
except ImportError:  # 便于在未安装可选依赖的环境中运行离线检查
    OpenAI = None  # type: ignore[assignment,misc]


LOGGER = logging.getLogger("tea-research-tracker")


@dataclass
class Paper:
    title: str
    authors: list[str] = field(default_factory=list)
    journal: str = ""
    year: int | None = None
    publication_date: date | None = None
    doi: str = ""
    url: str = ""
    abstract: str = ""
    pmcid: str = ""
    full_text: str = ""
    sources: set[str] = field(default_factory=set)
    categories: set[str] = field(default_factory=set)
    matched_keywords: set[str] = field(default_factory=set)

    @property
    def identity(self) -> str:
        if self.doi:
            return f"doi:{normalize_doi(self.doi)}"
        normalized = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", self.title.lower())
        return f"title:{normalized}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="生成最近 7 天的茶叶方向 Markdown 学术周报")
    parser.add_argument("--config", default="config.yaml", help="配置文件路径（默认：config.yaml）")
    parser.add_argument(
        "--date",
        help="周报结束日期，格式 YYYY-MM-DD；默认使用北京时间当天日期",
    )
    parser.add_argument("--output-dir", help="覆盖 config.yaml 中的报告输出目录")
    parser.add_argument("--dry-run", action="store_true", help="检索并预览，但不写入报告文件")
    return parser.parse_args()


def load_config(path: str | Path) -> dict[str, Any]:
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"找不到配置文件：{config_path}")
    with config_path.open("r", encoding="utf-8") as handle:
        config = yaml.safe_load(handle) or {}
    if not config.get("categories"):
        raise ValueError("config.yaml 必须包含 categories（研究方向与关键词）")
    return config


def build_session(config: dict[str, Any]) -> requests.Session:
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        backoff_factor=1.0,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
        respect_retry_after_header=True,
    )
    session = requests.Session()
    session.mount("https://", HTTPAdapter(max_retries=retry))
    email = os.getenv("CONTACT_EMAIL") or config.get("contact_email", "")
    agent = "tea-research-tracker/1.0"
    if email:
        agent += f" (mailto:{email})"
    session.headers.update({"User-Agent": agent, "Accept": "application/json"})
    return session


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = html.unescape(str(value))
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def xml_text(element: ET.Element | None) -> str:
    if element is None:
        return ""
    return clean_text("".join(element.itertext()))


def normalize_doi(value: str) -> str:
    value = clean_text(value).lower()
    value = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", value)
    return value.removeprefix("doi:").strip()


def safe_date(year: Any, month: Any = 1, day: Any = 1) -> date | None:
    try:
        return date(int(year), int(month or 1), int(day or 1))
    except (TypeError, ValueError):
        return None


def crossref_date(item: dict[str, Any]) -> date | None:
    for key in ("published-online", "published-print", "published", "issued", "created"):
        parts = item.get(key, {}).get("date-parts", [])
        if parts and parts[0]:
            values = list(parts[0]) + [1, 1]
            parsed = safe_date(values[0], values[1], values[2])
            if parsed:
                return parsed
    return None


def flatten_keywords(config: dict[str, Any]) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for category, keywords in config["categories"].items():
        for keyword in keywords or []:
            pair = (category, str(keyword).strip())
            if pair[1] and pair not in seen:
                seen.add(pair)
                pairs.append(pair)
    return pairs


def fetch_crossref(
    session: requests.Session,
    keyword: str,
    category: str,
    start: date,
    end: date,
    source_config: dict[str, Any],
    timeout: float,
    contact_email: str,
) -> list[Paper]:
    params: dict[str, Any] = {
        "query.bibliographic": keyword,
        "filter": f"from-pub-date:{start.isoformat()},until-pub-date:{end.isoformat()}",
        "rows": int(source_config.get("rows_per_keyword", 20)),
        "sort": "published",
        "order": "desc",
        "select": "DOI,title,author,container-title,published-online,published-print,published,issued,created,URL,abstract,type",
    }
    if contact_email:
        params["mailto"] = contact_email
    response = session.get("https://api.crossref.org/works", params=params, timeout=timeout)
    response.raise_for_status()
    papers: list[Paper] = []
    for item in response.json().get("message", {}).get("items", []):
        title = clean_text(" ".join(item.get("title") or []))
        if not title:
            continue
        pub_date = crossref_date(item)
        authors = []
        for author in item.get("author") or []:
            name = clean_text(" ".join(part for part in (author.get("given"), author.get("family")) if part))
            if name:
                authors.append(name)
        doi = normalize_doi(item.get("DOI", ""))
        papers.append(
            Paper(
                title=title,
                authors=authors,
                journal=clean_text("; ".join(item.get("container-title") or [])),
                year=pub_date.year if pub_date else None,
                publication_date=pub_date,
                doi=doi,
                url=f"https://doi.org/{doi}" if doi else clean_text(item.get("URL")),
                abstract=clean_text(item.get("abstract")),
                sources={"Crossref"},
                categories={category},
                matched_keywords={keyword},
            )
        )
    return papers


def fetch_semantic_scholar(
    session: requests.Session,
    keyword: str,
    category: str,
    start: date,
    end: date,
    source_config: dict[str, Any],
    timeout: float,
    _: str,
) -> list[Paper]:
    headers: dict[str, str] = {}
    api_key = os.getenv("S2_API_KEY", "").strip()
    if api_key:
        headers["x-api-key"] = api_key
    params = {
        "query": keyword,
        "limit": min(int(source_config.get("rows_per_keyword", 20)), 100),
        "fields": "title,authors,year,venue,publicationDate,externalIds,url,abstract",
    }
    response = session.get(
        "https://api.semanticscholar.org/graph/v1/paper/search",
        params=params,
        headers=headers,
        timeout=timeout,
    )
    response.raise_for_status()
    papers: list[Paper] = []
    for item in response.json().get("data") or []:
        title = clean_text(item.get("title"))
        published = safe_date(*(str(item.get("publicationDate", "")).split("-") + [1, 1])[:3])
        if not title or not published or not (start <= published <= end):
            continue
        ids = item.get("externalIds") or {}
        doi = normalize_doi(ids.get("DOI", ""))
        papers.append(
            Paper(
                title=title,
                authors=[clean_text(author.get("name")) for author in item.get("authors") or [] if author.get("name")],
                journal=clean_text(item.get("venue")),
                year=item.get("year") or published.year,
                publication_date=published,
                doi=doi,
                url=f"https://doi.org/{doi}" if doi else clean_text(item.get("url")),
                abstract=clean_text(item.get("abstract")),
                sources={"Semantic Scholar"},
                categories={category},
                matched_keywords={keyword},
            )
        )
    return papers


def pubmed_publication_date(article: ET.Element) -> date | None:
    article_date = article.find(".//ArticleDate")
    if article_date is not None:
        parsed = safe_date(
            xml_text(article_date.find("Year")),
            xml_text(article_date.find("Month")),
            xml_text(article_date.find("Day")),
        )
        if parsed:
            return parsed
    pub_date = article.find(".//JournalIssue/PubDate")
    if pub_date is None:
        return None
    year_text = xml_text(pub_date.find("Year"))
    if not year_text:
        match = re.search(r"(19|20)\d{2}", xml_text(pub_date.find("MedlineDate")))
        year_text = match.group(0) if match else ""
    month_text = xml_text(pub_date.find("Month"))
    month_lookup = {name.lower(): index for index, name in enumerate(
        ("", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
    )}
    month: int | str = month_lookup.get(month_text[:3].lower(), month_text or 1)
    return safe_date(year_text, month, xml_text(pub_date.find("Day")) or 1)


def fetch_pubmed(
    session: requests.Session,
    keyword: str,
    category: str,
    start: date,
    end: date,
    source_config: dict[str, Any],
    timeout: float,
    contact_email: str,
) -> list[Paper]:
    base = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
    api_key = os.getenv("NCBI_API_KEY", "").strip()
    common: dict[str, Any] = {"tool": "tea_research_tracker"}
    if contact_email:
        common["email"] = contact_email
    if api_key:
        common["api_key"] = api_key
    term = (
        f'("{keyword}"[Title/Abstract]) AND '
        f'("{start:%Y/%m/%d}"[Date - Publication] : "{end:%Y/%m/%d}"[Date - Publication])'
    )
    search_params = {
        **common,
        "db": "pubmed",
        "term": term,
        "retmax": int(source_config.get("rows_per_keyword", 20)),
        "retmode": "json",
        "sort": "pub date",
    }
    response = session.get(f"{base}/esearch.fcgi", params=search_params, timeout=timeout)
    response.raise_for_status()
    ids = response.json().get("esearchresult", {}).get("idlist", [])
    if not ids:
        return []
    fetch_params = {**common, "db": "pubmed", "id": ",".join(ids), "retmode": "xml"}
    response = session.get(f"{base}/efetch.fcgi", params=fetch_params, timeout=timeout)
    response.raise_for_status()
    root = ET.fromstring(response.content)
    papers: list[Paper] = []
    for record in root.findall(".//PubmedArticle"):
        article = record.find(".//Article")
        if article is None:
            continue
        title = xml_text(article.find("ArticleTitle"))
        if not title:
            continue
        authors: list[str] = []
        for author in article.findall(".//AuthorList/Author"):
            collective = xml_text(author.find("CollectiveName"))
            name = collective or clean_text(
                " ".join(part for part in (xml_text(author.find("ForeName")), xml_text(author.find("LastName"))) if part)
            )
            if name:
                authors.append(name)
        abstract_parts = []
        for part in article.findall(".//Abstract/AbstractText"):
            label = clean_text(part.attrib.get("Label", ""))
            text = xml_text(part)
            if text:
                abstract_parts.append(f"{label}: {text}" if label else text)
        doi = ""
        pmcid = ""
        pmid = xml_text(record.find(".//PMID"))
        for article_id in record.findall(".//ArticleId"):
            id_type = article_id.attrib.get("IdType", "").lower()
            if id_type == "doi":
                doi = normalize_doi(xml_text(article_id))
            elif id_type == "pmc":
                pmcid = xml_text(article_id).upper()
        published = pubmed_publication_date(record)
        papers.append(
            Paper(
                title=title,
                authors=authors,
                journal=xml_text(article.find(".//Journal/Title")),
                year=published.year if published else None,
                publication_date=published,
                doi=doi,
                url=f"https://doi.org/{doi}" if doi else f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
                abstract=" ".join(abstract_parts),
                pmcid=pmcid,
                sources={"PubMed"},
                categories={category},
                matched_keywords={keyword},
            )
        )
    return papers


SOURCE_FETCHERS: dict[str, Callable[..., list[Paper]]] = {
    "crossref": fetch_crossref,
    "pubmed": fetch_pubmed,
    "semantic_scholar": fetch_semantic_scholar,
}


def merge_paper(existing: Paper, incoming: Paper) -> None:
    existing.categories.update(incoming.categories)
    existing.sources.update(incoming.sources)
    existing.matched_keywords.update(incoming.matched_keywords)
    if len(incoming.abstract) > len(existing.abstract):
        existing.abstract = incoming.abstract
    if len(incoming.authors) > len(existing.authors):
        existing.authors = incoming.authors
    if len(incoming.full_text) > len(existing.full_text):
        existing.full_text = incoming.full_text
    for attribute in ("journal", "doi", "url", "publication_date", "year", "pmcid"):
        if not getattr(existing, attribute) and getattr(incoming, attribute):
            setattr(existing, attribute, getattr(incoming, attribute))


def extract_pmc_body(root: ET.Element, max_characters: int) -> str:
    """从 PMC JATS XML 中提取正文段落，保留章节名并排除参考文献。"""
    body = root.find(".//body")
    if body is None:
        return ""
    blocks: list[str] = []

    def append_section(section: ET.Element) -> None:
        title = xml_text(section.find("./title"))
        paragraphs = [xml_text(node) for node in section.findall("./p")]
        paragraphs = [paragraph for paragraph in paragraphs if paragraph]
        if title or paragraphs:
            block = "\n".join(([f"## {title}"] if title else []) + paragraphs)
            blocks.append(block)
        for child in section.findall("./sec"):
            append_section(child)

    direct_paragraphs = [xml_text(node) for node in body.findall("./p")]
    blocks.extend(paragraph for paragraph in direct_paragraphs if paragraph)
    for section in body.findall("./sec"):
        append_section(section)
    text = "\n\n".join(blocks).strip()
    return text[:max_characters].rstrip() if max_characters > 0 else text


def enrich_pmc_full_texts(
    papers: list[Paper],
    session: requests.Session,
    config: dict[str, Any],
) -> list[str]:
    """为带 PMCID 的论文补充开放获取全文；失败时保留摘要并继续。"""
    full_text_config = config.get("full_text", {})
    if not full_text_config.get("enabled", True):
        return []
    candidates = [paper for paper in papers if paper.pmcid and not paper.full_text]
    max_papers = int(full_text_config.get("max_papers_per_report", 30))
    max_characters = int(full_text_config.get("max_characters_per_paper", 30000))
    timeout = float(config.get("report", {}).get("request_timeout_seconds", 30))
    delay = float(config.get("report", {}).get("request_delay_seconds", 0.25))
    warnings: list[str] = []
    for paper in candidates[:max_papers]:
        try:
            response = session.get(
                "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi",
                params={"db": "pmc", "id": paper.pmcid, "retmode": "xml"},
                headers={"Accept": "application/xml"},
                timeout=timeout,
            )
            response.raise_for_status()
            paper.full_text = extract_pmc_body(ET.fromstring(response.content), max_characters)
            if not paper.full_text:
                warnings.append(f"{paper.pmcid} 未提取到可用正文，改用摘要总结")
        except (requests.RequestException, ET.ParseError, ValueError) as exc:
            warnings.append(f"{paper.pmcid} 全文获取失败，改用摘要总结：{clean_text(exc)}")
        if delay:
            time.sleep(delay)
    if len(candidates) > max_papers:
        warnings.append(
            f"本周有 {len(candidates)} 篇 PMC 全文候选，仅按配置读取前 {max_papers} 篇；其余使用摘要总结"
        )
    return warnings


def add_text_categories(paper: Paper, config: dict[str, Any]) -> None:
    haystack = f"{paper.title} {paper.abstract}".lower()
    category_terms: dict[str, list[str]] = {
        category: list(keywords or []) for category, keywords in config["categories"].items()
    }
    for category, terms in config.get("classification_terms", {}).items():
        category_terms.setdefault(category, []).extend(terms or [])
    for category, keywords in category_terms.items():
        if any(str(keyword).lower() in haystack for keyword in keywords or []):
            paper.categories.add(category)


def is_tea_relevant(paper: Paper) -> bool:
    """避免将医学中的 SSRI 或其他物种的普通 SSR 论文混入茶叶周报。"""
    original_text = f"{paper.title} {paper.abstract} {paper.journal}"
    text = original_text.lower()
    tea_patterns = (
        "camellia sinensis",
        "tea plant",
        "tea leaf",
        "tea leaves",
        "茶树",
        "茶叶",
    )
    # 只接受 Tea/tea，不接受西班牙语医学文献常用的全大写自闭症缩写 TEA。
    has_tea_signal = any(pattern in text for pattern in tea_patterns) or bool(
        re.search(r"\b(?:Tea|tea)\b", original_text)
    )
    if not has_tea_signal:
        return False
    # 课题聚焦茶树、育种和茶叶品质，排除仅把茶提取物用于临床、纳米材料或药物递送的论文。
    off_topic_terms = (
        "cancer", "apoptosis", "clinical", "patient", "nanoparticle", "photocatalytic",
        "gastrointestinal", "probiotic", "drug delivery", "tumor", "a549 cells",
    )
    plant_or_quality_terms = (
        "tea plant", "leaf", "leaves", "shoot", "root", "cultivar", "germplasm",
        "breeding", "genom", "transcriptom", "metabolom", "proteom", "gene ",
        "flavor", "aroma", "quality", "withering", "rolling", "fermentation",
        "stress", "resistance", "pest", "flowering", "yield", "photosynth",
        "茶树", "茶叶", "品质", "育种", "种质", "代谢", "基因",
    )
    if any(term in text for term in off_topic_terms) and not any(
        term in text for term in plant_or_quality_terms
    ):
        return False
    return True


def keyword_in_paper(paper: Paper, keyword: str) -> bool:
    """确认模糊搜索返回的记录确实包含当前关键词。"""
    text = f"{paper.title} {paper.abstract}".lower()
    normalized = keyword.strip().lower()
    if not normalized:
        return False
    if normalized.isascii() and len(normalized) <= 4:
        return bool(re.search(rf"\b{re.escape(normalized)}\b", text))
    return normalized in text


def collect_papers(
    config: dict[str, Any], session: requests.Session, start: date, end: date
) -> tuple[list[Paper], list[str]]:
    report_config = config.get("report", {})
    timeout = float(report_config.get("request_timeout_sec…4277 tokens truncated…: (
                    "你是一名严谨的茶学文献分析助手。只能根据用户提供的论文内容作答；"
                    "证据不足时必须明确说明，不得编造。"
                ),
            },
            {"role": "user", "content": openai_summary_prompt(paper, max_input)},
        ],
        text={"format": OPENAI_SUMMARY_SCHEMA},
        max_output_tokens=int(openai_config.get("max_output_tokens", 1400)),
    )
    raw = str(response.output_text or "").strip()
    if not raw:
        raise ValueError("OpenAI 返回了空结果")
    data = json.loads(raw)
    required = ("study", "methods", "conclusion", "inspiration", "recommendation")
    if any(not isinstance(data.get(key), str) or not data[key].strip() for key in required):
        raise ValueError("OpenAI 返回的总结字段不完整")
    basis = "OpenAI 深度总结（PMC 公开全文）" if paper.full_text else "OpenAI 总结（题名和摘要）"
    study = data["study"].strip()
    if paper.full_text and "PMC" not in study[:30] and "全文" not in study[:30]:
        study = f"基于 PMC 开放获取全文，{study}"
    elif not paper.full_text and "题名和摘要" not in study[:40]:
        study = f"基于题名和摘要，{study}"
    return PaperSummary(
        study=study,
        methods=data["methods"].strip(),
        conclusion=data["conclusion"].strip(),
        inspiration=data["inspiration"].strip(),
        recommendation=data["recommendation"].strip(),
        basis=basis,
    )


def summarize_papers(
    papers: list[Paper], end: date, config: dict[str, Any]
) -> tuple[dict[str, PaperSummary], list[str]]:
    """批量总结；单篇 API 失败不会阻断整份周报。"""
    summaries = {paper.identity: summarize_paper(paper, end, config) for paper in papers}
    if not papers:
        return summaries, []
    openai_config = config.get("openai", {})
    if not openai_config.get("enabled", True):
        return summaries, ["OpenAI 总结已在 config.yaml 中关闭，本次使用规则兜底"]
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        return summaries, ["未配置 OPENAI_API_KEY，本次使用规则兜底总结"]
    if OpenAI is None:
        return summaries, ["未安装 openai Python 包，本次使用规则兜底总结"]

    model = os.getenv("OPENAI_MODEL", "").strip() or str(
        openai_config.get("model", "gpt-5.4-mini")
    )
    client = OpenAI(
        api_key=api_key,
        timeout=float(openai_config.get("request_timeout_seconds", 120)),
        max_retries=2,
    )
    warnings: list[str] = []
    max_papers = int(openai_config.get("max_papers_per_report", 30))
    delay = float(openai_config.get("request_delay_seconds", 0.2))
    eligible = [paper for paper in papers if paper.abstract or paper.full_text]
    for paper in eligible[:max_papers]:
        try:
            summaries[paper.identity] = summarize_paper_with_openai(
                paper, client, model, openai_config
            )
        except Exception as exc:  # SDK/API/结构异常均按单篇降级，保证周报可生成
            warnings.append(f"OpenAI 总结《{paper.title}》失败，已使用规则兜底：{clean_text(exc)}")
        if delay:
            time.sleep(delay)
    if len(eligible) > max_papers:
        warnings.append(
            f"本周有 {len(eligible)} 篇可总结论文，仅按配置调用 OpenAI 处理前 {max_papers} 篇；其余使用规则兜底"
        )
    return summaries, warnings


def suggest_tracking_keywords(papers: list[Paper]) -> list[str]:
    corpus = " ".join(paper_text(paper) for paper in papers)
    scored = [
        (sum(corpus.count(term) for term in terms), keyword, index)
        for index, (keyword, terms) in enumerate(TRACKING_SUGGESTIONS)
    ]
    scored.sort(key=lambda item: (-item[0], item[2]))
    return [keyword for _, keyword, _ in scored[:6]]


def md_escape(value: str) -> str:
    return clean_text(value).replace("|", "\\|")


def truncate(value: str, limit: int) -> str:
    value = clean_text(value)
    if len(value) <= limit:
        return value
    return value[: max(1, limit - 12)].rstrip() + "……（已截断）"


def anchor_for(paper: Paper) -> str:
    return "paper-" + hashlib.sha1(paper.identity.encode("utf-8")).hexdigest()[:10]


def primary_category(paper: Paper, config: dict[str, Any]) -> str:
    categories = list(config["categories"])
    # 方法/组学方向优先于宽泛主题；用户也可在 config.yaml 中调整该顺序。
    ordered = [
        category for category in config.get("classification_priority", []) if category in categories
    ]
    ordered += [category for category in categories if category not in ordered and category != "茶树综合研究"]
    ordered += [category for category in categories if category == "茶树综合研究"]
    for category in ordered:
        if category in paper.categories:
            return category
    return "其他相关研究"


def format_authors(authors: list[str], maximum: int) -> str:
    if not authors:
        return "数据源未提供"
    if len(authors) <= maximum:
        return "; ".join(authors)
    return "; ".join(authors[:maximum]) + f" 等（共 {len(authors)} 位作者）"


def generate_report_legacy(
    papers: list[Paper], warnings: list[str], config: dict[str, Any], start: date, end: date
) -> str:
    report_config = config.get("report", {})
    top_n = int(report_config.get("top_papers", 8))
    max_abstract = int(report_config.get("max_abstract_characters", 1800))
    max_authors = int(report_config.get("max_authors", 12))
    ranked = sorted(papers, key=lambda paper: paper_score(paper, end, config), reverse=True)
    grouped: dict[str, list[Paper]] = {category: [] for category in config["categories"]}
    grouped["其他相关研究"] = []
    for paper in papers:
        grouped.setdefault(primary_category(paper, config), []).append(paper)
    category_counts = {category: len(items) for category, items in grouped.items() if items}
    source_counts = Counter(source for paper in papers for source in paper.sources)
    journal_counts = Counter(paper.journal for paper in papers if paper.journal)

    lines = [
        f"# 茶叶方向每周学术进展周报（{end.isoformat()}）",
        "",
        f"> 检索时间范围：**{start.isoformat()} 至 {end.isoformat()}**（最近 {(end - start).days + 1} 天）  ",
        f"> 共去重获得 **{len(papers)}** 篇相关论文。报告由公开学术接口自动生成，请在引用前核对论文原文。",
        "",
        "## 本周茶叶方向学术进展概览",
        "",
    ]
    if papers:
        category_summary = "、".join(f"{name} {count} 篇" for name, count in category_counts.items())
        source_summary = "、".join(f"{name} {count} 条记录" for name, count in source_counts.most_common())
        top_journals = "、".join(name for name, _ in journal_counts.most_common(5)) or "期刊信息不足"
        lines += [
            f"本周检索结果主要分布为：{category_summary or '尚未形成明确方向分布'}。",
            "",
            f"- 数据来源：{source_summary}",
            f"- 记录较多的期刊：{top_journals}",
            "- 阅读建议：优先查看下方“值得重点关注的论文”，再按自己的课题方向进入分类列表精读。",
        ]
    else:
        lines += [
            "本周在设定的时间范围和关键词下暂未检索到可确认的论文记录。",
            "这不等于本周没有相关研究，可能与数据库收录延迟、在线发表日期或接口限流有关。",
        ]

    lines += ["", "## 值得重点关注的论文", ""]
    if ranked:
        for index, paper in enumerate(ranked[:top_n], 1):
            category = primary_category(paper, config)
            reason = "；".join(inspiration_for(paper)[:1])
            lines.append(f"{index}. [{md_escape(paper.title)}](#{anchor_for(paper)})（{category}）")
            lines.append(f"   - 关注理由：{reason}")
    else:
        lines.append("暂无可推荐论文。")

    lines += ["", "## 按方向分类的论文列表", ""]
    for category, items in grouped.items():
        if not items:
            continue
        lines += [f"###{' '}{category}（{len(items)} 篇）", ""]
        items.sort(key=lambda paper: (paper.publication_date or date.min, paper.title), reverse=True)
        for paper in items:
            doi_display = paper.doi or "数据源未提供"
            pub_display = paper.publication_date.isoformat() if paper.publication_date else str(paper.year or "数据源未提供")
            link_display = paper.url or (f"https://doi.org/{paper.doi}" if paper.doi else "数据源未提供")
            abstract = truncate(paper.abstract, max_abstract) or "数据源未提供摘要"
            lines += [
                f'<a id="{anchor_for(paper)}"></a>',
                f"#### {md_escape(paper.title)}",
                "",
                f"- **作者：** {md_escape(format_authors(paper.authors, max_authors))}",
                f"- **期刊：** {md_escape(paper.journal or '数据源未提供')}",
                f"- **发表时间：** {pub_display}",
                f"- **DOI：** {md_escape(doi_display)}",
                f"- **链接：** {link_display}",
                f"- **数据来源：** {', '.join(sorted(paper.sources))}",
                f"- **命中关键词：** {md_escape('、'.join(sorted(paper.matched_keywords)))}",
                f"- **相关方向：** {md_escape('、'.join(sorted(paper.categories)))}",
                "- **摘要：**",
                "",
                "> " + abstract.replace("\n", "\n> "),
                "",
                "- **对硕士课题可能有启发的地方：**",
            ]
            lines.extend(f"  - {item}" for item in inspiration_for(paper))
            lines.append("")

    lines += ["## 检索与使用说明", ""]
    if warnings:
        lines.append(f"本次共有 {len(warnings)} 条接口提示。常见原因是免费接口临时限流，不影响其他数据源继续生成报告。")
        for warning in warnings[:10]:
            lines.append(f"- {md_escape(warning)}")
        if len(warnings) > 10:
            lines.append(f"- 其余 {len(warnings) - 10} 条提示已省略，请查看 GitHub Actions 运行日志。")
    else:
        lines.append("本次检索未记录接口错误。")
    lines += [
        "",
        "### 重要提醒",
        "",
        "- 数据库可能存在收录延迟；年份、卷期、DOI 和摘要应以出版社页面为准。",
        "- “重点关注”和“课题启发”是规则化辅助判断，不替代导师意见和全文精读。",
        "- 自动周报适合做信息雷达，不建议直接将其中的机器生成文字用于论文写作。",
        "",
    ]
    return "\n".join(lines)


def generate_report(
    papers: list[Paper],
    warnings: list[str],
    config: dict[str, Any],
    start: date,
    end: date,
    summaries: dict[str, PaperSummary] | None = None,
) -> str:
    """生成以中文摘要解读为核心的周报。"""
    ranked = sorted(papers, key=lambda paper: paper_score(paper, end, config), reverse=True)
    summaries = summaries or {
        paper.identity: summarize_paper(paper, end, config) for paper in papers
    }
    grouped: dict[str, list[Paper]] = {category: [] for category in config["categories"]}
    grouped["其他相关研究"] = []
    for paper in papers:
        grouped.setdefault(primary_category(paper, config), []).append(paper)

    category_counts = {category: len(items) for category, items in grouped.items() if items}
    source_counts = Counter(source for paper in papers for source in paper.sources)
    journal_counts = Counter(paper.journal for paper in papers if paper.journal)
    method_counts = Counter(method for paper in papers for method in detect_methods(paper))
    featured_count = min(5, len(ranked))
    basis_counts = Counter(summary.basis for summary in summaries.values())
    basis_summary = "、".join(
        f"{basis} {count} 篇" for basis, count in basis_counts.most_common()
    ) or "本周无论文"

    lines = [
        f"# 茶叶方向每周学术进展周报（{end.isoformat()}）",
        "",
        f"> 检索时间范围：**{start.isoformat()} 至 {end.isoformat()}**（最近 {(end - start).days + 1} 天）  ",
        f"> 共去重获得 **{len(papers)}** 篇相关论文。总结依据：{basis_summary}。  ",
        "> OpenAI 仅分析程序实际取得的 PMC 公开全文或摘要；无法取得的内容不会被补写。引用或调整试验方案前请核对原文。",
        "",
        "## 本周总体趋势",
        "",
    ]
    if papers:
        category_summary = "、".join(f"{name} {count} 篇" for name, count in category_counts.items())
        source_summary = "、".join(f"{name} {count} 条记录" for name, count in source_counts.most_common())
        top_journals = "、".join(name for name, _ in journal_counts.most_common(5)) or "期刊信息不足"
        top_methods = "、".join(name for name, _ in method_counts.most_common(5)) or "摘要中方法信息较少"
        lines += [
            f"本周检索结果主要分布为：{category_summary or '尚未形成明确方向分布'}。",
            "",
            f"- 数据来源：{source_summary}",
            f"- 高频研究方法：{top_methods}",
            f"- 记录较多的期刊：{top_journals}",
        ]
    else:
        lines += [
            "本周在设定的时间范围和关键词下暂未检索到可确认的论文记录。",
            "这不等于本周没有相关研究，可能与数据库收录延迟、在线发表日期或接口限流有关。",
        ]

    lines += ["", f"## 本周最值得关注的 {featured_count if featured_count else '3–5'} 篇论文", ""]
    if ranked:
        for index, paper in enumerate(ranked[:featured_count], 1):
            category = primary_category(paper, config)
            summary = summaries[paper.identity]
            lines.append(f"{index}. [{md_escape(paper.title)}](#{anchor_for(paper)})（{category}）")
            lines.append(f"   - 研究概括：{summary.study}")
            lines.append(f"   - 推荐等级：**{summary.recommendation}**；关注理由：{summary.inspiration}")
    else:
        lines.append("暂无可推荐论文。")

    lines += ["", "## 与茶叶方向硕士研究最相关的主题", ""]
    if category_counts:
        theme_notes = {
            "茶树综合研究": "适合把握茶树生物学、抗逆和调控研究的整体进展",
            "茶叶品质": "适合筛选品质表型、加工处理和感官评价指标",
            "茶树育种与种质资源": "适合关注材料评价、亲本选择和核心种质构建",
            "茶树代谢组": "适合建立代谢物、通路与品质性状之间的联系",
            "茶树基因组": "适合追踪候选基因、变异位点和多组学验证策略",
            "SSR与分子标记": "适合开展遗传多样性、指纹图谱和材料鉴定",
        }
        for category, count in sorted(category_counts.items(), key=lambda item: item[1], reverse=True)[:5]:
            note = theme_notes.get(category, "可结合自己的课题方向选择性精读")
            lines.append(f"- **{category}（{count} 篇）**：{note}。")
    else:
        lines.append("本周无论文记录，暂时无法判断主题分布。")

    lines += ["", "## 下周建议继续追踪的关键词", ""]
    lines.extend(f"- `{keyword}`" for keyword in suggest_tracking_keywords(papers))

    lines += ["", "## 按方向分类的论文", ""]
    for category, items in grouped.items():
        if not items:
            continue
        lines += [f"## 方向：{category}（{len(items)} 篇）", ""]
        items.sort(key=lambda paper: (paper.publication_date or date.min, paper.title), reverse=True)
        for paper in items:
            doi_display = paper.doi or "数据源未提供"
            year_display = str(paper.year or (paper.publication_date.year if paper.publication_date else "数据源未提供"))
            link_display = paper.url or (f"https://doi.org/{paper.doi}" if paper.doi else "数据源未提供")
            summary = summaries[paper.identity]
            lines += [
                f'<a id="{anchor_for(paper)}"></a>',
                f"### {md_escape(paper.title)}",
                "",
                f"- **期刊/年份：** {md_escape(paper.journal or '数据源未提供')} / {year_display}",
                f"- **DOI：** {md_escape(doi_display)}",
                f"- **原文链接：** {link_display}",
                f"- **研究内容概括：** {summary.study}",
                f"- **主要研究方法：** {summary.methods}",
                f"- **核心结论：** {summary.conclusion}",
                f"- **对我的研究启发：** {summary.inspiration}",
                f"- **推荐阅读等级：** {summary.recommendation}",
                "",
            ]

    lines += ["## 检索与使用说明", ""]
    if warnings:
        lines.append(f"本次共有 {len(warnings)} 条接口提示。常见原因是免费接口临时限流，不影响其他数据源继续生成报告。")
        for warning in warnings[:10]:
            lines.append(f"- {md_escape(warning)}")
        if len(warnings) > 10:
            lines.append(f"- 其余 {len(warnings) - 10} 条提示已省略，请查看 GitHub Actions 运行日志。")
    else:
        lines.append("本次检索未记录接口错误。")
    lines += [
        "",
        "### 重要提醒",
        "",
        "- 数据库可能存在收录延迟；年份、卷期、DOI 和摘要应以出版社页面为准。",
        "- OpenAI 总结严格限定于程序取得的公开全文或摘要；API 不可用时自动切换为保守规则总结。",
        "- 报告中的“PMC 公开全文”表示程序分析了开放获取正文；“题名和摘要”不等于全文分析。",
        "- “推荐等级”和“研究启发”用于筛选阅读顺序，不替代导师意见和全文精读。",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    args = parse_args()
    try:
        config = load_config(args.config)
        end = datetime.strptime(args.date, "%Y-%m-%d").date() if args.date else datetime.now(ZoneInfo("Asia/Shanghai")).date()
        days = int(config.get("report", {}).get("lookback_days", 7))
        if days < 1:
            raise ValueError("report.lookback_days 必须大于或等于 1")
        start = end - timedelta(days=days - 1)
        session = build_session(config)
        papers, warnings = collect_papers(config, session, start, end)
        warnings.extend(enrich_pmc_full_texts(papers, session, config))
        summaries, summary_warnings = summarize_papers(papers, end, config)
        warnings.extend(summary_warnings)
        report = generate_report(papers, warnings, config, start, end, summaries)
        if args.dry_run:
            print(report)
            return 0
        output_dir = Path(args.output_dir or config.get("report", {}).get("output_dir", "reports"))
        output_dir.mkdir(parents=True, exist_ok=True)
        prefix = config.get("report", {}).get("filename_prefix", "weekly_report_")
        output_path = output_dir / f"{prefix}{end.isoformat()}.md"
        output_path.write_text(report, encoding="utf-8")
        LOGGER.info("周报已保存：%s（%d 篇论文）", output_path, len(papers))
        return 0
    except Exception as exc:  # GitHub Actions 需要清晰、非零的失败状态
        LOGGER.exception("程序运行失败：%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
