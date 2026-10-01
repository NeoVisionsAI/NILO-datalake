const app = document.querySelector("#app");
const sections = [
  ["general", "General"],
  ["schedule", "Schedule"],
  ["storage", "Storage"],
  ["mongo", "MongoDB"],
  ["minio", "MinIO"],
  ["http", "Backend API"],
  ["ssh", "SSH"],
  ["ingest", "Ingest"],
  ["edge", "Edge"],
  ["account", "Account"],
];

let state = null;
let section = "general";
let message = null;

boot();

async function boot() {
  const response = await fetch("/console/api/settings");
  if (response.status === 401) {
    renderLogin();
    return;
  }
  if (!response.ok) {
    renderLogin(await errorText(response));
    return;
  }
  state = await response.json();
  renderApp();
}

function renderLogin(error) {
  app.innerHTML = "";
  const wrap = el("div", { className: "login-wrap" });
  const card = el("form", { className: "card login" });
  card.append(
    el("h1", { textContent: "NILO archive" }),
    el("p", { className: "lede", textContent: "Sign in to change how this archive runs." }),
  );
  const user = input("text", "admin");
  const password = input("password", "");
  card.append(field("Username", user), field("Password", password));
  if (error) card.append(el("p", { className: "error", textContent: error }));
  const button = el("button", { className: "primary", type: "submit", textContent: "Sign in" });
  card.append(button);
  card.addEventListener("submit", async (event) => {
    event.preventDefault();
    button.disabled = true;
    const response = await fetch("/console/api/login", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ username: user.value, password: password.value }),
    });
    button.disabled = false;
    if (!response.ok) {
      renderLogin(await errorText(response));
      return;
    }
    boot();
  });
  wrap.append(card);
  app.append(wrap);
}

function renderApp() {
  app.innerHTML = "";
  const shell = el("div", { className: "shell" });
  const nav = el("nav", { className: "nav" });
  nav.append(el("h1", { textContent: "NILO" }));
  nav.append(el("p", { className: "hint", textContent: state.settings.site_id }));
  for (const [id, label] of sections) {
    const button = el("button", { type: "button", textContent: label });
    if (id === section) button.className = "active";
    button.addEventListener("click", () => {
      section = id;
      renderApp();
    });
    nav.append(button);
  }
  const main = el("main", { className: "main" });
  const top = el("div", { className: "top" });
  top.append(
    el("div", {}, [
      el("h1", { textContent: sections.find((item) => item[0] === section)[1] }),
      el("p", { className: "lede", textContent: "Saved values replace the settings file. A blank secret keeps the one already stored." }),
    ]),
  );
  const actions = el("div", { className: "row" });
  actions.append(button("Save", "primary", save), button("Restart service", "ghost", restart), button("Sign out", "ghost", logout));
  top.append(actions);
  main.append(top);
  if (message) main.append(el("p", { className: message.kind, textContent: message.text }));
  const panel = el("section", { className: "panel" });
  panel.append(renderSection());
  main.append(panel);
  shell.append(nav, main);
  app.append(shell);
}

