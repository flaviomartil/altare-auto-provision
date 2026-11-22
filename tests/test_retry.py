import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch


SPEC = importlib.util.spec_from_file_location('altare_retry', Path(__file__).parents[1] / 'altare_retry.py')
app = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(app)


class ProvisionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = {
            'token': 'test-token', 'tenant': '00000000-0000-4000-8000-000000000001',
            'password': 'test-password', 'name': 'example-server', 'eggId': 1,
            'memoryMb': 4096, 'cpuPercent': 100, 'diskMb': 10240, 'location': 'sg-sgp',
            'interval': 600, 'state_dir': Path(self.temp.name),
        }
        self.api = Mock(cooldown=0)
        self.runner = app.Provisioner(self.config, self.api)

    def tearDown(self):
        self.temp.cleanup()

    def test_capacity_failure_retries_after_interval(self):
        self.api.call.side_effect = [(200, {'items': []}), (422, {})]
        self.assertEqual(self.runner.tick(), 'wait')
        self.assertNotIn('pending_key', self.runner.state)
        self.runner.tick()
        self.assertEqual(self.api.call.call_count, 2)

    def test_success_persisted_and_never_repeats(self):
        self.api.call.side_effect = [(200, {'items': []}), (200, {'id': 'new-server'})]
        self.assertEqual(self.runner.tick(), 'done')
        loaded = app.Provisioner(self.config, self.api)
        self.assertEqual(loaded.tick(), 'done')
        self.assertEqual(self.api.call.call_count, 2)
        saved = self.runner.path.read_text()
        self.assertNotIn('test-token', saved)
        self.assertNotIn('test-password', saved)

    def test_uncertain_response_reuses_idempotency_key(self):
        self.api.call.side_effect = [(200, {'items': []}), (0, {})]
        self.runner.tick()
        key = self.runner.state['pending_key']
        self.runner.state['next_attempt_at'] = 0
        self.api.call.side_effect = [(200, {'items': []}), (0, {})]
        self.runner.tick()
        self.assertEqual(self.api.call.call_args.args[2], key)

    def test_pending_server_reconciled_without_post(self):
        self.runner.state['pending_key'] = 'previous-attempt'
        server = {k: self.config[k] for k in ['name', 'memoryMb', 'cpuPercent', 'diskMb', 'eggId']}
        server['id'] = 'existing-server'
        self.api.call.return_value = (200, {'items': [server]})
        self.assertEqual(self.runner.tick(), 'done')
        self.assertEqual(self.api.call.call_count, 1)

    def test_unrelated_existing_server_pauses(self):
        self.api.call.return_value = (200, {'items': [{'name': 'example-server', 'id': 'existing'}]})
        with self.assertRaises(app.ProvisionError):
            self.runner.tick()
        self.assertEqual(self.runner.state['paused'], 'name_already_exists')
        self.assertEqual(self.api.call.call_count, 1)

    def test_authentication_failure_stops_retries(self):
        self.api.call.return_value = (401, {})
        with self.assertRaises(app.ProvisionError):
            self.runner.tick()
        with self.assertRaises(app.ProvisionError):
            self.runner.tick()
        self.assertEqual(self.api.call.call_count, 1)

    def test_changed_target_refused(self):
        self.runner.save()
        with self.assertRaises(app.ProvisionError):
            app.Provisioner({**self.config, 'location': 'other-region'}, self.api)

    def test_server_failure_checks_for_creation(self):
        self.api.call.side_effect = [(200, {'items': []}), (500, {}), (200, {'items': []})]
        self.runner.tick()
        self.assertNotIn('pending_key', self.runner.state)
        self.assertEqual(self.api.call.call_count, 3)

    def test_rate_limit_honors_server_cooldown(self):
        self.api.cooldown = app.time.time() + 7200
        self.api.call.side_effect = [(200, {'items': []}), (429, {})]
        self.runner.tick()
        self.assertGreaterEqual(self.runner.state['next_attempt_at'], self.api.cooldown)

    def test_credentials_only_sent_to_api(self):
        self.api.call.side_effect = [(200, {'items': []}), (201, {'id': 'server'})]
        self.runner.tick()
        payload = self.api.call.call_args.args[1]
        self.assertEqual(payload['ssh']['password'], 'test-password')
        self.assertEqual(payload['location'], 'sg-sgp')
        events = (self.config['state_dir'] / 'events.jsonl').read_text()
        self.assertNotIn('test-password', events)
        self.assertNotIn('test-token', events)


class ConfigurationTests(unittest.TestCase):
    def test_dotenv_does_not_execute_shell_and_preserves_environment(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'EXISTING': 'keep'}, clear=True):
            path = Path(folder) / '.env'
            path.write_text("EXISTING=replace\nPASSWORD='a # b $HOME'\nLITERAL='$(echo no)'\n")
            app.load_env(path)
            self.assertEqual(os.environ['EXISTING'], 'keep')
            self.assertEqual(os.environ['PASSWORD'], 'a # b $HOME')
            self.assertEqual(os.environ['LITERAL'], '$(echo no)')

    def test_missing_config_fails_before_api_call(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(app.ProvisionError):
                app.configuration()

    def test_short_interval_refused(self):
        env = {'ALTARE_TOKEN': 'test', 'ALTARE_TENANT_ID': '00000000-0000-4000-8000-000000000001',
               'ALTARE_SSH_PASSWORD': 'test', 'ALTARE_EGG_ID': '1', 'ALTARE_INTERVAL_SECONDS': '10'}
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaises(app.ProvisionError):
                app.configuration()


if __name__ == '__main__':
    unittest.main()
