import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { randomUUID } from 'node:crypto';
import { Readable } from 'node:stream';
import { EventEmitter } from 'node:events';
import { DesktopScope, ScopeError, authorized, apply, projectItem, ROUTE } from '../src/desktop_host/index.mjs';

function mockContext() {
  const workspaces = new Map(), sessions = new Map(), agents = new Map(), calls = [];
  let route, registered = 0;
  const ctx = {
    calls, sessions, workspaces,
    agents: {get: id => agents.get(id), list: () => [...agents.values()]},
    agentPresets: {resolve: async () => ({id: 'standard'})},
    tools: {modeFor: () => 'native', schemas: () => [{name: 'bash'}, {name: 'read_file'}], wireSchemas() { return {schemas: this.schemas()}; },
      restrict() { throw new Error('Bridge must not restrict normal tools'); },
      guard() { throw new Error('Bridge must not install tool denials'); },
      presentAs() { throw new Error('Bridge must not override tool presentation'); }},
    approval: {effectivePolicy: () => 'ask'},
    permissionPresets: {current: () => 'workspace-write'},
    llm: {resolveModelInfo: async () => ({defaultMaxTokens: 256000, reasoning: {defaultEffort: 'high'}}), providerRetryPolicy: () => ({mode: 'normal', maxRetries: 3})},
    sessionTitle: {get: () => ({source: {kind: 'user'}})},
    workspaceRegistry: {get: id => workspaces.get(id)},
    workspaceController: {
      async create({path: directory}) {
        const prior = [...workspaces.values()].find(w => w.path === directory);
        if (prior) return {workspace: {...prior, workspaceId: prior.id}, created: false};
        const workspace = {id: randomUUID(), path: directory, sessionIds: []}; workspaces.set(workspace.id, workspace); return {workspace: {workspaceId: workspace.id, path: workspace.path, sessionIds: []}, created: true};
      },
      async rename({workspaceId, title}) { workspaces.get(workspaceId).title = title; return {workspace: workspaces.get(workspaceId)}; }
    },
    sessionController: {
      async modelCatalog() { calls.push('modelCatalog'); return {default: {provider: 'deepseek-official', model: 'deepseek-flash', reasoningEffort: 'high'}}; },
      async projections() { return {values: {}}; },
      async resolveAgent(id) { return {agent: agents.get(id)}; },
      async create({sessionId, workspaceId, agentPreset}) {
        calls.push('create'); const workspace = workspaces.get(workspaceId);
        if (!sessions.has(sessionId)) { sessions.set(sessionId, {header: {id: sessionId, cwd: workspace.path, agentPreset}, messages: []}); workspace.sessionIds.push(sessionId); agents.set(sessionId, {session: sessions.get(sessionId), status: 'idle', inbox: {nextTurn: [], nextStep: []}}); }
        return {sessionId, agentPreset};
      },
      async inspect(id) { calls.push('inspect:' + id); return {meta: sessions.get(id)?.header, events: []}; },
      async rename({sessionId, title}) { calls.push('rename'); sessions.get(sessionId).title = title; return {title}; },
      async prompt(value) { calls.push('prompt'); sessions.get(value.sessionId).messages.push(value); return {accepted: true}; },
      async cancel({sessionId}) { calls.push('cancel:' + sessionId); return {accepted: true}; },
      async *follow({address}) { calls.push('follow:' + address.sessionId); yield {type: 'snapshot', header: sessions.get(address.sessionId).header, cursor: -1, records: []}; },
      async list() { throw new Error('Do not read ordinary desktop history'); },
      async selectModel() { throw new Error('Do not change desktop defaults'); }
    },
    webServer: {host: '127.0.0.1', port: 34569, register(value) { route = value; registered++; return () => { registered--; }; }},
    waterfall: async (_name, _req, _res, next) => next(),
    effect: effect => { ctx.dispose = effect(); },
    get route() { return route; }, get registered() { return registered; }
  };
  sessions.set('original-desktop-chat', {header: {id: 'original-desktop-chat', cwd: '/original'}, messages: ['untouched']});
  return ctx;
}

