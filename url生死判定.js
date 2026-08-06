/*
 * NETSEA URL 生死判定 + フォームリンク抽出（Apps Script版）
 */

const CONTINUATION_HANDLER = 'continueUrlViabilityCheck';

function getConfig() {
  const props = PropertiesService.getScriptProperties();
  const spreadsheetId = props.getProperty('SS_NETSEA_ID');
  const sheetName = props.getProperty('SHEET_NAME') || 'NETSEAサイトあり';
  const batchSize = parseInt(props.getProperty('BATCH_SIZE') || '100', 10);
  const maxRuntimeMs = parseInt(props.getProperty('MAX_RUNTIME_MS') || '300000', 10);
  const timeBufferMs = parseInt(props.getProperty('TIME_BUFFER_MS') || '30000', 10);
  const triggerDelaySec = parseInt(props.getProperty('TRIGGER_DELAY_SEC') || '60', 10);

  if (!spreadsheetId) {
    throw new Error('SS_NETSEA_ID が未設定です。スクリプトプロパティを確認してください。');
  }

  return {
    spreadsheetId,
    sheetName,
    batchSize: Number.isFinite(batchSize) && batchSize > 0 ? batchSize : 100,
    maxRuntimeMs: Number.isFinite(maxRuntimeMs) && maxRuntimeMs > 0 ? maxRuntimeMs : 300000,
    timeBufferMs: Number.isFinite(timeBufferMs) && timeBufferMs >= 0 ? timeBufferMs : 30000,
    triggerDelaySec: Number.isFinite(triggerDelaySec) && triggerDelaySec > 0 ? triggerDelaySec : 60,
  };
}

function getTargetRows(sheet) {
  const values = sheet.getDataRange().getValues();
  const targets = [];

  values.forEach((row, index) => {
    if (index === 0) return;

    const rowIndex = index + 1;
    const url = String(row[1] || '').trim();
    const colR = String(row[17] || '').trim();
    const colT = String(row[19] || '').trim();

    if (!url) return;
    if (colR || colT) return;
    if (!/^https?:\/\//i.test(url)) return;

    targets.push({ rowIndex, url });
  });

  Logger.log(`対象行: ${targets.length}件`);
  return targets;
}

// 元の生死判定関数（残す）
function checkUrl(url) {
  const headers = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
  };

  try {
    const response = UrlFetchApp.fetch(url, {
      method: 'get',
      followRedirects: true,
      muteHttpExceptions: true,
      headers: headers,
    });

    const code = response.getResponseCode();
    if (code >= 200 && code < 400) {
      return `Alive: HTTP ${code}`;
    }
    if (code === 401 || code === 403 || code === 407) {
      return `Blocked: HTTP ${code}`;
    }
    return `Dead: HTTP ${code}`;
  } catch (e) {
    return `Dead: ${e}`;
  }
}

// フォームリンク抽出（静的HTML内のリンクを探す）
function extractContactLinkRaw(html) {
  const lower = html.toLowerCase();
  const patterns = [
    /href=["']([^"']*?(?:お問い合わせ|contact|問い合わせ|inquiry|form|toiawase)[^"']*?)["']/gi,
    /href=["']([^"']*?\/(?:contact|form|inquiry|toiawase)[^"']*?)["']/gi
  ];
  for (let pattern of patterns) {
    let match;
    while ((match = pattern.exec(lower)) !== null) {
      let link = match[1];
      if (link.startsWith('//')) link = 'https:' + link;
      if (!link.startsWith('http')) {
        try {
          link = new URL(link, 'https://dummy.com').href;
        } catch (e) {}
      }
      if (link) return link;
    }
  }
  return null;
}

/*
 * 動的(JS描画)ページかどうかのヒューリスティック判定
 * ※ UrlFetchAppはJSを実行しないため、これは推定であり確定判定ではない。
 *   「取得したHTMLがSPAの空シェルらしい」兆候を複数チェックする。
 */
