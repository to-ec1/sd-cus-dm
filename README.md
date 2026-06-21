# SD_CUS DM 自動送信ツール

概要:
- `sd_cus_dm.py` を使って SuperDelivery の顧客ページからメッセージを自動送信します。

事前準備:
- Chrome/Chromium をリモートデバッグポート9222で起動しておく:

```powershell
"C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222 --user-data-dir="C:\tmp\chrome-profile-sd"
```

- `sd_cus/credentials.json` と `sd_cus/token.json` に gspread 用認証情報を準備する。

簡単なテスト実行:

```powershell
cd C:\data\dev\sd_cus
python -u sd_cus_dm.py --code 419853 --body-file dm.txt --subject "【特価商品のご案内】テイクオン株式会社"
```

引数:
- `--subject`, `-s`: 件名（省略時はデフォルト件名を使用）
- `--body-file`, `-b`: 本文を読み取るファイルパス（デフォルト `dm.txt`）
- `--code`, `-c`: 単一顧客テスト用コード（6桁またはフルコード）

注意:
- ページ構造が変わるとボタンや入力のセレクタが動作しなくなります。その場合は `sd_cus_dm.py` のセレクタ定義を調整してください。
