#!/usr/bin/env python3
import os
import sys
import re
import time
import random
import math
import glob
import json
import hashlib
import argparse
import subprocess
import platform
import tempfile
from pathlib import Path
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
import requests as req_lib
import gspread
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
import google.auth.exceptions
from DrissionPage import ChromiumPage, ChromiumOptions

# GitHub Actions では Secrets が環境変数として注入されるため引数なしで問題ない。
# ローカルの .env は読まない（GitHub専用スクリプト）。
load_dotenv()

TEFFY_URL = os.environ.get("TEFFY_URL")
# 対象サイトのベースURL(公開リポジトリのコード検索でドメイン名が直接ヒットしないよう、Secrets経由で注入する)
SD_BASE_URL = os.environ.get("SD_BASE_URL", "").rstrip("/")

# 画像キャッシュディレクトリ（GitHub Actions の一時作業領域）
CACHE_DIR = Path("./image_cache")
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# Chrome プロファイル: GitHub Actions はセッションごとにクリーンな環境なので
# GH版と同じく tempfile ベースで都度生成する。
CHROME_DEBUG_PORT = 9333


def notify_chat(text):
    """Google ChatのWebhook URL(TEFFY_URL)へ状況通知をPOSTする(SD DM送信専用スペースへ、target='sd_dm'で振り分け)。
    TEFFY_URL未設定、または送信失敗の場合は無視して処理を継続する(通知は補助機能であり本処理を止めない)。"""
    if not TEFFY_URL:
        return
    try:
        req_lib.post(TEFFY_URL, json={"text": text, "target": "sd_dm"}, timeout=10)
    except Exception as e_chat:
        print(f"-> Chat通知失敗（無視して続行）: {e_chat}")


# ── 画像ダウンロード・キャッシュ関連関数 ─────────────────────────────────────────
def get_cache_filepath(url: str) -> Path:
    """URLのハッシュ値からキャッシュファイルパスを生成（URL単位で重複防止）"""
    url_hash = hashlib.md5(url.encode('utf-8')).hexdigest()
    ext = ".jpg"
    clean_url = url.split("?")[0].lower()
    for e in [".png", ".jpg", ".jpeg", ".webp", ".gif"]:
        if clean_url.endswith(e):
            ext = e
            break
    return CACHE_DIR / f"{url_hash}{ext}"


def to_drive_direct_download_url(url: str) -> str:
    """
    GoogleドライブのURLが「共有リンク（人間がブラウザで見る用）」形式の場合、
    プログラムから直接ファイル本体を取得できる「直接ダウンロードURL」形式に変換する。
    該当しないURL（Drive以外・既に直接ダウンロード形式）はそのまま返す。
    """
    m = re.search(r"drive\.google\.com/file/d/([a-zA-Z0-9_-]+)", url)
    if m:
        file_id = m.group(1)
        return f"https://drive.google.com/uc?export=download&id={file_id}"
    return url


def download_image_to_cache(url: str) -> Path or None:
    """指定されたURLから画像をダウンロードしてローカルキャッシュに保存。"""
    if not url or not url.startswith("http"):
        return None

    cache_path = get_cache_filepath(url)
    if cache_path.exists() and cache_path.stat().st_size > 0:
        print(f"  └ [キャッシュ利用] 既存画像を使用: {cache_path.name}")
        return cache_path.resolve()

    download_url = to_drive_direct_download_url(url)
    if download_url != url:
        print(f"  └ [Google Drive URL変換] 共有リンク形式を直接ダウンロード形式に変換しました")

    print(f"  └ [ダウンロード開始] {url}")
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        }
        resp = req_lib.get(download_url, headers=headers, timeout=20)
        resp.raise_for_status()

        with open(cache_path, "wb") as f:
            f.write(resp.content)

        print(f"  └ [ダウンロード完了] 保存完了: {cache_path.name} ({len(resp.content)} bytes)")
        return cache_path.resolve()
    except Exception as e:
        print(f"  └ ⚠️ [ダウンロード失敗] URL: {url} | エラー: {e}")
        return None


def process_image_urls(raw_urls: list) -> tuple:
    """
    DMシートから取得した最大5個のURLリストを処理し、
    (ローカルキャッシュ済みの絶対パスリスト, 成功フラグ) を返却。
    URL指定があるにも関わらず1枚も準備できなかった場合は失敗フラグ(False)を返す。
    """
    local_paths = []
    valid_urls = [u.strip() for u in raw_urls if u and u.strip()]
    if not valid_urls:
        return ([], True)  # 画像指定がない場合は正常

    print(f"🖼️ 画像URL候補 {len(valid_urls)} 件を処理中...")
    for idx, url in enumerate(valid_urls[:5], 1):
        path = download_image_to_cache(url)
        if path and path.exists():
            local_paths.append(str(path))

    if len(valid_urls) > 0 and len(local_paths) == 0:
        print("❌ [画像エラー] 画像URLが指定されていますが、1枚もダウンロードできませんでした。")
        return ([], False)

    return (local_paths, True)


# ── 認証情報のパス（GitHub Actions上での一時出力パス） ─────────────────────────
CREDENTIAL_PATH = "./credentials.json"
TOKEN_PATH      = "./token.json"

# 環境変数(GitHub Actions Secrets)からGoogle認証ファイルを生成
if os.getenv("GSPREAD_CREDENTIALS_JSON"):
    with open(CREDENTIAL_PATH, "w", encoding="utf-8") as f:
        f.write(os.getenv("GSPREAD_CREDENTIALS_JSON"))

if os.getenv("GSPREAD_TOKEN_JSON"):
    with open(TOKEN_PATH, "w", encoding="utf-8") as f:
        f.write(os.getenv("GSPREAD_TOKEN_JSON"))

# グローバルプレースホルダー
gc = None
sh = None
ws_cus = None
ws_dm = None
ws_exclude = None
page = None
base_tab = None
SS_ID = os.getenv("SS_SD_CUS_ID")


def get_col(row_data, idx):
    return row_data[idx].strip() if len(row_data) > idx else ""


def read_body_from_file(path):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return f.read().strip()
    except Exception:
        return None


def human_delay(mean=0.0, sigma=0.6, minimum=0.2):
    try:
        val = math.exp(random.gauss(mean, sigma))
        time.sleep(max(minimum, val))
    except Exception:
        time.sleep(minimum)


def detect_block(tab):
    try:
        current_url = tab.url or ""
    except Exception:
        current_url = ""

    if "/login" in current_url or "login.do" in current_url:
        return "login_redirect"

    try:
        body_text = tab.run_js("document.body ? document.body.innerText : ''", as_expr=True) or ""
    except Exception:
        body_text = ""

    # ⚠️ "403" は画像ファイル名等の無関係な文脈に頻出する汎用的な数字列のため、
    #    誤検知が確定した（2026/7/13）。実際のHTTP 403応答は drain_and_check_http の
    #    statusコード分岐で別途正しく検知されるため、テキストキーワードとしての "403" は除外。
    block_keywords = ["Forbidden", "アクセスが拒否", "ページが見つかりません", "Access Denied"]
    for kw in block_keywords:
        if kw in body_text:
            return "blocked"

    return None


