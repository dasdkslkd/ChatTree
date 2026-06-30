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
  const budget = payload.budget || {};
  const workflow = () => hostCall('workflow', {});
  const log = (message, data) => hostCall('log', { message, data });
  const agent = (name, input, options = {}) => {
    if (typeof name === 'object' && name !== null) {
      return hostCall('agent', name);
    }
    return hostCall('agent', { name, input, options });
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
  const pipeline = async (steps, initial) => {
    let value = initial;
    for (const step of steps) {
      value = await step(value);
    }
    return value;
  };

  const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
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
    `"use strict"; const require = undefined; const process = undefined; const global = undefined; ${payload.script}`,
  );
  const result = await fn(agent, parallel, pipeline, phase, phase_start, phase_end, log, workflow, budget, args);
  emit({ type: 'done', result });
  process.exit(0);
} catch (error) {
  emit({ type: 'error', error: String(error?.stack || error?.message || error) });
  process.exit(1);
}