async function fixture(operation) {
  const directory = await fs.realpath(await fs.mkdtemp(path.join(os.tmpdir(), 'dsh-host-test-'))); const ctx = mockContext();
  try { await operation(await DesktopScope.open(ctx, {projectRoot: directory}), ctx, directory); }
  finally { await fs.rm(directory, {recursive: true, force: true}); }
}

test('one parent creates one directory workspace; children are distinct ordinary rows', () => fixture(async (scope, ctx) => {
  const a = await scope.create('parent-a', 'child-a', 'First'), b = await scope.create('parent-a', 'child-b', 'Second'), c = await scope.create('parent-b', 'child-c', 'Third');
  assert.equal(a.workspaceId, b.workspaceId); assert.notEqual(a.sessionId, b.sessionId); assert.notEqual(a.workspaceId, c.workspaceId);
  assert.equal(a.agentPreset, 'standard');
  assert.deepEqual(a.capabilities.modelToolNames, ['bash', 'read_file']);
  assert.equal(a.capabilities.modelToolCount, 2);
  assert.equal(a.capabilities.toolExecution, 'native-policy');
  assert.equal(a.capabilities.approvalPolicy, 'ask');
  assert.equal(ctx.sessions.get(a.sessionId).header.origin, undefined); assert.equal(ctx.sessions.get(a.sessionId).header.parentSession, undefined);
  assert.deepEqual(ctx.sessions.get('original-desktop-chat').messages, ['untouched']);
}));
test('actual normal default and PTC surface are inherited without mutating permissions', () => fixture(async (scope, ctx) => {
  ctx.agentPresets.resolve = async () => ({id: 'ptc'});
  ctx.tools.modeFor = () => 'ptc';
  ctx.tools.schemas = () => [{name: 'bash'}, {name: 'read_file'}, {name: 'run_code'}];
  ctx.tools.wireSchemas = () => ({schemas: [{name: 'run_code'}]});
  const a = await scope.create('parent-a', 'child-a', 'Normal PTC');
  assert.equal(a.agentPreset, 'ptc');
  assert.deepEqual(a.capabilities.modelToolNames, ['run_code']);
  assert.equal(a.capabilities.availableToolCount, 3);
  const info = await scope.preflight(a.sessionId);
  assert.equal(info.defaultAgentPreset, 'ptc');
  assert.equal(info.presetRecoveryPolicy, 'inherited DSH policy');
  assert.deepEqual(info.desktopActivity, {runningAgents: 0, queuedAgents: 0});
}));
test('old started history retains identity while the compatibility preset supplies normal tools', () => fixture(async (scope, ctx) => {
  const a = await scope.create('parent-a', 'child-a', 'Legacy');
  delete scope.state.children[a.sessionId].agentPreset;
  ctx.sessions.get(a.sessionId).header.agentPreset = 'codex-dsh-text';
  await scope.owned(a.sessionId);
  assert.equal((await scope.preflight(a.sessionId)).capabilities.modelToolCount, 2);
  await scope.prompt(a.sessionId, 'request-a', 'followup');
  assert.equal(ctx.sessions.get(a.sessionId).messages.length, 1);
  ctx.sessions.get(a.sessionId).header.agentPreset = 'foreign-preset';
  await scope.owned(a.sessionId); // Native DSH owns the selected preset, not the bridge.
}));
test('idempotent child creation and host restart preserve exact mapping', () => fixture(async (scope, ctx, directory) => {
  const a = await scope.create('parent-a', 'child-a', 'First');
  assert.equal((await scope.create('parent-a', 'child-a', 'ignored')).sessionId, a.sessionId);
  const resumed = await DesktopScope.open(ctx, {projectRoot: directory, inferenceApproved: true, expectedReasoningEffort: 'inherit'});
  assert.equal((await resumed.create('parent-a', 'child-a', 'ignored')).sessionId, a.sessionId);
  assert.equal(ctx.calls.filter(call => call === 'create').length, 1);
}));
test('new empty child recreates a deleted workspace at the identical authorized directory without moving old children', () => fixture(async (scope, ctx) => {
  const old = await scope.create('parent-a', 'child-old', 'Old test');
  scope.state.children[old.sessionId].jobs = {historical: {state: 'completed', result: 'unchanged original'}};
  const oldChild = structuredClone(scope.state.children[old.sessionId]);
  const originalParent = {...scope.state.parents['parent-a']};
  ctx.workspaces.delete(old.workspaceId); ctx.sessions.delete(old.sessionId);
  const current = await scope.create('parent-a', 'child-new', 'Fresh empty conversation');
  assert.notEqual(current.workspaceId, old.workspaceId);
  assert.equal(current.cwd, originalParent.path); assert.equal(current.agentPreset, 'standard');
  assert.equal(current.state, 'ready'); assert.equal(ctx.sessions.get(current.sessionId).title, 'Fresh empty conversation');
  assert.deepEqual(ctx.sessions.get(current.sessionId).messages, []);
  assert.deepEqual(scope.state.children[old.sessionId], oldChild);
  assert.deepEqual(scope.state.parents['parent-a'], {...originalParent, workspaceId: current.workspaceId});
  await assert.rejects(scope.prompt(old.sessionId, 'old-request', 'do not revive'), /Workspace membership changed/);
  await assert.rejects(scope.cancel(old.sessionId, 'old-request'), /Workspace membership changed/);
  assert.ok(!ctx.calls.includes('prompt')); assert.ok(!ctx.calls.includes('cancel:' + old.sessionId));
  assert.deepEqual(await scope.lookup('parent-a', 'child-old'), {state: 'unverified', conversation: null});
}));
test('deleted workspace recovery reuses an existing native workspace at the same path without renaming it', () => fixture(async (scope, ctx) => {
  const old = await scope.create('parent-a', 'child-old', 'Old');
  ctx.workspaces.delete(old.workspaceId);
  const replacement = await ctx.workspaceController.create({path: old.cwd});
  ctx.workspaces.get(replacement.workspace.workspaceId).title = 'Existing user title';
  const current = await scope.create('parent-a', 'child-new', 'New');
  assert.equal(current.workspaceId, replacement.workspace.workspaceId);
  assert.equal(ctx.workspaces.get(current.workspaceId).title, 'Existing user title');
  assert.deepEqual(ctx.sessions.get('original-desktop-chat').messages, ['untouched']);
}));
test('deleted workspace with changed directory identity refuses recovery before creating any native workspace or child', () => fixture(async (scope, ctx) => {
  const old = await scope.create('parent-a', 'child-old', 'Old');
  const parentBefore = {...scope.state.parents['parent-a']};
  ctx.workspaces.delete(old.workspaceId);
  await fs.rename(old.cwd, old.cwd + '-original'); await fs.mkdir(old.cwd, {mode: 0o700});
  let workspaceCreates = 0;
  ctx.workspaceController.create = async () => { workspaceCreates++; throw new Error('Must not create a replacement'); };
  await assert.rejects(scope.create('parent-a', 'child-new', 'New'), /Workspace identity changed/);
  assert.equal(workspaceCreates, 0); assert.deepEqual(scope.state.parents['parent-a'], parentBefore);
  assert.equal(Object.keys(scope.state.children).length, 1);
}));
test('existing workspace path conflict fails closed instead of migrating the parent mapping', () => fixture(async (scope, ctx, directory) => {
  const old = await scope.create('parent-a', 'child-old', 'Old');
  const parentBefore = {...scope.state.parents['parent-a']};
  ctx.workspaces.get(old.workspaceId).path = directory;
  let workspaceCreates = 0;
  ctx.workspaceController.create = async () => { workspaceCreates++; throw new Error('Must not migrate an existing workspace'); };
  await assert.rejects(scope.create('parent-a', 'child-new', 'New'), /Workspace membership changed/);
  assert.equal(workspaceCreates, 0); assert.deepEqual(scope.state.parents['parent-a'], parentBefore);
  assert.equal(Object.keys(scope.state.children).length, 1);
}));
test('partial old child is never recreated in a new workspace or marked ready', () => fixture(async (scope, ctx) => {
  const old = await scope.create('parent-a', 'child-old', 'Old');
  scope.state.children[old.sessionId].state = 'creating';
  const before = structuredClone(scope.state.children[old.sessionId]);
  ctx.workspaces.delete(old.workspaceId);
  let workspaceCreates = 0;
  ctx.workspaceController.create = async () => { workspaceCreates++; throw new Error('Must not move a partial child'); };
  await assert.rejects(scope.create('parent-a', 'child-old', 'Retry'), /Workspace membership changed/);
  assert.equal(workspaceCreates, 0); assert.deepEqual(scope.state.children[old.sessionId], before);
  assert.deepEqual(await scope.lookup('parent-a', 'child-old'), {state: 'creating', conversation: null});
}));
test('normal prompt admission preserves context and uses queue mode', () => fixture(async (scope, ctx) => {
  const a = await scope.create('parent-a', 'child-a', 'First');
  for (const [id, text] of [['request-a', 'first'], ['request-b', 'followup']]) await scope.prompt(a.sessionId, id, text);
  const messages = ctx.sessions.get(a.sessionId).messages;
  assert.deepEqual(messages.map(m => m.content[0].text), ['first', 'followup']); assert.equal(messages[0].mode, 'queue');
  assert.equal(messages[0].clientTimeZone, 'UTC'); assert.ok(!ctx.calls.includes('selectModel'));
}));
test('foreign conversation access never reaches inspect/prompt/cancel/follow', () => fixture(async (scope, ctx) => {
  await assert.rejects(scope.prompt('original-desktop-chat', 'request-a', 'x'), ScopeError);
  await assert.rejects(scope.cancel('original-desktop-chat'), ScopeError);
  await assert.rejects(async () => { for await (const _item of scope.follow('original-desktop-chat', new AbortController().signal)) {} }, ScopeError);
  assert.ok(!ctx.calls.includes('inspect:original-desktop-chat')); assert.deepEqual(ctx.sessions.get('original-desktop-chat').messages, ['untouched']);
}));
test('normal account followups ignore obsolete test policies and preserve their audit ledger', () => fixture(async (scope, ctx, directory) => {
  const a = await scope.create('parent-a', 'child-a', 'First'), b = await scope.create('parent-a', 'child-b', 'Second');
  ctx.sessionController.modelCatalog = async () => ({default: {provider: 'deepseek-account', model: 'deepseek-flash', reasoningEffort: 'low'}});
  scope.config.inferenceApproved = false; scope.config.expectedProvider = 'deepseek-official'; scope.config.boundedTest = {sessionIds: [], prompt: 'obsolete test text'};
  scope.state.testSubmissions = {[a.sessionId]: {requestId: 'historical-test', submittedAt: 1}};
  await fs.writeFile(scope.file, JSON.stringify(scope.state));
  const info = await scope.preflight(a.sessionId); assert.equal(info.effectiveReasoningEffort, 'low'); assert.equal(info.defaultMaxTokens, 256000); assert.equal(info.automaticTitlePinnedOff, true);
  await scope.prompt(a.sessionId, 'request-a', 'normal task');
  await scope.prompt(a.sessionId, 'request-b', 'followup');
  const resumed = await DesktopScope.open(ctx, {...scope.config, projectRoot: directory});
  await resumed.prompt(a.sessionId, 'request-after-restart', 'another followup');
  await resumed.prompt(b.sessionId, 'request-other', 'independent task'); assert.equal(ctx.calls.filter(c => c==='prompt').length,4);
  assert.deepEqual(resumed.state.testSubmissions[a.sessionId], {requestId: 'historical-test', submittedAt: 1});
  assert.equal((await resumed.preflight(a.sessionId)).inferencePolicy, 'native-dsh');
}));
test('current native model provider and reasoning selection are inherited without pins', () => fixture(async (scope, ctx) => {
  ctx.sessionController.modelCatalog = async () => ({default: {provider: 'native-provider', model: 'native-model', reasoningEffort: 'high'}});
  const child = await scope.create('parent-a', 'child-a', 'First');
  const first = await scope.prompt(child.sessionId, 'request-a', 'native model task');
  assert.equal(first.selection.provider, 'native-provider'); assert.equal(first.selection.model, 'native-model');
  ctx.sessionController.modelCatalog = async () => ({default: {provider: 'deepseek-account', model: 'deepseek-flash'}});
  const next = await scope.prompt(child.sessionId, 'request-b', 'normal account task');
  assert.equal(next.selection.provider, 'deepseek-account'); assert.equal(ctx.calls.filter(c => c==='prompt').length, 2); assert.equal(ctx.workspaces.size, 1);
}));
test('duplicate admission and simultaneous native work protect turn integrity', () => fixture(async (scope, ctx) => {
  const a = await scope.create('parent-a', 'child-a', 'First');
  await scope.prompt(a.sessionId, 'request-a', 'x'); await assert.rejects(scope.prompt(a.sessionId, 'request-a', 'retry'), ScopeError);
  ctx.agents.get(a.sessionId).status = 'running'; await assert.rejects(scope.prompt(a.sessionId, 'request-b', 'race'), ScopeError);
  assert.equal(ctx.sessions.get(a.sessionId).messages.length, 1);
}));
test('cancellation only targets the exact active native request and preserves queued messages', () => fixture(async (scope, ctx) => {
  const a = await scope.create('parent-a', 'child-a', 'First');
  const agent = ctx.agents.get(a.sessionId);
  let events = [{type: 'turn/start', data: {turn: 1}}, {type: 'user/message', data: {source: {rpcId: 'native-user-work'}}}];
  agent.session.snapshotEvents = () => events;
  agent.status = 'running'; agent.inbox.nextTurn = [{source: {rpcId: 'bridge-request'}}];
  assert.equal((await scope.cancel(a.sessionId, 'bridge-request')).cancel_requested, false);
  assert.equal(ctx.calls.filter(value => value.startsWith('cancel:')).length, 0);
  events = [{type: 'turn/start', data: {turn: 2}}, {type: 'user/message', data: {source: {rpcId: 'bridge-request'}}}];
  assert.equal((await scope.cancel(a.sessionId, 'bridge-request')).cancel_requested, true);
  assert.equal(ctx.calls.filter(value => value.startsWith('cancel:')).length, 1);
  assert.equal(agent.inbox.nextTurn.length, 1);
  events.push({type: 'turn/end', data: {turn: 2}});
  assert.equal((await scope.cancel(a.sessionId, 'bridge-request')).cancel_requested, false);
  events.push({type: 'turn/start', data: {turn: 3}}, {type: 'user/message', data: {source: {rpcId: 'other-request'}}});
  assert.equal((await scope.cancel(a.sessionId, 'bridge-request')).cancel_requested, false);
  events = [{type: 'turn/start', data: {turn: 4}}, {type: 'user/message', data: {source: {kind: 'user'}}}, {type: 'user/message', data: {source: {rpcId: 'bridge-request'}}}];
  assert.equal((await scope.cancel(a.sessionId, 'bridge-request')).cancel_requested, false);
  assert.equal(ctx.calls.filter(value => value.startsWith('cancel:')).length, 1);
  await assert.rejects(scope.cancel(a.sessionId), /Exact prompt request id/);
}));
test('lost creation receipt lookup reads only the exact owned child without replay or agent loading', () => fixture(async (scope, ctx, directory) => {
  const a = await scope.create('parent-a', 'child-a', 'First');
  const reopened = await DesktopScope.open(ctx, {projectRoot: directory});
  ctx.sessionController.resolveAgent = async () => { throw new Error('Lookup must not load an agent'); };
  const creations = ctx.calls.filter(value => value === 'create').length;
  assert.equal((await reopened.lookup('parent-a', 'child-a')).conversation.sessionId, a.sessionId);
  reopened.state.children[a.sessionId].state = 'creating';
  assert.deepEqual(await reopened.lookup('parent-a', 'child-a'), {state: 'creating', conversation: null});
  reopened.state.children[a.sessionId].state = 'ready';
  assert.deepEqual(await reopened.lookup('parent-b', 'child-a'), {state: 'not_found', conversation: null});
  assert.deepEqual(await reopened.lookup('parent-a', 'missing-child'), {state: 'not_found', conversation: null});
  assert.equal(ctx.calls.filter(value => value === 'create').length, creations);
  assert.ok(!ctx.calls.includes('inspect:original-desktop-chat'));
  ctx.workspaces.get(a.workspaceId).sessionIds = [];
  assert.deepEqual(await reopened.lookup('parent-a', 'child-a'), {state: 'unverified', conversation: null});
}));
test('opening snapshot discards unused cumulative history beyond eight MiB without changing native data', () => {
  const snapshot = {type: 'snapshot', header: {id: 'synthetic-owned'}, cursor: 120, records: [
    {event: {seq: 1, type: 'user/message', data: {content: [{type: 'text', text: 'x'.repeat(5 * 1024 * 1024)}]}}},
    {event: {seq: 2, type: 'assistant/message', data: {message: {content: [{type: 'text', text: 'y'.repeat(5 * 1024 * 1024)}]}}}}
  ]};
  assert.ok(Buffer.byteLength(JSON.stringify(snapshot)) > 8 * 1024 * 1024);
  const projected = projectItem(snapshot);
  assert.deepEqual(projected, {type: 'snapshot', header: snapshot.header, cursor: 120, records: []});
  assert.ok(Buffer.byteLength(JSON.stringify(projected)) < 1024);
  assert.equal(snapshot.records[0].event.data.content[0].text.length, 5 * 1024 * 1024);
});
test('named task directory reuses its native workspace without reading foreign history', () => fixture(async (scope, ctx, directory) => {
  const project = path.join(directory, 'project'); await fs.mkdir(project);
  const registered = await ctx.workspaceController.create({path: project});
  const prior = ctx.workspaces.get(registered.workspace.workspaceId); prior.title = 'Existing native workspace'; prior.sessionIds.push('original-desktop-chat');
  const a = await scope.create('parent-project', 'child-a', 'n'.repeat(120), project);
  const b = await scope.create('parent-project', 'child-b', 'Followup child', project);
  assert.equal(a.cwd, project); assert.equal(a.workspaceId, registered.workspace.workspaceId); assert.equal(a.workspaceId, b.workspaceId);
  assert.equal(prior.title, 'Existing native workspace'); assert.deepEqual(ctx.sessions.get('original-desktop-chat').messages, ['untouched']);
  assert.ok(!ctx.calls.includes('inspect:original-desktop-chat'));
  await assert.rejects(scope.create('parent-project', 'child-c', 'Mismatch', directory), /Parent workspace/);
  await assert.rejects(scope.prompt('original-desktop-chat', 'foreign-request', 'no'), ScopeError);
}));
test('different Codex parents sharing one canonical project use the same storage project identity', () => fixture(async (scope, ctx, directory) => {
  const project = path.join(directory, 'user-project'); await fs.mkdir(project);
  const alias = path.join(directory, 'alias'); await fs.symlink(project, alias);
  const a = await scope.create('parent-a', 'child-a', 'First', project);
  const b = await scope.create('parent-b', 'child-b', 'Second', alias);
  assert.equal(a.workspaceId, b.workspaceId); assert.equal(a.projectId, b.projectId);
  assert.equal(a.cwd, b.cwd); assert.equal(a.cwd, await fs.realpath(project));
  assert.notEqual(a.sessionId, b.sessionId);
}));
test('long prompt follows ordinary native admission with no test-sized text cap', () => fixture(async (scope, ctx) => {
  const a = await scope.create('parent-a', 'child-a', 'Long task');
  await scope.prompt(a.sessionId, 'request-a', 'x'.repeat(40000));
  assert.equal(ctx.sessions.get(a.sessionId).messages[0].content[0].text.length, 40000);
}));
test('changed physical directory or group membership refuses prompt', () => fixture(async (scope, ctx) => {
  const a = await scope.create('parent-a', 'child-a', 'First'); ctx.workspaces.get(a.workspaceId).sessionIds = [];
  await assert.rejects(scope.prompt(a.sessionId, 'request-a', 'x'), ScopeError);
  assert.ok(!ctx.calls.includes('prompt'));
}));
test('loopback capability rejects wrong host, browser origins, unknown method and missing token', () => {
  const token = 'test-only-token', request = {method: 'POST', url: ROUTE, socket: {remoteAddress: '127.0.0.1'}, headers: {host: '127.0.0.1:34569', authorization: `Bearer ${token}`}};
  assert.equal(authorized(request, token, 34569), true);
  for (const invalid of [{...request, method: 'GET'}, {...request, socket: {remoteAddress: '192.168.1.2'}}, {...request, headers: {...request.headers, origin: 'http://evil.invalid'}}, {...request, headers: {...request.headers, host: 'localhost:34569'}}, {...request, headers: {...request.headers, authorization: 'Bearer wrong'}}]) assert.equal(authorized(invalid, token, 34569), false);
});
test('disabled plugin creates no route, capability or desktop reads', async () => {
  const ctx = mockContext(); await apply(ctx, {enabled: false}); assert.equal(ctx.registered, 0); assert.deepEqual(ctx.calls, []);
});
test('scope capability uses existing listener and is removed on unload', async () => {
  const directory = await fs.realpath(await fs.mkdtemp(path.join(os.tmpdir(), 'dsh-host-activation-test-'))), ctx = mockContext();
  try {
    await apply(ctx, {enabled: true, inferenceApproved: false, projectRoot: directory, expectedReasoningEffort: 'inherit'});
    assert.equal(ctx.registered, 1); assert.equal(ctx.route.path, ROUTE);
    const file = path.join(directory, 'run/desktop-host/link.json'), metadata = JSON.parse(await fs.readFile(file, 'utf8'));
    assert.equal(metadata.url, `http://127.0.0.1:34569${ROUTE}`); assert.equal((await fs.stat(file)).mode & 0o777, 0o600);
    assert.equal(metadata.token.length, 43); assert.deepEqual(ctx.calls, []);
    await ctx.dispose(); assert.equal(ctx.registered, 0); await assert.rejects(fs.stat(file), {code: 'ENOENT'});
  } finally { await fs.rm(directory, {recursive: true, force: true}); }
});
test('diagnostic and reasoning chunks are not copied to MCP, original DSH data is untouched', () => {
  const error = {type: 'event', event: {seq: 1, type: 'turn/end', data: {turn: 1, reason: {kind: 'error', error: {message: 'secret-like diagnostic'}}}}};
  assert.equal(JSON.stringify(projectItem(error)).includes('secret-like'), false); assert.ok(error.event.data.reason.error);
  const chunk = {type: 'assistant-stream', frame: {type: 'chunk', chunk: {type: 'reasoning', text: 'normal DSH reasoning'}}};
  assert.equal(JSON.stringify(projectItem(chunk)).includes('normal DSH reasoning'), false); assert.equal(chunk.frame.chunk.text, 'normal DSH reasoning');
});
test('scoped HTTP wire delegates new/prompt/follow to normal owned Desktop services', async () => {
  const directory = await fs.realpath(await fs.mkdtemp(path.join(os.tmpdir(), 'dsh-host-wire-test-'))), ctx = mockContext();
  class Response extends EventEmitter {
    constructor() { super(); this.headers = {}; this.lines = []; this.headersSent = false; }
    setHeader(key, value) { this.headers[key] = value; }
    writeHead(status, headers = {}) { this.status = status; Object.assign(this.headers, headers); this.headersSent = true; }
    write(value) { this.lines.push(value); return true; }
    end(value = '') { this.lines.push(value); }
  }
  try {
    await apply(ctx, {enabled: true, inferenceApproved: true, projectRoot: directory, expectedReasoningEffort: 'inherit'});
    const link = JSON.parse(await fs.readFile(path.join(directory, 'run/desktop-host/link.json'), 'utf8'));
    async function call(body, auth = true) {
      const request = Readable.from([Buffer.from(JSON.stringify(body))]);
      Object.assign(request, {method: 'POST', url: ROUTE, socket: {remoteAddress: '127.0.0.1'}, headers: {host: '127.0.0.1:34569', ...(auth ? {authorization: `Bearer ${link.token}`} : {})}});
      const response = new Response(); await ctx.route.handler(request, response); return {status: response.status, text: response.lines.join('')};
    }
    const created = JSON.parse((await call({action: 'new', parent_id: 'parent-a', child_id: 'child-a', name: 'Wire child'})).text).value;
    const result = await call({action: 'prompt', session_id: created.sessionId, request_id: 'request-a', prompt: 'synthetic input'});
    assert.equal(result.status, 200); assert.equal(ctx.sessions.get(created.sessionId).messages[0].content[0].text, 'synthetic input');
    ctx.agents.get(created.sessionId).status = 'running';
    const busy = await call({action: 'prompt', session_id: created.sessionId, request_id: 'request-b', prompt: 'busy'});
    assert.equal(busy.status, 400); assert.equal(JSON.parse(busy.text).admission_state, 'not_submitted');
    assert.equal(JSON.parse(busy.text).scope_reason, 'Conversation has running/queued work');
    assert.equal(ctx.sessions.get(created.sessionId).messages.length, 1);
    const recovered = await call({action: 'lookup', parent_id: 'parent-a', child_id: 'child-a'});
    assert.equal(JSON.parse(recovered.text).value.conversation.sessionId, created.sessionId);
    const history = await call({action: 'follow', session_id: created.sessionId}); assert.equal(JSON.parse(history.text).header.id, created.sessionId);
    assert.equal((await call({action: 'cancel', session_id: 'original-desktop-chat'})).status, 400);
    assert.equal((await call({action: 'new', parent_id: 'parent-a', child_id: 'child-b', name: 'Unauthenticated'}, false)).status, 403);
    assert.deepEqual(ctx.sessions.get('original-desktop-chat').messages, ['untouched']);
    await ctx.dispose();
  } finally { await fs.rm(directory, {recursive: true, force: true}); }
});
test('startup waits for installed loader readiness without creating a new listener', async () => {
  const directory = await fs.realpath(await fs.mkdtemp(path.join(os.tmpdir(), 'dsh-host-ready-test-'))), ctx = mockContext();
  let ready; const settled = new Promise(resolve => { ready = resolve; });
  ctx.get = key => key === 'loader' ? {await: () => settled} : undefined;
  ctx.webServer.port = undefined;
  try {
    await apply(ctx, {enabled: true, inferenceApproved: false, projectRoot: directory, expectedReasoningEffort: 'inherit'});
    assert.equal(ctx.registered, 0); ctx.webServer.port = 34569; ready();
    const file = path.join(directory, 'run/desktop-host/link.json');
    let found = false;
    for (let i = 0; i < 100; i++) { try { await fs.stat(file); found = true; break; } catch { await new Promise(resolve => setImmediate(resolve)); } }
    assert.equal(found, true); assert.equal(ctx.registered, 1); await ctx.dispose();
  } finally { await fs.rm(directory, {recursive: true, force: true}); }
});
