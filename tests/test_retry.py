import importlib.util
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from contextlib import redirect_stdout


SPEC = importlib.util.spec_from_file_location('altare_retry', Path(__file__).parents[1] / 'altare_retry.py')
app = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(app)

TENANT = '00000000-0000-4000-8000-000000000001'
RESOURCES = {
    'package': {'memoryMb': 4096, 'diskMb': 40960, 'cpuPercent': 125, 'serverSlots': 2},
    'extra': {'memoryMb': 0, 'diskMb': 0, 'cpuPercent': 0, 'serverSlots': 0},
    'used': {'memoryMb': 1024, 'diskMb': 0, 'cpuPercent': 25, 'servers': 1},
    'remaining': {'memoryMb': 3072, 'diskMb': 40960, 'cpuPercent': 100, 'serverSlots': 1},
}


class FakeApi:
    """Answers by path: locations, eggs, resources, the server list and creation requests."""

    def __init__(self):
        self.cooldown = 0
        self.locations = [{'slug': 'sg-sgp', 'name': 'Singapore', 'creationPaused': False, 'plusOnly': False},
                          {'slug': 'de-fra', 'name': 'Frankfurt', 'creationPaused': True, 'plusOnly': False}]
        self.eggs = [{'eggId': 45, 'name': 'Ubuntu 26.04', 'sandbox': True},
                     {'eggId': 2, 'name': 'Minecraft', 'sandbox': False}]
        self.servers = []
        self.list_code = 200
        self.locations_code = 200
        self.create = []
        self.calls = []

    def call(self, path, body=None, key=None):
        self.calls.append((path, body, key))
        if path == app.LOCATIONS_PATH:
            return self.locations_code, {'items': self.locations}
        if path == app.EGGS_PATH:
            return 200, {'items': self.eggs}
        if path.endswith('/resources'):
            return 200, RESOURCES
        if body is None:
            return self.list_code, ({'items': list(self.servers)} if self.list_code == 200 else {})
        return self.create.pop(0)

    def items(self, path):
        code, data = self.call(path)
        return code, (data['items'] if code == 200 else None)

    @property
    def posts(self):
        return [call for call in self.calls if call[1] is not None]


class ProvisionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = {
            'token': 'test-token', 'tenant': TENANT, 'password': 'test-password', 'name': 'example-server',
            'eggId': 45, 'memoryMb': 4096, 'cpuPercent': 100, 'diskMb': 10240, 'location': 'sg-sgp',
            'interval': 600, 'state_dir': Path(self.temp.name), 'webhook': None,
        }
        self.api = FakeApi()
        self.runner = app.Provisioner(self.config, self.api)
        self.quiet = redirect_stdout(io.StringIO())
        self.quiet.__enter__()

    def tearDown(self):
        self.quiet.__exit__(None, None, None)
        self.temp.cleanup()

    def again(self):
        self.runner.state['next_attempt_at'] = 0
        return self.runner.tick()

    def matching_server(self, server_id='existing-server'):
        server = {k: self.config[k] for k in ['name', 'memoryMb', 'cpuPercent', 'diskMb', 'eggId']}
        return {**server, 'id': server_id}

    def test_capacity_failure_retries_after_interval(self):
        self.api.create = [(422, {})]
        self.assertEqual(self.runner.tick(), 'wait')
        self.assertNotIn('pending_key', self.runner.state)
        self.assertEqual(self.runner.tick(), 'wait')
        self.assertEqual(len(self.api.posts), 1)

    def test_success_persisted_and_never_repeats(self):
        self.api.create = [(200, {'id': 'new-server'})]
        self.assertEqual(self.runner.tick(), 'done')
        self.assertEqual(app.Provisioner(self.config, self.api).tick(), 'done')
        self.assertEqual(len(self.api.posts), 1)
        saved = self.runner.path.read_text()
        self.assertNotIn('test-token', saved)
        self.assertNotIn('test-password', saved)

    def test_paused_region_skips_creation(self):
        self.config['location'] = 'de-fra'
        runner = app.Provisioner(self.config, self.api)
        self.assertEqual(runner.tick(), 'wait')
        self.assertEqual([c[0] for c in self.api.calls], [app.LOCATIONS_PATH])
        self.assertTrue(runner.state['region_paused'])
        self.assertIn('does not accept new servers', '\n'.join(app.status_lines(runner.state, location='de-fra')))

    def test_reopened_region_creates(self):
        self.api.locations[0]['creationPaused'] = True
        self.runner.tick()
        self.api.locations[0]['creationPaused'] = False
        self.api.create = [(201, {'id': 'server'})]
        self.assertEqual(self.again(), 'done')

    def test_unknown_location_pauses(self):
        self.config['location'] = 'xx-nowhere'
        runner = app.Provisioner(self.config, self.api)
        with self.assertRaises(app.ProvisionError):
            runner.tick()
        self.assertEqual(runner.state['paused'], 'unknown_location')

    def test_uncertain_response_reuses_idempotency_key(self):
        self.api.create = [(0, {}), (0, {})]
        self.runner.tick()
        key = self.runner.state['pending_key']
        self.again()
        self.assertEqual(self.api.posts[-1][2], key)

    def test_pending_server_reconciled_without_post(self):
        self.runner.state['pending_key'] = 'previous-attempt'
        self.api.servers = [self.matching_server()]
        self.assertEqual(self.runner.tick(), 'done')
        self.assertEqual(self.api.posts, [])

    def test_pending_server_reconciled_while_region_paused(self):
        self.api.locations[0]['creationPaused'] = True
        self.runner.state['pending_key'] = 'previous-attempt'
        self.api.servers = [self.matching_server()]
        self.assertEqual(self.runner.tick(), 'done')

    def test_unrelated_existing_server_pauses(self):
        self.api.servers = [{'name': 'example-server', 'id': 'existing'}]
        with self.assertRaises(app.ProvisionError):
            self.runner.tick()
        self.assertEqual(self.runner.state['paused'], 'name_already_exists')
        self.assertEqual(self.api.posts, [])

    def test_authentication_failure_stops_retries(self):
        self.api.locations_code = 401
        with self.assertRaises(app.ProvisionError) as caught:
            self.runner.tick()
        self.assertIn('altare_session', str(caught.exception))
        self.assertEqual(self.runner.state['paused'], 'http_401')
        with self.assertRaises(app.ProvisionError):
            self.runner.tick()
        self.assertEqual(len(self.api.calls), 1)

    def test_changed_target_starts_fresh(self):
        self.runner.state.update(attempts=3, pending_key='old')
        self.runner.save()
        loaded = app.Provisioner({**self.config, 'location': 'de-fra'}, self.api)
        self.assertNotIn('pending_key', loaded.state)
        self.assertNotIn('attempts', loaded.state)
        self.assertIn('warning', (self.config['state_dir'] / 'events.jsonl').read_text())

    def test_server_failure_needs_no_extra_request(self):
        self.api.create = [(503, {})]
        self.runner.tick()
        self.assertEqual(len(self.api.calls), 3)
        self.assertIn('pending_key', self.runner.state)

    def test_server_failure_rotates_key_when_nothing_created(self):
        self.api.servers = [{'name': 'other-server', 'id': 'x'}]
        self.api.create = [(503, {}), (503, {})]
        self.runner.tick()
        old = self.runner.state['pending_key']
        self.again()
        self.assertNotEqual(self.api.posts[-1][2], old)

    def test_server_failure_reconciles_created_server(self):
        self.api.create = [(503, {})]
        self.runner.tick()
        self.api.servers = [self.matching_server('late-server')]
        self.assertEqual(self.again(), 'done')
        self.assertEqual(self.runner.state['server_id'], 'late-server')

    def test_resume_lifts_pause(self):
        self.api.list_code = 401
        with self.assertRaises(app.ProvisionError):
            self.runner.tick()
        self.runner.resume()
        self.assertNotIn('paused', self.runner.state)
        self.api.list_code = 200
        self.api.create = [(201, {'id': 'server'})]
        self.assertEqual(self.runner.tick(), 'done')

    def test_reset_starts_over_after_completion(self):
        self.runner.state.update(completed=True, server_id='old', attempts=4)
        self.runner.reset()
        self.assertEqual(self.runner.state, {'target': self.runner.fingerprint})

    def test_rate_limit_honors_server_cooldown(self):
        self.api.cooldown = app.time.time() + 7200
        self.api.create = [(429, {})]
        self.runner.tick()
        self.assertGreaterEqual(self.runner.state['next_attempt_at'], self.api.cooldown)

    def test_credentials_only_sent_to_api(self):
        self.api.create = [(201, {'id': 'server'})]
        self.runner.tick()
        payload = self.api.posts[0][1]
        self.assertEqual(payload['ssh']['password'], 'test-password')
        self.assertEqual(payload['location'], 'sg-sgp')
        events = (self.config['state_dir'] / 'events.jsonl').read_text()
        self.assertNotIn('test-password', events)
        self.assertNotIn('test-token', events)

    def test_webhook_on_created_and_paused(self):
        self.config['webhook'] = 'https://hooks.example/secret-path'
        runner = app.Provisioner(self.config, self.api)
        self.api.servers = [{'name': 'example-server', 'id': 'existing'}]
        with patch.object(app.subprocess, 'run') as run:
            run.return_value.stdout = '\nHTTP:200'
            with self.assertRaises(app.ProvisionError):
                runner.tick()
        self.assertEqual(run.call_count, 1)
        self.assertNotIn('secret-path', ' '.join(run.call_args.args[0]))
        self.assertIn('secret-path', run.call_args.kwargs['input'])
        self.assertIn('paused', run.call_args.kwargs['input'])


