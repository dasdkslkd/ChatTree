import { splitMarkdownCodeSegments } from './markdownSegments';

function markerIsEscaped(text: string, index: number): boolean {
  let slashCount = 0;
  for (let i = index - 1; i >= 0 && text[i] === '\\'; i -= 1) {
    slashCount += 1;
  }
  return slashCount % 2 === 1;
}

function findUnescaped(text: string, marker: string, from: number): number {
  let index = from;
  while (index < text.length) {
    const found = text.indexOf(marker, index);
    if (found === -1) return -1;
    if (!markerIsEscaped(text, found)) return found;
    index = found + marker.length;
  }
  return -1;
}

function replaceDelimited(text: string, open: string, close: string, nextOpen: string, nextClose: string): string {
  let result = '';
  let index = 0;

  while (index < text.length) {
    const start = findUnescaped(text, open, index);
    if (start === -1) {
      result += text.slice(index);
      break;
    }

    const end = findUnescaped(text, close, start + open.length);
    if (end === -1) {
      result += text.slice(index);
      break;
    }

    result += text.slice(index, start);
    result += nextOpen;
    result += text.slice(start + open.length, end);
    result += nextClose;
    index = end + close.length;
  }

  return result;
}

function normalizeTextSegment(text: string): string {
  const withInline = replaceDelimited(text, '\\(', '\\)', '$', '$');
  return replaceDelimited(withInline, '\\[', '\\]', '$$', '$$');
}

export function normalizeMathDelimiters(markdown: string): string {
  let result = '';
  let textBuffer = '';

  function flushText() {
    if (!textBuffer) return;
    result += normalizeTextSegment(textBuffer);
    textBuffer = '';
  }

  for (const segment of splitMarkdownCodeSegments(markdown)) {
    if (segment.kind === 'code') {
      flushText();
      result += segment.text;
      continue;
    }
    textBuffer += segment.text;
  }

  flushText();
  return result;
}
