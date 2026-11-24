import argparse
import datetime
from email.utils import parsedate_to_datetime
import fcntl
import getpass
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
LOCATIONS_PATH = '/api/core/locations'
EGGS_PATH = '/api/core/eggs'
CREATE_FIELDS = ('name', 'eggId', 'memoryMb', 'cpuPercent', 'diskMb', 'location')
SIZE_FIELDS = ('eggId', 'memoryMb', 'cpuPercent', 'diskMb')
FATAL_HTTP = (400, 401, 403, 404, 405, 409)
TOKEN_POLL_SECONDS = 60
NOTIFY_EVENTS = ('created', 'paused')
RESOURCE_ROWS = (('Memory (MB)', 'memoryMb', 'memoryMb'), ('Disk (MB)', 'diskMb', 'diskMb'),
                 ('CPU (%)', 'cpuPercent', 'cpuPercent'), ('Server slots', 'servers', 'serverSlots'))

LOOKUP_HELP = {
    'ALTARE_TOKEN': 'In DevTools open Application (Chrome) or Storage (Firefox) > Cookies > '
                    'https://console.altare.gg and copy the value of "altare_session".',
    'ALTARE_TENANT_ID': "In the DevTools console run: copy(localStorage.getItem('altare_selected_tenant_id'))\n"
                        '    Chrome asks you to type "allow pasting" first. This is the team selected in the console.',
    'ALTARE_EGG_ID': "Run 'options' and pick a template id, or run 'setup'.",
}


class ProvisionError(Exception):
    pass


# Configuration

def read_env(path):
    values = {}
    if not path.exists():
        return values
    for line in path.read_text().splitlines():
        words = shlex.split(line, comments=True)
        if words and words[0] == 'export':
            words = words[1:]
        if not words:
            continue
        if len(words) != 1 or '=' not in words[0]:
            raise ProvisionError('Invalid line in environment file.')
        key, value = words[0].split('=', 1)
        values[key] = value
    return values


def load_env(path):
    for key, value in read_env(path).items():
        os.environ.setdefault(key, value)


def write_env(path, values):
    """Update the given keys in place and keep every other line of the file."""
    lines = path.read_text().splitlines() if path.exists() else []
    pending = dict(values)
    for index, line in enumerate(lines):
        key = line.strip().removeprefix('export ').split('=', 1)[0].strip()
        if key in pending:
            lines[index] = f'{key}={shlex.quote(pending.pop(key))}'
    lines += [f'{key}={shlex.quote(value)}' for key, value in pending.items()]
    temp = path.with_name(path.name + '.tmp')
    temp.write_text('\n'.join(lines) + '\n')
    temp.chmod(0o600)
    temp.replace(path)


def lookup_help(keys):
    lines = ['Sign in at ' + API_BASE + ', then:']
    lines += [f'  {key}\n    {LOOKUP_HELP[key]}' for key in keys if key in LOOKUP_HELP]
    return '\n'.join(lines) if len(lines) > 1 else ''