function renderSection() {
  const settings = state.settings;
  const root = el("div");
  if (section === "general") {
    root.append(
      field("Site id", text(settings.site_id, (value) => { settings.site_id = value; })),
      field("Log level", select(settings.log_level, ["DEBUG", "INFO", "WARNING", "ERROR"], (value) => { settings.log_level = value; })),
      field("Trace directory (empty uses the catalog directory)", text(settings.trace.directory || "", (value) => { settings.trace.directory = value || null; })),
    );
  }
  if (section === "schedule") {
    const cron = text(settings.pull.schedule, (value) => { settings.pull.schedule = value; });
    root.append(
      check("Pull from the backend on a schedule", settings.pull.enabled, (value) => { settings.pull.enabled = value; }),
      field("Cron (minute hour day month weekday)", cron),
      el("div", { className: "row" }, [
        button("Daily at 02:00", "ghost", () => { settings.pull.schedule = "0 2 * * *"; renderApp(); }),
        button("Hourly", "ghost", () => { settings.pull.schedule = "0 * * * *"; renderApp(); }),
        button("Every 15 minutes", "ghost", () => { settings.pull.schedule = "*/15 * * * *"; renderApp(); }),
      ]),
      field("Timezone", text(settings.pull.timezone, (value) => { settings.pull.timezone = value; })),
      field("Safety delay (seconds)", number(settings.pull.safety_delay_seconds, (value) => { settings.pull.safety_delay_seconds = value; })),
      check("Watch the rsync/scp inbox", settings.inbox.enabled, (value) => { settings.inbox.enabled = value; }),
      field("Inbox poll (seconds)", number(settings.inbox.poll_seconds, (value) => { settings.inbox.poll_seconds = value; })),
      field("Abandon unfinished uploads after (hours)", number(settings.inbox.abandon_after_hours, (value) => { settings.inbox.abandon_after_hours = value; })),
    );
  }
  if (section === "storage") {
    root.append(el("p", { className: "hint", textContent: "The first enabled disk is filled first. Sizes are in GiB." }));
    for (const volume of settings.storage.volumes) {
      const row = el("div", { className: "list-row" });
      row.append(
        labeled("Id", text(volume.id, (value) => { volume.id = value; })),
        labeled("Path", text(volume.root, (value) => { volume.root = value; })),
        labeled("Enabled", checkbox(volume.enabled, (value) => { volume.enabled = value; })),
        button("Remove", "danger", () => {
          settings.storage.volumes = settings.storage.volumes.filter((item) => item !== volume);
          renderApp();
        }),
      );
      root.append(row);
    }
    root.append(button("Add disk", "ghost", () => {
      settings.storage.volumes.push({ id: "disk" + (settings.storage.volumes.length + 1), root: "", enabled: false });
      renderApp();
    }));
    root.append(
      field("Reserve (GiB)", text(toGiB(settings.storage.reserve_bytes), (value) => { settings.storage.reserve_bytes = fromGiB(value); })),
      field("Minimum free space to open a batch (GiB)", text(toGiB(settings.storage.min_free_bytes), (value) => { settings.storage.min_free_bytes = fromGiB(value); })),
      field("Catalog path", text(settings.storage.catalog_path, (value) => { settings.storage.catalog_path = value; })),
    );
  }
  if (section === "mongo") {
    const login = state.mongo_login;
    root.append(
      check("Pull MongoDB", settings.pull.mongo.enabled, (value) => { settings.pull.mongo.enabled = value; }),
      field("Host", text(login.host, (value) => { login.host = value; })),
      field("Username", text(login.username, (value) => { login.username = value; })),
      secretField("Password", login.password, login.password_set, (value) => { login.password = value; }),
      field("Auth database", text(login.auth_source, (value) => { login.auth_source = value; })),
      field("Database", text(settings.pull.mongo.database, (value) => { settings.pull.mongo.database = value; })),
      button("Test connection", "ghost", () => testKind("mongo")),
    );
    root.append(el("h2", { textContent: "Collections" }));
    for (const collection of settings.pull.mongo.collections) {
      const row = el("div", { className: "list-row collection" });
      row.append(
        labeled("Name", text(collection.name, (value) => { collection.name = value; })),
        labeled("Timestamp field", text(collection.timestamp_field, (value) => { collection.timestamp_field = value; })),
        labeled("Text timestamp", checkbox(collection.timestamp_is_string, (value) => { collection.timestamp_is_string = value; })),
        labeled("Chunk", number(collection.chunk_size, (value) => { collection.chunk_size = value; })),
        button("Remove", "danger", () => {
          settings.pull.mongo.collections = settings.pull.mongo.collections.filter((item) => item !== collection);
          renderApp();
        }),
      );
      root.append(row);
    }
    root.append(button("Add collection", "ghost", () => {
      settings.pull.mongo.collections.push({ name: "", timestamp_field: "updated_at", timestamp_is_string: false, chunk_size: 2000 });
      renderApp();
    }));
  }
  if (section === "minio") {
    const minio = settings.pull.minio;
    root.append(
      check("Pull MinIO", minio.enabled, (value) => { minio.enabled = value; }),
      field("Endpoint (host:port)", text(minio.endpoint, (value) => { minio.endpoint = value; })),
      field("Access key", text(minio.access_key, (value) => { minio.access_key = value; })),
      secretField("Secret key", minio.secret_key, state.secrets_set.includes("pull.minio.secret_key"), (value) => { minio.secret_key = value; }),
      check("TLS", minio.secure, (value) => { minio.secure = value; }),
      field("Session prefix", text(minio.session_prefix, (value) => { minio.session_prefix = value; })),
      button("Test connection", "ghost", () => testKind("minio")),
    );
    for (const bucket of minio.buckets) {
      const row = el("div", { className: "list-row bucket" });
      row.append(
        labeled("Bucket", text(bucket.name, (value) => { bucket.name = value; })),
        labeled("Prefix", text(bucket.prefix, (value) => { bucket.prefix = value; })),
        button("Remove", "danger", () => {
          minio.buckets = minio.buckets.filter((item) => item !== bucket);
          renderApp();
        }),
      );
      root.append(row);
    }
    root.append(button("Add bucket", "ghost", () => {
      minio.buckets.push({ name: "", prefix: "" });
      renderApp();
    }));
  }
  if (section === "http") {
    const http = settings.pull.http;
    root.append(
      check("Pull the backend archive API", http.enabled, (value) => { http.enabled = value; }),
      field("Base URL", text(http.base_url, (value) => { http.base_url = value; })),
      secretField("API key", http.api_key, state.secrets_set.includes("pull.http.api_key"), (value) => { http.api_key = value; }),
      field("Page size", number(http.page_limit, (value) => { http.page_limit = value; })),
      field("Timeout (seconds)", number(http.timeout_seconds, (value) => { http.timeout_seconds = value; })),
      button("Test connection", "ghost", () => testKind("http")),
    );
  }
  if (section === "ssh") {
    const ssh = settings.pull.ssh;
    root.append(
      check("Pull sessions over SSH", ssh.enabled, (value) => { ssh.enabled = value; }),
      field("rsync binary", text(ssh.binary, (value) => { ssh.binary = value; })),
      field("SSH command", text(ssh.ssh_command, (value) => { ssh.ssh_command = value; })),
    );
    for (const source of ssh.sources) {
      const row = el("div", { className: "list-row ssh" });
      row.append(
        labeled("Name", text(source.name, (value) => { source.name = value; })),
        labeled("Remote", text(source.remote, (value) => { source.remote = value; })),
        labeled("Delete after copy", checkbox(source.delete_after, (value) => { source.delete_after = value; })),
        button("Remove", "danger", () => {
          ssh.sources = ssh.sources.filter((item) => item !== source);
          renderApp();
        }),
      );
      root.append(row);
    }
    root.append(button("Add source", "ghost", () => {
      ssh.sources.push({ name: "", remote: "", delete_after: false });
      renderApp();
    }));
  }
  if (section === "ingest") {
    const ingest = settings.ingest;
    root.append(
      check("Accept pushes", ingest.enabled, (value) => { ingest.enabled = value; }),
      field("Bind host", text(ingest.host, (value) => { ingest.host = value; })),
      field("Port", number(ingest.port, (value) => { ingest.port = value; })),
      secretField("API key", ingest.api_key, state.secrets_set.includes("ingest.api_key"), (value) => { ingest.api_key = value; }),
      el("p", { className: "hint", textContent: "Changing the host or port is saved immediately and used after Restart service." }),
    );
  }
  if (section === "edge") {
    const edge = settings.edge;
    root.append(
      field("Spool directory", text(edge.spool_dir, (value) => { edge.spool_dir = value; })),
      field("Transport", select(edge.transport, ["http", "rsync"], (value) => { edge.transport = value; })),
      field("Interval (minutes)", text(String(Math.round(edge.interval_seconds / 60)), (value) => { edge.interval_seconds = Math.max(1, Number(value) || 1) * 60; })),
      field("Datalake URL", text(edge.datalake_url, (value) => { edge.datalake_url = value; })),
      secretField("API key", edge.api_key, state.secrets_set.includes("edge.api_key"), (value) => { edge.api_key = value; }),
      field("rsync target", text(edge.rsync_target, (value) => { edge.rsync_target = value; })),
      field("rsync binary", text(edge.rsync_binary, (value) => { edge.rsync_binary = value; })),
      field("Source name", text(edge.source_name, (value) => { edge.source_name = value; })),
      field("Timeout (seconds)", number(edge.timeout_seconds, (value) => { edge.timeout_seconds = value; })),
    );
  }
  if (section === "account") {
    root.append(
      field("Username", text(settings.console.username, (value) => { settings.console.username = value; })),
      secretField("Password", settings.console.password, state.secrets_set.includes("console.password"), (value) => { settings.console.password = value; }),
    );
  }
  return root;
}