def check_trading_status(tab, code):
    """
    軽量な事前チェック（引き継ぎ書 9章・10章の検証結果に基づく実装）。

    detail.doページを実際にレンダリングせず、同一セッション(Cookie)のまま
    同期XHRでHTMLソースのみ取得し、静的に埋め込まれている「取引を中止する」
    フォーム(action: trading/cancel/execute.do)の有無で取引可否を判定する。

    「メッセージを送る」ボタン自体はJS動的挿入のため生HTMLでは判定できないが、
    検証の結果(419853/648228および取引可10件)、取引中止フォームの有無と
    常に一致することを確認済み。

    戻り値:
        "active"         -> 取引中（メッセージ送信へ進んでよい）
        "withdrawn"       -> 退会済み（取引中止フォームなし）
        "login_redirect"  -> ログイン画面へリダイレクトされた
        "blocked"         -> BOT検知/アクセス拒否と思われる応答
        "check_failed"    -> チェック自体が失敗（安全側に倒し緊急停止させる）
    """
    url = f"{SD_BASE_URL}/l/management/customer/detail.do?code={code}"
    js = """
        var xhr = new XMLHttpRequest();
        var result;
        try {
            xhr.open('GET', arguments[0], false);
            xhr.send(null);
            result = JSON.stringify({status: xhr.status, responseURL: xhr.responseURL, text: xhr.responseText});
        } catch (e) {
            result = JSON.stringify({error: String(e)});
        }
        return result;
    """
    try:
        raw = tab.run_js(js, url)
    except Exception as e:
        print(f"⚠️ [{code}] 事前チェック用XHRの実行自体に失敗しました: {e}")
        return "check_failed"

    if not raw:
        print(f"⚠️ [{code}] 事前チェックのレスポンスが空でした。")
        return "check_failed"

    try:
        data = json.loads(raw)
    except Exception:
        print(f"⚠️ [{code}] 事前チェックのレスポンス解析に失敗しました: {str(raw)[:200]}")
        return "check_failed"

    if "error" in data:
        print(f"⚠️ [{code}] 事前チェックのXHRが例外を投げました: {data['error']}")
        return "check_failed"

    status = data.get("status")
    response_url = data.get("responseURL") or ""
    text = data.get("text") or ""

    if "/login" in response_url or "login.do" in response_url:
        return "login_redirect"

    if isinstance(status, int) and status in (403, 429, 500, 502, 503, 504):
        print(f"🚨 [{code}] 事前チェックでHTTP {status} を検知しました。BOT検知/アクセス制限の可能性が高いため緊急停止します。")
        return "blocked"

    # ⚠️ 以前は "403" も含めていたが、画像ファイル名(例: .../2403291438126fe9a5f62f.jpg)等の
    #    無関係な文脈に頻出する汎用的な数字列のため、誤検知(919048)が確定した（2026/7/13）。
    #    実際のHTTP 403応答は直前のstatusコード分岐で別途正しく検知されるため、
    #    このテキストキーワードとしての"403"は削除して問題ない。
    block_keywords = ["Forbidden", "アクセスが拒否", "ページが見つかりません", "Access Denied"]
    for kw in block_keywords:
        idx = text.find(kw)
        if idx != -1:
            # 診断用: 実際のHTTPステータス・一致したキーワード・前後の文脈を必ず出力する。
            snippet = text[max(0, idx - 40):idx + len(kw) + 40].replace("\n", " ").replace("\r", "")
            print(f"🚨 [{code}] 事前チェックでキーワード一致による退会/ブロック判定: status={status} 一致文字列='{kw}' 前後文脈='...{snippet}...'")
            return "blocked"

    if "trading/cancel/execute.do" in text:
        return "active"
    else:
        return "withdrawn"


def drain(tab, step_label, timeout=2.0):
    try:
        _ = tab.listen.wait(count=999, timeout=timeout, fit_count=False)
    except Exception:
        pass


def check_http_errors_in_packets(packets, domain=None):
    """
    捕捉した通信パケットの中に、対象ドメインへの明確な異常応答(403/429/5xx)が
    含まれていないか確認する。BOT検知・アクセス制限の直接的な兆候。
    戻り値: 検知したステータスコード(int) または None
    """
    if not packets:
        return None
    if domain is None:
        domain = SD_BASE_URL.replace("https://", "").replace("http://", "")
    if not isinstance(packets, list):
        packets = [packets]
    for p in packets:
        try:
            url = getattr(p, 'url', '') or ''
            if domain not in url:
                continue
            status = None
            if not getattr(p, 'is_failed', False) and getattr(p, 'response', None) is not None:
                try:
                    status = p.response.status
                except Exception:
                    status = None
            if isinstance(status, int) and status in (403, 429, 500, 502, 503, 504):
                return status
        except Exception:
            continue
    return None


def drain_and_check_http(tab, step_label, code, timeout=2.5):
    """
    drain()と同様に通信を消費しつつ、対象ドメインへの4xx/5xx応答が
    無かったかを確認する。異常検知時はステータスコードを返す（正常時はNone）。
    """
    try:
        packets = tab.listen.wait(count=999, timeout=timeout, fit_count=False)
    except Exception:
        packets = None
    http_err = check_http_errors_in_packets(packets)
    if http_err:
        print(f"🚨 [{code}] {step_label}: 対象サイトからHTTP {http_err} 応答を検知しました。BOT検知/アクセス制限の可能性が高いため緊急停止します。")
    return http_err


def login_to_target_site(tab):
    """環境変数からログイン情報を読み込み、自動ログインを試みる（詳細診断ログ機能付き）"""
    username = os.getenv("SD_USERNAME")
    password = os.getenv("SD_PASSWORD")
    if not username or not password:
        print("⚠️ 環境変数 SD_USERNAME または SD_PASSWORD が設定されていません")
        return False

    print("🔑 対象サイトにログインを試みます...")
    login_url = f"{SD_BASE_URL}/l/management/login.do"
    print(f"📥 ログインページへアクセス中: {login_url}")

    try:
        tab.get(login_url)
        time.sleep(2.0)  # ログインページの読み込み待ち

        # 🧪 診断情報収集開始
        current_url = tab.url or ""
        print(f"📡 アクセス後の実際のURL: {current_url}")

        # 画面上に存在する全input要素をリストアップして属性を出力
        try:
            inputs_info = tab.run_js(
                "return Array.from(document.querySelectorAll('input')).map(el => "
                "({id: el.id, name: el.name, type: el.type, className: el.className, value: el.value})"
                ");"
            )
            print("📋 発見したすべての input 要素:")
            for idx, inp in enumerate(inputs_info or []):
                print(f"  [{idx}] ID: {inp.get('id')}, Name: {inp.get('name')}, Type: {inp.get('type')}, Class: {inp.get('className')}")
        except Exception as e:
            print(f"⚠️ input要素の抽出に失敗: {e}")

        # 1. ログインID入力欄の取得と入力
        id_ele = tab.ele("#input-id", timeout=5) or tab.ele("@name=identification", timeout=5)
        if not id_ele:
            print("❌ ログインID入力欄が見つかりません。診断用HTMLソースを以下に出力します:")
            try:
                html_snippet = tab.html
                print("-" * 80)
                print(html_snippet[:5000])
                print("-" * 80)
            except Exception as e:
                print(f"⚠️ HTMLソースの取得失敗: {e}")
            return False

        print("✅ ログインID入力欄を特定しました。クリアして値を入力します。")
        try:
            id_ele.clear()
        except Exception:
            tab.run_js("document.getElementById('input-id').value = '';")
        id_ele.input(username)
        time.sleep(0.3)

        # 2. パスワード入力欄の取得と入力
        pass_ele = tab.ele("#input-pass", timeout=5) or tab.ele("@name=password", timeout=5)
        if not pass_ele:
            print("❌ パスワード入力欄が見つかりません。")
            return False

        print("✅ パスワード入力欄を特定しました。クリアして値を入力します。")
        try:
            pass_ele.clear()
        except Exception:
            tab.run_js("document.getElementById('input-pass').value = '';")
        pass_ele.input(password)
        time.sleep(0.3)

        # 3. ログインボタンの取得とクリック
        btn_ele = tab.ele("input.formbtn", timeout=5) or tab.ele("@type=submit", timeout=5)
        if not btn_ele:
            print("❌ ログインボタンが見つかりません。")
            return False

        print("✅ ログインボタンを特定しました。フォーム送信を開始します。")
        btn_ele.click()

        # 4. ログイン成功の確実な判定（ログイン画面のコンテナ「#unique-common-login」の消失を検知）
        print("⏳ 画面遷移とログイン認証結果を待機しています...")
        login_success = False
        for attempt in range(8):  # 最大8秒待機
            time.sleep(0.5)
            try:
                login_container = tab.ele("#unique-common-login", timeout=1)
                if not login_container:
                    login_success = True
                    break
            except Exception:
                login_success = True
                break

            current_url = tab.url or ""
            if "login" not in current_url and "login.do" not in current_url:
                login_success = True
                break

        if not login_success:
            print("❌ ログイン認証に失敗しました（ログイン画面から遷移していないか、エラーメッセージが表示されています）")
            return False

        print("✅ ログインに成功しました")
        return True
    except Exception as e:
        print(f"⚠️ ログイン処理中に例外が発生しました: {e}")
        return False


