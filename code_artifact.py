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
from pathlib import Path
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
import requests as req_lib
import gspread
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
import google.auth.exceptions
from DrissionPage import ChromiumPage, ChromiumOptions

load_dotenv()

TEFFY_URL = os.environ.get("TEFFY_URL")
# 対象サイトのベースURL(公開リポジトリのコード検索でドメイン名が直接ヒットしないよう、Secrets経由で注入する)
SD_BASE_URL = os.environ.get("SD_BASE_URL", "").rstrip("/")

# 画像キャッシュディレクトリの定義
CACHE_DIR = Path("./image_cache")
CACHE_DIR.mkdir(parents=True, exist_ok=True)


def notify_chat(text):
    """Google ChatのWebhook URL(TEFFY_URL)へ状況通知をPOSTする(SD DM送信専用スペースへ、target='sd_dm'で振り分け)。
    TEFFY_URL未設定、または送信失敗の場合は無視して処理を継続する(通知は補助機能であり本処理を止めない)。"""
    if not TEFFY_URL:
        return
    try:
        req_lib.post(TEFFY_URL, json={"text": text, "target": "sd_dm"}, timeout=10)
    except Exception as e_chat:
        print(f"-> Chat通知失敗（無視して続行）: {e_chat}")


def get_cache_filepath(url: str) -> Path:
    """URLのハッシュ値からキャッシュファイルパスを生成"""
    url_hash = hashlib.md5(url.encode('utf-8')).hexdigest()
    ext = ".jpg"
    clean_url = url.split("?")[0].lower()
    for e in [".png", ".jpg", ".jpeg", ".webp", ".gif"]:
        if clean_url.endswith(e):
            ext = e
            break
    return CACHE_DIR / f"{url_hash}{ext}"


def download_image_to_cache(url: str) -> Path or None:
    """指定されたURLから画像をダウンロードしてローカルキャッシュに保存。"""
    if not url or not url.startswith("http"):
        return None

    cache_path = get_cache_filepath(url)
    if cache_path.exists() and cache_path.stat().st_size > 0:
        print(f"  └ [キャッシュ利用] 既存画像を使用: {cache_path.name}")
        return cache_path.resolve()

    print(f"  └ [ダウンロード開始] {url}")
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        }
        resp = req_lib.get(url, headers=headers, timeout=15)
        resp.raise_for_status()

        with open(cache_path, "wb") as f:
            f.write(resp.content)

        print(f"  └ [ダウンロード完了] 保存完了: {cache_path.name} ({len(resp.content)} bytes)")
        return cache_path.resolve()
    except Exception as e:
        print(f"  └ ⚠️ [ダウンロード失敗] URL: {url} | エラー: {e}")
        return None


def process_image_urls(raw_urls: list) -> list:
    """
    D〜H列から取得した最大5個のURLリストを処理し、
    ローカルキャッシュ済みの絶対パスリスト（最大5件）を返却。
    """
    local_paths = []
    valid_urls = [u.strip() for u in raw_urls if u and u.strip()]
    if not valid_urls:
        return []

    print(f"🖼️ 画像URL候補 {len(valid_urls)} 件を処理します...")
    for idx, url in enumerate(valid_urls[:5], 1):
        path = download_image_to_cache(url)
        if path and path.exists():
            local_paths.append(str(path))

    return local_paths


CREDENTIAL_PATH = "./credentials.json"
TOKEN_PATH      = "./token.json"

# 環境変数からGoogle認証ファイルを生成
if os.getenv("GSPREAD_CREDENTIALS_JSON"):
    with open(CREDENTIAL_PATH, "w", encoding="utf-8") as f:
        f.write(os.getenv("GSPREAD_CREDENTIALS_JSON"))

if os.getenv("GSPREAD_TOKEN_JSON"):
    with open(TOKEN_PATH, "w", encoding="utf-8") as f:
        f.write(os.getenv("GSPREAD_TOKEN_JSON"))

