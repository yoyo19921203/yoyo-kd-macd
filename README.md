# KD + MACD 個人選股觀察（私人使用）

僅供個人資料整理與策略觀察，不構成任何投資建議。

## 這是什麼

每個交易日自動抓取台股上市／上櫃的收盤資料，計算 KD、MACD、量能、相對強度（RS）等技術指標，
把「近期出現 KD 黃金交叉」的股票整理成一個網頁報告，密碼保護，只有知道密碼的人看得到。

## 運作方式

1. `.github/workflows/daily.yml` 這個排程，會在每個平日台灣時間下午 3:30 自動執行。
2. 排程會執行 `scripts/fetch_and_compute.py`：抓證交所/櫃買中心當天收盤資料 → 存進 `data/raw/` →
   用過去累積的歷史資料算出 KD/MACD/量能/RS → 產生 `docs/data/` 底下的 JSON 報告。
3. GitHub Pages 直接把 `docs/` 資料夾發布成網頁，`docs/index.html` 讀取 JSON 顯示成表格。
4. 全部都在 GitHub 的伺服器上跑，不需要你的電腦開著。

## 第一次使用

1. 到 GitHub Actions 頁面，手動執行一次 `daily.yml`，`backfill_days` 填 `260`
   （回補最近 260 個交易日左右的歷史資料，KD/MACD 才會準）。第一次會跑比較久（幾分鐘），因為要抓約 260 個交易日的資料。
2. 之後排程就會每個交易日自動抓「當天」，不用再手動跑。

## 修改密碼

網頁密碼存的是 SHA-256 雜湊值（`docs/index.html` 裡的 `PW_HASH`），原始密碼不會出現在程式碼裡。
預設密碼是 `yoyo2026`，**建議盡快更換**——把新密碼告訴 Claude，請它幫你算出雜湊值，
貼到 `docs/index.html` 的 `PW_HASH` 那一行即可。

## 已知限制（之後可以再優化）

- 上櫃股票的相對強度（RS）目前先用「加權指數」當基準，還沒接上真正的「櫃買指數」，
  上市股票的 RS 是準的，上櫃股票的 RS 會有一點誤差。
- KD3 強勢的判斷邏輯（黃金交叉後 K-D 差距是否持續擴大）是簡化版本，跟你先生原本工具的細節不一定完全一樣。
- 剛跑起來的前一兩個月，因為歷史資料還不夠長，KD/MACD 數字會比較不穩定，之後會越來越準。

## 重要提醒

這個工具只是把技術指標「客觀計算出來」，不代表這些股票明天會漲、也不是買賣建議。
出現在名單上只代表「符合設定的技術條件」，進場與否要自己判斷風險。


## 這版已修正

- GitHub Actions 已移到正確位置：`.github/workflows/daily.yml`。
- 密碼輸入框改成文字鍵盤，手機可以正常輸入 `yoyo2026`。
- Workflow 提交步驟改成沒有新資料時不會硬推送。
- MACD 已改為 9 / 12 / 130：Signal=9、Fast EMA=12、Slow EMA=130，篩選條件為 `EMA12 - EMA130 > 0`。

## GitHub Pages 設定

Repository 建立完成並上傳後，到 **Settings → Pages**，Source 選 **Deploy from a branch**，Branch 選 `main`，資料夾選 `/docs`。
第一次到 **Actions → 每日更新 KD+MACD 觀察名單 → Run workflow**，`backfill_days` 輸入 `260` 後執行。