def get_dm_template(dm_records, customer_business, customer_country):
    """
    `DM` シートから顧客の「業態キーワード」と「国名」に100%一致するテンプレート（件名, 本文, 画像URL群）を抽出する。
    DMシート構造（2026/8時点の最新レイアウト）:
      A列: 件名
      B列: 本文
      C列: キーワード（業態）
      D列: 国名
      E列: 予備1（不使用）
      F列: 予備2（不使用）
      G~K列: 画像1~5
    """
    if not customer_business or not customer_country:
        print(f"⚠️ 顧客の業態('{customer_business}')または国名('{customer_country}')が空欄です。")
        return None

    for row in dm_records[1:]:  # ヘッダー行スキップ
        subject = get_col(row, 0)      # A列: 件名
        body = get_col(row, 1)         # B列: 本文
        keyword_type = get_col(row, 2) # C列: キーワード（業態）
        country_type = get_col(row, 3) # D列: 国名
        if keyword_type == customer_business and country_type == customer_country:
            image_urls = [get_col(row, idx) for idx in range(6, 11)]  # G~K列: 画像1~5
            return {
                "subject": subject,
                "body": body,
                "image_urls": image_urls
            }

    print(f"⚠️ 照合不一致: 業態='{customer_business}' × 国名='{customer_country}' に合致するテンプレートがDMシート内に見つかりません。")
    return None


