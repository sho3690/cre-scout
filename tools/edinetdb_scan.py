#!/usr/bin/env python3
"""EDINET DB 企業不動産スキャナ — CRE Scout Phase 2

民間サービス「EDINET DB」(edinetdb.jp) のAPIで上場企業の
保有不動産(物件単位の簿価・所在地)・財務・有報テキストを取得し、
CRE Scoutの台帳に取り込めるレポートとJSONを作る。

2つの使い方:
  1) 深掘り: 指定した企業の不動産・財務を調べ、台帳取り込みJSONを作る
     python3 tools/edinetdb_scan.py --company 淀川製鋼所 --company 7003
  2) スクリーナー: 無料枠(100回/日)の範囲で全上場企業を少しずつ調べ、
     「含み益・土地簿価ランキング」を育てる(毎日実行すると増える)
     python3 tools/edinetdb_scan.py --screen

APIキーは tools/edinet_key.txt。金額の単位は百万円に統一。
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE = "https://edinetdb.jp/v1"
TOOL_DIR = Path(__file__).resolve().parent
ROOT = TOOL_DIR.parent
KEY_FILE = TOOL_DIR / "edinet_key.txt"
CACHE_DIR = TOOL_DIR / "cache"
RE_CACHE = CACHE_DIR / "realestate_cache.json"
CO_CACHE = CACHE_DIR / "companies_cache.json"
DIVES_CACHE = CACHE_DIR / "dives.json"
USAGE_FILE = CACHE_DIR / "usage.json"
REPORT_DIR = ROOT / "reports"
DATA_DIR = ROOT / "data"

DAILY_LIMIT = 90  # 無料枠100回/日のうち、余裕を残して使う上限

# 売却シグナルのキーワード → CRE Scoutのシグナルキー対応
KEYWORD_SIGNALS = {
    "減損損失": "impairment",
    "リースバック": "slb",
    "固定資産の譲渡": "asset_transfer",
    "固定資産の売却": "asset_transfer",
    "閉鎖": "plant_close",
    "統廃合": "site_consol",
    "拠点統合": "site_consol",
    "本社移転": "hq_move",
    "遊休": "idle_asset",
    "撤退": "withdraw",
    "構造改革": "restructure",
    "事業再編": "restructure",
    "資本効率": "cap_eff",
}

# 不動産を持ちやすい業種(スクリーナーの優先順)。
# 不動産業はプロの保有者(売り手というより買い手候補)なので優先しない。
PRIORITY_INDUSTRIES = [
    "倉庫・運輸関連業", "陸運業", "小売業", "繊維製品", "鉄鋼",
    "パルプ・紙", "化学", "食料品", "機械", "電気機器", "輸送用機器",
    "金属製品", "ガラス・土石製品", "非鉄金属", "卸売業", "建設業",
    "海運業", "サービス業", "電気・ガス業",
]


# ---------------------------------------------------------------- 基盤
def read_key() -> str:
    env = os.environ.get("EDINETDB_API_KEY", "").strip()
    if env:
        return env
    try:
        key = KEY_FILE.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        sys.exit(f"APIキーファイルがありません: {KEY_FILE}")
    if not key:
        sys.exit(f"APIキーファイルが空です: {KEY_FILE}")
    return key


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


class Budget:
    """無料枠を使い切らないための日次カウンタ"""
    def __init__(self, limit: int):
        self.limit = limit
        self.today = dt.date.today().isoformat()
        data = load_json(USAGE_FILE, {})
        self.used = int(data.get(self.today, 0))

    def spend(self, n: int = 1) -> bool:
        if self.used + n > self.limit:
            return False
        self.used += n
        save_json(USAGE_FILE, {self.today: self.used})
        return True

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.used)


BUDGET: Budget = None
API_KEY: str = ""


def api(path: str, params: dict = None) -> dict | list | None:
    """APIを1回呼ぶ(予算を1消費)。枠切れならNone。"""
    if not BUDGET.spend():
        return None
    url = f"{BASE}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "X-API-Key": API_KEY, "User-Agent": "cre-scout-edinetdb-scan/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=60) as res:
            body = json.loads(res.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            sys.exit("EDINET DBのAPIキーが認証されませんでした。tools/edinet_key.txt を確認してください。")
        if e.code == 429:
            print("※ API側のレート制限に達しました。今日はここまでにします。")
            return None
        print(f"  ! APIエラー {e.code}: {path}")
        return None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        print(f"  ! 通信エラー: {e}")
        return None
    return body.get("data") if isinstance(body, dict) and "data" in body else body


# ---------------------------------------------------------------- 数値処理
def yen_to_m(v):
    """円 → 百万円"""
    return round(v / 1e6, 1) if isinstance(v, (int, float)) else None


def fmt_m(m):
    """百万円 → 読みやすい表記"""
    if m is None:
        return "—"
    if abs(m) >= 100:
        return f"{m/100:,.2f}".rstrip("0").rstrip(".") + "億円"
    return f"{m*100:,.0f}万円"


FOOTNOTE_RE = re.compile(r"^[＊*※]\s*[0-9０-９]*\s*[::．.]?\s*")


def dedupe_facilities(items: list) -> list:
    seen, out = set(), []
    for f in items or []:
        # 有報の脚注(「＊1: …」等)が物件として混入することがあるため掃除する
        name = FOOTNOTE_RE.sub("", str(f.get("name") or "")).strip()
        if not name:
            continue
        f = {**f, "name": name}
        key = (f.get("name"), f.get("location_raw"), f.get("book_value_total_m_yen"),
               f.get("fiscal_year"))
        if key in seen:
            continue
        seen.add(key)
        out.append(f)
    # 最新年度のみ
    years = [f.get("fiscal_year") for f in out if f.get("fiscal_year")]
    if years:
        latest = max(years)
        out = [f for f in out if f.get("fiscal_year") == latest]
    out.sort(key=lambda f: -(f.get("book_value_total_m_yen") or 0))
    return out


def latest_note(re_data: dict) -> dict:
    """real_estate_notes(年度ごとの注記)から最新年度分を返す"""
    notes = [n for n in (re_data or {}).get("real_estate_notes") or [] if isinstance(n, dict)]
    return max(notes, key=lambda n: n.get("fiscal_year", 0)) if notes else {}


def note_bs(re_data: dict) -> dict:
    """最新注記のBS計上額(百万円): land_m_yen / buildings_m_yen / real_estate_total_m_yen"""
    return latest_note(re_data).get("bs") or {}


def extract_lease(re_data: dict) -> dict:
    """賃貸等不動産注記(簿価・時価・含み益、百万円)を抽出"""
    ld = latest_note(re_data).get("lease_disclosure") or {}
    out = {}
    for src, dst in [("book_value_m_yen", "book"), ("fair_value_m_yen", "fair"),
                     ("book_value_gap_m_yen", "gap")]:
        if isinstance(ld.get(src), (int, float)):
            out[dst] = ld[src]
    if "gap" not in out and "fair" in out and "book" in out:
        out["gap"] = round(out["fair"] - out["book"], 1)
    if isinstance(ld.get("fv_bv_ratio"), (int, float)):
        out["fv_ratio"] = round(ld["fv_bv_ratio"], 2)
    if ld.get("region_description"):
        out["region"] = ld["region_description"]
    return out


def guess_prop_type(name: str) -> str:
    n = name or ""
    if "工場" in n or "製造" in n: return "工場"
    if "倉庫" in n or "物流" in n or "配送" in n or "センター" in n: return "倉庫"
    if "寮" in n or "社宅" in n: return "社宅・寮"
    if "遊休" in n or "未利用" in n: return "遊休地"
    if "店" in n: return "店舗"
    if "本社" in n or "ビル" in n or "事務所" in n or "支店" in n or "営業所" in n: return "自社ビル"
    return "その他"


# ---------------------------------------------------------------- 深掘り
def find_company(query: str) -> dict | None:
    res = api("/search", {"q": query})
    items = res if isinstance(res, list) else []
    if not items:
        print(f"  ! 見つかりませんでした: {query}")
        return None
    listed = [c for c in items if c.get("listing_status") == "listed"] or items
    return listed[0]


def deep_dive(query: str, with_text: bool) -> dict | None:
    co = find_company(query)
    if not co:
        return None
    code = co.get("edinet_code")
    name = co.get("name") or co.get("name_ja") or code
    print(f"■ {name} ({code} / {co.get('industry','—')})")

    re_data = api(f"/companies/{code}/real-estate") or {}
    fins = api(f"/companies/{code}/financials") or []
    latest_fin = max(fins, key=lambda f: f.get("fiscal_year", 0)) if fins else {}

    facilities = dedupe_facilities(re_data.get("facilities"))
    bs = note_bs(re_data)
    lease = extract_lease(re_data)

    total_assets = yen_to_m(latest_fin.get("total_assets"))
    # BS不動産計上額(百万円): 注記のBS(土地+建物)を最優先、なければ設備明細の合算
    re_book = None
    land_bs, bldg_bs = bs.get("land_m_yen"), bs.get("buildings_m_yen")
    if isinstance(land_bs, (int, float)) or isinstance(bldg_bs, (int, float)):
        re_book = round((land_bs or 0) + (bldg_bs or 0), 1)
    elif isinstance(bs.get("real_estate_total_m_yen"), (int, float)):
        re_book = bs["real_estate_total_m_yen"]
    if re_book is None and facilities:
        re_book = round(sum((f.get("book_value_land_m_yen") or 0) +
                            (f.get("book_value_buildings_m_yen") or 0) for f in facilities), 1)

    signals, snippets = {}, []
    if with_text:
        tb = api(f"/companies/{code}/text-blocks") or {}
        text = json.dumps(tb, ensure_ascii=False)
        for kw, sig in KEYWORD_SIGNALS.items():
            cnt = text.count(kw)
            if cnt:
                signals[kw] = {"count": cnt, "signal": sig}
                pos = text.find(kw)
                s = re.sub(r"[\s　\\]+", " ", text[max(0, pos-25): pos+len(kw)+35])
                snippets.append(f"{kw}: …{s}…")

    ratio = round(re_book / total_assets * 100, 1) if re_book and total_assets else None
    print(f"   総資産 {fmt_m(total_assets)} / 不動産簿価 {fmt_m(re_book)}"
          f" / 不動産比率 {ratio if ratio is not None else '—'}%")
    if lease.get("gap") is not None:
        print(f"   賃貸等不動産の含み益(注記): {fmt_m(lease['gap'])}")
    if signals:
        print("   シグナル: " + "、".join(f"{k}×{v['count']}" for k, v in signals.items()))
    print(f"   主要物件 {len(facilities)}件")

    return {
        "code": code, "name": name, "industry": co.get("industry") or "",
        "sec_code": (co.get("sec_code") or "")[:4],
        "total_assets": total_assets, "re_book": re_book, "ratio": ratio,
        "lease": lease, "facilities": facilities,
        "signals": signals, "snippets": snippets,
        "fiscal_year": latest_fin.get("fiscal_year"),
        "net_income": yen_to_m(latest_fin.get("net_income")),
        "cf_operating": yen_to_m(latest_fin.get("cf_operating")),
    }


def to_cre_company(d: dict) -> dict:
    """CRE Scout台帳の追記取り込み用フォーマットへ変換"""
    props = []
    for f in d["facilities"][:6]:
        loc = f.get("location_raw") or f.get("prefecture") or ""
        props.append({
            "type": guess_prop_type(f.get("name")),
            "location": f"{loc}({f.get('name','')})",
            "area": (f"{f['land_area_m2']:,.0f}㎡" if f.get("land_area_m2") else ""),
            "book": f.get("book_value_total_m_yen"),
            "est": None, "note": "EDINET DB(有報の設備状況)より",
        })
    memo_lines = [f"EDINET DBから自動取得({dt.date.today().isoformat()}, {d.get('fiscal_year','—')}年度有報ベース)。"]
    if d["lease"].get("gap") is not None:
        memo_lines.append(f"賃貸等不動産注記の含み益: {fmt_m(d['lease']['gap'])}")
    if d["snippets"]:
        memo_lines.append("有報テキストの検出: " + " / ".join(d["snippets"][:3]))
    return {
        "name": d["name"], "listing": "上場", "industry": d["industry"],
        "region": (d["facilities"][0].get("prefecture") if d["facilities"] else "") or "",
        "url": "", "memo": "\n".join(memo_lines),
        "fin": {"totalAssets": d["total_assets"], "reBook": d["re_book"], "debtNote": ""},
        "properties": props,
        "signals": sorted({v["signal"] for v in d["signals"].values()}),
        "score": {"needs": 0, "value": 0, "access": 0, "timing": 0, "profit": 0},
        "deal": {"stage": 0, "prob": 10, "type": "未定", "price": None,
                 "buyMargin": None, "nextAction": "台帳情報の確認・スコアリング", "nextDate": ""},
    }


# ---------------------------------------------------------------- スクリーナー
def all_companies() -> list:
    cache = load_json(CO_CACHE, None)
    if cache:
        return cache
    print("上場企業リストを取得中(初回のみ・1リクエスト)...")
    res = api("/companies", {"per_page": 5000})
    items = res if isinstance(res, list) else []
    items = [c for c in items if not c.get("is_delisted")]
    if items:
        save_json(CO_CACHE, items)
        print(f"  {len(items)}社を保存しました")
    return items


def industry_rank(industry: str) -> int:
    try:
        return PRIORITY_INDUSTRIES.index(industry)
    except ValueError:
        return len(PRIORITY_INDUSTRIES)


def screen():
    companies = all_companies()
    if not companies:
        print("企業リストを取得できませんでした(本日の枠切れの可能性)")
        return
    cache = load_json(RE_CACHE, {})
    todo = [c for c in companies if c.get("edinet_code") and c["edinet_code"] not in cache]
    todo.sort(key=lambda c: (industry_rank(c.get("industry", "")), c.get("edinet_code")))
    print(f"調査済み {len(cache)}社 / 未調査 {len(todo)}社 / 本日の残り枠 {BUDGET.remaining}回")

    n = 0
    for c in todo:
        if BUDGET.remaining <= 0:
            break
        code = c["edinet_code"]
        re_data = api(f"/companies/{code}/real-estate")
        if re_data is None:
            break
        facilities = dedupe_facilities((re_data or {}).get("facilities"))
        lease = extract_lease(re_data or {})
        bs = note_bs(re_data or {})
        land = bs.get("land_m_yen")
        if not isinstance(land, (int, float)):
            land = round(sum(f.get("book_value_land_m_yen") or 0 for f in facilities), 1)
        total = bs.get("real_estate_total_m_yen")
        if not isinstance(total, (int, float)):
            total = round(sum(f.get("book_value_total_m_yen") or 0 for f in facilities), 1)
        cache[code] = {
            "name": c.get("name"), "industry": c.get("industry") or "",
            "sec_code": (c.get("sec_code") or "")[:4],
            "land_book": land, "total_book": total,
            "gap": lease.get("gap"), "lease_book": lease.get("book"),
            "n_fac": len(facilities),
            "top_fac": (facilities[0].get("name") if facilities else ""),
            "top_loc": (facilities[0].get("location_raw") if facilities else ""),
            "checked": dt.date.today().isoformat(),
        }
        n += 1
        if n % 10 == 0:
            save_json(RE_CACHE, cache)
            print(f"  …{n}社調査(残り枠 {BUDGET.remaining})")
    save_json(RE_CACHE, cache)
    print(f"本日 {n}社を新規調査。累計 {len(cache)}社。")

    with_gap = sorted([v for v in cache.values() if v.get("gap") is not None],
                      key=lambda v: -v["gap"])
    top_gap = with_gap[:30]
    top_biz = [v for v in with_gap if v.get("industry") != "不動産業"][:30]
    top_land = sorted([v for v in cache.values() if v.get("industry") != "不動産業"],
                      key=lambda v: -(v.get("land_book") or 0))[:30]

    print("\n◆ 含み益上位【事業会社(不動産業を除く)= 本命ターゲット】:")
    for v in top_biz[:10]:
        print(f"  {fmt_m(v['gap']):>12}  {v['name']}({v['industry']})")
    print("\n◆ 土地簿価上位【事業会社】:")
    for v in top_land[:10]:
        print(f"  {fmt_m(v['land_book']):>12}  {v['name']}({v['industry']})")

    report = build_screen_report(cache, top_biz, top_land, top_gap)
    print(f"\nランキングレポート: {report}")
    return report


def esc(s):
    return html.escape(str(s or ""))


REPORT_CSS = """
  :root { --bg:#f8f9fa; --surface:#fff; --text:#212529; --sub:#6c757d; --border:#dee2e6;
          --primary:#0017C1; --accent:#0031D8; --subtle:#E8F1FE; }
  * { margin:0; padding:0; box-sizing:border-box; }
  body { font-family:"Hiragino Sans","Yu Gothic UI",system-ui,sans-serif; background:var(--bg);
         color:var(--text); font-size:14px; line-height:1.6; padding:40px 24px; }
  .wrap { max-width:1000px; margin:0 auto; }
  h1 { font-size:1.35rem; color:var(--primary); }
  h2 { font-size:1.05rem; margin:28px 0 10px; }
  .lede { color:var(--sub); font-size:.8125rem; margin:4px 0 20px; }
  table { width:100%; border-collapse:collapse; background:var(--surface);
          border:1px solid var(--border); border-radius:12px; overflow:hidden; }
  th { text-align:left; font-size:.72rem; color:var(--sub); padding:9px 14px;
       border-bottom:1px solid var(--border); white-space:nowrap; }
  td { padding:10px 14px; border-bottom:1px solid var(--border); vertical-align:top; }
  .r { text-align:right; white-space:nowrap; }
  .num { font-family:ui-monospace,Menlo,monospace; font-variant-numeric:tabular-nums; }
  .muted { color:var(--sub); font-size:.75rem; }
  .kw { display:inline-block; font-size:.72rem; background:var(--subtle); color:var(--primary);
        border-radius:999px; padding:1px 9px; margin:2px 4px 2px 0; }
  .stats { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:8px; margin:10px 0; }
  .stat { background:var(--surface); border:1px solid var(--border); border-radius:8px; padding:8px 12px; }
  .stat .l { font-size:.7rem; color:var(--sub); }
  .stat .v { font-weight:700; font-family:ui-monospace,Menlo,monospace; }
  .foot { color:var(--sub); font-size:.72rem; margin-top:20px; line-height:1.7; }
"""


def build_screen_report(cache: dict, top_biz: list, top_land: list, top_gap: list) -> Path:
    now = dt.datetime.now()
    def rows(items, key):
        out = []
        for i, v in enumerate(items, 1):
            out.append(f"""<tr><td class="r num">{i}</td>
              <td><b>{esc(v['name'])}</b> <span class="muted">({esc(v['sec_code'])} / {esc(v['industry'])})</span>
                <div class="muted">{esc(v['top_fac'])} {esc(v['top_loc'])} ほか{v['n_fac']}件</div></td>
              <td class="r num">{fmt_m(v.get('gap'))}</td>
              <td class="r num">{fmt_m(v.get('land_book'))}</td>
              <td class="r num">{fmt_m(v.get('total_book'))}</td></tr>""")
        return "".join(out)
    doc = f"""<!DOCTYPE html><html lang="ja"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>不動産スクリーナー {now.strftime('%Y-%m-%d')}</title><style>{REPORT_CSS}</style></head>
<body><div class="wrap">
<h1>上場企業 不動産スクリーナー</h1>
<p class="lede">調査済み {len(cache)}社(毎日実行すると増えます) — {now.strftime('%Y-%m-%d %H:%M')} / 出典: EDINET DB(有報の設備状況・賃貸等不動産注記)</p>
<h2>含み益上位 — 事業会社(不動産業を除く)= 本命ターゲット</h2>
<table><thead><tr><th class="r">#</th><th>企業</th><th class="r">含み益</th><th class="r">土地簿価</th><th class="r">設備簿価計</th></tr></thead>
<tbody>{rows(top_biz, 'gap')}</tbody></table>
<h2>土地簿価上位 — 事業会社</h2>
<table><thead><tr><th class="r">#</th><th>企業</th><th class="r">含み益</th><th class="r">土地簿価</th><th class="r">設備簿価計</th></tr></thead>
<tbody>{rows(top_land, 'land_book')}</tbody></table>
<h2>参考: 含み益上位(不動産業を含む全体)</h2>
<table><thead><tr><th class="r">#</th><th>企業</th><th class="r">含み益</th><th class="r">土地簿価</th><th class="r">設備簿価計</th></tr></thead>
<tbody>{rows(top_gap, 'gap')}</tbody></table>
<p class="foot">含み益は「賃貸等不動産」注記を開示している企業のみ算出できます(約2,000社)。
数値は有報からの機械抽出のため、商談前に必ず原典(有価証券報告書)を確認してください。</p>
</div></body></html>"""
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    out = REPORT_DIR / f"screener_{now.strftime('%Y-%m-%d')}.html"
    out.write_text(doc, encoding="utf-8")
    return out


def build_dive_report(dives: list) -> Path:
    now = dt.datetime.now()
    secs = []
    for d in dives:
        fac_rows = "".join(f"""<tr><td>{esc(guess_prop_type(f.get('name')))}</td>
          <td>{esc(f.get('name'))}<div class="muted">{esc(f.get('location_raw'))}</div></td>
          <td class="r num">{fmt_m(f.get('book_value_land_m_yen'))}</td>
          <td class="r num">{fmt_m(f.get('book_value_buildings_m_yen'))}</td>
          <td class="r num">{fmt_m(f.get('book_value_total_m_yen'))}</td></tr>"""
          for f in d["facilities"][:10])
        kws = "".join(f'<span class="kw">{esc(k)} <b>{v["count"]}</b></span>'
                      for k, v in d["signals"].items())
        secs.append(f"""
<h2>{esc(d['name'])} <span class="muted">({esc(d['sec_code'])} / {esc(d['industry'])} / {d.get('fiscal_year','—')}年度)</span></h2>
<div class="stats">
  <div class="stat"><div class="l">総資産</div><div class="v">{fmt_m(d['total_assets'])}</div></div>
  <div class="stat"><div class="l">不動産簿価(土地+建物)</div><div class="v">{fmt_m(d['re_book'])}</div></div>
  <div class="stat"><div class="l">不動産比率</div><div class="v">{d['ratio'] if d['ratio'] is not None else '—'}%</div></div>
  <div class="stat"><div class="l">含み益(賃貸等不動産注記)</div><div class="v">{fmt_m(d['lease'].get('gap'))}</div></div>
  <div class="stat"><div class="l">純利益</div><div class="v">{fmt_m(d['net_income'])}</div></div>
  <div class="stat"><div class="l">営業CF</div><div class="v">{fmt_m(d['cf_operating'])}</div></div>
</div>
{f'<div style="margin:6px 0">{kws}</div>' if kws else ''}
<table><thead><tr><th>種別(推定)</th><th>物件・所在地</th><th class="r">土地</th><th class="r">建物</th><th class="r">帳簿計</th></tr></thead>
<tbody>{fac_rows or '<tr><td colspan=5 class=muted>設備データなし</td></tr>'}</tbody></table>""")
    doc = f"""<!DOCTYPE html><html lang="ja"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>企業不動産 深掘りレポート {now.strftime('%Y-%m-%d')}</title><style>{REPORT_CSS}</style></head>
<body><div class="wrap">
<h1>企業不動産 深掘りレポート</h1>
<p class="lede">{now.strftime('%Y-%m-%d %H:%M')} / 出典: EDINET DB(有価証券報告書の機械抽出)。数値は概算・要原典確認。</p>
{''.join(secs)}
<p class="foot">「台帳取り込みJSON」をCRE Scoutの「データ管理 → JSONを追記で取り込む」で読み込むと、
これらの企業が台帳に追加されます(既存の企業はそのまま)。</p>
</div></body></html>"""
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    out = REPORT_DIR / f"dive_{now.strftime('%Y-%m-%d_%H%M')}.html"
    out.write_text(doc, encoding="utf-8")
    return out


# ---------------------------------------------------------------- アプリ連携
def export_sourcing_js():
    """CRE Scoutの「発掘」タブが読むデータファイル(data/sourcing.js)を書き出す。
    <script src> で読み込むため、file:// でもサーバー無しで動く。"""
    cache = load_json(RE_CACHE, {})
    dives = load_json(DIVES_CACHE, {})
    with_gap = sorted([v for v in cache.values() if v.get("gap") is not None],
                      key=lambda v: -v["gap"])
    ranking = [{**v, "top_fac": FOOTNOTE_RE.sub("", str(v.get("top_fac") or "")).strip()}
               for v in with_gap if v.get("industry") != "不動産業"][:100]
    payload = {
        "updated": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "scanned": len(cache),
        "total": len(load_json(CO_CACHE, []) or []) or None,
        "ranking": ranking,
        "dives": dives,
    }
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "sourcing.js").write_text(
        "window.CRE_SOURCING = " + json.dumps(payload, ensure_ascii=False) + ";",
        encoding="utf-8")


