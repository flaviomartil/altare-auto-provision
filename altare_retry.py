import argparse
import datetime
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
            raise ProvisionError('Formato inválido no arquivo de ambiente.')
        key, value = words[0].split('=', 1)
        os.environ.setdefault(key, value)


def configuration():
    required = ['ALTARE_TOKEN', 'ALTARE_TENANT_ID', 'ALTARE_SSH_PASSWORD', 'ALTARE_EGG_ID']
    if any(not os.environ.get(key) for key in required):
        raise ProvisionError('Preencha: ' + ', '.join(required))
    try:
        config = {
            'token': os.environ['ALTARE_TOKEN'],
            'tenant': str(uuid.UUID(os.environ['ALTARE_TENANT_ID'])),
            'password': os.environ['ALTARE_SSH_PASSWORD'],
            'name': os.environ.get('ALTARE_SERVER_NAME', 'my-server'),
            'eggId': int(os.environ['ALTARE_EGG_ID']),
            'memoryMb': int(os.environ.get('ALTARE_MEMORY_MB', '4096')),
            'cpuPercent': int(os.environ.get('ALTARE_CPU_PERCENT', '100')),
            'diskMb': int(os.environ.get('ALTARE_DISK_MB', '10240')),
            'location': os.environ.get('ALTARE_LOCATION', 'sg-sgp'),
            'interval': int(os.environ.get('ALTARE_INTERVAL_SECONDS', '600')),
            'state_dir': Path(os.environ.get('ALTARE_STATE_DIR', '.state')).expanduser().resolve(),
        }
    except (ValueError, KeyError):
        raise ProvisionError('UUID ou valor numérico inválido.') from None
    if any(config[k] <= 0 for k in ['eggId', 'memoryMb', 'cpuPercent', 'diskMb']):
        raise ProvisionError('Template, RAM, CPU e disco precisam ser positivos.')
    if config['interval'] < 600:
        raise ProvisionError('Use um intervalo de pelo menos 600 segundos.')
    if not config['name'].strip() or not config['location'].strip():
        raise ProvisionError('Nome e região são obrigatórios.')
    if not shutil.which('curl'):
        raise ProvisionError('Instale curl antes de continuar.')
    return config


class Api:
    def __init__(self, config):
        self.config = config
        self.cooldown = 0

    def call(self, path, body=None, key=None):
        headers = {'Authorization': 'Bearer ' + self.config['token'], 'Content-Type': 'application/json'}
        if key:
            headers['Idempotency-Key'] = key
        lines = ['url = ' + json.dumps('https://console.altare.gg' + path)]
        lines.extend('header = ' + json.dumps(k + ': ' + v) for k, v in headers.items())
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
            for line in header_file.read_text().splitlines() if header_file.exists() else []:
                if line.lower().startswith('retry-after:'):
                    value = line.split(':', 1)[1].strip()
                    try:
                        delay = int(value)
                    except ValueError:
                        from email.utils import parsedate_to_datetime
                        try:
                            delay = int(parsedate_to_datetime(value).timestamp() - time.time())
                        except (ValueError, TypeError, OverflowError):
                            delay = self.config['interval']
                    self.cooldown = max(self.cooldown, time.time() + max(0, delay))
        raw, _, code = result.stdout.rpartition('\nHTTP:')
        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                data = {}
        except ValueError:
            data = {}
        return int(code or 0), data


