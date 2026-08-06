#!/usr/bin/env python3
"""
sd_cus_dm 画像添付機能 単体テストスクリプト
============================================

【目的】
  SuperDelivery メッセージ編集画面での「画像添付」処理だけを検証する。
  件名/本文の入力、確認画面への遷移、送信ボタン押下は一切行わない。

【フロー】
  1. Chrome をデバッグポート 9444 で単独起動（サイレントではない。
     この画面はあなたが操作するためのもの）
  2. https://www.superdelivery.com/i/authentication/form を開いて一時停止
  3. あなたが手動でログイン → さらに画像添付をテストしたい
     メッセージ編集画面（「ファイルを選択」等が表示される画面）まで
     手動で遷移する
  4. Enter を押すと再開:
     - sd_cus の credentials.json / token.json（既存のgspread認証）を流用し、
       Google Drive API 経由で TEST_IMAGE_URLS の画像を認証付きダウンロード
       (C:\\data\\dev\\temp-sandbox に保存)。フォルダのリンクを指定した場合は
       中の画像ファイルを自動で全部列挙します。公開設定（リンクを知っている
       全員）は不要です。
     - ページ上の <input type=file> を探して直接ファイルパスをセット
       （OSのファイル選択ダイアログは使わず、DrissionPage経由でCDPから
       直接設定するため、ダイアログ操作より確実で高速）
  5. あなたが画面を目視確認する
  6. Enter を押すと一時ファイルを削除して終了

【使い方】
  1. このファイルの TEST_IMAGE_URLS に、テストしたい Google ドライブの
     共有リンクを1〜数個入れてから実行してください。
  2. 実行: C:\\data\\dev\\.313p\\.venv\\Scripts\\python.exe C:\\data\\dev\\sd_cus\\test.py
"""

import os
import re
import sys
import json
import time
import requests
from dotenv import load_dotenv
from DrissionPage import ChromiumPage, ChromiumOptions
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as GoogleAuthRequest

# .313p 配下の共通ユーティリティ（chrome_utils）を使う
sys.path.append(r"C:\data\dev\.313p")
import chrome_utils

# ------------------------------------------------------------------
# 設定
# ------------------------------------------------------------------
# ポート9444・専用プロファイルで起動（他プロジェクト＝9333等と衝突しないように分離）
chrome_utils.CHROME_DEBUG_PORT = 9444
chrome_utils.CHROME_USER_DATA_DIR = os.path.join(
    os.path.expanduser("~"), "AppData", "Local", "Temp", "chrome_dev_profile_9444"
)

LOGIN_URL = "https://www.superdelivery.com/i/authentication/form"
TEMP_DIR = r"C:\data\dev\temp-sandbox"

CREDENTIAL_PATH = r"C:\data\dev\sd_cus\credentials.json"
TOKEN_PATH = r"C:\data\dev\sd_cus\token.json"

# ここにテスト用の Google ドライブのリンクを入れてください。
# ★必ずダブルクォートで囲むこと（"..."）。囲まないと SyntaxError になります。
# ・フォルダのリンクでもOK（中の画像ファイルを自動で全部列挙してダウンロードします）
# ・個別ファイルのリンク（/file/d/.../view）でもOK
TEST_IMAGE_URLS = [
    "https://drive.google.com/drive/u/2/folders/1RCmkTGNhKF_0xSPzzlqEIY4RaFbve-P3",
]


# ------------------------------------------------------------------
# Google ドライブ認証（sd_cus の credentials.json / token.json を流用）
# ------------------------------------------------------------------
def get_drive_access_token():
    """
    gspread.oauth() で発行済みの token.json を使い、Drive API 用の
    アクセストークンを取得する。gspread のデフォルトスコープには
    通常 'https://www.googleapis.com/auth/drive' が含まれているため、
    同じ token.json をそのまま Drive API 呼び出しに流用できる。
    """
    if not os.path.exists(TOKEN_PATH):
        print(f"  [ERROR] token.json が見つかりません: {TOKEN_PATH}")
        return None
    try:
        creds = Credentials.from_authorized_user_file(TOKEN_PATH)
    except Exception as e:
        print(f"  [ERROR] token.json 読み込み失敗: {e}")
        return None

    if not creds.valid:
        if creds.expired and creds.refresh_token:
            print("  トークンが期限切れのためリフレッシュします...")
            try:
                creds.refresh(GoogleAuthRequest())
            except Exception as e:
                print(f"  [ERROR] トークンのリフレッシュに失敗しました: {e}")
                print("  → sd_cus_dm.py 等を一度実行して再ログインしてから、"
                      "このスクリプトを再実行してください。")
                return None
        else:
            print("  [ERROR] token.json が無効で、リフレッシュもできません。")
            return None

    return creds.token


