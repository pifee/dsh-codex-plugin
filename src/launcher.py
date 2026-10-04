"""Scoped shared Desktop launcher with exact-process exit supervision."""
import argparse
import asyncio
import ctypes
import fcntl
import hashlib
import json
import os
import select
import signal
from pathlib import Path
import shlex
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
SOURCE = Path(__file__).resolve().parent
sys.path.insert(0, str(SOURCE))
from app_server_websocket import WebSocket, WebSocketError

URL = 'ws://127.0.0.1:4500'
APP = Path('/Applications/ChatGPT.app')
DESKTOP = APP / 'Contents/MacOS/ChatGPT'
SERVER = APP / 'Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex'



class Refusal(Exception):
    def __init__(self, code): self.code = code


class System:
    def __init__(self):
        self.spawned_server = None

    def command(self, *args):
        # Output stays private and is never included in errors or user records.
        result = subprocess.run(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, timeout=5, check=False)
        if len(result.stdout) > 65536: raise Refusal('inspection_unavailable')
        return result.returncode, result.stdout.decode('utf-8', errors='strict')

    def desktops(self):
        code, raw = self.command('/usr/bin/pgrep', '-x', DESKTOP.name)
        if code == 1 and not raw: return []
        if code: raise Refusal('desktop_status_unavailable')
        rows = []
        for value in raw.splitlines():
            if not value.isdecimal() or int(value) <= 0:
                raise Refusal('desktop_identity_unverified')
            pid = int(value)
            try: identity = self.identity(pid)
            except Refusal:
                if not self.process_alive(pid): continue
                raise Refusal('desktop_identity_unverified')
            if identity[0] != str(DESKTOP) or identity[1] != os.getuid():
                raise Refusal('desktop_identity_unverified')
            rows.append({'pid': pid})
        return rows

    def listeners(self):
        code, raw = self.command('/usr/sbin/lsof', '-nP', '-iTCP:4500', '-sTCP:LISTEN', '-Fpn')
        if code == 1 and not raw: return {}
        if code: raise Refusal('listener_inspection_unavailable')
        rows, pid = {}, None
        for line in raw.splitlines():
            if line.startswith('p'):
                pid = int(line[1:]); rows.setdefault(pid, [])
            elif line.startswith('n') and pid is not None: rows[pid].append(line[1:])
        if not rows: raise Refusal('listener_inspection_unavailable')
        return rows

    def identity(self, pid):
        lib = ctypes.CDLL('/usr/lib/libproc.dylib')
        buffer = ctypes.create_string_buffer(4096)
        if lib.proc_pidpath(pid, buffer, len(buffer)) <= 0:
            raise Refusal('server_identity_unverified')
        code, raw = self.command('/bin/ps', '-p', str(pid), '-o', 'uid=', '-o', 'lstart=', '-o', 'command=')
        if code: raise Refusal('server_identity_unverified')
        parts = raw.strip().split(None, 6)
        if len(parts) != 7: raise Refusal('server_identity_unverified')
        # PROC_PIDTBSDINFO exposes microsecond birth time, unlike ps lstart.
        class BSDInfo(ctypes.Structure):
            _fields_ = [('prefix', ctypes.c_byte * 120),
                        ('start_seconds', ctypes.c_uint64), ('start_microseconds', ctypes.c_uint64)]
        info = BSDInfo()
        if lib.proc_pidinfo(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info)) != ctypes.sizeof(info):
            raise Refusal('process_birth_unverified')
        birth = (info.start_seconds, info.start_microseconds)
        return buffer.value.decode(), int(parts[0]), birth, tuple(shlex.split(parts[6]))

    def process_alive(self, pid):
        try: os.kill(pid, 0); return True
        except ProcessLookupError: return False
        except PermissionError: raise Refusal('process_identity_unverified')

    def connected(self, pid):
        code, raw = self.command('/usr/sbin/lsof', '-nP', '-a', '-p', str(pid),
                                 '-iTCP:4500', '-sTCP:ESTABLISHED', '-Fn')
        return code == 0 and any(line.startswith('n') and '->127.0.0.1:4500' in line
                                 for line in raw.splitlines())

    def handshake(self, timeout=5):
        async def probe():
            connection = await WebSocket.connect(websocket_url=URL, max_message_bytes=65536)
            try:
                await connection.send_json({'id': 1, 'method': 'initialize',
                    'params': {'clientInfo': {'name': 'dsh-shared-launcher', 'version': '2.0.0'},
                               'capabilities': {}}})
                while True:
                    response = await connection.receive_json()
                    if not isinstance(response, dict): raise Refusal('server_handshake_failed')
                    # Ignore notifications/requests; never answer native approvals.
                    if type(response.get('id')) is not int or response['id'] != 1 or 'method' in response:
                        continue
                    if 'error' in response or not isinstance(response.get('result'), dict):
                        raise Refusal('server_handshake_failed')
                    break
                await connection.send_json({'method': 'initialized', 'params': {}})
            finally: await connection.close()
        asyncio.run(asyncio.wait_for(probe(), timeout))

    def start_server(self):
        self.spawned_server = subprocess.Popen([str(SERVER), 'app-server', '--listen', URL],
            cwd=ROOT, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True)
        return self.spawned_server.pid

    def cleanup_spawned_server(self):
        child = self.spawned_server
        if child is None or child.poll() is not None or self.desktops(): return False
        # An unreaped child handle identifies our own process lifecycle, even
        # when startup never reached the listener stage. Never use an arbitrary PID.
        child.terminate()
        try: child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill(); child.wait(timeout=2)
        return True

    def start_desktop(self):
        env = os.environ.copy()
        env.pop('CODEX_APP_SERVER_FORCE_CLI', None)
        env['CODEX_APP_SERVER_WS_URL'] = URL
        return subprocess.Popen([str(DESKTOP)], cwd=ROOT, env=env,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True).pid

    def activate_desktop(self, pid):
        rows = self.desktops()
        if len(rows) != 1 or rows[0]['pid'] != pid or not self.connected(pid):
            raise Refusal('desktop_connection_unverified')
        code, _ = self.command('/usr/bin/open', '-a', str(APP))
        if code: raise Refusal('desktop_activation_failed')

    def monitor_status(self, desktop_pid, server_pid):
        path = ROOT / 'run/launcher/exit-monitor.lock'
        try:
            with open(path, 'r') as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
                    return {'state': 'inactive'}
                except BlockingIOError: pass
                record = json.loads(lock.read(16384))
                expected = lifecycle_snapshot(self, desktop_pid, server_pid)
                if (canonical(record['pair']) == canonical(expected)
                        and identity_record(self.identity(record['monitor_pid'])) == record['monitor_identity']):
                    return {'state': 'active', 'pid': record['monitor_pid']}
        except FileNotFoundError: return {'state': 'inactive'}
        except Exception: pass
        return {'state': 'unconfirmed'}

    def attach_monitor(self, desktop_pid, server_pid):
        expected = lifecycle_snapshot(self, desktop_pid, server_pid)
        read_fd, write_fd = os.pipe()
        try:
            subprocess.Popen([sys.executable, '-B', str(Path(__file__).resolve()),
                '--monitor', json.dumps(expected), '--ready-fd', str(write_fd)],
                cwd=ROOT, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, start_new_session=True, pass_fds=(write_fd,))
            os.close(write_fd); write_fd = None
            if not select.select([read_fd], [], [], 15)[0]:
                raise Refusal('exit_monitor_unavailable')
            result = os.read(read_fd, 128)
            if result not in (b'attached', b'reused'):
                raise Refusal('exit_monitor_unavailable')
            return result.decode()
        finally:
            os.close(read_fd)
            if write_fd is not None: os.close(write_fd)

    def watch_processes(self, desktop_pid, server_pid):
        return ProcessExitWatch(desktop_pid, server_pid)

    def terminate_server(self, pid, expected):
        watcher = select.kqueue()
        try:
            watcher.control([select.kevent(pid, filter=select.KQ_FILTER_PROC,
                flags=select.KQ_EV_ADD | select.KQ_EV_ENABLE,
                fflags=select.KQ_NOTE_EXIT)], 0, 0)
            if identity_record(self.identity(pid)) != expected or self.desktops(): return False
            if watcher.control(None, 1, 0): return True
            os.kill(pid, signal.SIGTERM)
            if watcher.control(None, 1, 5): return True
            if identity_record(self.identity(pid)) != expected or self.desktops(): return False
            if watcher.control(None, 1, 0): return True
            os.kill(pid, signal.SIGKILL)
            return bool(watcher.control(None, 1, 2))
        except (ProcessLookupError, Refusal): return False
        finally: watcher.close()

    def monotonic(self): return time.monotonic()

    def pause(self, duration=.2): time.sleep(duration)