class Provisioner:
    def __init__(self, config, api):
        self.config = config
        self.api = api
        self.directory = config['state_dir']
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.directory / 'state.json'
        self.state = json.loads(self.path.read_text()) if self.path.exists() else {}
        target = {k: config[k] for k in ['tenant', 'name', 'eggId', 'location', 'memoryMb', 'cpuPercent', 'diskMb']}
        fingerprint = hashlib.sha256(json.dumps(target, sort_keys=True).encode()).hexdigest()
        if self.state.get('target', fingerprint) != fingerprint:
            raise ProvisionError('A configuração mudou. Use outro ALTARE_STATE_DIR para outro servidor.')
        self.state['target'] = fingerprint
        self.servers = '/api/tenants/' + quote(config['tenant'], safe='') + '/servers'

    def save(self):
        temp = self.path.with_suffix('.tmp')
        with temp.open('w') as handle:
            json.dump(self.state, handle, indent=2)
        temp.chmod(0o600)
        temp.replace(self.path)

    def event(self, name, **values):
        entry = {'at': datetime.datetime.now(datetime.timezone.utc).isoformat(), 'event': name, **values}
        with (self.directory / 'events.jsonl').open('a') as handle:
            handle.write(json.dumps(entry) + '\n')
        print(json.dumps(entry), flush=True)

    def complete(self, server):
        self.state['server_id'] = server['id']
        self.state['completed'] = True
        self.state.pop('pending_key', None)
        self.save()
        self.event('created', panel='https://console.altare.gg/server/' + str(server['id']))
        return 'done'

    def tick(self):
        if self.state.get('completed'):
            return 'done'
        if self.state.get('paused'):
            raise ProvisionError('Rotina pausada. Consulte --status e o README antes de retomar.')
        if time.time() < self.state.get('next_attempt_at', 0):
            return 'wait'
        code, listing = self.api.call(self.servers)
        if code != 200 or not isinstance(listing.get('items'), list):
            return self.failure(code, 'list_failed')
        matches = [s for s in listing['items'] if s.get('name') == self.config['name']]
        if matches:
            if self.state.get('pending_key') and len(matches) == 1:
                server = matches[0]
                fields = ['memoryMb', 'cpuPercent', 'diskMb', 'eggId']
                if all(server.get(k) == self.config[k] for k in fields):
                    return self.complete(server)
            self.state['paused'] = 'name_already_exists'
            self.save()
            raise ProvisionError('Já existe um servidor com esse nome. Confira o painel antes de continuar.')
        if not self.state.get('pending_key'):
            self.state['pending_key'] = str(uuid.uuid4())
        self.state['attempts'] = self.state.get('attempts', 0) + 1
        self.state['next_attempt_at'] = time.time() + self.config['interval']
        self.save()
        payload = {k: self.config[k] for k in ['name', 'eggId', 'memoryMb', 'cpuPercent', 'diskMb', 'location']}
        payload['ssh'] = {'password': self.config['password']}
        code, response = self.api.call(self.servers, payload, self.state['pending_key'])
        if 200 <= code < 300 and response.get('id'):
            return self.complete(response)
        if code in (422, 429):
            self.state.pop('pending_key', None)
        elif code >= 500:
            check_code, check = self.api.call(self.servers)
            if check_code == 200 and check.get('items') == []:
                self.state.pop('pending_key', None)
        return self.failure(code, 'creation_failed')

    def failure(self, code, event):
        self.state['last_http'] = code
        self.state['next_attempt_at'] = max(time.time() + self.config['interval'], self.api.cooldown)
        if code in (400, 401, 403, 404, 405, 409):
            self.state['paused'] = 'http_' + str(code)
        self.save()
        self.event(event, http=code, attempts=self.state.get('attempts', 0))
        if self.state.get('paused'):
            raise ProvisionError('Requisição recusada. Confira autenticação, configuração e permissões.')
        return 'wait'


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description='Tenta criar um servidor Altare até haver disponibilidade.')
    parser.add_argument('--env-file', type=Path, default=Path('.env'))
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--loop', action='store_true', help='Repete a cada intervalo até criar.')
    mode.add_argument('--status', action='store_true', help='Mostra somente o estado local.')
    mode.add_argument('--check', action='store_true', help='Consulta recursos, sem criar servidor.')
    mode.add_argument('--list-options', action='store_true', help='Lista regiões e templates, sem criar.')
    args = parser.parse_args()
    try:
        load_env(args.env_file)
        config = configuration()
        config['state_dir'].mkdir(parents=True, exist_ok=True, mode=0o700)
        if args.status:
            state_file = config['state_dir'] / 'state.json'
            print(json.dumps(json.loads(state_file.read_text()) if state_file.exists() else {}, indent=2))
            return 0
        with (config['state_dir'] / 'run.lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                print('Outra execução já está ativa.')
                return 0
            api = Api(config)
            runner = Provisioner(config, api)
            if args.check or args.list_options:
                paths = ['/api/core/locations', '/api/core/eggs'] if args.list_options else [
                    '/api/tenants/' + config['tenant'] + '/resources']
                for path in paths:
                    code, data = api.call(path)
                    if code != 200:
                        raise ProvisionError('Falha na consulta. HTTP ' + str(code))
                    print(json.dumps(data, indent=2, ensure_ascii=False))
                return 0
            while True:
                outcome = runner.tick()
                if outcome == 'done' or not args.loop:
                    return 0
                time.sleep(max(config['interval'], runner.state.get('next_attempt_at', 0) - time.time()))
    except ProvisionError as error:
        print(str(error), file=sys.stderr)
        return 2
    except (OSError, ValueError):
        print('Erro de configuração, autenticação ou estado. Confira o .env, --status e o README.', file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == '__main__':
    sys.exit(main())