# ------------------------------------------------------------------
# Google ドライブから画像をダウンロード（Drive API v3・認証あり）
# ------------------------------------------------------------------
def is_folder_url(url):
    return '/folders/' in url


def extract_folder_id(url):
    m = re.search(r'/folders/([a-zA-Z0-9_-]+)', url)
    return m.group(1) if m else None


def extract_drive_file_id(url):
    """Google ドライブ共有URL（ファイル単体）からFILE_IDを抽出する"""
    m = re.search(r'/d/([a-zA-Z0-9_-]+)', url)
    if m:
        return m.group(1)
    m = re.search(r'[?&]id=([a-zA-Z0-9_-]+)', url)
    if m:
        return m.group(1)
    return None


def guess_ext_from_content_type(content_type):
    mapping = {
        'image/jpeg': '.jpg',
        'image/jpg': '.jpg',
        'image/png': '.png',
        'image/gif': '.gif',
        'image/webp': '.webp',
    }
    return mapping.get((content_type or '').split(';')[0].strip().lower(), '.jpg')


def list_folder_images(folder_id, access_token):
    """フォルダ内の画像ファイル一覧を取得する（{'id':..., 'name':...} のリスト）"""
    url = "https://www.googleapis.com/drive/v3/files"
    params = {
        'q': f"'{folder_id}' in parents and mimeType contains 'image/' and trashed = false",
        'fields': 'files(id,name,mimeType)',
        'pageSize': 100,
    }
    headers = {'Authorization': f'Bearer {access_token}'}
    print(f"  フォルダ内の画像を検索中... (folder_id={folder_id})")
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=30)
        resp.raise_for_status()
        files = resp.json().get('files', [])
        print(f"  検出: {len(files)} 件の画像ファイル")
        for f in files:
            print(f"    - {f.get('name')} ({f.get('id')})")
        return files
    except Exception as e:
        print(f"  [ERROR] フォルダ一覧取得に失敗しました: {e}")
        try:
            print(f"  レスポンス: {resp.text[:300]}")
        except Exception:
            pass
        return []


def get_file_metadata(file_id, access_token):
    """単体ファイルの name / mimeType を取得する"""
    url = f"https://www.googleapis.com/drive/v3/files/{file_id}"
    params = {'fields': 'id,name,mimeType'}
    headers = {'Authorization': f'Bearer {access_token}'}
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=15)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(f"  [ERROR] ファイル情報取得に失敗しました (file_id={file_id}): {e}")
        return None


def resolve_image_files(urls, access_token):
    """
    TEST_IMAGE_URLS（フォルダ/ファイル混在可）を、
    [{'id':..., 'name':...}, ...] の平坦なリストに解決する。
    """
    resolved = []
    for url in urls:
        if is_folder_url(url):
            folder_id = extract_folder_id(url)
            if not folder_id:
                print(f"  [ERROR] フォルダIDを抽出できません: {url}")
                continue
            files = list_folder_images(folder_id, access_token)
            resolved.extend(files)
        else:
            file_id = extract_drive_file_id(url)
            if not file_id:
                print(f"  [ERROR] FILE_ID を抽出できません: {url}")
                continue
            meta = get_file_metadata(file_id, access_token)
            if meta:
                resolved.append(meta)
    return resolved


def download_drive_file_by_id(file_id, name, index, access_token):
    """Drive API 経由でファイルを認証付きダウンロードする（公開設定不要）"""
    url = f"https://www.googleapis.com/drive/v3/files/{file_id}"
    params = {'alt': 'media'}
    headers = {'Authorization': f'Bearer {access_token}'}
    print(f"  [DL開始] {name} (id={file_id})")
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=60)
        resp.raise_for_status()
        ext = os.path.splitext(name or '')[1]
        if not ext:
            ext = guess_ext_from_content_type(resp.headers.get('Content-Type'))
        dest_path = os.path.join(TEMP_DIR, f"test_img_{index}{ext}")
        with open(dest_path, 'wb') as f:
            f.write(resp.content)
        size = os.path.getsize(dest_path)
        print(f"  [OK] ダウンロード完了: {dest_path} ({size} bytes)")
        return dest_path
    except Exception as e:
        print(f"  [ERROR] ダウンロード失敗 ({name}): {e}")
        return None


