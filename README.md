# Altare Auto Provision

Try to create an [Altare](https://console.altare.gg) server every 10 minutes until capacity becomes available. Set the region, memory, CPU, disk, and template in `.env`. Once the API confirms creation, the program prints the console URL and stops trying.

## Is it free? Is it a VPS?

Altare offers an Always Free plan. The [resource documentation checked on November 21, 2026](https://docs.altare.gg/platform/resources) lists 6 GB of memory, 150% CPU, 10 GB of disk, and four server slots shared by a team. See the [current plans](https://docs.altare.gg/platform/plans) for details.

Your account's actual resources, allowed regions, and available capacity may differ. Check your console and run `--check`. The example requests 4 GB of memory, 100% CPU, and 10 GB of disk. A 100% CPU allocation represents one core's quota; it does not guarantee a dedicated core.

The endpoint used here creates a container-based application server. SSH access depends on the selected template. This project does not provide a full virtual machine, host access, or guaranteed capacity. It requests resources already available to your team. It does not buy upgrades or change your plan.

## Requirements

- macOS or Linux with Python 3.10 or later and `curl`.
- An Altare account with permission to create servers in your team.
- A valid token or session and enough unused resources.

No Python packages are required. Windows users can use WSL; native Windows is not supported because the execution lock uses Unix file locking.

## Quick start

```bash
git clone https://github.com/flaviomartil/altare-auto-provision.git
cd altare-auto-provision
cp .env.example .env
chmod 600 .env
```

Fill in your local `.env` file:

```dotenv
ALTARE_TOKEN=
ALTARE_TENANT_ID=
ALTARE_SSH_PASSWORD=
ALTARE_EGG_ID=
ALTARE_SERVER_NAME=my-server
ALTARE_LOCATION=sg-sgp
ALTARE_MEMORY_MB=4096
ALTARE_CPU_PERCENT=100
ALTARE_DISK_MB=10240
ALTARE_INTERVAL_SECONDS=600
ALTARE_STATE_DIR=.state
```

Use single quotes around passwords containing spaces, `#`, or other special characters. The file accepts `KEY=value`, quoted values, and comments. It does not execute shell commands or expand variables. Existing environment variables take precedence over values in `.env`.

### Finding your configuration values

1. Sign in to the [Altare console](https://console.altare.gg) and select your team.
2. Open your browser's developer tools and select the **Network** tab.
3. Choose a template and region in the creation form. When you make an authorized creation attempt, find the `POST /api/tenants/<uuid>/servers` request. If that request already created a server, check the console before running this program.
4. The UUID in the request URL is `ALTARE_TENANT_ID`. The value after `Bearer ` in the `authorization` header is `ALTARE_TOKEN`.
5. In the JSON request body, `eggId` identifies the template and `location` identifies the region. Copy only the needed values into your local `.env` file.

Browser sessions can expire. This script uses the console API, whose contract may change. This is an unofficial integration and does not bypass the provider's authentication challenges.

After filling in the required values, inspect your options and resources without creating anything:

```bash
python3 altare_retry.py --list-options
python3 altare_retry.py --check
```

`--list-options` lists regions and templates. Check each region's `creationPaused` and `plusOnly` fields. `--check` shows your team's package, usage, and remaining resources.

### Try once

```bash
python3 altare_retry.py
```

The command makes at most one creation attempt. If capacity is unavailable, it saves its state and exits. Repeated invocations within the configured interval do not make another attempt.

### Retry until creation succeeds

```bash
python3 altare_retry.py --loop
```

Keep the machine powered on and connected. The program waits at least 600 seconds between attempts. After successful creation, it prints a `created` event with the console URL and exits. API confirmation does not mean the template has finished installing; follow installation progress in the console.

To run in the background:

```bash
nohup python3 -u altare_retry.py --loop > retry.log 2>&1 &
```

Press `Ctrl+C` to stop a foreground process. For a background process, use the PID printed by your shell with `kill <PID>`.

### Schedule with cron

Run `command -v python3` to find your Python executable and `pwd` to find the project directory. Open `crontab -e` and add a line while preserving your existing jobs:

```cron
*/10 * * * * cd /path/to/altare-auto-provision && /path/to/python3 altare_retry.py >> retry.log 2>&1
```

Do not combine `--loop` with cron. After success, later invocations exit without calling the creation API. The program does not edit your crontab; remove the entry when you also want to stop the schedule. Both cron and `nohup` require your machine to remain on. They do not run the task on GitHub.

## Configuration

| Variable | Purpose | Default |
| --- | --- | --- |
| `ALTARE_TOKEN` | Token or session used for Bearer authentication | Required |
| `ALTARE_TENANT_ID` | Team UUID | Required |
| `ALTARE_SSH_PASSWORD` | SSH password sent to the template | Required |
| `ALTARE_EGG_ID` | ID of a template compatible with the SSH payload | Required |
| `ALTARE_SERVER_NAME` | Server name | `my-server` |
| `ALTARE_LOCATION` | Region | `sg-sgp` |
| `ALTARE_MEMORY_MB` | Memory in MiB | `4096` |
| `ALTARE_CPU_PERCENT` | CPU quota as a percentage | `100` |
| `ALTARE_DISK_MB` | Disk in MiB | `10240` |
| `ALTARE_INTERVAL_SECONDS` | Retry interval, at least 600 seconds | `600` |
| `ALTARE_STATE_DIR` | Persistent state directory | `.state` |

To use a different environment file:

```bash
python3 altare_retry.py --env-file /path/to/config.env --loop
```

## State, failures, and recovery

```bash
python3 altare_retry.py --status
```

The state directory contains `state.json`, `events.jsonl`, and `run.lock`. These files do not store the token or SSH password. Preserve them when restarting. The execution lock prevents concurrent runs sharing the same state directory. Use one machine and one state directory for each request.

- **HTTP 422:** capacity is unavailable or the resource combination was rejected. The program retries after the interval. If this persists, check your quotas, template, and region.
- **HTTP 429:** high traffic or rate limiting. The program respects `Retry-After` and the configured interval.
- **HTTP 5xx:** provider failure. The program checks the server list to reconcile the result before the next attempt.
- **Timeout or uncertain response:** the program preserves the idempotency key for the next attempt and checks the server list before creating. This protection also depends on how the provider handles idempotency.
- **HTTP 400, 401, 403, 404, 405, or 409:** retries pause until you review the configuration, session, or permissions.
- **Existing server name:** retries pause to avoid creating another server. A pending creation can be reconciled using its name and resource fields returned by the API.

Logs contain events and HTTP status codes. API error bodies, authentication headers, and passwords are not printed. The completed state stores the server ID. Later runs do not create another server, even if you subsequently delete it in the console.

To resume a paused request, first check the console and fix the cause. With the process stopped, remove only the `paused` field from `state.json`. Preserve `pending_key` if present. Do not delete state after an uncertain response without checking whether the server was created.

To create another server, use a different name and `ALTARE_STATE_DIR`. Changing the region, template, or resource settings of an existing request requires a new state directory. Resolve any pending attempt for the previous configuration first.

## Credentials and scope

The `.env` file, default state directory, and logs are excluded through `.gitignore`. Keep tokens local. Do not publish your browser's “Copy as cURL” output, which often contains session credentials and cookies. Use the program with your own account and follow the provider's rules. Capacity and free-plan terms may change.

The program only queries the API and requests creation of one server. It does not delete servers, resize existing servers, or send messages or notifications.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

Tests simulate the API without creating resources. They cover successful creation, unavailable capacity, authentication failures, uncertain responses, persistence, and prevention of further creation attempts after success. The public version has also passed a read-only resource query against the live API. No additional server was created to validate this version.

## License

[MIT](LICENSE).
