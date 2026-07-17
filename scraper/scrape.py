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


# ---------------------------------------------------------------------------
# サイト別ハンドラ
#
# 一部団体(茨城県・埼玉県・神奈川県・東京都)は電子入札システムがフレーム構成や
# 多段階の検索フォームになっており、単純な GET では案件一覧に到達できない。
# これらの団体は config/organizations.json の "handler" フィールドで下記の
# 関数名を指定し、案件一覧ページの HTML を独自に取得したうえで
# extract_candidates() にそのまま渡す(リンク+周辺テキストの抽出ロジックは
# 共通化し、重複させない)。
#
# これらのシステムは案件詳細への遷移が javascript:doEdit(...) 等の
# フォーム送信で行われ、GETだけで踏める詳細URLが存在しない。そのため
# 案件へのリンクはセッション依存の「ベストエフォート」URLとなり、
# 別セッションのブラウザで直接開くとエラーになる場合がある。
# ---------------------------------------------------------------------------


def _ibaraki_style(base, entry_action, kikan_value, kikan_name,
                    supplytype, menu_case_action, search_action,
                    frame_action, search_extra=None):
    """茨城県の「入札情報公開システム」を発注機関選択〜検索まで操作する。"""
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT})

    r1 = s.get(f"{base}{entry_action}", timeout=TIMEOUT)
    html1 = r1.content.decode("cp932", errors="replace")
    m = re.search(r'action="(/koukai/do/[^"]*)"', html1)
    action_path = m.group(1) if m else entry_action
    kk_action = action_path.replace("KF000ShowAction", "KK000ShowAction")

    s.post(
        f"{base}{kk_action}",
        data={
            "hachukikan": kikan_value,
            "bukyoku": "",
            "kakakari": "",
            "kasho_name": kikan_name + "　",
            "hachukikan_name": kikan_name,
            "supplytype": supplytype,
        },
        timeout=TIMEOUT,
    )
    s.get(f"{base}/koukai/do/koukai_menu", timeout=TIMEOUT)
    s.post(f"{base}{menu_case_action}", data={}, timeout=TIMEOUT)

    search_data = {
        "A046": "",
        "koujimei": "",
        "koujibasho": "",
        "date_start": "",
        "date_end": "",
        "A300": "040",
        "perPage": "0",
        "curPage": "0",
        "recordnumstart": "0",
        "recordnumend": "0",
        "recordNum": "",
    }
    if search_extra:
        search_data.update(search_extra)
    s.post(f"{base}{search_action}", data=search_data, timeout=TIMEOUT)

    r = s.get(f"{base}{frame_action}", timeout=TIMEOUT)
    html = r.content.decode("cp932", errors="replace")
    return html


def ibaraki_buppin():
    """茨城県 物品・役務入札情報(ppi2.cals-ibaraki.lg.jp)。"""
    base = "http://ppi2.cals-ibaraki.lg.jp"
    html = _ibaraki_style(
        base,
        entry_action="/koukai/do/KF000ShowAction",
        kikan_value="0000ZZZZZZ",
        kikan_name="茨城県",
        supplytype="11",
        menu_case_action="/koukai/do/KB301ShowAction",
        search_action="/koukai/do/KB301SearchAction",
        frame_action="/koukai/do/KFB301FrameShow",
    )
    html = re.sub(
        r"javascript:doEdit\('(\d+)'\)",
        base + r"/koukai/do/KB302ShowAction?control_no=\1",
        html,
    )
    return html, base + "/koukai/do/"


def saitama_buppin():
    """埼玉県 入札情報公開システム 物品・役務(ebidjk2.ebid2.pref.saitama.lg.jp)。"""
    base = "https://ebidjk2.ebid2.pref.saitama.lg.jp"
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT})

    s.get(f"{base}/koukai/do/KF000ShowAction", timeout=TIMEOUT)
    s.get(f"{base}/koukai/do/koukai_menu", timeout=TIMEOUT)
    r3 = s.get(f"{base}/koukai/do/koukai_main", timeout=TIMEOUT)

    common = {
        "chotatsuType": "11",
        "select_kikan": "0000ZZZZZZ",
        "auth": "",
        "gyosyu_type": "",
    }
    r4 = s.post(
        f"{base}/koukai/do/KB301ShowAction",
        data=common,
        timeout=TIMEOUT,
        headers={"Referer": r3.url},
    )
    search_data = dict(
        common,
        koujimei="",
        koujibangou="",
        basyo="",
        A300="040",
        searchflg="1",
    )
    s.post(
        f"{base}/koukai/do/KB301SearchAction",
        data=search_data,
        timeout=TIMEOUT,
        headers={"Referer": r4.url},
    )
    r6 = s.get(f"{base}/koukai/do/KFB301FrameShow", timeout=TIMEOUT)
    html = r6.content.decode("cp932", errors="replace")
    html = re.sub(
        r"javascript:doEdit\('(\d+)'\);?",
        base + r"/koukai/do/KB302ShowAction?control_no=\1",
        html,
    )
    return html, base + "/koukai/do/"