def server_identity(system):
    listeners = system.listeners()
    if not listeners: return None
    if len(listeners) != 1: raise Refusal('unexpected_listener')
    pid, addresses = next(iter(listeners.items()))
    if not addresses or any(address != '127.0.0.1:4500' for address in addresses):
        raise Refusal('unexpected_listener')
    identity = system.identity(pid)
    path, uid, _, argv = identity
    if path != str(SERVER) or uid != os.getuid(): raise Refusal('server_identity_unverified')
    if (argv.count('app-server') != 1 or argv.count('--listen') != 1
            or argv[argv.index('--listen') + 1:] != (URL,)
            or argv.index('app-server') + 1 != argv.index('--listen')):
        raise Refusal('server_identity_unverified')
    return pid, listeners, identity


def verified_server(system, timeout=5, expected=None):
    snapshot = server_identity(system)
    if expected is not None and snapshot != expected:
        raise Refusal('server_identity_changed')
    if snapshot is None: return None
    pid, listeners, identity = snapshot
    try:
        system.handshake(timeout)
    finally:
        if server_identity(system) != snapshot:
            raise Refusal('server_identity_changed')
    return pid


def launch(system, check=False, adopt=False):
    system.owned_server = None
    try: return launch_impl(system, check, adopt)
    except Exception:
        if not check and system.owned_server is not None:
            cleanup_owned_server(system, system.owned_server)
        if not check and hasattr(system, 'cleanup_spawned_server'):
            try: system.cleanup_spawned_server()
            except Exception: pass
        raise