function looksDynamic(html) {
  const lower = html.toLowerCase();

  // 1. 代表的なSPAルート要素
  const spaRootPatterns = [
    /id=["']app["']/,
    /id=["']root["']/,
    /id=["']__next["']/,
    /id=["']__nuxt["']/,
    /data-reactroot/,
    /ng-version=/
  ];
  if (spaRootPatterns.some((re) => re.test(lower))) return true;

  // 2. フレームワーク特有の痕跡
  const frameworkPatterns = [
    /__nuxt__/,
    /webpackjsonp/,
    /window\.__initial_state__/,
    /_next\/static/
  ];
  if (frameworkPatterns.some((re) => re.test(lower))) return true;

  // 3. 「JavaScriptを有効にしてください」系のnoscript警告
  const noscriptMatch = lower.match(/<noscript>([\s\S]*?)<\/noscript>/);
  if (noscriptMatch) {
    const noscriptText = noscriptMatch[1];
    if (/javascript|有効|enable/.test(noscriptText)) return true;
  }

  // 4. body内の実テキスト量が極端に少ない（空のシェルHTML）
  const bodyMatch = lower.match(/<body[^>]*>([\s\S]*?)<\/body>/);
  if (bodyMatch) {
    const bodyText = bodyMatch[1]
      .replace(/<script[\s\S]*?<\/script>/g, '')
      .replace(/<style[\s\S]*?<\/style>/g, '')
      .replace(/<[^>]+>/g, '')
      .trim();
    if (bodyText.length < 50) return true;
  }

  return false;
}

// 生死 + フォーム判定（動的/なし判定込み）
function checkUrlWithForm(url) {
  const headers = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36'
  };
  try {
    const response = UrlFetchApp.fetch(url, {
      method: 'get',
      followRedirects: true,
      muteHttpExceptions: true,
      headers: headers
    });
    const code = response.getResponseCode();
    let status = (code >= 200 && code < 400) ? `Alive: HTTP ${code}` : `Dead: HTTP ${code}`;
    let formLink = 'なし';

    if (code >= 200 && code < 400) {
      const html = response.getContentText();
      const rawLink = extractContactLinkRaw(html);
      if (rawLink) {
        formLink = rawLink;
      } else if (looksDynamic(html)) {
        formLink = '動的';
      } else {
        formLink = 'なし';
      }
    }
    return { status, formLink };
  } catch (e) {
    return { status: `Dead: ${e}`, formLink: 'なし' };
  }
}

/* ------------------------------------------------------------------ *
 * 自己継続トリガー関連
 * ------------------------------------------------------------------ */

function deleteContinuationTriggers() {
  const triggers = ScriptApp.getProjectTriggers();
  let count = 0;
  triggers.forEach((trigger) => {
    if (trigger.getHandlerFunction() === CONTINUATION_HANDLER) {
      ScriptApp.deleteTrigger(trigger);
      count++;
    }
  });
  if (count) {
    Logger.log(`継続トリガーを${count}件削除しました。`);
  }
  return count;
}

function scheduleContinuation(delaySec) {
  deleteContinuationTriggers();
  ScriptApp.newTrigger(CONTINUATION_HANDLER)
    .timeBased()
    .after(delaySec * 1000)
    .create();
  Logger.log(`継続トリガーを${delaySec}秒後に設定しました。（関数: ${CONTINUATION_HANDLER}）`);
}

function continueUrlViabilityCheck() {
  deleteContinuationTriggers();
  try {
    runUrlViabilityCheck();
  } catch (e) {
    Logger.log(`continueUrlViabilityCheck 実行中にエラー: ${e}`);
    const config = getConfig();
    scheduleContinuation(config.triggerDelaySec);
  }
}

/* ------------------------------------------------------------------ *
 * メイン処理
 * ------------------------------------------------------------------ */

function runUrlViabilityCheck() {
  const config = getConfig();
  const startedAt = Date.now();
  const timeLimit = config.maxRuntimeMs - config.timeBufferMs;

  const spreadsheet = SpreadsheetApp.openById(config.spreadsheetId);
  const sheet = spreadsheet.getSheetByName(config.sheetName);
  if (!sheet) {
    throw new Error(`シートが見つかりません: ${config.sheetName}`);
  }

  const targets = getTargetRows(sheet);
  if (!targets.length) {
    Logger.log('対象行なし。全件処理完了とみなします。');
    deleteContinuationTriggers();
    return { processed: 0, remaining: 0, finished: true };
  }

  const batch = targets.slice(0, config.batchSize);
  let processedCount = 0;
  let timeUp = false;

  for (let i = 0; i < batch.length; i++) {
    const elapsed = Date.now() - startedAt;
    if (elapsed > timeLimit) {
      Logger.log(`残り時間僅少のため中断します（${processedCount}/${batch.length}件処理済み）`);
      timeUp = true;
      break;
    }

    const item = batch[i];
    const result = checkUrlWithForm(item.url);

    sheet.getRange(item.rowIndex, 20).setValue(result.status);   // T列生死
    sheet.getRange(item.rowIndex, 21).setValue(result.formLink); // U列フォーム
    SpreadsheetApp.flush();
    Logger.log(`[${i + 1}/${batch.length}] 行${item.rowIndex}: ${result.status} | フォーム: ${result.formLink}`);

    processedCount++;
    Utilities.sleep(500);
  }

  const remaining = targets.length - processedCount;

  if (timeUp || remaining > 0) {
    Logger.log(`未処理が${remaining}件残っています。${config.triggerDelaySec}秒後に再開トリガーを設定します。`);
    scheduleContinuation(config.triggerDelaySec);
  } else {
    Logger.log('全対象行を処理完了しました。');
    deleteContinuationTriggers();
  }

  Logger.log(`本回の処理結果: 完了${processedCount}件 / 残り${remaining}件`);
  return { processed: processedCount, remaining, finished: !timeUp && remaining === 0 };
}

function doGet(e) {
  try {
    const result = runUrlViabilityCheck();
    return ContentService.createTextOutput(JSON.stringify({ status: 'success', ...result }));
  } catch (error) {
    Logger.log(error);
    return ContentService.createTextOutput(JSON.stringify({ status: 'error', message: error.message }));
  }
}

function createHourlyTrigger() {
  ScriptApp.newTrigger('runUrlViabilityCheck').timeBased().everyHours(1).create();
}