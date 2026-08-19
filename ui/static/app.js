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

/* The three buttons, in order of how much each one changes. */
const ACTIONS = {
  collect: { endpoint: "/api/collect", running: "Collecting…" },
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
 * One request, and one place errors turn into something a person can read.
 * Every endpoint answers json, including its refusals, so a failure here is a
 * server that fell over rather than a request that was refused.
 */
async function ask(path, body) {
  let response;
  try {
    response = await fetch(path, {
      method: body === undefined ? "GET" : "POST",
      headers: body === undefined ? {} : { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: AbortSignal.timeout(TIMEOUT_MS),
    });
  } catch (error) {
    throw new Error(
      error.name === "TimeoutError"
        ? "the server did not answer in time; the run may still be going on - reload to see"
        : `the server could not be reached: ${error.message}`,
    );
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
  for (const name of [
    "postcode",
    "house_number",
    "addition",
    "lookahead_days",
    "due_time",
    "todoist_project",
    "remind_days_before",
  ]) {
    el(name).value = config[name];
  }
  el("todoist_enabled").checked = config.todoist_enabled;
  el("todoist_token").value = config.todoist_token;
  el("api_key").value = config.api_key;
  el("config_path").textContent = config.config_path;

  fillTypes(config.known_types, config.types);

  /* Either secret may come from the environment, which wins over the file. */
  tokenIsFromTheEnvironment = config.token_from_environment;
  el("token_hint").hidden = !tokenIsFromTheEnvironment;
  el("todoist_token").readOnly = tokenIsFromTheEnvironment;

  apiKeyIsFromTheEnvironment = config.api_key_from_environment;
  el("api_key_hint").hidden = !apiKeyIsFromTheEnvironment;
  el("api_key").readOnly = apiKeyIsFromTheEnvironment;

  /* The installer decides whether this file is the service user's to write. */
  configIsWritable = config.writable;
  el("save").disabled = !configIsWritable;
  if (!configIsWritable) {
    showConfigError(`${config.config_path} is not writable by the server; edit it there.`);
  }
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
    lookahead_days: Number(el("lookahead_days").value),
    due_time: el("due_time").value,
    types: [...document.querySelectorAll("#types input:checked")].map((box) => box.value),
    todoist_enabled: el("todoist_enabled").checked,
    todoist_project: el("todoist_project").value,
    remind_days_before: Number(el("remind_days_before").value),
  };
  /* Sending these would be refused anyway; not sending them says so without asking. */
  if (!tokenIsFromTheEnvironment) payload.todoist_token = el("todoist_token").value;
  if (!apiKeyIsFromTheEnvironment) payload.api_key = el("api_key").value;
  return payload;
}

async function save(event) {
  event.preventDefault();
  showConfigError(null);

  const button = el("save");
  button.disabled = true;
  button.textContent = "Saving…";
  try {
    const saved = await ask("/api/config", formPayload());
    fillConfig(saved.config);
    button.textContent = "Saved";
    setTimeout(() => (button.textContent = "Save configuration"), 1500);
  } catch (error) {
    showConfigError(error.message);
    button.textContent = "Save configuration";
  } finally {
    button.disabled = !configIsWritable;
  }
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

load();