def launch_impl(system, check=False, adopt=False):
    desktops = system.desktops()
    if len(desktops) > 1: raise Refusal('multiple_desktops')
    if desktops and not system.connected(desktops[0]['pid']):
        raise Refusal('desktop_requires_normal_quit')
    server = verified_server(system)
    if desktops:
        if server is None or not system.connected(desktops[0]['pid']):
            raise Refusal('desktop_connection_unverified')
        monitor = system.monitor_status(desktops[0]['pid'], server) if check else system.attach_monitor(desktops[0]['pid'], server)
        return {'ok': True, 'exit_monitor': monitor, 'action': 'reused', 'mode': 'shared',
                'desktop_pid': desktops[0]['pid'], 'server_pid': server}
    if adopt: raise Refusal('desktop_not_running')
    if check:
        return {'ok': True, 'action': 'start_desktop' if server else 'start_server_and_desktop',
                'mode': 'desktop_stopped', 'server_pid': server, 'readiness': 'ready'}
    if server is None:
        started = system.start_server()
        deadline = system.monotonic() + 15
        expected = None
        while system.monotonic() < deadline:
            snapshot = server_identity(system)
            if snapshot is not None:
                if snapshot[0] != started: raise Refusal('server_start_identity_mismatch')
                if expected is not None and snapshot != expected:
                    raise Refusal('server_identity_changed')
                expected = snapshot
                system.owned_server = (started, identity_record(snapshot[2]))
                remaining = deadline - system.monotonic()
                if remaining <= 0: break
                try:
                    server = verified_server(system, min(5, remaining), expected)
                except (TimeoutError, OSError, WebSocketError):
                    server = None
                if server is not None: break
            elif expected is not None:
                raise Refusal('server_identity_changed')
            remaining = deadline - system.monotonic()
            if remaining > 0: system.pause(min(.2, remaining))
        if server is None: raise Refusal('server_start_timeout')
    # Recheck Desktop status before creation to avoid an external launch race.
    if system.desktops(): raise Refusal('desktop_started_during_launch')
    pid = system.start_desktop()
    for _ in range(75):
        rows = system.desktops()
        if rows:
            if len(rows) != 1 or rows[0]['pid'] != pid: raise Refusal('desktop_start_identity_mismatch')
            if system.connected(pid):
                if verified_server(system) != server: raise Refusal('server_identity_changed')
                monitor = system.attach_monitor(pid, server)
                return {'ok': True, 'exit_monitor': monitor, 'action': 'started', 'mode': 'shared',
                        'desktop_pid': pid, 'server_pid': server}
        system.pause()
    raise Refusal('desktop_start_timeout')


