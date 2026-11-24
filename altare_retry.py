import argparse
import datetime
from email.utils import parsedate_to_datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from urllib.parse import quote
import uuid


API_BASE = 'https://console.altare.gg'
CREATE_FIELDS = ('name', 'eggId', 'memoryMb', 'cpuPercent', 'diskMb', 'location')
SIZE_FIELDS = ('eggId', 'memoryMb', 'cpuPercent', 'diskMb')
FATAL_HTTP = (400, 401, 403, 404, 405, 409)


class ProvisionError(Exception):
    pass


def load_env(path):
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        words = shlex.split(line, comments=True)
        if words and words[0] == 'export':
            words = words[1:]
        if not words:
            continue
        if len(words) != 1 or '=' not in words[0]:
            raise ProvisionError('Invalid line in environment file.')
        key, value = words[0].split('=', 1)
        os.environ.setdefault(key, value)


def configuration():
    required = ['ALTARE_TOKEN', 'ALTARE_TENANT_ID', 'ALTARE_SSH_PASSWORD', 'ALTARE_EGG_ID']
    missing = [key for key in required if not os.environ.get(key)]
    if missing:
        raise ProvisionError('Missing settings: ' + ', '.join(missing))
    env = os.environ.get
    try:
        config = {
            'token': env('ALTARE_TOKEN'),
            'tenant': str(uuid.UUID(env('ALTARE_TENANT_ID'))),
            'password': env('ALTARE_SSH_PASSWORD'),
            'name': env('ALTARE_SERVER_NAME', 'my-server').strip(),
            'eggId': int(env('ALTARE_EGG_ID')),
            'memoryMb': int(env('ALTARE_MEMORY_MB', '4096')),
            'cpuPercent': int(env('ALTARE_CPU_PERCENT', '100')),
            'diskMb': int(env('ALTARE_DISK_MB', '10240')),
            'location': env('ALTARE_LOCATION', 'sg-sgp').strip(),
            'interval': int(env('ALTARE_INTERVAL_SECONDS', '600')),
            'state_dir': Path(env('ALTARE_STATE_DIR', '.state')).expanduser().resolve(),
        }
    except ValueError:
        raise ProvisionError('Invalid tenant UUID or numeric value.') from None
    if any(config[k] <= 0 for k in SIZE_FIELDS):
        raise ProvisionError('Template, RAM, CPU and disk must be positive.')
    if config['interval'] < 600:
        raise ProvisionError('Use an interval of at least 600 seconds.')
    if not config['name'] or not config['location']:
        raise ProvisionError('Server name and location are required.')
    if not shutil.which('curl'):
        raise ProvisionError('Install curl before continuing.')
    return config


def tenant_path(config, suffix):
    return '/api/tenants/' + quote(config['tenant'], safe='') + suffix


def retry_after_seconds(value, fallback):
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return int(parsedate_to_datetime(value).timestamp() - time.time())
    except (ValueError, TypeError, OverflowError):
        return fallback


def local_time(timestamp):
    return datetime.datetime.fromtimestamp(timestamp).astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')


def duration(seconds):
    minutes = round(seconds / 60)
    return f'{minutes} min' if minutes < 120 else f'{minutes / 60:.1f} h'


def status_lines(state, now=None):
    now = time.time() if now is None else now
    if not state:
        phase = 'not started'
    elif state.get('completed'):
        phase = 'completed'
    elif state.get('paused'):
        phase = 'paused (' + state['paused'] + '), run --resume after fixing the cause'
    else:
        phase = 'waiting for capacity'
    rows = [('Status', phase)]
    if 'server_id' in state:
        rows.append(('Server', API_BASE + '/server/' + str(state['server_id'])))
    rows.append(('Attempts', str(state.get('attempts', 0))))
    if 'last_http' in state:
        rows.append(('Last HTTP', str(state['last_http'])))
    due = state.get('next_attempt_at')
    if due and not state.get('completed') and not state.get('paused'):
        when = 'in ' + duration(due - now) if due > now else 'now'
        rows.append(('Next attempt', f'{local_time(due)} ({when})'))
    if state.get('pending_key'):
        rows.append(('Unconfirmed', 'the last attempt may have created the server, the next run checks'))
    width = max(len(label) for label, _ in rows) + 1
    return [f'{label + ":":<{width}} {value}' for label, value in rows]


