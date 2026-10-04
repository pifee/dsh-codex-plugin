// Local adapter to ordinary Desktop sessions; native DSH owns model/tool permissions.
// No ACP process, DSH_HOME override, key reader, full-operator cookie or new listener.
import fs from 'node:fs/promises';
import path from 'node:path';
import { randomUUID, randomBytes, createHash, timingSafeEqual } from 'node:crypto';
import { once } from 'node:events';
import {SharedController, ControllerError} from './controller.mjs';

export const name = 'codex-dsh-desktop-link';
export const inject = ['webServer', 'workspaceController', 'workspaceRegistry', 'sessionController', 'agents', 'agentPresets', 'tools', 'llm', 'sessionTitle', 'approval', 'permissionPresets'];
export const ROUTE = '/codex-dsh-subagent';
// Existing started histories retain their preset identity; its profile declaration
// is now a compatibility copy of the normal Desktop composition, with no guards.
const LEGACY_PRESET = 'codex-dsh-text';
const DEFAULT_FRAME_BYTES = 8 * 1024 * 1024;
export class ScopeError extends Error {}
export class AdmissionRejected extends ScopeError {}
const must = (ok, message) => { if (!ok) throw new ScopeError(message); };
const identifier = value => typeof value === 'string' && /^[a-zA-Z0-9_-]+$/u.test(value) && !['__proto__', 'constructor', 'prototype'].includes(value);

async function privateDirectory(directory) {
  await fs.mkdir(directory, {recursive: true, mode: 0o700});
  const info = await fs.lstat(directory);
  must(info.isDirectory() && !info.isSymbolicLink() && !(info.mode & 0o077) && info.uid === process.getuid(), 'Private physical directory required');
}
async function save(file, value) {
  const temp = `${file}.${randomUUID()}.tmp`;
  await fs.writeFile(temp, JSON.stringify(value), {mode: 0o600, flag: 'wx'});
  await fs.rename(temp, file);
}
async function readState(file) {
  try {
    const info = await fs.lstat(file);
    must(info.isFile() && !info.isSymbolicLink() && !(info.mode & 0o077) && info.uid === process.getuid(), 'Private state file required');
    return JSON.parse(await fs.readFile(file, 'utf8'));
  } catch (error) { if (error.code === 'ENOENT') return {parents: {}, children: {}}; throw error; }
}

