/*
 * The page, driven from the endpoints in src/garbage_collection_automation/api.py.
 *
 * index.html is a shell: this fills it. Nothing here decides anything the server
 * has not already decided - the statuses, the delta, the summary sentence and
 * the log lines all arrive shaped, and this only puts them where they go. That
 * is deliberate: the answers a person acts on should come from the code that
 * runs the job, not from a second, slightly different opinion written in
 * javascript.
 *
 * Every marked-up shape is a <template> at the bottom of index.html rather than
 * a string here, so the stylesheet's class names appear exactly once.
 */

const el = (id) => document.getElementById(id);
const clone = (id) => el(id).content.firstElementChild.cloneNode(true);

/*
 * The three buttons, in order of how much each one changes. The key is the
 * button's id in index.html; the endpoint is the server's route, and the two
 * differ for the first one on purpose: a path ending in /collect is what
 * content blockers drop as a Google Analytics beacon, so the request never
 * leaves the browser and the page can only say "NetworkError" about a server
 * that is running perfectly well. See ROUTES in web.py.
 */
const ACTIONS = {
  collect: { endpoint: "/api/gather", running: "Collecting…" },
  check: { endpoint: "/api/check", running: "Asking Todoist…" },
  apply: { endpoint: "/api/apply", running: "Applying…" },
};

/* What api.py puts in a row's "state", and how the table says it. */
const ROW_STATES = {
  synced: { label: "synced", modifier: "status--synced" },
  add: { label: "to add", modifier: "status--add" },
  rewrite: { label: "to rewrite", modifier: "status--rewrite" },
  remove: { label: "to remove", modifier: "status--remove" },
  pending: { label: "not exported", modifier: "" },
};

/* All set from /api/state: what the server will and will not accept a save of. */
let tokenIsFromTheEnvironment = false;
let apiKeyIsFromTheEnvironment = false;
let configIsWritable = true;

/*
 * The configuration as the server last handed it over, which is the file as it
 * was on disk at that moment. Kept so a save can say what it is about to
 * change: the confirmation is worth reading only if it names the differences,
 * and this is the only copy of the "before" the page has.
 */
let onDisk = {};

/* Every field the form posts, and what the confirmation calls it. */
const FIELD_LABELS = {
  postcode: "Postcode",
  house_number: "House number",
  addition: "Addition",
  api_key: "mijnafvalwijzer API key",
  timeout_seconds: "Timeout (seconds)",
  retries: "Retries",
  lookahead_days: "Look ahead (days)",
  due_time: "Due time",
  types: "Waste types",
  todoist_enabled: "Export to Todoist",
  todoist_token: "Todoist API token",
  todoist_project: "Todoist project",
  todoist_section: "Todoist section",
  remind_days_before: "Remind (days before)",
  web_enabled: "Serve this page",
  web_host: "Web host",
  web_port: "Web port",
  logging_level: "Log level",
};

/*
 * The three the running server cannot be talked out of: it bound its address
 * and port at startup, and the file is only what the next start will read.
 */
const RESTART_FIELDS = ["web_enabled", "web_host", "web_port"];

/* Changed, not to what: a confirmation is not a place to print either secret. */
const SECRET_FIELDS = ["api_key", "todoist_token"];

// --- talking to the server ------------------------------------------------------------

/*
 * How long a request may take before the page gives up on it. An action holds
 * the connection for the whole run - the schedule request and its retries, then
 * whatever Todoist needs - so this is minutes rather than seconds. It is not a
 * deadline for the run, which the server finishes either way: it is what keeps a
 * server that never answers from leaving the three buttons disabled until
 * someone thinks to reload the page.
 */
const TIMEOUT_MS = 180_000;

/*
 * How quickly a failure has to come back to be a connection that was already
 * dead rather than a run that fell over. A browser holds connections open in
 * the hope of reusing them, and one of those can be gone - a tunnel restarted,
 * a server upgraded - without anything having said so; writing into it fails in
 * the time it takes to notice, before the request has reached anyone. That one
 * is worth trying again on a new connection. A failure minutes into a run is
 * not: something took the answer away mid-flight, and the run itself may well
 * have happened.
 */