# ------------------------------------------------------------------
# 画像添付（DrissionPage経由・OSダイアログ不使用）
# ------------------------------------------------------------------
def find_file_inputs(tab):
    """現在のページ上の <input type=file> 要素を全て取得する"""
    try:
        eles = tab.eles('css:input[type=file]')
        return eles or []
    except Exception as e:
        print(f"  [ERROR] file input 検索失敗: {e}")
        return []


def find_upload_trigger(tab):
    """input[type=file] が見つからない場合の代替: 「ファイルを選択」等のボタンを探す"""
    selectors = [
        "xpath://input[contains(@value,'ファイルを選択')]",
        "xpath://button[contains(.,'ファイルを選択')]",
        "xpath://a[contains(.,'ファイルを選択')]",
        "xpath://*[contains(text(),'画像') and contains(text(),'追加')]",
    ]
    for sel in selectors:
        try:
            ele = tab.ele(sel, timeout=1)
        except Exception:
            ele = None
        if ele:
            return ele
    return None


# ------------------------------------------------------------------
# headless実行時に「目視確認」の代わりとなる、添付成功判定材料の自動収集
# ------------------------------------------------------------------
_DOM_SCAN_JS = """
    (function(){
        var results = [];
        // blob: プレビュー画像(<img src="blob:...">)は添付成功時の代表的な兆候
        document.querySelectorAll('img').forEach(function(img){
            if (img.src && img.src.indexOf('blob:') === 0) {
                results.push({type:'img_blob', tag: img.tagName, class: img.className, id: img.id, src: img.src.slice(0,60)});
            }
        });
        // それらしいクラス名/ID(サムネイル・プレビュー・添付一覧等)を持つ要素も候補として拾う
        var pattern = /thumb|preview|attach|upload|file[-_]?item|file[-_]?list|selected/i;
        document.querySelectorAll('[class],[id]').forEach(function(el){
            var cls = el.className || '';
            var idv = el.id || '';
            if (pattern.test(String(cls)) || pattern.test(String(idv))) {
                results.push({
                    type: 'name_match',
                    tag: el.tagName,
                    class: String(cls).slice(0,60),
                    id: String(idv).slice(0,60),
                    text: (el.textContent || '').trim().slice(0,30)
                });
            }
        });
        return JSON.stringify(results);
    })()
"""

# 前回のログで件数が変化しなかった、添付処理に直結していそうな要素のみ、
# 個数ではなく中身(outerHTML)をそのまま見るためのスキャン
# ※ DrissionPage の run_js() は list をそのまま引数として渡せないため、
#   セレクタはJSコード内に直接埋め込む（arguments経由にしない）。
_TARGETED_HTML_JS = """
    (function(){
        var selectors = ['.attachment', '.attaching-list', '.attaching-loading'];
        var out = {};
        selectors.forEach(function(sel){
            var els = document.querySelectorAll(sel);
            var arr = [];
            els.forEach(function(el){
                arr.push({
                    outerHTML: el.outerHTML.slice(0, 800),
                    style_display: window.getComputedStyle(el).display,
                    visible: !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
                });
            });
            out[sel] = arr;
        });
        return JSON.stringify(out);
    })()
"""


def scan_targeted_html(tab):
    """添付に直結していそうな要素の中身(outerHTML)をそのまま取得する"""
    try:
        raw = tab.run_js(_TARGETED_HTML_JS, as_expr=True)
        return json.loads(raw) if raw else {}
    except Exception as e:
        print(f"  [WARN] targeted HTMLスキャン失敗: {e}")
        return {}


def print_targeted_html(data, label=""):
    prefix = f"[中身確認{('・' + label) if label else ''}]"
    for sel, els in data.items():
        if not els:
            print(f"  {prefix} {sel} -> 要素なし")
            continue
        for i, el in enumerate(els):
            print(f"  {prefix} {sel} [{i}] display={el.get('style_display')} visible={el.get('visible')}")
            print(f"    {el.get('outerHTML')}")


def scan_dom_candidates(tab):
    """添付成功のヒントになりそうなDOM要素（プレビュー画像・それらしいクラス名など）を機械的に収集する"""
    try:
        raw = tab.run_js(_DOM_SCAN_JS, as_expr=True)
        return json.loads(raw) if raw else []
    except Exception as e:
        print(f"  [WARN] DOM候補スキャン失敗: {e}")
        return []


