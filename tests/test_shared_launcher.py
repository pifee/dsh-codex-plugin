"""Synthetic launcher tests: no app launch, network, credentials or inference."""
import importlib.util
import os
import subprocess
import sys
from pathlib import Path
import unittest
from unittest import mock
import tempfile

spec = importlib.util.spec_from_file_location('shared_launcher',
    Path(__file__).resolve().parents[1] / 'src/launcher.py')
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


class FakeSystem:
    def __init__(self, desktop=False, server=True, connected=True):
        self.desktop = desktop
        self.server = server
        self.connection = connected
        self.actions = []
        self.change = False
        self.unknown = False
        self.timeout = False
        self.clock = 0

    def desktops(self): return [{'pid': 200}] if self.desktop else []
    def listeners(self): return {100: ['127.0.0.1:4500']} if self.server else {}
    def identity(self, pid):
        if pid == 200:
            return (str(launcher.DESKTOP), os.getuid(), ('desktop-birth',), (str(launcher.DESKTOP),))
        return ('/unknown' if self.unknown else str(launcher.SERVER), os.getuid(),
                ('changed' if self.change else 'original',),
                (str(launcher.SERVER), 'app-server', '--listen', launcher.URL))
    def connected(self, pid): return self.connection
    def handshake(self, timeout=5):
        self.actions.append('handshake')
        if getattr(self, 'change_on_handshake', False): self.change = True
    def start_server(self): self.actions.append('server'); self.server = True; return 100
    def start_desktop(self):
        self.actions.append('desktop')
        if not self.timeout: self.desktop = True
        return 200
    def attach_monitor(self, desktop_pid, server_pid):
        self.actions.append('monitor'); return 'attached'
    def monitor_status(self, desktop_pid, server_pid): return {'state': 'inactive'}
    def terminate_server(self, pid, expected):
        self.actions.append('terminate'); self.server = False; return True
    def monotonic(self): return self.clock
    def pause(self, duration=.2): self.clock += duration


