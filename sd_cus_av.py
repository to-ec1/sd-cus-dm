import os
import sys
import time
import random
import math
from dotenv import load_dotenv
import gspread
from DrissionPage import ChromiumPage, ChromiumOptions
import requests
from bs4 import BeautifulSoup

sys.path.append(r"C:\data\dev\.313p")
import chrome_utils

load_dotenv()
SS_ID = os.getenv("SS_SD_CUS_ID")

CREDENTIAL_PATH = r"C:\data\dev\sd_cus\credentials.json"
TOKEN_PATH      = r"C:\data\dev\sd_cus\token.json"

gc = gspread.oauth(
    credentials_filename=CREDENTIAL_PATH,
    authorized_user_filename=TOKEN_PATH,
)
sh = gc.open_by_key(SS_ID)
ws = sh.worksheet("CUS_TO_SD")

# ── Chrome 起動（専用ポート 9333）─────────────────────────────────────────────
chrome_utils.CHROME_DEBUG_PORT    = 9333
chrome_utils.CHROME_USER_DATA_DIR = os.path.join(
    os.path.expanduser("~"), "AppData", "Local", "Temp", "chrome_dev_profile_9333"
)
chrome_utils.start_chrome()
print("Chrome 起動完了。ブラウザで操作してください。準備できたら Enter を押してください...")
input()

co = ChromiumOptions()
co.set_local_port(9333)
page = ChromiumPage(co)

records = ws.get_all_values()

def get_col(row_data, idx):
    return row_data[idx].strip() if len(row_data) > idx else ""

# ── 定数 ─────────────────────────────────────────────────────────────────────
LIMIT           = 5000
BATCH_SIZE      = 50
MAX_RETRIES     = 5
BACKOFF_BASE    = 2
MAX_BACKOFF     = 300
MAX_RUNTIME_SEC = 6 * 3600   # [I] 6時間上限

# ── メトリクス ────────────────────────────────────────────────────────────────
metrics = {
    "requests_success":  0,
    "browser_fallback":  0,
    "batch_flushes":     0,
    "total_updates":     0,
    "skip_429":          0,
    "skip_403":          0,
    "empty_reason":      {"no_element": 0, "fetch_failed": 0, "page_error": 0},
}

# ── [B] ブラウザの Cookie を requests セッションに同期 ───────────────────────
def sync_cookies_from_browser(pg, sess):
    """ブラウザのログイン済み Cookie を requests.Session に注入する。"""
    try:
        for cookie in pg.cookies():
            sess.cookies.set(
                cookie["name"],
                cookie["value"],
                domain=cookie.get("domain", ""),
            )
        print("✅ Cookie 同期完了")
    except Exception as e:
        print(f"⚠️  Cookie 同期失敗: {e}")

# ── HTTP セッション ───────────────────────────────────────────────────────────
session = requests.Session()
session.headers.update({
    # [C] より自然なヘッダーセット
    "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "ja-JP,ja;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection":      "keep-alive",
    "Referer":         "https://www.superdelivery.com/l/management/customer/list.do",
})

# 起動直後にブラウザ Cookie をセッションへ注入（[B]）
sync_cookies_from_browser(page, session)

# ── バッファ & フラッシュ ─────────────────────────────────────────────────────
buffer = []

def flush_buffer(buf):
    """バッファ内容を Google Sheets へバッチ書き込み（リトライ付き）。"""
    if not buf:
        return
    payload = [
        {"range": f"AV{r}:AV{r}", "values": [[v]]}
        for r, v in sorted(buf, key=lambda x: x[0])
    ]
    success = False
    attempt = 0
    while not success and attempt <= MAX_RETRIES:
        try:
            ws.batch_update(payload)
            metrics["batch_flushes"]  += 1
            metrics["total_updates"]  += len(payload)
            success = True
            print(f"✅ バッチ更新完了 ({len(payload)} 件)")
        except Exception as e:
            attempt += 1
            wait = min(MAX_BACKOFF, BACKOFF_BASE ** attempt + random.random())
            print(f"バッチ更新失敗 (試行 {attempt}/{MAX_RETRIES}): {e} — {wait:.1f}s 待機")
            time.sleep(wait)
    if not success:
        print("致命的: バッチ更新に失敗しました")

