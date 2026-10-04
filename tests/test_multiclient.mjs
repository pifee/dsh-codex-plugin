import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {spawn} from 'node:child_process';
import {EventEmitter, once} from 'node:events';
import {Readable} from 'node:stream';
import {apply, ROUTE} from '../src/desktop_host/index.mjs';
import {SharedController} from '../src/desktop_host/controller.mjs';

const project = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
async function storedState(root) {
  const index = JSON.parse(await fs.readFile(path.join(root, 'data/global/project-index.json'), 'utf8')), sessions = {};
  for (const id of Object.keys(index.projects)) Object.assign(sessions, JSON.parse(await fs.readFile(path.join(root, 'data/projects', id, 'state.json'), 'utf8')).sessions);
  return {sessions};
}


async function fixture(operation) {
  const root = await fs.realpath(await fs.mkdtemp(path.join(os.tmpdir(), 'dsh-multiclient-')));
  await fs.mkdir(path.join(root, 'config')); await fs.mkdir(path.join(root, 'src'));
  await fs.writeFile(path.join(root, 'config/bridge.json'), JSON.stringify({runtime_root: 'run/bridge', link_file: 'run/desktop-host/link.json', rpc_timeout_seconds: 1, cancel_grace_seconds: .2, turn_timeout_seconds: 0}));
  await fs.writeFile(path.join(root, 'src/desktop_bridge.py'), `import sys, asyncio\nsys.path.insert(0, ${JSON.stringify(project)})\nfrom tests.mock_desktop_controller import main\nasyncio.run(main(sys.argv[-1]))\n`);
  try { await operation(root); } finally { await fs.rm(root, {recursive: true, force: true}); }
}

class Response extends EventEmitter {
  constructor() { super(); this.lines = []; }
  setHeader() {}
  writeHead(status) { this.status = status; this.headersSent = true; }
  write(line) { this.lines.push(line); return true; }
  end(line = '') { this.lines.push(line); }
}
function context() {
  const ctx = {
    webServer: {host: '127.0.0.1', port: 34569, register(route) { ctx.route = route; return () => { ctx.route = null; }; }},
    waterfall: (_name, _req, _res, next) => next(),
    effect(effect) { ctx.dispose = effect(); },
  };
  return ctx;
}
async function route(ctx, root, value, auth = true) {
  const link = JSON.parse(await fs.readFile(path.join(root, 'run/desktop-host/link.json'), 'utf8'));
  const req = Readable.from([Buffer.from(JSON.stringify(value))]);
  Object.assign(req, {method: 'POST', url: ROUTE, socket: {remoteAddress: '127.0.0.1'}, headers: {host: '127.0.0.1:34569', ...(auth ? {authorization: `Bearer ${link.token}`} : {})}});
  const res = new Response(); await ctx.route.handler(req, res);
  return {status: res.status, body: res.lines.join('') ? JSON.parse(res.lines.join('')) : null};
}

class Client {
  constructor(root, send) {
    const code = `import sys, asyncio; sys.path.insert(0, ${JSON.stringify(project)}); from tests.mock_desktop_client import main; asyncio.run(main(sys.argv[1]))`;
    this.child = spawn('/usr/bin/python3', ['-c', code, path.join(root, 'config/bridge.json')], {stdio: ['pipe', 'pipe', 'ignore']});
    this.pending = new Map(); this.sequence = 0; let buffer = '';
    this.child.stdout.on('data', part => {
      buffer += part.toString(); let newline;
      while ((newline = buffer.indexOf('\n')) !== -1) {
        const frame = JSON.parse(buffer.slice(0, newline)); buffer = buffer.slice(newline + 1);
        if (frame.kind === 'host') {
          send(frame.value).then(response => this.write({kind: 'host', id: frame.id, value: response}), () => this.write({kind: 'host', id: frame.id, value: {ok: false}}));
        } else {
          const pending = this.pending.get(frame.value.id);
          if (pending) { clearTimeout(pending.timer); this.pending.delete(frame.value.id); pending.resolve(frame.value); }
        }
      }
    });
    this.child.once('exit', () => {
      for (const pending of this.pending.values()) { clearTimeout(pending.timer); pending.reject(new Error('Synthetic client exited')); }
      this.pending.clear();
    });
  }
  write(frame) { if (!this.child.stdin.destroyed) this.child.stdin.write(JSON.stringify(frame) + '\n'); }
  request(method, params = {}) {
    const id = ++this.sequence;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => { this.pending.delete(id); reject(new Error('Synthetic client timeout')); }, 5000);
      this.pending.set(id, {resolve, reject, timer});
      this.write({kind: 'mcp', value: {jsonrpc: '2.0', id, method, params}});
    });
  }
  async start() { await this.request('initialize', {protocolVersion: '2025-11-25'}); }
  async tool(name, args = {}) {
    const message = await this.request('tools/call', {name, arguments: args});
    const result = message.result;
    if (!result || result.isError) throw new Error(result?.content?.[0]?.text ?? 'Synthetic MCP failure');
    return JSON.parse(result.content[0].text);
  }
  async close() {
    if (this.child.exitCode !== null) return;
    const exit = once(this.child, 'exit'); this.child.stdin.end(); await exit;
  }
}
async function settled(client, sid, jid) {
  for (let i = 0; i < 70; i++) {
    const result = await client.tool('dsh_status', {session_id: sid, job_id: jid, verbose: true});
    if (['completed', 'cancelled', 'failed'].includes(result.job.state)) return result.job;
    await delay(20);
  }
  throw new Error('Synthetic job did not settle');
}

