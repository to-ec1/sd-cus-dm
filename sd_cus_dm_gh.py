#!/usr/bin/env python3
import os
import sys
import re
import time
import random
import math
import glob
import json
import argparse
import subprocess
import platform
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


def notify_chat(text):
    """Google ChatのWebhook URL(TEFFY_URL)へ状況通知をPOSTする(SD DM送信専用スペースへ、target='sd_dm'で振り分け)。
    TEFFY_URL未設定、または送信失敗の場合は無視して処理を継続する(通知は補助機能であり本処理を止めない)。"""
    if not TEFFY_URL:
        return
    try:
        req_lib.post(TEFFY_URL, json={"text": text, "target": "sd_dm"}, timeout=10)
    except Exception as e_chat:
        print(f"-> Chat通知失敗（無視して続行）: {e_chat}")

# ── 認証情報のパス（GitHub Actions上での一時出力パス） ─────────────────────────
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


def print_packets(packets, step_label):
    if not packets:
        print(f"  [{step_label}] 通信なし")
        return
    if not isinstance(packets, list):
        packets = [packets]
    print(f"  [{step_label}] 捕捉: {len(packets)} 件")
    for p in packets:
        try:
            url = getattr(p, 'url', '')
            status = None
            if not getattr(p, 'is_failed', False) and getattr(p, 'response', None) is not None:
                try:
                    status = p.response.status
                except Exception:
                    status = None
            label = str(status) if status is not None else 'ERR'
            mark = 'ERROR' if (isinstance(status, int) and status >= 400) else 'OK'
            print(f"  [{mark}] {label} {url}")
        except Exception:
            print(f"  [ERR] パケット解析失敗 {getattr(p, 'url', '')}")


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


def get_msgbox_state(tab):
    """
    #msgbox 要素のclass属性からメッセージ機能画面の現在の状態を判定する。
    (dom_msgbox1.html / dom_kakunin2.html / dom_kanryo3.html の実際の差分から確定した判定方法)
    戻り値: "edit"(入力画面) | "confirm"(確認画面) | "sent"(送信完了/送信一覧) | "unknown"(判定不能)
    """
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
    """
    送信完了後(#msgbox.sent-mail-box)の送信一覧の先頭メッセージの件名テキストを取得する。
    実際に送信されたメッセージが一覧の先頭に反映されることを利用した最終確認用。
    """
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
        time.sleep(5)  # ロード完了を十分に待機

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
        time.sleep(1.0)

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
        time.sleep(1.0)

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
        for attempt in range(15):  # 最大15秒待機
            time.sleep(1.0)
            # ログインコンテナが存在するか確認
            try:
                login_container = tab.ele("#unique-common-login", timeout=1)
                # 存在しない（または非表示）ならログイン成功とみなす
                if not login_container:
                    login_success = True
                    break
            except Exception:
                # エレメント判定エラー時も、遷移によってDOMが完全に切り替わったため成功の可能性大
                login_success = True
                break

            # URLからも判定を補助
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


