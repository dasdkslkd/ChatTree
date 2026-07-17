const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const ts = require('typescript');

const source = fs.readFileSync(path.join(__dirname, '../src/pages/MainPage.tsx'), 'utf8');
const sourceFile = ts.createSourceFile(
  'MainPage.tsx',
  source,
  ts.ScriptTarget.Latest,
  true,
  ts.ScriptKind.TSX,
);

function collectNodes(predicate) {
  const matches = [];
  function visit(node) {
    if (predicate(node)) matches.push(node);
    ts.forEachChild(node, visit);
  }
  visit(sourceFile);
  return matches;
}

function getRequiredExpressionAttribute(element, name) {
  const attributes = element.attributes.properties.filter((property) => (
    ts.isJsxAttribute(property) && property.name.text === name
  ));
  assert.equal(attributes.length, 1, `ChatInput must declare ${name} exactly once`);

  const [attribute] = attributes;
  assert.ok(
    attribute.initializer
      && ts.isJsxExpression(attribute.initializer)
      && attribute.initializer.expression,
    `ChatInput ${name} must have a non-empty expression initializer`,
  );
  return attribute.initializer.expression;
}

function getReturnedExpression(callback) {
  if (ts.isArrowFunction(callback) && !ts.isBlock(callback.body)) return callback.body;

  assert.ok(ts.isBlock(callback.body), 'useMemo collector callback must have a body');
  const returns = callback.body.statements.filter(ts.isReturnStatement);
  assert.equal(returns.length, 1, 'useMemo collector callback must return exactly one expression');
  assert.ok(returns[0].expression, 'useMemo collector callback return must be non-empty');
  return returns[0].expression;
}

function testMainPageDoesNotRenderInlineApprovalGroups() {
  assert.equal(source.includes('<ToolApprovalGroup'), false);
  assert.equal(source.includes('function ToolApprovalGroup'), false);
  assert.equal(source.includes('function ToolApprovalCard'), false);
}

function testInputPopupReceivesUnifiedRunSurface() {
  const collectorBindings = collectNodes((node) => {
    if (!ts.isVariableDeclaration(node) || !ts.isIdentifier(node.name)) return false;
    const initializer = node.initializer;
    if (
      !initializer
      || !ts.isCallExpression(initializer)
      || !ts.isIdentifier(initializer.expression)
      || initializer.expression.text !== 'useMemo'
    ) return false;
    const callback = initializer.arguments[0];
    if (!callback || (!ts.isArrowFunction(callback) && !ts.isFunctionExpression(callback))) {
      return false;
    }
    const returned = getReturnedExpression(callback);
    return ts.isCallExpression(returned)
      && ts.isIdentifier(returned.expression)
      && returned.expression.text === 'collectPendingToolApprovalPrompts';
  });
  assert.equal(
    collectorBindings.length,
    1,
    'one memoized pending-approval collector must feed the input popup',
  );

  const collectorBinding = collectorBindings[0];
  const promptInitializer = collectorBinding.initializer;
  const collectorCallback = promptInitializer.arguments[0];
  const collectorCall = getReturnedExpression(collectorCallback);
  assert.equal(
    collectorCall.arguments.length,
    2,
    'the collector must receive run states and the server-confirmed pending ID set',
  );

  const chatInputs = collectNodes((node) => (
    (ts.isJsxOpeningElement(node) || ts.isJsxSelfClosingElement(node))
    && ts.isIdentifier(node.tagName)
    && node.tagName.text === 'ChatInput'
  ));
  const approvalChatInputs = chatInputs.filter((element) => (
    element.attributes.properties.some((property) => (
      ts.isJsxAttribute(property) && property.name.text === 'pendingToolApprovals'
    ))
  ));
  assert.equal(
    approvalChatInputs.length,
    1,
    'MainPage must render one ChatInput connected to the pending approval surface',
  );

  const pendingApprovals = getRequiredExpressionAttribute(
    approvalChatInputs[0],
    'pendingToolApprovals',
  );
  assert.ok(
    ts.isIdentifier(pendingApprovals)
      && pendingApprovals.text === collectorBinding.name.text,
    'ChatInput pendingToolApprovals must receive the memoized collector result',
  );

  getRequiredExpressionAttribute(
    approvalChatInputs[0],
    'onToolApprovalDecision',
  );
}

testMainPageDoesNotRenderInlineApprovalGroups();
testInputPopupReceivesUnifiedRunSurface();

console.log('toolApprovalPlacement tests passed');
