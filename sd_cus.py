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

# ── Chrome 起動（専用ポート 9222）─────────────────────────────────────────────
chrome_utils.CHROME_DEBUG_PORT    = 9222
chrome_utils.CHROME_USER_DATA_DIR = os.path.join(
    os.path.expanduser("~"), "AppData", "Local", "Temp", "chrome_dev_profile_9222"
)
chrome_utils.start_chrome()
print("Chrome 起動完了。ブラウザで操作してください。準備できたら Enter を押してください...")
input()

co = ChromiumOptions()
co.set_local_port(9222)
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
MAX_RUNTIME_SEC = 6 * 3600  # 6時間上限

# ── メトリクス ────────────────────────────────────────────────────────────────
metrics = {
    "requests_success": 0,
    "browser_fallback": 0,
    "batch_flushes":    0,
    "total_updates":    0,
    "skip_429":         0,
    "skip_403":         0,
    "empty_reason":     {"no_element": 0, "fetch_failed": 0, "page_error": 0},
}

# ── ブラウザの Cookie を requests セッションに同期 ────────────────────────────
def sync_cookies_from_browser(pg, sess):
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
    "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "ja-JP,ja;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection":      "keep-alive",
    "Referer":         "https://www.superdelivery.com/l/management/customer/list.do",
})

# 起動直後にブラウザ Cookie をセッションへ注入
sync_cookies_from_browser(page, session)

# ── バッファ & フラッシュ ─────────────────────────────────────────────────────
# バッファ: list of (row_1based, [AL, AM, AN, AO, AP, AQ, AR, AS, AT, AU, AV])
buffer = []

def flush_buffer(buf):
    if not buf:
        return
    payload = [
        {"range": f"AL{r}:AV{r}", "values": [v]}
        for r, v in sorted(buf, key=lambda x: x[0])
    ]
    success = False
    attempt = 0
    while not success and attempt <= MAX_RETRIES:
        try:
            ws.batch_update(payload)
            metrics["batch_flushes"] += 1
            metrics["total_updates"] += len(payload)
            success = True
            print(f"✅ バッチ更新完了 ({len(payload)} 件)")
        except Exception as e:
            attempt += 1
            wait = min(MAX_BACKOFF, BACKOFF_BASE ** attempt + random.random())
            print(f"バッチ更新失敗 (試行 {attempt}/{MAX_RETRIES}): {e} — {wait:.1f}s 待機")
            time.sleep(wait)
    if not success:
        print("致命的: バッチ更新に失敗しました")

# ── 対数正規分布による人間らしいスリープ ─────────────────────────────────────
def human_sleep(mean=0.5, sigma=0.6, minimum=0.5):
    val = math.exp(random.gauss(mean, sigma))
    time.sleep(max(minimum, val))

# ── HTML から全項目を抽出 ─────────────────────────────────────────────────────
def extract_all_from_html(html_text):
    """
    返り値: (dict, reason_str)
    dict キー: freq, genres, status, labels, logs, product, business
    取得できなかった項目は空文字。
    """
    result = {
        "freq": "", "genres": "", "status": "",
        "labels": "", "logs": "", "product": "", "business": "",
    }
    reason = ""
    try:
        try:
            soup = BeautifulSoup(html_text, "lxml")
        except Exception:
            soup = BeautifulSoup(html_text, "html.parser")

        # 購入頻度
        freq_tag = soup.select_one(".purchase-frequency-data")
        if freq_tag:
            result["freq"] = freq_tag.get_text(strip=True)

        # 購入ジャンル
        genre_tags = soup.select(".purchase-genres-data li")
        result["genres"] = "\n".join(t.get_text(strip=True) for t in genre_tags)

        # ステータス
        status_tag = soup.select_one(".customer-shop-detail-title-status")
        if status_tag:
            result["status"] = status_tag.get_text(strip=True)

        # ラベル
        label_spans  = soup.select(".member-label span")
        label_titles = soup.select(".customer-shop-detail-title-label")
        labels = [
            t.get_text(strip=True)
            for t in label_spans + label_titles
            if t.get_text(strip=True) != "入会直後の会員"
        ]
        result["labels"] = "\n".join(labels)

        # ログ
        log_tags = soup.select(".customer-shop-detail-applyLogList li")
        result["logs"] = "\n".join(
            t.get_text(strip=True).replace("取引開始：", "") for t in log_tags
        )

        # 主な取り扱い商品
        product_th = soup.find(lambda tag: tag.name == "th" and tag.get_text(strip=True) == "主な取り扱い商品")
        if product_th:
            product_td = product_th.find_next_sibling("td")
            if product_td:
                result["product"] = product_td.get_text(strip=True)

        # 業種
        business_th = soup.find(lambda tag: tag.name == "th" and tag.get_text(strip=True) == "業種")
        if business_th:
            business_td = business_th.find_next_sibling("td")
            if business_td:
                divs = business_td.find_all("div")
                raw = divs[1].get_text(strip=True) if len(divs) >= 2 else business_td.get_text(" ", strip=True)
                # '>' 以降を除去
                for sep in (">", "＞"):
                    idx = raw.find(sep)
                    if idx != -1:
                        raw = raw[:idx].strip()
                        break
                result["business"] = raw

    except Exception as e:
        print(f"HTML 解析例外: {e}")
        reason = "parse_error"

    return result, reason

