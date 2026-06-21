#!/usr/bin/env python3
import os
import sys
import time
import random
import math
import glob
import argparse
from datetime import datetime
from dotenv import load_dotenv
import gspread
from DrissionPage import ChromiumPage, ChromiumOptions

# match sd_cus.py startup: ensure .313p utilities in path, then import start_chrome
sys.path.append(r"C:\data\dev\.313p")
import chrome_utils

# set debug port to 9333 to mirror sd_cus.py behavior but using 9333
chrome_utils.CHROME_DEBUG_PORT = 9333
chrome_utils.CHROME_USER_DATA_DIR = os.path.join(
    os.path.expanduser("~"), "AppData", "Local", "Temp", "chrome_dev_profile_9333"
)

# placeholders - actual initialization (Chrome, gspread, page) is done in main()
gc = None
sh = None
ws = None
page = None
base_tab = None
SS_ID = None
CREDENTIAL_PATH = r"C:\data\dev\sd_cus\credentials.json"
TOKEN_PATH = r"C:\data\dev\sd_cus\token.json"


def get_col(row_data, idx):
    return row_data[idx].strip() if len(row_data) > idx else ""


def read_body_from_file(path):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return f.read().strip()
    except Exception:
        return None


def human_delay(mean=0.0, sigma=0.6, minimum=0.2):
    """Sleep a small, log-normal-like human delay (no network activity)."""
    try:
        val = math.exp(random.gauss(mean, sigma))
        time.sleep(max(minimum, val))
    except Exception:
        time.sleep(minimum)


def detect_block(tab):
    """
    ページ遷移後にBOT検知・セッション切れを判定する。
    戻り値: "blocked" | "login_redirect" | None
    """
    try:
        current_url = tab.url or ""
    except Exception:
        current_url = ""

    # ログイン画面へリダイレクトされた場合
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
    # concise output: status and URL only
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
            # mark errors (4xx/5xx) clearly
            mark = 'ERROR' if (isinstance(status, int) and status >= 400) else 'OK'
            print(f"  [{mark}] {label} {url}")
        except Exception:
            print(f"  [ERR] パケット解析失敗 {getattr(p, 'url', '')}")


def drain(tab, step_label, timeout=2.0):
    # silent drain: consume recent packets without printing
    try:
        _ = tab.listen.wait(count=999, timeout=timeout, fit_count=False)
    except Exception:
        pass


