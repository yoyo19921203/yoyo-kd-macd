#!/usr/bin/env python3
"""
KD + MACD 個人選股觀察 - 資料抓取與指標計算（僅供個人研究觀察，不構成投資建議）

用法：
    python scripts/fetch_and_compute.py                  # 只抓「今天」，平常排程用
    python scripts/fetch_and_compute.py --backfill 260   # 第一次建議回補 260 個交易日，讓 EMA130 有足夠歷史

資料來源：
    上市：證交所 MI_INDEX 公開資料
    上櫃：櫃買中心 daily_close_quotes 公開資料
"""
import argparse
import json
import os
import time
from datetime import date, timedelta

import pandas as pd
import requests

RAW_DIR = "data/raw"
DOCS_DATA_DIR = "docs/data"
HEADERS = {"User-Agent": "Mozilla/5.0"}


def get_json_with_retry(url: str, retries: int = 4, base_wait: float = 1.5):
    """HTTP GET with retry/backoff. Returns JSON dict, or None after repeated failure."""
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(url, headers=HEADERS, timeout=25)
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as e:
            last_err = e
            if attempt < retries:
                wait = base_wait * attempt
                print(f"請求失敗，第 {attempt}/{retries} 次：{e}；{wait:.1f}s 後重試")
                time.sleep(wait)
    print(f"連續 {retries} 次失敗，跳過此筆資料：{last_err}")
    return None

# MACD 參數：9 / 12 / 130
# 依策略定義：Signal=9、Fast EMA=12、Slow EMA=130。
# 測試版：Weighted Close → DIF=EMA12-EMA130 → MACD=EMA9(DIF)；篩選條件為 MACD > 0。
MACD_SIGNAL = 9
MACD_FAST = 12
MACD_SLOW = 130


def to_roc_date(d: date) -> str:
    return f"{d.year - 1911}/{d.month:02d}/{d.day:02d}"


def _num(row, idx, key):
    if key not in idx:
        return None
    v = row[idx[key]]
    if v is None:
        return None
    v = str(v).replace(",", "").strip()
    if v in ("", "--"):
        return None
    try:
        return float(v)
    except ValueError:
        return None


def fetch_twse(d: date):
    ymd = d.strftime("%Y%m%d")
    url = f"https://www.twse.com.tw/exchangeReport/MI_INDEX?response=json&date={ymd}&type=ALLBUT0999"
    data = get_json_with_retry(url)
    if not data:
        return [], None
    if data.get("stat") != "OK":
        return [], None
    rows, index_close = [], None
    for t in data.get("tables", []):
        fields = t.get("fields", [])
        if "證券代號" in fields and "收盤價" in fields:
            idx = {f: i for i, f in enumerate(fields)}
            for row in t.get("data", []):
                try:
                    code = row[idx["證券代號"]].strip()
                    if not (len(code) == 4 and code.isdigit()):
                        continue  # 只留一般股票代號，排除權證/牛熊證/受益證券等
                    o, h, l, c = (_num(row, idx, k) for k in ("開盤價", "最高價", "最低價", "收盤價"))
                    vol_shares = _num(row, idx, "成交股數")
                    if None in (o, h, l, c) or vol_shares is None:
                        continue
                    rows.append({
                        "market": "上市", "code": code, "name": row[idx["證券名稱"]].strip(),
                        "open": o, "high": h, "low": l, "close": c,
                        "volume": vol_shares / 1000.0,  # 換算成張
                    })
                except (KeyError, IndexError):
                    continue
        if "指數" in fields and "收盤指數" in fields:
            idx = {f: i for i, f in enumerate(fields)}
            for row in t.get("data", []):
                if row[idx["指數"]].strip() == "發行量加權股價指數":
                    v = _num(row, idx, "收盤指數")
                    if v is not None:
                        index_close = v
    return rows, index_close


def fetch_tpex(d: date):
    roc = to_roc_date(d)
    url = f"https://www.tpex.org.tw/web/stock/aftertrading/daily_close_quotes/stk_quote_result.php?l=zh-tw&d={roc}"
    data = get_json_with_retry(url)
    if not data:
        return []
    rows = []
    for t in data.get("tables", []):
        fields = t.get("fields", [])
        if "代號" in fields and "收盤" in fields:
            idx = {f: i for i, f in enumerate(fields)}
            for row in t.get("data", []):
                try:
                    code = row[idx["代號"]].strip()
                    if not (len(code) == 4 and code.isdigit()):
                        continue
                    o, h, l, c = (_num(row, idx, k) for k in ("開盤", "最高", "最低", "收盤"))
                    vol_shares = _num(row, idx, "成交股數")
                    if None in (o, h, l, c) or vol_shares is None:
                        continue
                    rows.append({
                        "market": "上櫃", "code": code, "name": row[idx["名稱"]].strip(),
                        "open": o, "high": h, "low": l, "close": c,
                        "volume": vol_shares / 1000.0,
                    })
                except (KeyError, IndexError):
                    continue
    # 已知限制：櫃買指數官方端點尚未串接，RS 計算目前上市/上櫃都先用同一個
    # 加權指數當基準，之後可以再換成真正的櫃買指數。
    return rows


