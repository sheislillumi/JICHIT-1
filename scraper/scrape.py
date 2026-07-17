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
import unicodedata
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

# 締切日・金額の精度向上のため、当日マッチした案件の詳細ページを追加取得する際の設定。
# 一覧ページのクロールより件数が少ない(1日あたり50件前後)ため、タイムアウトは短めにし、
# 失敗しても収集全体を止めない。
DETAIL_FETCH_TIMEOUT = 15
DETAIL_FETCH_SLEEP = 0.7

# 「展示会・商談会」および「販路拡大・バイヤーマッチング」を指すキーワード
KEYWORDS_TOPIC = [
    "展示", "商談", "見本市", "物産展", "産業展",
    "販路拡大", "販路開拓", "流通拡大", "販売促進",
    "マッチング", "バイヤー", "求評会",
]

# 「運営・委託・公募」であることを示すキーワード
KEYWORDS_OPERATION = [
    "運営", "開催", "委託", "事務局", "実施", "企画運営", "企画競争",
    "プロポーザル", "公募", "業務委託", "請負", "受託者", "事業者募集",
]

# 「マッチング」「バイヤー」は単体だと汎用的すぎて、ふるさと納税マッチングや
# 外国人材/雇用マッチングなど無関係分野を拾ってしまう。他により具体的な
# トピックキーワードが一致していない場合に限り、これらの語が併記されていたら
# 誤検知とみなして除外する。
GENERIC_TOPIC_KEYWORDS = {"マッチング", "バイヤー"}
NEGATIVE_KEYWORDS = ["ふるさと納税", "人材", "雇用"]

TEXT_TAGS = ["li", "tr", "p", "div", "dd", "article"]

# ---------------------------------------------------------------------------
# 締切日・金額のベストエフォート抽出
#
# 一覧ページの周辺テキストだけを対象にした正規表現ベースの抽出であり、
# 詳細ページ/PDFにしか書かれていない場合は拾えず null になる。100%網羅は
# 目指さず、拾えたものはダッシュボードの参考情報として表示する用途。
# ---------------------------------------------------------------------------

REIWA_EPOCH = 2018  # 令和N年 = 2018+N 年 (令和1年=2019年)

# 和暦(令和) / 西暦(年月日) / 西暦(区切り文字) / 年省略、の優先順で1つの正規表現にまとめる。
# finditer は左から非重複で走査するため、年付きの表記が年省略パターンより先に
# マッチしていれば、その一部(月日)だけが二重にマッチすることはない。
DATE_RE = re.compile(
    r"令和(?P<rey>\d{1,2})年(?P<rem>\d{1,2})月(?P<red>\d{1,2})日"
    r"|[RrＲｒ](?P<ry>\d{1,2})[・./\-](?P<rm>\d{1,2})[・./\-](?P<rd>\d{1,2})"
    r"|(?P<wy>\d{4})年(?P<wm>\d{1,2})月(?P<wd>\d{1,2})日"
    r"|(?P<sy>\d{4})[/\-](?P<sm>\d{1,2})[/\-](?P<sd>\d{1,2})"
    r"|(?P<bm>\d{1,2})月(?P<bd>\d{1,2})日"
)

DEADLINE_KEYWORDS = [
    "締切", "締め切り", "応募期限", "提出期限", "納入期限", "納期限",
    "受付期間", "受付締切", "公募期間", "応募期間",
]
# 詳細ページの本文全体を対象にすると、「質問受付期限」のような本来の締切とは
# 別の補助的な期限にまで「期限」の文字だけで反応してしまう(詳細ページには
# 応募期間・質問受付期限・提出期限などが並記されることがあり、汎用的すぎる
# 「期限」はそのうち最も近い日付＝無関係な補助的期限を誤って選んでしまう)。
# そのためDEADLINE_KEYWORDSの具体的な語で見つからなかった場合のみ、
# 最終手段としてこちらを使う。
DEADLINE_FALLBACK_KEYWORDS = ["期限"]
DEADLINE_WINDOW = 30
# 「令和8年4月9日から令和8年4月22日17時まで」のような期間表記では、締切として
# 意味があるのは終了日側。マッチ直後(この文字数以内)に「まで」があれば、
# それを開始日より優先する。
DEADLINE_UNTIL_WINDOW = 20

# 「11,000,000円」「1,100万円」「12,000千円」のような金額表記。
# 全角カンマ「，」はNFKC正規化すれば半角に統一できるが、それはnormalize_amount()が
# 表示用文字列を組み立てる段階の話であり、ここ(amount_rawとして拾う範囲を決める
# 正規表現)で対応しておかないと全角カンマの手前で数値が途切れてしまう
# (例:「２，０７８，０００円」の下2桁しか拾えない)ため、あえてそのまま残す。
AMOUNT_RE = re.compile(r"\d[\d,，]*(?:\.\d+)?(?:億|万|千)?円")
AMOUNT_KEYWORDS = ["上限", "予定価格", "委託料", "契約金額", "限度額"]
AMOUNT_WINDOW = 30