const DEAD_SOCKET_MS = 1000;

/* One attempt, with the page's own deadline on it. */
function send(path, body) {
  return fetch(path, {
    method: body === undefined ? "GET" : "POST",
    headers: body === undefined ? {} : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
    signal: AbortSignal.timeout(TIMEOUT_MS),
  });
}

/* A failed fetch, said the way the page says things. */
function unreachable(error) {
  return new Error(
    error.name === "TimeoutError"
      ? "the server did not answer in time; the run may still be going on - reload to see"
      : `the server could not be reached: ${error.message}`,
  );
}

/*
 * One request, and one place errors turn into something a person can read.
 * Every endpoint answers json, including its refusals, so a failure here is a
 * server that fell over rather than a request that was refused.
 */
async function ask(path, body) {
  let response;
  const started = performance.now();
  try {
    response = await send(path, body);
  } catch (error) {
    if (error.name === "TimeoutError" || performance.now() - started > DEAD_SOCKET_MS) {
      throw unreachable(error);
    }
    try {
      /* On a connection this one opens, the last one having turned out to be
         a socket nobody was listening on any more. */
      response = await send(path, body);
    } catch (again) {
      throw unreachable(again);
    }
  }

  let payload = null;
  try {
    payload = await response.json();
  } catch {
    throw new Error(`the server answered ${response.status} without saying why`);
  }
  if (!response.ok) throw new Error(payload.error || `the server answered ${response.status}`);
  return payload;
}

// --- the page's own state -------------------------------------------------------------

async function load() {
  let state;
  try {
    state = await ask("/api/state");
  } catch (error) {
    setStatus("error", "unreachable");
    el("headline").textContent = `The server could not be asked: ${error.message}`;
    return;
  }

  fillConfig(state.config);
  fillConfigFile(state.config_file);
  fillSchedule(state.schedule);
  fillLastExport(state.last_run);
}

/*
 * `describe` is what tells the summary above the table what the record holds.
 * A refresh after a run leaves it alone: what that run just did is newer news
 * than the file it wrote, and is already on the page.
 */
function fillLastExport(lastRun, { describe = true } = {}) {
  el("state_updated").textContent = lastRun.updated_at
    ? momentText(lastRun.updated_at)
    : "never";

  if (lastRun.address) {
    el("address").textContent = lastRun.address;
    el("address").hidden = false;
  }
  if (!describe) return;
  if (!lastRun.known) {
    el("headline").textContent = "Nothing exported yet";
    el("reason").textContent = "No run has been recorded here. Press a button above to start one.";
    return;
  }
  if (!lastRun.complete) {
    el("headline").textContent = "The last export stopped halfway";
    el("reason").textContent =
      `${lastRun.tasks.length} todo(s) were recorded, but Todoist may hold more. ` +
      "The next run asks Todoist itself what is there and finishes the job.";
    return;
  }
  el("headline").textContent = `${lastRun.tasks.length} todo(s) recorded from an earlier run`;
  el("reason").textContent = "Press a button above to see what a run would do now.";
}

// --- the configuration form -----------------------------------------------------------