def send_dm_for_code(browser_page, tab, code, subject, body, image_paths=None, delay_range=(1.0, 2.5), interactive=False, save_debug=False, perform_send=True):
    url = f"{SD_BASE_URL}/l/management/customer/detail.do?code={code}"
    print(f"🔍 処理開始店舗コード: {code} -> {url}")

    # --- 軽量事前チェック（引き継ぎ書 9章・10章の検証結果に基づく） ---
    status_check = check_trading_status(tab, code)

    if status_check == "withdrawn":
        print(f"🚪 [{code}] 事前チェック(軽量XHR)で退会済みと判定しました。詳細ページの完全ロードはスキップします。")
        return "withdrawn"

    if status_check == "blocked":
        print(f"🚨 [{code}] 事前チェック(軽量XHR)でBOT検知/アクセス拒否の兆候を検出しました。安全のため緊急停止します。")
        return "blocked"

    if status_check == "login_redirect":
        print(f"⚠️ [{code}] 事前チェック(軽量XHR)でセッション切れを検出しました。再ログインを試みます。")
        if login_to_target_site(tab):
            status_check = check_trading_status(tab, code)
            if status_check == "withdrawn":
                print(f"🚪 [{code}] 再ログイン後の事前チェックで退会済みと判定しました。")
                return "withdrawn"
            if status_check != "active":
                print(f"❌ [{code}] 再ログイン後も事前チェックが正常な結果(active)を返しませんでした（結果: {status_check}）。安全のため緊急停止します。")
                return "login_redirect"
        else:
            return "login_redirect"
    elif status_check == "check_failed":
        print(f"⚠️ [{code}] 事前チェック自体が失敗しました。原因不明のまま処理を続けるのは危険なため緊急停止します。")
        return "emergency_stop"
    elif status_check != "active":
        print(f"⚠️ [{code}] 事前チェックが想定外の値を返しました（結果: {status_check}）。安全のため緊急停止します。")
        return "emergency_stop"

    try:
        try:
            tab.listen.start(targets=True, method=True, res_type=True)
        except Exception:
            pass
        tab.get(url)
    except Exception:
        try:
            tab.open(url)
        except Exception:
            print('❌ ページ遷移に致命的な失敗が発生しました')
            return "nav_failure"

    time.sleep(random.uniform(1.5, 2.5))
    http_err = drain_and_check_http(tab, "STEP1 詳細ページ遷移", code, timeout=2.0)
    if http_err:
        return "emergency_stop"

    block_reason = detect_block(tab)
    if block_reason == "login_redirect":
        print(f"⚠️ [{code}] セッション切れを検出しました。再ログインを試みます。")
        if login_to_target_site(tab):
            try:
                tab.get(url)
                time.sleep(1.5)
                block_reason = detect_block(tab)
            except Exception:
                return "login_redirect"
        else:
            return "login_redirect"

    if block_reason == "blocked":
        print()
        print("=" * 60)
        print(f"🚨 BOT検知またはアクセス拒否を検出しました ({code})")
        print("  安全確保のため、処理を即時緊急停止します。")
        print("=" * 60)
        print()
        return "blocked"

    if interactive and sys.stdin.isatty():
        print(f"レコード {code} を処理します。準備ができたら Enter を押してください...")
        try:
            input()
        except Exception:
            pass

    try:
        handles_before = list(browser_page.tab_ids)
    except Exception:
        handles_before = []

    btn_selectors = [
        "xpath://input[contains(@value,'メッセージを送る')]",
        "css:input.co-btns-ss[value*='メッセージ']",
        "css:input[value='メッセージを送る']",
        "xpath://button[contains(.,'メッセージを送る')]",
    ]

    btn = None
    for sel in btn_selectors:
        try:
            btn = tab.ele(sel, timeout=2)
        except Exception:
            btn = None
        if btn:
            break

    human_delay(mean=-0.5, sigma=0.5, minimum=0.15)
    if not btn:
        try:
            current_url_check = tab.url or ""
        except Exception:
            current_url_check = ""
        if "/detail.do" in current_url_check and f"code={code}" in current_url_check:
            print(f"🚪 [{code}] 詳細ページは表示できましたが「メッセージを送る」ボタンが存在しません。退会済み会員と判定します。（現在地: {current_url_check}）")
            return "withdrawn"
        else:
            print(f"❌ [{code}] メッセージボタンが見つからず、想定の詳細ページにもいません（現在地: {current_url_check}）。不正検知・レイアウト変更の可能性があるため緊急停止します。")
            return "emergency_stop"
    else:
        try:
            btn.click()
        except Exception:
            try:
                tab.run_js("var b=document.querySelector(\"input[value*='メッセージ']\"); if(b) b.click();")
            except Exception:
                print("❌ ボタンのクリックに失敗しました。不正検知対策のため緊急停止します。")
                return "emergency_stop"

    new_tab = None
    for _ in range(8):
        time.sleep(0.4 if (interactive and sys.stdin.isatty()) else 0.2)
        try:
            handles_after = list(browser_page.tab_ids)
        except Exception:
            handles_after = handles_before
        for h in handles_after:
            if h not in handles_before:
                try:
                    new_tab = browser_page.get_tab(h)
                except Exception:
                    new_tab = None
                break
        if new_tab:
            break

    if not new_tab:
        try:
            handles_after = list(browser_page.tab_ids)
            for h in handles_after:
                try:
                    t = browser_page.get_tab(h)
                    url = ''
                    try:
                        url = t.get_current_url()
                    except Exception:
                        try:
                            url = t.url or ''
                        except Exception:
                            url = ''
                    if '/i/msgbox/edit' in (url or ''):
                        new_tab = t
                        break
                except Exception:
                    continue
        except Exception:
            pass

        if not new_tab:
            try:
                tab.run_js(f"window.open('{SD_BASE_URL}/i/msgbox/edit','_blank');")
                time.sleep(0.3)
                handles_after = list(browser_page.tab_ids)
                for h in handles_after:
                    if h not in handles_before:
                        try:
                            new_tab = browser_page.get_tab(h)
                        except Exception:
                            new_tab = None
                        break
            except Exception:
                pass

    if not new_tab:
        print('❌ 編集タブを開くことができませんでした。')
        return "nav_failure"

    try:
        try:
            new_tab.listen.start(targets=True, method=True, res_type=True)
        except Exception:
            pass
    except Exception:
        pass

    if interactive and sys.stdin.isatty():
        print('編集タブを開きました。ブラウザで内容を確認してください。準備できたら Enter を押してください...')
        try:
            input()
        except Exception:
            pass

    # ── 📷 画像添付処理 ＆ 厳格なエラー検証 ──────────────────────────────────
    if image_paths and len(image_paths) > 0:
        print(f"📷 添付画像 {len(image_paths)} 枚をアップロード中...")
        try:
            # ファイル選択欄(input[type=file])に multiple 属性が無いため、
            # 1回の操作で複数ファイルは受け付けられない仕様。
            # 「1枚選択 → .attaching-list への反映確認 → 次の1枚」を繰り返す。
            attach_success = False
            attach_error_detected = False

            for img_idx, img_path in enumerate(image_paths, 1):
                file_input = new_tab.ele('css:input[type="file"]', timeout=3)
                if not file_input:
                    print(f"❌ [画像添付エラー] {img_idx}枚目: 画面上にファイル選択要素 (input[type=file]) が見つかりませんでした。送信を中止します。")
                    attach_error_detected = True
                    break

                print(f"  └ [{img_idx}/{len(image_paths)}枚目] を選択中...")
                file_input.input(img_path)

                print(new_tab.run_js(
                    """
                    (function() {
                        var el = document.querySelector('input[type="file"]');
                        return JSON.stringify({
                            filesLength: el && el.files ? el.files.length : null,
                            fileName: el && el.files && el.files[0] ? el.files[0].name : null,
                            fileSize: el && el.files && el.files[0] ? el.files[0].size : null,
                            fileInputCount: document.querySelectorAll('input[type="file"]').length
                        });
                    })()
                    """,
                    as_expr=True
                ))

                this_file_ok = False
                for _ in range(15):  # 最大15秒待機
                    time.sleep(1.0)
                    # 添付済み件数: ファイル名を持つ空でないspanのみをカウント
                    # （テンプレートdiv内の空spanを除外するため textContent の有無で判定）
                    attached_count = new_tab.run_js(
                        "Array.from(document.querySelectorAll('.attaching-list div span:not(.attaching-loading)'))"
                        ".filter(function(s){ return s.textContent.trim().length > 0; }).length",
                        as_expr=True
                    ) or 0
                    # ロード中判定: .attaching-loading の「親div」がdisplay:noneかどうかで見る
                    # （.attaching-loading自体は常にDOM常駐しており、非表示なのは親div側のため）
                    loading_exist = new_tab.run_js(
                        "(function(){ var el = document.querySelector('.attaching-list .attaching-loading');"
                        " var p = el ? el.closest('div') : null;"
                        " return p ? window.getComputedStyle(p).display !== 'none' : false; })()",
                        as_expr=True
                    )

                    # .fo-errors-box はページ読み込み時から常にDOM上に固定文言が入っており、
                    # 通常時は display:none で非表示。実際にエラーが起きた時だけサイト側JSが表示状態に切り替える。
                    err_text = new_tab.run_js(
                        "(function(){ var el = document.querySelector('.attachment .fo-errors-box');"
                        " if (!el) return '';"
                        " var st = window.getComputedStyle(el);"
                        " var visible = st.display !== 'none' && st.visibility !== 'hidden' && (el.offsetWidth > 0 || el.offsetHeight > 0);"
                        " return visible ? el.innerText : ''; })()",
                        as_expr=True
                    ) or ""

                    if err_text and len(err_text.strip()) > 0:
                        print(f"❌ [画像添付エラー] {img_idx}枚目で画面上に添付エラーが検出されました: {err_text}")
                        attach_error_detected = True
                        break

                    if not loading_exist and attached_count >= img_idx:
                        this_file_ok = True
                        print(f"  └ ✅ {img_idx}/{len(image_paths)}枚目の添付を確認しました（累計{attached_count}枚）。")
                        break

                if attach_error_detected:
                    break
                if not this_file_ok:
                    print(new_tab.run_js(
                        """
                        (function() {
                            var list = document.querySelector('.attaching-list');
                            var loading = document.querySelector('.attaching-list .attaching-loading');
                            var loadingParent = loading ? loading.parentElement : null;
                            var error = document.querySelector('.attachment .fo-errors-box');
                            return JSON.stringify({
                                attachedCount: document.querySelectorAll('.attaching-list div span:not(.attaching-loading)').length,
                                listText: list ? list.innerText : null,
                                listHtml: list ? list.innerHTML : null,
                                loadingDisplay: loadingParent ? getComputedStyle(loadingParent).display : null,
                                loadingVisibility: loadingParent ? getComputedStyle(loadingParent).visibility : null,
                                errorText: error ? error.innerText : null,
                                errorDisplay: error ? getComputedStyle(error).display : null
                            });
                        })()
                        """,
                        as_expr=True
                    ))
                    print(f"❌ [画像添付エラー] {img_idx}枚目の添付確認がタイムアウトしました。")
                    break
            else:
                attach_success = True  # 全ファイルがbreakなしで完了した場合のみ成功

            if not attach_success:
                print("❌ [画像添付エラー] アップロード不一致またはタイムアウト。送信ボタンを押さずにタブを閉じます。")
                if new_tab and hasattr(new_tab, 'close'):
                    new_tab.close()
                return "attach_failed"

        except Exception as e_img:
            print(f"❌ [画像添付例外エラー] 想定外の例外が発生しました: {e_img}")
            if new_tab and hasattr(new_tab, 'close'):
                new_tab.close()
            return "attach_failed"

    # ── 件名・本文の入力処理（多重アプローチ） ──────────────────────────────────
    subj_found = False
    body_found = False
    for _ in range(12):
        try:
            subj_found = bool(new_tab.ele('css:#new-mail-subject', timeout=1))
        except Exception:
            subj_found = False
        try:
            body_found = bool(new_tab.ele('css:#new-mail-body', timeout=1))
        except Exception:
            body_found = False
        if subj_found or body_found:
            break
        time.sleep(0.15)

    approaches = []

    def approach_element_input(tab_):
        s = None
        b = None
        try:
            s = tab_.ele('css:#new-mail-subject', timeout=1)
        except Exception:
            s = None
        try:
            b = tab_.ele('css:#new-mail-body', timeout=1)
        except Exception:
            b = None
        ok = False
        if s:
            try:
                try:
                    tab_.run_js("var e=document.getElementById('new-mail-subject'); if(e){e.value='';}")
                except Exception:
                    pass
                s.input(subject)
                ok = True
            except Exception:
                ok = False
        if b:
            try:
                try:
                    tab_.run_js("var e=document.getElementById('new-mail-body'); if(e){e.value='';}")
                except Exception:
                    pass
                b.input(body)
                ok = ok or True
            except Exception:
                ok = ok or False

    # 最優先: ネイティブ入力シミュレーション（.input()）。inputイベントが発火するためVue側にも反映される。
    approaches.append(('element_input_ids', approach_element_input))

    def approach_js_ids(tab_):
        tab_.run_js("var s=document.getElementById('new-mail-subject'); if(s) s.value=arguments[0]; var b=document.getElementById('new-mail-body'); if(b) b.value=arguments[1];", subject, body)

    # フォールバック: DOM直接書き換え（Vue側に反映されない可能性があるため二番手）。
    approaches.append(('run_js_ids', approach_js_ids))

    # ※ 旧 find_inputs アプローチは削除。
    # 件名欄はページ初期化時点でVueの初期値「ご連絡」が非空のまま入っており、
    # 「値が空でなければ成功」という緩い判定と組み合わさると、
    # 実際には#new-mail-subjectに書き込んでいなくても誤って成功判定されるバグの原因だった。

    success_method = None
    for name, func in approaches:
        try:
            func(new_tab)
        except Exception:
            pass
        time.sleep(0.25)
        try:
            check_subj = new_tab.run_js("document.getElementById('new-mail-subject') ? document.getElementById('new-mail-subject').value : null", as_expr=True)
        except Exception:
            check_subj = None
        try:
            check_body = new_tab.run_js("document.getElementById('new-mail-body') ? document.getElementById('new-mail-body').value : null", as_expr=True)
        except Exception:
            check_body = None

        time.sleep(0.3)

        if (check_subj is not None and str(check_subj) == subject) or (check_body is not None and str(check_body) == body):
            print(f"  └ ✅ 件名・本文の入力を確認しました（方式: {name}）。")
            if interactive and sys.stdin.isatty():
                ans = input("目視で内容が入っているか確認してください。問題なければ y を入力、続けて別方法を試すなら n を入力: ")
                if ans.strip().lower().startswith('y'):
                    success_method = name
                    break
                else:
                    print("次のアプローチを試します。")
                    continue
            else:
                success_method = name
                break

    if save_debug:
        try:
            html = new_tab.html
            fname = f"debug_msgbox_{code}.html"
            with open(fname, 'w', encoding='utf-8') as f:
                f.write(html)
            print(f"💾 編集タブのHTMLを保存しました: {fname}")
        except Exception:
            pass

    if success_method is None:
        print(f"❌ [{code}] 件名・本文の入力を確認できませんでした（全アプローチで不一致）。送信ボタンを押さずにタブを閉じます。")
        if new_tab and hasattr(new_tab, 'close'):
            new_tab.close()
        return "subject_body_unconfirmed"

    if interactive and sys.stdin.isatty():
        print('編集タブが開きました。ブラウザで内容を確認してください。')
        while True:
            ans = input("送信を続行しますか？ (y = 確認画面へ→送信 / n = スキップ): ").strip().lower()
            if ans == 'y':
                break
            if ans == 'n':
                print('このレコードはスキップします。')
                try:
                    if new_tab and hasattr(new_tab, 'close'):
                        new_tab.close()
                except Exception:
                    pass
                return False
            print("y または n を入力してください。")

    try:
        click_ok = new_tab.run_js(
            "(function(){ var b=document.querySelector(\"input[value='確認画面へ'], input[value*='確認画面']\");"
            " if(!b) return 'button_not_found';"
            " b.click(); return 'clicked'; })()",
            as_expr=True
        )
        if click_ok != 'clicked':
            print(f"❌ [{code}] 確認画面へボタンが見つかりませんでした。")
    except Exception:
        pass
    time.sleep(random.uniform(0.8, 1.5))

    http_err = drain_and_check_http(new_tab, "STEP4 確認画面へ遷移後", code, timeout=1.5)
    if http_err:
        return "emergency_stop"

    block_reason = detect_block(new_tab)
    if block_reason == "login_redirect":
        print(f"⚠️ [{code}] STEP4でセッション切れを検出しました。安全のため処理を停止します。")
        return "login_redirect"
    elif block_reason == "blocked":
        print(f"🚨 [{code}] STEP4でBOT検知/アクセス拒否と思われる表示を検知しました。緊急停止します。")
        return "blocked"

    if not perform_send:
        return "night_disabled"
    try:
        new_tab.run_js("var s=document.querySelector(\"input[value='メッセージを送信'], input[value*='送信']\"); if(s) s.click();")
    except Exception:
        pass
    time.sleep(random.uniform(1.0, 2.0))

    http_err = drain_and_check_http(new_tab, "STEP5 送信ボタンクリック後", code, timeout=1.5)
    if http_err:
        return "emergency_stop"

    block_reason = detect_block(new_tab)
    if block_reason == "login_redirect":
        print(f"⚠️ [{code}] STEP5でセッション切れを検出しました。安全のため処理を停止します。")
        return "login_redirect"
    elif block_reason == "blocked":
        print(f"🚨 [{code}] STEP5でBOT検知/アクセス拒否と思われる表示を検知しました。緊急停止します。")
        return "blocked"

    idle_sec = random.uniform(4.0, 8.0)
    print(f"⏳ [{code}] 完了ページへの反映待ち＋アクセス集中回避のため {idle_sec:.1f} 秒待機します（タブは維持したまま）...")
    time.sleep(idle_sec)

    try:
        visible_text = new_tab.run_js(
            "(document.querySelector('.mail-list-container') || document.querySelector('.main-container') || document.body).innerText",
            as_expr=True
        ) or ""
    except Exception:
        visible_text = ""
    code_match_search = re.search(r'\((\d+)\)', visible_text)
    detected_code = code_match_search.group(1) if code_match_search else None
    code_confirmed = (detected_code is not None and str(detected_code) == str(code))
    print(f"📊[SEND-CONFIRM][{code}] 送信一覧の先頭宛先コード: {detected_code} / 一致: {code_confirmed}")

    try:
        try:
            if new_tab and hasattr(new_tab, 'close'):
                new_tab.close()
        except Exception:
            pass
    except Exception:
        pass

    if not code_confirmed:
        print(f"❌ [{code}] STEP5: 送信一覧先頭の宛先コードが一致しませんでした（期待:'{code}' / 実際:'{detected_code}'）。誤った宛先へ送信された可能性があるため、安全のため処理全体を停止します。")
        return "code_unconfirmed"

    return True