def print_dom_candidates(items, label=""):
    blob_count = sum(1 for it in items if it.get('type') == 'img_blob')
    name_matches = [it for it in items if it.get('type') == 'name_match']
    # クラス名単位で重複排除して件数を出す（同じ構造の要素が大量に一致するノイズ対策）
    seen = {}
    for it in name_matches:
        key = (it.get('tag'), it.get('class'))
        seen[key] = seen.get(key, 0) + 1
    print(f"  [DOM候補{('・' + label) if label else ''}] blob画像プレビュー: {blob_count} 件")
    if seen:
        print(f"  [DOM候補{('・' + label) if label else ''}] それらしい名前を持つ要素（種類別）:")
        for (tag, cls), cnt in list(seen.items())[:15]:
            print(f"    - <{tag} class=\"{cls}\"> x{cnt}")
    else:
        print(f"  [DOM候補{('・' + label) if label else ''}] class/id一致なし")


def attach_images(tab, image_paths):
    """
    画像を添付する。
    方式A（優先）: <input type=file> を直接検出し .input(path) でファイルパスをセット。
                    CDP経由で直接値をセットするためOSダイアログは一切出ない。確実で速い。
    方式B（代替）: input[type=file] が見つからない場合、「ファイルを選択」的なボタンを
                    click.to_upload(path) で押す。DrissionPageがネイティブの
                    ファイル選択ダイアログを自動で処理する。

    各ステップの前後でDOM候補スキャンを行い、添付1件ごとに検出件数がどう変化するかを
    記録する（headless実行での自動成功判定の材料を集めるための診断用）。
    """
    inputs = find_file_inputs(tab)
    print(f"  検出した <input type=file> 要素数: {len(inputs)}")

    ok_count = 0

    print("  [診断] 添付前のDOM状態を記録します...")
    baseline = scan_dom_candidates(tab)
    print_dom_candidates(baseline, label="添付前")
    print_targeted_html(scan_targeted_html(tab), label="添付前")

    if inputs:
        for i, path in enumerate(image_paths):
            target = inputs[i] if i < len(inputs) else inputs[-1]
            print(f"  [{i+1}/{len(image_paths)}] 方式A(直接input)で添付試行: {path}")
            try:
                target.input(path)
                time.sleep(1.0)
                ok_count += 1
                print("    -> セット完了")
            except Exception as e:
                print(f"    -> [ERROR] 添付失敗: {e}")
                continue
            # AJAXアップロードが走っている場合に備え、反映されるまで少し待ってからスキャン
            time.sleep(1.5)
            after = scan_dom_candidates(tab)
            print_dom_candidates(after, label=f"{i+1}枚目添付後")
            print_targeted_html(scan_targeted_html(tab), label=f"{i+1}枚目添付後")
        print(f"添付試行完了: {ok_count}/{len(image_paths)} 件成功（ブラウザで目視確認してください）")
        dump_debug_html(tab, "debug_attach_final")
        return ok_count > 0

    # 方式B: input要素が見つからない場合
    print("  <input type=file> が見つからないため、代替方式(ボタン+to_upload)を試します。")
    trigger = find_upload_trigger(tab)
    if not trigger:
        print("  [ERROR] 添付トリガーとなる要素も見つかりません。手動で画面を確認してください。")
        return False

    for i, path in enumerate(image_paths):
        print(f"  [{i+1}/{len(image_paths)}] 方式B(to_upload)で添付試行: {path}")
        try:
            trigger.click.to_upload(path)
            time.sleep(1.5)
            ok_count += 1
            print("    -> セット完了")
        except Exception as e:
            print(f"    -> [ERROR] 添付失敗: {e}")
            continue
        after = scan_dom_candidates(tab)
        print_dom_candidates(after, label=f"{i+1}枚目添付後")
        print_targeted_html(scan_targeted_html(tab), label=f"{i+1}枚目添付後")

    print(f"添付試行完了: {ok_count}/{len(image_paths)} 件成功（ブラウザで目視確認してください）")
    dump_debug_html(tab, "debug_attach_final")
    return ok_count > 0


