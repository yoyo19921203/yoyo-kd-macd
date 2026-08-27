#!/usr/bin/env python3
"""
TPEx 歷史資料最小驗證工具
目的：
1. 抓 3324 雙鴻在 2026/08/25 與 2026/08/26 的資料
2. 顯示 OHLCV
3. 自動判斷兩天是否真的不同
4. 同時印出 TPEx 回傳的日期/欄位資訊，避免「日期參數被忽略」卻沒發現

執行：
    python scripts/tpex_history_test.py

如果要測別支股票：
    python scripts/tpex_history_test.py --code 4979

如果要改日期：
    python scripts/tpex_history_test.py --date1 20260825 --date2 20260826
"""

import argparse
import json
import re
import sys
from datetime import datetime

import requests

TIMEOUT = 30

# TPEx 歷史行情頁所使用的 result endpoint
HIST_URL = (
    "https://www.tpex.org.tw/web/stock/aftertrading/"
    "otc_quotes_no1430/stk_wn1430_result.php"
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 Chrome/126 Safari/537.36"
    ),
    "Accept": "application/json,text/plain,*/*",
    "Referer": "https://www.tpex.org.tw/",
}


def to_roc_date(yyyymmdd: str) -> str:
    dt = datetime.strptime(yyyymmdd, "%Y%m%d")
    return f"{dt.year - 1911}/{dt.month:02d}/{dt.day:02d}"


def clean_text(v):
    return re.sub(r"<[^>]*>", "", str(v or "")).strip()


def clean_num(v):
    s = clean_text(v).replace(",", "").strip()
    if s in ("", "--", "---", "----", "N/A"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def print_response_shape(data, label):
    print(f"\n--- {label} 回傳結構 ---")
    if not isinstance(data, dict):
        print("最外層不是 dict")
        return

    print("Top-level keys:", list(data.keys()))

    # 常見日期欄位都印出來
    for k, v in data.items():
        lk = str(k).lower()
        if any(word in lk for word in ["date", "time", "report", "title"]):
            if isinstance(v, (str, int, float)):
                print(f"{k}: {v}")

    tables = data.get("tables")
    if isinstance(tables, list):
        print("tables 數量:", len(tables))
        for i, t in enumerate(tables[:3]):
            if isinstance(t, dict):
                fields = t.get("fields", [])
                rows = t.get("data", [])
                print(f"tables[{i}] fields({len(fields)}): {fields[:12]}")
                print(f"tables[{i}] data rows: {len(rows) if isinstance(rows, list) else 'N/A'}")


def fetch_one(date_str: str, code: str):
    roc = to_roc_date(date_str)

    # 保留我們真正送出去的參數，方便肉眼確認
    params = {
        "l": "zh-tw",
        "d": roc,
        "se": "EW",
        "o": "json",
    }

    print("\n============================================================")
    print(f"要求日期：{date_str}（民國 {roc}）")
    print("URL:", HIST_URL)
    print("params:", params)

    r = requests.get(HIST_URL, params=params, headers=HEADERS, timeout=TIMEOUT)
    print("HTTP:", r.status_code)
    print("實際 request URL:", r.url)
    r.raise_for_status()

    try:
        data = r.json()
    except Exception:
        print("❌ 回傳不是 JSON")
        print(r.text[:1000])
        return None

    print_response_shape(data, date_str)

    tables = data.get("tables", [])
    if not tables:
        print("❌ 找不到 tables")
        return None

    # 逐一搜尋各 table，不假設一定是 tables[0]
    candidates = []
    for ti, table in enumerate(tables):
        if not isinstance(table, dict):
            continue
        fields = table.get("fields", [])
        rows = table.get("data", [])
        if not isinstance(rows, list):
            continue

        # 先用固定位置抓；歷史行情常見欄位：
        # 0代號 1名稱 2收盤 3漲跌 4開盤 5最高 6最低 7成交股數
        for row in rows:
            if not isinstance(row, list) or len(row) < 8:
                continue
            row_code = clean_text(row[0])
            if row_code == code:
                candidates.append({
                    "table_index": ti,
                    "fields": fields,
                    "code": row_code,
                    "name": clean_text(row[1]),
                    "close": clean_num(row[2]),
                    "open": clean_num(row[4]),
                    "high": clean_num(row[5]),
                    "low": clean_num(row[6]),
                    "volume_shares": clean_num(row[7]),
                    "raw_row": row[:12],
                })

    if not candidates:
        print(f"❌ 找不到 {code}")
        return None

    item = candidates[0]
    print(f"\n✅ 找到 {code} {item['name']}（table {item['table_index']}）")
    print(
        "OHLCV:",
        f"open={item['open']}, high={item['high']}, low={item['low']}, "
        f"close={item['close']}, volume_shares={item['volume_shares']}"
    )
    print("raw row:", item["raw_row"])
    return item


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--code", default="3324")
    ap.add_argument("--date1", default="20260825")
    ap.add_argument("--date2", default="20260826")
    args = ap.parse_args()

    a = fetch_one(args.date1, args.code)
    b = fetch_one(args.date2, args.code)

    print("\n\n================ 最終判斷 ================")
    if not a or not b:
        print("❌ 至少有一天抓不到資料，所以目前不能判定歷史資料源可用。")
        sys.exit(2)

    fields = ["open", "high", "low", "close", "volume_shares"]
    diffs = {k: (a.get(k), b.get(k)) for k in fields if a.get(k) != b.get(k)}

    print(f"{args.date1}: "
          f"O={a['open']} H={a['high']} L={a['low']} C={a['close']} V={a['volume_shares']}")
    print(f"{args.date2}: "
          f"O={b['open']} H={b['high']} L={b['low']} C={b['close']} V={b['volume_shares']}")

    if diffs:
        print("\n✅ 兩天資料有差異。")
        print("差異欄位：")
        for k, (x, y) in diffs.items():
            print(f"  {k}: {x} → {y}")
        print("\n這代表這個查詢至少有回傳『不同的兩日資料』，才值得進一步拿來回補歷史。")
        sys.exit(0)
    else:
        print("\n🚨 兩天 OHLCV 完全一樣。")
        print("這非常可疑，代表日期參數可能被忽略、被 redirect、或資料源仍是最新快照。")
        print("先不要拿這個來源跑 260 天。")
        sys.exit(3)


if __name__ == "__main__":
    main()
