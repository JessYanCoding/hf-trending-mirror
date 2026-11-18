#!/usr/bin/env python3
"""
抓取 github.com/trending（实时 HTML 页面）并输出为 JSON，供 Actions 写入 issue body。

为什么要单独做这个镜像：
    下游的云端定时任务所在环境访问 github.com/trending 会被代理拦截（403）
    或拿到冻结的缓存页面，而 GitHub Actions runner 拥有完整外网，能拿到实时数据。
    与 HF trending 的做法一致：runner 抓取 -> 写入固定 issue -> 下游读取 issue。

GitHub 没有官方的 trending API，所以只能解析 HTML。页面结构一旦改版，解析可能
失效；脚本在解析结果为空或明显异常时会直接失败（非 0 退出），宁可让 Action 报红，
也不要用空数据覆盖 issue 里上一次的有效数据。

环境变量：
    TRENDING_SINCE      统计窗口：daily / weekly / monthly，默认 daily
    TRENDING_LANGUAGE   语言过滤（如 python、rust），默认空 = 全部语言
    README_MAX_CHARS    每个仓库 README 截取的最大字符数，默认 1500
    GITHUB_TOKEN        可选；用于调用 REST API 拉 README，避免匿名 60 次/小时的限流
    OUTPUT_PATH         输出文件路径，默认 github_trending.json
"""

import datetime
import json
import os
import re
import sys
from typing import Optional

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

TRENDING_BASE_URL = "https://github.com/trending"
GITHUB_API_BASE_URL = "https://api.github.com"
REQUEST_TIMEOUT_SECONDS = 30
VALID_SINCE_VALUES = ("daily", "weekly", "monthly")
# trending 页面正常固定返回 25 个仓库；少于该阈值大概率是页面改版或被限流返回了异常页
MIN_EXPECTED_REPO_COUNT = 10
# 使用普通浏览器 UA：GitHub 对无 UA 或脚本 UA 的请求可能返回精简页面
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def build_http_session() -> requests.Session:
    """
    构建带重试策略的 HTTP Session。

    对 429 / 5xx 做指数退避重试，避免 GitHub 偶发抖动导致整次任务失败；
    respect_retry_after_header 让 429 时遵守服务端给出的 Retry-After。
    """
    retry_strategy = Retry(
        total=4,
        backoff_factor=2,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
        respect_retry_after_header=True,
    )
    session = requests.Session()
    session.mount("https://", HTTPAdapter(max_retries=retry_strategy))
    return session


def parse_int(text: Optional[str]) -> Optional[int]:
    """把 '12,345' / '1,234 stars today' 这类文本中的第一个数字解析为 int，失败返回 None。"""
    if not text:
        return None
    match = re.search(r"\d[\d,]*", text)
    if not match:
        return None
    return int(match.group(0).replace(",", ""))


