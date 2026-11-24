# Altare Auto Provision

Try to create an [Altare](https://console.altare.gg) server every 10 minutes until capacity becomes available. Set the region, memory, CPU, disk, and template in `.env`. Once the API confirms creation, the program prints the console URL and stops trying.

## Is it free? Is it a VPS?

Altare offers an Always Free plan. The [resource documentation checked on November 21, 2026](https://docs.altare.gg/platform/resources) lists 6 GB of memory, 150% CPU, 10 GB of disk, and four server slots shared by a team. See the [current plans](https://docs.altare.gg/platform/plans) for details.

Your account's actual resources, allowed regions, and available capacity may differ. Check your console and run `resources`. The example requests 4 GB of memory, 100% CPU, and 10 GB of disk. A 100% CPU allocation represents one core's quota; it does not guarantee a dedicated core.

The endpoint used here creates a container-based application server. SSH access depends on the selected template. This project does not provide a full virtual machine, host access, or guaranteed capacity. It requests resources already available to your team. It does not buy upgrades or change your plan.

## Requirements

- macOS or Linux with Python 3.10 or later and `curl`.
- An Altare account with permission to create servers in your team.
- A valid token or session and enough unused resources.

No Python packages are required. Windows users can use WSL; native Windows is not supported because the execution lock uses Unix file locking.

## Quick start

```bash
git clone https://github.com/Knyllahsyhn/altare-auto-provision.git
cd altare-auto-provision
python3 altare_retry.py setup
python3 altare_retry.py run --loop
```

Running the program without a command shows the list of commands.

| Command | Purpose |
| --- | --- |
| `setup` | Ask for all settings and write `.env` |
| `run` | Make at most one creation attempt; `run --loop` repeats until it succeeds |
| `token` | Store a new session token after the old one expired |
| `status` | Show the local state with local times |
| `options` | List locations and templates; `options --all` includes game and app templates |
| `resources` | Show used, free, and total team resources |
| `resume` | Lift a pause after fixing its cause |
| `reset` | Discard the local state and start over |

`status`, `options`, and `resources` only read and also work while a loop is running.

### Setup

`setup` asks for the session token and the team ID and checks both against the API. It then shows the locations with their status, the sandbox templates, and the free team resources, and asks for your choice. It suggests memory, CPU, and disk sizes that fit the free resources. At the end it shows a summary and asks before writing `.env` with mode 600. Run it again at any time; pressing Enter keeps the current value. Lines in `.env` that `setup` does not manage stay unchanged.

### Finding the token and team ID

`setup` and the error messages explain this as well. Sign in to the [Altare console](https://console.altare.gg), select your team and open your browser's developer tools.

1. **`ALTARE_TOKEN`:** open **Application** (Chrome) or **Storage** (Firefox), then **Cookies** › `https://console.altare.gg`, and copy the value of the `altare_session` cookie. The cookie is HttpOnly, so a script in the page cannot read it. The `cf_clearance` cookie next to it is not needed.
2. **`ALTARE_TENANT_ID`:** in the **Console** tab, run the following line. It copies the ID of the team currently selected in the console to the clipboard. Chrome asks you to type `allow pasting` before it accepts pasted code.

   ```js
   copy(localStorage.getItem('altare_selected_tenant_id'))
   ```

The session expires after some time, and Altare offers no way to renew it from outside the browser. The API then answers with HTTP 401 and the routine pauses; with a webhook configured, you receive a `paused` message. Copy the new `altare_session` value and run:

```bash
python3 altare_retry.py token
```

`token` checks the new value against the API and writes only `ALTARE_TOKEN` to `.env`. A loop started with `run --loop` keeps running while the session is expired. It reads `.env` once a minute and continues as soon as it finds a token that the API accepts, so editing `.env` by hand works as well. Without a running loop, `token` lifts the pause and the next `run` continues.

This script uses the console API with a browser session, and that contract may change. Team API tokens (`alt_…`) do not work, because Altare does not allow them to create servers. This is an unofficial integration and does not bypass the provider's authentication challenges.

### Run once or in a loop

`run` checks whether the configured location accepts new servers. If its `creationPaused` flag is set, the program sends no creation request and waits for the next interval; `status` then names the location. Otherwise it makes at most one creation attempt, saves its state, and exits. Repeated invocations within the configured interval do not make another attempt.

`run --loop` repeats this every interval until the server is created. Keep the machine powered on and connected. After successful creation, the program prints a `created` event with the console URL and exits. API confirmation does not mean the template has finished installing; follow installation progress in the console.

To run in the background:

```bash
nohup python3 -u altare_retry.py run --loop > retry.log 2>&1 &
```

Press `Ctrl+C` to stop a foreground process. For a background process, use the PID printed by your shell with `kill <PID>`. A running loop reads `.env` only at start; restart it after changing settings.

### Schedule with cron

Run `command -v python3` to find your Python executable and `pwd` to find the project directory. Open `crontab -e` and add a line while preserving your existing jobs:

```cron
*/10 * * * * cd /path/to/altare-auto-provision && /path/to/python3 altare_retry.py run >> retry.log 2>&1
```

Do not combine `--loop` with cron. After success, later invocations exit without calling the creation API. The program does not edit your crontab; remove the entry when you also want to stop the schedule. Both cron and `nohup` require your machine to remain on.

### Notifications

Set `ALTARE_WEBHOOK_URL` to receive a POST request when the server has been created (`created`) and when the routine pauses (`paused`), for example after the session expired. The body is JSON:

```json
{"text": "Altare provisioning paused: The session has expired ...", "at": "2026-11-24T03:10:46+00:00",
 "event": "paused", "reason": "http_401", "message": "The session has expired ..."}
```

A `created` event carries `panel` with the console URL instead of `reason` and `message`. Services that read a `text` field, such as Slack, Mattermost, and Rocket.Chat, show the message directly. Discord accepts this format when you append `/slack` to its webhook URL. ntfy shows the whole JSON body as the message text. A failed webhook prints a warning and does not stop the routine.

## Configuration

`setup` writes these values. You can also edit `.env` by hand; the file accepts `KEY=value`, quoted values, and comments. It does not execute shell commands or expand variables. Use single quotes around values containing spaces, `#`, or other special characters. Existing environment variables take precedence over values in `.env`.

| Variable | Purpose | Default |
| --- | --- | --- |
| `ALTARE_TOKEN` | Value of the `altare_session` cookie | Required |
| `ALTARE_TENANT_ID` | Team UUID | Required |
| `ALTARE_SSH_PASSWORD` | SSH password sent to the template | Required for `run` |
| `ALTARE_EGG_ID` | Template ID, see `options` | Required for `run` |
| `ALTARE_SERVER_NAME` | Server name | `my-server` |
| `ALTARE_LOCATION` | Location slug, see `options` | `sg-sgp` |
| `ALTARE_MEMORY_MB` | Memory in MiB | `4096` |
| `ALTARE_CPU_PERCENT` | CPU quota as a percentage | `100` |
| `ALTARE_DISK_MB` | Disk in MiB | `10240` |
| `ALTARE_INTERVAL_SECONDS` | Retry interval, at least 600 seconds | `600` |
| `ALTARE_STATE_DIR` | Persistent state directory | `.state` |
| `ALTARE_WEBHOOK_URL` | Optional webhook for `created` and `paused` | none |

Every command accepts `--env-file` to use a different settings file:

```bash
python3 altare_retry.py run --loop --env-file /path/to/config.env
```

## State, failures, and recovery

`status` prints the current phase, the number of attempts, the last HTTP status and the time of the next check in local time.

The state directory contains `state.json`, `events.jsonl`, and `run.lock`. These files do not store the token or SSH password. Preserve them when restarting. The execution lock prevents concurrent runs sharing the same state directory. Use one machine and one state directory for each request.

- **Location paused:** the program waits without sending a creation request, see above.
- **HTTP 503:** provider failure. The program keeps the idempotency key and checks the server list at the start of the next attempt. If no server with the configured name exists, it continues with a new key.
- **HTTP 422:** capacity is unavailable or the resource combination was rejected. The program retries after the interval. If this persists, check your quotas, template, and location.
- **HTTP 429:** high traffic or rate limiting. The program respects `Retry-After` and the configured interval.
- **Other HTTP 5xx:** handled like 503.
- **Timeout or uncertain response:** the program preserves the idempotency key for the next attempt and checks the server list before creating. This protection also depends on how the provider handles idempotency.
- **HTTP 401:** the session has expired, see [Finding the token and team ID](#finding-the-token-and-team-id).
- **HTTP 400, 403, 404, 405, or 409:** retries pause until you review the configuration or permissions.
- **Unknown location:** retries pause until `ALTARE_LOCATION` names a slug from `options`.
- **Existing server name:** retries pause to avoid creating another server. A pending creation can be reconciled using its name and resource fields returned by the API.

Logs contain events and HTTP status codes. API error bodies, authentication headers, and passwords are not printed. The completed state stores the server ID. Later runs do not create another server, even if you subsequently delete it in the console.

To resume a paused request, first check the console and fix the cause, then stop any running process and run `resume`. This lifts the pause and keeps an unconfirmed attempt, so the next run first checks whether that attempt created the server.

To start over, for example after deleting the created server in the console, run `reset`. It discards the local state. Changing the name, location, template, or resource settings has the same effect on the next run. In both cases the program logs a warning if the last attempt was unconfirmed. A server with the same name still pauses the routine instead of creating a duplicate, but a server created under a previous name is not detected. Check the console in that case.

To run several requests side by side, give each one its own name and `ALTARE_STATE_DIR`.

## Credentials and scope

The `.env` file, default state directory, and logs are excluded through `.gitignore`. Keep tokens local. Do not share the `altare_session` value or your browser's “Copy as cURL” output, which contains it. Use the program with your own account and follow the provider's rules. Capacity and free-plan terms may change.

The program only queries the API, requests creation of one server, and calls the optional webhook. It does not delete or resize servers.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

Tests simulate the API without creating resources. They cover successful creation, paused locations, unavailable capacity, authentication failures, uncertain responses, persistence, prevention of further creation attempts after success, the setup dialog, and webhooks. The public version has also passed a read-only resource query against the live API. No additional server was created to validate this version.

## License

[MIT](LICENSE).
