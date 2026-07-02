const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const tooltipSource = fs.readFileSync(path.join(__dirname, '../src/components/ui/tooltip.tsx'), 'utf8');

function testTooltipUsesProjectSurfaceStyle() {
  assert.doesNotMatch(tooltipSource, /bg-foreground text-background/);
  assert.doesNotMatch(tooltipSource, /rounded-md px-3 py-1\.5/);
  assert.match(tooltipSource, /bg-\[var\(--bg-elevated\)\]/);
  assert.match(tooltipSource, /text-\[var\(--fg-secondary\)\]/);
  assert.match(tooltipSource, /border-\[var\(--border\)\]/);
  assert.match(tooltipSource, /rounded-lg/);
  assert.match(tooltipSource, /shadow-\[var\(--shadow-md\)\]/);
}

function testTooltipArrowUsesProjectSurfaceStyle() {
  assert.match(tooltipSource, /bg-\[var\(--bg-elevated\)\]/);
  assert.match(tooltipSource, /fill-\[var\(--bg-elevated\)\]/);
}

function testTooltipWaitsBeforeOpening() {
  assert.match(tooltipSource, /delayDuration = 2000/);
  assert.match(tooltipSource, /skipDelayDuration = 0/);
  assert.match(tooltipSource, /delayDuration=\{delayDuration\}/);
  assert.match(tooltipSource, /skipDelayDuration=\{skipDelayDuration\}/);
}

function main() {
  testTooltipUsesProjectSurfaceStyle();
  testTooltipArrowUsesProjectSurfaceStyle();
  testTooltipWaitsBeforeOpening();
  console.log('tooltipStyles tests passed');
}

main();