export class DesktopScope {
  constructor(ctx, config, root, state) {
    this.ctx = ctx; this.config = config; this.root = root; this.state = state;
    this.file = path.join(config.projectRoot, 'data/global/host-state.json'); this.chain = Promise.resolve(); this.promptIds = new Set();
    this.projectsRoot = path.join(config.projectRoot, 'data/projects');
    this.maxRequestBytes = config.maxRequestBytes ?? DEFAULT_FRAME_BYTES;
    this.maxHistoryFrameBytes = config.maxHistoryFrameBytes ?? DEFAULT_FRAME_BYTES;
    for (const limit of [this.maxRequestBytes, this.maxHistoryFrameBytes]) must(Number.isSafeInteger(limit) && limit >= 8192, 'Invalid frame size configuration');
  }
  static async open(ctx, config) {
    must(path.isAbsolute(config.projectRoot), 'Explicit projectRoot required');
    const project = await fs.realpath(config.projectRoot);
    must(project === config.projectRoot, 'Canonical projectRoot required');
    for (const directory of ['data', 'data/global', 'data/projects', 'run', 'run/desktop-host']) await privateDirectory(path.join(project, directory));
    const root = path.join(project, 'run/desktop-host');
    return new DesktopScope(ctx, config, root, await readState(path.join(project, 'data/global/host-state.json')));
  }
  serial(operation) {
    const task = this.chain.then(operation); this.chain = task.catch(() => {}); return task;
  }
  async verifyParentDirectory(parent) {
    const info = await fs.lstat(parent.path);
    must(!info.isSymbolicLink() && info.isDirectory() && await fs.realpath(parent.path) === parent.path && info.dev === parent.dev && info.ino === parent.ino, 'Workspace identity changed');
  }
  async projectIdentity(directory, isolatedParent) {
    const index = await readState(path.join(this.config.projectRoot, 'data/global/project-index.json'));
    const existing = Object.entries(index.projects ?? {}).find(([, item]) => item.cwd === directory);
    if (existing) {
      must(/^[0-9a-f]{64}$/u.test(existing[0]), 'Invalid project identity');
      return existing[0];
    }
    return createHash('sha256').update(isolatedParent === undefined ? `cwd:${directory}` : `parent:${isolatedParent}`).digest('hex');
  }
  async owned(sid) {
    must(typeof sid === 'string' && Object.hasOwn(this.state.children, sid), 'Unknown bridge-owned desktop conversation');
    const child = this.state.children[sid];
    const parent = this.state.parents[child.parent]; must(parent, 'Missing parent mapping');
    must(child.workspaceId === parent.workspaceId, 'Workspace membership changed');
    await this.verifyParentDirectory(parent);
    const workspace = this.ctx.workspaceRegistry.get(parent.workspaceId);
    must(workspace?.path === parent.path && workspace.sessionIds.includes(sid), 'Workspace membership changed');
    const observed = await this.ctx.sessionController.inspect(sid, new AbortController().signal);
    const header = observed?.meta;
    must(header?.id === sid && header.cwd === parent.path && !header.origin && !header.parentSession, 'Conversation ownership changed');
    return child;
  }
  async model(sid) {
    const catalog = await this.ctx.sessionController.modelCatalog();
    const projections = sid ? await this.ctx.sessionController.projections({sessionId: sid}, new AbortController().signal) : null;
    const selected = projections?.values?.modelSelection;
    const current = selected?.next ?? selected?.lastUsed ?? catalog.default;
    must(typeof current?.provider === 'string' && typeof current.model === 'string', 'DSH model selection unavailable');
    return {provider: current.provider, model: current.model, reasoningEffort: current.reasoningEffort ?? null};
  }
  capabilities(sid) {
    const agent = this.ctx.agents.get(sid);
    must(agent, 'Owned conversation agent unavailable');
    const available = this.ctx.tools.schemas(agent).map(tool => tool.name);
    const model = this.ctx.tools.wireSchemas(agent).schemas.map(tool => tool.name);
    return {modelToolCount: model.length, modelToolNames: model,
      availableToolCount: available.length, availableToolNames: available,
      presentation: this.ctx.tools.modeFor(agent), toolExecution: 'native-policy',
      approvalPolicy: this.ctx.approval.effectivePolicy(agent.session),
      permissionPreset: this.ctx.permissionPresets.current(agent.session),
      permissionView: 'normal DSH desktop UI only'};
  }
  async preflight(sid) {
    await this.owned(sid); const resolved = await this.ctx.sessionController.resolveAgent(sid);
    // The installed public controller returns {agent} or {error}, not an Agent.
    must(resolved?.agent?.session && !resolved.error, 'Owned conversation could not be resumed');
    const agent = resolved.agent;
    const selection = await this.model(sid); const capabilities = this.capabilities(sid);
    const info = await this.ctx.llm.resolveModelInfo(selection.provider, selection.model);
    const retry = this.ctx.llm.providerRetryPolicy(selection.provider);
    const normalPreset = await this.ctx.agentPresets.resolve();
    const live = this.ctx.agents.list();
    return {selection, capabilities, defaultAgentPreset: normalPreset.id,
      agentStatus: agent.status, queuedMessages: (agent.inbox?.nextTurn?.length ?? 0) + (agent.inbox?.nextStep?.length ?? 0),
      desktopActivity: {runningAgents: live.filter(item => item.status === 'running').length,
        queuedAgents: live.filter(item => item.inbox?.nextTurn?.length || item.inbox?.nextStep?.length).length},
      inferencePolicy: 'native-dsh', effectiveReasoningEffort: selection.reasoningEffort ?? info.reasoning?.defaultEffort ?? null, defaultMaxTokens: info.defaultMaxTokens ?? null, providerRetryPolicy: retry ?? null, presetRecoveryPolicy: 'inherited DSH policy', automaticTitlePinnedOff: this.ctx.sessionTitle.get(agent.session)?.source.kind === 'user'};
  }
  async create(parentId, childId, title, workingDirectory) {
    must(identifier(parentId) && identifier(childId), 'Stable parent and child identifiers required');
    must(typeof title === 'string' && !/[\x00-\x1f]/u.test(title), 'Invalid conversation title');
    let requestedDirectory;
    if (workingDirectory !== undefined) {
      must(typeof workingDirectory === 'string' && path.isAbsolute(workingDirectory), 'Existing absolute working directory required');
      requestedDirectory = await fs.realpath(workingDirectory);
      must((await fs.stat(requestedDirectory)).isDirectory(), 'Existing absolute working directory required');
    }
    return this.serial(async () => {
      // Creating a normal empty conversation performs no inference.
      let parent = this.state.parents[parentId];
      let child = Object.values(this.state.children).find(item => item.parent === parentId && item.childId === childId);
      must(!parent || !requestedDirectory || parent.path === requestedDirectory, 'Parent workspace already uses another directory');
      if (parent) {
        await this.verifyParentDirectory(parent);
        parent.projectId ??= await this.projectIdentity(parent.path);
        const workspace = this.ctx.workspaceRegistry.get(parent.workspaceId);
        if (workspace === undefined || workspace === null) {
          // A deleted workspace can be recreated only for a new child, using
          // the same previously authorized physical directory. Old children
          // retain their original workspace identity and remain inaccessible.
          must(!child, 'Workspace membership changed');
          const created = await this.ctx.workspaceController.create({path: parent.path});
          must(typeof created.workspace?.workspaceId === 'string' && created.workspace.path === parent.path, 'Workspace response contract mismatch');
          await this.verifyParentDirectory(parent);
          if (created.created) await this.ctx.workspaceController.rename({workspaceId: created.workspace.workspaceId, title: `Codex ${parentId.slice(0, 12)}`});
          parent = {...parent, workspaceId: created.workspace.workspaceId};
          this.state.parents[parentId] = parent; await save(this.file, this.state);
        } else must(workspace.path === parent.path, 'Workspace membership changed');
      }
      if (!parent) {
        const projectId = requestedDirectory === undefined ? createHash('sha256').update(`parent:${parentId}`).digest('hex') : await this.projectIdentity(requestedDirectory);
        const directory = requestedDirectory ?? path.join(this.projectsRoot, projectId, 'workspace');
        if (!requestedDirectory) { await privateDirectory(path.dirname(directory)); await privateDirectory(directory); }
        const created = await this.ctx.workspaceController.create({path: directory});
        must(typeof created.workspace.workspaceId === 'string' && created.workspace.path === directory, 'Workspace response contract mismatch');
        if (created.created) await this.ctx.workspaceController.rename({workspaceId: created.workspace.workspaceId, title: `Codex ${parentId.slice(0, 12)}`});
        const info = await fs.stat(directory);
        parent = {path: directory, workspaceId: created.workspace.workspaceId, dev: info.dev, ino: info.ino, projectId};
        this.state.parents[parentId] = parent; await save(this.file, this.state);
      }
      if (child) must(child.workspaceId === parent.workspaceId, 'Workspace membership changed');
      if (child?.state === 'ready') { await this.owned(child.sessionId); await this.ctx.sessionController.resolveAgent(child.sessionId); return {...child, projectId: parent.projectId, capabilities: this.capabilities(child.sessionId)}; }
      if (!child) {
        const preset = await this.ctx.agentPresets.resolve();
        must(typeof preset.id === 'string' && !preset.broken, 'Normal Desktop preset unavailable');
        const sid = `codex-${randomUUID()}`;
        child = {parent: parentId, childId, sessionId: sid, workspaceId: parent.workspaceId, cwd: parent.path, projectId: parent.projectId, agentPreset: preset.id, title, state: 'creating'};
        this.state.children[sid] = child; await save(this.file, this.state);
      }
      const presetId = child.agentPreset ?? LEGACY_PRESET;
      const created = await this.ctx.sessionController.create({sessionId: child.sessionId, workspaceId: parent.workspaceId, agentPreset: presetId});
      must(created.sessionId === child.sessionId && created.agentPreset === presetId, 'Created conversation/preset mismatch');
      await this.owned(child.sessionId);
      const capabilities = this.capabilities(child.sessionId);
      if (title) await this.ctx.sessionController.rename({sessionId: child.sessionId, title});
      child.state = 'ready'; await save(this.file, this.state); return {...child, capabilities};
    });
  }
  async prompt(sid, requestId, text) {
    must(identifier(requestId) && typeof text === 'string' && text.trim(), 'Invalid prompt');
    return this.serial(async () => {
      await this.owned(sid); await this.ctx.sessionController.resolveAgent(sid); const selection = await this.model(sid);
      this.capabilities(sid);
      must(!this.promptIds.has(requestId), 'Prompt request id already submitted; no automatic retry');
      const agent = this.ctx.agents.get(sid);
      if (!agent || agent.status === 'running' || agent.inbox?.nextTurn?.length || agent.inbox?.nextStep?.length) throw new AdmissionRejected('Conversation has running/queued work');
      this.promptIds.add(requestId); // admission uncertainty does not permit a retry
      const result = await this.ctx.sessionController.prompt({sessionId: sid, requestId, mode: 'queue', content: [{type: 'text', text}], clientTimeZone: 'UTC'}, new AbortController().signal);
      return {...result, selection};
    });
  }
  async lookup(parentId, childId) {
    must(identifier(parentId) && identifier(childId), 'Stable parent and child identifiers required');
    return this.serial(async () => {
      const child = Object.values(this.state.children).find(item => item.parent === parentId && item.childId === childId);
      if (!child) return {state: 'not_found', conversation: null};
      if (child.state !== 'ready') return {state: 'creating', conversation: null};
      // Never replay creation or load an agent while recovering a lost receipt.
      try { await this.owned(child.sessionId); }
      catch { return {state: 'unverified', conversation: null}; }
      return {state: child.state, conversation: {...child}};
    });
  }
  async cancel(sid, requestId) {
    await this.owned(sid);
    must(identifier(requestId), 'Exact prompt request id required for cancellation');
    const agent = this.ctx.agents.get(sid);
    // Native cancel is synchronous and session-wide. Check the live turn's
    // identity synchronously immediately before calling it, with no await gap.
    const events = agent?.session?.snapshotEvents?.();
    let turn = null, request = null, seenUser = false;
    if (Array.isArray(events)) for (const event of events) {
      if (event.type === 'turn/start') { turn = event.data?.turn ?? null; request = null; seenUser = false; }
      if (event.type === 'user/message' && turn !== null && !seenUser) { request = event.data?.source?.rpcId ?? null; seenUser = true; }
      if (event.type === 'turn/end' && event.data?.turn === turn) { turn = null; request = null; seenUser = false; }
    }
    if (agent?.status !== 'running' || turn === null || request !== requestId) return {cancel_requested: false, reason: 'active_request_not_matched'};
    const result = this.ctx.sessionController.cancel({sessionId: sid});
    return {...result, cancel_requested: true};
  }
  async *follow(sid, signal) {
    await this.owned(sid);
    for await (const item of this.ctx.sessionController.follow({address: {kind: 'session', sessionId: sid}, assistantStream: true}, signal)) {
      if (item.type === 'snapshot') must(item.header?.id === sid && !item.header.origin && !item.header.parentSession, 'History identity mismatch');
      yield projectItem(item); // ordinary desktop still consumes the original unmodified history
    }
  }
}