def lifecycle_snapshot(system, desktop_pid, server_pid):
    rows = system.desktops()
    if len(rows) != 1 or rows[0]['pid'] != desktop_pid or not system.connected(desktop_pid):
        raise Refusal('desktop_connection_unverified')
    desktop = system.identity(desktop_pid)
    if desktop[0] != str(DESKTOP) or desktop[1] != os.getuid():
        raise Refusal('desktop_identity_unverified')
    server = server_identity(system)
    if server is None or server[0] != server_pid:
        raise Refusal('server_identity_changed')
    return {'desktop_pid': desktop_pid, 'desktop_identity': identity_record(desktop),
            'server_pid': server_pid, 'server_identity': identity_record(server[2])}


def identity_record(identity):
    path, uid, birth, argv = identity
    return {'path': path, 'uid': uid, 'birth': list(birth),
            'digest': hashlib.sha256(canonical(identity).encode()).hexdigest()}


def cleanup_owned_server(system, owned):
    pid, identity = owned
    try:
        snapshot = server_identity(system)
        if snapshot is None or snapshot[0] != pid or identity_record(snapshot[2]) != identity: return False
        if system.desktops(): return False
        return system.terminate_server(pid, identity)
    except Exception: return False


def canonical(value): return json.dumps(value, sort_keys=True, separators=(',', ':'))


class ProcessExitWatch:
    """Kernel exit events, independent of windows and app activation."""
    def __init__(self, desktop_pid, server_pid):
        self.queue = select.kqueue()
        self.desktop_pid, self.server_pid = desktop_pid, server_pid
        try:
            events = [select.kevent(pid, filter=select.KQ_FILTER_PROC,
                flags=select.KQ_EV_ADD | select.KQ_EV_ENABLE,
                fflags=select.KQ_NOTE_EXIT) for pid in (desktop_pid, server_pid)]
            self.queue.control(events, 0, 0)
        except BaseException:
            self.queue.close(); raise

    def wait_for_desktop_exit(self):
        while True:
            events = self.queue.control(None, 2, None)
            if any(event.ident == self.server_pid for event in events): return False
            if any(event.ident == self.desktop_pid for event in events): return True

    def server_exited(self):
        return any(event.ident == self.server_pid for event in self.queue.control(None, 2, 0))

    def close(self): self.queue.close()


def supervise_exit(system, expected, ready):
    """Only a registered Desktop process exit can authorize paired cleanup."""
    if canonical(lifecycle_snapshot(system, expected['desktop_pid'], expected['server_pid'])) != canonical(expected):
        raise Refusal('monitor_identity_changed')
    watch = system.watch_processes(expected['desktop_pid'], expected['server_pid'])
    try:
        # Registration and identity checking bracket the PID reuse race.
        if canonical(lifecycle_snapshot(system, expected['desktop_pid'], expected['server_pid'])) != canonical(expected):
            raise Refusal('monitor_identity_changed')
        ready()
        if not watch.wait_for_desktop_exit(): return 'server_exited'
        # A launch already in progress owns the service handoff; never wait on
        # its lock while it is waiting for this guardian to release monitor.lock.
        try: cleanup_lock = acquire_lock(ROOT / 'run/launcher')
        except Refusal: return 'server_reused'
        try:
            snapshot = server_identity(system)
            if (snapshot is None or snapshot[0] != expected['server_pid']
                    or identity_record(snapshot[2]) != expected['server_identity']
                    or watch.server_exited()):
                return 'server_changed'
            if system.desktops(): return 'server_reused'
            stopped = system.terminate_server(expected['server_pid'], expected['server_identity'])
            return 'server_terminated' if stopped else 'server_cleanup_unconfirmed'
        finally: cleanup_lock.close()
    finally: watch.close()


