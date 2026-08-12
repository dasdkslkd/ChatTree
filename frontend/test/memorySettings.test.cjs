const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const read = (relative) => fs.readFileSync(path.join(__dirname, '..', relative), 'utf8');

const section = read('src/components/settings/sections/memory.tsx');
const dialog = read('src/components/SettingsDialog.tsx');
const navigation = read('src/store/navigationStore.ts');
const api = read('src/api/memory.ts');
const types = read('src/types/model.ts');

assert.match(dialog, /\{ key: 'memory', label: '记忆', icon: Brain, group: '应用' \}/);
assert.match(dialog, /\{section === 'memory' && <MemorySection \/>\}/);
assert.match(navigation, /'memory'/);
assert.match(section, /configApi\.update\(\{ memory: \{ enabled \} \}\)/);
assert.match(section, /\(\['global', 'project'\] as const\)/);
assert.match(section, /requestVersion\.current !== version/);
assert.match(section, /selectedProject\.current === projectId/);
assert.match(section, /file\.content/);
assert.doesNotMatch(section, /contentEditable|textarea|删除记忆/);
assert.match(api, /params: projectId \? \{ project_id: projectId \} : undefined/);
assert.match(types, /export interface MemoryViewResponse/);

console.log('PASS memorySettings');