class OutputTests(unittest.TestCase):
    def test_status_is_readable(self):
        now = 1_800_000_000
        text = '\n'.join(app.status_lines({'attempts': 7, 'last_http': 503, 'next_attempt_at': now + 300}, now))
        self.assertIn('waiting for capacity', text)
        self.assertIn('(in 5 min)', text)
        self.assertNotIn(str(now + 300), text)
        self.assertIn('not started', app.status_lines({})[0])

    def test_locations_sorted_open_first(self):
        rows = app.location_rows(FakeApi().locations[::-1])
        self.assertEqual([key for key, _ in rows], ['sg-sgp', 'de-fra'])
        self.assertTrue(rows[1][1].endswith('paused'))

    def test_templates_hide_non_sandbox_by_default(self):
        lines = app.template_lines(FakeApi().eggs, show_all=False)
        self.assertIn('Ubuntu', lines[0])
        self.assertIn('1 game and app templates hidden', lines[1])
        self.assertEqual(len(app.template_lines(FakeApi().eggs, show_all=True)), 2)

    def test_resource_table(self):
        lines = app.resource_table(RESOURCES)
        self.assertEqual(lines[1].split()[-3:], ['1024', '3072', '4096'])
        self.assertEqual(lines[4].split()[-3:], ['1', '1', '2'])

    def test_no_command_prints_help(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(app.main([]), 0)
        self.assertIn('setup', out.getvalue())


class ConfigurationTests(unittest.TestCase):
    def test_dotenv_does_not_execute_shell_and_preserves_environment(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'EXISTING': 'keep'}, clear=True):
            path = Path(folder) / '.env'
            path.write_text("EXISTING=replace\nPASSWORD='a # b $HOME'\nLITERAL='$(echo no)'\n")
            app.load_env(path)
            self.assertEqual(os.environ['EXISTING'], 'keep')
            self.assertEqual(os.environ['PASSWORD'], 'a # b $HOME')
            self.assertEqual(os.environ['LITERAL'], '$(echo no)')

    def test_write_env_round_trips_and_keeps_other_lines(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {}, clear=True):
            path = Path(folder) / '.env'
            path.write_text('# comment\nOTHER=1\nALTARE_TOKEN=old\n')
            app.write_env(path, {'ALTARE_TOKEN': "n'ew # $x", 'ALTARE_EGG_ID': '45'})
            text = path.read_text()
            self.assertTrue(text.startswith('# comment\nOTHER=1\n'))
            self.assertEqual(oct(path.stat().st_mode & 0o777), '0o600')
            app.load_env(path)
            self.assertEqual(os.environ['ALTARE_TOKEN'], "n'ew # $x")
            self.assertEqual(os.environ['ALTARE_EGG_ID'], '45')

    def test_missing_config_fails_before_api_call(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(app.ProvisionError):
                app.configuration()

    def test_missing_token_explains_lookup(self):
        with patch.dict(os.environ, {'ALTARE_TENANT_ID': TENANT}, clear=True):
            with self.assertRaises(app.ProvisionError) as caught:
                app.configuration(create=False)
        message = str(caught.exception)
        self.assertIn('altare_session', message)
        self.assertNotIn('altare_selected_tenant_id', message)

    def test_queries_need_only_token_and_tenant(self):
        with patch.dict(os.environ, {'ALTARE_TOKEN': 'test', 'ALTARE_TENANT_ID': TENANT}, clear=True):
            self.assertEqual(app.configuration(create=False)['eggId'], 0)
            with self.assertRaises(app.ProvisionError):
                app.configuration()

    def test_short_interval_refused(self):
        env = {'ALTARE_TOKEN': 'test', 'ALTARE_TENANT_ID': TENANT, 'ALTARE_SSH_PASSWORD': 'test',
               'ALTARE_EGG_ID': '1', 'ALTARE_INTERVAL_SECONDS': '10'}
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaises(app.ProvisionError):
                app.configuration()


class TokenRenewalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.env_file = self.folder / '.env'
        self.env_file.write_text('ALTARE_TOKEN=expired\n')
        self.config = {
            'token': 'expired', 'tenant': TENANT, 'password': 'pw', 'name': 'example-server', 'eggId': 45,
            'memoryMb': 4096, 'cpuPercent': 100, 'diskMb': 10240, 'location': 'sg-sgp', 'interval': 600,
            'state_dir': self.folder / 'state', 'webhook': None,
        }
        self.api = FakeApi()
        self.quiet = [redirect_stdout(io.StringIO()), patch('sys.stderr', io.StringIO())]
        for context in self.quiet:
            context.__enter__()

    def tearDown(self):
        for context in reversed(self.quiet):
            context.__exit__(None, None, None)
        self.temp.cleanup()

    def test_loop_waits_for_new_token_and_continues(self):
        self.api.locations_code = self.api.list_code = 401
        polls = []

        def sleep(seconds):
            polls.append(seconds)
            if len(polls) == 2:
                self.env_file.write_text('ALTARE_TOKEN=fresh\n')
                self.api.locations_code = self.api.list_code = 200
                self.api.create = [(201, {'id': 'server'})]

        args = app.argparse.Namespace(command='run', loop=True, env_file=self.env_file)
        with patch.object(app.time, 'sleep', sleep):
            self.assertEqual(app.run_locked(args, self.config, self.api), 0)
        self.assertEqual(self.config['token'], 'fresh')
        self.assertEqual(polls, [app.TOKEN_POLL_SECONDS] * 2)
        state = app.json.loads((self.config['state_dir'] / 'state.json').read_text())
        self.assertTrue(state['completed'])

    def test_loop_accepts_token_fixed_before_start(self):
        self.api.create = [(201, {'id': 'server'})]
        runner = app.Provisioner(self.config, self.api)
        runner.state['paused'] = 'http_401'
        runner.save()
        args = app.argparse.Namespace(command='run', loop=True, env_file=self.env_file)
        with patch.object(app.time, 'sleep', lambda _: self.fail('should not wait')):
            self.assertEqual(app.run_locked(args, self.config, self.api), 0)

    def test_run_without_loop_stops_on_expired_session(self):
        self.api.locations_code = 401
        args = app.argparse.Namespace(command='run', loop=False, env_file=self.env_file)
        with self.assertRaises(app.ProvisionError):
            app.run_locked(args, self.config, self.api)

    def test_token_command_writes_and_resumes(self):
        state_dir = self.config['state_dir']
        env = {'ALTARE_TOKEN': 'expired', 'ALTARE_TENANT_ID': TENANT, 'ALTARE_SSH_PASSWORD': 'pw', 'ALTARE_EGG_ID': '45',
               'ALTARE_SERVER_NAME': 'example-server', 'ALTARE_STATE_DIR': str(state_dir)}
        with patch.dict(os.environ, env, clear=True), patch.object(app, 'check_session', lambda t, _: 200), \
                patch.object(app.getpass, 'getpass', lambda _: 'fresh'):
            runner = app.Provisioner(app.configuration(), self.api)
            runner.state['paused'] = 'http_401'
            runner.save()
            app.update_token(self.env_file)
            self.assertEqual(app.read_env(self.env_file)['ALTARE_TOKEN'], 'fresh')
            state = app.json.loads((state_dir / 'state.json').read_text())
            self.assertNotIn('paused', state)

    def test_token_command_rejects_bad_token(self):
        with patch.dict(os.environ, {'ALTARE_TENANT_ID': TENANT}, clear=True), \
                patch.object(app, 'check_session', lambda t, _: 401), \
                patch.object(app.getpass, 'getpass', lambda _: 'bad'):
            with self.assertRaises(app.ProvisionError):
                app.update_token(self.env_file)
        self.assertEqual(app.read_env(self.env_file)['ALTARE_TOKEN'], 'expired')


class SetupTests(unittest.TestCase):
    def test_setup_writes_env(self):
        answers = iter([TENANT, '2', '45', '', '', '', 'box', '', ''])
        secrets = iter(['session-value', 'pw', 'pw'])
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {}, clear=True), \
                patch.object(app, 'Api', lambda config: FakeApi()), \
                patch('builtins.input', lambda _: next(answers)), \
                patch.object(app.getpass, 'getpass', lambda _: next(secrets)), \
                redirect_stdout(io.StringIO()):
            path = Path(folder) / '.env'
            app.setup(path)
            app.load_env(path)
            self.assertEqual(os.environ['ALTARE_TOKEN'], 'session-value')
            self.assertEqual(os.environ['ALTARE_LOCATION'], 'de-fra')
            self.assertEqual(os.environ['ALTARE_EGG_ID'], '45')
            self.assertEqual(os.environ['ALTARE_MEMORY_MB'], '3072')
            self.assertEqual(os.environ['ALTARE_CPU_PERCENT'], '100')
            self.assertEqual(os.environ['ALTARE_SERVER_NAME'], 'box')
            self.assertEqual(os.environ['ALTARE_SSH_PASSWORD'], 'pw')


if __name__ == '__main__':
    unittest.main()
