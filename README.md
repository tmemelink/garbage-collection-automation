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

Or from the Proxmox host, without entering the container:

```sh
pct exec <vmid> -- bash -c 'curl -fsSL https://raw.githubusercontent.com/tmemelink/garbage-collection-automation/main/install.sh | bash'
```

Pin a tag for reproducible installs: `... | bash -s -- --ref v0.1.0`.

A first install asks for the two things it cannot guess — the address to look up
and the mijnafvalwijzer.nl app key — and writes them into `config.toml`:

```
==> Configuration. Press enter to leave one out; /etc/garbage-collection-automation/config.toml
    then keeps the example value, and the first run says which.

    Postcode [1234AB]: 1234 AB
    House number [56]: 56
    Addition, if any:
    Afvalwijzer API key: ...
```

Each answer is checked as it is typed, against the same rules the application
applies. An upgrade never asks: the config file it keeps already holds them.
Where there is no terminal to ask on — `pct exec`, cloud-init, CI — the answers
are the `--postcode`, `--house-number`, `--addition` and `--api-key` flags, and
without those the example values are written and the first run says so.

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

`--ssh` may be left off: a download that fails falls back to ssh by itself when
root has a key to try. The fetch stops for nothing — an install reached through
a pipe has no terminal to answer a prompt on — so a key with a passphrase needs
an agent, and the host key has to be known already. When it fails, this says
what it is stuck on:

```sh
git ls-remote git@github.com:tmemelink/garbage-collection-automation.git
```

With a token instead — a fine-grained one, read access to this repository's
contents — https works for both halves:

```sh
export GITHUB_TOKEN=github_pat_...
curl -fsSL -H "Authorization: Bearer ${GITHUB_TOKEN}" \
  https://raw.githubusercontent.com/tmemelink/garbage-collection-automation/main/install.sh \
  | bash
```

The installer picks `GITHUB_TOKEN` up from the environment for its own download
too. Re-running it to upgrade needs it again: nothing it writes into the
container keeps a copy.

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


Set `GITHUB_TOKEN` if the repository is private and there is no key to use
instead. `APP_USER`, `INSTALL_DIR`, `CONFIG_DIR`, `STATE_DIR`, `LOG_DIR`,
`HOME_CMD_DIR` (the folder the by-hand commands go in, `~/garbage-collection` by
default), `HOME_CMD`, `HOME_WEB_CMD`, `REPO` and `SSH_URL` (the address `--ssh`
fetches from, `git@github.com:$REPO.git` by default) can be overridden through
the environment.

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


`/etc/garbage-collection-automation/` and both files in it belong to `root`, and
only `root` may create or remove anything there — so an editor run as root meets
an ordinary root-owned file, and the service user cannot put anything next to the
file holding the tokens. The one concession the web interface asks for is that
`config.toml` is group-writable by the service user (mode `0660`, group `gca`),
which is what its *Save configuration* button needs; `--no-web` installs it `0640`
and nothing but root writes it. The installer checks at the end that root really
can write both files, and says so with the kernel's own words when it cannot.

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

The API key is a fixed key the public afvalwijzer clients all send — not
per-user, but not ours to hardcode either. A run without one stops with exit
code 4.

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

To change the schedule, edit `/etc/cron.d/garbage-collection-automation`. The
change is live within a minute - cron re-reads that directory on every tick, so
there is nothing to restart, and restarting cron by hand next to a daemon the
init system does not track is how you end up with `cron: can't lock /var/run/crond.pid`. Reinstalling rewrites that file from `scheduling/*.cron`,
so persistent changes belong in the repository template.

### Web interface

A single local page showing what the last run found, the delta it would apply,
and the configuration the headless run reads — every key of it, in a form that
writes it back, next to `config.toml` itself as it is on disk. It is off until
asked for — in `config.toml`:

```toml
[web]
enabled = true
port = 8080
```

```sh
systemctl restart garbage-collection-automation-web
journalctl -u garbage-collection-automation-web -f    # the access log lives here
```

The service is the copy that comes back after a reboot. To watch the page answer
instead of reading the journal afterwards, serve it on your own terminal: the
command below runs in the foreground until ctrl-c, as the `gca` user, against the
same config file, `state.json` and `ui/` the service uses. Only one of the two can
hold the port, so it stops and says so while the service is running, and anything
you pass it is handed to the server.

```sh
systemctl stop garbage-collection-automation-web
~/garbage-collection/run-web-interface.sh
```