# ── [A] 対数正規分布による人間らしいスリープ ─────────────────────────────────
def human_sleep(mean=0.5, sigma=0.6, minimum=0.5):
    """対数正規分布でより自然な待機時間を生成する。"""
    # math.exp(random.gauss(mean, sigma)) は標準ライブラリだけで対数正規を近似できる
    val = math.exp(random.gauss(mean, sigma))
    time.sleep(max(minimum, val))

# ── [H] 空欄理由ログ付き business 抽出（requests）──────────────────────────
def extract_business_from_html(html_text):
    """HTML テキストから業種を抽出する。理由付きで空欄を返す。"""
    reason = "no_element"
    try:
        try:
            soup = BeautifulSoup(html_text, "lxml")   # [J] lxml 優先
        except Exception:
            soup = BeautifulSoup(html_text, "html.parser")

        th = soup.find(lambda tag: tag.name == "th" and tag.get_text(strip=True) == "業種")
        if not th:
            return "", reason
        td = th.find_next_sibling("td")
        if not td:
            return "", reason
        divs = td.find_all("div")
        if len(divs) >= 2:
            return divs[1].get_text(strip=True), ""
        return td.get_text(" ", strip=True), ""
    except Exception as e:
        print(f"HTML 解析例外: {e}")
        return "", "parse_error"

def trim_business(business):
    """業種の '>' 以降を除去する。"""
    if not business:
        return business
    for sep in (">", "＞"):
        idx = business.find(sep)
        if idx != -1:
            return business[:idx].strip()
    return business

# ── メインループ ──────────────────────────────────────────────────────────────
processed_count   = 0
START_TIME        = time.time()