function projectEvent(event) {
  if (!event || !Number.isSafeInteger(event.seq)) throw new ScopeError('Malformed event');
  const type = event.type;
  if (type === 'turn/end') return {...event, data: {turn: event.data?.turn, reason: {kind: event.data?.reason?.kind}}};
  if (['user/message', 'assistant/message', 'turn/start'].includes(type)) return event;
  return {type: 'bridge/progress', seq: event.seq, time: event.time, data: {eventType: type}};
}
export function projectItem(item) {
  if (item.type === 'snapshot') return {type: 'snapshot', header: item.header, cursor: item.cursor, records: []};
  if (item.type === 'event') return {type: 'event', event: projectEvent(item.event)};
  if (item.type === 'assistant-stream') return {type: 'assistant-stream', frame: {...item.frame, ...(item.frame.type === 'chunk' ? {chunk: {type: 'bridge/live-progress'}} : {})}};
  throw new ScopeError('Unsupported history frame');
}

export function authorized(request, token, port) {
  const address = request.socket?.remoteAddress;
  if (!['127.0.0.1', '::1', '::ffff:127.0.0.1'].includes(address)) return false;
  if (request.method !== 'POST' || request.url !== ROUTE || request.headers.host !== `127.0.0.1:${port}` || request.headers.origin || request.headers['sec-fetch-site'] === 'cross-site') return false;
  const value = request.headers.authorization;
  if (typeof value !== 'string') return false;
  const actual = Buffer.from(value), expected = Buffer.from(`Bearer ${token}`);
  return actual.length === expected.length && timingSafeEqual(actual, expected);
}
async function body(request, maximum) {
  let length = 0; const parts = [];
  for await (const part of request) { length += part.length; must(length <= maximum, 'Request too large'); parts.push(part); }
  return JSON.parse(Buffer.concat(parts).toString('utf8'));
}
export async function apply(ctx, config) {
  if (config?.enabled !== true) return;
  must(ctx.webServer.host === '127.0.0.1', 'Existing Host must be loopback only');
  const scope = await DesktopScope.open(ctx, config);
  const controller = config.multiClient === true ? new SharedController(config.projectRoot) : null;
  const token = randomBytes(32).toString('base64url');
  let port;
  const linkFile = path.join(scope.root, 'link.json');
  const handler = async (req, res) => {
    res.setHeader('Cache-Control', 'no-store, no-transform');
    if (!authorized(req, token, port)) { res.writeHead(403); res.end(); return; }
    try {
      const value = await body(req, scope.maxRequestBytes); must(value && typeof value === 'object' && !Array.isArray(value), 'Invalid request');
      const keys = {new: ['action', 'parent_id', 'child_id', 'name'], lookup: ['action', 'parent_id', 'child_id'], preflight: ['action', 'session_id'], prompt: ['action', 'session_id', 'request_id', 'prompt'], cancel: ['action', 'session_id', 'request_id'], follow: ['action', 'session_id'], mcp: ['action', 'name', 'arguments']}[value.action];
      must(keys && Object.keys(value).every(key => keys.includes(key) || (value.action === 'new' && key === 'working_directory')) && keys.every(key => Object.hasOwn(value, key)), 'Unsupported action/fields');
      if (value.action === 'mcp') {
        must(controller, 'Multi-client controller not enabled');
        const result = await controller.tool(value.name, value.arguments);
        res.writeHead(200, {'Content-Type': 'application/json'}); res.end(JSON.stringify({ok: true, value: result})); return;
      }
      if (value.action === 'follow') {
        const stop = new AbortController(); res.once('close', () => stop.abort());
        res.writeHead(200, {'Content-Type': 'application/x-ndjson'});
        for await (const item of scope.follow(value.session_id, stop.signal)) {
          const line = JSON.stringify(item) + '\n'; must(Buffer.byteLength(line) <= scope.maxHistoryFrameBytes, 'History frame too large');
          if (!res.write(line)) await Promise.race([once(res, 'drain'), once(res, 'close').then(() => { throw new ScopeError('Reader disconnected'); })]);
        }
        res.end(); return;
      }
      const result = value.action === 'new' ? await scope.create(value.parent_id, value.child_id, value.name, value.working_directory) : value.action === 'lookup' ? await scope.lookup(value.parent_id, value.child_id) : value.action === 'preflight' ? await scope.preflight(value.session_id) : value.action === 'prompt' ? await scope.prompt(value.session_id, value.request_id, value.prompt) : await scope.cancel(value.session_id, value.request_id);
      res.writeHead(200, {'Content-Type': 'application/json'}); res.end(JSON.stringify({ok: true, value: result}));
    } catch (error) {
      // Only local policy literals may leave this adapter, never raw Host errors.
      const response = JSON.stringify({ok: false, error: 'Scoped desktop action failed; upstream diagnostics withheld', scope_reason: error instanceof ScopeError || error instanceof ControllerError ? error.message : 'upstream-service-refused', ...(error instanceof AdmissionRejected ? {admission_state: 'not_submitted'} : {})});
      if (res.headersSent) res.end(JSON.stringify({type: 'bridge/error'}) + '\n');
      else { res.writeHead(400, {'Content-Type': 'application/json'}); res.end(response); }
    }
  };
  let disposed = false, remove;
  ctx.effect(() => async () => { disposed = true; await controller?.close(); remove?.(); await fs.unlink(linkFile).catch(() => {}); });
  const start = async () => {
    if (disposed) return;
    port = ctx.webServer.port; must(Number.isInteger(port) && port > 0, 'Existing Host not ready');
    // Preserve the installed Desktop update admission lock; this route owns no writer.
    const guarded = (req, res) => ctx.waterfall('connection/request', req, res, () => handler(req, res));
    remove = ctx.webServer.register({kind: 'exact', path: ROUTE, handler: guarded});
    try { await save(linkFile, {url: `http://127.0.0.1:${port}${ROUTE}`, token, scope: 'bridge-owned conversations only', instance: randomUUID(), multiClient: !!controller}); }
    catch (error) { remove(); throw error; }
    if (disposed) { remove(); await fs.unlink(linkFile).catch(() => {}); }
  };
  // Match installed Web readiness discipline; do not await the loader inside apply.
  const settled = ctx.get?.('loader')?.await();
  if (settled) settled.then(start).catch(() => { ctx.logger?.warn('Desktop link not activated; diagnostics withheld'); });
  else await start();
}
