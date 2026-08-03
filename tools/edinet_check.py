#!/usr/bin/env python3
"""EDINET売却シグナル監視 — CRE Scout Phase 2

金融庁のEDINET API(無料・公式)から新着開示を取得し、
有価証券報告書・半期報告書・臨時報告書の本文から
「減損損失」「閉鎖」「リースバック」などの売却シグナルを検出して
HTMLレポートを生成する。

使い方:
  python3 tools/edinet_check.py                # 今日から過去1日分
  python3 tools/edinet_check.py --days 3      # 今日から過去3日分(週明け用)
  python3 tools/edinet_check.py --date 2026-07-31   # 特定日
  python3 tools/edinet_check.py --days 3 --open     # 終了後レポートを開く

APIキーは tools/edinet_key.txt に置く(このスクリプトには書かない)。
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import io
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

BASE = "https://api.edinet-fsa.go.jp/api/v2"
TOOL_DIR = Path(__file__).resolve().parent
ROOT = TOOL_DIR.parent
# 金融庁の公式EDINET APIキー(EDINET DBのedb_キーとは別物)
KEY_FILE = TOOL_DIR / "edinet_key_fsa.txt"
REPORT_DIR = ROOT / "reports"
PDF_DIR = REPORT_DIR / "pdf"

# 監視対象の書類種別(EDINET docTypeCode)
DOC_TYPES = {
    "120": "有価証券報告書",
    "130": "訂正有価証券報告書",
    "160": "半期報告書",
    "170": "訂正半期報告書",
    "180": "臨時報告書",
    "190": "訂正臨時報告書",
}

# 売却シグナルの監視キーワード(営業基準 §03 に対応)
KEYWORDS = [
    "減損損失",
    "リースバック",
    "固定資産の譲渡",
    "固定資産の売却",
    "閉鎖",
    "統廃合",
    "拠点統合",
    "本社移転",
    "遊休",
    "撤退",
    "構造改革",
    "事業再編",
    "資本効率",
]

TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"[\s　]+")


def read_key() -> str:
    env = os.environ.get("EDINET_FSA_KEY", "").strip()
    if env:
        return env
    try:
        key = KEY_FILE.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        sys.exit(f"APIキーファイルがありません: {KEY_FILE}\n"
                 "金融庁の公式EDINET APIキー(無料)を取得して、このファイルに1行で保存してください。\n"
                 "取得先: https://api.edinet-fsa.go.jp/api/auth/index.aspx?mode=1\n"
                 "※ EDINET DBの edb_ で始まるキーとは別物です。")
    if not key:
        sys.exit(f"APIキーファイルが空です: {KEY_FILE}")
    return key


def fetch(url: str, timeout: int = 60, retries: int = 2) -> bytes:
    last_err = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "cre-scout-edinet-check/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as res:
                return res.read()
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                sys.exit("EDINET APIキーが認証されませんでした(401/403)。\n"
                         "キーの綴りと、EDINET側の利用登録が完了しているかを確認してください。")
            if e.code == 404:
                raise
            last_err = e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last_err = e
        time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"通信に失敗しました: {url} ({last_err})")


def list_documents(key: str, date: str) -> list:
    url = f"{BASE}/documents.json?date={date}&type=2&Subscription-Key={key}"
    data = json.loads(fetch(url).decode("utf-8"))
    return data.get("results") or []


def document_text(key: str, doc_id: str) -> str:
    """書類のCSV(XBRLデータ)を取得し、タグを除いた本文テキストを返す"""
    url = f"{BASE}/documents/{doc_id}?type=5&Subscription-Key={key}"
    try:
        raw = fetch(url)
    except urllib.error.HTTPError:
        return ""
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile:
        return ""
    parts = []
    for name in zf.namelist():
        if not name.lower().endswith(".csv"):
            continue
        try:
            parts.append(zf.read(name).decode("utf-16", errors="ignore"))
        except Exception:
            continue
    text = "\n".join(parts)
    text = TAG_RE.sub(" ", text)
    text = html.unescape(text)
    return text


def scan_text(text: str) -> dict:
    """キーワードごとの出現回数と前後の抜粋を返す"""
    hits = {}
    for kw in KEYWORDS:
        count = text.count(kw)
        if not count:
            continue
        snippets = []
        start = 0
        for _ in range(2):  # 抜粋は最大2件(前後の文脈を広めに)
            pos = text.find(kw, start)
            if pos < 0:
                break
            s = WS_RE.sub(" ", text[max(0, pos - 60): pos + len(kw) + 90]).strip()
            snippets.append(s)
            start = pos + len(kw)
        hits[kw] = {"count": count, "snippets": snippets}
    return hits


def save_pdf(key: str, doc_id: str) -> Path | None:
    path = PDF_DIR / f"{doc_id}.pdf"
    if path.exists():
        return path
    url = f"{BASE}/documents/{doc_id}?type=2&Subscription-Key={key}"
    try:
        raw = fetch(url)
    except (urllib.error.HTTPError, RuntimeError):
        return None
    if not raw.startswith(b"%PDF"):
        return None
    path.write_bytes(raw)
    return path


def build_report(dates: list, hits: list, checked: int) -> Path:
    now = dt.datetime.now()
    period = f"{dates[-1]} 〜 {dates[0]}" if len(dates) > 1 else dates[0]
    rows = []
    for h in sorted(hits, key=lambda x: -x["total"]):
        kw_chips = "".join(
            f'<span class="kw">{html.escape(k)} <b>{v["count"]}</b></span>'
            for k, v in sorted(h["hits"].items(), key=lambda i: -i[1]["count"]))
        snips = []
        for k, v in sorted(h["hits"].items(), key=lambda i: -i[1]["count"])[:3]:
            for s in v["snippets"][:1]:
                snips.append(f'<div class="snip">…{html.escape(s)}…</div>')
        pdf_link = (f'<a href="pdf/{h["docID"]}.pdf">PDFを開く</a>' if h["pdf"] else
                    f'<span class="muted">PDFなし(docID: {h["docID"]})</span>')
        sec = f'({h["secCode"][:4]})' if h.get("secCode") else ""
        rows.append(f"""
        <tr>
          <td><div class="name">{html.escape(h["filerName"])} <span class="muted">{sec}</span></div>
              <div class="muted">{html.escape(h["docDescription"])} — 提出 {html.escape(h["submitDateTime"])}</div>
              <div class="chips">{kw_chips}</div>{"".join(snips)}
              <div class="links">{pdf_link}</div></td>
          <td class="r num">{h["total"]}</td>
        </tr>""")
    body_rows = "".join(rows) or '<tr><td colspan="2" class="muted" style="padding:32px;text-align:center">シグナルに該当する開示はありませんでした。</td></tr>'
    doc = f"""<!DOCTYPE html>