# ── applyingRetailers API から freq / genres を取得 ───────────────────────────
def fetch_dynamic_data(html_text, sess):
    """
    HTMLから retailerCode を抽出し、動的APIを叩いて freq と genres を返す。
    失敗した場合は ("", "") を返す。
    """
    import re
    match = re.search(r"const retailerCode\s*=\s*(\d+);", html_text)
    if not match:
        print("  ⚠️  retailerCode がHTMLに見つかりません")
        return "", ""

    retailer_code = match.group(1)
    api_url = (
        f"https://www.superdelivery.com/i/api/dealer/applyingRetailers/search"
        f"?retailerCodes={retailer_code}"
    )
    try:
        api_resp = sess.get(api_url, timeout=10)
        if api_resp.status_code != 200:
            print(f"  ⚠️  動的API ステータス: {api_resp.status_code}")
            return "", ""

        achievement = api_resp.json().get(str(retailer_code), {})

        # freq
        freq = achievement.get("purchaseFrequency", "")

        # genres: purchaseGenres or purchaseGenreSchemas
        genres_list = achievement.get("purchaseGenres") or achievement.get("purchaseGenreSchemas") or []
        if genres_list:
            genres = "\n".join(
                f"{g['name']}：{g['percent']}%" for g in genres_list
            )
        else:
            genres = ""

        print(f"  ✅ 動的API取得: freq={freq!r}, genres件数={len(genres_list)}")
        return freq, genres

    except Exception as e:
        print(f"  ⚠️  動的API例外: {e}")
        return "", ""


# ── メインループ ──────────────────────────────────────────────────────────────
processed_count = 0
START_TIME      = time.time()