def configuration(create=True):
    required = ['ALTARE_TOKEN', 'ALTARE_TENANT_ID']
    if create:
        required += ['ALTARE_SSH_PASSWORD', 'ALTARE_EGG_ID']
    missing = [key for key in required if not os.environ.get(key)]
    if missing:
        raise ProvisionError('\n\n'.join(filter(None, [
            'Missing settings: ' + ', '.join(missing), lookup_help(missing), "Or run 'setup'."])))
    env = os.environ.get
    try:
        config = {
            'token': env('ALTARE_TOKEN'),
            'tenant': str(uuid.UUID(env('ALTARE_TENANT_ID'))),
            'password': env('ALTARE_SSH_PASSWORD'),
            'name': env('ALTARE_SERVER_NAME', 'my-server').strip(),
            'eggId': int(env('ALTARE_EGG_ID') or 0),
            'memoryMb': int(env('ALTARE_MEMORY_MB', '4096')),
            'cpuPercent': int(env('ALTARE_CPU_PERCENT', '100')),
            'diskMb': int(env('ALTARE_DISK_MB', '10240')),
            'location': env('ALTARE_LOCATION', 'sg-sgp').strip(),
            'interval': int(env('ALTARE_INTERVAL_SECONDS', '600')),
            'state_dir': Path(env('ALTARE_STATE_DIR', '.state')).expanduser().resolve(),
            'webhook': env('ALTARE_WEBHOOK_URL', '').strip() or None,
        }
    except ValueError:
        raise ProvisionError('Invalid tenant UUID or numeric value.') from None
    if create and any(config[k] <= 0 for k in SIZE_FIELDS):
        raise ProvisionError('Template, RAM, CPU and disk must be positive.')
    if config['interval'] < 600:
        raise ProvisionError('Use an interval of at least 600 seconds.')
    if not config['name'] or not config['location']:
        raise ProvisionError('Server name and location are required.')
    if config['webhook'] and not config['webhook'].startswith(('https://', 'http://')):
        raise ProvisionError('ALTARE_WEBHOOK_URL must start with https:// or http://.')
    if not shutil.which('curl'):
        raise ProvisionError('Install curl before continuing.')
    return config


def tenant_path(config, suffix):
    return '/api/tenants/' + quote(config['tenant'], safe='') + suffix


# HTTP

def curl(url, headers, body=None, header_file=None):
    # Options go through stdin so tokens and webhook URLs never show up in the process list.
    lines = ['url = ' + json.dumps(url)] + ['header = ' + json.dumps(h) for h in headers]
    if body is not None:
        lines.append('data = ' + json.dumps(json.dumps(body)))
    command = ['curl', '--config', '-', '--silent', '--show-error', '--connect-timeout', '10',
               '--max-time', '45', '--user-agent', 'Mozilla/5.0', '--write-out', '\nHTTP:%{http_code}']
    if header_file:
        command += ['--dump-header', str(header_file)]
    try:
        result = subprocess.run(command, input='\n'.join(lines) + '\n', capture_output=True, text=True, timeout=55)
    except subprocess.TimeoutExpired:
        return 0, ''
    raw, _, code = result.stdout.rpartition('\nHTTP:')
    return (int(code) if code.strip().isdigit() else 0), raw


def retry_after_seconds(value, fallback):
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return int(parsedate_to_datetime(value).timestamp() - time.time())
    except (ValueError, TypeError, OverflowError):
        return fallback


class Api:
    def __init__(self, config):
        self.config = config
        self.cooldown = 0

    def call(self, path, body=None, key=None):
        headers = ['Authorization: Bearer ' + self.config['token'], 'Content-Type: application/json']
        if key:
            headers.append('Idempotency-Key: ' + key)
        with tempfile.TemporaryDirectory() as directory:
            header_file = Path(directory) / 'headers'
            code, raw = curl(API_BASE + path, headers, body, header_file)
            if header_file.exists():
                self._note_retry_after(header_file.read_text())
        try:
            data = json.loads(raw)
        except ValueError:
            data = None
        return code, (data if isinstance(data, dict) else {})

    def items(self, path):
        code, data = self.call(path)
        items = data.get('items')
        return code, (items if code == 200 and isinstance(items, list) else None)

    def _note_retry_after(self, headers):
        for line in headers.splitlines():
            name, _, value = line.partition(':')
            if name.strip().lower() == 'retry-after':
                delay = retry_after_seconds(value.strip(), self.config['interval'])
                self.cooldown = max(self.cooldown, time.time() + max(0, delay))


def check_session(token, tenant):
    code, _ = Api({'token': token, 'interval': 600}).call(tenant_path({'tenant': tenant}, '/resources'))
    return code


def send_webhook(url, entry):
    if entry['event'] == 'created':
        text = 'Altare server created: ' + entry.get('panel', '')
    else:
        text = 'Altare provisioning paused: ' + entry.get('message', entry.get('reason', ''))
    code, _ = curl(url, ['Content-Type: application/json'], {'text': text, **entry})
    if not 200 <= code < 300:
        print(f'Webhook failed with HTTP {code}.', file=sys.stderr)