<html lang="ja"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>EDINET売却シグナル {period}</title>
<style>
  :root {{ --bg:#f8f9fa; --surface:#fff; --text:#212529; --sub:#6c757d; --border:#dee2e6;
          --primary:#0017C1; --accent:#0031D8; --subtle:#E8F1FE; }}
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{ font-family:"Hiragino Sans","Yu Gothic UI",system-ui,sans-serif; background:var(--bg);
         color:var(--text); font-size:14px; line-height:1.6; padding:40px 24px; }}
  .wrap {{ max-width:960px; margin:0 auto; }}
  h1 {{ font-size:1.35rem; color:var(--primary); }}
  .lede {{ color:var(--sub); font-size:.8125rem; margin:4px 0 24px; }}
  table {{ width:100%; border-collapse:collapse; background:var(--surface);
          border:1px solid var(--border); border-radius:12px; overflow:hidden; }}
  th {{ text-align:left; font-size:.75rem; color:var(--sub); padding:10px 16px;
       border-bottom:1px solid var(--border); }}
  td {{ padding:14px 16px; border-bottom:1px solid var(--border); vertical-align:top; }}
  .r {{ text-align:right; }} .num {{ font-family:ui-monospace,Menlo,monospace; font-weight:700; }}
  .name {{ font-weight:700; }}
  .muted {{ color:var(--sub); font-size:.75rem; }}
  .chips {{ margin:6px 0 2px; }}
  .kw {{ display:inline-block; font-size:.75rem; background:var(--subtle); color:var(--primary);
        border-radius:999px; padding:1px 10px; margin:2px 4px 2px 0; }}
  .kw b {{ font-family:ui-monospace,Menlo,monospace; }}
  .snip {{ font-size:.75rem; color:#495057; background:#f1f3f5; border-radius:6px;
          padding:4px 10px; margin-top:4px; }}
  .links {{ margin-top:6px; font-size:.8125rem; }}
  a {{ color:var(--accent); }}
  .foot {{ color:var(--sub); font-size:.72rem; margin-top:20px; line-height:1.7; }}
</style></head><body><div class="wrap">
  <h1>EDINET売却シグナル レポート</h1>
  <p class="lede">対象期間: {period} / 調査書類: {checked}件(有報・半期・臨時報告書) /
    検出: {len(hits)}社 — 作成 {now.strftime("%Y-%m-%d %H:%M")}</p>
  <table><thead><tr><th>企業・開示内容(スコア順)</th><th class="r">検出数</th></tr></thead>
  <tbody>{body_rows}</tbody></table>
  <p class="foot">キーワードの機械検出のため誤検知を含みます(例:「閉鎖」「撤退」は文脈確認が必要)。
  気になる企業はPDF原文を確認のうえ、CRE Scoutの企業台帳に登録してください。<br>
  未公表情報ではなく公開開示のみを対象としていますが、商談化の際はインサイダー情報の取扱いに注意してください。</p>
</div></body></html>"""
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    out = REPORT_DIR / f"edinet_{dates[0]}_{now.strftime('%H%M')}.html"
    out.write_text(doc, encoding="utf-8")
    return out


LATIN_NOISE_RE = re.compile(r"[A-Za-z_:.\"']{6,}")


def clean_passage(s: str, limit: int = 170) -> str:
    """XBRLタグ名などの英字ノイズを除き、引用として読める形に整える"""
    s = LATIN_NOISE_RE.sub(" ", str(s or ""))
    s = re.sub(r"[\s　]+", " ", s).strip()
    return s[:limit]


def export_disclosures_js(new_hits: list):
    """直近14日分のヒットを蓄積して、アプリの「発掘」タブ用データを書き出す"""
    cache_file = TOOL_DIR / "cache" / "disclosure_hits.json"
    try:
        cache = json.loads(cache_file.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        cache = {}
    for h in new_hits:
        top_kws = sorted(h["hits"].items(), key=lambda i: -i[1]["count"])[:4]
        passages = {k: [p for p in (clean_passage(s) for s in v["snippets"][:2]) if p]
                    for k, v in top_kws}
        cache[h["docID"]] = {
            "docID": h["docID"], "filerName": h["filerName"],
            "secCode": (h.get("secCode") or "")[:4],
            "docDescription": h["docDescription"],
            "submitDateTime": h["submitDateTime"],
            "keywords": {k: v["count"] for k, v in h["hits"].items()},
            "snippet": next((s for v in sorted(h["hits"].values(),
                             key=lambda x: -x["count"]) for s in v["snippets"][:1]), ""),
            "passages": passages,
            "total": h["total"],
        }
    cutoff = (dt.date.today() - dt.timedelta(days=14)).isoformat()
    cache = {k: v for k, v in cache.items()
             if (v.get("submitDateTime") or "")[:10] >= cutoff}
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
    hits_sorted = sorted(cache.values(), key=lambda v: v.get("submitDateTime") or "", reverse=True)
    payload = {"updated": dt.datetime.now().strftime("%Y-%m-%d %H:%M"), "hits": hits_sorted}
    data_dir = ROOT / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "disclosures.js").write_text(
        "window.CRE_DISCLOSURES = " + json.dumps(payload, ensure_ascii=False) + ";",
        encoding="utf-8")


def main():
    ap = argparse.ArgumentParser(description="EDINET売却シグナル監視")
    ap.add_argument("--days", type=int, default=1, help="今日から過去N日分を調べる(既定1)")
    ap.add_argument("--date", help="特定日だけ調べる(YYYY-MM-DD)")
    ap.add_argument("--max-docs", type=int, default=0, help="調査する書類数の上限(0=無制限)")
    ap.add_argument("--no-pdf", action="store_true", help="PDFのダウンロードを省略(CI用)")
    ap.add_argument("--open", action="store_true", help="終了後にレポートをブラウザで開く")
    args = ap.parse_args()

    key = read_key()
    if args.date:
        dates = [args.date]
    else:
        today = dt.date.today()
        dates = [(today - dt.timedelta(days=i)).isoformat() for i in range(args.days)]

    PDF_DIR.mkdir(parents=True, exist_ok=True)
    targets = []
    for date in dates:
        docs = list_documents(key, date)
        picked = [d for d in docs
                  if d.get("docTypeCode") in DOC_TYPES and d.get("csvFlag") == "1"
                  and d.get("filerName")
                  # 投資信託・ファンドの開示は対象外(事業会社の不動産シグナルが目的)
                  and not re.search(r"投資信託|受益証券|投資証券",
                                    d.get("docDescription") or "")
                  and not re.search(r"アセットマネジメント|投資信託|投資顧問",
                                    d.get("filerName") or "")]
        print(f"{date}: 全開示 {len(docs)}件 → 対象 {len(picked)}件")
        targets.extend(picked)

    if args.max_docs and len(targets) > args.max_docs:
        print(f"※ 上限指定により {args.max_docs}件に制限します(全{len(targets)}件)")
        targets = targets[: args.max_docs]

    hits = []
    for i, d in enumerate(targets, 1):
        doc_id = d["docID"]
        print(f"  [{i}/{len(targets)}] {d['filerName'][:24]} ({DOC_TYPES[d['docTypeCode']]})", flush=True)
        text = document_text(key, doc_id)
        if not text:
            continue
        found = scan_text(text)
        if not found:
            continue
        pdf = None if args.no_pdf else save_pdf(key, doc_id)
        hits.append({
            "docID": doc_id,
            "filerName": d["filerName"],
            "secCode": d.get("secCode") or "",
            "docDescription": d.get("docDescription") or DOC_TYPES[d["docTypeCode"]],
            "submitDateTime": d.get("submitDateTime") or "",
            "hits": found,
            "total": sum(v["count"] for v in found.values()),
            "pdf": bool(pdf),
        })
        time.sleep(0.1)

    report = build_report(dates, hits, len(targets))
    export_disclosures_js(hits)
    print()
    print(f"検出: {len(hits)}社 / 調査 {len(targets)}書類")
    for h in sorted(hits, key=lambda x: -x["total"])[:10]:
        kws = "、".join(f"{k}×{v['count']}" for k, v in
                        sorted(h["hits"].items(), key=lambda i: -i[1]["count"])[:4])
        print(f"  {h['total']:>4}  {h['filerName']}  [{kws}]")
    print(f"\nレポート: {report}")
    if args.open:
        subprocess.run(["open", str(report)])


if __name__ == "__main__":
    main()