class LauncherTests(unittest.TestCase):
    def test_concurrent_launch_lock_refuses_then_releases(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            lock = launcher.acquire_lock(path)
            try:
                with self.assertRaises(launcher.Refusal) as caught:
                    launcher.acquire_lock(path)
                self.assertEqual(caught.exception.code, 'launch_in_progress')
            finally: lock.close()
            launcher.acquire_lock(path).close()

    def refusal(self, fake, code, check=False):
        with self.assertRaises(launcher.Refusal) as caught: launcher.launch(fake, check)
        self.assertEqual(caught.exception.code, code)

    def test_unknown_listener_never_receives_handshake_or_launch(self):
        fake = FakeSystem(); fake.unknown = True
        self.refusal(fake, 'server_identity_unverified')
        self.assertEqual(fake.actions, [])

    def test_normal_desktop_blocks_even_if_server_absent(self):
        fake = FakeSystem(desktop=True, server=False, connected=False)
        self.refusal(fake, 'desktop_requires_normal_quit')
        self.assertEqual(fake.actions, [])

    def test_shared_desktop_only_reused(self):
        fake = FakeSystem(desktop=True)
        self.assertEqual(launcher.launch(fake)['action'], 'reused')
        self.assertEqual(fake.actions, ['handshake', 'monitor'])

    def test_server_and_desktop_started_once(self):
        fake = FakeSystem(server=False)
        result = launcher.launch(fake)
        self.assertEqual(result['action'], 'started')
        self.assertEqual(fake.actions.count('server'), 1)
        self.assertEqual(fake.actions.count('desktop'), 1)

    def test_existing_server_reused(self):
        fake = FakeSystem()
        launcher.launch(fake)
        self.assertNotIn('server', fake.actions)

    def test_check_never_starts(self):
        fake = FakeSystem(server=False)
        self.assertEqual(launcher.launch(fake, True)['action'], 'start_server_and_desktop')
        self.assertEqual(fake.actions, [])

    def test_identity_change_rejected_after_handshake(self):
        fake = FakeSystem(); fake.change_on_handshake = True
        self.refusal(fake, 'server_identity_changed')
        self.assertNotIn('desktop', fake.actions)

    def test_desktop_failure_cleans_owned_service(self):
        fake = FakeSystem(server=False); fake.timeout = True
        self.refusal(fake, 'desktop_start_timeout')
        self.assertFalse(fake.server)
        self.assertEqual(set(fake.actions), {'server', 'desktop', 'handshake', 'terminate'})

    def test_startup_transient_handshake_retried(self):
        fake = FakeSystem(server=False)
        def handshake(timeout=5):
            fake.actions.append('handshake')
            if fake.actions.count('handshake') == 1: raise TimeoutError()
        fake.handshake = handshake
        self.assertEqual(launcher.launch(fake)['action'], 'started')
        self.assertEqual(fake.actions.count('server'), 1)
        self.assertEqual(fake.actions.count('handshake'), 3)

    def test_startup_timeout_uses_total_monotonic_budget(self):
        fake = FakeSystem(server=False)
        def handshake(timeout=5):
            fake.clock += timeout
            raise TimeoutError()
        fake.handshake = handshake
        self.refusal(fake, 'server_start_timeout')
        self.assertLessEqual(fake.clock, 15)
        self.assertNotIn('desktop', fake.actions)

    def test_startup_identity_change_after_transient_failure_refused(self):
        fake = FakeSystem(server=False)
        def handshake(timeout=5):
            fake.change = True
            raise TimeoutError()
        fake.handshake = handshake
        self.refusal(fake, 'server_identity_changed')
        self.assertNotIn('desktop', fake.actions)

    def test_desktop_failure_preserves_reused_service(self):
        fake = FakeSystem(); fake.timeout = True
        self.refusal(fake, 'desktop_start_timeout')
        self.assertTrue(fake.server)
        self.assertNotIn('terminate', fake.actions)

    def test_check_reused_desktop_does_not_attach_monitor(self):
        fake = FakeSystem(desktop=True)
        result = launcher.launch(fake, True)
        self.assertEqual(result['exit_monitor']['state'], 'inactive')
        self.assertNotIn('monitor', fake.actions)

    def test_non_loopback_listener_rejected(self):
        fake = FakeSystem(); fake.listeners = lambda: {100: ['*:4500']}
        self.refusal(fake, 'unexpected_listener')
        self.assertEqual(fake.actions, [])

    def test_multiple_desktops_rejected(self):
        fake = FakeSystem(); fake.desktops = lambda: [{'pid': 200}, {'pid': 201}]
        self.refusal(fake, 'multiple_desktops')


class PortableLauncherTests(unittest.TestCase):
    def test_adopt_absent_desktop_never_launches(self):
        fake = FakeSystem(desktop=False)
        with self.assertRaises(launcher.Refusal) as error: launcher.launch(fake, adopt=True)
        self.assertEqual(error.exception.code, 'desktop_not_running')
        self.assertNotIn('server', fake.actions); self.assertNotIn('desktop', fake.actions)

    def test_adopt_existing_pair_attaches_without_activation(self):
        fake = FakeSystem(desktop=True)
        result = launcher.launch(fake, adopt=True)
        self.assertEqual(result['action'], 'reused'); self.assertIn('monitor', fake.actions)

    def test_native_desktop_discovery_requires_path_and_user(self):
        system = launcher.System()
        system.command = lambda *_: (0, '200\n')
        system.identity = lambda _: (str(launcher.DESKTOP), os.getuid(), (1, 2), ())
        self.assertEqual(system.desktops(), [{'pid': 200}])
        system.identity = lambda _: ('/other/ChatGPT', os.getuid(), (1, 2), ())
        with self.assertRaises(launcher.Refusal): system.desktops()
        system.identity = lambda _: (str(launcher.DESKTOP), os.getuid() + 1, (1, 2), ())
        with self.assertRaises(launcher.Refusal): system.desktops()

    def test_discovery_skips_process_that_exited_during_inspection(self):
        system = launcher.System(); system.command = lambda *_: (0, '200\n')
        def identity(_): raise launcher.Refusal('process_birth_unverified')
        system.identity = identity; system.process_alive = lambda _: False
        self.assertEqual(system.desktops(), [])
        system.process_alive = lambda _: True
        with self.assertRaises(launcher.Refusal): system.desktops()

    def test_official_server_spawn_has_no_script_or_model_override(self):
        system = launcher.System()
        with mock.patch.object(launcher.subprocess, 'Popen') as spawn:
            spawn.return_value.pid = 100
            self.assertEqual(system.start_server(), 100)
        self.assertEqual(spawn.call_args.args[0], [str(launcher.SERVER), 'app-server', '--listen', launcher.URL])

    def test_start_sh_handles_spaces_defaults_to_activation_and_forwards_check(self):
        with tempfile.TemporaryDirectory(prefix='portable launcher ') as directory:
            root = Path(directory); (root / 'src').mkdir()
            source = Path(__file__).resolve().parents[1]
            (root / 'start.sh').write_bytes((source / 'start.sh').read_bytes())
            (root / 'src/launcher.py').write_text('import json,sys; print(json.dumps(sys.argv[1:]))')
            default = subprocess.run(['/bin/sh', str(root / 'start.sh')], capture_output=True, text=True, check=True)
            check = subprocess.run(['/bin/sh', str(root / 'start.sh'), '--check'], capture_output=True, text=True, check=True)
            self.assertEqual(default.stdout.strip(), '["--activate"]')
            self.assertEqual(check.stdout.strip(), '["--check"]')


class HandshakeTests(unittest.TestCase):
    def connection(self, responses):
        class Connection:
            def __init__(self): self.sent = []; self.closed = False
            async def send_json(self, value): self.sent.append(value)
            async def receive_json(self): return responses.pop(0)
            async def close(self): self.closed = True
        return Connection()

    def test_initialize_ignores_native_requests_and_unrelated_ids(self):
        connection = self.connection([{'id': 10, 'method': 'approval/request'},
            {'id': True, 'result': {}}, {'id': 1, 'result': {}}])
        async def connect(**kwargs): return connection
        with mock.patch.object(launcher.WebSocket, 'connect', side_effect=connect):
            launcher.System().handshake()
        self.assertEqual([value['method'] for value in connection.sent], ['initialize', 'initialized'])
        self.assertTrue(connection.closed)

    def test_rejected_initialize_is_sanitized_and_connection_closes(self):
        connection = self.connection([{'id': 1, 'error': {'message': 'untrusted diagnostic'}}])
        async def connect(**kwargs): return connection
        with mock.patch.object(launcher.WebSocket, 'connect', side_effect=connect):
            with self.assertRaises(launcher.Refusal) as error: launcher.System().handshake()
        self.assertEqual(error.exception.code, 'server_handshake_failed')
        self.assertTrue(connection.closed)
        self.assertEqual(len(connection.sent), 1)


class FakeExitWatch:
    def __init__(self, system, server_first=False):
        self.system = system; self.server_first = server_first; self.closed = False
    def wait_for_desktop_exit(self):
        if self.server_first: return False
        self.system.desktop = False
        if getattr(self.system, 'change_after_exit', False): self.system.change = True
        if getattr(self.system, 'new_desktop', False): self.system.desktop = True
        return True
    def server_exited(self): return self.server_first
    def close(self): self.closed = True


class LifecycleTests(unittest.TestCase):
    def prepare(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        patch = mock.patch.object(launcher, 'ROOT', Path(temporary.name))
        patch.start(); self.addCleanup(patch.stop)
        system = FakeSystem(desktop=True)
        watch = FakeExitWatch(system)
        system.watch_processes = lambda *_: watch
        expected = launcher.lifecycle_snapshot(system, 200, 100)
        return system, watch, expected

    def test_true_process_exit_stops_exact_server(self):
        system, watch, expected = self.prepare()
        ready = []
        self.assertEqual(launcher.supervise_exit(system, expected, lambda: ready.append(True)), 'server_terminated')
        self.assertEqual(ready, [True]); self.assertTrue(watch.closed)
        self.assertEqual(system.actions, ['terminate'])

    def test_server_pid_birth_change_never_killed(self):
        system, watch, expected = self.prepare(); system.change_after_exit = True
        self.assertEqual(launcher.supervise_exit(system, expected, lambda: None), 'server_changed')
        self.assertNotIn('terminate', system.actions)

    def test_server_exits_first_never_signals_reused_pid(self):
        system, watch, expected = self.prepare(); watch.server_first = True
        self.assertEqual(launcher.supervise_exit(system, expected, lambda: None), 'server_exited')
        self.assertNotIn('terminate', system.actions)

    def test_relaunched_desktop_not_yet_connected_preserves_server(self):
        system, watch, expected = self.prepare(); system.new_desktop = True
        original_wait = watch.wait_for_desktop_exit
        def wait():
            result = original_wait(); system.connection = False; return result
        watch.wait_for_desktop_exit = wait
        self.assertEqual(launcher.supervise_exit(system, expected, lambda: None), 'server_reused')
        self.assertNotIn('terminate', system.actions)

    def test_in_progress_launch_hands_off_without_lock_deadlock(self):
        system, watch, expected = self.prepare()
        lock = launcher.acquire_lock(launcher.ROOT / 'run/launcher')
        try:
            self.assertEqual(launcher.supervise_exit(system, expected, lambda: None), 'server_reused')
            self.assertNotIn('terminate', system.actions)
        finally: lock.close()

    def test_identity_change_during_watch_registration_refused(self):
        system, watch, expected = self.prepare()
        def register(*_): system.change = True; return watch
        system.watch_processes = register
        with self.assertRaises(launcher.Refusal):
            launcher.supervise_exit(system, expected, lambda: self.fail('must not report ready'))
        self.assertTrue(watch.closed); self.assertNotIn('terminate', system.actions)

    def test_window_close_does_not_supply_process_exit(self):
        system, watch, expected = self.prepare()
        def wait():
            # Closing a UI window leaves the process alive; cleanup has not begun.
            self.assertTrue(system.desktop)
            self.assertNotIn('terminate', system.actions)
            raise InterruptedError('synthetic observation stop')
        watch.wait_for_desktop_exit = wait
        with self.assertRaises(InterruptedError): launcher.supervise_exit(system, expected, lambda: None)
        self.assertNotIn('terminate', system.actions); self.assertTrue(watch.closed)

    def test_owned_child_before_listener_is_cleaned_without_pid_guessing(self):
        system = launcher.System()
        system.desktops = lambda: []
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
        system.spawned_server = child
        try:
            self.assertTrue(system.cleanup_spawned_server())
            self.assertIsNotNone(child.returncode)
        finally:
            if child.poll() is None: child.kill(); child.wait()

    def test_closed_child_handle_never_signals_reused_pid(self):
        system = launcher.System()
        child = subprocess.Popen([sys.executable, '-c', 'pass']); child.wait()
        system.spawned_server = child
        system.desktops = lambda: self.fail('already-exited child needs no further cleanup')
        self.assertFalse(system.cleanup_spawned_server())

    def test_monitor_record_has_no_argv_and_duplicate_does_not_watch_twice(self):
        system, watch, expected = self.prepare()
        self.assertNotIn('argv', launcher.canonical(expected))
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(launcher, 'ROOT', Path(directory)), mock.patch.object(launcher, 'System', return_value=system):
            first_read, first_write = os.pipe(); second_read, second_write = os.pipe()
            calls = []
            def supervise(sys, pair, ready):
                calls.append(True); ready()
                self.assertEqual(launcher.monitor_main(pair, second_write), 0)
            try:
                with mock.patch.object(launcher, 'supervise_exit', side_effect=supervise):
                    self.assertEqual(launcher.monitor_main(expected, first_write), 0)
                self.assertEqual(os.read(first_read, 100), b'attached')
                self.assertEqual(os.read(second_read, 100), b'reused')
                self.assertEqual(calls, [True])
            finally: os.close(first_read); os.close(second_read)


if __name__ == '__main__': unittest.main()
