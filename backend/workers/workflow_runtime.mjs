import readline from 'node:readline';

const rl = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
const pending = new Map();
let nextId = 1;

function emit(message) {
  process.stdout.write(`${JSON.stringify(message)}\n`);
}

function hostCall(method, params = {}) {
  const id = nextId++;
  emit({ type: 'host_call', id, method, params });
  return new Promise((resolve, reject) => {
    pending.set(id, { resolve, reject });
  });
}

rl.on('line', (line) => {
  if (!line.trim()) return;
  let message;
  try {
    message = JSON.parse(line);
  } catch {
    return;
  }
  if (message.type === 'host_result') {
    const waiter = pending.get(message.id);
    if (!waiter) return;
    pending.delete(message.id);
    if (message.error) waiter.reject(new Error(String(message.error)));
    else waiter.resolve(message.result);
  }
});

const firstLine = await new Promise((resolve) => {
  rl.once('line', resolve);
});

try {
  const payload = JSON.parse(firstLine);
  const args = payload.args || {};
  const rawBudget = payload.budget || {};
  const budget = {
    ...rawBudget,
    total: rawBudget.total ?? null,
    spent: () => Number(rawBudget.spent || 0),
    remaining: () => rawBudget.total == null ? Infinity : Math.max(0, Number(rawBudget.total) - Number(rawBudget.spent || 0)),
  };

  function agent(prompt, options = {}) {
    if (arguments.length > 2) {
      throw new Error('agent(prompt, options) accepts only prompt and options');
    }
    if (typeof prompt !== 'string' || !prompt.trim()) {
      throw new Error('agent(prompt, options) requires a non-empty string prompt');
    }
    if (!options || typeof options !== 'object' || Array.isArray(options)) {
      throw new Error('agent(prompt, options) requires an options object');
    }
    return hostCall('agent', {
      name: options.agentType || 'workflow-worker',
      input: prompt,
      options,
    });
  }
  const log = (message, data) => hostCall('log', { message, data });
  const phase_start = (name, data) => hostCall('phase_start', { name, data });
  const phase_end = (name, data) => hostCall('phase_end', { name, data });
  const phase = async (name, fn) => {
    if (typeof name !== 'string' || !name.trim()) {
      throw new Error('phase(name, asyncFn) requires a non-empty phase name');
    }
    if (typeof fn !== 'function') {
      throw new Error('phase(name, asyncFn) requires an async function');
    }
    await phase_start(name);
    try {
      const result = await fn();
      await phase_end(name, { ok: true });
      return result;
    } catch (error) {
      await phase_end(name, { ok: false, error: String(error?.message || error) });
      throw error;
    }
  };
  const parallel = async (thunks) => {
    if (!Array.isArray(thunks)) throw new Error('parallel(thunks) requires an array');
    return Promise.all(thunks.map((thunk) => {
      if (typeof thunk !== 'function') throw new Error('parallel(thunks) items must be functions');
      return thunk();
    }));
  };
  const pipeline = async (items, ...stages) => {
    if (!Array.isArray(items)) throw new Error('pipeline items must be an array');
    for (const stage of stages) {
      if (typeof stage !== 'function') throw new Error('pipeline stages must be functions');
    }
    return Promise.all(items.map(async (item, index) => {
      let value = item;
      try {
        for (const stage of stages) {
          value = await stage(value, item, index);
          if (value === null || value === undefined) break;
        }
        return value ?? null;
      } catch {
        return null;
      }
    }));
  };
  const workflowContext = {
    agent,
    parallel,
    pipeline,
    phase,
    log,
    args,
    budget,
  };

  const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
  const script = compileWorkflowScript(String(payload.script || ''));
  const fn = new AsyncFunction(
    'workflowContext',
    `"use strict"; const require = undefined; const process = undefined; const global = undefined; ${script}`,
  );
  const result = await fn(workflowContext);
  emit({ type: 'done', result });
  process.exit(0);
} catch (error) {
  emit({ type: 'error', error: String(error?.stack || error?.message || error) });
  process.exit(1);
}

function compileWorkflowScript(source) {
  const trimmed = source.trim();
  if (!/^export\s+default\s+async\s+function\s+workflow\s*\(\s*ctx\s*\)\s*\{/.test(trimmed)) {
    throw new Error('workflow script must be `export default async function workflow(ctx) { ... }`');
  }
  return `${trimmed.replace(/^export\s+default\s+async\s+function\s+workflow/, 'async function workflow')}\nreturn await workflow(workflowContext);`;
}