# Output

def local_time(timestamp):
    return datetime.datetime.fromtimestamp(timestamp).astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')


def duration(seconds):
    minutes = round(seconds / 60)
    return f'{minutes} min' if minutes < 120 else f'{minutes / 60:.1f} h'


def status_lines(state, now=None, location=None):
    now = time.time() if now is None else now
    if not state:
        phase = 'not started'
    elif state.get('completed'):
        phase = 'completed'
    elif state.get('paused'):
        phase = 'paused (' + state['paused'] + "), run 'resume' after fixing the cause"
    elif state.get('region_paused'):
        phase = 'waiting, ' + (f'location {location}' if location else 'the location') + \
            ' does not accept new servers right now'
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
        rows.append(('Next check', f'{local_time(due)} ({when})'))
    if state.get('pending_key'):
        rows.append(('Unconfirmed', 'the last attempt may have created the server, the next run checks'))
    width = max(len(label) for label, _ in rows) + 1
    return [f'{label + ":":<{width}} {value}' for label, value in rows]


def location_status(location):
    if location.get('creationPaused'):
        return 'paused'
    return 'Plus only' if location.get('plusOnly') else 'open'


def location_rows(locations):
    order = {'open': 0, 'Plus only': 1, 'paused': 2}
    ordered = sorted(locations, key=lambda l: (order[location_status(l)], str(l.get('slug'))))
    return [(str(l.get('slug')), f"{l.get('slug', ''):<8} {l.get('name', ''):<28} {location_status(l)}")
            for l in ordered]


def template_lines(eggs, show_all):
    shown = [e for e in eggs if show_all or e.get('sandbox')]
    lines = [f"{e.get('eggId', ''):>4}  {e.get('name', '')}"
             for e in sorted(shown, key=lambda e: e.get('eggId') or 0)]
    hidden = len(eggs) - len(shown)
    if hidden:
        lines.append(f'      ({hidden} game and app templates hidden, show them with --all)')
    return lines


def resource_table(data):
    package, extra, used, remaining = (data.get(k) or {} for k in ('package', 'extra', 'used', 'remaining'))
    lines = [f"{'':<14}{'used':>8}{'free':>8}{'total':>8}"]
    for label, used_key, key in RESOURCE_ROWS:
        total = (package.get(key) or 0) + (extra.get(key) or 0)
        lines.append(f'{label:<14}{used.get(used_key, 0):>8}{remaining.get(key, 0):>8}{total:>8}')
    return lines


# Provisioning

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
        if name in NOTIFY_EVENTS and self.config.get('webhook'):
            send_webhook(self.config['webhook'], entry)

    def pause(self, reason, message):
        self.state['paused'] = reason
        self.save()
        self.event('paused', reason=reason, message=message.split('\n\n')[0])
        raise ProvisionError(message)

    def named(self, servers):
        return [s for s in servers if s.get('name') == self.config['name']]

    def schedule_next(self):
        self.state['next_attempt_at'] = max(time.time() + self.config['interval'], self.api.cooldown)

    def complete(self, server):
        self.state.update(server_id=server['id'], completed=True)
        for key in ('pending_key', 'retire_key', 'paused', 'region_paused'):
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

    def wait_for_region(self):
        self.schedule_next()
        self.save()
        self.event('region_paused', location=self.config['location'])
        return 'wait'

    def tick(self):
        if self.state.get('completed'):
            return 'done'
        if self.state.get('paused'):
            raise ProvisionError("Routine is paused. Check 'status', then run 'resume'.")
        if time.time() < self.state.get('next_attempt_at', 0):
            return 'wait'

        code, locations = self.api.items(LOCATIONS_PATH)
        if locations is None:
            return self.failure(code, 'locations_failed')
        location = next((l for l in locations if l.get('slug') == self.config['location']), None)
        if location is None:
            self.pause('unknown_location', f"Location {self.config['location']} does not exist. "
                                           "Run 'options' to list the locations.")
        region_open = not location.get('creationPaused')
        self.state['region_paused'] = not region_open
        if not region_open and not self.state.get('pending_key'):
            return self.wait_for_region()

        code, servers = self.api.items(self.servers)
        if servers is None:
            return self.failure(code, 'list_failed')
        matches = self.named(servers)
        if matches:
            # A previous attempt may have created the server even though the response was lost.
            if self.state.get('pending_key') and len(matches) == 1 and \
                    all(matches[0].get(k) == self.config[k] for k in SIZE_FIELDS):
                return self.complete(matches[0])
            self.pause('name_already_exists', 'A server with this name already exists. Check the panel first.')
        if self.state.pop('retire_key', False):
            # The last attempt failed on the provider side and the listing shows no server.
            self.state.pop('pending_key', None)
        if not region_open:
            return self.wait_for_region()

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
        self.schedule_next()
        self.save()
        self.event(event, http=code, attempts=self.state.get('attempts', 0))
        if code == 401:
            self.pause('http_401', "The session has expired or the token is wrong. Run 'token' to store a "
                                   "new one. A running loop continues on its own.\n\n" + lookup_help(['ALTARE_TOKEN']))
        if code in FATAL_HTTP:
            self.pause('http_' + str(code), 'Request rejected. Check authentication, configuration and permissions.')
        return 'wait'