function fillConfig(config) {
  onDisk = config;

  for (const name of [
    "postcode",
    "house_number",
    "addition",
    "timeout_seconds",
    "retries",
    "lookahead_days",
    "due_time",
    "todoist_project",
    "todoist_section",
    "remind_days_before",
    "web_host",
    "web_port",
  ]) {
    el(name).value = config[name];
  }
  el("todoist_enabled").checked = config.todoist_enabled;
  el("web_enabled").checked = config.web_enabled;
  el("todoist_token").value = config.todoist_token;
  el("api_key").value = config.api_key;
  el("config_path").textContent = config.config_path;

  fillTypes(config.known_types, config.types);
  fillLevels(config.known_levels, config.logging_level);

  /* Either secret may come from the environment, which wins over the file. */
  tokenIsFromTheEnvironment = config.token_from_environment;
  el("token_hint").hidden = !tokenIsFromTheEnvironment;
  el("todoist_token").readOnly = tokenIsFromTheEnvironment;

  apiKeyIsFromTheEnvironment = config.api_key_from_environment;
  el("api_key_hint").hidden = !apiKeyIsFromTheEnvironment;
  el("api_key").readOnly = apiKeyIsFromTheEnvironment;

  /* The installer decides whether this file is the service user's to write.
     Stopping writes it too - it is [web] enabled turned off - so a file this
     server may not write leaves both buttons out of reach, not just the save. */
  configIsWritable = config.writable;
  el("save").disabled = !configIsWritable;
  el("stop").disabled = !configIsWritable;
  if (!configIsWritable) {
    showConfigError(`${config.config_path} is not writable by the server; edit it there.`);
  }
}

/*
 * The file the form is a reading of, shown as it actually is: comments, hand
 * edits, the sections the form has no field for. api.py masks the secrets in it
 * before it is sent, so this panel is the one part of the page that can be
 * screenshotted without a thought.
 */
function fillConfigFile(file) {
  el("config_file_text").textContent = file.text ?? file.error;
  el("config_file_meta").textContent = describeFile(file);
}

function describeFile(file) {
  /* Either it could not be read, or it could not be parsed and so could not be
     masked; the panel itself carries the server's sentence about which. */
  if (file.text === null) return "not shown";
  const notes = [file.path];
  /* The server's own clock, written the way it sent it: this is a file's mtime
     on that machine, not a moment to re-read in the reader's timezone. */
  if (file.modified_at) notes.push(`saved ${file.modified_at.replace("T", " ")}`);
  if (file.masked) notes.push("secrets masked");
  if (file.truncated) notes.push("shown in part");
  if (!file.writable) notes.push("read-only");
  return notes.join("  \u00b7  ");
}

function fillLevels(known, chosen) {
  const select = el("logging_level");
  select.replaceChildren();
  for (const level of known) {
    const option = document.createElement("option");
    option.value = level;
    option.textContent = level;
    select.append(option);
  }
  select.value = chosen;
}

function fillTypes(known, chosen) {
  const container = el("types");
  container.replaceChildren();
  for (const { code, label } of known) {
    const node = clone("type-template");
    node.dataset.type = code;
    const box = node.querySelector("input");
    box.value = code;
    box.checked = chosen.includes(code);
    node.querySelector(".switch__label").textContent = label;
    container.append(node);
  }
}

function fillSchedule(schedule) {
  el("cron").value = schedule.cron || "not installed";
  el("cron_hint").textContent = schedule.cron
    ? `from ${schedule.path}, which only root may change`
    : `no schedule installed; cron reads ${schedule.path}`;
}

function formPayload() {
  const payload = {
    postcode: el("postcode").value.trim(),
    house_number: el("house_number").value.trim(),
    addition: el("addition").value.trim(),
    timeout_seconds: Number(el("timeout_seconds").value),
    retries: Number(el("retries").value),
    lookahead_days: Number(el("lookahead_days").value),
    due_time: el("due_time").value,
    types: [...document.querySelectorAll("#types input:checked")].map((box) => box.value),
    todoist_enabled: el("todoist_enabled").checked,
    todoist_project: el("todoist_project").value,
    todoist_section: el("todoist_section").value.trim(),
    remind_days_before: Number(el("remind_days_before").value),
    web_enabled: el("web_enabled").checked,
    web_host: el("web_host").value.trim(),
    web_port: Number(el("web_port").value),
    logging_level: el("logging_level").value,
  };
  /* Sending these would be refused anyway; not sending them says so without asking. */
  if (!tokenIsFromTheEnvironment) payload.todoist_token = el("todoist_token").value;
  if (!apiKeyIsFromTheEnvironment) payload.api_key = el("api_key").value;
  return payload;
}

