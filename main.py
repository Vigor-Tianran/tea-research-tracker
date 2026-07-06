#!/usr/bin/env python3
"""茶叶方向每周学术进展追踪程序。

数据源均为公开学术接口：Crossref、PubMed E-utilities 和 Semantic Scholar。
程序不使用大模型；“重点关注”和“课题启发”由透明的关键词与元数据规则生成。
"""

from __future__ import annotations

import argparse
import hashlib
import html
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
        pmid = xml_text(record.find(".//PMID"))
        for article_id in record.findall(".//ArticleId"):
            if article_id.attrib.get("IdType", "").lower() == "doi":
                doi = normalize_doi(xml_text(article_id))
                break
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
    for attribute in ("journal", "doi", "url", "publication_date", "year"):
        if not getattr(existing, attribute) and getattr(incoming, attribute):
            setattr(existing, attribute, getattr(incoming, attribute))


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
    timeout = float(report_config.get("request_timeout_seconds", 30))
    delay = float(report_config.get("request_delay_seconds", 0.25))
    contact_email = os.getenv("CONTACT_EMAIL") or config.get("contact_email", "")
    all_papers: dict[str, Paper] = {}
    warnings: list[str] = []
    keyword_pairs = flatten_keywords(config)
    for source_name, source_config in config.get("sources", {}).items():
        if not source_config.get("enabled", False):
            continue
        fetcher = SOURCE_FETCHERS.get(source_name)
        if not fetcher:
            warnings.append(f"未知数据源 `{source_name}`，已跳过")
            continue
        LOGGER.info("开始检索数据源：%s", source_name)
        for category, keyword in keyword_pairs:
            try:
                found = fetcher(
                    session, keyword, category, start, end, source_config, timeout, contact_email
                )
                LOGGER.info("%s | %s | 找到 %d 篇", source_name, keyword, len(found))
                for paper in found:
                    if (
                        not paper.title
                        or not is_tea_relevant(paper)
                        or not keyword_in_paper(paper, keyword)
                    ):
                        continue
                    add_text_categories(paper, config)
                    if paper.identity in all_papers:
                        merge_paper(all_papers[paper.identity], paper)
                    else:
                        all_papers[paper.identity] = paper
            except (requests.RequestException, ValueError, ET.ParseError) as exc:
                message = f"{source_name} 检索“{keyword}”失败：{clean_text(exc)}"
                warnings.append(message)
                LOGGER.warning(message)
                if source_name == "semantic_scholar" and not os.getenv("S2_API_KEY", "").strip():
                    warnings.append("Semantic Scholar 匿名接口已限流，本次运行跳过其余关键词；Crossref 和 PubMed 将继续检索。")
                    break
            if delay:
                time.sleep(delay)
    papers = list(all_papers.values())
    papers.sort(key=lambda paper: (paper.publication_date or date.min, paper.title), reverse=True)
    return papers, warnings


def paper_score(paper: Paper, end: date, config: dict[str, Any]) -> int:
    score = 0
    if paper.publication_date:
        score += max(0, 4 - max(0, (end - paper.publication_date).days) // 2)
    if paper.abstract:
        score += 2
    if paper.doi:
        score += 1
    score += min(len(paper.categories), 3)
    text = f"{paper.title} {paper.abstract}".lower()
    for term in config.get("priority_terms", []):
        if str(term).lower() in text:
            score += 2
    return score


def inspiration_for(paper: Paper) -> list[str]:
    suggestions: list[str] = []
    category_text = " ".join(paper.categories)
    if "育种" in category_text or "种质" in category_text:
        suggestions.append("可借鉴其材料选择、表型评价或遗传多样性分析框架，用于优化种质资源筛选与育种亲本选择。")
    if "基因组" in category_text:
        suggestions.append("可重点关注候选基因、变异位点与性状关联方法，并评估其是否能用于你的茶树材料验证。")
    if "代谢组" in category_text:
        suggestions.append("可参考其代谢物提取、质谱分析和差异代谢物筛选流程，建立品质性状与代谢通路之间的联系。")
    if "品质" in category_text:
        suggestions.append("可比较其品质指标、感官评价或环境处理设计，作为你选择核心表型和对照条件的依据。")
    if "SSR" in category_text or "分子标记" in category_text:
        suggestions.append("可核对引物设计、位点多态性与群体鉴定指标，为SSR标记开发或种质指纹构建提供参数参考。")
    if not suggestions:
        suggestions.append("可从研究对象、试验设计和统计方法三个层面与自己的课题对照，判断能否形成可复现的小规模验证实验。")
    if paper.abstract:
        suggestions.append("建议先通读摘要与方法部分，记录其样本量、关键技术和主要结论，再决定是否下载全文精读。")
    return suggestions[:2]


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


def generate_report(
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
        report = generate_report(papers, warnings, config, start, end)
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