# Interactive setup

def prompt(label, default='', secret=False, check=None, required=True):
    hint = (' [keep current]' if secret else f' [{default}]') if default else ''
    while True:
        value = (getpass.getpass if secret else input)(f'{label}{hint}: ').strip() or default
        error = check(value) if value and check else None
        if not value and required:
            error = 'A value is required.'
        if not error:
            return value
        print('  ' + error)


def whole_number(value):
    return None if value.isdigit() and int(value) > 0 else 'Enter a positive whole number.'


def valid_uuid(value):
    try:
        uuid.UUID(value)
    except ValueError:
        return 'This is not a UUID.'
    return None


def choose(label, rows, default):
    for number, (_, text) in enumerate(rows, 1):
        print(f'  {number:>2}) {text}')
    keys = [key for key, _ in rows]
    value = prompt(label, default, check=lambda v: None if v in keys or (
        v.isdigit() and 1 <= int(v) <= len(keys)) else 'Pick a number or a name from the list.')
    return value if value in keys else keys[int(value) - 1]


def setup(env_file):
    env = os.environ.get
    print(f'This writes your settings to {env_file}. Press Enter to keep the value in brackets.\n')
    print(LOOKUP_HELP['ALTARE_TOKEN'])
    token = prompt('ALTARE_TOKEN', env('ALTARE_TOKEN', ''), secret=True)
    print('\n' + LOOKUP_HELP['ALTARE_TENANT_ID'])
    tenant = str(uuid.UUID(prompt('ALTARE_TENANT_ID', env('ALTARE_TENANT_ID', ''), check=valid_uuid)))

    api = Api({'token': token, 'interval': 600})
    code, resources = api.call(tenant_path({'tenant': tenant}, '/resources'))
    if code == 401:
        raise ProvisionError("The session was rejected (HTTP 401). Copy a fresh altare_session value "
                             "and run 'setup' again.")
    if code != 200:
        raise ProvisionError('Could not load the team resources. HTTP ' + str(code))
    _, locations = api.items(LOCATIONS_PATH)
    _, eggs = api.items(EGGS_PATH)
    if not locations or not eggs:
        raise ProvisionError('Could not load the locations and templates.')

    print('\nLocations')
    rows = location_rows(locations)
    current = env('ALTARE_LOCATION', '')
    location = choose('ALTARE_LOCATION', rows, current if current in [k for k, _ in rows] else rows[0][0])

    print('\nTemplates (sandbox images)')
    print('\n'.join('  ' + line for line in template_lines(eggs, show_all=False)))
    egg_ids = {str(e.get('eggId')) for e in eggs}
    egg = prompt('ALTARE_EGG_ID', env('ALTARE_EGG_ID', ''),
                 check=lambda v: None if v in egg_ids else 'Unknown template id.')

    print('\nTeam resources')
    print('\n'.join('  ' + line for line in resource_table(resources)))
    free = resources.get('remaining') or {}
    sizes = {}
    for key, field, fallback in (('ALTARE_MEMORY_MB', 'memoryMb', 4096), ('ALTARE_CPU_PERCENT', 'cpuPercent', 100),
                                 ('ALTARE_DISK_MB', 'diskMb', 10240)):
        sizes[key] = prompt(key, env(key) or str(min(free.get(field) or fallback, fallback)), check=whole_number)
        if free.get(field) is not None and int(sizes[key]) > free[field]:
            print(f'  Note: only {free[field]} are free right now.')

    print()
    name = prompt('ALTARE_SERVER_NAME', env('ALTARE_SERVER_NAME', 'my-server'))
    current_password = env('ALTARE_SSH_PASSWORD', '')
    while True:
        password = prompt('ALTARE_SSH_PASSWORD', current_password, secret=True)
        if password == current_password or getpass.getpass('Repeat the password: ') == password:
            break
        print('  The passwords do not match.')
    webhook = prompt('ALTARE_WEBHOOK_URL (optional, Enter to skip)', env('ALTARE_WEBHOOK_URL', ''), required=False,
                     check=lambda v: None if v.startswith(('https://', 'http://')) else 'Use an http(s) URL.')

    values = {'ALTARE_TOKEN': token, 'ALTARE_TENANT_ID': tenant, 'ALTARE_SSH_PASSWORD': password,
              'ALTARE_EGG_ID': egg, 'ALTARE_SERVER_NAME': name, 'ALTARE_LOCATION': location, **sizes,
              'ALTARE_WEBHOOK_URL': webhook}
    print(f"\nServer {name}: template {egg} in {location}, {sizes['ALTARE_MEMORY_MB']} MB RAM, "
          f"{sizes['ALTARE_CPU_PERCENT']} % CPU, {sizes['ALTARE_DISK_MB']} MB disk.")
    if input(f'Write these settings to {env_file}? [Y/n] ').strip().lower() not in ('', 'y', 'yes'):
        print('Nothing written.')
        return
    write_env(env_file, values)
    print(f"Saved. Start with: python3 {Path(sys.argv[0]).name} run --loop")
    print('Restart a loop that is already running so it picks up the new settings.')