test('two independent MCP clients share the existing authenticated route and one durable writer', () => fixture(async root => {
  const ctx = context(); await apply(ctx, {enabled: true, multiClient: true, projectRoot: root});
  const send = async value => (await route(ctx, root, value)).body;
  let a = new Client(root, send), b = new Client(root, send), c;
  try {
    await Promise.all([a.start(), b.start()]);
    assert.notEqual(a.child.pid, b.child.pid);
    const [first, second] = await Promise.all([a.tool('dsh_new', {parent_thread_id: 'parent-a', verbose: true}), b.tool('dsh_new', {parent_thread_id: 'parent-b', verbose: true})]);
    assert.notEqual(first.workspaceId, second.workspaceId); assert.notEqual(first.cwd, second.cwd);
    const compactChild = await b.tool('dsh_new', {parent_thread_id: 'parent-a'});
    assert.deepEqual(Object.keys(compactChild), ['session_id']);
    const compactJob = await b.tool('dsh_prompt', {session_id: compactChild.session_id, prompt: 'snake case'});
    assert.deepEqual(Object.keys(compactJob), ['session_id', 'job_id']);
    assert.equal(compactJob.session_id, compactChild.session_id);
    assert.equal((await settled(b, compactChild.session_id, compactJob.job_id)).result, 'synthetic answer: snake case');
    const sibling = await b.tool('dsh_new', {parent_thread_id: 'parent-a', verbose: true});
    assert.equal(first.workspaceId, sibling.workspaceId);
    const [one, two] = await Promise.all([a.tool('dsh_prompt', {session_id: first.sessionId, prompt: 'hold'}), b.tool('dsh_prompt', {session_id: second.sessionId, prompt: 'hold'})]);
    await assert.rejects(b.tool('dsh_prompt', {session_id: first.sessionId, prompt: 'race'}), /Running or uncertain/);
    assert.equal((await b.tool('dsh_status', {session_id: first.sessionId})).job_id, one.job_id);
    await a.close(); a = null; // disconnect must not cancel either shared job
    c = new Client(root, send); await c.start();
    assert.equal((await c.tool('dsh_status', {session_id: first.sessionId, verbose: true})).job.state, 'running');
    await c.tool('dsh_cancel', {session_id: first.sessionId});
    assert.equal((await settled(b, first.sessionId, one.job_id)).state, 'cancelled');
    assert.equal((await b.tool('dsh_status', {session_id: second.sessionId, verbose: true})).job.state, 'running');
    await b.tool('dsh_cancel', {session_id: second.sessionId});
    assert.equal((await settled(c, second.sessionId, two.job_id)).state, 'cancelled');
    const [followup, independent] = await Promise.all([c.tool('dsh_prompt', {session_id: first.sessionId, prompt: 'followup'}), b.tool('dsh_prompt', {session_id: second.sessionId, prompt: 'parallel'})]);
    assert.equal((await settled(b, first.sessionId, followup.job_id)).result, 'synthetic answer: followup');
    assert.equal((await settled(c, second.sessionId, independent.job_id)).result, 'synthetic answer: parallel');
    await c.tool('dsh_close', {session_id: first.sessionId});
    await assert.rejects(b.tool('dsh_prompt', {session_id: first.sessionId, prompt: 'closed'}), /Resume locally/);
    await b.tool('dsh_resume', {session_id: first.sessionId});
    const before = await storedState(root);
    await ctx.dispose();
    await apply(ctx, {enabled: true, multiClient: true, projectRoot: root});
    assert.equal((await c.tool('dsh_status', {session_id: first.sessionId, job_id: followup.job_id})).result, 'synthetic answer: followup');
    const after = await storedState(root);
    assert.deepEqual(Object.keys(after.sessions), Object.keys(before.sessions));
    assert.deepEqual(after.sessions[first.sessionId].jobs, before.sessions[first.sessionId].jobs);
    assert.equal((await b.tool('dsh_list')).sessions.length, 4);
    await assert.rejects(c.tool('dsh_status', {session_id: 'foreign-desktop-chat'}), /Unknown adapter-owned/);
    assert.equal((await route(ctx, root, {action: 'mcp', name: 'dsh_list', arguments: {}}, false)).status, 403);
    const native = JSON.parse(await fs.readFile(path.join(root, 'synthetic-dsh.json'), 'utf8'));
    assert.deepEqual(native.sessions[first.sessionId].messages, ['hold', 'followup']);
    assert.deepEqual(native.sessions[second.sessionId].messages, ['hold', 'parallel']);
  } finally {
    await Promise.all([a?.close(), b.close(), c?.close()]); await ctx.dispose();
  }
}));