def monitor_main(expected, ready_fd):
    directory = ROOT / 'run/launcher'
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock = open(directory / 'exit-monitor.lock', 'a+')
    def ready(message=b'attached'):
        os.write(ready_fd, message)
    try:
        # Independently verify the pair even when another guardian owns the lock.
        system = System()
        fresh = lifecycle_snapshot(system, expected['desktop_pid'], expected['server_pid'])
        if canonical(fresh) != canonical(expected): raise Refusal('monitor_identity_changed')
        try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock.seek(0)
            record = json.loads(lock.read(16384))
            if identity_record(system.identity(record['monitor_pid'])) != record['monitor_identity']:
                raise Refusal('monitor_identity_changed')
            if canonical(record['pair']) == canonical(expected):
                ready(b'reused'); return 0
            old = record['pair']
            # Different pair is eligible only after the recorded old Desktop
            # process is gone; a recycled PID does not constitute that process.
            try: old_identity = identity_record(system.identity(old['desktop_pid']))
            except Refusal:
                if system.process_alive(old['desktop_pid']):
                    raise Refusal('monitor_identity_changed')
                old_identity = None
            if old_identity == old['desktop_identity']:
                raise Refusal('monitor_identity_changed')
            deadline = time.monotonic() + 5
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB); break
                except BlockingIOError:
                    if time.monotonic() >= deadline: raise Refusal('exit_monitor_unavailable')
                    time.sleep(.05)
            if canonical(lifecycle_snapshot(system, expected['desktop_pid'], expected['server_pid'])) != canonical(expected):
                raise Refusal('monitor_identity_changed')
        lock.seek(0); lock.truncate(); lock.write(canonical({'pair': expected, 'monitor_pid': os.getpid(),
            'monitor_identity': identity_record(system.identity(os.getpid()))})); lock.flush()
        supervise_exit(system, expected, ready)
        return 0
    except Exception:
        try: ready(b'failed')
        except OSError: pass
        return 2
    finally:
        lock.close(); os.close(ready_fd)


def acquire_lock(directory):
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock = open(directory / 'launcher.lock', 'a')
    try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        raise Refusal('launch_in_progress')
    return lock


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--check', action='store_true')
    modes.add_argument('--adopt', action='store_true')
    modes.add_argument('--activate', action='store_true')
    parser.add_argument('--monitor', help=argparse.SUPPRESS)
    parser.add_argument('--ready-fd', type=int, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.monitor is not None:
        if args.check or args.adopt or args.activate or args.ready_fd is None: return 2
        try: expected = json.loads(args.monitor)
        except Exception: return 2
        return monitor_main(expected, args.ready_fd)
    record = {'ok': False, 'code': 'launcher_unavailable'}
    lock = None
    try:
        if not DESKTOP.is_file() or not SERVER.is_file(): raise Refusal('official_app_unavailable')
        if not args.check:
            directory = ROOT / 'run/launcher'
            lock = acquire_lock(directory)
        system = System()
        record = launch(system, args.check, args.adopt)
        if args.activate:
            system.activate_desktop(record['desktop_pid'])
            record['activated'] = True
    except Refusal as error: record = {'ok': False, 'code': error.code}
    except Exception: record = {'ok': False, 'code': 'launcher_inspection_failed'}
    finally:
        if lock is not None: lock.close()
    if not record['ok']: record['error'] = record['code']
    record['message'] = ('共享启动已就绪；用 ⌘Q 退出 Desktop 后配套服务会停止，关闭窗口不会停止服务。'
        if record['ok'] and not args.check else
        '只读检查完成，没有启动或激活应用。' if record['ok'] else
        '共享启动未完成：' + record['code'] + '。现有 Desktop 未被关闭。')
    print(json.dumps(record, ensure_ascii=False, sort_keys=True))
    return 0 if record['ok'] else 2


if __name__ == '__main__': sys.exit(main())