/*
 * What this form would change about the file, as a list a person can read
 * before saying yes. The comparison is against the last answer the server gave,
 * which is the file as it was then - not against the form's own defaults.
 */
function changed(payload) {
  const listed = [];
  for (const [field, value] of Object.entries(payload)) {
    if (sameValue(field, value, onDisk[field])) continue;
    listed.push(
      /* "replaced" rather than the new one: a secret is not printed here even
         though the field above it holds the same characters. */
      SECRET_FIELDS.includes(field)
        ? { field, was: "", now: value ? "replaced" : "cleared" }
        : { field, was: valueText(onDisk[field]), now: valueText(value) },
    );
  }
  return listed;
}

function sameValue(field, value, was) {
  /* The switches are in the order the server lists the streams, which is not
     always the order the file has them in; that difference is not a change. */
  if (field === "types") return String([...value].sort()) === String([...(was ?? [])].sort());
  return value === was;
}

function valueText(value) {
  if (typeof value === "boolean") return value ? "on" : "off";
  if (Array.isArray(value)) return value.join(", ") || "none";
  return value === "" ? "(empty)" : String(value);
}

async function save(event) {
  event.preventDefault();
  showConfigError(null);

  const payload = formPayload();
  const changes = changed(payload);
  const restarting = changes.some((change) => RESTART_FIELDS.includes(change.field));
  const agreed = await confirmed({
    title: "Overwrite the configuration?",
    text:
      (changes.length
        ? `This rewrites ${onDisk.config_path}, the file the scheduled run reads.`
        : `Nothing on this form differs from the file. Saving rewrites ` +
          `${onDisk.config_path} anyway.`) +
      " The whole document is re-rendered, so any comment or key you added to it" +
      " by hand and the page has no field for is not written back.",
    changes,
    note: restarting
      ? "[web] is read when the server starts. This one keeps the address and " +
        "port it is already listening on until it is restarted."
      : null,
    label: "Save",
  });
  if (!agreed) return;

  const button = el("save");
  button.disabled = true;
  button.textContent = "Saving…";
  try {
    const saved = await ask("/api/config", payload);
    fillConfig(saved.config);
    fillConfigFile(saved.config_file);
    button.textContent = "Saved";
    setTimeout(() => (button.textContent = "Save configuration"), 1500);
  } catch (error) {
    showConfigError(error.message);
    button.textContent = "Save configuration";
  } finally {
    button.disabled = !configIsWritable;
  }
}

// --- switching the page off -----------------------------------------------------------

/*
 * The last thing this page does. The server writes [web] enabled = false, then
 * answers, then stops - so this request succeeds and the next one would not
 * connect at all. Nothing here reloads or retries afterwards for that reason:
 * there is no server left to ask, and the takeover below is the whole ending.
 */
async function switchOff() {
  const agreed = await confirmed({
    title: "Switch the page off and stop it?",
    text:
      `This writes enabled = false under [web] in ${onDisk.config_path} - ` +
      "re-rendering the whole file, exactly as a save does - and then stops the " +
      "server. The job keeps running from cron; only the page goes away.",
    note:
      "Bringing it back means editing that file on the machine itself and " +
      "starting the service - there will be no page here to do it from.",
    label: "Switch off and stop",
  });
  if (!agreed) return;

  const button = el("stop");
  button.disabled = true;
  button.textContent = "Stopping…";
  try {
    const answer = await ask("/api/stop", {});
    sayGoodbye(answer.config.config_path);
  } catch (error) {
    showConfigError(error.message);
    button.textContent = "Switch off and stop the server";
    button.disabled = !configIsWritable;
  }
}

function sayGoodbye(configPath) {
  el("farewell_path").textContent = configPath;
  document.querySelector("main").hidden = true;
  el("status").hidden = true;
  el("farewell").hidden = false;
}

// --- asking first ---------------------------------------------------------------------