try:
    for i, row in enumerate(records):
        # 実行時間上限チェック
        if time.time() - START_TIME > MAX_RUNTIME_SEC:
            print("⏰ 実行時間上限（6h）到達: 安全終了します")
            break

        col_a  = get_col(row, 0)   # A列
        col_b  = get_col(row, 1)   # B列（会員コード）
        col_c  = get_col(row, 2)   # C列
        col_al = get_col(row, 37)  # AL列

        # 処理条件: A・B・C列あり、AL列が空、上限未達
        if not (col_a and col_b and col_c and not col_al and processed_count < LIMIT):
            continue

        url = f"https://www.superdelivery.com/l/management/customer/detail.do?code={col_b}"
        print(f"\n[行 {i+1}] 処理開始: code={col_b}")

        data         = None
        fetch_reason = "fetch_failed"
        cookie_synced = False

        # ── requests で取得 ──────────────────────────────────────────────────
        for attempt in range(2):
            try:
                resp = session.get(url, timeout=10)
                print(f"  requests ステータス: {resp.status_code}")

                if resp.status_code == 200:
                    metrics["requests_success"] += 1
                    data, fetch_reason = extract_all_from_html(resp.text)
                    # freq / genres は動的APIから上書き取得
                    data["freq"], data["genres"] = fetch_dynamic_data(resp.text, session)
                    break

                elif resp.status_code == 429:
                    wait = int(resp.headers.get("Retry-After", 60))
                    print(f"  ⚠️  429 レート制限: {wait}s 待機（スキップ）")
                    metrics["skip_429"] += 1
                    time.sleep(wait)
                    break

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

            except SystemExit:
                raise
            except Exception as e:
                print(f"  requests 例外: {e}")
                fetch_reason = "fetch_failed"
                break

        # ── ブラウザフォールバック ────────────────────────────────────────────
        if data is None and fetch_reason not in ("",):
            metrics["browser_fallback"] += 1
            print(f"  🌐 ブラウザフォールバック (reason={fetch_reason})")
            try:
                page.get(url)
                try:
                    page.wait.load_start()
                    page.wait.ele_displayed('xpath://th[text()="業種"]', timeout=10)
                except Exception:
                    time.sleep(random.uniform(2.0, 4.0))

                data = {"freq": "", "genres": "", "status": "", "labels": "", "logs": "", "product": "", "business": ""}

                # freq / genres は動的APIから取得（ブラウザHTMLには空で入っているため）
                browser_html = page.html
                data["freq"], data["genres"] = fetch_dynamic_data(browser_html, session)

                status_ele = page.ele("css:.customer-shop-detail-title-status", timeout=5)
                data["status"] = status_ele.text if status_ele else ""

                label_spans  = page.eles("css:.member-label span")
                label_titles = page.eles("css:.customer-shop-detail-title-label")
                data["labels"] = "\n".join(
                    e.text for e in label_spans + label_titles if e.text != "入会直後の会員"
                )

                log_eles = page.eles("css:.customer-shop-detail-applyLogList li")
                data["logs"] = "\n".join(
                    e.text.replace("取引開始：", "").strip() for e in log_eles
                )

                product_ele = page.ele('xpath://th[text()="主な取り扱い商品"]/following-sibling::td', timeout=5)
                data["product"] = product_ele.text if product_ele else ""

                business_ele = page.ele('xpath://th[text()="業種"]/following-sibling::td/div[2]', timeout=5)
                raw = business_ele.text.strip() if business_ele else ""
                for sep in (">", "＞"):
                    idx = raw.find(sep)
                    if idx != -1:
                        raw = raw[:idx].strip()
                        break
                data["business"] = raw

                print(f"  ブラウザで取得完了")

            except Exception as e:
                metrics["empty_reason"]["page_error"] += 1
                print(f"  ブラウザ取得例外: {e}")
                data = {"freq": "", "genres": "", "status": "", "labels": "", "logs": "", "product": "", "business": ""}

        if data is None:
            metrics["empty_reason"]["fetch_failed"] += 1
            data = {"freq": "", "genres": "", "status": "", "labels": "", "logs": "", "product": "", "business": ""}

        # ── バッファ追加（AL〜AV の 11列）────────────────────────────────────
        # AL=freq, AM=genres, AN=status, AO=labels, AP=logs, AQ=product,
        # AR=配送先2(空), AS=配送先3(空), AT=配送先4(空), AU=配送先5(空), AV=business
        values_row = [
            data["freq"],
            data["genres"],
            data["status"],
            data["labels"],
            data["logs"],
            data["product"],
            "",  # AR: 配送先2（空欄）
            "",  # AS: 配送先3（空欄）
            "",  # AT: 配送先4（空欄）
            "",  # AU: 配送先5（空欄）
            data["business"],
        ]

        rownum = i + 1
        buffer.append((rownum, values_row))
        processed_count += 1
        print(f"  バッファ追加 ({len(buffer)}/{BATCH_SIZE}): business={data['business']!r}")

        # フラッシュ条件
        if len(buffer) >= BATCH_SIZE or processed_count >= LIMIT:
            print(f"バッファフラッシュ ({len(buffer)} 件)")
            flush_buffer(buffer)
            buffer.clear()

        # 件数ベースの休憩
        if processed_count % 200 == 0:
            rest = random.uniform(180, 360)
            print(f"⏸️  長休止: {rest:.0f}s（{processed_count} 件処理済み）")
            time.sleep(rest)
        elif processed_count % 50 == 0:
            rest = random.uniform(30, 90)
            print(f"⏸️  小休止: {rest:.0f}s（{processed_count} 件処理済み）")
            time.sleep(rest)
        else:
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
print(f"処理件数              : {processed_count}")
print(f"経過時間              : {elapsed/60:.1f} 分")
print(f"requests 成功         : {metrics['requests_success']}")
print(f"ブラウザフォールバック: {metrics['browser_fallback']}")
print(f"429 スキップ          : {metrics['skip_429']}")
print(f"403 検知              : {metrics['skip_403']}")
print(f"バッチ送信回数        : {metrics['batch_flushes']}")
print(f"総更新行数            : {metrics['total_updates']}")
print(f"空欄内訳              : {metrics['empty_reason']}")