test('abrupt mock controller restart preserves committed results and blocks uncertain replay', () => fixture(async root => {
  let service = new SharedController(root);
  async function tool(name, args) {
    const result = await service.tool(name, args);
    if (result.isError) throw new Error(result.content[0].text);
    return JSON.parse(result.content[0].text);
  }
  try {
    const session = await tool('dsh_new', {parent_thread_id: 'parent-a'});
    const completed = await tool('dsh_prompt', {session_id: session.sessionId, prompt: 'retained'});
    await delay(250);
    assert.equal((await tool('dsh_status', {session_id: session.sessionId})).job.state, 'completed');
    const active = await tool('dsh_prompt', {session_id: session.sessionId, prompt: 'hold'});
    await delay(60);
    const child = service.child, exit = once(child, 'exit'); child.kill('SIGKILL'); await exit; // only synthetic owned fixture
    await service.close(); service = new SharedController(root);
    assert.equal((await tool('dsh_status', {session_id: session.sessionId, job_id: completed.job_id})).job.result, 'synthetic answer: retained');
    assert.equal((await tool('dsh_status', {session_id: session.sessionId, job_id: active.job_id})).job.state, 'interrupted_unconfirmed');
    await assert.rejects(tool('dsh_prompt', {session_id: session.sessionId, prompt: 'replay'}), /Running or uncertain/);
    const native = JSON.parse(await fs.readFile(path.join(root, 'synthetic-dsh.json'), 'utf8'));
    assert.deepEqual(native.sessions[session.sessionId].messages, ['retained', 'hold']);
  } finally { await service.close(); }
}));

test('multi-client route stays disabled without explicit activation and never runs arbitrary methods', () => fixture(async root => {
  const ctx = context(); await apply(ctx, {enabled: true, projectRoot: root});
  try {
    assert.equal((await route(ctx, root, {action: 'mcp', name: 'dsh_list', arguments: {}})).body.scope_reason, 'Multi-client controller not enabled');
    await ctx.dispose(); await apply(ctx, {enabled: true, multiClient: true, projectRoot: root});
    assert.equal((await route(ctx, root, {action: 'mcp', name: 'execute_shell', arguments: {}})).body.scope_reason, 'Unknown tool or arguments');
    await assert.rejects(fs.stat(path.join(root, 'data/global/project-index.json')), {code: 'ENOENT'});
  } finally { await ctx.dispose(); }
}));

test('a competing legacy writer is refused without takeover or terminating the owner', () => fixture(async root => {
  const owner = new SharedController(root), competitor = new SharedController(root);
  try {
    await owner.tool('dsh_list', {}); const ownerPid = owner.child.pid;
    await assert.rejects(competitor.tool('dsh_list', {}), /Shared controller unavailable/);
    assert.equal(owner.child.pid, ownerPid); assert.equal(owner.child.exitCode, null);
    assert.equal((await owner.tool('dsh_list', {})).isError, false);
  } finally { await competitor.close(); await owner.close(); }
}));