With `[web] enabled = false` it prints that and exits straight away — the switch
in `config.toml` is the same one either way.

The server binds **the loopback interface only**, and the configuration accepts
nothing else: the page has no login and shows both secrets. Reach it over an ssh
tunnel, which authenticates the person the page cannot:

```sh
ssh -N -L 8080:127.0.0.1:8080 root@<container>                       # direct
ssh -N -J root@<proxmox-host> -L 8080:127.0.0.1:8080 root@<container> # via Proxmox
```

Then open [http://127.0.0.1:8080/](http://127.0.0.1:8080/). `curl http://127.0.0.1:8080/healthz` is the
quickest way to tell a broken tunnel from a stopped server.

The local end of the tunnel is free to be another port when 8080 is taken —
`-L 8081:127.0.0.1:8080`, then open `http://127.0.0.1:8081/`. `localhost` works
in place of `127.0.0.1` as well; what the endpoints refuse is a name that merely
resolves here.

The buttons run the same pipeline the weekly job runs, and differ only in how
far they are allowed to get:


| Button                        | Endpoint            | Reads              | Writes                            |
| ----------------------------- | ------------------- | ------------------ | --------------------------------- |
| *Collect now*                 | `POST /api/gather`  | mijnafvalwijzer.nl | nothing — a dry run               |
| *Check Todoist*               | `POST /api/check`   | also Todoist       | nothing                           |
| *Apply delta*                 | `POST /api/apply`   | also Todoist       | the to-dos, and `state.json`      |
| *Save configuration*          | `POST /api/config`  | the form           | `config.toml`, rewritten in place |
| *Switch off and stop*         | `POST /api/stop`    | the form           | `[web] enabled = false`, then ends the process |


`GET /api/state` returns the configuration, the file it came from, the last run
and the cron line without touching the network — it is what the page draws
itself from.

The three run actions take the same lock the cron job takes; a lock they cannot
have is answered **409 immediately, never a wait**. The schedule is shown but
never written — change it over ssh.

#### The configuration on the page

The form covers every key in the table under [Configuration](#configuration) —
`[web]` and `[logging]` included — and saving asks first, listing what is about
to change. The whole document is re-rendered on a save, so a comment you added
by hand is not written back; the annotations in the file are the ones
`configuration.render()` writes.

`[web]` is the one section a save cannot make true of the server answering the
request: the address and port were bound at startup, so those three take effect
at the next `systemctl restart garbage-collection-automation-web`.

Below the table the page shows `config.toml` as it actually is on disk, comments
and hand edits and all, with both secrets replaced by `••••••••` and the last
few characters — enough to tell one key from another, and safe to screenshot.
The form's own two fields still hold the real values behind a reveal button;
that is the one place either appears. A file too broken to parse is not shown at
all, since nothing can then tell a secret in it from a setting.

#### Switching it off

*Switch off and stop* is the page putting itself away: it writes
`[web] enabled = false` and then ends the process, in that order, so a write
that is refused leaves the server up and able to say why. The exit is `0`, which
is not a failure, so `Restart=on-failure` leaves it stopped; the key in the file
is what keeps it stopped across a reboot, where the unit still starts, reads it
and exits.

Getting it back is an edit and a start on the machine itself:

```sh
sed -i '/^\[web\]/,/^\[/ s/^enabled = false/enabled = true/' \
    /etc/garbage-collection-automation/config.toml
systemctl start garbage-collection-automation-web
```

The job is untouched either way: cron still runs it, and nothing about the
to-dos depends on the page being served.

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

`src/run-job.sh` picks its layout from where it sits. To skip it and call the
CLI directly, pass `--state` yourself:

```sh
uv run garbage-collection-automation --config config/config.toml --state .local/state.json --dry-run
uv run garbage-collection-automation-web --config config/config.toml   # needs [web] enabled = true
```

`./build.sh --list` shows the build targets (`lxc` today, `docker` planned).
`config/config.toml`, `config/env` and `.local/` are gitignored and never end up
in a bundle.

### Tests

```sh
uv run pytest
uv run pytest --cov --cov-report=term-missing
```

No test touches the network: captured mijnafvalwijzer.nl responses in
`tests/fixtures/` are replayed through `httpx.MockTransport`, Todoist answers
from a table of canned replies, and a fixture in `conftest.py` fails any test
that tries to reach a host other than the loopback one the web tests serve. The suite also covers the shell — the wrapper's two layouts,
the installer's file handling, and the contents of the bundle `./build.sh lxc`
produces.

### Pre-commit hooks

What runs before a commit is written lives in
[.pre-commit-config.yaml](.pre-commit-config.yaml), with the Python rules in the
`[tool.ruff]` section of `pyproject.toml`. Install it once per checkout — the
line above in the setup block — after which it is automatic:

```sh
uv run pre-commit install          # once; writes .git/hooks/pre-commit
uv run pre-commit run --all-files  # everything now, without committing
uv run pre-commit autoupdate       # move the pinned hook versions forward
```

Ten things are checked, roughly in the order they can save you time:


| Check                                  | What it stops                                                                                                                                            |
| -------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `ruff check --fix`                     | Undefined names, unused imports, import order, comprehensions written the long way, syntax older than the 3.14 this targets, and bandit's security rules |
| `ruff format`                          | Arguments about layout. The width is 100 — what this repository already writes, rather than ruff's default 88                                            |
| `uv-lock`                              | A `pyproject.toml` edit without the `uv.lock` to match; an install resolves against the lock, so the container would build something the tests never ran |
| `shellcheck`                           | Unquoted variables and typo'd tests in a thousand lines of bash — one file of which is piped into a root shell over the network                          |
| `detect-secrets`, `detect-private-key` | An API key, a Todoist token or a private key pasted into a config example, a test or a captured fixture                                                  |
| `check-toml` / `-json` / `-yaml`       | A syntax error in config the application parses at startup, or in a fixture the tests replay: a failed run at 04:00 rather than a failed test            |
| `check-added-large-files`              | Build output, a virtualenv, a coverage database — anything over 1.5 MB. `dist/` is gitignored, but `git add -f` is one keystroke away                    |
| shebang / executable bit               | A script that cron, systemd or the installer invokes by path, committed without its executable bit — a job that silently never runs                      |
| merge conflict / case conflict         | Markers left in a file, and names that collide only on someone else's filesystem                                                                         |
| whitespace / EOF / line endings        | Trailing whitespace and missing final newlines, which turn a one-line diff into a page of them, and CRLF in files shipped to a Debian container          |


The hooks only ever see **staged** files, so `config/config.toml` and
`config/env` — the two gitignored files that hold the real secrets — are never
scanned or rewritten. The ones that fix rather than report (both ruff hooks, the
whitespace ones) stop the commit after editing, so you can read what changed and
stage it; committing again then goes through.

`detect-secrets` compares against [.secrets.baseline](.secrets.baseline), which
holds the hashes of the placeholder tokens the tests use. It stops a commit for
two different reasons, and they want different things from you.

*The baseline file was updated* is bookkeeping. A placeholder moved lines, so the
hook rewrote the baseline and stopped rather than commit a stale one. Read the
diff — it should touch nothing but `line_number` and `generated_at` — then stage
it and commit again:

```sh
git add .secrets.baseline
```

*Potential secrets about to be committed* is a decision, and here the baseline is
left alone. If the finding is real, take it out of the file. If it is a fixture
or an example, the inline pragma is the cheaper fix — it keeps the reason next to
the line it excuses, and leaves the baseline untouched:

```python
TOKEN = "not-a-real-token"  # pragma: allowlist secret
```

Regenerating the baseline is the heavier option, and the obvious command is the
wrong one. A bare `detect-secrets scan` walks only what git already tracks, so on
a checkout whose files are still untracked it finds nothing and writes an empty
baseline — after which every placeholder in the tests reads as a new secret.
Naming files narrows it the same way: the results block is replaced, not merged.
So scan everything, and repeat the exclusions, which live in the baseline and are
therefore only as good as the last command that wrote it. Without them the
gitignored files that hold the real secrets get hashed into a file you are about
to commit:

```sh
uv run detect-secrets scan --all-files --baseline .secrets.baseline \
  --exclude-files '^\.venv/' --exclude-files '^\.git/' --exclude-files '^dist/' \
  --exclude-files '^\.local/' --exclude-files '^\.pytest_cache/' \
  --exclude-files '^\.ruff_cache/' --exclude-files '^\.env$' \
  --exclude-files '^config/config\.toml$' --exclude-files '^config/env$' \
  --exclude-files '^uv\.lock$'   # then read the diff before staging it
```

## Security

Please report vulnerabilities privately; see [SECURITY.md](SECURITY.md). Never
put a live token, API key, address or other private data in a public issue.

## License

[Apache 2.0](LICENSE)