def update_token(env_file):
    tenant = os.environ.get('ALTARE_TENANT_ID', '')
    if not tenant or valid_uuid(tenant):
        raise ProvisionError("ALTARE_TENANT_ID is missing. Run 'setup' instead.")
    print(LOOKUP_HELP['ALTARE_TOKEN'])
    token = prompt('ALTARE_TOKEN', secret=True)
    code = check_session(token, tenant)
    if code != 200:
        raise ProvisionError(f'The token was rejected (HTTP {code}). Nothing written.')
    write_env(env_file, {'ALTARE_TOKEN': token})
    os.environ['ALTARE_TOKEN'] = token
    print(f'Saved to {env_file}.')
    try:
        config = configuration()
    except ProvisionError:
        return
    lock = try_lock(config)
    if lock is None:
        print(f'The running loop picks up the new token within {TOKEN_POLL_SECONDS} seconds.')
        return
    with lock:
        runner = Provisioner(config, Api(config))
        if runner.state.get('paused') == 'http_401':
            runner.resume()
            print("Start the loop again with 'run --loop'.")


def wait_for_new_token(env_file, config, runner):
    """Keep a paused loop alive until .env holds a token that the API accepts."""
    rejected = None
    token = config['token']
    while True:
        if not token or token == rejected:
            time.sleep(TOKEN_POLL_SECONDS)
            token = read_env(env_file).get('ALTARE_TOKEN', '')
            continue
        config['token'] = token
        code, _ = runner.api.items(runner.servers)
        if code == 200:
            runner.resume()
            return
        if code in (401, 403):
            if rejected is None:
                print(f'Waiting for a new ALTARE_TOKEN in {env_file}.', flush=True)
            else:
                print(f'The new token was rejected (HTTP {code}).', flush=True)
            rejected = token
        else:
            # A network error or similar: check again after the next poll.
            time.sleep(TOKEN_POLL_SECONDS)
            token = read_env(env_file).get('ALTARE_TOKEN', '') or token