def fetch_one_day(d: date):
    twse_rows, twse_index = fetch_twse(d)
    tpex_rows = fetch_tpex(d)
    if not twse_rows and not tpex_rows:
        return None
    return {
        "date": d.strftime("%Y%m%d"),
        "index": {"TWSE": twse_index},
        "rows": twse_rows + tpex_rows,
    }


def backfill(days: int):
    os.makedirs(RAW_DIR, exist_ok=True)
    d = date.today()
    got, checked = 0, 0
    while got < days and checked < days * 3:
        path = f"{RAW_DIR}/{d.strftime('%Y%m%d')}.json"
        if not os.path.exists(path):
            snap = fetch_one_day(d)
            if snap:
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(snap, f, ensure_ascii=False)
                got += 1
                print(f"抓到 {d}：{len(snap['rows'])} 檔")
            time.sleep(1.0)
        else:
            got += 1
        checked += 1
        d -= timedelta(days=1)
    print(f"回補完成：成功取得/已有 {got} 個交易日資料，共檢查 {checked} 個日曆日")


def fetch_today():
    os.makedirs(RAW_DIR, exist_ok=True)
    d = date.today()
    path = f"{RAW_DIR}/{d.strftime('%Y%m%d')}.json"
    snap = fetch_one_day(d)
    if snap:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(snap, f, ensure_ascii=False)
        print(f"抓到 {d}：{len(snap['rows'])} 檔")
    else:
        print(f"{d} 沒有資料（可能是假日或尚未收盤）")


# ---------- 指標計算 ----------

def load_history():
    if not os.path.isdir(RAW_DIR):
        return pd.DataFrame(), {}
    frames, index_map = [], {}
    for fn in sorted(os.listdir(RAW_DIR)):
        with open(f"{RAW_DIR}/{fn}", encoding="utf-8") as f:
            snap = json.load(f)
        d = snap["date"]
        for row in snap["rows"]:
            frames.append({**row, "date": d})
        if snap.get("index", {}).get("TWSE"):
            index_map[d] = snap["index"]["TWSE"]
    if not frames:
        return pd.DataFrame(), index_map
    df = pd.DataFrame(frames)
    df["date"] = pd.to_datetime(df["date"], format="%Y%m%d")
    return df, index_map


def compute_kd(g: pd.DataFrame):
    low9 = g["low"].rolling(9, min_periods=9).min()
    high9 = g["high"].rolling(9, min_periods=9).max()
    denom = (high9 - low9).replace(0, pd.NA)
    rsv = (g["close"] - low9) / denom * 100
    k = pd.Series(index=g.index, dtype=float)
    dd = pd.Series(index=g.index, dtype=float)
    prev_k, prev_d = 50.0, 50.0
    for i in g.index:
        r = rsv.loc[i]
        if pd.isna(r):
            k.loc[i], dd.loc[i] = float("nan"), float("nan")
            continue
        cur_k = prev_k * 2 / 3 + r * 1 / 3
        cur_d = prev_d * 2 / 3 + cur_k * 1 / 3
        k.loc[i], dd.loc[i] = cur_k, cur_d
        prev_k, prev_d = cur_k, cur_d
    return k, dd


def compute_macd(g: pd.DataFrame):
    """
    測試版 MACD(9,12,130)
    Weighted Close = (2*Close + High + Low) / 4
    DIF = EMA12(Weighted Close) - EMA130(Weighted Close)
    MACD = EMA9(DIF)
    OSC = DIF - MACD

    本測試版的篩選條件：MACD > 0
    """
    weighted_close = (g["close"] * 2 + g["high"] + g["low"]) / 4
    ema_fast = weighted_close.ewm(span=MACD_FAST, adjust=False).mean()
    ema_slow = weighted_close.ewm(span=MACD_SLOW, adjust=False).mean()
    dif = ema_fast - ema_slow
    macd_signal = dif.ewm(span=MACD_SIGNAL, adjust=False).mean()
    osc = dif - macd_signal
    return dif, macd_signal, osc