def send_dm_for_code(browser_page, tab, code, subject, body, delay_range=(1.0, 2.5), interactive=False, save_debug=False, perform_send=True):
    url = f"https://www.superdelivery.com/l/management/customer/detail.do?code={code}"
    print(f"処理: {code} -> {url}")
    # start passive network capture on the tab (no extra requests)
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
            print('ページ遷移に失敗しました')
            return "nav_failure"
    time.sleep(random.uniform(3.0, 5.0))
    try:
        drain(tab, "STEP1 詳細ページ遷移", timeout=2.5)
    except Exception:
        pass

    # BOT検知・セッション切れチェック
    block_reason = detect_block(tab)
    if block_reason == "login_redirect":
        print(f"[{code}] セッション切れ: ログイン画面へリダイレクトされました。処理を停止します。")
        return "login_redirect"
    elif block_reason == "blocked":
        print()
        print("=" * 60)
        print(f"BOT検知またはアクセス拒否 ({code})")
        print("  対処: 時間を置いてから再実行してください。")
        print("=" * 60)
        print()
        return "blocked"

    # ループごとに必ず一時停止（インタラクティブ時）
    if interactive:
        print(f"レコード {code} を処理します。準備ができたら Enter を押してください...")
        try:
            input()
        except Exception:
            pass

    # ボタンを探してクリック（新しいタブが開く想定）
    try:
        handles_before = list(browser_page.tab_ids)
    except Exception:
        handles_before = []

    # 複数の候補セレクタでボタンを探す
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

    # small pre-click jitter to mimic human hesitation
    human_delay(mean=-0.5, sigma=0.5, minimum=0.15)
    if not btn:
        # 念のためJSで検索
        try:
            tab.run_js("var b=document.querySelector(\"input[value*='メッセージ']\"); if(b) b.click();")
        except Exception:
            print("メッセージボタンが見つかりません。スキップします。")
            return False
    else:
        try:
            btn.click()
        except Exception:
            try:
                tab.run_js("var b=document.querySelector(\"input[value*='メッセージ']\"); if(b) b.click();")
            except Exception:
                print("ボタンのクリックに失敗しました。スキップします。")
                return False

    # 新しいタブまたは遷移先を待機
    new_tab = None
    for _ in range(10):
        time.sleep(1.0 if interactive else 0.4)
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
        # 既に '/i/msgbox/edit' を開いているタブがないか確認する
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
            # 別タブで確実に開く
            try:
                tab.run_js("window.open('https://www.superdelivery.com/i/msgbox/edit','_blank');")
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
        print('編集タブを開きません。')
        return "nav_failure"

    # 新タブ側も受動的に通信を監視
    try:
        try:
            new_tab.listen.start(targets=True, method=True, res_type=True)
        except Exception:
            pass
    except Exception:
        pass

    # 対話モードならここで一時停止して目視確認させる
    if interactive:
        print('編集タブを開きました。ブラウザで内容を確認してください。準備できたら Enter を押してください...')
        try:
            input()
        except Exception:
            pass

    # 件名・本文要素が出るまで待機
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

    # 複数アプローチで実際にセットできるか試す
    approaches = []

    # 1: 直接JSでIDを設定
    def approach_js_ids(tab_):
        tab_.run_js("var s=document.getElementById('new-mail-subject'); if(s) s.value=arguments[0]; var b=document.getElementById('new-mail-body'); if(b) b.value=arguments[1];", subject, body)

    approaches.append(('run_js_ids', approach_js_ids))

    # 2: 要素を取得して element.input() を使う
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

    # 3: 汎用的に textarea/input を探して input() を試す
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

    # randomize approach order slightly to avoid deterministic pattern
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
        # short pause after attempting an approach to let browser update
        time.sleep(0.8)
        try:
            check_subj = new_tab.run_js("return document.getElementById('new-mail-subject') ? document.getElementById('new-mail-subject').value : null;", as_expr=True)
        except Exception:
            check_subj = None
        try:
            check_body = new_tab.run_js("return document.getElementById('new-mail-body') ? document.getElementById('new-mail-body').value : null;", as_expr=True)
        except Exception:
            check_body = None
        # concise mode: do not print per-approach debug output

        # give browser a moment to process input
        time.sleep(1.0)

        if (check_subj and len(str(check_subj).strip())>0) or (check_body and len(str(check_body).strip())>0):
            if interactive:
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

    if not success_method:
        # skip verbose confirmation; continue with send flow
        pass
    # デバッグ用に現在の編集タブのHTMLを保存（オプトイン）
    if save_debug:
        try:
            html = new_tab.html
            fname = f"debug_msgbox_{code}.html"
            with open(fname, 'w', encoding='utf-8') as f:
                f.write(html)
            print(f"編集タブのHTMLを保存しました: {fname}")
        except Exception:
            pass

    # インタラクティブモードなら目視確認を挟む
    if interactive:
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

    # 確認画面へボタンを押す
    try:
        new_tab.run_js("var b=document.querySelector(\"input[value='確認画面へ'], input[value*='確認画面']\"); if(b) b.click();")
    except Exception:
        pass
    time.sleep(random.uniform(1.0, 2.0))

    try:
        drain(new_tab, "STEP4 確認画面へ遷移後", timeout=2.5)
    except Exception:
        pass
    # 送信ボタンを押す
    # If sending is disabled (night mode), skip actual send and return special code
    if not perform_send:
        return "night_disabled"
    try:
        new_tab.run_js("var s=document.querySelector(\"input[value='メッセージを送信'], input[value*='送信']\"); if(s) s.click();")
    except Exception:
        pass
    # wait after sending to ensure request completes
    time.sleep(random.uniform(1.5, 3.0))
    try:
        drain(new_tab, "STEP5 送信ボタンクリック後", timeout=2.5)
    except Exception:
        pass

    # タブを閉じて元に戻る
    try:
        try:
            if new_tab and hasattr(new_tab, 'close'):
                new_tab.close()
        except Exception:
            pass
    except Exception:
        pass

    return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--save-debug', action='store_true', help='編集タブのHTMLを debug_msgbox_*.html として保存します（デフォルト: 保存しない）')
    parser.add_argument('--clean-debug', action='store_true', help='実行前に既存の debug_msgbox_*.html を削除します')
    parser.add_argument('--code', '-c', help='単一テスト用6桁またはフルコード')
    parser.add_argument('--interactive', '-i', action='store_true', help='対話モードで一時停止する (デフォルト: なし)')
    parser.add_argument('--shop-file', '-f', help='処理する店舗コードを改行で並べたファイルパス')
    parser.add_argument('--ss-id', help='処理するスプレッドシートのID（省略時は環境変数を使用）')
    args = parser.parse_args()
    # runtime controls
    START_TIME = time.time()
    MAX_RUNTIME_SEC = 6 * 3600  # 6 hours

    # night window: 20:00-07:59 local time (host system time)
    hour_now = datetime.now().hour
    if hour_now >= 20 or hour_now < 8:
        print("夜間時間帯のため処理を停止します（20:00-08:00）。終了します。")
        return

    # not night: allow sends unless per-record check later overrides
    allow_send = True

    # start or attach chrome (will print port number from chrome_utils)
    chrome_utils.start_chrome()
    print("Chrome 起動完了（ポート9333）。ブラウザで操作してください。準備できたら Enter を押してください...")
    try:
        input()
    except Exception:
        pass

    # load environment and sheet id
    load_dotenv()
    SS_ID = os.getenv("SS_SD_CUS_ID")

    # Initialize gspread here (after night check and after user confirmation)
    try:
        gc = gspread.oauth(credentials_filename=CREDENTIAL_PATH, authorized_user_filename=TOKEN_PATH)
        sh = gc.open_by_key(SS_ID)
        ws = sh.worksheet("CUS_TO_SD")
        print(f"スプレッドシートを開きました: {SS_ID}")
    except Exception as e:
        gc = None
        sh = None
        ws = None
        print('スプレッドシート初期化失敗:', e)

    # connect to the running chrome on the configured debug port (9333)
    co = ChromiumOptions()
    co.set_local_port(9333)
    page = ChromiumPage(co)
    base_tab = page.get_tab(page.latest_tab)

    # CLI flags
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
                print(f"既存の debug_msgbox_*.html を {removed} 件削除しました。")
        except Exception:
            pass

    # subject/body will be read from the spreadsheet DM sheet (A2/B2)
    subject = None
    body = None

    # use pre-initialized Chrome page and worksheet (initialized at module import)
    base_tab = page.get_tab(page.latest_tab)

    ws_local = ws
    ss_id_use = args.ss_id or SS_ID
    if not ws_local and ss_id_use:
        try:
            gc_local = gspread.oauth(credentials_filename=CREDENTIAL_PATH, authorized_user_filename=TOKEN_PATH)
            sh_local = gc_local.open_by_key(ss_id_use)
            ws_local = sh_local.worksheet('CUS_TO_SD')
            print(f"スプレッドシートを開きました: {ss_id_use}")
        except Exception as e:
            print('スプレッドシートを開けません:', e)
            return
    interactive_flag = True if args.interactive else False

    # Ensure sh_local variable exists (may have been set above) — fall back to module-level `sh`
    try:
        _ = sh_local
    except Exception:
        sh_local = sh

    # If spreadsheet is available, try to read subject/body from sheet named 'DM' (A2/B2)
    try:
        if sh_local:
            try:
                dm_ws = sh_local.worksheet('DM')
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
                        print('スプレッドシートの DM シートから件名/本文を読み取りました。')
                except Exception:
                    pass
    except Exception:
        pass

    # Require subject and body from DM sheet only
    if not subject or not body:
        print("エラー: スプレッドシートの 'DM' シートの A2（件名）または B2（本文）が空です。処理を中止します。")
        sys.exit(1)

    if args.code:
        c = args.code.strip()
        code_use = c
        # re-evaluate allow_send at call time to handle runtime crossings into night window
        hour_now = datetime.now().hour
        allow_send_now = not (hour_now >= 20 or hour_now < 8)
        res = send_dm_for_code(page, base_tab, code_use, subject, body, interactive=interactive_flag, save_debug=save_debug, perform_send=allow_send_now)
        # single-line summary output
        if res is True:
            print(f"{code_use} のメッセージ送信完了。エラーなし")
        elif res == "night_disabled":
            print(f"{code_use} の送信は夜間のためスキップされました")
        else:
            print(f"{code_use} の処理エラー: {res}")
        return
        return

    if args.shop_file:
        try:
            with open(args.shop_file, 'r', encoding='utf-8') as f:
                codes = [l.strip() for l in f.readlines() if l.strip()]
        except Exception as e:
            print(f"shop-file の読み込みに失敗しました: {e}")
            return

        for code in codes:
            code_use = code
            try:
                hour_now = datetime.now().hour
                allow_send_now = not (hour_now >= 20 or hour_now < 8)
                result = send_dm_for_code(page, base_tab, code_use, subject, body, interactive=interactive_flag, save_debug=save_debug, perform_send=allow_send_now)
                if result in ("blocked", "login_redirect", "nav_failure"):
                    print(f"処理を中断しました（理由: {result}）。")
                    return
                if result == "night_disabled":
                    print(f"{code_use} の送信は夜間のためスキップされました")
                    continue
            except Exception as e:
                print(f"{code} の処理中にエラー: {e}")
            time.sleep(random.uniform(1.0, 2.0))
        return

    # Default: read from sheet
    if not ws_local:
        print('処理対象が指定されていません（shop-file または スプレッドシートが必要）。')
        return

    records = ws_local.get_all_values()

    for i, row in enumerate(records):
        # runtime: stop if exceeded max runtime
        try:
            if time.time() - START_TIME > MAX_RUNTIME_SEC:
                print("⏰ 実行時間上限（6h）到達: 安全終了します")
                break
        except Exception:
            pass
        be = get_col(row, 56)
        bf = get_col(row, 57)
        if not be or bf:
            continue
        code = get_col(row, 1)
        if not code:
            continue
        try:
            hour_now = datetime.now().hour
            allow_send_now = not (hour_now >= 20 or hour_now < 8)
            result = send_dm_for_code(page, base_tab, code, subject, body, interactive=interactive_flag, save_debug=save_debug, perform_send=allow_send_now)
            if result in ("blocked", "login_redirect", "nav_failure"):
                print(f"処理を中断しました（理由: {result}）。処理済み件数でスプレッドシートは更新済みです。")
                break
            if result == "night_disabled":
                print(f"{code} の送信は夜間のためスキップされました")
                continue
            if result is True:
                now = datetime.now()
                date_str = f"{now.month}/{now.day}"
                rownum = i + 1
                # single-line success output; still update sheet but do not print extra lines
                try:
                    ws_local.update(values=[[date_str]], range_name=f"BF{rownum}:BF{rownum}")
                except Exception:
                    pass
                print(f"{code} のメッセージ送信完了。エラーなし")
        except Exception as e:
            print(f"{code} の処理中にエラー: {e}")
        time.sleep(random.uniform(1.0, 3.0))


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        try:
            print('\n処理を中断しました（KeyboardInterrupt） — 安全に停止します')
        except Exception:
            pass
    except SystemExit as e:
        try:
            print(f'\n終了: {e}')
        except Exception:
            pass
    except Exception as e:
        print(f'\n予期しない例外: {e}')
        raise