class Api:
    def __init__(self, config):
        self.config = config
        self.cooldown = 0

    def call(self, path, body=None, key=None):
        # Options go through stdin so the token never shows up in the process list.
        headers = {'Authorization': 'Bearer ' + self.config['token'], 'Content-Type': 'application/json'}
        if key:
            headers['Idempotency-Key'] = key
        lines = ['url = ' + json.dumps(API_BASE + path)]
        lines += ['header = ' + json.dumps(k + ': ' + v) for k, v in headers.items()]
        if body is not None:
            lines.append('data = ' + json.dumps(json.dumps(body)))
        with tempfile.TemporaryDirectory() as directory:
            header_file = Path(directory) / 'headers'
            try:
                result = subprocess.run(
                    ['curl', '--config', '-', '--silent', '--show-error', '--connect-timeout', '10',
                     '--max-time', '45', '--user-agent', 'Mozilla/5.0', '--dump-header', str(header_file),
                     '--write-out', '\nHTTP:%{http_code}'],
                    input='\n'.join(lines) + '\n', capture_output=True, text=True, timeout=55,
                )
            except subprocess.TimeoutExpired:
                return 0, {}
            if header_file.exists():
                self._note_retry_after(header_file.read_text())
        raw, _, code = result.stdout.rpartition('\nHTTP:')
        try:
            data = json.loads(raw)
        except ValueError:
            data = None
        return (int(code) if code.strip().isdigit() else 0), (data if isinstance(data, dict) else {})

    def _note_retry_after(self, headers):
        for line in headers.splitlines():
            name, _, value = line.partition(':')
            if name.strip().lower() == 'retry-after':
                delay = retry_after_seconds(value.strip(), self.config['interval'])
                self.cooldown = max(self.cooldown, time.time() + max(0, delay))


class Provisioner:
    def __init__(self, config, api):
        self.config = config
        self.api = api
        self.directory = config['state_dir']
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.directory / 'state.json'
        self.servers = tenant_path(config, '/servers')
        target = {k: config[k] for k in ('tenant',) + CREATE_FIELDS}
        self.fingerprint = hashlib.sha256(json.dumps(target, sort_keys=True).encode()).hexdigest()
        self.state = json.loads(self.path.read_text()) if self.path.exists() else {}
        if self.state.setdefault('target', self.fingerprint) != self.fingerprint:
            self.reset('config_changed')

    def save(self):
        temp = self.path.with_suffix('.tmp')
        temp.write_text(json.dumps(self.state, indent=2))
        temp.chmod(0o600)
        temp.replace(self.path)

    def event(self, name, **values):
        entry = {'at': datetime.datetime.now(datetime.timezone.utc).isoformat(), 'event': name, **values}
        line = json.dumps(entry)
        with (self.directory / 'events.jsonl').open('a') as handle:
            handle.write(line + '\n')
        print(line, flush=True)

    def pause(self, reason, message):
        self.state['paused'] = reason
        self.save()
        raise ProvisionError(message)

    def named(self, listing):
        return [s for s in listing.get('items') or [] if s.get('name') == self.config['name']]

    def list_servers(self):
        code, listing = self.api.call(self.servers)
        ok = code == 200 and isinstance(listing.get('items'), list)
        return code, (listing if ok else None)

    def complete(self, server):
        self.state.update(server_id=server['id'], completed=True)
        for key in ('pending_key', 'retire_key', 'paused'):
            self.state.pop(key, None)
        self.save()
        self.event('created', panel=API_BASE + '/server/' + str(server['id']))
        return 'done'

    def resume(self):
        if self.state.pop('paused', None):
            self.state['next_attempt_at'] = 0
            self.save()
            self.event('resumed')

    def reset(self, reason='reset'):
        # Safe without an API call: tick() pauses instead of creating a second server with the same name.
        unconfirmed = bool(self.state.get('pending_key'))
        self.state = {'target': self.fingerprint}
        self.save()
        self.event(reason, **({'warning': 'last attempt was unconfirmed, check the panel'} if unconfirmed else {}))

    def tick(self):
        if self.state.get('completed'):
            return 'done'
        if self.state.get('paused'):
            raise ProvisionError('Routine is paused. Check --status, then run --resume.')
        if time.time() < self.state.get('next_attempt_at', 0):
            return 'wait'

        code, listing = self.list_servers()
        if listing is None:
            return self.failure(code, 'list_failed')
        matches = self.named(listing)
        if matches:
            # A previous attempt may have created the server even though the response was lost.
            if self.state.get('pending_key') and len(matches) == 1 and \
                    all(matches[0].get(k) == self.config[k] for k in SIZE_FIELDS):
                return self.complete(matches[0])
            self.pause('name_already_exists', 'A server with this name already exists. Check the panel first.')
        if self.state.pop('retire_key', False):
            # The last attempt failed on the provider side and the listing shows no server.
            self.state.pop('pending_key', None)

        self.state.setdefault('pending_key', str(uuid.uuid4()))
        self.state['attempts'] = self.state.get('attempts', 0) + 1
        self.state['next_attempt_at'] = time.time() + self.config['interval']
        self.save()
        payload = {k: self.config[k] for k in CREATE_FIELDS}
        payload['ssh'] = {'password': self.config['password']}
        code, response = self.api.call(self.servers, payload, self.state['pending_key'])
        if 200 <= code < 300 and response.get('id'):
            return self.complete(response)
        if code in (422, 429):
            self.state.pop('pending_key', None)
        elif code >= 500:
            # Keep the key until the next listing confirms that nothing was created.
            self.state['retire_key'] = True
        return self.failure(code, 'creation_failed')

    def failure(self, code, event):
        self.state['last_http'] = code
        self.state['next_attempt_at'] = max(time.time() + self.config['interval'], self.api.cooldown)
        self.event(event, http=code, attempts=self.state.get('attempts', 0))
        if code in FATAL_HTTP:
            self.pause('http_' + str(code), 'Request rejected. Check authentication, configuration and permissions.')
        self.save()
        return 'wait'