# amount_raw (全角混じりの生テキスト)を「1,234,567円」形式に正規化する際に使う。
AMOUNT_UNIT_MULTIPLIERS = {"億": 100_000_000, "万": 10_000, "千": 1_000}
NORMALIZE_AMOUNT_RE = re.compile(r"([\d,]+(?:\.\d+)?)\s*(億|万|千)?\s*円")


def _date_from_match(m, current_year):
    """DATE_RE のマッチからISO形式の日付文字列を組み立てる。不正な日付はNone。"""
    try:
        if m.group("rey"):
            year = REIWA_EPOCH + int(m.group("rey"))
            month, day = int(m.group("rem")), int(m.group("red"))
        elif m.group("ry"):
            year = REIWA_EPOCH + int(m.group("ry"))
            month, day = int(m.group("rm")), int(m.group("rd"))
        elif m.group("wy"):
            year, month, day = int(m.group("wy")), int(m.group("wm")), int(m.group("wd"))
        elif m.group("sy"):
            year, month, day = int(m.group("sy")), int(m.group("sm")), int(m.group("sd"))
        elif m.group("bm"):
            year, month, day = current_year, int(m.group("bm")), int(m.group("bd"))
        else:
            return None
        return datetime.date(year, month, day).isoformat()
    except (ValueError, TypeError):
        return None


def _keyword_positions(text, keywords):
    """各キーワードの (開始位置, 終了位置) を返す。"""
    positions = []
    for kw in keywords:
        start = 0
        while True:
            idx = text.find(kw, start)
            if idx == -1:
                break
            positions.append((idx, idx + len(kw)))
            start = idx + 1
    return positions


def _nearest_match(candidates, kw_positions, window, text=None, until_window=None):
    """kw_positions の直後(「期限：2026年8月15日」等)にあるマッチを優先し、
    見つからなければ直前にあるマッチを採用する。

    テーブル行のように「公開日 2026-06-01 提出期限 2026年7月20日」のような
    無関係な日付がキーワード直前に隣接することがあるため、単純な最短距離では
    掲載日を誤って締切日と判定してしまう。キーワード→値の語順を優先することで
    これを避ける。

    text と until_window を指定した場合、「令和8年4月9日から令和8年4月22日
    17時まで」のような期間表記で、直後(until_window文字以内)に「まで」が
    続く候補(=期間の終了日)があればそれを最優先する。「応募期間」等の
    キーワードでは開始日ではなく終了日こそが締切として意味を持つため。
    """
    forward_dist = {}
    for c in candidates:
        for kp_start, kp_end in kw_positions:
            if c.start() >= kp_end:
                dist = c.start() - kp_end
                if dist <= window and (c not in forward_dist or dist < forward_dist[c]):
                    forward_dist[c] = dist

    if forward_dist:
        if text is not None and until_window:
            until_candidates = []
            for c in forward_dist:
                tail = text[c.end():c.end() + until_window]
                until_idx = tail.find("まで")
                if until_idx == -1:
                    continue
                until_pos = c.end() + until_idx
                # cと「まで」の間に別の日付マッチが割り込んでいる場合、cは
                # 期間の開始日であって終了日ではないと判断し対象から外す。
                blocked = any(
                    other is not c and c.end() <= other.start() < until_pos
                    for other in candidates
                )
                if not blocked:
                    until_candidates.append(c)
            if until_candidates:
                return min(until_candidates, key=lambda c: forward_dist[c])
        return min(forward_dist, key=lambda c: forward_dist[c])

    backward, backward_dist = None, None
    for c in candidates:
        for kp_start, kp_end in kw_positions:
            if c.end() <= kp_start:
                dist = kp_start - c.end()
                if dist <= window and (backward_dist is None or dist < backward_dist):
                    backward, backward_dist = c, dist
    return backward


def extract_deadline(text, current_year):
    """締切キーワード近傍の日付のみを締切日として採用する(掲載日の誤検出を防ぐ)。

    具体的な締切キーワード(DEADLINE_KEYWORDS)で見つかればそれを優先し、
    何も見つからなかった場合のみ汎用的な「期限」(DEADLINE_FALLBACK_KEYWORDS)で
    再試行する。
    """
    if not text:
        return None, None
    date_matches = list(DATE_RE.finditer(text))
    if not date_matches:
        return None, None

    for keywords in (DEADLINE_KEYWORDS, DEADLINE_FALLBACK_KEYWORDS):
        kw_positions = _keyword_positions(text, keywords)
        if not kw_positions:
            continue
        best = _nearest_match(
            date_matches, kw_positions, DEADLINE_WINDOW,
            text=text, until_window=DEADLINE_UNTIL_WINDOW,
        )
        if best is not None:
            return _date_from_match(best, current_year), best.group(0)
    return None, None


def extract_amount(text):
    """金額キーワード近傍を優先しつつ、見つかった金額表記をraw文字列で返す。"""
    if not text:
        return None
    amount_matches = list(AMOUNT_RE.finditer(text))
    if not amount_matches:
        return None
    kw_positions = _keyword_positions(text, AMOUNT_KEYWORDS)
    best = _nearest_match(amount_matches, kw_positions, AMOUNT_WINDOW) if kw_positions else None
    return (best or amount_matches[0]).group(0)