# グローバルプレースホルダー
gc = None
sh = None
ws = None
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
        body_text = tab.run_js("return document.body ? document.body.innerText : '';", as_expr=True) or ""
    except Exception:
        body_text = ""

    block_keywords = ["403", "Forbidden", "アクセスが拒否", "ページが見つかりません", "Access Denied"]
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

    block_keywords = ["Forbidden", "アクセスが拒否", "ページが見つかりません", "Access Denied"]
    for kw in block_keywords:
        idx = text.find(kw)
        if idx != -1:
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
    try:
        packets = tab.listen.wait(count=999, timeout=timeout, fit_count=False)
    except Exception:
        packets = None
    http_err = check_http_errors_in_packets(packets)
    if http_err:
        print(f"🚨 [{code}] {step_label}: 対象サイトからHTTP {http_err} 応答を検知しました。BOT検知/アクセス制限の可能性が高いため緊急停止します。")
    return http_err


def get_msgbox_state(tab):
    try:
        cls = tab.run_js(
            "var el=document.getElementById('msgbox'); return el ? el.className : '';",
            as_expr=True
        ) or ""
    except Exception:
        cls = ""
    if "sent-mail-box" in cls:
        return "sent"
    if "confirm-message" in cls:
        return "confirm"
    if "edit-message" in cls:
        return "edit"
    return "unknown"


def get_top_sent_message_title(tab):
    try:
        title = tab.run_js(
            "var li = document.querySelector('.mail-list-container li .mail-info .title');"
            "return li ? li.innerText : '';",
            as_expr=True
        ) or ""
    except Exception:
        title = ""
    return title.strip()


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
        time.sleep(2.0)

        current_url = tab.url or ""
        print(f"📡 アクセス後の実際のURL: {current_url}")

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

        btn_ele = tab.ele("input.formbtn", timeout=5) or tab.ele("@type=submit", timeout=5)
        if not btn_ele:
            print("❌ ログインボタンが見つかりません。")
            return False

        print("✅ ログインボタンを特定しました。フォーム送信を開始します。")
        btn_ele.click()

        print("⏳ 画面遷移とログイン認証結果を待機しています...")
        login_success = False
        for attempt in range(8):
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


def send_dm_for_code(browser_page, tab, code, subject, body, image_paths=None, delay_range=(1.0, 2.5), interactive=False, save_debug=False, perform_send=True):
    """
    1件のメッセージ送信（＋多重入力アプローチ＋画像添付）処理を実行する
    """
    url = f"{SD_BASE_URL}/l/management/customer/detail.do?code={code}"
    print(f"🔍 処理開始店舗コード: {code} -> {url}")

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
        print("\n" + "=" * 60)
        print(f"🚨 BOT検知またはアクセス拒否を検出しました ({code})")
        print("  安全確保のため、処理を即時緊急停止します。")
        print("=" * 60 + "\n")
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
                    url_t = ''
                    try:
                        url_t = t.get_current_url()
                    except Exception:
                        try:
                            url_t = t.url or ''
                        except Exception:
                            url_t = ''
                    if '/i/msgbox/edit' in (url_t or ''):
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

    # ── 📷 画像添付処理 ──────────────────────────────────────────
    if image_paths and len(image_paths) > 0:
        print(f"📷 添付画像 {len(image_paths)} 枚をアップロード中...")
        try:
            file_input = new_tab.ele('css:input[type="file"]', timeout=3)
            if file_input:
                file_input.input(image_paths)
                print("  └ ✅ 画像のアップロード処理を実行しました。")
                time.sleep(2.0)
            else:
                print("  └ ⚠️ ファイルアップロード用要素 (input[type=file]) が見つかりませんでした。")
        except Exception as e_img:
            print(f"  └ ⚠️ 画像添付中にエラーが発生しました: {e_img}")

    # ── 件名・本文の多重アプローチ入力処理 ───────────────────────
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

    def approach_js_ids(tab_):
        tab_.run_js("var s=document.getElementById('new-mail-subject'); if(s) s.value=arguments[0]; var b=document.getElementById('new-mail-body'); if(b) b.value=arguments[1];", subject, body)

    approaches.append(('run_js_ids', approach_js_ids))

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

    approaches.append(('element_input_ids', approach_element_input))

    def approach_find_inputs(tab_):
        tried = False
        try:
            ta = tab_.ele('css:textarea', timeout=1)
            if ta:
                try:
                    tab_.run_js("var e=document.querySelector('textarea'); if(e) e.value='';")
                except Exception:
                    pass
                ta.input(body)
                tried = True
        except Exception:
            pass
        try:
            it = tab_.ele('css:input[type=text], css:input[type=email], css:input[type=search]', timeout=1)
            if it:
                try:
                    tab_.run_js("var e=document.querySelector('input[type=text], input[type=email], input[type=search]'); if(e) e.value='';")
                except Exception:
                    pass
                it.input(subject)
                tried = True
        except Exception:
            pass
        return tried

    approaches.append(('find_inputs', approach_find_inputs))

    try:
        random.shuffle(approaches)
    except Exception:
        pass

    success_method = None
    for name, func in approaches:
        try:
            func(new_tab)
        except Exception:
            pass
        time.sleep(0.25)
        try:
            check_subj = new_tab.run_js("return document.getElementById('new-mail-subject') ? document.getElementById('new-mail-subject').value : null;", as_expr=True)
        except Exception:
            check_subj = None
        try:
            check_body = new_tab.run_js("return document.getElementById('new-mail-body') ? document.getElementById('new-mail-body').value : null;", as_expr=True)
        except Exception:
            check_body = None

        time.sleep(0.3)

        if (check_subj and len(str(check_subj).strip()) > 0) or (check_body and len(str(check_body).strip()) > 0):
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
        new_tab.run_js("var b=document.querySelector(\"input[value='確認画面へ'], input[value*='確認画面']\"); if(b) b.click();")
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
        if new_tab and hasattr(new_tab, 'close'):
            new_tab.close()
    except Exception:
        pass

    if not code_confirmed:
        print(f"❌ [{code}] STEP5: 送信一覧先頭の宛先コードが一致しませんでした（期待:'{code}' / 実際:'{detected_code}'）。誤った宛先へ送信された可能性があるため、安全のため処理全体を停止します。")
        return "code_unconfirmed"

    return True


