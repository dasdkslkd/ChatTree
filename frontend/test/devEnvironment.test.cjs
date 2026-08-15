const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const read = (relative) => fs.readFileSync(path.join(__dirname, '..', relative), 'utf8');

const section = read('src/components/settings/sections/dev_environment.tsx');
const dialog = read('src/components/SettingsDialog.tsx');
const projects = read('src/components/settings/sections/projects.tsx');
const types = read('src/types/model.ts');
const api = read('src/api/config.ts');

function testEditorCoversPresetsAndEnvironments() {
  assert.match(
    section,
    /const DEV_TOOL_PRESETS = \['python', 'node', 'npm', 'git', 'java', 'go', 'cargo', 'uv'\];/,
    'editor should preset common development tools',
  );
  assert.match(section, /并列虚拟环境/, 'editor should expose parallel environment rows');
  assert.match(section, /default_environment/, 'editor should manage the default environment');
  assert.match(section, /configApi\.getDevEnvironmentDetected\(\)/, 'editor should load system PATH detection');
  assert.match(
    section,
    /placeholder=\{detected\[row\.name\] \|\| '系统 PATH 未检测到'\}/,
    'detected default should render as grey placeholder inside the input',
  );
  assert.match(section, /function pickFilePath\(\): Promise<string \| null>/, 'file picking should be one atomic helper');
  assert.match(section, /getPathForFile\(input\.files\[0\]\)/, 'picked file should resolve to a native absolute path');
  assert.doesNotMatch(section, /pendingPick|fileInputRef/, 'no out-of-band pick state should remain');
  assert.doesNotMatch(section, /DetectedHint/, 'hint text under the input should be removed');
}

function testSectionSavesGlobalDevEnvironment() {
  assert.match(
    section,
    /await configApi\.update\(\{ dev_environment: form \}\);/,
    'section should save the global dev_environment via PUT /config',
  );
  assert.match(section, /setForm\(data\.dev_environment \|\| \{\}\);/, 'section should load existing config');
}

function testSettingsDialogRegistersSection() {
  assert.match(dialog, /import \{ DevEnvironmentSection \} from '\.\/settings\/sections\/dev_environment';/);
  assert.match(dialog, /\{ key: 'dev_environment', label: '开发环境', icon: Terminal, group: '工具与能力' \}/);
  assert.match(dialog, /\{section === 'dev_environment' && <DevEnvironmentSection \/>\}/);
}

function testProjectSectionOverridesDevEnvironment() {
  assert.match(
    projects,
    /dev_environment: selectedProject\.config\?\.dev_environment \|\| \{\},/,
    'project draft should initialize dev_environment',
  );
  assert.match(
    projects,
    /<DevEnvironmentEditor[\s\S]*value=\{draft\.dev_environment \|\| \{\}\}[\s\S]*dev_environment: next/,
    'project detail should render the shared dev environment editor',
  );
}

function testTypesAndApiCarryDevEnvironment() {
  assert.match(types, /export interface DevEnvironmentConfig \{/);
  assert.match(types, /dev_environment\?: DevEnvironmentConfig;/);
  assert.match(api, /getDevEnvironmentDetected[\s\S]*'\/config\/dev-environment\/detected'/);
}

testEditorCoversPresetsAndEnvironments();
testSectionSavesGlobalDevEnvironment();
testSettingsDialogRegistersSection();
testProjectSectionOverridesDevEnvironment();
testTypesAndApiCarryDevEnvironment();

console.log('PASS devEnvironment');
