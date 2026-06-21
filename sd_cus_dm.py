#!/usr/bin/env python3
import os
import sys
import time
import random
import glob
import argparse
from datetime import datetime

from dotenv import load_dotenv
import gspread
from DrissionPage import ChromiumPage, ChromiumOptions

# match sd_cus.py startup: ensure .313p utilities in path, then import start_chrome
sys.path.append(r"C:\data\dev\.313p")
from chrome_utils import start_chrome

load_dotenv()

SS_ID = os.getenv("SS_SD_CUS_ID")

CREDENTIAL_PATH = r"C:\data\dev\sd_cus\credentials.json"
TOKEN_PATH = r"C:\data\dev\sd_cus\token.json"

# Initialize gspread at import time (like sd_cus.py) using provided credentials/token
try:
    gc = gspread.oauth(
        credentials_filename=CREDENTIAL_PATH,
        authorized_user_filename=TOKEN_PATH
    )
    sh = gc.open_by_key(SS_ID)
    ws = sh.worksheet("CUS_TO_SD")
    print(f"スプレッドシートを開きました: {SS_ID}")
except Exception as e:
    gc = None
    sh = None
    ws = None
    print('スプレッドシート初期化失敗:', e)

# start or attach chrome and prepare DrissionPage connection
start_chrome()
co = ChromiumOptions()
co.set_local_port(9333)
page = ChromiumPage(co)
base_tab = page.get_tab(page.latest_tab)


def get_col(row_data, idx):
    return row_data[idx].strip() if len(row_data) > idx else ""


def read_body_from_file(path):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return f.read().strip()
    except Exception:
        return None


def send_dm_for_code(browser_page, tab, code, subject, body, delay_range=(1.0, 2.5), interactive=False, save_debug=False):
    url = f"https://www.superdelivery.com/l/management/customer/detail.do?code={code}"
    print(f"処理: {code} -> {url}")
    try:
        tab.get(url)
    except Exception:
        try:
            tab.open(url)
        except Exception:
            print('ページ遷移に失敗しました')
            return False
    time.sleep(random.uniform(3.0, 5.0))

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
        print('編集タブを開けません。')
        return False

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
        print(f"アプローチ {name} → セット後: 件名='{check_subj}' 本文長={len(check_body) if check_body else 0}")

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
        print("件名・本文のセット確認をスキップしました（検証JS限界）。送信処理を続行します。")
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

    # 送信ボタンを押す
    try:
        new_tab.run_js("var s=document.querySelector(\"input[value='メッセージを送信'], input[value*='送信']\"); if(s) s.click();")
    except Exception:
        pass

    # wait after sending to ensure request completes
    time.sleep(random.uniform(1.5, 3.0))

    # タブを閉じて元に戻る
    try:
        try:
            if new_tab and hasattr(new_tab, 'close'):
                new_tab.close()
        except Exception:
            pass
    except Exception:
        pass

    print(f"{code} のメッセージ送信完了。")
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
        send_dm_for_code(page, base_tab, code_use, subject, body, interactive=interactive_flag, save_debug=save_debug)
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
                send_dm_for_code(page, base_tab, code_use, subject, body, interactive=interactive_flag, save_debug=save_debug)
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
        be = get_col(row, 56)
        bf = get_col(row, 57)
        if not be or bf:
            continue
        code = get_col(row, 1)
        if not code:
            continue
        try:
            ok = send_dm_for_code(page, base_tab, code, subject, body, interactive=interactive_flag, save_debug=save_debug)
            if ok:
                now = datetime.now()
                date_str = f"{now.month}/{now.day}"
                rownum = i + 1
                try:
                    ws_local.update(values=[[date_str]], range_name=f"BF{rownum}:BF{rownum}")
                    print(f"行 {rownum} の列BFに日付を記録しました: {date_str}")
                except Exception as e:
                    print(f"スプレッドシート更新失敗: {e}")
        except Exception as e:
            print(f"{code} の処理中にエラー: {e}")
        time.sleep(random.uniform(1.0, 3.0))


if __name__ == '__main__':
    main()
