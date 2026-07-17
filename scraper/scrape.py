#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
自治体・関連団体の公募/入札情報ページを巡回し、
「展示会・商談会の開催運営業務」に関連しそうな公募案件を抽出して
data/listings.json を更新するスクリプト。

設計方針:
- サイトごとに専用パーサーを作り込むのは対象数(50超)が多いため現実的でなく、
  汎用ロジック(リンク+その周辺テキストをキーワードでフィルタ)で運用する。
  誤検知/検知漏れが一定発生する前提で、config/organizations.json の note に
  各サイトの癖を記録し、必要に応じて個別調整する。
- GitHub Actions から日次実行され、data/listings.json (履歴保持) と
  data/scrape_log.json (収集ステータス) を更新してコミットする想定。
"""

import json
import re
import sys
import time
import hashlib
import datetime
import pathlib
import urllib.parse

import requests
from bs4 import BeautifulSoup

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "organizations.json"
DATA_DIR = ROOT / "data"
LISTINGS_PATH = DATA_DIR / "listings.json"
LOG_PATH = DATA_DIR / "scrape_log.json"

USER_AGENT = (
    "Mozilla/5.0 (compatible; KoboDashboardBot/1.0; "
    "+https://github.com/) research/internal-use"
)
TIMEOUT = 25
RETRY = 2
SLEEP_BETWEEN_REQUESTS = 1.5  # 相手サーバへの配慮

# 「展示会・商談会」自体を指すキーワード
KEYWORDS_TOPIC = [
    "展示", "商談", "見本市", "物産展", "産業展",
]

# 「運営・委託・公募」であることを示すキーワード
KEYWORDS_OPERATION = [
    "運営", "開催", "委託", "事務局", "実施", "企画運営", "企画競争",
    "プロポーザル", "公募", "業務委託", "請負", "受託者", "事業者募集",
]

TEXT_TAGS = ["li", "tr", "p", "div", "dd", "article"]


def load_config():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = json.load(f)
    return [o for o in cfg["organizations"] if o.get("active", True)]


def load_json(path, default):
    if path.exists():
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except json.JSONDecodeError:
            return default
    return default


def make_item_id(org_id, url, title):
    raw = f"{org_id}|{url}|{title}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def matches_keywords(text):
    if not text:
        return []
    hit_topic = [k for k in KEYWORDS_TOPIC if k in text]
    hit_op = [k for k in KEYWORDS_OPERATION if k in text]
    if hit_topic and hit_op:
        return hit_topic + hit_op
    return []


def fetch(url):
    last_err = None
    for attempt in range(RETRY + 1):
        try:
            resp = requests.get(
                url,
                headers={"User-Agent": USER_AGENT},
                timeout=TIMEOUT,
            )
            resp.raise_for_status()
            # 文字コード自動判定に失敗するサイト対策
            if resp.encoding is None or resp.encoding.lower() == "iso-8859-1":
                resp.encoding = resp.apparent_encoding
            return resp.text
        except requests.RequestException as e:
            last_err = e
            time.sleep(1)
    raise last_err


def extract_candidates(html, base_url):
    """ページ内のリンクとその周辺テキストから候補を抽出する。"""
    soup = BeautifulSoup(html, "lxml")
    candidates = []
    seen_local = set()

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith("javascript:") or href.startswith("#"):
            continue
        abs_url = urllib.parse.urljoin(base_url, href)

        link_text = a.get_text(" ", strip=True)

        context_el = a.find_parent(TEXT_TAGS)
        context_text = context_el.get_text(" ", strip=True) if context_el else link_text

        # 周辺テキストが長すぎる場合(レイアウト用divなど)はリンクテキストのみで判定
        match_text = context_text if len(context_text) <= 300 else link_text

        matched = matches_keywords(match_text)
        if not matched:
            continue

        title = link_text if link_text else context_text[:80]
        if not title:
            continue

        dedup_key = (abs_url, title)
        if dedup_key in seen_local:
            continue
        seen_local.add(dedup_key)

        candidates.append(
            {
                "url": abs_url,
                "title": title[:200],
                "context": context_text[:400],
                "matched_keywords": sorted(set(matched)),
            }
        )
    return candidates


def run():
    today = datetime.date.today().isoformat()
    now_iso = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

    orgs = load_config()
    existing = load_json(LISTINGS_PATH, {"generated_at": None, "items": []})
    existing_items = {item["id"]: item for item in existing.get("items", [])}

    log_entries = []
    total_new = 0
    total_matched = 0

    for org in orgs:
        org_id = org["id"]
        org_name = org["name"]
        url = org["url"]
        status = "ok"
        error_msg = None
        matched_count = 0

        try:
            html = fetch(url)
            candidates = extract_candidates(html, url)
            matched_count = len(candidates)
            total_matched += matched_count

            for c in candidates:
                item_id = make_item_id(org_id, c["url"], c["title"])
                if item_id in existing_items:
                    existing_items[item_id]["last_seen"] = today
                    existing_items[item_id]["title"] = c["title"]
                    existing_items[item_id]["context"] = c["context"]
                else:
                    existing_items[item_id] = {
                        "id": item_id,
                        "org_id": org_id,
                        "org_name": org_name,
                        "org_category": org.get("category", "prefecture"),
                        "title": c["title"],
                        "context": c["context"],
                        "url": c["url"],
                        "source_page": url,
                        "matched_keywords": c["matched_keywords"],
                        "first_seen": today,
                        "last_seen": today,
                    }
                    total_new += 1

        except Exception as e:  # noqa: BLE001 - 収集継続を優先
            status = "error"
            error_msg = str(e)[:300]

        log_entries.append(
            {
                "org_id": org_id,
                "org_name": org_name,
                "url": url,
                "status": status,
                "error": error_msg,
                "matched_count": matched_count,
                "checked_at": now_iso,
            }
        )

        time.sleep(SLEEP_BETWEEN_REQUESTS)

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    all_items = sorted(
        existing_items.values(),
        key=lambda x: (x["first_seen"], x["org_name"]),
        reverse=True,
    )

    with open(LISTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(
            {"generated_at": now_iso, "items": all_items},
            f,
            ensure_ascii=False,
            indent=2,
        )

    error_count = sum(1 for e in log_entries if e["status"] == "error")
    with open(LOG_PATH, "w", encoding="utf-8") as f:
        json.dump(
            {
                "generated_at": now_iso,
                "org_count": len(orgs),
                "error_count": error_count,
                "new_items_today": total_new,
                "matched_today": total_matched,
                "entries": log_entries,
            },
            f,
            ensure_ascii=False,
            indent=2,
        )

    print(
        f"[{now_iso}] orgs={len(orgs)} errors={error_count} "
        f"matched_today={total_matched} new_items={total_new} "
        f"total_items={len(all_items)}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(run())