def build_report(target_date_str: str, lookback_cross_days: int = 5):
    df, index_map = load_history()
    if df.empty:
        print("目前沒有任何歷史資料，請先跑 --backfill")
        return None

    target_dt = pd.to_datetime(target_date_str, format="%Y%m%d")
    df = df[df["date"] <= target_dt].copy()
    if df.empty:
        print(f"{target_date_str} 之前沒有歷史資料")
        return None

    df = df.sort_values(["code", "date"])
    out_rows = []
    for code, g in df.groupby("code"):
        g = g.reset_index(drop=True)
        if g["date"].iloc[-1].strftime("%Y%m%d") != target_date_str:
            continue
        k, dd = compute_kd(g)
        dif, macd_signal, osc = compute_macd(g)
        gap = k - dd
        cross = (k > dd) & (k.shift(1) <= dd.shift(1)) & k.notna() & dd.notna() & k.shift(1).notna()
        if not cross.any():
            continue
        last_cross_idx = cross[cross].index.max()
        days_since_cross = len(g) - 1 - last_cross_idx
        if days_since_cross > lookback_cross_days:
            continue
        widening = bool(gap.iloc[-1] > gap.iloc[last_cross_idx]) if pd.notna(gap.iloc[-1]) else False
        vol5 = g["volume"].rolling(5).mean().iloc[-1]
        vol10 = g["volume"].rolling(10).mean().iloc[-1]
        vol20 = g["volume"].rolling(20).mean().iloc[-1]
        dates_list = [dt.strftime("%Y%m%d") for dt in g["date"]]

        def idx_return(n):
            if len(g) <= n:
                return None
            i_now, i_prev = index_map.get(dates_list[-1]), index_map.get(dates_list[-1 - n])
            if not i_now or not i_prev:
                return None
            return (i_now / i_prev - 1) * 100

        def stock_return(n):
            if len(g) <= n:
                return None
            return (g["close"].iloc[-1] / g["close"].iloc[-1 - n] - 1) * 100

        def rs(n):
            sr, ir = stock_return(n), idx_return(n)
            return round(sr - ir, 2) if (sr is not None and ir is not None) else None

        out_rows.append({
            "market": g["market"].iloc[-1], "code": code, "name": g["name"].iloc[-1],
            "kd_date": g["date"].iloc[last_cross_idx].strftime("%m/%d"),
            "close": g["close"].iloc[-1],
            "days_since_cross": int(days_since_cross) + 1,
            "vol_ok": bool(vol5 > vol10) if pd.notna(vol5) and pd.notna(vol10) else False,
            "vol20": round(vol20, 0) if pd.notna(vol20) else None,
            "kd3_strong": widening,
            # 測試版：MACD130 欄位改代表「MACD 線（Signal 9）」而不是 DIF
            "dif130": round(float(dif.iloc[-1]), 6) if pd.notna(dif.iloc[-1]) else None,
            "macd130": round(float(macd_signal.iloc[-1]), 6) if pd.notna(macd_signal.iloc[-1]) else None,
            "osc130": round(float(osc.iloc[-1]), 6) if pd.notna(osc.iloc[-1]) else None,
            "macd130_positive": bool(macd_signal.iloc[-1] > 0) if pd.notna(macd_signal.iloc[-1]) else False,
            "macd_positive": bool(macd_signal.iloc[-1] > 0) if pd.notna(macd_signal.iloc[-1]) else False,  # 相容舊前端
            "rs5": rs(5), "rs20": rs(20),
        })
    out_rows.sort(key=lambda r: (not r["kd3_strong"], not r["macd130_positive"], not r["vol_ok"]))
    return out_rows


def update_docs(target_date_str: str, rows):
    os.makedirs(DOCS_DATA_DIR, exist_ok=True)
    with open(f"{DOCS_DATA_DIR}/{target_date_str}.json", "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False)
    dates_path = f"{DOCS_DATA_DIR}/dates.json"
    dates = []
    if os.path.exists(dates_path):
        with open(dates_path, encoding="utf-8") as f:
            dates = json.load(f)
    if target_date_str not in dates:
        dates.append(target_date_str)
    dates = sorted(set(dates))[-30:]
    with open(dates_path, "w", encoding="utf-8") as f:
        json.dump(dates, f, ensure_ascii=False)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill", type=int, default=0, help="回補最近 N 個交易日歷史")
    ap.add_argument("--report-date", type=str, default="", help="只重算指定日期，例如 20260826")
    args = ap.parse_args()

    if args.report_date:
        target = args.report_date
        rows = build_report(target)
        if rows is not None:
            update_docs(target, rows)
            print(f"{target} 產生 {len(rows)} 檔觀察名單")
        return

    if args.backfill:
        backfill(args.backfill)

        # 重算最近 30 個已有交易日，方便直接比較歷史日期（例如 20260826）
        df, _ = load_history()
        if df.empty:
            return
        report_dates = sorted(df["date"].dt.strftime("%Y%m%d").unique())[-30:]
        for target in report_dates:
            rows = build_report(target)
            if rows is not None:
                update_docs(target, rows)
                print(f"{target} 重新產生 {len(rows)} 檔觀察名單")
    else:
        fetch_today()
        target = date.today().strftime("%Y%m%d")
        rows = build_report(target)
        if rows is not None:
            update_docs(target, rows)
            print(f"{target} 產生 {len(rows)} 檔觀察名單")


if __name__ == "__main__":
    main()