def fetch_trending_html(session: requests.Session, since: str, language: str) -> str:
    """
    拉取 trending 页面 HTML。

    强制 Accept-Language 为英文，保证 'stars today' 等文案可预期，
    不会因 runner 区域设置不同而变成其他语言。
    """
    url = TRENDING_BASE_URL
    if language:
        url = "/".join([TRENDING_BASE_URL, language])
    response = session.get(
        url,
        params={"since": since},
        headers={
            "User-Agent": BROWSER_USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-US,en;q=0.9",
            # 显式要求不使用中间缓存，防止拿到旧页面
            "Cache-Control": "no-cache",
        },
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    return response.text


def parse_trending_repos(html: str) -> list:
    """
    解析 trending 页面中的仓库列表。

    页面结构（截至 2026 年）：每个仓库是一个 <article class="Box-row">，其中
      - h2 > a[href="/owner/repo"]                  仓库路径
      - p                                           描述
      - span[itemprop=programmingLanguage]          主语言
      - a[href$="/stargazers"] / a[href$="/forks"]  总 star / fork
      - span.float-sm-right                         'N stars today/this week/this month'
      - span 'Built by' 下的 a > img[alt=@user]      主要贡献者
    每个字段单独做容错：某个字段缺失只置空，不影响整条记录。
    """
    soup = BeautifulSoup(html, "html.parser")
    repos = []
    for rank, article in enumerate(soup.select("article.Box-row"), start=1):
        title_link = article.select_one("h2 a[href]")
        if title_link is None:
            # 没有仓库链接的条目无法识别，直接跳过
            continue
        full_name = title_link["href"].strip("/")
        if full_name.count("/") != 1:
            continue
        owner, name = full_name.split("/")

        description_node = article.select_one("p")
        language_node = article.select_one('span[itemprop="programmingLanguage"]')
        stars_node = article.select_one('a[href$="/stargazers"]')
        forks_node = article.select_one('a[href$="/forks"]')
        period_stars_node = article.select_one("span.float-sm-right")

        built_by = []
        for avatar in article.select("a img.avatar[alt]"):
            login = avatar["alt"].lstrip("@").strip()
            if login:
                built_by.append(login)

        repos.append(
            {
                "rank": rank,
                "full_name": full_name,
                "owner": owner,
                "name": name,
                "url": "".join(["https://github.com/", full_name]),
                "description": " ".join(description_node.get_text().split()) if description_node else "",
                "language": language_node.get_text(strip=True) if language_node else None,
                "stars": parse_int(stars_node.get_text()) if stars_node else None,
                "forks": parse_int(forks_node.get_text()) if forks_node else None,
                "period_stars": parse_int(period_stars_node.get_text()) if period_stars_node else None,
                "period_stars_text": " ".join(period_stars_node.get_text().split()) if period_stars_node else "",
                "built_by": built_by,
                "readme": "",
            }
        )
    return repos


# README 清洗用的正则：预编译，避免 25 个仓库重复编译
HTML_COMMENT_PATTERN = re.compile(r"<!--.*?-->", re.DOTALL)
# 整块删除的标签（含内容）：脚本/样式/图片容器对写简介没有价值
HTML_BLOCK_PATTERN = re.compile(r"<(script|style|picture|svg)\b.*?</\1\s*>", re.DOTALL | re.IGNORECASE)
HTML_TAG_PATTERN = re.compile(r"</?[a-zA-Z][^>]*>")
# 徽章：[![alt](img)](link)，必须先于普通图片处理，否则会残留外层链接
MARKDOWN_BADGE_PATTERN = re.compile(r"\[!\[[^\]]*\]\([^)]*\)\]\([^)]*\)")
MARKDOWN_IMAGE_PATTERN = re.compile(r"!\[[^\]]*\]\([^)]*\)")
MARKDOWN_TABLE_SEPARATOR_PATTERN = re.compile(r"^\s*\|?\s*:?-{3,}.*(?:\n|$)", re.MULTILINE)
MULTI_BLANK_LINE_PATTERN = re.compile(r"\n\s*\n+")
# 拉取原文时多取一些再清洗，保证清洗后仍有足够正文
README_RAW_FETCH_MULTIPLIER = 4


def clean_readme(raw_text: str) -> str:
    """
    清洗 README：去掉 HTML 标签、注释、徽章、图片和表格分隔行，压缩空行。

    为什么要清洗：
      1. 这些内容对写中文简介没有价值，却占用 issue body 的字符预算；
      2. 下游若读 issue 网页版，残留的 HTML 会被浏览器/抓取器当真标签处理，
         破坏 JSON 文本（这是第一次交接失败的直接诱因）。
    保留 <a>/<p> 等标签内的文字，只去标签本身。
    """
    text = HTML_COMMENT_PATTERN.sub("", raw_text)
    text = HTML_BLOCK_PATTERN.sub("", text)
    text = MARKDOWN_BADGE_PATTERN.sub("", text)
    text = MARKDOWN_IMAGE_PATTERN.sub("", text)
    text = HTML_TAG_PATTERN.sub("", text)
    text = MARKDOWN_TABLE_SEPARATOR_PATTERN.sub("", text)
    # 常见 HTML 实体还原为普通字符
    text = text.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    lines = [line.rstrip() for line in text.splitlines()]
    text = MULTI_BLANK_LINE_PATTERN.sub("\n\n", "\n".join(lines))
    return text.strip()


def fetch_readme_excerpt(session: requests.Session, full_name: str, token: str, max_chars: int) -> str:
    """
    通过 REST API 拉取仓库 README 原文，清洗后截取前 max_chars 个字符。

    使用 application/vnd.github.raw 直接拿原文，省去 base64 解码；
    带 token 时限额为 1000 次/小时/仓库（GITHUB_TOKEN），足够 25 个仓库。
    README 不存在（404）属于正常情况，返回空串而不是抛异常。
    """
    headers = {
        "Accept": "application/vnd.github.raw",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "trending-mirror-action",
    }
    if token:
        headers["Authorization"] = " ".join(["Bearer", token])
    response = session.get(
        "/".join([GITHUB_API_BASE_URL, "repos", full_name, "readme"]),
        headers=headers,
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    if response.status_code == 404:
        return ""
    response.raise_for_status()
    # 先多截一段原文再清洗，最后按 max_chars 截断；直接截原文会被大段 HTML 吃掉预算
    raw_excerpt = response.text[: max_chars * README_RAW_FETCH_MULTIPLIER]
    return clean_readme(raw_excerpt)[:max_chars]


def main() -> int:
    since = os.environ.get("TRENDING_SINCE", "daily").strip().lower() or "daily"
    language = os.environ.get("TRENDING_LANGUAGE", "").strip().lower()
    readme_max_chars = int(os.environ.get("README_MAX_CHARS", "1500"))
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    output_path = os.environ.get("OUTPUT_PATH", "github_trending.json")

    if since not in VALID_SINCE_VALUES:
        raise ValueError(f"Invalid TRENDING_SINCE '{since}', expected one of {VALID_SINCE_VALUES}")

    session = build_http_session()

    # 1) 抓取并解析 trending 列表
    html = fetch_trending_html(session, since, language)
    repos = parse_trending_repos(html)
    if len(repos) < MIN_EXPECTED_REPO_COUNT:
        # 解析数量异常：可能是页面改版 / 被限流 / 返回了验证页，直接失败以保留 issue 中的旧数据
        raise RuntimeError(
            f"Parsed only {len(repos)} repos from trending page (expected >= {MIN_EXPECTED_REPO_COUNT}); "
            "page structure may have changed"
        )

    # 2) 逐个补充 README 摘要；单个失败只记录错误，不影响整体
    for repo in repos:
        try:
            repo["readme"] = fetch_readme_excerpt(session, repo["full_name"], token, readme_max_chars)
        except Exception as error:  # noqa: BLE001 - 单仓库失败需要被吞掉并记录
            repo["readme_error"] = str(error)

    payload = {
        "source": TRENDING_BASE_URL,
        "since": since,
        "language": language or None,
        "fetchedAt": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "repos": repos,
    }
    with open(output_path, "w", encoding="utf-8") as output_file:
        json.dump(payload, output_file, ensure_ascii=False, indent=2)

    readme_chars = sum(len(repo["readme"]) for repo in repos)
    print(f"wrote {len(repos)} repos ({since}), {readme_chars} chars of READMEs -> {output_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