def kanagawa_buppin():
    """神奈川県 入札情報サービスシステム 物品・一般委託(ebid-joho.e-kanagawa.lg.jp)。

    案件名がリンクではなく素の<td>テキストのため、extract_candidates()が
    拾えるよう<a>タグで包んでから返す(周辺テキスト抽出ロジック自体は再利用)。
    """
    base = "https://ebid-joho.e-kanagawa.lg.jp/DENTYO"
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT})

    def csrf_tabid(html):
        csrf = re.search(r'name="_csrf" value="([^"]+)"', html).group(1)
        tabid = re.search(r'name="tabId" value="([^"]+)"', html).group(1)
        return csrf, tabid

    r1 = s.get(f"{base}/GPPI_MENU", timeout=TIMEOUT)
    csrf, tabid = csrf_tabid(r1.text)

    r2 = s.post(
        f"{base}/P5000_10",
        data={"_csrf": csrf, "hdn_dantai": "0001", "tabId": tabid},
        timeout=TIMEOUT,
        headers={"Referer": r1.url},
    )
    csrf, tabid = csrf_tabid(r2.text)

    r3 = s.post(
        f"{base}/P6510_10?hdn_gyoshu=3",
        data={
            "_csrf": csrf,
            "hdn_dantai": "0001",
            "hdn_dantaiNm": "神奈川県",
            "menuCd": "P6510",
            "menuName": "入札公告",
            "tabId": tabid,
            "action": "disp",
        },
        timeout=TIMEOUT,
        headers={"Referer": r2.url},
    )
    csrf, tabid = csrf_tabid(r3.text)

    search_data = {
        "_csrf": csrf,
        "hdn_dantai": "0001",
        "hdn_dantaiNm": "神奈川県",
        "hdn_gyoshu": "3",
        "orderGroup": "0001",
        "denshiNyusatsuDiv": "",
        "keisaiNen": "",
        "hacchuBuCd": "",
        "hacchuJimuCd": "",
        "ankenNumber": "",
        "nyusatsuType": "",
        "nameSearch": "",
        "kokokuStartDateYear": "",
        "kokokuStartDateMonth": "",
        "kokokuStartDateDay": "",
        "kokokuEndDateYear": "",
        "kokokuEndDateMonth": "",
        "kokokuEndDateDay": "",
        "pageSize": "100",
        "tabId": tabid,
        "action": "search",
    }
    r4 = s.post(
        f"{base}/P6510_10/Search",
        data=search_data,
        timeout=TIMEOUT,
        headers={"Referer": r3.url},
    )

    soup = BeautifulSoup(r4.text, "lxml")
    detail_url = f"{base}/P6510_10"
    for td in soup.select("td.construction-name"):
        if td.find("a") or not td.get_text(strip=True):
            continue
        a_tag = soup.new_tag("a", href=detail_url)
        a_tag.string = td.get_text(strip=True)
        td.clear()
        td.append(a_tag)
    return str(soup), detail_url


def tokyo_pbi():
    """東京都 入札情報サービス(発注予定情報)。

    レガシーなサーバ側フォームバリデーションが requests での単純な
    POST再現に対して不安定なため、Playwrightで実ブラウザ操作を行う。
    """
    from playwright.sync_api import sync_playwright

    base = "https://www.e-procurement.metro.tokyo.lg.jp"
    nav_timeout = TIMEOUT * 1000
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page(user_agent=USER_AGENT)
            # チャットウィジェットが常時通信するため networkidle は使わず、
            # 各ステップの遷移先に現れるはずの要素を明示的に待つ。
            # クリックイベント経由だとチャットウィジェット等の影響で不安定なため、
            # リンクの javascript: href が呼ぶ関数を直接評価して遷移する。
            page.goto(f"{base}/indexPbi.jsp", timeout=nav_timeout)
            page.wait_for_selector('form[name="main"]', state="attached", timeout=nav_timeout)
            page.evaluate("SelectTargetSubmit(3,3,'_top')")

            page.wait_for_selector("a.btnS:has-text('検索')", timeout=nav_timeout)
            page.evaluate("SelectSubmitOrder(4,1)")

            page.wait_for_selector(
                "table.list-data, a.btnS:has-text('表示')", timeout=nav_timeout
            )
            if page.locator("a.btnS:has-text('表示')").count() > 0:
                page.evaluate("SelectSubmit(4,3)")
                page.wait_for_selector("table.list-data", timeout=nav_timeout)
            html = page.content()
        finally:
            browser.close()

    html = re.sub(r"javascript:SelectSubmitNo\([^)]*\)", base + "/indexPbi.jsp", html)
    return html, base + "/"


HANDLERS = {
    "ibaraki_buppin": ibaraki_buppin,
    "saitama_buppin": saitama_buppin,
    "kanagawa_buppin": kanagawa_buppin,
    "tokyo_pbi": tokyo_pbi,
}


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
            handler_name = org.get("handler")
            if handler_name:
                html, effective_url = HANDLERS[handler_name]()
            else:
                html = fetch(url)
                effective_url = url
            candidates = extract_candidates(html, effective_url)
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