def kill_zombie_chrome():
    """残存・孤立しているChromeプロセスをOSレベルで強力かつ確実に一掃する"""
    sys_name = platform.system()
    print("🧹 ポート競合とゾンビ起動を防ぐため、既存の Chrome プロセスを強制終了します...")
    try:
        if sys_name == "Linux":
            subprocess.run(["pkill", "-9", "-f", "chrome"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["pkill", "-9", "-f", "chromium"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif sys_name == "Windows":
            subprocess.run(["taskkill", "/F", "/IM", "chrome.exe", "/T"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        print(f"⚠️ プロセス終了処理中にエラー（無視して続行）: {e}")


def try_launch_chrome():
    """5パターンの起動施策を検証。完全に人間的なフィンガープリントへの偽装処理を統合。"""
    import tempfile
    tmp_base = tempfile.gettempdir()

    UA_DESKTOP = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"

    strategies = [
        {
            "name": "施策1: 新ヘッドレス + 完全検出回避パラメータ",
            "setup": lambda co: [
                co.set_argument('--headless=new'),
                co.set_argument('--no-sandbox'),
                co.set_argument('--disable-gpu'),
                co.set_argument('--disable-dev-shm-usage'),
                co.set_argument('--remote-allow-origins=*'),
                co.set_argument('--remote-debugging-port=9222'),
                co.set_argument('--disable-blink-features=AutomationControlled'),
                co.set_argument(f'--user-agent={UA_DESKTOP}'),
                co.set_argument('--lang=ja-JP'),
                co.set_argument('--accept-lang=ja-JP,ja;q=0.9,en-US;q=0.8,en;q=0.7')
            ]
        },
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
        {
            "name": "施策4: 旧ヘッドレスモード指定 + 完全検出回避パラメータ",
            "setup": lambda co: [
                co.set_argument('--headless'),
                co.set_argument('--no-sandbox'),
                co.set_argument('--disable-gpu'),
                co.set_argument('--disable-dev-shm-usage'),
                co.set_argument('--remote-allow-origins=*'),
                co.set_argument('--remote-debugging-port=9222'),
                co.set_argument('--disable-blink-features=AutomationControlled'),
                co.set_argument(f'--user-agent={UA_DESKTOP}'),
                co.set_argument('--lang=ja-JP'),
                co.set_argument('--accept-lang=ja-JP,ja;q=0.9,en-US;q=0.8,en;q=0.7')
            ]
        },
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
    global gc, sh, ws, page, base_tab, SS_ID, subject, body
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
    if hour_now >= 21 or hour_now < 7:
        print(f"🛑 夜間時間帯のため処理を停止します（21:00-07:00 JST）。現在時刻: {hour_now}時。")
        notify_chat(f"🌙【SD DM送信】夜間時間帯（現在{hour_now}時）のため今回は起動見送りとなりました。")
        return

    page = try_launch_chrome()
    base_tab = page.get_tab(page.latest_tab)

    if not login_to_target_site(base_tab):
        print("❌ 初回ログインに失敗したため処理を終了します")
        notify_chat("🛑【SD DM送信】エラー終了: 初回ログインに失敗しました。")
        return

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
    else:
        print("  - Client ID 整合性: 解析できませんでした ⚠️")

    print(f"  - refresh_token の内包: {'あり ✅' if token_has_refresh else 'なし ❌'}")

    now_utc = datetime.now(timezone.utc)
    print(f"  - GitHub Actions環境 現在時刻 (UTC): {now_utc.isoformat()}")

    try:
        sa_json = os.getenv("GSPREAD_SERVICE_ACCOUNT_JSON")
        if sa_json:
            print("\n🛡️ サービスアカウント接続プロセスを開始します...")
            sa_info = json.loads(sa_json)
            gc = gspread.service_account_from_dict(sa_info)
            print("✅ サービスアカウントによる安全なセッション認証に成功しました。")
        else:
            print(f"\n🔑 OAuthによる明示的構築プロセスを開始します...")
            with open(CREDENTIAL_PATH, "r", encoding="utf-8") as f:
                creds_info = json.load(f)
            with open(TOKEN_PATH, "r", encoding="utf-8") as f:
                token_info = json.load(f)

            web_or_installed = creds_info.get("installed") or creds_info.get("web")
            if not web_or_installed:
                raise ValueError("credentials.json の構造が異常です")

            creds = Credentials(
                token=token_info.get("token"),
                refresh_token=token_info.get("refresh_token"),
                token_uri=token_info.get("token_uri") or "https://oauth2.googleapis.com/token",
                client_id=web_or_installed.get("client_id"),
                client_secret=web_or_installed.get("client_secret"),
                scopes=["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
            )

            if creds.expired:
                print("⚠️ アクセストークンは期限切れです。リフレッシュを実行します...")
                if not token_info.get("refresh_token"):
                    raise ValueError("🚨 token.json 内に 'refresh_token' が見つかりません。")

                request = Request()
                creds.refresh(request)
                print("🎉 トークンのリフレッシュに成功しました！")

                new_token_data = {
                    "token": creds.token,
                    "refresh_token": creds.refresh_token or token_info.get("refresh_token"),
                    "token_uri": creds.token_uri,
                    "client_id": creds.client_id,
                    "client_secret": creds.client_secret,
                    "scopes": creds.scopes,
                    "expiry": creds.expiry.isoformat() if creds.expiry else None
                }
                with open(TOKEN_PATH, "w", encoding="utf-8") as f:
                    json.dump(new_token_data, f, indent=2)

            gc = gspread.authorize(creds)
            print("✅ OAuth認証セッションの生成に成功しました。")

        sh = gc.open_by_key(ss_id_use)
        ws = sh.worksheet("CUS_TO_SD")
        print("✅ スプレッドシートおよび 'CUS_TO_SD' ワークシートへのアクセスに成功しました。")

    except Exception as e:
        err_msg = str(e)
        print("\n" + "!"*60)
        print("❌ スプレッドシートの初期化処理でエラーが発生しました。")
        print(f"🚨 [エラー詳細]: {err_msg}")
        print("!"*60)
        notify_chat(f"🛑【SD DM送信】エラー終了: スプレッドシート初期化失敗。\n詳細: {err_msg[:300]}")
        return

    save_debug = True if getattr(args, 'save_debug', False) else False
    if getattr(args, 'clean_debug', False):
        try:
            for fp in glob.glob(os.path.join(os.getcwd(), 'debug_msgbox_*.html')):
                try:
                    os.remove(fp)
                except Exception:
                    pass
        except Exception:
            pass

    # ── 件名・本文の読み込み ──────────────────────────────────────────
    subject = None
    body = None
    try:
        dm_ws = sh.worksheet('DM')
        subject = str(dm_ws.acell('A2').value or "").strip()
        body = str(dm_ws.acell('B2').value or "").strip()
    except Exception:
        pass

    if not subject or not body:
        print("❌ DMシートの件名(A2)または本文(B2)が空です。")
        notify_chat("🛑【SD DM送信】件名または本文が未設定です。")
        sys.exit(1)

    interactive_flag = True if args.interactive else False

    if args.code:
        code_use = args.code.strip()
        hour_now = datetime.now(JST).hour
        allow_send_now = not (hour_now >= 21 or hour_now < 7)
        res = send_dm_for_code(page, base_tab, code_use, subject, body, interactive=interactive_flag, save_debug=save_debug, perform_send=allow_send_now)
        if res is True:
            print(f"✅ {code_use} のメッセージ送信完了。")
        elif res == "withdrawn":
            print(f"🚪 {code_use} は退会済み会員と判定されました。")
        else:
            print(f"❌ {code_use} の処理エラー: {res}")
        return

    records = ws.get_all_values()
    max_scan_rows = len(records)
    print(f"ℹ️ スプレッドシート走査開始: 全{max_scan_rows}行")

    for i in range(max_scan_rows):
        row = records[i]
        if time.time() - START_TIME > RUNTIME_LIMIT_SEC:
            print(f"⏰ 実行時間の安全上限に到達したため安全終了します。")
            break

        if sent_count >= MAX_SEND_PER_RUN:
            print(f"🔁 バッチ{batch_no}完了。累計: {total_sent_count}件。続きから開始します。")
            sent_count = 0
            batch_no += 1

        be = get_col(row, 56)
        bf = get_col(row, 57)
        if not be or bf:
            continue

        code = get_col(row, 1)
        if not code:
            continue

        # ── 📷 D〜H列 (index 3〜7) から画像URLの抽出とダウンロード ─────
        raw_image_urls = [get_col(row, idx) for idx in range(3, 8)]
        cached_image_paths = process_image_urls(raw_image_urls)

        hour_now = datetime.now(JST).hour
        allow_send_now = not (hour_now >= 21 or hour_now < 7)
        if not allow_send_now:
            print("🛑 夜間時間帯に達したため処理を停止します。")
            break

        print(f"\n👉 [進捗: {sent_count + 1}/{MAX_SEND_PER_RUN} | 累計: {total_sent_count + 1}件目] 店舗: {code}")

        rownum = i + 1
        try:
            live_bf = ws.cell(rownum, 58).value
        except Exception:
            live_bf = None

        if live_bf:
            print(f"⏭️ ライブ確認で既に送信済みと判断されスキップ (行 {rownum})")
            continue

        try:
            result = send_dm_for_code(
                page, base_tab, code, subject, body,
                image_paths=cached_image_paths,
                interactive=interactive_flag,
                save_debug=save_debug,
                perform_send=allow_send_now
            )

            if result in ("blocked", "login_redirect", "nav_failure", "emergency_stop", "code_unconfirmed"):
                print(f"🚨 重大なエラー({result})が発生したため処理を緊急停止します。")
                notify_chat(f"🛑【SD DM送信】重大エラーにより停止: {result} (店舗: {code})")
                break

            if result == "withdrawn":
                now = datetime.now(JST)
                ws.update(values=[[f"{now.month}/{now.day}退会"]], range_name=f"BF{rownum}:BF{rownum}")
                continue

            if result is True:
                sent_count += 1
                total_sent_count += 1
                now = datetime.now(JST)
                ws.update(values=[[f"{now.month}/{now.day}"]], range_name=f"BF{rownum}:BF{rownum}")
                print(f"✅ {code} 送信成功")

        except Exception as e:
            print(f"❌ {code} 処理例外: {e}")

        time.sleep(random.uniform(1.0, 2.0))

    print(f"\n🎯 処理完了。合計送信数: {total_sent_count} 件")


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        print(f'\n💥 致命的例外: {e}')
        notify_chat(f"🛑【SD DM送信】例外発生: {e}")
    finally:
        if page:
            page.quit()
        for path in [CREDENTIAL_PATH, TOKEN_PATH]:
            if os.path.exists(path):
                try:
                    os.remove(path)
                except Exception:
                    pass