def kill_zombie_chrome():
    """残存・孤立しているChromeプロセスをOSレベルで強力かつ確実に一掃する"""
    sys_name = platform.system()
    print("🧹 ポート競合とゾンビ起動を防ぐため、既存のChromeプロセスを強制終了します...")
    try:
        if sys_name == "Linux":
            subprocess.run(["pkill", "-9", "-f", "chrome"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["pkill", "-9", "-f", "chromium"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif sys_name == "Windows":
            subprocess.run(["taskkill", "/F", "/IM", "chrome.exe", "/T"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        print(f"⚠️ プロセス終了処理中にエラー（無視して続行）: {e}")


def try_launch_chrome():
    """5パターンの起動施策を検証。完全に人間的なフィンガープリントへの偽装処理を統合。
    GitHub Actions 専用のため常時ヘッドレス固定。"""
    tmp_base = tempfile.gettempdir()

    # 完全に人間と同じデスクトップのユーザーエージェントを定義（Linuxヘッドレス感を完全に消去）
    UA_DESKTOP = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"

    # 全起動オプションに検出回避（webdriver消去、言語 ja-JP、UA偽装、Automation表示削除）を完全に統合
    strategies = [
        # ── 施策1: デフォルト標準ヘッドレス + 人間化パラメータ ──
        {
            "name": "施策1: 新ヘッドレス + 完全検出回避パラメータ",
            "setup": lambda co: [
                co.set_argument('--headless=new'),
                co.set_argument('--no-sandbox'),
                co.set_argument('--disable-gpu'),
                co.set_argument('--disable-dev-shm-usage'),
                co.set_argument('--remote-allow-origins=*'),
                co.set_argument(f'--remote-debugging-port={CHROME_DEBUG_PORT}'),
                co.set_argument('--disable-blink-features=AutomationControlled'),
                co.set_argument(f'--user-agent={UA_DESKTOP}'),
                co.set_argument('--lang=ja-JP'),
                co.set_argument('--accept-lang=ja-JP,ja;q=0.9,en-US;q=0.8,en;q=0.7')
            ]
        },
        # ── 施策2: DP推奨 headless(True) + 人間化パラメータ ──
        {
            "name": "施策2: DP推奨 headless(True) + 完全検出回避パラメータ",
            "setup": lambda co: [
                co.headless(True),
                co.set_argument('--no-sandbox'),
                co.set_argument('--disable-gpu'),
                co.set_argument('--disable-dev-shm-usage'),
                co.set_argument('--remote-allow-origins=*'),
                co.set_argument('--disable-blink-features=AutomationControlled'),
                co.set_argument(f'--user-agent={UA_DESKTOP}'),
                co.set_argument('--lang=ja-JP'),
                co.set_argument('--accept-lang=ja-JP,ja;q=0.9,en-US;q=0.8,en;q=0.7')
            ]
        },
        # ── 施策3: 独立した隔離用ユーザデータパス + 人間化パラメータ ──
        {
            "name": "施策3: 独立プロファイルディレクトリ強制 + 完全検出回避パラメータ",
            "setup": lambda co: [
                co.set_argument('--headless=new'),
                co.set_argument('--no-sandbox'),
                co.set_argument('--disable-gpu'),
                co.set_argument('--disable-dev-shm-usage'),
                co.set_argument('--remote-allow-origins=*'),
                co.set_argument('--remote-debugging-port=9322'),
                co.set_argument(f'--user-data-dir={os.path.join(tmp_base, "dp_profile_9322")}'),
                co.set_argument('--disable-blink-features=AutomationControlled'),
                co.set_argument(f'--user-agent={UA_DESKTOP}'),
                co.set_argument('--lang=ja-JP'),
                co.set_argument('--accept-lang=ja-JP,ja;q=0.9,en-US;q=0.8,en;q=0.7')
            ]
        },
        # ── 施策4: 従来型旧ヘッドレス + 人間化パラメータ ──
        {
            "name": "施策4: 旧ヘッドレスモード指定 + 完全検出回避パラメータ",
            "setup": lambda co: [
                co.set_argument('--headless'),
                co.set_argument('--no-sandbox'),
                co.set_argument('--disable-gpu'),
                co.set_argument('--disable-dev-shm-usage'),
                co.set_argument('--remote-allow-origins=*'),
                co.set_argument(f'--remote-debugging-port={CHROME_DEBUG_PORT}'),
                co.set_argument('--disable-blink-features=AutomationControlled'),
                co.set_argument(f'--user-agent={UA_DESKTOP}'),
                co.set_argument('--lang=ja-JP'),
                co.set_argument('--accept-lang=ja-JP,ja;q=0.9,en-US;q=0.8,en;q=0.7')
            ]
        },
        # ── 施策5: 127.0.0.1固定指定 + 人間化パラメータ ──
        {
            "name": "施策5: 127.0.0.1固定指定起動 + 完全検出回避パラメータ",
            "setup": lambda co: [
                co.set_argument('--headless=new'),
                co.set_argument('--no-sandbox'),
                co.set_argument('--disable-gpu'),
                co.set_argument('--disable-dev-shm-usage'),
                co.set_argument('--remote-debugging-address=127.0.0.1'),
                co.set_argument('--remote-debugging-port=9422'),
                co.set_argument('--disable-blink-features=AutomationControlled'),
                co.set_argument(f'--user-agent={UA_DESKTOP}'),
                co.set_argument('--lang=ja-JP'),
                co.set_argument('--accept-lang=ja-JP,ja;q=0.9,en-US;q=0.8,en;q=0.7')
            ]
        }
    ]

    for idx, strat in enumerate(strategies, 1):
        kill_zombie_chrome()

        print("\n" + "="*50)
        print(f"🔄 起動検証試行 {idx}/5 -> {strat['name']}")
        print("="*50)

        co = ChromiumOptions()
        co.set_retry(0)

        if os.path.exists('/usr/bin/google-chrome'):
            co.set_browser_path('/usr/bin/google-chrome')

        try:
            strat['setup'](co)
            p = ChromiumPage(co)

            try:
                p.run_js("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
            except Exception:
                pass

            current_url = p.url
            print(f"🎉 接続成功！ 現在のURLを取得できました: {current_url}")
            return p
        except Exception as e:
            print(f"❌ {strat['name']} が失敗しました。待機せずに即時次の施策へ切り替えます。例外情報:\n{e}")
            co = None

    raise RuntimeError("🚨 5種類の施策すべてでChromeのWebSocket接続に失敗しました。")


def main():
    global gc, sh, ws_cus, ws_dm, ws_exclude, page, base_tab, SS_ID
    parser = argparse.ArgumentParser()
    parser.add_argument('--save-debug', action='store_true', help='編集タブのHTMLを debug_msgbox_*.html として保存します')
    parser.add_argument('--clean-debug', action='store_true', help='実行前に既存の debug_msgbox_*.html を削除します')
    parser.add_argument('--code', '-c', help='単一テスト用6桁またはフルコード')
    parser.add_argument('--interactive', '-i', action='store_true', help='対話モードで一時停止する')
    parser.add_argument('--shop-file', '-f', help='処理する店舗コードを改行で並べたファイルパス')
    parser.add_argument('--ss-id', help='処理するスプレッドシートのID')
    parser.add_argument('--no-jitter', action='store_true', help='起動時ランダム待機を無効化')
    args = parser.parse_args()

    github_event_name = os.environ.get("GITHUB_EVENT_NAME", "")
    is_cron_trigger = (github_event_name == "schedule")
    if not args.no_jitter and not args.code and not args.interactive and is_cron_trigger:
        jitter_sec = random.uniform(0, 300)
        print(f"🎲 起動時刻の固定パターン化を避けるため {jitter_sec:.1f} 秒間ランダム待機します...")
        time.sleep(jitter_sec)
    elif not args.no_jitter and not args.code and not args.interactive:
        print(f"🎲 起動時ランダム待機はスキップします（トリガー: {github_event_name or 'ローカル実行'}）")

    START_TIME = time.time()
    MAX_RUNTIME_SEC = 6 * 3600
    SAFETY_MARGIN_SEC = 10 * 60
    RUNTIME_LIMIT_SEC = MAX_RUNTIME_SEC - SAFETY_MARGIN_SEC

    MAX_SEND_PER_RUN = 15
    sent_count = 0
    total_sent_count = 0
    batch_no = 1

    JST = timezone(timedelta(hours=9))
    hour_now = datetime.now(JST).hour
    if hour_now >= 23 or hour_now < 7:
        print(f"🛑 夜間時間帯のため処理を停止します（21:00-07:00 JST）。現在時刻: {hour_now}時。終了します。")
        notify_chat(f"🌙【SD DM送信】夜間時間帯（現在{hour_now}時）のため今回は起動見送りとなりました。")
        return

    page = try_launch_chrome()
    base_tab = page.get_tab(page.latest_tab)

    if not login_to_target_site(base_tab):
        print("❌ 初回ログインに失敗したため処理を終了します")
        notify_chat("🛑【SD DM送信】エラー終了: 初回ログインに失敗しました。")
        return

    # ── gspread 接続 ＆ 詳細自己診断ロギング（元コード100%継承） ──
    ss_id_use = args.ss_id or SS_ID
    print("\n" + "="*50)
    print("📊 スプレッドシートの初期化処理を開始します...")
    print("="*50)

    creds_client_id = None
    token_client_id = None
    token_has_refresh = False
    token_expiry_str = None

    if os.path.exists(CREDENTIAL_PATH):
        try:
            with open(CREDENTIAL_PATH, "r", encoding="utf-8") as f:
                creds_data = json.load(f)
                web_or_installed = creds_data.get("installed") or creds_data.get("web")
                if web_or_installed:
                    creds_client_id = web_or_installed.get("client_id")
        except Exception as e:
            print(f"⚠️ credentials.json の自己解析失敗: {e}")

    if os.path.exists(TOKEN_PATH):
        try:
            with open(TOKEN_PATH, "r", encoding="utf-8") as f:
                token_data = json.load(f)
                token_client_id = token_data.get("client_id")
                token_has_refresh = "refresh_token" in token_data and bool(token_data["refresh_token"])
                token_expiry_str = token_data.get("expiry")
        except Exception as e:
            print(f"⚠️ token.json の自己解析失敗: {e}")

    print("\n🔬 認証トークン自己検証レポート:")

    if creds_client_id and token_client_id:
        match_status = "一致しています ✅" if creds_client_id == token_client_id else "不一致です ❌"
        print(f"  - Client ID 整合性: {match_status}")
        print(f"    * credentials.json: {creds_client_id[:15]}...{creds_client_id[-10:] if len(creds_client_id) > 25 else ''}")
        print(f"    * token.json:       {token_client_id[:15]}...{token_client_id[-10:] if len(token_client_id) > 25 else ''}")
        if creds_client_id != token_client_id:
            print("    ⚠️ 【警告】credentials と token の紐付けがズレています。これが 'invalid_grant' の直接原因です！")
    else:
        print("  - Client ID 整合性: 解析できませんでした ⚠️")

    print(f"  - refresh_token の内包: {'あり ✅' if token_has_refresh else 'なし ❌'}")
    if not token_has_refresh:
        print("    ⚠️ 【警告】token.json に refresh_token が含まれていません。")

    now_utc = datetime.now(timezone.utc)
    print(f"  - 現在時刻 (UTC): {now_utc.isoformat()}")
    if token_expiry_str:
        print(f"  - token.json 有効期限 (expiry): {token_expiry_str}")
        try:
            clean_expiry = token_expiry_str.replace("Z", "+00:00")
            expiry_dt = datetime.fromisoformat(clean_expiry)
            if expiry_dt.tzinfo is None:
                expiry_dt = expiry_dt.replace(tzinfo=timezone.utc)

            if now_utc > expiry_dt:
                print(f"    * 判定: アクセストークンは既に期限切れしています ⚠️")
            else:
                print(f"    * 判定: アクセストークンは現在も有効です ✅")
        except Exception as e:
            print(f"    * 有効期限の比較失敗: {e}")

    try:
        sa_json = os.getenv("GSPREAD_SERVICE_ACCOUNT_JSON")
        if sa_json:
            print("\n🛡️ サービスアカウント接続プロセスを開始します...")
            sa_info = json.loads(sa_json)
            gc = gspread.service_account_from_dict(sa_info)
            print("✅ サービスアカウントによる認証に成功しました。")
        else:
            print(f"\n🔑 OAuth（認証ファイル）による明示的構築プロセスを開始します...")

            with open(CREDENTIAL_PATH, "r", encoding="utf-8") as f:
                creds_info = json.load(f)
            with open(TOKEN_PATH, "r", encoding="utf-8") as f:
                token_info = json.load(f)

            web_or_installed = creds_info.get("installed") or creds_info.get("web")
            client_id = web_or_installed.get("client_id")
            client_secret = web_or_installed.get("client_secret")
            token_val = token_info.get("token")
            refresh_token = token_info.get("refresh_token")
            token_uri = token_info.get("token_uri") or "https://oauth2.googleapis.com/token"

            creds = Credentials(
                token=token_val,
                refresh_token=refresh_token,
                token_uri=token_uri,
                client_id=client_id,
                client_secret=client_secret,
                scopes=["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
            )

            if creds.expired:
                print("⚠️ アクセストークンは期限切れです。リフレッシュを実行します...")
                request = Request()
                creds.refresh(request)
                print("🎉 トークンのリフレッシュに成功しました！")

                new_token_data = {
                    "token": creds.token,
                    "refresh_token": creds.refresh_token or refresh_token,
                    "token_uri": creds.token_uri,
                    "client_id": creds.client_id,
                    "client_secret": creds.client_secret,
                    "scopes": creds.scopes,
                    "expiry": creds.expiry.isoformat() if creds.expiry else None
                }
                with open(TOKEN_PATH, "w", encoding="utf-8") as f:
                    json.dump(new_token_data, f, indent=2)

            gc = gspread.authorize(creds)

        sh = gc.open_by_key(ss_id_use)
        ws_cus = sh.worksheet("CUS_TO_SD")
        ws_dm = sh.worksheet("DM")

        try:
            ws_exclude = sh.worksheet("除外")
        except Exception:
            print("⚠️ '除外' シートが見つからないため新規作成します...")
            ws_exclude = sh.add_worksheet(title="除外", rows="1000", cols="5")
            ws_exclude.update(values=[["除外顧客ID"]], range_name="A1:A1")

        print("✅ 全スプレッドシート（CUS_TO_SD / DM / 除外）への接続完了")

    except Exception as e:
        err_msg = str(e)
        print("\n" + "!"*60)
        print("❌ スプレッドシートの初期化処理でエラーが発生しました。")
        print(f"🚨 [エラー詳細]: {err_msg}")
        print("!"*60)
        notify_chat(f"🛑【SD DM送信】エラー終了: スプレッドシート初期化失敗。\n詳細: {err_msg[:300]}")
        return

    save_debug = True if getattr(args, 'save_debug', False) else False

    # ── 1. 「除外」シートのA列（顧客ID）をロード ──────────────────────────
    try:
        exclude_records = ws_exclude.col_values(1)  # A列全取得
        exclude_set = set(str(x).strip() for x in exclude_records[1:] if str(x).strip())
        print(f"🚫 '除外' シートから {len(exclude_set)} 件の除外対象IDを読み込みました。")
    except Exception as e_ex:
        print(f"⚠️ '除外' シートの読み込みに失敗しました: {e_ex}")
        exclude_set = set()

    # ── 2. `DM` シートの全テンプレート行をロード ─────────────────────────
    try:
        dm_records = ws_dm.get_all_values()
        print(f"📋 `DM` シートから {len(dm_records) - 1} 件のメッセージテンプレートをロードしました。")
    except Exception as e_dm:
        print(f"❌ `DM` シートの読み込みに失敗しました: {e_dm}")
        sys.exit(1)

    records = ws_cus.get_all_values()
    max_scan_rows = len(records)
    print(f"ℹ️ 顧客シート（CUS_TO_SD）の走査を開始します（全{max_scan_rows}行）")

    end_reason = None
    consecutive_failures = 0
    MAX_CONSECUTIVE_FAILURES = 2

    # ── カラム位置の設定 ───────────────────────────────────────────
    CUS_COL_CODE = 1        # B列 (店舗コード)
    CUS_COL_BE = 56         # BE列（対象フラグ兼キーワード/業態。文字列の有無=対象フラグ、その文字列自体が業態）
    CUS_COL_BUSINESS = CUS_COL_BE  # 業態キーワードもBE列と同じ列（ユーザー確認済み・2重役割）
    CUS_COL_COUNTRY = 50    # AY列 (国名)
    CUS_COL_BF = 57         # BF列 (送信完了日)
    skipped_no_template_count = 0

    for i in range(max_scan_rows):
        row = records[i]
        if time.time() - START_TIME > RUNTIME_LIMIT_SEC:
            print(f"⏰ 実行時間の安全上限に到達したため安全終了します。")
            end_reason = "runtime_limit"
            break

        if sent_count >= MAX_SEND_PER_RUN:
            print(f"🔁 バッチ{batch_no}（{MAX_SEND_PER_RUN}件）完了。累計: {total_sent_count}件。続きから開始します。")
            sent_count = 0
            batch_no += 1

        be = get_col(row, CUS_COL_BE)
        bf = get_col(row, CUS_COL_BF)
        if not be or bf:
            continue

        code = get_col(row, CUS_COL_CODE)
        if not code:
            continue

        # ── 🚫 【ルール4-a】ページ移動前の「除外」シート事前照合 ─────────────
        if code in exclude_set:
            print(f"⏭️ [{code}] (行{i+1}) '除外' シートに含まれているため、ページ遷移前にスキップします。")
            continue

        # ── 🌐 【ルール5】顧客属性（業態 × 国名）からテンプレート照合 ─────────
        cus_business = get_col(row, CUS_COL_BUSINESS)
        cus_country = get_col(row, CUS_COL_COUNTRY)

        dm_tmpl = get_dm_template(dm_records, cus_business, cus_country)
        if not dm_tmpl:
            # 完全一致なら通す、一致しないなら通さない、というシンプルな仕様（ユーザー確認済み）。
            # 対応テンプレートが無い国（中国本土・英語圏など現時点で意図的に未対応）は、
            # この1件だけスキップして次の行へ進む。バッチ全体は止めない。
            print(f"⏭️ [{code}] (行{i+1}) 業態('{cus_business}') × 国名('{cus_country}') に該当するDMテンプレートが無いためスキップします。")
            skipped_no_template_count += 1
            continue

        subject = dm_tmpl["subject"]
        body = dm_tmpl["body"]
        raw_image_urls = dm_tmpl["image_urls"]

        # ── 📷 【ルール3】画像ダウンロード ＆ 最小キャッシュ処理 ─────────────
        cached_image_paths, dl_success = process_image_urls(raw_image_urls)

        if not dl_success:
            print(f"❌ [{code}] 画像ダウンロード失敗のため、この行の処理をスキップ/停止します。")
            consecutive_failures += 1
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                end_reason = "image_dl_failed"
                break
            continue

        hour_now = datetime.now(JST).hour
        allow_send_now = not (hour_now >= 23 or hour_now < 7)
        if not allow_send_now:
            print(f"🌙 夜間時間帯に入ったため安全終了します。")
            end_reason = "night"
            break

        print(f"\n👉 [バッチ{batch_no} 進捗: {sent_count + 1}/{MAX_SEND_PER_RUN}｜累計: {total_sent_count + 1}件目] 店舗 {code} (業態:{cus_business} / 国名:{cus_country}) の処理を開始します。")

        rownum = i + 1
        try:
            live_bf = ws_cus.cell(rownum, 58).value
        except Exception:
            live_bf = None

        if live_bf:
            print(f"⏭️ [{code}] 既に処理済み（BF{rownum}='{live_bf}'）のためスキップします。")
            continue

        try:
            result = send_dm_for_code(
                page, base_tab, code, subject, body,
                image_paths=cached_image_paths,
                interactive=args.interactive,
                save_debug=save_debug,
                perform_send=allow_send_now
            )

            if result in ("blocked", "login_redirect", "nav_failure", "emergency_stop", "code_unconfirmed", "attach_failed", "subject_body_unconfirmed"):
                print(f"🛑 危険検知・添付失敗による緊急終了（理由: {result}）。安全のため全中断します。")
                end_reason = f"error:{result}"
                break

            # ── 🚪 【ルール4-b】退会顧客の「除外」シートA列リアルタイム自動追記 ──────
            if result == "withdrawn":
                consecutive_failures = 0
                now = datetime.now(JST)
                date_str = f"{now.month}/{now.day}"

                # CUS_TO_SDシートに記録
                ws_cus.update(values=[[f"{date_str}退会"]], range_name=f"BF{rownum}:BF{rownum}")
                print(f"📝 CUS_TO_SD シートの BF{rownum} 列に退会記録（{date_str}退会）を書き込みました")

                # 「除外」シートのA列最下行へ追記
                try:
                    ws_exclude.append_row([code])
                    exclude_set.add(code)  # メモリ上のセットにも反映
                    print(f"🚫 '除外' シートの A列最下行へ店舗コード {code} を自動追加しました。")
                except Exception as e_ex_app:
                    print(f"⚠️ '除外' シートへの自動追記に失敗しました: {e_ex_app}")

                continue

            if result is True:
                consecutive_failures = 0
                sent_count += 1
                total_sent_count += 1
                now = datetime.now(JST)
                date_str = f"{now.month}/{now.day}"
                ws_cus.update(values=[[date_str]], range_name=f"BF{rownum}:BF{rownum}")
                print(f"📝 CUS_TO_SD シートの BF{rownum} 列に処理完了日（{date_str}）を書き込みました")
                print(f"✅ {code} のメッセージ送信に成功しました。")
            else:
                consecutive_failures += 1
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    end_reason = "consecutive_failures"
                    break
        except Exception as e:
            print(f"❌ {code} の処理中に深刻なエラーが発生しました: {e}")
            consecutive_failures += 1
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                end_reason = "consecutive_failures"
                break

        time.sleep(random.uniform(1.0, 2.0))

    print(f"\n🎯 処理完了。今回の合計送信成功数: {total_sent_count} 件（テンプレート未対応でスキップ: {skipped_no_template_count} 件）")

    if end_reason == "runtime_limit":
        notify_chat(f"⏰【SD DM送信】時間切れ終了: 実行時間の安全上限に到達したため終了しました。累計送信数: {total_sent_count}件。")
    elif end_reason == "night":
        notify_chat(f"🌙【SD DM送信】時間切れ終了: 夜間時間帯に入ったため終了しました。累計送信数: {total_sent_count}件。")
    elif end_reason == "consecutive_failures":
        notify_chat(f"🛑【SD DM送信】エラー終了: 連続失敗回数が上限に達したため緊急停止しました。累計送信数: {total_sent_count}件。")
    elif end_reason and end_reason.startswith("error:"):
        notify_chat(f"🛑【SD DM送信】エラー終了: 危険検知・添付失敗（理由: {end_reason.split(':',1)[1]}）のため緊急停止しました。累計送信数: {total_sent_count}件。")


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        try:
            print('\n🛑 処理を中断しました（KeyboardInterrupt） — 安全に停止します')
        except Exception:
            pass
    except SystemExit as e:
        try:
            print(f'\n🚪 終了: {e}')
        except Exception:
            pass
    except Exception as e:
        print(f'\n💥 予期しない致命的例外: {e}')
        notify_chat(f"🛑【SD DM送信】エラー終了: 予期しない致命的例外が発生しました。\n詳細: {e}")
        raise
    finally:
        if page:
            try:
                page.quit()
                print("🔌 Chromeプロセスを正常にクローズしました。")
            except Exception:
                pass
        for path in [CREDENTIAL_PATH, TOKEN_PATH]:
            if os.path.exists(path):
                try:
                    os.remove(path)
                except Exception:
                    pass