/*
 * Both buttons that write config.toml go through here. The file belongs to a
 * job that runs unattended at four in the morning, and neither "save" nor
 * "stop" is something to do by brushing past a button.
 *
 * Resolves true when the person said yes. Escape is a no, which is why the
 * return value is set before the dialog opens rather than trusted from last time.
 */
function confirmed({ title, text, changes = [], note = null, label }) {
  const dialog = el("confirm");
  el("confirm_title").textContent = title;
  el("confirm_text").textContent = text;
  el("confirm_ok").textContent = label;

  const list = el("confirm_list");
  list.replaceChildren();
  for (const change of changes) {
    const node = clone("change-template");
    node.querySelector(".modal__field").textContent = `${FIELD_LABELS[change.field]}:`;
    node.querySelector(".modal__was").textContent = change.was;
    node.querySelector(".modal__now").textContent = change.now;
    list.append(node);
  }

  el("confirm_note").textContent = note ?? "";
  el("confirm_note").hidden = note === null;

  dialog.returnValue = "cancel";
  dialog.showModal();
  return new Promise((resolve) => {
    dialog.addEventListener("close", () => resolve(dialog.returnValue === "confirm"), {
      once: true,
    });
  });
}

function showConfigError(message) {
  const notice = el("config_error");
  notice.textContent = message || "";
  notice.hidden = !message;
}

// --- the three buttons ----------------------------------------------------------------

async function act(name) {
  const { endpoint, running } = ACTIONS[name];
  const buttons = Object.keys(ACTIONS).map(el);
  const label = el(name).textContent;

  buttons.forEach((button) => (button.disabled = true));
  el(name).textContent = running;
  setStatus("running", "running");
  try {
    const answer = await ask(endpoint, {});
    showResult(answer.result);
    showLog(answer.log);
    /* A run that wrote something rewrote the record the "Last export" line is
       drawn from; the server says which kind of run it was. */
    if (answer.result.ok && !answer.result.dry_run) await refreshLastExport();
  } catch (error) {
    setStatus("error", "failed");
    el("headline").textContent = error.message;
    el("reason").textContent = "";
  } finally {
    buttons.forEach((button) => (button.disabled = false));
    el(name).textContent = label;
  }
}

/*
 * The "Last export" line, redrawn from the record the run just wrote - and only
 * that line, not the whole page: reloading the form here would throw away
 * whatever is typed into it and not yet saved, and the table and the console
 * already hold what the run itself answered with.
 */
async function refreshLastExport() {
  try {
    const state = await ask("/api/state");
    fillLastExport(state.last_run, { describe: false });
  } catch {
    /* The summary already says what the run did; a stale timestamp next to it
       is not worth replacing that with an error. */
  }
}

function showResult(result) {
  setStatus(result.ok ? "ok" : "error", result.ok ? "OK" : result.status);
  el("headline").textContent = result.summary;
  el("reason").textContent = result.decision
    ? `todoist ${result.queried ? "queried" : "not queried"}: ${result.decision.reason}`
    : "";

  if (result.address) {
    el("address").textContent = result.address;
    el("address").hidden = false;
  }
  showRows(result.rows);
  showDelta(result.delta);
}

function showRows(rows) {
  const body = el("rows");
  body.replaceChildren();
  for (const row of rows) {
    const node = clone("row-template");
    node.querySelector(".cell-date").textContent = dateText(row.date);
    node.querySelector('[data-field="weekday"]').textContent = row.weekday;
    node.querySelector('[data-field="due"]').textContent = timeText(row.due_at);
    node.querySelector('[data-field="remind"]').textContent = row.remind_at
      ? momentText(row.remind_at)
      : "–";
    node.querySelector(".cell-id").textContent = row.task_id || "–";

    const waste = node.querySelector(".waste");
    waste.classList.add(`waste--${row.code}`);
    waste.textContent = row.waste_type;

    const state = ROW_STATES[row.state] || ROW_STATES.pending;
    const status = node.querySelector(".status");
    if (state.modifier) status.classList.add(state.modifier);
    status.textContent = state.label;

    body.append(node);
  }
  el("rows-empty").hidden = rows.length > 0;
}

