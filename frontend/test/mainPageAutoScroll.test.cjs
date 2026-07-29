const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const source = fs.readFileSync(path.join(__dirname, '../src/pages/MainPage.tsx'), 'utf8');

function testScrollPositionOwnsFollowState() {
  assert.match(
    source,
    /const autoScrollRef = useRef\(true\);/,
    'MainPage should keep one mutable bottom-follow state',
  );
  assert.match(
    source,
    /const handleScroll = useCallback\(\(\) => \{[\s\S]*?autoScrollRef\.current = isAtBottom\(\);/,
    'Every scroll should leave or restore bottom-follow mode from the actual position',
  );
  assert.doesNotMatch(
    source,
    /shouldAutoScroll|userScrollingRef|programmaticScrollRef|scrollEndTimeoutRef/,
    'Competing scroll mode flags should not return',
  );
}

function testStreamingAndSendingFollowBottom() {
  assert.match(
    source,
    /useLayoutEffect\(\(\) => \{\s*if \(currentBranchHasStreamingChat && autoScrollRef\.current\) \{\s*scrollToBottom\(\);/,
    'Streaming renders should follow the bottom before paint while follow mode is active',
  );
  assert.match(
    source,
    /\}, \[currentBranchHasStreamingChat, scrollToBottom, transcriptItems\]\);/,
    'Every rendered transcript patch should drive bottom following during a stream',
  );
  assert.match(
    source,
    /const handleSend = async \([\s\S]*?if \(!val\.trim\(\)\) return;\s*autoScrollRef\.current = true;\s*requestAnimationFrame\(scrollToBottom\);/,
    'Sending a message should restore bottom-follow mode and request a scroll',
  );
  assert.match(
    source,
    /pendingScrollId\.current = null;\s*autoScrollRef\.current = isAtBottom\(\);/,
    'Restoring a conversation should derive follow mode from its restored position',
  );
}

testScrollPositionOwnsFollowState();
testStreamingAndSendingFollowBottom();