def normalize_amount(raw_text):
    """amount_raw の表記ゆれ(全角数字、億/万/千単位)を「1,234,567円」形式に統一する。

    「6,000千円（1事業者）×2事業者」のような複合表記は、掛け算などせず最初に
    見つかった数値・単位をそのまま円換算するだけに留める(ベストエフォート)。
    パースできない場合は None を返す。
    """
    if not raw_text:
        return None
    normalized = unicodedata.normalize("NFKC", raw_text)
    m = NORMALIZE_AMOUNT_RE.search(normalized)
    if not m:
        return None
    try:
        number = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    multiplier = AMOUNT_UNIT_MULTIPLIERS.get(m.group(2), 1)
    value = round(number * multiplier)
    return f"{value:,}円"


def fetch_detail_deadline_amount(url, current_year):
    """当日マッチした案件の詳細ページを追加取得し、締切日・金額を抽出する。

    一覧ページのcontextより、詳細ページの方が「契約期間」「委託金額」のような
    見出し付きで構造化されており精度が高いことが多い。詳細ページの取得・解析に
    失敗しても例外を上位に伝播させず (None, None, None) を返し、
    呼び出し側で一覧ページの抽出結果をそのまま使わせる。
    """
    if url.split("?", 1)[0].lower().endswith(".pdf"):
        return None, None, None
    try:
        resp = requests.get(
            url,
            headers={"User-Agent": USER_AGENT},
            timeout=DETAIL_FETCH_TIMEOUT,
        )
        resp.raise_for_status()
        content_type = resp.headers.get("Content-Type", "")
        if "pdf" in content_type.lower():
            return None, None, None
        if resp.encoding is None or resp.encoding.lower() == "iso-8859-1":
            resp.encoding = resp.apparent_encoding
        soup = BeautifulSoup(resp.text, "lxml")
        body = soup.body or soup
        text = body.get_text(" ", strip=True)
    except Exception:  # noqa: BLE001 - 詳細取得の失敗は一覧の結果を維持して続行
        return None, None, None

    deadline_date, deadline_raw = extract_deadline(text, current_year)
    amount_raw = extract_amount(text)
    return deadline_date, deadline_raw, amount_raw


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
    if not (hit_topic and hit_op):
        return []
    specific_hit = [k for k in hit_topic if k not in GENERIC_TOPIC_KEYWORDS]
    if not specific_hit and any(neg in text for neg in NEGATIVE_KEYWORDS):
        return []
    return hit_topic + hit_op


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


def extract_candidates(html, base_url, current_year=None):
    """ページ内のリンクとその周辺テキストから候補を抽出する。"""
    if current_year is None:
        current_year = datetime.date.today().year

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

        deadline_date, deadline_raw = extract_deadline(context_text, current_year)
        amount_raw = extract_amount(context_text)
        amount_display = normalize_amount(amount_raw)

        candidates.append(
            {
                "url": abs_url,
                "title": title[:200],
                "context": context_text[:400],
                "matched_keywords": sorted(set(matched)),
                "deadline_date": deadline_date,
                "deadline_raw": deadline_raw,
                "amount_raw": amount_raw,
                "amount_display": amount_display,
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
    current_year = datetime.date.today().year
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
            candidates = extract_candidates(html, effective_url, current_year)
            matched_count = len(candidates)
            total_matched += matched_count

            # 当日マッチした案件のみ、詳細ページを追加取得して締切日・金額の
            # 精度を上げる。詳細URLがセッション依存の団体(detail_fetch_unsafe)は
            # 別セッションでは開けないためスキップし、一覧ページの抽出結果のみ使う。
            if not org.get("detail_fetch_unsafe"):
                for c in candidates:
                    d_date, d_raw, a_raw = fetch_detail_deadline_amount(c["url"], current_year)
                    if d_date or d_raw:
                        c["deadline_date"] = d_date
                        c["deadline_raw"] = d_raw
                    if a_raw:
                        c["amount_raw"] = a_raw
                        c["amount_display"] = normalize_amount(a_raw)
                    time.sleep(DETAIL_FETCH_SLEEP)

            for c in candidates:
                item_id = make_item_id(org_id, c["url"], c["title"])
                if item_id in existing_items:
                    existing_items[item_id]["last_seen"] = today
                    existing_items[item_id]["title"] = c["title"]
                    existing_items[item_id]["context"] = c["context"]
                    existing_items[item_id]["deadline_date"] = c["deadline_date"]
                    existing_items[item_id]["deadline_raw"] = c["deadline_raw"]
                    existing_items[item_id]["amount_raw"] = c["amount_raw"]
                    existing_items[item_id]["amount_display"] = c["amount_display"]
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
                        "deadline_date": c["deadline_date"],
                        "deadline_raw": c["deadline_raw"],
                        "amount_raw": c["amount_raw"],
                        "amount_display": c["amount_display"],
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