async function save() {
  const response = await fetch("/console/api/settings", {
    method: "PUT",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(payload()),
  });
  if (response.status === 401) return renderLogin();
  if (!response.ok) {
    message = { kind: "error", text: await errorText(response) };
    renderApp();
    return;
  }
  const body = await response.json();
  state = body;
  message = {
    kind: body.restart_required ? "warn" : "ok",
    text: body.restart_required
      ? "Saved. Restart the service to bind the new host or port."
      : "Saved. The schedule and credentials are already in use.",
  };
  renderApp();
}

async function testKind(kind) {
  const response = await fetch("/console/api/test/" + kind, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(payload()),
  });
  if (response.status === 401) return renderLogin();
  const body = await response.json();
  message = { kind: body.ok ? "ok" : "error", text: body.detail || body.error || "Test failed" };
  renderApp();
}

async function restart() {
  message = { kind: "warn", text: "Restarting. This page will reconnect in a few seconds." };
  renderApp();
  await fetch("/console/api/restart", { method: "POST" });
  for (let attempt = 0; attempt < 20; attempt += 1) {
    await sleep(1000);
    try {
      const health = await fetch("/v1/health");
      if (health.ok) {
        message = { kind: "ok", text: "The service is back." };
        boot();
        return;
      }
    } catch (_error) {
      // The process is still down.
    }
  }
  message = { kind: "error", text: "The service did not come back. Check the container logs." };
  renderApp();
}