# Command line

def build_parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument('--env-file', type=Path, default=Path('.env'), help='Settings file (default: .env).')
    parser = argparse.ArgumentParser(
        description='Retries creating an Altare server until a location accepts it.',
        epilog="Start with 'setup', then use 'run --loop'.")
    commands = parser.add_subparsers(dest='command', metavar='command')
    commands.add_parser('setup', parents=[common], help='Ask for the settings and write the .env file.')
    run = commands.add_parser('run', parents=[common], help='Try to create the server once, or until it works.')
    run.add_argument('--loop', action='store_true', help='Repeat every interval until the server is created.')
    commands.add_parser('token', parents=[common], help='Store a new session token after it expired.')
    commands.add_parser('status', parents=[common], help='Show the local state.')
    options = commands.add_parser('options', parents=[common], help='List locations and templates.')
    options.add_argument('--all', action='store_true', help='Include game and app templates.')
    commands.add_parser('resources', parents=[common], help='Show used and free team resources.')
    commands.add_parser('resume', parents=[common], help='Lift a pause so the next run tries again.')
    commands.add_parser('reset', parents=[common], help='Discard the local state and start over.')
    return parser


def try_lock(config):
    """Return the open lock file, or None while another run holds it."""
    config['state_dir'].mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = (config['state_dir'] / 'run.lock').open('a')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        return None
    return lock


def run_locked(args, config, api):
    lock = try_lock(config)
    if lock is None:
        print('Another run is already active. Stop it first.')
        return 0
    with lock:
        runner = Provisioner(config, api)
        if args.command == 'resume':
            runner.resume()
        elif args.command == 'reset':
            runner.reset()
        else:
            while True:
                try:
                    if runner.tick() == 'done' or not args.loop:
                        break
                except ProvisionError as error:
                    if not (args.loop and runner.state.get('paused') == 'http_401'):
                        raise
                    print(str(error).split('\n\n')[0], file=sys.stderr, flush=True)
                    wait_for_new_token(args.env_file, config, runner)
                    continue
                time.sleep(max(config['interval'], runner.state.get('next_attempt_at', 0) - time.time()))
        return 0


def main(argv=None):
    os.umask(0o077)
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 0
    try:
        load_env(args.env_file)
        if args.command == 'setup':
            setup(args.env_file)
            return 0
        if args.command == 'token':
            update_token(args.env_file)
            return 0
        config = configuration(create=args.command in ('run', 'resume', 'reset'))
        api = Api(config)
        if args.command == 'status':
            state_file = config['state_dir'] / 'state.json'
            state = json.loads(state_file.read_text()) if state_file.exists() else {}
            print('\n'.join(status_lines(state, location=config['location'])))
        elif args.command == 'options':
            for path, title in ((LOCATIONS_PATH, 'Locations (ALTARE_LOCATION)'), (EGGS_PATH, 'Templates (ALTARE_EGG_ID)')):
                code, items = api.items(path)
                if items is None:
                    raise ProvisionError('Query failed. HTTP ' + str(code))
                lines = [text for _, text in location_rows(items)] if path == LOCATIONS_PATH \
                    else template_lines(items, args.all)
                print(title + '\n' + '\n'.join('  ' + line for line in lines) + '\n')
        elif args.command == 'resources':
            code, data = api.call(tenant_path(config, '/resources'))
            if code != 200:
                raise ProvisionError('Query failed. HTTP ' + str(code))
            print('\n'.join(resource_table(data)))
        else:
            return run_locked(args, config, api)
        return 0
    except ProvisionError as error:
        print(str(error), file=sys.stderr)
        return 2
    except (OSError, ValueError):
        print("Configuration, authentication or state error. Check .env, 'status' and the README.", file=sys.stderr)
        return 2
    except (KeyboardInterrupt, EOFError):
        print()
        return 130


if __name__ == '__main__':
    sys.exit(main())