def query(api, paths):
    for path in paths:
        code, data = api.call(path)
        if code != 200:
            raise ProvisionError('Query failed. HTTP ' + str(code))
        print(json.dumps(data, indent=2, ensure_ascii=False))


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description='Retries creating an Altare server until capacity is available.')
    parser.add_argument('--env-file', type=Path, default=Path('.env'))
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--loop', action='store_true', help='Repeat every interval until the server is created.')
    mode.add_argument('--status', action='store_true', help='Show the local state only.')
    mode.add_argument('--check', action='store_true', help='Query tenant resources without creating a server.')
    mode.add_argument('--list-options', action='store_true', help='List locations and templates without creating.')
    mode.add_argument('--resume', action='store_true', help='Lift a pause and retry on the next run.')
    mode.add_argument('--reset', action='store_true', help='Discard the local state and start over.')
    args = parser.parse_args()
    try:
        load_env(args.env_file)
        config = configuration()
        state_dir = config['state_dir']
        state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if args.status:
            state_file = state_dir / 'state.json'
            print('\n'.join(status_lines(json.loads(state_file.read_text()) if state_file.exists() else {})))
            return 0
        with (state_dir / 'run.lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                print('Another run is already active. Stop it first.')
                return 0
            api = Api(config)
            if args.list_options:
                query(api, ['/api/core/locations', '/api/core/eggs'])
                return 0
            if args.check:
                query(api, [tenant_path(config, '/resources')])
                return 0
            runner = Provisioner(config, api)
            if args.resume:
                runner.resume()
                return 0
            if args.reset:
                runner.reset()
                return 0
            while runner.tick() != 'done' and args.loop:
                time.sleep(max(config['interval'], runner.state.get('next_attempt_at', 0) - time.time()))
            return 0
    except ProvisionError as error:
        print(str(error), file=sys.stderr)
        return 2
    except (OSError, ValueError):
        print('Configuration, authentication or state error. Check .env, --status and the README.', file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == '__main__':
    sys.exit(main())