async function logout() {
  await fetch("/console/api/logout", { method: "POST" });
  state = null;
  renderLogin();
}

function payload() {
  return { settings: state.settings, mongo_login: state.mongo_login };
}

function secretField(label, value, stored, setter) {
  const hint = stored ? "A value is stored. Leave this blank to keep it." : "No value stored yet.";
  const box = el("div", { className: "field" });
  box.append(el("label", { textContent: label }), input("password", value, setter), el("span", { className: "hint", textContent: hint }));
  return box;
}

function field(label, control) {
  return el("div", { className: "field" }, [el("label", { textContent: label }), control]);
}

function labeled(label, control) {
  return el("div", { className: "field" }, [el("label", { textContent: label }), control]);
}

function check(label, value, setter) {
  const box = el("div", { className: "field inline" });
  box.append(checkbox(value, setter), el("label", { textContent: label }));
  return box;
}

function text(value, setter) {
  return input("text", value ?? "", setter);
}

function number(value, setter) {
  const node = input("number", value ?? 0, (raw) => setter(Number(raw)));
  return node;
}

function input(type, value, setter) {
  const node = el("input", { type, value: value ?? "" });
  if (setter) node.addEventListener("input", () => setter(node.value));
  return node;
}

function checkbox(value, setter) {
  const node = el("input", { type: "checkbox" });
  node.checked = Boolean(value);
  node.addEventListener("change", () => setter(node.checked));
  return node;
}

function select(value, options, setter) {
  const node = el("select");
  for (const option of options) {
    node.append(el("option", { value: option, textContent: option }));
  }
  node.value = value;
  node.addEventListener("change", () => setter(node.value));
  return node;
}

function button(label, className, onClick) {
  const node = el("button", { type: "button", className, textContent: label });
  node.addEventListener("click", onClick);
  return node;
}

function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) node[key] = value;
  for (const child of children) node.append(child);
  return node;
}

function toGiB(bytes) {
  return String(Number((Number(bytes) / 1073741824).toFixed(4)));
}

function fromGiB(text) {
  const value = Number(text);
  if (!Number.isFinite(value) || value < 0) return 0;
  return Math.round(value * 1073741824);
}

async function errorText(response) {
  try {
    const body = await response.json();
    return body.detail || JSON.stringify(body);
  } catch (_error) {
    return response.statusText || "Request failed";
  }
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
