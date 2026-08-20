# garbage-collection-automation

A small service that looks up your household waste collection dates and turns
them into Todoist to-dos. It runs as a weekly cron job in a Debian 12 LXC
container, with an optional local web page to inspect and trigger it by hand.

While this project is built around a Debian 12 LXC target - installation should work on any Debian-based OS.

Dates come from the [mijnafvalwijzer.nl](https://www.mijnafvalwijzer.nl) JSON
API — one request per run, for one address.

## How it works

Each run is a short pipeline — collect, process, export — and then exits. There
is no daemon except the optional web interface.

```
cron ─> run-job.sh ─> CLI ─> configuration
                       │
                       └──> application ─> collect ─> process ─> export
                                                                    │
                                                state.json <─> reconcile <─> Todoist

systemd ─> web ─> ui/          (localhost only, and only when [web] enabled)
            └──> api ─> application      (the buttons, into the same pipeline)
```

Todoist is not called on every run. Each run records which todo it created for
which collection in `state.json`, and only picks up the phone when something no
longer agrees:


| What the run finds                                    | What it does                                                                                                       |
| ----------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------ |
| The same collections as last time                     | Nothing at all — Todoist is not called                                                                             |
| A date added, moved or no longer collected            | Asks Todoist what it holds, works out the difference, applies it                                                   |
| `due_time`, `project`, `section` or `remind_days_before` changed | The same, and rewrites every todo that stays                                                                       |
| No state file, or one it cannot read                  | The same; a missing record is a reason to ask                                                                      |
| A previous run that stopped halfway                   | The same, and rewrites every todo that stays; what got through was recorded, but only Todoist can say what did not |


The state file determines *whether* to ask. However, the update delta is always
based on first-hand Todoist data. A to-do deleted by hand comes back, and stray
duplicates are cleaned up. We never consider anything that happened before now.
The state file is disposable and will automatically be rebuilt on subsequent
runs.

The schedule API publishes **dates only, never a time**, which is why
`due_time` is a configuration key rather than something read from the source.

### The to-dos

One todo per collection, in the project named by `export.todoist.project` — in
the section named by `export.todoist.section`, when one is named — due on the
collection day at `due_time` with a reminder `remind_days_before` days ahead of
that:

```
Restafval buitenzetten                       ← the line you read
  due    Thu 20 Aug 2026 07:00               ← due_time, Europe/Amsterdam
  label  garbage-collection                  ← what makes it ours
  [gca:2026-08-20:restafval]                 ← in the description: what it is for
```

The **label** is what a run looks for: every open todo carrying it is this
project's, in whichever project it sits, so one you drag elsewhere is found
again and moved back on the next rewrite. With no `section` configured, where
inside the project a todo sits is left to you. The **marker** in the description is
how a run knows which collection a todo stands for — rename the line above it
and it is still recognised, delete the marker and the todo is left alone from
then on, untouched and unmanaged. Everything else — the content, the reminder
you add yourself, the labels you add yourself — is yours.

Custom reminders are a Todoist Pro feature. On an account without it Todoist
answers `403` to every reminder, so a run says so once, writes its to-dos with
their due moment as usual and leaves the reminders out; nothing else about the
run changes.

A todo you tick off is done as far as Todoist is concerned, so a run that has to
ask about a collection day that has not passed yet will write it again. Runs
that find the schedule unchanged never ask, which is why this is rare.

## Installation

The target is a **Debian 12 LXC container**. `install.sh` is used to configure that container.

### Create the container

The recommended LXC configuration is as follows.


| Setting       | Value                | Why                                                        |
| ------------- | -------------------- | ---------------------------------------------------------- |
| Template      | `debian-12-standard` | What the installer targets                                 |
| Cores         | 1                    | One HTTP request a day                                     |
| Memory / Swap | 256 MiB each         | A run peaks around 40 MB, the web interface holds about 30 |
| Disk          | 2 GiB                | A finished install uses about 0.8 GB                       |
| Privilege     | unprivileged         | Nothing the job does needs more                            |
| Network       | DHCP on `vmbr0`      | Outbound HTTPS; the web interface stays on loopback        |
| Time zone     | host                 | Due times come from `zoneinfo` regardless                  |
| Autostart     | on boot              | So the schedule survives a host reboot                     |

### Installation

Inside the container, as root:

```sh
curl -fsSL https://raw.githubusercontent.com/tmemelink/garbage-collection-automation/main/install.sh | bash
```

Pin a tag for reproducible installs: `... | bash -s -- --ref v0.1.0`.


### A private repository

GitHub answers an unauthenticated request for a private repository with a 404 —
the same answer a ref that does not exist gets. Both halves of a plain install
are such requests: `install.sh` itself, and the source it downloads next.
Either an ssh key or a token gets past that.

With a key the container holds — its own key, registered with GitHub, for which
a deploy key on the repository is enough — fetch the installer over ssh and let
it fetch the rest the same way. The standard Debian template has no git yet:

```sh
apt-get update && apt-get install -y git
tmp_dir="$(mktemp -d)"
git -C "$tmp_dir" init -q
git -C "$tmp_dir" fetch -q --depth=1 \
  git@github.com:tmemelink/garbage-collection-automation.git main
git -C "$tmp_dir" show FETCH_HEAD:install.sh > install.sh
rm -r "$tmp_dir"
chmod +x install.sh
./install.sh --ssh
```

### Installer options

The installer is idempotent — re-run it to upgrade. It never overwrites an
existing config file or the env file holding the secrets. It does re-apply their
ownership and permissions on every run, so an install that was interrupted is
repaired by running it again.


| Option                                         | Effect                                                               |
| ---------------------------------------------- | -------------------------------------------------------------------- |
| `--ref <git-ref>`                              | Install a specific branch, tag or commit (default `main`)            |
| `--source <path>`                              | Install from a local directory or tarball instead of downloading     |
| `--ssh`                                        | Fetch the source over ssh rather than https, for a private repository |
| `--no-schedule`                                | Install without adding the cron entry                                |
| `--no-web`                                     | Install without the web interface service (and remove it if present) |
| `--postcode` / `--house-number` / `--addition` | The address, answered in advance                                     |
| `--api-key <key>`                              | The mijnafvalwijzer.nl app key, answered in advance                  |
| `--no-prompt`                                  | Do not ask for either; write the example values                      |
| `--run-now` / `--no-run-now`                   | Answer the "run it once now?" question in advance                    |
| `--uninstall`                                  | Remove app, user and schedule; keeps config, state and logs          |


### What it installs where


| Path                                                            | Contents                                                      |
| --------------------------------------------------------------- | ------------------------------------------------------------- |
| `/opt/garbage-collection-automation`                            | Application and its virtualenv                                |
| `/etc/garbage-collection-automation/config.toml`                | Configuration — the installer writes the answers it asked for |
| `/etc/garbage-collection-automation/env`                        | Secrets, `KEY=value` per line                                 |
| `/root/garbage-collection/run-garbage-collection.sh`            | The by-hand command                                           |
| `/root/garbage-collection/run-web-interface.sh`                 | The by-hand way to serve the page                             |
| `/etc/cron.d/garbage-collection-automation`                     | Schedule, 04:00 every Saturday by default                     |
| `/etc/systemd/system/garbage-collection-automation-web.service` | The web interface                                             |
| `/var/lib/garbage-collection-automation/`                       | `state.json` and the run lock                                 |
| `/var/log/garbage-collection-automation/`                       | Job output, rotated weekly                                    |


## Configuration

Configuration lives in `/etc/garbage-collection-automation/config.toml`; see
[config/config.example.toml](config/config.example.toml) for the annotated
template. The installer asks for the postcode, house number and app key and
writes them in; everything else is edited there afterwards.


| Key                                                | Default                          | Meaning                                                                                                                                                    |
| -------------------------------------------------- | -------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `address.postcode` / `.house_number` / `.addition` | —                                | The address to look up                                                                                                                                     |
| `collection.api_key`                               | —                                | The key the schedule API expects; every run needs it                                                                                                       |
| `collection.lookahead_days`                        | `30`                             | How far ahead to build to-dos                                                                                                                              |
| `collection.due_time`                              | `"07:00"`                        | Due moment on the collection day, Europe/Amsterdam                                                                                                         |
| `collection.types`                                 | `["restafval", "papier", "gft"]` | `restafval`, `papier`, `gft`, `pmd`, `glas`, `textiel`, `kca`, `kerstbomen`                                                                                |
| `collection.timeout_seconds` / `.retries`          | `15` / `1`                       | Limits on the single request per run                                                                                                                       |
| `export.todoist.enabled` / `.token` / `.project`   | `true` / — / `"Home"`            | The Todoist target; on unless the file says otherwise, and skipped with a log line when no token is configured                                             |
| `export.todoist.section`                           | `""`                             | The section within that project, by name; empty puts the to-dos in the project itself. A section that is not there fails the run, exactly as a missing project does                                         |
| `export.todoist.remind_days_before`                | `1`                              | How long before the collection the reminder goes off; `0` is the due moment itself. Reminders need Todoist Pro; without it they are skipped with a warning |
| `web.enabled`                                      | `false`                          | Whether the local page is served at all                                                                                                                    |
| `web.host` / `.port`                               | `"127.0.0.1"` / `8080`           | Loopback addresses and unprivileged ports only                                                                                                             |
| `logging.level`                                    | `"INFO"`                         | `DEBUG` adds application detail; credential-bearing HTTP query strings remain suppressed                                                                   |


Unknown keys are rejected rather than ignored, so a typo fails the run with a
message naming the key. Changing `due_time`, `project`, `section` or
`remind_days_before` makes the next run rewrite every todo it already created.

### Secrets

Two installation-level values are not compiled into the code. Each may sit in
`config.toml`, and each has an environment variable that wins over the file:


| Configuration key      | Environment variable      | Where to get it                                                                                                |
| ---------------------- | ------------------------- | -------------------------------------------------------------------------------------------------------------- |
| `collection.api_key`   | `GCA_AFVALWIJZER_API_KEY` | Open mijnafvalwijzer.nl with the browser's network tab, look up any address, copy the `apikey` query parameter |
| `export.todoist.token` | `GCA_TODOIST_TOKEN`       | Todoist › Settings › Integrations › Developer                                                                  |


A cron job inherits nothing from anyone's shell, so `run-job.sh` sources an env
file before every run — that file is how either variable reaches a scheduled
run, and the web interface's unit reads the same one:

```sh
# /etc/garbage-collection-automation/env   (root:gca, mode 0640)
GCA_AFVALWIJZER_API_KEY=the-app-key
GCA_TODOIST_TOKEN=your-token-here
```

The installer writes that file with both keys commented out and never touches it
again on upgrade. From a checkout the same file is `config/env` (gitignored);
`ENV_FILE` overrides the path in either layout.

## Usage

The job runs on its own from cron. To run it by hand, use the command the
installer leaves in `~/garbage-collection/` in root's home:

```sh
~/garbage-collection/run-garbage-collection.sh              # run as the gca user, output on your terminal
~/garbage-collection/run-garbage-collection.sh --dry-run    # collect and process, write nothing
~/garbage-collection/run-web-interface.sh                   # serve the page here until ctrl-c

tail -f /var/log/garbage-collection-automation/cron.log

cat /var/lib/garbage-collection-automation/state.json   # private address/task metadata
rm  /var/lib/garbage-collection-automation/state.json   # forget it; next run re-checks Todoist
```

To change the schedule, edit `/etc/cron.d/garbage-collection-automation`.

### Web interface

A single local page showing what the last run found, the delta it would apply,
and the configuration the headless run reads — every key of it, in a form that
writes it back, next to `config.toml` itself as it is on disk.

```toml
[web]
enabled = true
port = 8080
```

```sh
systemctl restart garbage-collection-automation-web
journalctl -u garbage-collection-automation-web -f    # the access log lives here
```

The page has no login and shows both secrets. Reach it over an ssh
tunnel, which authenticates the person the page cannot:

```sh
ssh -N -L 8080:127.0.0.1:8080 root@<container>                        # direct
ssh -N -J root@<proxmox-host> -L 8080:127.0.0.1:8080 root@<container> # via Proxmox
```

Then open [http://127.0.0.1:8080/](http://127.0.0.1:8080/).


The buttons run the same pipeline the weekly job runs, and differ only in how
far they are allowed to get:


| Button                        | Endpoint            | Reads              | Writes                            |
| ----------------------------- | ------------------- | ------------------ | --------------------------------- |
| *Collect now*                 | `POST /api/gather`  | mijnafvalwijzer.nl | nothing — a dry run               |
| *Check Todoist*               | `POST /api/check`   | also Todoist       | nothing                           |
| *Apply delta*                 | `POST /api/apply`   | also Todoist       | the to-dos, and `state.json`      |
| *Save configuration*          | `POST /api/config`  | the form           | `config.toml`, rewritten in place |
| *Switch off and stop*         | `POST /api/stop`    | the form           | `[web] enabled = false`, then ends the process |


### Exit codes

`run-job.sh` passes the CLI's codes straight through. `0`–`5` belong to the job,
`6` to the web interface.


| Code | Meaning                                                                                                                                                                                     |
| ---- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `0`  | Run completed — also what the wrapper reports when it skipped an overlapping run                                                                                                            |
| `1`  | The wrapper could not start the run — no readable config, or nothing installed                                                                                                              |
| `2`  | The configuration is missing or invalid — the log says which key                                                                                                                            |
| `3`  | Todoist could not be reached, or refused — a token, a project that is not there, their API having a moment. The message names the call it failed on, and repeats what Todoist said about it |
| `4`  | The schedule could not be collected — source unreachable, or unknown address                                                                                                                |
| `5`  | The export could not be recorded — to-dos may exist while `state.json` does not say so                                                                                                      |
| `6`  | The web interface could not start — port taken, or `ui/` not where it should be                                                                                                             |


## Development

The whole pipeline runs from a checkout, touching **only** the repository: it
reads `config/config.toml`, uses the `.venv/` next to it, keeps its lock and
state in `.local/`, and writes nothing to `/etc`, `/opt`, `/var` or cron.

```sh
uv sync                                     # create .venv (Python 3.14 via uv)
uv run pre-commit install                   # the checks below, on every commit
cp config/config.example.toml config/config.toml
$EDITOR config/config.toml                  # your address

echo 'GCA_AFVALWIJZER_API_KEY=...' > config/env
echo 'GCA_TODOIST_TOKEN=...' >> config/env  # only if you enable the export

./src/run-job.sh --dry-run                  # same wrapper cron runs
```

### Tests

```sh
uv run pytest
uv run pytest --cov --cov-report=term-missing
```

No test touches the network: captured mijnafvalwijzer.nl responses in
`tests/fixtures/` are replayed through `httpx.MockTransport`, Todoist answers
from a table of canned replies, and a fixture in `conftest.py` fails any test
that tries to reach a host other than the loopback one the web tests serve.
The suite also covers the shell — the wrapper's two layouts, the installer's file handling, and the contents of the bundle `./build.sh lxc`
produces.

## Security

Please report vulnerabilities privately; see [SECURITY.md](SECURITY.md). Never
put a live token, API key, address or other private data in a public issue.

## License

[Apache 2.0](LICENSE)