# ---------------------------------------------------------------- main
def main():
    global BUDGET, API_KEY
    ap = argparse.ArgumentParser(description="EDINET DB 企業不動産スキャナ")
    ap.add_argument("--company", action="append", default=[],
                    help="深掘りする企業(名前・証券コード・EDINETコード)。複数指定可")
    ap.add_argument("--no-text", action="store_true", help="有報テキストのシグナル検出を省略(1社あたり1回節約)")
    ap.add_argument("--screen", action="store_true", help="スクリーナーモード(無料枠の範囲で少しずつ全社調査)")
    ap.add_argument("--budget", type=int, default=DAILY_LIMIT, help=f"今日使ってよいAPI回数の上限(既定{DAILY_LIMIT})")
    ap.add_argument("--open", action="store_true", help="終了後にレポートを開く")
    args = ap.parse_args()

    API_KEY = read_key()
    BUDGET = Budget(args.budget)
    print(f"本日のAPI使用: {BUDGET.used}回 / 上限 {BUDGET.limit}回(無料枠100回/日)")

    reports = []
    if args.company:
        dives = []
        for q in args.company:
            d = deep_dive(q, with_text=not args.no_text)
            if d:
                dives.append(d)
        if dives:
            report = build_dive_report(dives)
            cre_companies = [to_cre_company(d) for d in dives]
            imp = {"app": "cre-scout-import", "version": 1, "companies": cre_companies}
            imp_path = REPORT_DIR / f"cre-import_{dt.datetime.now().strftime('%Y-%m-%d_%H%M')}.json"
            save_json(imp_path, imp)
            # 深掘り結果を蓄積(発掘タブの「台帳に追加」が詳細データを使えるように)
            dcache = load_json(DIVES_CACHE, {})
            for c in cre_companies:
                dcache[c["name"]] = c
            save_json(DIVES_CACHE, dcache)
            print(f"\n深掘りレポート: {report}")
            print(f"台帳取り込みJSON: {imp_path}")
            reports.append(report)

    if args.screen:
        r = screen()
        if r:
            reports.append(r)

    if not args.company and not args.screen:
        ap.print_help()
        return

    export_sourcing_js()
    print(f"\n本日のAPI使用: {BUDGET.used}回(残り {BUDGET.remaining}回)")
    if args.open:
        for r in reports:
            subprocess.run(["open", str(r)])


if __name__ == "__main__":
    main()
