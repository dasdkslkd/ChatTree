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
  const workflow = (nameOrRef, workflowArgs) => hostCall('workflow', { nameOrRef, args: workflowArgs });
  const log = (message, data) => hostCall('log', { message, data });
  const isNewAgentOptions = (value) => {
    if (!value || typeof value !== 'object' || Array.isArray(value)) return false;
    return Object.prototype.hasOwnProperty.call(value, 'agentType')
      || Object.prototype.hasOwnProperty.call(value, 'agent')
      || Object.prototype.hasOwnProperty.call(value, 'name');
  };
  const agent = (promptOrName, inputOrOptions = {}, maybeOptions = {}) => {
    if (typeof promptOrName === 'object' && promptOrName !== null) {
      return hostCall('agent', promptOrName);
    }
    if (typeof inputOrOptions === 'string' || !isNewAgentOptions(inputOrOptions)) {
      return hostCall('agent', { name: promptOrName, input: inputOrOptions, options: maybeOptions || {} });
    }
    const options = inputOrOptions;
    const name = options.agentType || options.agent || options.name || 'workflow-worker';
    return hostCall('agent', { name, input: promptOrName, options });
  };
  const phase_start = (name, data) => hostCall('phase_start', { name, data });
  const phase_end = (name, data) => hostCall('phase_end', { name, data });
  const phase = async (name, fn) => {
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
  const parallel = async (items) => Promise.all(items.map((item) => typeof item === 'function' ? item() : item));
  const pipeline = async (items, ...stages) => {
    if (!Array.isArray(items)) throw new Error('pipeline items must be an array');
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

  const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
  const script = String(payload.script || '').replace(/\bexport\s+const\s+meta\s*=/, 'const meta =');
  const fn = new AsyncFunction(
    'agent',
    'parallel',
    'pipeline',
    'phase',
    'phase_start',
    'phase_end',
    'log',
    'workflow',
    'budget',
    'args',
    `"use strict"; const require = undefined; const process = undefined; const global = undefined; ${script}`,
  );
  const result = await fn(agent, parallel, pipeline, phase, phase_start, phase_end, log, workflow, budget, args);
  emit({ type: 'done', result });
  process.exit(0);
} catch (error) {
  emit({ type: 'error', error: String(error?.stack || error?.message || error) });
  process.exit(1);
}
