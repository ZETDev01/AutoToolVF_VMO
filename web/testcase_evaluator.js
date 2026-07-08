(function (global) {
  'use strict';

  const VI_LOCALE = 'vi-VN';
  const COMBINING_MARKS = /[\u0300-\u036f]/g;
  const STOP_WORDS = new Set([
    'a',
    'cac',
    'cho',
    'co',
    'cua',
    'da',
    'day',
    'duoc',
    'la',
    'mot',
    'nay',
    'nhe',
    'noi',
    'o',
    'tai',
    'thi',
    'thuoc',
    'vao',
    'va',
    'vui',
    'vua'
  ]);
  const DEFAULT_PHRASE_ALIASES = [
    ['que huong', 'que'],
    ['sinh nhat', 'ngay sinh'],
    ['ngay thang nam sinh', 'ngay sinh']
  ];

  function stripAccents(value) {
    return String(value || '').normalize('NFD').replace(COMBINING_MARKS, '');
  }

  function normalizeText(value) {
    return stripAccents(value)
      .toLocaleLowerCase(VI_LOCALE)
      .replace(/[“”"']/g, '')
      .replace(/[()[\]{}]/g, ' ')
      .replace(/\s+/g, ' ')
      .trim();
  }

  function normalizeLooseText(value) {
    return normalizeText(value)
      .replace(/[.,:!?]+/g, ' ')
      .replace(/\s+/g, ' ')
      .trim();
  }

  function configuredPhraseAliases() {
    const custom = Array.isArray(global.TestcaseEvaluatorAliases)
      ? global.TestcaseEvaluatorAliases
      : [];
    return DEFAULT_PHRASE_ALIASES.concat(custom);
  }

  function applyPhraseAliases(value) {
    let text = ` ${normalizeLooseText(value)} `;
    configuredPhraseAliases().forEach(([from, to]) => {
      text = text.replace(new RegExp(`\\b${from}\\b`, 'g'), to);
    });
    return text.replace(/\s+/g, ' ').trim();
  }

  function normalizeThousandSeparators(value) {
    let text = normalizeText(value);
    let previous = '';
    while (previous !== text) {
      previous = text;
      text = text.replace(/(\d)[.,\s](?=\d{3}(?:\D|$))/g, '$1');
    }
    return text.replace(/\s+/g, ' ').trim();
  }

  function normalizeDigitSpacing(value) {
    return normalizeThousandSeparators(value)
      .replace(/(\d)[\s.,:/-]+(?=\d)/g, '$1')
      .replace(/\s+/g, ' ')
      .trim();
  }

  function hasDigitSpacingMatch(expected, actual) {
    const expectedText = normalizeDigitSpacing(expected);
    if (!/\d/.test(expectedText)) return false;
    return normalizeDigitSpacing(actual).includes(expectedText);
  }

  function shouldKeepCommaAsNumberSeparator(text, index) {
    const before = text[index - 1] || '';
    if (!/\d/.test(before)) return false;
    const after = text.slice(index + 1);
    return /^\d{3}(?:\D|$)/.test(after);
  }

  function splitList(value) {
    const text = String(value || '').trim();
    if (!text) return [];

    const parts = [];
    let start = 0;
    for (let index = 0; index < text.length; index += 1) {
      const char = text[index];
      const shouldSplit =
        char === ';' ||
        char === '\n' ||
        char === '\r' ||
        (char === ',' && !shouldKeepCommaAsNumberSeparator(text, index));
      if (!shouldSplit) continue;
      const part = text.slice(start, index).trim();
      if (part) parts.push(part);
      start = index + 1;
    }

    const tail = text.slice(start).trim();
    if (tail) parts.push(tail);
    return parts;
  }

  function canonicalYear(value) {
    const year = Number(value);
    if (!Number.isFinite(year)) return '';
    if (year < 100) return String(year + (year >= 70 ? 1900 : 2000));
    return String(year);
  }

  function canonicalDate(day, month, year) {
    const dayNumber = Number(day);
    const monthNumber = Number(month);
    const yearText = canonicalYear(year);
    if (
      !yearText ||
      dayNumber < 1 ||
      dayNumber > 31 ||
      monthNumber < 1 ||
      monthNumber > 12
    ) {
      return '';
    }
    return `${yearText.padStart(4, '0')}-${String(monthNumber).padStart(2, '0')}-${String(dayNumber).padStart(2, '0')}`;
  }

  function canonicalMonthDay(day, month) {
    const dayNumber = Number(day);
    const monthNumber = Number(month);
    if (dayNumber < 1 || dayNumber > 31 || monthNumber < 1 || monthNumber > 12) {
      return '';
    }
    return `${String(monthNumber).padStart(2, '0')}-${String(dayNumber).padStart(2, '0')}`;
  }

  function extractDates(value) {
    const text = normalizeText(value);
    const dates = {full: new Set(), monthDay: new Set()};
    const numeric = /(^|\D)(\d{1,2})\s*[./-]\s*(\d{1,2})\s*[./-]\s*(\d{2,4})(?=\D|$)/g;
    const numericMonthDay = /(^|\D)(\d{1,2})\s*[./-]\s*(\d{1,2})(?=\D|$)/g;
    const vietnamese = /(?:ngay\s*)?(\d{1,2})\s*(?:thang)\s*(\d{1,2})\s*(?:nam)\s*(\d{2,4})/g;
    const vietnameseMonthDay = /(?:ngay\s*)?(\d{1,2})\s*(?:thang)\s*(\d{1,2})(?=\D|$)/g;

    let match = null;
    while ((match = numeric.exec(text))) {
      const date = canonicalDate(match[2], match[3], match[4]);
      const monthDay = canonicalMonthDay(match[2], match[3]);
      if (date) dates.full.add(date);
      if (monthDay) dates.monthDay.add(monthDay);
    }
    while ((match = vietnamese.exec(text))) {
      const date = canonicalDate(match[1], match[2], match[3]);
      const monthDay = canonicalMonthDay(match[1], match[2]);
      if (date) dates.full.add(date);
      if (monthDay) dates.monthDay.add(monthDay);
    }
    while ((match = numericMonthDay.exec(text))) {
      const monthDay = canonicalMonthDay(match[2], match[3]);
      if (monthDay) dates.monthDay.add(monthDay);
    }
    while ((match = vietnameseMonthDay.exec(text))) {
      const monthDay = canonicalMonthDay(match[1], match[2]);
      if (monthDay) dates.monthDay.add(monthDay);
    }

    return dates;
  }

  function hasSharedDate(expected, actual) {
    const expectedDates = extractDates(expected);
    if (!expectedDates.full.size && !expectedDates.monthDay.size) return false;
    const actualDates = extractDates(actual);
    for (const date of expectedDates.full) {
      if (actualDates.full.has(date)) return true;
    }
    for (const monthDay of expectedDates.monthDay) {
      if (actualDates.monthDay.has(monthDay)) return true;
    }
    return false;
  }

  function canonicalTime(hour, minute, marker) {
    let hourNumber = Number(hour);
    const minuteNumber = Number(minute || 0);
    const markerText = normalizeText(marker || '').replace(/\./g, '');
    if (
      !Number.isFinite(hourNumber) ||
      !Number.isFinite(minuteNumber) ||
      minuteNumber < 0 ||
      minuteNumber > 59
    ) {
      return '';
    }

    if (markerText === 'am' || markerText === 'pm') {
      if (hourNumber < 1 || hourNumber > 12) return '';
      if (markerText === 'am') {
        hourNumber = hourNumber === 12 ? 0 : hourNumber;
      } else {
        hourNumber = hourNumber === 12 ? 12 : hourNumber + 12;
      }
    } else if (markerText) {
      if (hourNumber < 0 || hourNumber > 23) return '';
      if ((markerText === 'chieu' || markerText === 'toi') && hourNumber < 12) {
        hourNumber += 12;
      } else if (markerText === 'dem') {
        if (hourNumber === 12) {
          hourNumber = 0;
        } else if (hourNumber >= 7 && hourNumber < 12) {
          hourNumber += 12;
        }
      } else if (markerText === 'sang' && hourNumber === 12) {
        hourNumber = 0;
      } else if (markerText === 'trua' && hourNumber < 11) {
        hourNumber += 12;
      }
    } else if (hourNumber < 0 || hourNumber > 23) {
      return '';
    }

    return `${String(hourNumber).padStart(2, '0')}:${String(minuteNumber).padStart(2, '0')}`;
  }

  function extractTimes(value) {
    const text = normalizeText(value);
    const times = new Set();
    const ampm = /(^|\D)(\d{1,2})(?:\s*:\s*(\d{1,2}))?\s*(a\.?m\.?|p\.?m\.?|am|pm)(?=\D|$)/g;
    const colon = /(^|\D)([01]?\d|2[0-3])\s*:\s*([0-5]\d)(?=\D|$)/g;
    const vietnamese = /(^|\D)(\d{1,2})\s*(?:h|gio)\s*(?:(\d{1,2})(?:\s*phut)?)?\s*(sang|chieu|toi|dem|trua)?(?=\D|$)/g;

    let match = null;
    while ((match = ampm.exec(text))) {
      const time = canonicalTime(match[2], match[3], match[4]);
      if (time) times.add(time);
    }
    while ((match = colon.exec(text))) {
      const time = canonicalTime(match[2], match[3], '');
      if (time) times.add(time);
    }
    while ((match = vietnamese.exec(text))) {
      const time = canonicalTime(match[2], match[3], match[4]);
      if (time) times.add(time);
    }

    return times;
  }

  function hasSharedTime(expected, actual) {
    const expectedTimes = extractTimes(expected);
    if (!expectedTimes.size) return false;
    const actualTimes = extractTimes(actual);
    for (const time of expectedTimes) {
      if (actualTimes.has(time)) return true;
    }
    return false;
  }

  function tokenizeMeaningful(value) {
    const text = applyPhraseAliases(value).replace(/[^\p{L}\p{N}_]+/gu, ' ');
    return text
      .split(/\s+/)
      .map((token) => token.trim())
      .filter((token) => token && !STOP_WORDS.has(token));
  }

  function countTokenOverlap(expectedTokens, actualTokens) {
    const actualSet = new Set(actualTokens);
    return expectedTokens.filter((token) => actualSet.has(token)).length;
  }

  function hasTokenSubset(expected, actual) {
    const expectedTokens = Array.from(new Set(tokenizeMeaningful(expected)));
    if (!expectedTokens.length || expectedTokens.length > 5) return false;
    const actualTokens = tokenizeMeaningful(actual);
    return countTokenOverlap(expectedTokens, actualTokens) === expectedTokens.length;
  }

  function hasSemanticSimilarity(expected, actual) {
    const expectedTokens = Array.from(new Set(tokenizeMeaningful(expected)));
    if (expectedTokens.length < 6) return false;

    const actualTokens = tokenizeMeaningful(actual);
    const overlap = countTokenOverlap(expectedTokens, actualTokens);
    const coverage = overlap / expectedTokens.length;

    return coverage >= 0.82;
  }

  function containsFlexible(actual, expected) {
    const expectedText = String(expected || '').trim();
    const actualText = String(actual || '').trim();
    if (!expectedText) return {matched: true, mode: 'empty'};
    if (!actualText) return {matched: false, mode: 'empty-actual'};

    const normalizedActual = normalizeText(actualText);
    const normalizedExpected = normalizeText(expectedText);
    if (normalizedActual.includes(normalizedExpected)) {
      return {matched: true, mode: 'text'};
    }

    const looseActual = normalizeLooseText(actualText);
    const looseExpected = normalizeLooseText(expectedText);
    if (looseActual.includes(looseExpected)) {
      return {matched: true, mode: 'loose-text'};
    }

    const numericActual = normalizeThousandSeparators(actualText);
    const numericExpected = normalizeThousandSeparators(expectedText);
    if (numericActual.includes(numericExpected)) {
      return {matched: true, mode: 'number-separator'};
    }

    if (hasDigitSpacingMatch(expectedText, actualText)) {
      return {matched: true, mode: 'digit-spacing'};
    }

    if (hasSharedDate(expectedText, actualText)) {
      return {matched: true, mode: 'date'};
    }

    if (hasSharedTime(expectedText, actualText)) {
      return {matched: true, mode: 'time'};
    }

    const aliasActual = applyPhraseAliases(actualText);
    const aliasExpected = applyPhraseAliases(expectedText);
    if (aliasActual.includes(aliasExpected)) {
      return {matched: true, mode: 'alias-text'};
    }

    if (hasTokenSubset(expectedText, actualText)) {
      return {matched: true, mode: 'token-subset'};
    }

    if (hasSemanticSimilarity(expectedText, actualText)) {
      return {matched: true, mode: 'semantic-coverage'};
    }

    return {matched: false, mode: 'missing'};
  }

  function containsForbidden(actual, forbidden) {
    const expectedText = String(forbidden || '').trim();
    const actualText = String(actual || '').trim();
    if (!expectedText) return {matched: false, mode: 'empty'};
    if (!actualText) return {matched: false, mode: 'empty-actual'};

    const normalizedActual = normalizeText(actualText);
    const normalizedExpected = normalizeText(expectedText);
    if (normalizedActual.includes(normalizedExpected)) {
      return {matched: true, mode: 'text'};
    }

    const looseActual = normalizeLooseText(actualText);
    const looseExpected = normalizeLooseText(expectedText);
    if (looseActual.includes(looseExpected)) {
      return {matched: true, mode: 'loose-text'};
    }

    const numericExpected = normalizeThousandSeparators(expectedText);
    if (/\d/.test(numericExpected)) {
      const numericActual = normalizeThousandSeparators(actualText);
      if (numericActual.includes(numericExpected)) {
        return {matched: true, mode: 'number-separator'};
      }
      if (hasDigitSpacingMatch(expectedText, actualText)) {
        return {matched: true, mode: 'digit-spacing'};
      }
    }

    if (hasSharedDate(expectedText, actualText)) {
      return {matched: true, mode: 'date'};
    }

    if (hasSharedTime(expectedText, actualText)) {
      return {matched: true, mode: 'time'};
    }

    const aliasActual = applyPhraseAliases(actualText);
    const aliasExpected = applyPhraseAliases(expectedText);
    if (aliasActual.includes(aliasExpected)) {
      return {matched: true, mode: 'alias-text'};
    }

    return {matched: false, mode: 'missing'};
  }

  function expectedChecks(item) {
    const checks = [];
    const keywordChecks = splitList(item && item.expected_keywords).map((value) => ({
      source: 'expected_keywords',
      value
    }));
    if (keywordChecks.length) {
      return keywordChecks;
    }

    const expectedResponse = String((item && item.expected_response) || '').trim();
    if (expectedResponse) {
      checks.push({source: 'expected_response', value: expectedResponse});
    }
    return checks;
  }

  function forbiddenChecks(item) {
    return splitList(item && item.forbidden_keywords).map((value) => ({
      source: 'forbidden_keywords',
      value
    }));
  }

  function evaluateTestcase(item, output) {
    const missing = [];
    const matched = [];
    const blocked = [];

    expectedChecks(item).forEach((check) => {
      const match = containsFlexible(output, check.value);
      if (match.matched) {
        matched.push({...check, mode: match.mode});
      } else {
        missing.push(check);
      }
    });

    forbiddenChecks(item).forEach((check) => {
      const match = containsForbidden(output, check.value);
      if (match.matched) {
        blocked.push({...check, mode: match.mode});
      }
    });

    if (missing.length || blocked.length) {
      const messages = [];
      if (missing.length) {
        const label = missing.every((item) => item.source === 'expected_keywords')
          ? 'Missing keywords'
          : 'Missing expected';
        messages.push(`${label}: ${missing.map((item) => item.value).join(', ')}`);
      }
      if (blocked.length) {
        messages.push(`Forbidden found: ${blocked.map((item) => `${item.value} (${item.mode})`).join(', ')}`);
      }
      return {
        status: 'FAIL',
        result: `FAIL: ${messages.join(' | ')}`,
        details: {missing, blocked, matched}
      };
    }

    return {
      status: 'PASS',
      result: 'PASS',
      details: {missing, blocked, matched}
    };
  }

  const api = {
    containsFlexible,
    evaluateTestcase,
    extractDates,
    extractTimes,
    applyPhraseAliases,
    normalizeDigitSpacing,
    normalizeLooseText,
    normalizeText,
    normalizeThousandSeparators,
    tokenizeMeaningful,
    splitList
  };

  global.TestcaseEvaluator = api;
  if (typeof module !== 'undefined' && module.exports) {
    module.exports = api;
  }
})(typeof window !== 'undefined' ? window : globalThis);