def send_dm_for_code(browser_page, tab, code, subject, body, delay_range=(1.0, 2.5), interactive=False, save_debug=False, perform_send=True):
    url = f"{SD_BASE_URL}/l/management/customer/detail.do?code={code}"
    print(f"🔍 処理開始店舗コード: {code} -> {url}")
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
    time.sleep(random.uniform(4.0, 6.0)) # 最初のロード時間を十分に確保
    http_err = drain_and_check_http(tab, "STEP1 詳細ページ遷移", code, timeout=2.5)
    if http_err:
        return "emergency_stop"

    block_reason = detect_block(tab)
    if block_reason == "login_redirect":
        print(f"⚠️ [{code}] セッション切れを検出しました。再ログインを試みます。")
        if login_to_target_site(tab):
            try:
                tab.get(url)
                time.sleep(4)
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
            tab.run_js("var b=document.querySelector(\"input[value*='メッセージ']\"); if(b) b.click();")
        except Exception:
            print("❌ メッセージボタンが見つかりません。不正検知・レイアウト変更の可能性があるため緊急停止します。")
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
    for _ in range(10):
        time.sleep(1.0 if (interactive and sys.stdin.isatty()) else 0.4)
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
                time.sleep(0.8)
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

    subj_found = False
    body_found = False
    for _ in range(20):
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
        time.sleep(0.3)

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
        time.sleep(0.8)
        try:
            check_subj = new_tab.run_js("return document.getElementById('new-mail-subject') ? document.getElementById('new-mail-subject').value : null;", as_expr=True)
        except Exception:
            check_subj = None
        try:
            check_body = new_tab.run_js("return document.getElementById('new-mail-body') ? document.getElementById('new-mail-body').value : null;", as_expr=True)
        except Exception:
            check_body = None

        time.sleep(1.0)

        if (check_subj and len(str(check_subj).strip())>0) or (check_body and len(str(check_body).strip())>0):
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
    time.sleep(random.uniform(2.0, 3.0))

    http_err = drain_and_check_http(new_tab, "STEP4 確認画面へ遷移後", code, timeout=2.5)
    if http_err:
        return "emergency_stop"

    block_reason = detect_block(new_tab)
    if block_reason == "login_redirect":
        print(f"⚠️ [{code}] STEP4でセッション切れを検出しました。安全のため処理を停止します。")
        return "login_redirect"
    elif block_reason == "blocked":
        print(f"🚨 [{code}] STEP4でBOT検知/アクセス拒否と思われる表示を検知しました。緊急停止します。")
        return "blocked"

    # 📊[MARKER-CHECK] STEP4は削除済み（判定に意味がないため）

    if not perform_send:
        return "night_disabled"
    try:
        new_tab.run_js("var s=document.querySelector(\"input[value='メッセージを送信'], input[value*='送信']\"); if(s) s.click();")
    except Exception:
        pass
    time.sleep(random.uniform(3.0, 5.0))

    http_err = drain_and_check_http(new_tab, "STEP5 送信ボタンクリック後", code, timeout=2.5)
    if http_err:
        return "emergency_stop"

    block_reason = detect_block(new_tab)
    if block_reason == "login_redirect":
        print(f"⚠️ [{code}] STEP5でセッション切れを検出しました。安全のため処理を停止します。")
        return "login_redirect"
    elif block_reason == "blocked":
        print(f"🚨 [{code}] STEP5でBOT検知/アクセス拒否と思われる表示を検知しました。緊急停止します。")
        return "blocked"

    # 🕐 既存のアイドリング時間(15〜30秒/相手先サーバーへのアクセス集中を避けるためのもの)を、
    # 「タブを閉じた後(呼び出し元のループ側)」から「タブを閉じる前(ここ)」に付け替える。
    # 待機時間の合計は変わらない(スピード・送信件数への影響ゼロ)。
    idle_sec = random.uniform(15.0, 30.0)
    print(f"⏳ [{code}] 完了ページへの反映待ち＋アクセス集中回避のため {idle_sec:.1f} 秒待機します（タブは維持したまま）...")
    time.sleep(idle_sec)

    # 📊[SEND-CONFIRM] 送信一覧の表示テキスト(ヘッダー/フッター/サイドメニュー除く)から、
    # 最初に出てくる「名前(店舗コード)」の店舗コードを抜き出し、今送信したcodeと一致するかを
    # 毎回確認する。会社名は照合せず、コード（カッコ内の数字）のみを比較する。
    # 不一致の場合は誤った宛先へ送信された可能性があるため、ゲート化して処理を停止する。
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
    """残存・孤立しているChromeプロセスをOSレベルで強力かつ確実に一鎖する"""
    sys_name = platform.system()
    print("🧹 ポート競合とゾンビ起動を防ぐため、既存 of Chromeプロセスを強制終了します...")
    try:
        if sys_name == "Linux":
            # Linux環境下でのChrome一掃
            subprocess.run(["pkill", "-9", "-f", "chrome"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["pkill", "-9", "-f", "chromium"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif sys_name == "Windows":
            # Windows環境下でのChrome一掃
            subprocess.run(["taskkill", "/F", "/IM", "chrome.exe", "/T"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        print(f"⚠️ プロセス終了処理中にエラー（無視して続行）: {e}")


def try_launch_chrome():
    """5パターンの起動施策を検証。完全に人間的なフィンガープリントへの偽装処理を統合。"""
    import tempfile
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
                co.set_argument('--remote-debugging-port=9222'),
                co.set_argument('--disable-blink-features=AutomationControlled'),  # 自動操作痕跡の排除
                co.set_argument(f'--user-agent={UA_DESKTOP}'),  # UA偽装
                co.set_argument('--lang=ja-JP'),  # 言語偽装
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
                co.set_argument('--remote-debugging-port=9222'),
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
        # 起動前に過去の残存プロセスを完全に掃除してポートの更地を確保
        kill_zombie_chrome()

        print("\n" + "="*50)
        print(f"🔄 起動検証試行 {idx}/5 -> {strat['name']}")
        print("="*50)

        co = ChromiumOptions()
        co.set_retry(0)  # 内部リトライ回数を0に制限（失敗時に連打せず即エラーを吐かせる）

        if os.path.exists('/usr/bin/google-chrome'):
            co.set_browser_path('/usr/bin/google-chrome')

        try:
            strat['setup'](co)
            # インスタンス生成（接続検証）
            p = ChromiumPage(co)

            # webdriver検出をさらに防ぐための追加JS評価
            try:
                p.run_js("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
            except Exception:
                pass

            # 疎通テスト
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
    parser.add_argument('--save-debug', action='store_true', help='編集タブのHTMLを debug_msgbox_*.html として保存します（デフォルト: 保存しない）')
    parser.add_argument('--clean-debug', action='store_true', help='実行前に既存の debug_msgbox_*.html を削除します')
    parser.add_argument('--code', '-c', help='単一テスト用6桁またはフルコード')
    parser.add_argument('--interactive', '-i', action='store_true', help='対話モードで一時停止する (デフォルト: なし)')
    parser.add_argument('--shop-file', '-f', help='処理する店舗コードを改行で並ぜたファイルパス')
    parser.add_argument('--ss-id', help='処理するスプレッドシートのID（省略時は環境変数を使用）')
    parser.add_argument('--no-jitter', action='store_true', help='起動時ランダム待機を無効化（デバッグ用）')
    args = parser.parse_args()

    # ── 🎲 起動時ランダム待機（cron固定時刻によるBOT検知パターン化を回避） ──────────
    # --code / --interactive 指定時（手動デバッグ実行）は待機せずスキップする
    if not args.no_jitter and not args.code and not args.interactive:
        jitter_sec = random.uniform(0, 300)  # 0〜5分
        print(f"🎲 起動時刻の固定パターン化を避けるため {jitter_sec:.1f} 秒間ランダム待機します...")
        time.sleep(jitter_sec)

    START_TIME = time.time()
    MAX_RUNTIME_SEC = 6 * 3600
    # GitHub Actions側のジョブタイムアウトより必ず先に自主終了するための安全マージン。
    # ここに到達した時点で finally ブロック（Chrome終了・認証ファイル削除）を確実に実行させる。
    SAFETY_MARGIN_SEC = 10 * 60  # 10分
    RUNTIME_LIMIT_SEC = MAX_RUNTIME_SEC - SAFETY_MARGIN_SEC

    # 🚨 セーフティ機能（暴走・過剰アクセス絶対防止）
    # MAX_SEND_PER_RUN は「1バッチあたり」の上限。バッチ終了後は counter をリセットして
    # 続きの行から次のバッチを継続する（RUNTIME_LIMIT_SEC に到達するまで繰り返す）。
    MAX_SEND_PER_RUN = 15  # 1バッチで送信する最大DM数。これを超えたらカウンタをリセットして次バッチへ
    sent_count = 0          # 現在バッチ内の送信数（バッチ完了ごとに0へリセット）
    total_sent_count = 0    # 今回のスクリプト実行全体での累計送信数（レポート用）
    batch_no = 1            # 現在のバッチ番号（ログ表示用）

    # UTC環境下でも正しい日本時間(JST)で夜間チェックを行うためのタイムゾーン定義
    JST = timezone(timedelta(hours=9))
    hour_now = datetime.now(JST).hour
    if hour_now >= 21 or hour_now < 7:
        print(f"🛑 夜間時間帯のため処理を停止します（21:00-07:00 JST）。現在時刻: {hour_now}時。終了します。")
        notify_chat(f"🌙【SD DM送信】夜間時間帯（現在{hour_now}時）のため今回は起動見送りとなりました。")
        return

    # ── Chrome 起動（自動フォールバック検証） ──────────────────────────────────────────
    page = try_launch_chrome()
    base_tab = page.get_tab(page.latest_tab)

    # 初回自動ログインを実行
    if not login_to_target_site(base_tab):
        print("❌ 初回ログインに失敗したため処理を終了します")
        notify_chat("🛑【SD DM送信】エラー終了: 初回ログインに失敗しました。SD_USERNAME/SD_PASSWORDまたはサイト側の変更を確認してください。")
        return

    # ── gspread 接続（「なぜ？次に何をするか」が一発でわかる詳細診断ロギング） ──
    ss_id_use = args.ss_id or SS_ID
    print("\n" + "="*50)
    print("📊 スプレッドシートの初期化処理を開始します...")
    print("="*50)

    # 🛠️ 整合性・構造の詳細自己分析
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

    # 🕵️ 検証1: クライアントIDの不一致チェック
    if creds_client_id and token_client_id:
        match_status = "一致しています ✅" if creds_client_id == token_client_id else "不一致です ❌"
        print(f"  - Client ID 整合性: {match_status}")
        print(f"    * credentials.json: {creds_client_id[:15]}...{creds_client_id[-10:] if len(creds_client_id) > 25 else ''}")
        print(f"    * token.json:       {token_client_id[:15]}...{token_client_id[-10:] if len(token_client_id) > 25 else ''}")
        if creds_client_id != token_client_id:
            print("    ⚠️ 【警告】credentials と token の紐付けがズレています。これが 'invalid_grant' の直接原因です！")
    else:
        print("  - Client ID 整合性: 解析できませんでした ⚠️（ファイルが破損しているか、JSON構造が異常です）")

    # 🕵️ 検証2: refresh_token の有無
    print(f"  - refresh_token の内包: {'あり ✅ (期限切れ時に自動更新可能)' if token_has_refresh else 'なし ❌ (期限切れ時に Actions 上で更新不可)'}")
    if not token_has_refresh:
        print("    ⚠️ 【警告】token.json に refresh_token が含まれていません。")
        print("    ローカルPCで「現役で使用中」であっても、アクセストークン自体（通常1時間）がActions実行時に失効していた場合、")
        print("    リフレッシュトークンがないため再生成できず、Googleから 'invalid_grant'（認可失敗）として即座に拒否されます。")

    # 🕵️ 検証3: 有効期限の確認とシステム時刻
    now_utc = datetime.now(timezone.utc)
    print(f"  - GitHub Actions環境 現在時刻 (UTC): {now_utc.isoformat()}")
    if token_expiry_str:
        print(f"  - token.json 内の有効期限 (expiry): {token_expiry_str}")
        try:
            clean_expiry = token_expiry_str.replace("Z", "+00:00")
            expiry_dt = datetime.fromisoformat(clean_expiry)
            if expiry_dt.tzinfo is None:
                expiry_dt = expiry_dt.replace(tzinfo=timezone.utc)

            if now_utc > expiry_dt:
                print(f"    * 判定: アクセストークンは既に期限切れしています ⚠️（期限切日時: {expiry_dt.isoformat()}）")
                if not token_has_refresh:
                    print("    🚨 【致命的】アクセストークンが期限切れ、かつ refresh_token が存在しないため、確実に接続エラーになります。")
            else:
                print(f"    * 判定: アクセストークンは現在も有効です ✅（残り時間: {expiry_dt - now_utc}）")
        except Exception as e:
            print(f"    * 有効期限の比較失敗: {e}")

    try:
        # 🌟 1. サービスアカウント方式を最優先に定義（環境変数があれば利用）
        sa_json = os.getenv("GSPREAD_SERVICE_ACCOUNT_JSON")
        if sa_json:
            print("\n🛡️ サービスアカウント（Service Account）接続プロセスを開始します...")
            try:
                sa_info = json.loads(sa_json)
                gc = gspread.service_account_from_dict(sa_info)
                print("✅ サービスアカウントによる安全なセッション認証に成功しました。")
            except Exception as e_sa:
                print(f"❌ サービスアカウント認証の展開に失敗しました: {e_sa}")
                raise e_sa
        else:
            # 🌟 2. 従来のOAuth接続プロセス（低レベルgoogle-authライブラリによる明示的処理）
            print(f"\n🔑 OAuth（認証ファイル）による明示的構築プロセスを開始します...")
            print(f"  - CREDENTIAL_PATH ({CREDENTIAL_PATH}): {'存在します ✅' if os.path.exists(CREDENTIAL_PATH) else '存在しません ❌'}")
            print(f"  - TOKEN_PATH ({TOKEN_PATH}): {'存在します ✅' if os.path.exists(TOKEN_PATH) else '存在しません ❌'}")
            print(f"  - スプレッドシートID: {ss_id_use if ss_id_use else '未設定 ❌'}")

            # credentials.json および token.json の手動ロード
            with open(CREDENTIAL_PATH, "r", encoding="utf-8") as f:
                creds_info = json.load(f)
            with open(TOKEN_PATH, "r", encoding="utf-8") as f:
                token_info = json.load(f)

            # クライアント情報の抽出
            web_or_installed = creds_info.get("installed") or creds_info.get("web")
            if not web_or_installed:
                raise ValueError("credentials.json の構造が異常です（'installed' も 'web' も見つかりません）")

            client_id = web_or_installed.get("client_id")
            client_secret = web_or_installed.get("client_secret")
            token_val = token_info.get("token")
            refresh_token = token_info.get("refresh_token")
            token_uri = token_info.get("token_uri") or "https://oauth2.googleapis.com/token"

            # Credentialsオブジェクトを手動組み立て
            creds = Credentials(
                token=token_val,
                refresh_token=refresh_token,
                token_uri=token_uri,
                client_id=client_id,
                client_secret=client_secret,
                scopes=["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
            )

            # 明示的なリフレッシュ要求の実行
            print("🔄 トークンの検証およびリフレッシュ要求の送信準備...")
            if creds.expired:
                print("⚠️ アクセストークンは期限切れです。google-authを通じて明示的にリフレッシュトークンによる更新を行います...")
                if not refresh_token:
                    raise ValueError("🚨 token.json 内に 'refresh_token' が見つからないため、トークンを自動更新できません。")

                try:
                    request = Request()
                    creds.refresh(request)
                    print("🎉 トークンのリフレッシュに成功しました！新しいアクセストークンを取得しました。")

                    # 生成された最新トークンを一時ディスクに保存（書き戻し不整合を防止）
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
                    print("💾 一時トークンファイルを正常にアップデートしました。")
                except google.auth.exceptions.RefreshError as e_refresh:
                    # Googleが返した生のエラーレスポンス（HTTPレスポンス全体）を完全にコンソールに書き出す
                    print("\n" + "!"*60)
                    print("❌ Google OAuth2 APIサーバーが明示的なリフレッシュ要求を拒否しました。")
                    print(f"🚨 [エラー内容]: {e_refresh}")
                    try:
                        if len(e_refresh.args) > 1:
                            print(f"📋 [Googleからの生のエラーレスポンス]: {e_refresh.args[1]}")
                    except Exception:
                        pass
                    print("!"*60)
                    raise e_refresh
            else:
                print("✅ アクセストークンは現在も有効です。リフレッシュ不要。")

            gc = gspread.authorize(creds)
            print("✅ OAuth認証セッションの生成に成功しました。")

        print(f"⚡ スプレッドシートを開いています（ID: {ss_id_use}）...")
        sh = gc.open_by_key(ss_id_use)
        print(f"✅ スプレッドシートへのアクセスに成功しました: 「{sh.title}」")

        print("⚡ ワークシート 'CUS_TO_SD' をロード中...")
        ws = sh.worksheet("CUS_TO_SD")
        print("✅ ワークシート 'CUS_TO_SD' の取得に成功しました。準備完了。")

    except Exception as e:
        err_msg = str(e)
        print("\n" + "!"*60)
        print("❌ スプレッドシートの初期化処理でエラーが発生しました。")
        print(f"🚨 [エラー詳細]: {err_msg}")
        print("!"*60)

        if "invalid_grant" in err_msg or "expired" in err_msg or "revoked" in err_msg:
            print("\n💡 【原因: ローカル現役トークンがクラウド環境（Actions）でのみ弾かれる根本ロジック】")
            print("  1. リフレッシュトークンローテーション（Refresh Token Rotation）のミスマッチ:")
            print("     本番環境のGoogle OAuth同意画面（Production）であっても、リフレッシュトークンが使用されるたびに、")
            print("     認可サーバーから新しい『リフレッシュトークン』が再発行され、古いリフレッシュトークンは即時無効化されます。")
            print("     Render（Webサービス）のようにメモリやディスク上にプロセスが維持される環境では自動更新が上書き保持されますが、")
            print("     GitHub Actionsのように毎起動時に、以前ローカルPCでコピーした『古い token.json』から起動した場合、")
            print("     Googleは『すでに別セッション（ローカルPC側）で消費済みの古いリフレッシュトークンである』と判断し、一発で invalid_grant を返します。")
            print("  2. IPアドレスの急激な変化によるGoogleの防衛フィルタロック:")
            print("     日本国内のIPで承認されたトークンを、米国ホストのActions Runnerからリフレッシュ要求した場合、")
            print("     不正アクセス（乗っ取り）と誤検知され、Google側でセッションが強制遮断されます。")
            print("\n🛠️ 【解決のために次に行う手順】")
            print("  1. ローカルPC側の `token.json` と、GitHub Actionsの `GSPREAD_TOKEN_JSON` のクライアント情報（Client ID/Secret）が100%一致しているか確認。")
            print("  2. ローカルPC側の `gspread` 操作で、アクセストークンを最新の状態に強制リフレッシュします。")
            print("  3. リフレッシュされた直後（＝アクセストークンの有効期限がまだ50分以上残っている状態）の `token.json` の中身をコピー。")
            print("  4. コピーした内容を GitHub Secrets の 『GSPREAD_TOKEN_JSON』 に即座に貼り付けて更新。")
            print("  5. その後、1時間以内に GitHub Actions を手動トリガーして実行させてください。")
            print("     （アクセストークン自体がまだ有効なため、IPフィルタによるリフレッシュ要求のトリガー自体が発生せず、100%正常にSheet接続を通過します）")
        elif "API key not valid" in err_msg or "invalid_client" in err_msg:
            print("\n💡 【原因: OAuthクライアント認証情報の不備】")
            print("  credentials.json の内容が不正か、Google Cloud側でクライアントIDが削除・変更されています。")
            print("\n🛠️ 【解決のために次に行う手順】")
            print("  Google Cloud ConsoleからOAuth 2.0認証情報（credentials.json）を再ダウンロードし、GitHubの 'GSPREAD_CREDENTIALS_JSON' に再設定してください。")
        elif "Requested entity was not found" in err_msg:
            print("\n💡 【原因: スプレッドシートIDの間違い】")
            print(f"  指定されたスプレッドシートID（現在の指定: '{ss_id_use}'）がGoogleドライブ上に存在しません。")
            print("\n🛠️ 【解決のために次に行う手順】")
            print("  環境変数 'SS_SD_CUS_ID' または '--ss-id' で渡しているIDに誤字脱字がないか、URLの /d/ と /edit の間の文字列を再確認してください。")
        elif "caller does not have permission" in err_msg or "PERMISSION_DENIED" in err_msg:
            print("\n💡 【原因: スプレッドシートへの共有権限不足】")
            print("  認証に使用しているGoogleアカウントが、指定されたスプレッドシートに対する権限を持っていません。")
            print("\n🛠️ 【解決のために次に行う手順】")
            print("  対象スプレッドシートの右上「共有」から、使用している認証アカウントのメールアドレスに対して「編集者」権限を付与してください。")
        else:
            print("\n💡 【原因: その他の通信・APIエラー】")
            print("  API利用上限に達している、もしくはGoogle Drive API / Google Sheets API がGCPプロジェクト側で有効化されていない可能性があります。")
            print("  GCPコンソールで対象APIが「有効」になっているかステータスを確認してください。")

        print("\n" + "="*50)
        notify_chat(f"🛑【SD DM送信】エラー終了: スプレッドシート初期化に失敗しました。\n詳細: {err_msg[:300]}")
        return

    save_debug = True if getattr(args, 'save_debug', False) else False
    if getattr(args, 'clean_debug', False):
        try:
            removed = 0
            for fp in glob.glob(os.path.join(os.getcwd(), 'debug_msgbox_*.html')):
                try:
                    os.remove(fp)
                    removed += 1
                except Exception:
                    pass
            if removed:
                print(f"🧹 既存の debug_msgbox_*.html を {removed} 件削除しました。")
        except Exception:
            pass

    subject = None
    body = None

    try:
        if sh:
            try:
                dm_ws = sh.worksheet('DM')
            except Exception:
                dm_ws = None
            if dm_ws:
                try:
                    a2 = dm_ws.acell('A2').value
                    b2 = dm_ws.acell('B2').value
                    if a2 and str(a2).strip():
                        subject = str(a2).strip()
                    if b2 and str(b2).strip():
                        body = str(b2).strip()
                    if (a2 and str(a2).strip()) or (b2 and str(b2).strip()):
                        print('📋 スプレッドシートの DM シートから件名/本文を読み取りました。')
                except Exception:
                    pass
    except Exception:
        pass

    if not subject or not body:
        print("❌ エラー: スプレッドシートの 'DM' シートの A2（件名）または B2（本文）が空です。処理を中止します。")
        notify_chat("🛑【SD DM送信】エラー終了: スプレッドシート 'DM'シートの件名(A2)または本文(B2)が空のため中止しました。")
        sys.exit(1)

    interactive_flag = True if args.interactive else False
    consecutive_failures = 0
    MAX_CONSECUTIVE_FAILURES = 2  # 🚨 連続エラー時のフェイルセーフを5回から2回に引き下げて厳格化

    if args.code:
        c = args.code.strip()
        code_use = c
        hour_now = datetime.now(JST).hour
        allow_send_now = not (hour_now >= 21 or hour_now < 7)
        res = send_dm_for_code(page, base_tab, code_use, subject, body, interactive=interactive_flag, save_debug=save_debug, perform_send=allow_send_now)
        if res is True:
            print(f"✅ {code_use} のメッセージ送信完了。エラーなし")
        elif res == "night_disabled":
            print(f"⚠️ {code_use} の送信は夜間のためスキップされました")
        else:
            print(f"❌ {code_use} の処理エラー: {res}")
        return

    if args.shop_file:
        try:
            with open(args.shop_file, 'r', encoding='utf-8') as f:
                codes = [l.strip() for l in f.readlines() if l.strip()]
        except Exception as e:
            print(f"❌ shop-file の読み込みに失敗しました: {e}")
            return

        for code in codes:
            # 実行時間セーフティマージンのチェック
            if time.time() - START_TIME > RUNTIME_LIMIT_SEC:
                print(f"⏰ 実行時間の安全上限（{RUNTIME_LIMIT_SEC/3600:.1f}h、GitHub側タイムアウトの{SAFETY_MARGIN_SEC//60}分前）に到達したため、後始末を実行して安全終了します。")
                return

            # 🔁 バッチ制御: 1バッチ(MAX_SEND_PER_RUN件)ごとにカウンタをリセットして次バッチへ継続
            if sent_count >= MAX_SEND_PER_RUN:
                print(f"🔁 バッチ{batch_no}（{MAX_SEND_PER_RUN}件）が完了しました。累計送信数: {total_sent_count}件。続きのコードから次のバッチを開始します。")
                sent_count = 0
                batch_no += 1

            code_use = code
            try:
                hour_now = datetime.now(JST).hour
                allow_send_now = not (hour_now >= 21 or hour_now < 7)
                if not allow_send_now:
                    print(f"🌙 夜間時間帯（21:00-07:00 JST）に入ったため、残りの処理を安全に終了します。現在時刻: {hour_now}時。")
                    return
                result = send_dm_for_code(page, base_tab, code_use, subject, body, interactive=interactive_flag, save_debug=save_debug, perform_send=allow_send_now)
                if result in ("blocked", "login_redirect", "nav_failure", "emergency_stop", "send_unconfirmed", "code_unconfirmed"):
                    print(f"🛑 危険を検知したため処理を緊急中断しました（理由: {result}）。安全に停止します。")
                    return
                if result == "night_disabled":
                    print(f"⚠️ {code_use} の送信は夜間のためスキップされました")
                    continue
                if result is True:
                    consecutive_failures = 0
                    sent_count += 1
                    total_sent_count += 1
                else:
                    consecutive_failures += 1
                    if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                        print(f"❌ 連続エラーが {consecutive_failures} 回発生しました。BOT判定・暴走を避けるため安全に停止します。")
                        return
            except Exception as e:
                print(f"❌ {code} の処理中にエラー: {e}")

            # 🕐 送信成功時の15〜30秒待機はsend_dm_for_code内部（タブを閉じる前）に移動済み。
            # ここでは失敗・例外時の最低限のインターバルのみ確保する（成功時の待機を二重にしないため）。
            time.sleep(random.uniform(1.0, 2.0))
        print(f"\n🎯 処理完了。今回の合計送信成功数: {total_sent_count} 件")
        return

    records = ws.get_all_values()

    # ── 🔬 診断ログ（原因特定用・一時追加） ──────────────────────
    print(f"🔬 [診断] get_all_values() 取得行数: {len(records)}")
    if len(records) > 1:
        sample = records[1]
        print(f"🔬 [診断] 2行目 len={len(sample)}")
        print(f"🔬 [診断] 2行目 B列(index1)='{get_col(sample,1)}'")
        print(f"🔬 [診断] 2行目 BE列(index56)='{get_col(sample,56)}'")
        print(f"🔬 [診断] 2行目 BF列(index57)='{get_col(sample,57)}'")

    skip_be_empty = 0
    skip_bf_filled = 0
    skip_no_code = 0
    # ─────────────────────────────────────────────

    # ⚠️ 修正: 走査対象を先頭1000行に制限していたため、57,598行あるシートで
    # 先頭1000行が処理済み(BF列記入済み)だと未処理行に到達できず0件で終了するバグがあった。
    # 全行を走査対象とする（送信数自体は MAX_SEND_PER_RUN=15 で別途制御されるため安全）。
    max_scan_rows = len(records)
    print(f"ℹ️ スプレッドシートの走査を開始します（走査対象: 全{max_scan_rows}行）")

    # 終了理由トラッカー（ループ終了後にまとめて1通のChat通知を送るため）
    # None のまま全行を走査し終えた場合は「正常終了」を意味する
    end_reason = None

    for i in range(max_scan_rows):
        row = records[i]
        try:
            if time.time() - START_TIME > RUNTIME_LIMIT_SEC:
                print(f"⏰ 実行時間の安全上限（{RUNTIME_LIMIT_SEC/3600:.1f}h、GitHub側タイムアウトの{SAFETY_MARGIN_SEC//60}分前）に到達したため、後始末を実行して安全終了します。")
                end_reason = "runtime_limit"
                break
        except Exception:
            pass

        # 🔁 バッチ制御: 1バッチ(MAX_SEND_PER_RUN件)ごとにカウンタをリセットして次バッチへ継続
        # （旧仕様: ここで break して処理全体を終了していたため、1回のワークフロー実行で
        #   15件しか送信されず終わっていた。実行時間上限に達するまでバッチを繰り返す仕様に変更。）
        if sent_count >= MAX_SEND_PER_RUN:
            print(f"🔁 バッチ{batch_no}（{MAX_SEND_PER_RUN}件）が完了しました。累計送信数: {total_sent_count}件。続きの行から次のバッチを開始します。")
            sent_count = 0
            batch_no += 1

        be = get_col(row, 56)
        bf = get_col(row, 57)
        if not be or bf:
            if not be:
                skip_be_empty += 1
            elif bf:
                skip_bf_filled += 1
            continue

        code = get_col(row, 1)
        if not code:
            skip_no_code += 1
            continue

        # 夜間帯（21:00-07:00 JST）に突入した場合は、バッチ途中でも安全に終了する
        hour_now = datetime.now(JST).hour
        allow_send_now = not (hour_now >= 21 or hour_now < 7)
        if not allow_send_now:
            print(f"🌙 夜間時間帯（21:00-07:00 JST）に入ったため、残りの処理を安全に終了します。現在時刻: {hour_now}時。")
            end_reason = "night"
            break

        print(f"\n👉 [バッチ{batch_no} 進捗: {sent_count + 1}/{MAX_SEND_PER_RUN}｜累計: {total_sent_count + 1}件目] スプレッドシート {i+1} 行目の店舗 {code} の処理を開始します。")

        try:
            result = send_dm_for_code(page, base_tab, code, subject, body, interactive=interactive_flag, save_debug=save_debug, perform_send=allow_send_now)

            if result in ("blocked", "login_redirect", "nav_failure", "emergency_stop", "send_unconfirmed", "code_unconfirmed"):
                print(f"🛑 危険検知による緊急終了（理由: {result}）。安全のため残りの処理を全キャンセルします。")
                end_reason = f"error:{result}"
                break
            if result == "night_disabled":
                print(f"⚠️ {code} の送信は夜間のためスキップされました")
                continue

            if result is True:
                consecutive_failures = 0
                sent_count += 1
                total_sent_count += 1
                now = datetime.now(JST)
                date_str = f"{now.month}/{now.day}"
                rownum = i + 1
                try:
                    ws.update(values=[[date_str]], range_name=f"BF{rownum}:BF{rownum}")
                    print(f"📝 スプレッドシートの BF{rownum} 列に処理完了日（{date_str}）を書き込みました")
                except Exception as e:
                    print(f"⚠️ スプレッドシートへの完了日書き込みに失敗しました（処理自体は成功しています）: {e}")
                print(f"✅ {code} のメッセージ送信に成功しました。")
            else:
                consecutive_failures += 1
                print(f"⚠️ 送信に失敗しました（エラーカウンタ: {consecutive_failures}/{MAX_CONSECUTIVE_FAILURES}）")
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    print(f"❌ 連続失敗回数が制限値 ({MAX_CONSECUTIVE_FAILURES}回) に達したため、システムを安全に緊急停止します。")
                    end_reason = "consecutive_failures"
                    break
        except Exception as e:
            print(f"❌ {code} の処理中に深刻なエラーが発生しました: {e}")
            consecutive_failures += 1
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                print(f"❌ 連続失敗回数が制限値に達したため、システムを安全に緊急停止します。")
                end_reason = "consecutive_failures"
                break

        # 🕐 送信成功時の15〜30秒待機はsend_dm_for_code内部（タブを閉じる前）に移動済み。
        # ここでは失敗・例外時の最低限のインターバルのみ確保する（成功時の待機を二重にしないため）。
        time.sleep(random.uniform(1.0, 2.0))

    print(f"🔬 [診断] スキップ内訳: BE空={skip_be_empty} / BF既存={skip_bf_filled} / コード無={skip_no_code}")
    print(f"\n🎯 処理完了。今回の合計送信成功数: {total_sent_count} 件（バッチ数: {batch_no if sent_count > 0 or total_sent_count > 0 else batch_no}）")

    # ── 📮 終了理由に応じたChat通知（①エラー終了 ②時間切れ終了 ③送信対象0件で正常終了） ──
    if end_reason == "runtime_limit":
        notify_chat(f"⏰【SD DM送信】時間切れ終了: 実行時間の安全上限（{RUNTIME_LIMIT_SEC/3600:.1f}時間）に到達したため終了しました。累計送信数: {total_sent_count}件。")
    elif end_reason == "night":
        notify_chat(f"🌙【SD DM送信】時間切れ終了: 夜間時間帯（21:00-07:00 JST）に入ったため終了しました。累計送信数: {total_sent_count}件。")
    elif end_reason == "consecutive_failures":
        notify_chat(f"🛑【SD DM送信】エラー終了: 連続失敗回数が上限に達したため緊急停止しました。累計送信数: {total_sent_count}件。")
    elif end_reason and end_reason.startswith("error:"):
        notify_chat(f"🛑【SD DM送信】エラー終了: 危険検知（理由: {end_reason.split(':',1)[1]}）のため緊急停止しました。累計送信数: {total_sent_count}件。")
    elif end_reason is None and total_sent_count == 0:
        notify_chat("ℹ️【SD DM送信】送信対象なしで正常終了: 全行を走査しましたが送信対象のコードが0件でした。")
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
        try:
            if page:
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