function showDelta(delta) {
  el("delta").hidden = delta === null;
  if (delta === null) return;

  for (const part of ["create", "update", "delete"]) {
    const items = delta[part];
    el("delta").querySelector(`[data-count="${part}"]`).textContent = items.length;
    el("delta").querySelector(`[data-empty="${part}"]`).hidden = items.length > 0;

    const list = el("delta").querySelector(`[data-list="${part}"]`);
    list.replaceChildren();
    for (const item of items) {
      const node = clone("delta-item-template");
      node.querySelector(".delta__date").textContent = dateText(item.date);
      const waste = node.querySelector(".waste");
      waste.classList.add(`waste--${item.code}`);
      waste.textContent = item.waste_type;
      list.append(node);
    }
  }
}

function showLog(lines) {
  const console_ = el("console");
  console_.replaceChildren();
  for (const line of lines) {
    const node = clone("log-template");
    node.querySelector(".log-time").textContent = `[${line.time.replace("T", " ")}]`;
    const level = node.querySelector(".log-level");
    level.classList.add(`log-level--${line.level.toLowerCase()}`);
    level.textContent = line.level;
    node.querySelector(".log-message").textContent = line.message;
    console_.append(node, "\n");
  }
}

function setStatus(kind, text) {
  const badge = el("status");
  badge.className = `badge badge--${kind}`;
  badge.textContent = text;
  badge.hidden = false;
}

// --- dates, in the reader's format but the collection round's timezone ----------------

/*
 * The reader's locale decides how a date is written; it does not get to decide
 * which moment is meant. A collection at 07:00 is 07:00 at the kerb in Voorbeeldstad -
 * data_processing.TIMEZONE, the same constant the job builds the due moment
 * from - so a browser in London must not turn that into 06:00. Plain dates are
 * parsed as UTC midnight by every engine, and this offset is always ahead of
 * it, so pinning the zone keeps those on the right day too.
 */
const ZONE = "Europe/Amsterdam";

const dateText = (iso) =>
  new Date(iso).toLocaleDateString(undefined, {
    day: "numeric",
    month: "short",
    year: "numeric",
    timeZone: ZONE,
  });

const timeText = (iso) =>
  new Date(iso).toLocaleTimeString(undefined, {
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
    timeZone: ZONE,
  });

const momentText = (iso) =>
  new Date(iso).toLocaleString(undefined, {
    day: "numeric",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
    timeZone: ZONE,
  });

// --- wiring ---------------------------------------------------------------------------

/*
 * The one thing markup and CSS cannot do on their own: turn a secret field from
 * dots into text. The switches and the collapsing console are a checkbox and a
 * <details>, and still work if this file never loads.
 */
document.querySelectorAll("[data-reveal]").forEach((button) => {
  const field = el(button.dataset.reveal);
  if (!field) return;

  /* "Show token", "Show API key": the markup's label names what is revealed. */
  const noun = (button.getAttribute("aria-label") || "Show value").replace(/^\S+\s+/, "");
  button.addEventListener("click", () => {
    const hidden = field.type === "password";
    field.type = hidden ? "text" : "password";
    button.setAttribute("aria-pressed", String(hidden));
    button.setAttribute("aria-label", `${hidden ? "Hide" : "Show"} ${noun}`);
  });
});

for (const name of Object.keys(ACTIONS)) {
  el(name).addEventListener("click", () => act(name));
}
el("config").addEventListener("submit", save);
el("stop").addEventListener("click", switchOff);

/* The dialog's own two buttons. They are not a <form method="dialog"> because
   the page is served under a Content-Security-Policy that names form-action. */
el("confirm_ok").addEventListener("click", () => el("confirm").close("confirm"));
el("confirm_cancel").addEventListener("click", () => el("confirm").close("cancel"));

load();
