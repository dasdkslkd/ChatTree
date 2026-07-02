import { splitMarkdownCodeSegments } from './markdownSegments';

export type MarkdownFeatures = {
  hasMath: boolean;
  hasMermaid: boolean;
  hasRawHtml: boolean;
};

const mermaidFencePattern = /(^|\n)(```+|~~~+)\s*mermaid(?:\s|\n|$)/i;
const rawHtmlTagPattern = /<\/?[A-Za-z][A-Za-z0-9-]*(?:\s+[^<>]*?)?>/;

function markerIsEscaped(text: string, index: number): boolean {
  let slashCount = 0;
  for (let i = index - 1; i >= 0 && text[i] === '\\'; i -= 1) {
    slashCount += 1;
  }
  return slashCount % 2 === 1;
}

function hasDelimitedPair(text: string, open: string, close: string): boolean {
  let index = 0;
  while (index < text.length) {
    const start = text.indexOf(open, index);
    if (start === -1) return false;
    if (markerIsEscaped(text, start)) {
      index = start + open.length;
      continue;
    }

    const end = text.indexOf(close, start + open.length);
    if (end !== -1 && !markerIsEscaped(text, end)) {
      return true;
    }
    index = start + open.length;
  }
  return false;
}

function hasDollarMath(text: string): boolean {
  if (hasDelimitedPair(text, '$$', '$$')) return true;

  let index = 0;
  while (index < text.length) {
    const start = text.indexOf('$', index);
    if (start === -1) return false;
    if (text[start + 1] === '$' || markerIsEscaped(text, start)) {
      index = start + 1;
      continue;
    }

    let end = start + 1;
    while (end < text.length) {
      end = text.indexOf('$', end);
      if (end === -1) return false;
      if (text[end - 1] !== '\\' && text[end + 1] !== '$') {
        return true;
      }
      end += 1;
    }
  }
  return false;
}

function hasBracketMath(text: string): boolean {
  return hasDelimitedPair(text, '\\(', '\\)') || hasDelimitedPair(text, '\\[', '\\]');
}

export function detectMarkdownFeatures(markdown: string): MarkdownFeatures {
  let hasMath = false;
  let hasRawHtml = false;
  let textBuffer = '';

  function inspectBufferedText() {
    if (!textBuffer) return;
    hasMath ||= hasDollarMath(textBuffer) || hasBracketMath(textBuffer);
    hasRawHtml ||= rawHtmlTagPattern.test(textBuffer);
    textBuffer = '';
  }

  for (const segment of splitMarkdownCodeSegments(markdown)) {
    if (segment.kind === 'code') {
      inspectBufferedText();
      continue;
    }
    textBuffer += segment.text;
    if (hasMath && hasRawHtml) break;
  }
  inspectBufferedText();

  return {
    hasMath,
    hasMermaid: mermaidFencePattern.test(markdown),
    hasRawHtml,
  };
}
