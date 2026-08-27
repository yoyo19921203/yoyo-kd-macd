#!/usr/bin/env python3
"""
Yoyo KD + MACD 多策略選股觀察
僅供資料整理與策略研究，不構成投資建議。

功能：
1. 原 KD + MACD 觀察
2. 底部萌芽：跌深、築底、均線收斂、MACD 負值收斂
3. 底部轉強：萌芽 + KD 低檔黃金交叉 + 站回 MA5
4. 底部確認：轉強 + 接近/站回 MA20 + 量能改善
5. --report-date YYYYMMDD 指定日期重算
6. --backtest 回測底部策略 5/10/20 日後表現
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

MACD_SIGNAL = 9
MACD_FAST = 12
MACD_SLOW = 130

# 底部轉強策略門檻（可之後依回測再調）
BOTTOM_DROP_MIN = 15.0          # 近60日高點至目前至少回檔15%
BOTTOM_NEAR_LOW_MAX = 10.0      # 現價距近20日低點不超過10%
BOTTOM_RANGE10_MAX = 12.0       # 近10日高低振幅不超過12%
BOTTOM_MA_CONVERGE_MAX = 5.0    # MA5/10/20 最大差距不超過5%
BOTTOM_KD_MAX = 45.0            # KD低檔轉強時 K/D 盡量在45以下
BOTTOM_KD_LOOKBACK = 3          # 黃金交叉最近3個交易日內


def get_json_with_retry(url: str, retries: int = 4, base_wait: float = 1.5):
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
    print(f"連續 {retries} 次失敗，跳過：{last_err}")
    return None


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


def is_ordinary_stock(code: str, name: str) -> bool:
    """保留一般股票；排除常見 ETF/基金類 00xx，其他非4碼已在資料抓取時排除。"""
    if not (len(code) == 4 and code.isdigit()):
        return False
    if code.startswith("00"):
        return False
    bad_words = ("ETF", "ETN", "指數", "槓桿", "反向")
    return not any(w in (name or "") for w in bad_words)


def fetch_twse(d: date):
    ymd = d.strftime("%Y%m%d")
    url = f"https://www.twse.com.tw/exchangeReport/MI_INDEX?response=json&date={ymd}&type=ALLBUT0999"
    data = get_json_with_retry(url)
    if not data or data.get("stat") != "OK":
        return [], None
    rows, index_close = [], None
    for t in data.get("tables", []):
        fields = t.get("fields", [])
        if "證券代號" in fields and "收盤價" in fields:
            idx = {f: i for i, f in enumerate(fields)}
            for row in t.get("data", []):
                try:
                    code = row[idx["證券代號"]].strip()
                    name = row[idx["證券名稱"]].strip()
                    if not (len(code) == 4 and code.isdigit()):
                        continue
                    o, h, l, c = (_num(row, idx, k) for k in ("開盤價", "最高價", "最低價", "收盤價"))
                    vol_shares = _num(row, idx, "成交股數")
                    if None in (o, h, l, c) or vol_shares is None:
                        continue
                    rows.append({
                        "market": "上市", "code": code, "name": name,
                        "open": o, "high": h, "low": l, "close": c,
                        "volume": vol_shares / 1000.0,
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
    """
    抓指定日期的 TPEx 上櫃歷史日行情。

    TPEx historical endpoint 的 JSON 主要資料通常在 tables[0]。
    欄位常見順序：
      0 代號
      1 名稱
      2 收盤
      3 漲跌
      4 開盤
      5 最高
      6 最低
      7 成交股數
    因此這裡不再要求欄位名稱必須「完全等於」特定字串，
    避免 TPEx 回傳欄位名稱帶空白/HTML/不同文字時整批失敗。
    """
    roc = to_roc_date(d)
    url = (
        "https://www.tpex.org.tw/web/stock/aftertrading/"
        "otc_quotes_no1430/stk_wn1430_result.php"
        f"?l=zh-tw&d={roc}&se=EW&o=json"
    )
    data = get_json_with_retry(url)
    if not data:
        print(f"TPEx {d}：沒有 JSON 回應")
        return []

    tables = data.get("tables", [])
    if not tables:
        print(f"TPEx {d}：JSON 沒有 tables；keys={list(data.keys())}")
        return []

    # 歷史日行情主表通常就是 tables[0]
    target_table = tables[0]
    fields = target_table.get("fields", [])
    raw_rows = target_table.get("data", [])

    if not raw_rows:
        print(f"TPEx {d}：tables[0] 沒有 data；fields={fields}")
        return []

    def clean_num(v):
        if v is None:
            return None
        s = str(v).replace(",", "").strip()
        # 清掉可能混入的 HTML 標記
        import re
        s = re.sub(r"<[^>]*>", "", s).strip()
        if s in ("", "--", "---", "----", "N/A"):
            return None
        try:
            return float(s)
        except ValueError:
            return None

    def clean_text(v):
        import re
        return re.sub(r"<[^>]*>", "", str(v or "")).strip()

    rows = []

    # 第一優先：官方歷史行情目前通用的固定欄位順序。
    if len(fields) >= 8:
        for row in raw_rows:
            if len(row) < 8:
                continue
            try:
                code = clean_text(row[0])
                name = clean_text(row[1])
                if not (len(code) == 4 and code.isdigit()):
                    continue

                c = clean_num(row[2])
                o = clean_num(row[4])
                h = clean_num(row[5])
                l = clean_num(row[6])
                vol_shares = clean_num(row[7])

                if None in (o, h, l, c) or vol_shares is None:
                    continue

                rows.append({
                    "market": "上櫃",
                    "code": code,
                    "name": name,
                    "open": o,
                    "high": h,
                    "low": l,
                    "close": c,
                    "volume": vol_shares / 1000.0,
                })
            except (IndexError, TypeError):
                continue

    # 若固定位置完全抓不到，才用「模糊欄位名稱」備援。
    if not rows and fields:
        norm_fields = []
        import re
        for f in fields:
            s = re.sub(r"<[^>]*>", "", str(f or ""))
            s = s.replace(" ", "").replace("\n", "").strip()
            norm_fields.append(s)

        def find_col(*keywords):
            for i, f in enumerate(norm_fields):
                if any(k in f for k in keywords):
                    return i
            return None

        ci = find_col("代號")
        ni = find_col("名稱")
        close_i = find_col("收盤")
        open_i = find_col("開盤")
        high_i = find_col("最高")
        low_i = find_col("最低")
        vol_i = find_col("成交股數", "成交量")

        if None not in (ci, ni, close_i, open_i, high_i, low_i, vol_i):
            for row in raw_rows:
                try:
                    code = clean_text(row[ci])
                    name = clean_text(row[ni])
                    if not (len(code) == 4 and code.isdigit()):
                        continue
                    c = clean_num(row[close_i])
                    o = clean_num(row[open_i])
                    h = clean_num(row[high_i])
                    l = clean_num(row[low_i])
                    vol_shares = clean_num(row[vol_i])
                    if None in (o, h, l, c) or vol_shares is None:
                        continue
                    rows.append({
                        "market": "上櫃",
                        "code": code,
                        "name": name,
                        "open": o,
                        "high": h,
                        "low": l,
                        "close": c,
                        "volume": vol_shares / 1000.0,
                    })
                except (IndexError, TypeError):
                    continue

    print(
        f"TPEx {d}：fields={len(fields)} 欄、raw={len(raw_rows)} 列、"
        f"成功解析 {len(rows)} 檔上櫃股票"
    )
    if rows:
        sample = ", ".join(f"{r['code']} {r['name']}" for r in rows[:5])
        print(f"TPEx {d} 範例：{sample}")
    else:
        print(f"TPEx {d} 解析失敗；fields={fields[:12]}")

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



def repair_tpex(days: int):
    """
    修補最近 N 個 raw 日期檔的 TPEx 上櫃歷史資料。

    規則：
    1. TPEx 指定日期有歷史行情：
       保留原本上市資料，移除舊上櫃資料，改寫成該日真正的上櫃歷史行情。
    2. TPEx 沒資料，且該 raw 檔也沒有上市交易資料：
       視為週末/休市日，清掉可能被錯誤快照污染的上櫃 rows。
    3. TPEx 沒資料，但該日有上市交易資料：
       視為異常，保留原檔並警告，不亂刪。
    """
    if not os.path.isdir(RAW_DIR):
        print("找不到 data/raw，請先有歷史資料")
        return

    files = sorted(
        [fn for fn in os.listdir(RAW_DIR) if fn.endswith(".json")],
        reverse=True
    )[:days]

    repaired = 0
    cleaned_closed = 0
    failed = 0

    for fn in files:
        path = f"{RAW_DIR}/{fn}"
        with open(path, encoding="utf-8") as f:
            snap = json.load(f)

        dstr = snap.get("date", fn.replace(".json", ""))
        try:
            d = date(int(dstr[:4]), int(dstr[4:6]), int(dstr[6:8]))
        except Exception:
            print(f"略過無效日期檔：{fn}")
            continue

        old_rows = snap.get("rows", [])
        twse_rows = [r for r in old_rows if r.get("market") != "上櫃"]
        old_tpex_rows = [r for r in old_rows if r.get("market") == "上櫃"]

        tpex_rows = fetch_tpex(d)

        if tpex_rows:
            merged = {}
            for r in twse_rows + tpex_rows:
                merged[(r.get("market"), r.get("code"))] = r

            snap["rows"] = list(merged.values())

            with open(path, "w", encoding="utf-8") as f:
                json.dump(snap, f, ensure_ascii=False)

            repaired += 1
            print(
                f"✅ {dstr} 交易日修補完成：上市 {len(twse_rows)} 檔 + "
                f"上櫃 {len(tpex_rows)} 檔 = {len(snap['rows'])} 檔"
            )
        else:
            # 沒有 TPEx 歷史資料，而且這天連 TWSE 也沒有股票資料，
            # 就視為休市日。把可能被舊錯誤快照塞進去的上櫃 rows 清掉。
            if len(twse_rows) == 0:
                if old_tpex_rows:
                    snap["rows"] = []
                    with open(path, "w", encoding="utf-8") as f:
                        json.dump(snap, f, ensure_ascii=False)
                    cleaned_closed += 1
                    print(
                        f"🧹 {dstr} 判定為休市日：已清除錯誤上櫃快照 "
                        f"{len(old_tpex_rows)} 檔"
                    )
                else:
                    print(f"休市日 {dstr}：本來就沒有 rows")
            else:
                failed += 1
                print(
                    f"⚠️ {dstr} 有上市 {len(twse_rows)} 檔但 TPEx 抓不到，"
                    f"視為異常，保留原檔不動"
                )

        time.sleep(0.8)

    print(
        f"TPEx 修補完成：交易日成功 {repaired} 日，"
        f"休市日清理 {cleaned_closed} 日，異常失敗 {failed} 日"
    )

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


def load_history():
    if not os.path.isdir(RAW_DIR):
        return pd.DataFrame(), {}
    frames, index_map = [], {}
    for fn in sorted(os.listdir(RAW_DIR)):
        if not fn.endswith(".json"):
            continue
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
        cur_k = prev_k * 2 / 3 + r / 3
        cur_d = prev_d * 2 / 3 + cur_k / 3
        k.loc[i], dd.loc[i] = cur_k, cur_d
        prev_k, prev_d = cur_k, cur_d
    return k, dd


def compute_macd(g: pd.DataFrame):
    weighted_close = (g["close"] * 2 + g["high"] + g["low"]) / 4
    ema_fast = weighted_close.ewm(span=MACD_FAST, adjust=False).mean()
    ema_slow = weighted_close.ewm(span=MACD_SLOW, adjust=False).mean()
    dif = ema_fast - ema_slow
    macd_signal = dif.ewm(span=MACD_SIGNAL, adjust=False).mean()
    osc = dif - macd_signal
    return dif, macd_signal, osc


def enrich_group(g: pd.DataFrame) -> pd.DataFrame:
    g = g.sort_values("date").reset_index(drop=True).copy()
    k, d = compute_kd(g)
    dif, macd, osc = compute_macd(g)
    g["k"] = k
    g["d"] = d
    g["kd_gap"] = k - d
    g["kd_cross"] = (k > d) & (k.shift(1) <= d.shift(1)) & k.notna() & d.notna()
    g["dif130"] = dif
    g["macd130"] = macd
    g["osc130"] = osc

    for n in (5, 10, 20):
        g[f"ma{n}"] = g["close"].rolling(n).mean()
    g["vol5"] = g["volume"].rolling(5).mean()
    g["vol10"] = g["volume"].rolling(10).mean()
    g["vol20"] = g["volume"].rolling(20).mean()

    high60 = g["high"].rolling(60, min_periods=40).max()
    low20 = g["low"].rolling(20, min_periods=15).min()
    high10 = g["high"].rolling(10, min_periods=8).max()
    low10 = g["low"].rolling(10, min_periods=8).min()

    g["drop60"] = (high60 / g["close"] - 1) * 100
    g["near_low20"] = (g["close"] / low20 - 1) * 100
    g["range10"] = (high10 / low10 - 1) * 100

    ma_max = g[["ma5", "ma10", "ma20"]].max(axis=1)
    ma_min = g[["ma5", "ma10", "ma20"]].min(axis=1)
    ma_mid = g[["ma5", "ma10", "ma20"]].mean(axis=1)
    g["ma_converge"] = (ma_max - ma_min) / ma_mid * 100

    # 最近 KD 交叉距今天幾個交易日
    cross_age = []
    last_cross = None
    for i, v in enumerate(g["kd_cross"].fillna(False)):
        if v:
            last_cross = i
        cross_age.append(None if last_cross is None else i - last_cross)
    g["kd_cross_age"] = cross_age

    # MACD/OSC 收斂：仍在零軸附近/下方，但連續改善
    g["dif_rising"] = (g["dif130"] > g["dif130"].shift(1)) & (g["dif130"].shift(1) >= g["dif130"].shift(2))
    g["osc_rising"] = (g["osc130"] > g["osc130"].shift(1)) & (g["osc130"].shift(1) >= g["osc130"].shift(2))

    base = (
        (g["drop60"] >= BOTTOM_DROP_MIN) &
        (g["near_low20"] <= BOTTOM_NEAR_LOW_MAX) &
        (g["range10"] <= BOTTOM_RANGE10_MAX) &
        (g["ma_converge"] <= BOTTOM_MA_CONVERGE_MAX) &
        (g["dif130"] < 0) &
        (g["macd130"] < 0) &
        g["dif_rising"] &
        g["osc_rising"]
    )
    g["bottom_seed"] = base.fillna(False)

    kd_turn = (
        g["kd_cross_age"].notna() &
        (g["kd_cross_age"] <= BOTTOM_KD_LOOKBACK) &
        (g["k"] > g["d"]) &
        (g[["k", "d"]].max(axis=1) <= BOTTOM_KD_MAX)
    )
    g["bottom_turn"] = (g["bottom_seed"] & kd_turn & (g["close"] >= g["ma5"] * 0.98)).fillna(False)

    price_confirm = (g["close"] >= g["ma10"] * 0.99) & (g["close"] >= g["ma20"] * 0.97)
    vol_confirm = g["vol5"] >= g["vol10"]
    g["bottom_confirm"] = (g["bottom_turn"] & price_confirm & vol_confirm).fillna(False)

    # 0~100 分，方便排序，不當作買賣訊號
    score = pd.Series(0.0, index=g.index)
    score += (g["drop60"] >= 15).astype(int) * 15
    score += (g["near_low20"] <= 10).astype(int) * 15
    score += (g["range10"] <= 12).astype(int) * 10
    score += (g["ma_converge"] <= 5).astype(int) * 10
    score += g["dif_rising"].astype(int) * 15
    score += g["osc_rising"].astype(int) * 10
    score += kd_turn.astype(int) * 15
    score += (g["close"] >= g["ma5"] * 0.98).astype(int) * 5
    score += vol_confirm.fillna(False).astype(int) * 5
    g["bottom_score"] = score
    return g


def _latest_cross_idx(g):
    xs = g.index[g["kd_cross"].fillna(False)]
    return int(xs.max()) if len(xs) else None


def build_report(target_date_str: str, old_lookback_days: int = 5):
    df, index_map = load_history()
    if df.empty:
        print("目前沒有任何歷史資料，請先跑 --backfill")
        return None

    target_dt = pd.to_datetime(target_date_str, format="%Y%m%d")
    df = df[df["date"] <= target_dt].copy()
    df = df.sort_values(["code", "date"])
    out_rows = []

    for code, raw in df.groupby("code"):
        g = raw.reset_index(drop=True)
        if g["date"].iloc[-1].strftime("%Y%m%d") != target_date_str:
            continue
        name = g["name"].iloc[-1]
        if not is_ordinary_stock(code, name):
            continue
        if len(g) < 40:
            continue

        g = enrich_group(g)
        r = g.iloc[-1]
        last_cross_idx = _latest_cross_idx(g)
        if last_cross_idx is None:
            cross_age = None
            old_candidate = False
            kd_date = "-"
        else:
            cross_age = len(g) - 1 - last_cross_idx
            old_candidate = cross_age <= old_lookback_days
            kd_date = g["date"].iloc[last_cross_idx].strftime("%m/%d")

        # 原 KD3：目前 K>D 且 K-D gap 比前一交易日擴大
        kd3 = False
        if len(g) >= 2 and pd.notna(r["kd_gap"]) and pd.notna(g["kd_gap"].iloc[-2]):
            kd3 = bool(r["k"] > r["d"] and r["kd_gap"] > g["kd_gap"].iloc[-2])

        vol_ok = bool(r["vol5"] > r["vol10"]) if pd.notna(r["vol5"]) and pd.notna(r["vol10"]) else False
        macd_ok = bool(r["macd130"] > 0) if pd.notna(r["macd130"]) else False

        # 只輸出至少符合一種策略的候選，避免整個市場太肥
        if not (old_candidate or r["bottom_seed"] or r["bottom_turn"] or r["bottom_confirm"]):
            continue

        dates_list = [dt.strftime("%Y%m%d") for dt in g["date"]]
        def stock_return(n):
            if len(g) <= n:
                return None
            return (g["close"].iloc[-1] / g["close"].iloc[-1-n] - 1) * 100
        def idx_return(n):
            if len(g) <= n:
                return None
            a, b = index_map.get(dates_list[-1]), index_map.get(dates_list[-1-n])
            if not a or not b:
                return None
            return (a / b - 1) * 100
        def rs(n):
            sr, ir = stock_return(n), idx_return(n)
            return round(sr-ir, 2) if sr is not None and ir is not None else None

        out_rows.append({
            "market": r["market"], "code": code, "name": name,
            "close": round(float(r["close"]), 2),
            "kd_date": kd_date,
            "days_since_cross": None if cross_age is None else int(cross_age)+1,
            "old_candidate": bool(old_candidate),
            "vol_ok": vol_ok,
            "vol20": round(float(r["vol20"]), 0) if pd.notna(r["vol20"]) else None,
            "kd3_strong": kd3,
            "macd130_positive": macd_ok,
            "dif130": round(float(r["dif130"]), 4) if pd.notna(r["dif130"]) else None,
            "macd130": round(float(r["macd130"]), 4) if pd.notna(r["macd130"]) else None,
            "osc130": round(float(r["osc130"]), 4) if pd.notna(r["osc130"]) else None,
            "k": round(float(r["k"]), 2) if pd.notna(r["k"]) else None,
            "d": round(float(r["d"]), 2) if pd.notna(r["d"]) else None,
            "ma5": round(float(r["ma5"]), 2) if pd.notna(r["ma5"]) else None,
            "ma10": round(float(r["ma10"]), 2) if pd.notna(r["ma10"]) else None,
            "ma20": round(float(r["ma20"]), 2) if pd.notna(r["ma20"]) else None,
            "drop60": round(float(r["drop60"]), 1) if pd.notna(r["drop60"]) else None,
            "range10": round(float(r["range10"]), 1) if pd.notna(r["range10"]) else None,
            "ma_converge": round(float(r["ma_converge"]), 1) if pd.notna(r["ma_converge"]) else None,
            "bottom_seed": bool(r["bottom_seed"]),
            "bottom_turn": bool(r["bottom_turn"]),
            "bottom_confirm": bool(r["bottom_confirm"]),
            "bottom_score": int(r["bottom_score"]),
            "rs5": rs(5), "rs20": rs(20),
        })

    out_rows.sort(key=lambda x: (
        not x["bottom_confirm"],
        not x["bottom_turn"],
        not x["bottom_seed"],
        -x["bottom_score"],
        not x["kd3_strong"],
        not x["vol_ok"],
    ))
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


def run_backtest():
    """以既有 raw 歷史資料回測底部策略首次觸發日後 5/10/20 日報酬。"""
    df, _ = load_history()
    if df.empty:
        print("沒有歷史資料可回測")
        return
    results = {"seed": [], "turn": [], "confirm": []}

    for code, raw in df.sort_values(["code","date"]).groupby("code"):
        raw = raw.reset_index(drop=True)
        name = raw["name"].iloc[-1]
        if not is_ordinary_stock(code, name) or len(raw) < 70:
            continue
        g = enrich_group(raw)
        for key, col in [("seed","bottom_seed"),("turn","bottom_turn"),("confirm","bottom_confirm")]:
            flag = g[col].fillna(False)
            first = flag & ~flag.shift(1, fill_value=False)
            for i in g.index[first]:
                item = {"code":code, "date":g.loc[i,"date"].strftime("%Y%m%d")}
                for n in (5,10,20):
                    if i+n < len(g):
                        item[f"r{n}"] = (g.loc[i+n,"close"]/g.loc[i,"close"]-1)*100
                    else:
                        item[f"r{n}"] = None
                results[key].append(item)

    summary = {}
    for key, arr in results.items():
        s = {"signals": len(arr)}
        for n in (5,10,20):
            vals = [x[f"r{n}"] for x in arr if x[f"r{n}"] is not None]
            s[f"n{n}"] = len(vals)
            s[f"avg{n}"] = round(sum(vals)/len(vals),2) if vals else None
            s[f"win{n}"] = round(sum(v>0 for v in vals)/len(vals)*100,1) if vals else None
        summary[key] = s

    os.makedirs(DOCS_DATA_DIR, exist_ok=True)
    with open(f"{DOCS_DATA_DIR}/backtest.json","w",encoding="utf-8") as f:
        json.dump({"summary":summary}, f, ensure_ascii=False)
    print("回測完成：", summary)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill", type=int, default=0)
    ap.add_argument("--report-date", type=str, default="")
    ap.add_argument("--backtest", action="store_true")
    ap.add_argument("--repair-tpex", type=int, default=0, help="修補最近 N 個 raw 日期檔的上櫃歷史資料")
    args = ap.parse_args()

    if args.repair_tpex:
        repair_tpex(args.repair_tpex)
        df, _ = load_history()
        if not df.empty:
            for target in sorted(df["date"].dt.strftime("%Y%m%d").unique())[-30:]:
                rows = build_report(target)
                if rows is not None:
                    update_docs(target, rows)
                    print(f"{target} 修補後重新產生 {len(rows)} 檔")
        return

    if args.backtest:
        run_backtest()
        return

    if args.report_date:
        rows = build_report(args.report_date)
        if rows is not None:
            update_docs(args.report_date, rows)
            print(f"{args.report_date} 產生 {len(rows)} 檔多策略候選")
        return

    if args.backfill:
        backfill(args.backfill)
        df, _ = load_history()
        if df.empty:
            return
        for target in sorted(df["date"].dt.strftime("%Y%m%d").unique())[-30:]:
            rows = build_report(target)
            if rows is not None:
                update_docs(target, rows)
                print(f"{target} 重新產生 {len(rows)} 檔")
        run_backtest()
    else:
        fetch_today()
        target = date.today().strftime("%Y%m%d")
        rows = build_report(target)
        if rows is not None:
            update_docs(target, rows)
            print(f"{target} 產生 {len(rows)} 檔多策略候選")


if __name__ == "__main__":
    main()