def dump_debug_html(tab, name):
    """現在のページHTML全体をカレントディレクトリに保存する（後でClaudeに共有するため）"""
    try:
        html = tab.html
        fname = f"{name}.html"
        with open(fname, 'w', encoding='utf-8') as f:
            f.write(html)
        print(f"  [診断] ページHTMLを保存しました: {os.path.abspath(fname)}")
        print("  → このファイルをそのままアップロードしてもらえれば、プレビュー要素の特定に使えます。")
    except Exception as e:
        print(f"  [WARN] HTML保存に失敗しました: {e}")


# ------------------------------------------------------------------
# メイン処理
# ------------------------------------------------------------------
def main():
    load_dotenv()

    if not TEST_IMAGE_URLS:
        print("[ERROR] TEST_IMAGE_URLS が空です。")
        print("        スクリプト冒頭の TEST_IMAGE_URLS にテスト用の")
        print("        Google ドライブ共有リンクを1つ以上設定してから再実行してください。")
        return

    os.makedirs(TEMP_DIR, exist_ok=True)
    print(f"一時保存フォルダ: {TEMP_DIR}")

    # 1. Chrome をポート9444で単独起動（サイレントではない）
    print("Chrome を起動しています（ポート9444）...")
    chrome_utils.start_chrome()
    print("Chrome 起動完了（ポート9444）。")

    co = ChromiumOptions()
    co.set_local_port(9444)
    page = ChromiumPage(co)
    tab = page.get_tab(page.latest_tab)

    # 2. ログインページへ遷移
    print(f"ログインページへ遷移します: {LOGIN_URL}")
    try:
        tab.get(LOGIN_URL)
    except Exception as e:
        print(f"[WARN] ログインページへの遷移に失敗しました（手動で開いてください）: {e}")

    # 3. 一時停止（ログイン＋テスト対象画面への手動遷移を待つ）
    print()
    print("=" * 60)
    print("ブラウザで手動ログインしてください。")
    print("続けて、画像添付をテストしたいメッセージ編集画面まで")
    print("手動で遷移してください（「ファイルを選択」等が見える画面）。")
    print("準備ができたら Enter を押してください...")
    print("=" * 60)
    try:
        input()
    except Exception:
        pass

    # 4. Google Drive 認証 → 画像一覧解決 → ダウンロード
    print()
    print("Google Drive の認証情報を読み込んでいます...")
    access_token = get_drive_access_token()
    if not access_token:
        print("[ERROR] Drive API のアクセストークンを取得できませんでした。処理を中止します。")
        return

    print("画像ファイルを解決しています（フォルダの場合は中身を列挙します）...")
    image_files = resolve_image_files(TEST_IMAGE_URLS, access_token)
    if not image_files:
        print("[ERROR] 対象の画像ファイルが1件も見つかりませんでした。処理を中止します。")
        return
    print(f"対象画像: {len(image_files)} 件")

    print()
    print("画像をダウンロードしています...")
    downloaded_paths = []
    for i, f in enumerate(image_files, start=1):
        path = download_drive_file_by_id(f.get('id'), f.get('name'), i, access_token)
        if path:
            downloaded_paths.append(path)

    if not downloaded_paths:
        print("[ERROR] ダウンロードできた画像が1件もありません。処理を中止します。")
        return

    print(f"ダウンロード完了: {len(downloaded_paths)}/{len(image_files)} 件")

    # 5. 添付テスト（手動遷移後の最新タブを再取得してから実行）
    print()
    print("画像添付を試みます...")
    tab = page.get_tab(page.latest_tab)
    attach_images(tab, downloaded_paths)

    # 6. 目視確認 → Enterでクリーンアップ
    print()
    print("=" * 60)
    print("ブラウザで添付結果を目視確認してください。")
    print("確認が終わったら Enter を押すと、一時ダウンロードファイルを")
    print("削除してスクリプトを終了します。")
    print("=" * 60)
    try:
        input()
    except Exception:
        pass

    print("一時ファイルを削除しています...")
    for p in downloaded_paths:
        try:
            os.remove(p)
            print(f"  削除: {p}")
        except Exception as e:
            print(f"  [WARN] 削除失敗: {p} ({e})")

    print("テスト終了。")
    print()
    print("★ debug_attach_final.html が C:\\data\\dev\\sd_cus\\ に残っています。")
    print("  このファイルをアップロードしてもらえれば、headless実行での")
    print("  自動成功判定に使えるDOM要素を特定します。")


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('\n処理を中断しました（KeyboardInterrupt）')
    except Exception as e:
        print(f'\n予期しない例外: {e}')
        raise
