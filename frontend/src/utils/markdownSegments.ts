export type MarkdownSegment = {
  text: string;
  kind: 'text' | 'code';
};

function fenceMarker(line: string): string | null {
  const match = line.match(/^\s*(```+|~~~+)/);
  return match ? match[1] : null;
}

function matchingFence(line: string, marker: string): boolean {
  const trimmed = line.trimStart();
  const fenceChar = marker[0];
  return trimmed.startsWith(fenceChar.repeat(marker.length));
}

function splitInlineCode(line: string): MarkdownSegment[] {
  const segments: MarkdownSegment[] = [];
  let index = 0;

  while (index < line.length) {
    const tickStart = line.indexOf('`', index);
    if (tickStart === -1) {
      segments.push({ kind: 'text', text: line.slice(index) });
      break;
    }

    if (tickStart > index) {
      segments.push({ kind: 'text', text: line.slice(index, tickStart) });
    }

    let tickEnd = tickStart;
    while (tickEnd < line.length && line[tickEnd] === '`') {
      tickEnd += 1;
    }
    const marker = line.slice(tickStart, tickEnd);
    const close = line.indexOf(marker, tickEnd);

    if (close === -1) {
      segments.push({ kind: 'text', text: line.slice(tickStart) });
      break;
    }

    segments.push({ kind: 'code', text: line.slice(tickStart, close + marker.length) });
    index = close + marker.length;
  }

  return segments;
}

export function splitMarkdownCodeSegments(markdown: string): MarkdownSegment[] {
  const lines = markdown.split(/(\r?\n)/);
  const segments: MarkdownSegment[] = [];
  let activeFence: string | null = null;

  for (let i = 0; i < lines.length; i += 2) {
    const line = lines[i] ?? '';
    const newline = lines[i + 1] ?? '';
    const lineWithNewline = line + newline;

    if (activeFence) {
      segments.push({ kind: 'code', text: lineWithNewline });
      if (matchingFence(line, activeFence)) {
        activeFence = null;
      }
      continue;
    }

    const marker = fenceMarker(line);
    if (marker) {
      segments.push({ kind: 'code', text: lineWithNewline });
      activeFence = marker;
      continue;
    }

    const inlineSegments = splitInlineCode(line);
    for (const segment of inlineSegments) {
      segments.push(segment);
    }
    if (newline) {
      segments.push({ kind: 'text', text: newline });
    }
  }

  return segments;
}
