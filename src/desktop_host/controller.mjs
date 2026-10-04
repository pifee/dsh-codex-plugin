// One owned Python controller, anonymous pipes, no listener or extra credentials.
import {spawn} from 'node:child_process';
import fs from 'node:fs/promises';
import path from 'node:path';

const METHODS = new Set(['dsh_new', 'dsh_list', 'dsh_prompt', 'dsh_status', 'dsh_cancel', 'dsh_close', 'dsh_resume']);
export class ControllerError extends Error {}
const fail = () => new ControllerError('Shared controller unavailable; admission may be uncertain; no automatic retry');

export class SharedController {
  constructor(projectRoot) {
    this.root = projectRoot; this.child = null; this.starting = null;
    this.pending = new Map(); this.nextId = 0; this.closed = false;
  }
  async start() {
    if (this.closed) throw fail();
    if (this.starting) return this.starting;
    if (this.child) return;
    this.starting = this.launch();
    try { await this.starting; } finally { this.starting = null; }
  }
  async launch() {
    const configFile = path.join(this.root, 'config/bridge.json');
    const config = JSON.parse(await fs.readFile(configFile, 'utf8'));
    this.maximum = config.max_frame_bytes ?? 8388608;
    this.capacity = config.max_pending_requests ?? 32;
    this.timeout = ((config.rpc_timeout_seconds ?? 10) + (config.cancel_grace_seconds ?? 5) + 2) * 1000;
    if (![this.maximum, this.capacity].every(Number.isSafeInteger) || this.maximum < 8192 || this.capacity < 1 || !Number.isFinite(this.timeout) || this.timeout <= 0) throw fail();
    if (this.closed) throw fail();
    const child = spawn('python3', [path.join(this.root, 'src/desktop_bridge.py'), '--config', configFile], {cwd: this.root, stdio: ['pipe', 'pipe', 'ignore']});
    this.child = child;
    let buffer = Buffer.alloc(0);
    const reject = () => {
      if (this.child === child) this.child = null;
      for (const [id, item] of this.pending) {
        if (item.child !== child) continue;
        clearTimeout(item.timer); this.pending.delete(id); item.reject(fail());
      }
    };
    child.once('error', reject); child.once('exit', reject);
    child.stdin.on('error', reject);
    child.stdout.on('error', reject);
    child.stdout.on('data', part => {
      buffer = Buffer.concat([buffer, part]);
      try {
        let newline;
        while ((newline = buffer.indexOf(10)) !== -1) {
          if (newline > this.maximum) throw fail();
          const line = buffer.subarray(0, newline); buffer = buffer.subarray(newline + 1);
          const message = JSON.parse(line.toString('utf8'));
          const item = this.pending.get(message.id);
          if (!item || item.child !== child) continue; // late reply to a timed-out request
          clearTimeout(item.timer); this.pending.delete(message.id);
          if (message.error || !Object.hasOwn(message, 'result')) item.reject(fail());
          else item.resolve(message.result);
        }
        if (buffer.length > this.maximum) throw fail();
      } catch { reject(); child.stdin.destroy(); child.kill('SIGTERM'); }
    });
    try {
      const initialized = await this.request(child, 'initialize', {protocolVersion: '2025-11-25', capabilities: {}, clientInfo: {name: 'dsh-host-controller', version: '0.3.0'}});
      if (initialized?.serverInfo?.name !== 'dsh-subagent-bridge') throw fail();
    } catch {
      await this.stop(child); throw fail();
    }
  }
  request(child, method, params) {
    if (this.closed || this.child !== child || this.pending.size >= this.capacity) return Promise.reject(fail());
    const id = ++this.nextId;
    const line = JSON.stringify({jsonrpc: '2.0', id, method, params}) + '\n';
    if (Buffer.byteLength(line) > this.maximum) return Promise.reject(fail());
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => { this.pending.delete(id); reject(fail()); }, this.timeout);
      this.pending.set(id, {resolve, reject, timer, child});
      child.stdin.write(line, error => {
        if (!error) return;
        const item = this.pending.get(id);
        if (item) { clearTimeout(item.timer); this.pending.delete(id); item.reject(fail()); }
      });
    });
  }
  async tool(name, args) {
    if (!METHODS.has(name) || !args || typeof args !== 'object' || Array.isArray(args)) throw new ControllerError('Unknown tool or arguments');
    await this.start();
    return this.request(this.child, 'tools/call', {name, arguments: args});
  }
  async close() {
    this.closed = true;
    if (this.starting) await this.starting.catch(() => {});
    const child = this.child;
    if (!child) return;
    await this.stop(child);
  }
  async stop(child) {
    if (child.exitCode !== null || child.signalCode !== null) return;
    await new Promise(resolve => {
      let force;
      const timer = setTimeout(() => {
        child.kill('SIGTERM');
        force = setTimeout(() => child.kill('SIGKILL'), 2000);
      }, this.timeout);
      child.once('exit', () => { clearTimeout(timer); clearTimeout(force); resolve(); });
      child.stdin.end(); // controller drains admissions, cancels own turns, saves
    });
  }
}