try:
    for i, row in enumerate(records):
        # [I] 実行時間上限チェック
        if time.time() - START_TIME > MAX_RUNTIME_SEC:
            print("⏰ 実行時間上限（6h）到達: 安全終了します")
            break

        col_b  = get_col(row, 1)   # 会員コード
        col_al = get_col(row, 37)  # AL 列
        col_av = get_col(row, 47)  # AV 列

        # 処理条件: AL にデータあり、AV が空、会員コードあり、上限未達
        if not (col_al and not col_av and col_b and processed_count < LIMIT):
            continue

        url = f"https://www.superdelivery.com/l/management/customer/detail.do?code={col_b}"
        print(f"\n[行 {i+1}] 処理開始: code={col_b}")

        business      = ""
        fetch_reason  = "fetch_failed"
        cookie_synced = False  # 403 時の Cookie 再同期フラグ（1回だけ）

        # ── requests で取得 ─────────────────────────────────────────────────
        for attempt in range(2):   # Cookie 同期後の再試行を含め最大 2 回
            try:
                resp = session.get(url, timeout=10)
                print(f"  requests ステータス: {resp.status_code}")

                # [E] ステータスコード別分岐
                if resp.status_code == 200:
                    metrics["requests_success"] += 1
                    business, fetch_reason = extract_business_from_html(resp.text)
                    break

                elif resp.status_code == 429:
                    wait = int(resp.headers.get("Retry-After", 60))
                    print(f"  ⚠️  429 レート制限: {wait}s 待機（ブラウザフォールバックなし）")
                    metrics["skip_429"] += 1
                    time.sleep(wait)
                    break   # この行はスキップ、ブラウザも使わない

                elif resp.status_code == 403:
                    print()
                    print("=" * 60)
                    print("BOT検知された可能性があります (403 Forbidden)")
                    print(f"   行: {i+1}  会員コード: {col_b}")
                    print(f"   処理済み件数: {processed_count}")
                    print("   対処: 時間を置いてから再実行してください")
                    print("=" * 60)
                    print()
                    metrics["skip_403"] += 1
                    if buffer:
                        print(f"バッファをフラッシュして終了します（{len(buffer)} 件）")
                        flush_buffer(buffer)
                        buffer.clear()
                    raise SystemExit("403検知により中止")

                else:
                    print(f"  ⚠️  予期しないステータス {resp.status_code}: ブラウザフォールバックへ")
                    break

            except Exception as e:
                print(f"  requests 例外: {e}")
                fetch_reason = "fetch_failed"
                break

        # ── ブラウザフォールバック（[G] ページ完了待機あり）──────────────────
        if not business and fetch_reason not in ("", ):
            metrics["browser_fallback"] += 1
            print(f"  🌐 ブラウザフォールバック (reason={fetch_reason})")
            try:
                page.get(url)
                # [G] ロード完了 + 要素表示待機
                try:
                    page.wait.load_start()
                    page.wait.ele_displayed('xpath://th[text()="業種"]', timeout=10)
                except Exception:
                    time.sleep(random.uniform(2.0, 4.0))

                business_ele = page.ele('xpath://th[text()="業種"]/following-sibling::td/div[2]', timeout=5)
                if business_ele:
                    business = business_ele.text.strip()
                    print(f"  ブラウザで取得成功: {business}")
                else:
                    metrics["empty_reason"]["no_element"] += 1
                    print("  ブラウザでも業種要素が見つかりませんでした")
            except Exception as e:
                metrics["empty_reason"]["page_error"] += 1
                print(f"  ブラウザ取得例外: {e}")

        # [H] 空欄理由をメトリクスに記録
        if not business and fetch_reason:
            if fetch_reason in metrics["empty_reason"]:
                metrics["empty_reason"][fetch_reason] += 1

        business = trim_business(business)

        # ── バッファ追加 ──────────────────────────────────────────────────────
        rownum = i + 1
        buffer.append((rownum, business))
        processed_count    += 1
        print(f"  バッファ追加 ({len(buffer)}/{BATCH_SIZE}): {business!r}")

        # フラッシュ条件
        if len(buffer) >= BATCH_SIZE or processed_count >= LIMIT:
            print(f"バッファフラッシュ ({len(buffer)} 件)")
            flush_buffer(buffer)
            buffer.clear()

        # [D] 件数ベースの休憩
        if processed_count % 200 == 0:
            rest = random.uniform(180, 360)
            print(f"⏸️  長休止: {rest:.0f}s（{processed_count} 件処理済み）")
            time.sleep(rest)
        elif processed_count % 50 == 0:
            rest = random.uniform(30, 90)
            print(f"⏸️  小休止: {rest:.0f}s（{processed_count} 件処理済み）")
            time.sleep(rest)
        else:
            # [A] 対数正規分布スリープ
            human_sleep(mean=0.5, sigma=0.6, minimum=0.5)

except KeyboardInterrupt:
    print("\nKeyboardInterrupt 受信: 安全に停止します")
except Exception as e:
    print(f"\n予期しない例外: {e}")
    raise

# ── 後処理 ────────────────────────────────────────────────────────────────────
if buffer:
    print(f"最終バッファフラッシュ ({len(buffer)} 件)")
    flush_buffer(buffer)

# ── メトリクスサマリ ──────────────────────────────────────────────────────────
elapsed = time.time() - START_TIME
print("\n========== 処理完了 ==========")
print(f"処理件数           : {processed_count}")
print(f"経過時間           : {elapsed/60:.1f} 分")
print(f"requests 成功      : {metrics['requests_success']}")
print(f"ブラウザフォールバック: {metrics['browser_fallback']}")
print(f"429 スキップ       : {metrics['skip_429']}")
print(f"403 Cookie 同期    : {metrics['skip_403']}")
print(f"バッチ送信回数     : {metrics['batch_flushes']}")
print(f"総更新セル数       : {metrics['total_updates']}")
print(f"空欄内訳           : {metrics['empty_reason']}")