#!/usr/bin/env python3
"""
Yoyo 市場資料修補 v2

目的：
1. 上櫃成交量改採 TPEx dailyQuotes（完整成交量口徑）。
2. 保留原本已驗證過的上櫃 OHLC，只覆寫 volume，避免影響 KD/價格歷史。
3. 若某個 TPEx 交易日 raw 裡上市資料是 0 檔，自動向 TWSE 補回上市 OHLCV 與 TWSE 指數。
4. 更新 TPEx 指數。
5. 修補完成後重算最近 30 個交易日報告。

僅供資料整理與策略研究，不構成投資建議。
"""

import argparse
import json
import os
import time
from datetime import date

import requests

from fetch_and_compute import (
    RAW_DIR,
    HEADERS,
    fetch_twse,
    fetch_tpex_index,
    load_history,
    build_report,
    update_docs,
)


def _clean_num(v):
    if v is None:
        return None
    s = str(v).replace(",", "").strip()
    if s in ("", "--", "---", "----", "N/A"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def get_json_with_retry(url, params=None, retries=4, base_wait=1.5):
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(
                url,
                params=params,
                headers={
                    **HEADERS,
                    "Referer": "https://www.tpex.org.tw/",
                    "Accept": "application/json",
                },
                timeout=30,
            )
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as e:
            last_err = e
            if attempt < retries:
                wait = base_wait * attempt
                print(f"dailyQuotes 請求失敗 {attempt}/{retries}: {e}；{wait:.1f}s 後重試")
                time.sleep(wait)
    print(f"dailyQuotes 連續失敗，跳過：{last_err}")
    return None


def fetch_tpex_complete_volume(d: date):
    """抓 TPEx 官方 dailyQuotes，回傳 {股票代號: 完整成交量(張)}。"""
    url = "https://www.tpex.org.tw/www/zh-tw/afterTrading/dailyQuotes"
    params = {
        "date": d.strftime("%Y/%m/%d"),
        "type": "EW",
        "response": "json",
    }
    data = get_json_with_retry(url, params=params)
    if not data or data.get("stat") != "ok":
        print(f"TPEx 完整成交量 {d}：沒有有效資料，stat={None if not data else data.get('stat')}")
        return {}

    tables = data.get("tables", [])
    if not tables:
        print(f"TPEx 完整成交量 {d}：沒有 tables")
        return {}

    table = tables[0]
    fields = table.get("fields", [])
    rows = table.get("data", [])
    idx = {str(f).strip(): i for i, f in enumerate(fields)}

    code_i = idx.get("代號")
    vol_i = idx.get("成交股數")
    if code_i is None or vol_i is None:
        print(f"TPEx 完整成交量 {d}：欄位異常 fields={fields}")
        return {}

    out = {}
    for row in rows:
        try:
            code = str(row[code_i]).strip()
            if not (len(code) == 4 and code.isdigit()):
                continue
            shares = _clean_num(row[vol_i])
            if shares is None:
                continue
            out[code] = shares / 1000.0
        except (IndexError, TypeError):
            continue

    print(f"TPEx 完整成交量 {d}：成功解析 {len(out)} 檔")
    if "3324" in out:
        print(f"  3324 完整成交量 = {out['3324']:.3f} 張")
    return out


def repair_market_data(days: int):
    if not os.path.isdir(RAW_DIR):
        print("找不到 data/raw")
        return

    files = sorted(
        [fn for fn in os.listdir(RAW_DIR) if fn.endswith(".json")],
        reverse=True,
    )[:days]

    patched_days = 0
    volume_days = 0
    twse_refilled_days = 0
    skipped_days = 0

    for fn in files:
        path = os.path.join(RAW_DIR, fn)
        with open(path, encoding="utf-8") as f:
            snap = json.load(f)

        dstr = snap.get("date", fn.replace(".json", ""))
        try:
            d = date(int(dstr[:4]), int(dstr[4:6]), int(dstr[6:8]))
        except Exception:
            print(f"略過無效日期：{fn}")
            continue

        rows = snap.get("rows", [])
        tpex_rows = [r for r in rows if r.get("market") == "上櫃"]
        twse_rows = [r for r in rows if r.get("market") == "上市"]

        if not tpex_rows:
            skipped_days += 1
            continue

        changed = False

        # 1) 上櫃 volume 改為完整成交量口徑
        complete_vol = fetch_tpex_complete_volume(d)
        if complete_vol:
            changed_count = 0
            for r in tpex_rows:
                code = str(r.get("code", ""))
                if code in complete_vol:
                    new_v = complete_vol[code]
                    old_v = r.get("volume")
                    if old_v != new_v:
                        r["volume"] = new_v
                        changed_count += 1
            if changed_count:
                changed = True
            volume_days += 1
            print(f"✅ {dstr} 完整成交量套用：{changed_count}/{len(tpex_rows)} 檔有更新")

        # 2) TPEx 有交易、但 raw 裡上市是 0 檔 => 自動補回 TWSE
        if len(twse_rows) == 0:
            new_twse_rows, twse_index = fetch_twse(d)
            if new_twse_rows:
                rows = new_twse_rows + tpex_rows
                snap["rows"] = rows
                snap.setdefault("index", {})
                snap["index"]["TWSE"] = twse_index
                twse_refilled_days += 1
                changed = True
                print(f"✅ {dstr} 補回上市資料：{len(new_twse_rows)} 檔，TWSE={twse_index}")
            else:
                print(f"⚠️ {dstr} TPEx 有交易，但 TWSE 仍抓不到；先不亂補")
        else:
            snap["rows"] = twse_rows + tpex_rows

        # 3) TPEx 指數同步刷新
        tpex_index = fetch_tpex_index(d)
        if tpex_index is not None:
            snap.setdefault("index", {})
            if snap["index"].get("TPEx") != tpex_index:
                snap["index"]["TPEx"] = tpex_index
                changed = True

        if changed:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(snap, f, ensure_ascii=False)
            patched_days += 1

        time.sleep(0.7)

    print(
        f"市場資料修補完成：實際改寫 {patched_days} 日；"
        f"完整成交量成功 {volume_days} 日；"
        f"補回 TWSE {twse_refilled_days} 日；"
        f"略過休市/空檔 {skipped_days} 日"
    )

    # 重新產生最近 30 個交易日網站報告
    df, _ = load_history()
    if not df.empty:
        targets = sorted(df["date"].dt.strftime("%Y%m%d").unique())[-30:]
        for target in targets:
            report = build_report(target)
            if report is not None:
                update_docs(target, report)
                print(f"{target} 重新產生 {len(report)} 檔")

    # 驗證 3324 / 20260826
    raw_path = os.path.join(RAW_DIR, "20260826.json")
    if os.path.exists(raw_path):
        with open(raw_path, encoding="utf-8") as f:
            snap = json.load(f)
        r3324 = next((r for r in snap.get("rows", []) if r.get("market") == "上櫃" and r.get("code") == "3324"), None)
        if r3324:
            print(f"驗證 raw 3324 20260826：close={r3324.get('close')} volume={r3324.get('volume')} 張")

    report_path = os.path.join("docs", "data", "20260826.json")
    if os.path.exists(report_path):
        with open(report_path, encoding="utf-8") as f:
            report = json.load(f)
        r3324 = next((r for r in report if r.get("code") == "3324"), None)
        if r3324:
            print(
                "驗證 report 3324 20260826："
                f"vol20={r3324.get('vol20')} "
                f"RS5={r3324.get('rs5')} RS20={r3324.get('rs20')} "
                f"MACD130={r3324.get('macd130')}"
            )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=260)
    args = ap.parse_args()
    repair_market_data(args.days)


if __name__ == "__main__":
    main()
