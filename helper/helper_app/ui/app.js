/* vCenter to OCI migration - helper web UI (no framework, hash routing). */
(function () {
  "use strict";

  const app = document.getElementById("app");
  const nav = document.getElementById("nav");
  const userBox = document.getElementById("user");
  const state = { me: null, config: null, jobsByVm: {} };
  let activePoll = null;

  // ---------------------------------------------------------------------- api
  class ApiError extends Error {
    constructor(message, status) { super(message); this.status = status; }
  }

  async function api(method, path, body) {
    const headers = { "Accept": "application/json" };
    if (body !== undefined) headers["Content-Type"] = "application/json";
    const resp = await fetch("../api" + path, {
      method, headers, credentials: "same-origin",
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const text = await resp.text();
    let data = null;
    try { data = text ? JSON.parse(text) : null; } catch (_) { data = { detail: text }; }
    if (!resp.ok) {
      const detail = data && data.detail ? (typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail)) : resp.statusText;
      if (resp.status === 401 && !path.startsWith("/auth/login")) { state.me = null; showLogin(); }
      throw new ApiError(detail, resp.status);
    }
    return data;
  }

  // -------------------------------------------------------------------- utils
  const fmtBytes = (n) => {
    if (n === null || n === undefined) return "-";
    const u = ["B", "KB", "MB", "GB", "TB"]; let i = 0; let v = n;
    while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
    return (i === 0 ? v : v.toFixed(v >= 100 ? 0 : 1)) + " " + u[i];
  };
  const el = (tag, attrs, ...children) => {
    const e = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (k === "class") e.className = v; else if (k.startsWith("on")) e.addEventListener(k.slice(2), v); else if (v !== null && v !== undefined) e.setAttribute(k, v);
    }
    for (const c of children) if (c !== null && c !== undefined) e.append(c.nodeType ? c : document.createTextNode(String(c)));
    return e;
  };
  const kv = (container, pairs) => {
    container.innerHTML = "";
    for (const [k, v] of pairs) { container.append(el("span", { class: "k" }, k), el("span", { class: "v" }, v)); }
  };
  const showError = (msg) => { app.innerHTML = ""; app.append(el("div", { class: "card" }, el("span", { class: "error" }, msg))); };
  const tpl = (id) => document.getElementById(id).content.cloneNode(true);
  const stopPolling = () => { if (activePoll) { activePoll(); activePoll = null; } };
  const isWindows = (vm) => /windows/i.test((vm.guest_id || "") + " " + (vm.guest_full_name || ""));
  const TERMINAL = ["COMPLETED", "FAILED", "CANCELLED"];

  // -------------------------------------------------------------------- auth
  function setUser(me) {
    state.me = me;
    nav.hidden = !me;
    userBox.hidden = !me;
    if (me) userBox.querySelector("[data-username]").textContent = `${me.username} @ ${me.vcenter_host}`;
  }

  async function showLogin() {
    stopPolling();
    setUser(null);
    app.innerHTML = "";
    app.append(tpl("tpl-login"));
    const form = document.getElementById("login-form");
    const err = document.getElementById("login-error");
    const btn = document.getElementById("login-btn");
    try {
      state.config = state.config || await api("GET", "/auth/config");
      form.elements.vcenter.value = state.config.vcenter_host + (state.config.vcenter_port !== 443 ? ":" + state.config.vcenter_port : "");
      if (!state.config.vcenter_host) err.textContent = "HELPER_VCENTER_HOST is not configured on the helper.";
    } catch (e) { err.textContent = e.message; }
    form.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      err.textContent = ""; btn.disabled = true;
      try {
        const me = await api("POST", "/auth/login", { username: form.elements.username.value, password: form.elements.password.value });
        setUser(me);
        route();
      } catch (e) { err.textContent = e.message; }
      finally { btn.disabled = false; }
    });
  }

  document.getElementById("logout-btn").addEventListener("click", async () => {
    try { await api("POST", "/auth/logout"); } catch (_) { /* ignore */ }
    location.hash = "#/vms";
    showLogin();
  });

  // ------------------------------------------------------------- job rendering
  function renderJob(container, job, opts) {
    opts = opts || {};
    let root = container.querySelector(".job");
    if (!root) { root = tpl("tpl-job").firstElementChild; container.innerHTML = ""; container.append(root); }
    const phase = root.querySelector("[data-phase]");
    phase.textContent = job.phase; phase.className = "phase " + job.phase;
    root.querySelector("[data-message]").textContent = job.message || "";
    root.querySelector("[data-error]").textContent = job.error || "";

    const disks = root.querySelector("[data-disks]");
    disks.innerHTML = "";
    for (const d of job.disks) {
      // The stream is compressed; use the raw capacity as an upper bound and clamp.
      const pct = d.status === "COPIED" ? 100 : Math.min(99, Math.round(100 * (d.bytes_received || 0) / Math.max(1, d.capacity_bytes)));
      const barClass = "bar" + (d.status === "COPIED" ? " done" : d.status === "FAILED" ? " failed" : "");
      const detail = d.status === "COPIED" ? `copied, ${fmtBytes(d.bytes_received)} received, ${fmtBytes(d.bytes_written)} written` :
        d.status === "COPYING" ? `${fmtBytes(d.bytes_received)} received (attempt ${d.attempts})` :
        d.status === "FAILED" ? (d.error || "failed") : d.status.toLowerCase();
      disks.append(el("div", { class: "disk" },
        el("div", { class: "meta" },
          el("span", {}, `${d.label || "disk " + d.index} (${fmtBytes(d.capacity_bytes)})${d.device ? " -> " + d.device : ""}`),
          el("span", {}, detail)),
        el("div", { class: barClass }, el("div", { style: `width:${pct}%` }))));
    }

    kv(root.querySelector("[data-oci]"), [
      ["Source VM", `${job.vm.name} (${job.vm.moid})`],
      ["Step", job.step || "-"],
      ["Instance", job.instance_id || "-"],
      ["Seed image", job.seed_image_id || "-"],
      ["Launch options", job.launch_options ? `${job.launch_options.firmware}, boot ${job.launch_options.boot_volume_type}, nic ${job.launch_options.network_type}` : "-"],
      ["Started by", `${job.created_by || "-"} at ${new Date(job.created_at).toLocaleString()}`],
      ["Job id", job.id],
    ]);

    const terminal = TERMINAL.includes(job.phase);
    const cancelBtn = root.querySelector("[data-cancel]");
    cancelBtn.hidden = job.phase === "COMPLETED" || job.phase === "CANCELLED";
    cancelBtn.textContent = job.phase === "FAILED" ? "Clean up OCI resources" : "Cancel";
    cancelBtn.onclick = async () => {
      if (!confirm("Cancel this migration? The OCI instance and volumes created so far will be deleted.")) return;
      cancelBtn.disabled = true;
      try { await api("POST", `/jobs/${job.id}/cancel`); } catch (e) { alert(e.message); }
      finally { cancelBtn.disabled = false; }
    };
    const licSel = root.querySelector("[data-license]"); const licBtn = root.querySelector("[data-license-btn]");
    const showLic = isWindows(job.vm) && job.instance_id && (job.phase === "COMPLETED" || job.phase === "FINALIZING");
    licSel.hidden = licBtn.hidden = !showLic;
    if (showLic) {
      if (job.target.windows_license_type && !licSel.dataset.touched) licSel.value = job.target.windows_license_type;
      licSel.onchange = () => { licSel.dataset.touched = "1"; };
      licBtn.onclick = async () => {
        licBtn.disabled = true;
        try { await api("POST", `/jobs/${job.id}/licensing`, { license_type: licSel.value }); alert("License type updated."); }
        catch (e) { alert(e.message); } finally { licBtn.disabled = false; }
      };
    }
    if (opts.onTerminal && terminal) opts.onTerminal(job);
    return job.phase === "COMPLETED" || job.phase === "CANCELLED";
  }

  function pollJob(jobId, container, opts) {
    let timer = null; let stopped = false;
    const tick = async () => {
      if (stopped) return;
      try {
        const job = await api("GET", `/jobs/${jobId}`);
        if (renderJob(container, job, opts)) return;
      } catch (e) { if (e.status === 401) return; console.warn(e); }
      timer = setTimeout(tick, 3000);
    };
    tick();
    return () => { stopped = true; clearTimeout(timer); };
  }

  // ------------------------------------------------------------------ VM list
  async function vmsView() {
    app.innerHTML = "";
    app.append(tpl("tpl-vms"));
    const rows = document.getElementById("vm-rows");
    const filter = document.getElementById("vm-filter");
    const offOnly = document.getElementById("vm-off-only");
    const count = document.getElementById("vm-count");
    const err = document.getElementById("vm-error");
    let vms = [];

    const render = () => {
      const q = filter.value.trim().toLowerCase();
      rows.innerHTML = "";
      let shown = 0;
      for (const vm of vms) {
        if (offOnly.checked && vm.power_state !== "poweredOff") continue;
        if (q && !`${vm.name} ${vm.folder} ${vm.guest_full_name}`.toLowerCase().includes(q)) continue;
        shown++;
        const job = state.jobsByVm[vm.moid];
        const off = vm.power_state === "poweredOff";
        const active = job && !TERMINAL.includes(job.phase);
        rows.append(el("tr", {},
          el("td", { class: "name" }, vm.name),
          el("td", { class: "muted" }, vm.folder || "-"),
          el("td", {}, el("span", { class: "power " + vm.power_state }, vm.power_state.replace("powered", "").toLowerCase())),
          el("td", {}, vm.guest_full_name || vm.guest_id || "-"),
          el("td", {}, `${vm.num_cpu} / ${fmtBytes(vm.memory_mb * 1024 * 1024)}`),
          el("td", {}, `${vm.num_disks} (${fmtBytes(vm.disk_capacity_bytes)})`),
          el("td", {}, job ? el("a", { href: `#/jobs/${job.id}`, class: "phase " + job.phase }, job.phase) : el("span", { class: "muted" }, "-")),
          el("td", {}, active
            ? el("a", { href: `#/jobs/${job.id}`, class: "button secondary small" }, "View job")
            : el("a", { href: `#/export/${vm.moid}`, class: "button primary small" + (off ? "" : " disabled"), title: off ? "" : "Power off the VM first" }, "Migrate"))));
      }
      count.textContent = `${shown} of ${vms.length} virtual machines`;
    };

    const load = async (refresh) => {
      err.textContent = ""; count.textContent = "Loading inventory...";
      try {
        const [list, jobs] = await Promise.all([api("GET", "/vms" + (refresh ? "?refresh=true" : "")), api("GET", "/jobs")]);
        vms = list;
        state.jobsByVm = {};
        for (const j of jobs) if (!state.jobsByVm[j.vm.moid]) state.jobsByVm[j.vm.moid] = j; // jobs are newest first
        render();
      } catch (e) { if (e.status !== 401) err.textContent = e.message; count.textContent = ""; }
    };
    filter.addEventListener("input", render);
    offOnly.addEventListener("change", render);
    document.getElementById("vm-refresh").addEventListener("click", () => load(true));
    await load(false);
  }

  // -------------------------------------------------------------- export view
  async function exportView(moid) {
    app.innerHTML = "";
    app.append(tpl("tpl-export"));
    const form = document.getElementById("target-form");
    const formError = document.getElementById("form-error");
    const submit = document.getElementById("submit-btn");

    let inspection, options;
    try {
      [inspection, options] = await Promise.all([api("GET", `/vms/${encodeURIComponent(moid)}`), api("GET", "/oci/options")]);
    } catch (e) { if (e.status !== 401) showError("Cannot load VM or OCI information: " + e.message); return; }
    const vm = inspection.vm;

    kv(document.getElementById("vm-details"), [
      ["Name", vm.name], ["Guest OS", vm.guest_full_name || vm.guest_id],
      ["Power state", vm.power_state], ["CPU / memory", `${vm.num_cpu} vCPU / ${fmtBytes(vm.memory_mb * 1024 * 1024)}`],
      ["Firmware", vm.firmware.toUpperCase() + (vm.secure_boot ? " (secure boot)" : "")],
      ["Disks", vm.disks.map((d) => `${d.label}: ${fmtBytes(d.capacity_bytes)} on ${d.controller_type}`).join("; ")],
      ["Network", vm.nics.map((n) => `${n.label}: ${n.adapter_type}`).join("; ") || "-"],
    ]);
    const problems = document.getElementById("vm-problems");
    for (const p of inspection.problems) problems.append(el("li", {}, p));
    const warnings = document.getElementById("vm-warnings");
    for (const w of inspection.warnings) warnings.append(el("li", {}, w));

    // populate the target form
    const sel = (name) => form.elements[name];
    for (const c of options.compartments) sel("compartment_id").append(el("option", { value: c.id }, c.path || c.name));
    for (const ad of options.availability_domains) sel("availability_domain").append(el("option", { value: ad }, ad + (ad === options.helper_availability_domain ? " (helper)" : "")));
    sel("availability_domain").value = options.helper_availability_domain;
    // VCN -> subnet: the subnet list is filtered by the selected VCN
    let netOptions = options;
    const fillSubnets = () => {
      const vcnId = sel("vcn_id").value;
      const subnets = netOptions.subnets.filter((s) => s.vcn_id === vcnId);
      sel("subnet_id").innerHTML = "";
      for (const s of subnets) sel("subnet_id").append(el("option", { value: s.id }, `${s.name} (${s.cidr_block})${s.prohibit_public_ip ? ", private" : ""}${s.availability_domain ? ", " + s.availability_domain : ""}`));
      document.getElementById("subnet-hint").textContent = subnets.length ? "" : (vcnId ? "No subnets in this VCN within the selected compartment." : "Select a VCN first.");
    };
    const fillNetworks = (o) => {
      netOptions = o;
      const vcnSel = sel("vcn_id");
      const previous = vcnSel.value;
      vcnSel.innerHTML = "";
      for (const v of o.vcns) vcnSel.append(el("option", { value: v.id }, `${v.name}${v.cidr_blocks.length ? " (" + v.cidr_blocks.join(", ") + ")" : ""}`));
      // VCNs that only show up through their subnets (VCN in another compartment)
      for (const s of o.subnets) if (!o.vcns.some((v) => v.id === s.vcn_id) && ![...vcnSel.options].some((op) => op.value === s.vcn_id)) vcnSel.append(el("option", { value: s.vcn_id }, s.vcn_name || s.vcn_id));
      if ([...vcnSel.options].some((op) => op.value === previous)) vcnSel.value = previous;
      fillSubnets();
    };
    sel("vcn_id").addEventListener("change", fillSubnets);
    fillNetworks(options);
    sel("shape").append(el("option", { value: "" }, `${options.default_shape} (default)`));
    for (const s of options.shapes) if (s.name !== options.default_shape) sel("shape").append(el("option", { value: s.name }, s.name));
    document.getElementById("shape-hint").textContent = `Sized to ${Math.max(1, Math.ceil(vm.num_cpu / 2))} OCPU / ${Math.max(1, Math.ceil(vm.memory_mb / 1024))} GB from the source VM.`;
    sel("display_name").value = vm.name;
    const isWin = isWindows(vm);
    document.getElementById("windows-fieldset").hidden = !isWin;
    if (options.compartments.some((c) => c.id === options.helper_compartment_id)) sel("compartment_id").value = options.helper_compartment_id;
    else if (options.compartments.length) sel("compartment_id").selectedIndex = 0;
    sel("compartment_id").addEventListener("change", async () => {
      formError.textContent = "";
      try { fillNetworks(await api("GET", `/oci/options?compartment_id=${encodeURIComponent(sel("compartment_id").value)}`)); }
      catch (e) { formError.textContent = e.message; }
    });

    const bootType = { ide: "IDE", lsilogic: "SCSI", lsilogicsas: "SCSI", buslogic: "SCSI" }[vm.disks[0] && vm.disks[0].controller_type] || "PARAVIRTUALIZED";
    const netType = vm.nics.length && vm.nics.every((n) => /^e1000|pcnet/.test(n.adapter_type)) ? "E1000" : "PARAVIRTUALIZED";
    kv(document.getElementById("launch-preview"), [
      ["Firmware", vm.firmware === "efi" ? "UEFI_64" : "BIOS"], ["Boot volume type", bootType], ["Network type", netType],
    ]);

    submit.disabled = !inspection.can_export;

    // resume display of an active job for this VM, if any
    const jobCard = document.getElementById("job-card"); const jobView = document.getElementById("job-view");
    try {
      const jobs = await api("GET", `/jobs?vm_moid=${encodeURIComponent(moid)}`);
      const active = jobs.find((j) => !TERMINAL.includes(j.phase));
      if (active) { jobCard.hidden = false; submit.disabled = true; activePoll = pollJob(active.id, jobView, { onTerminal: () => { submit.disabled = !inspection.can_export; } }); }
    } catch (_) { /* ignore */ }

    form.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      formError.textContent = "";
      const fd = new FormData(form);
      const target = {
        compartment_id: fd.get("compartment_id"),
        availability_domain: fd.get("availability_domain"),
        subnet_id: fd.get("subnet_id"),
        shape: fd.get("shape") || null,
        display_name: fd.get("display_name") || null,
        assign_public_ip: fd.get("assign_public_ip") === "on",
        start_after_migration: fd.get("start_after_migration") === "on",
        windows_license_type: isWin ? fd.get("windows_license_type") : null,
        compatibility_mode: fd.get("compatibility_mode") === "on",
        boot_volume_type_override: fd.get("boot_volume_type_override") || null,
        network_type_override: fd.get("network_type_override") || null,
      };
      if (target.availability_domain !== options.helper_availability_domain) {
        formError.textContent = "The availability domain must match the helper VM's AD."; return;
      }
      submit.disabled = true;
      try {
        const job = await api("POST", "/jobs", { vm_moid: moid, target });
        jobCard.hidden = false;
        stopPolling();
        activePoll = pollJob(job.id, jobView, { onTerminal: () => { submit.disabled = !inspection.can_export; } });
      } catch (e) { formError.textContent = e.message; submit.disabled = false; }
    });
  }

  // ---------------------------------------------------------------- jobs view
  async function jobsView() {
    let jobs = [];
    try { jobs = await api("GET", "/jobs"); } catch (e) { if (e.status !== 401) showError(e.message); return; }
    app.innerHTML = "";
    const table = el("table", { class: "jobs" },
      el("thead", {}, el("tr", {}, ...["VM", "Phase", "Message", "OCI instance", "Started", "By", ""].map((h) => el("th", {}, h)))),
      el("tbody", {}, ...jobs.map((j) => el("tr", {},
        el("td", { class: "name" }, j.vm.name), el("td", {}, el("span", { class: "phase " + j.phase }, j.phase)),
        el("td", {}, j.message || ""), el("td", { class: "ocid" }, j.instance_id || "-"),
        el("td", {}, new Date(j.created_at).toLocaleString()), el("td", {}, j.created_by || "-"),
        el("td", {}, el("a", { class: "button secondary small", href: `#/jobs/${j.id}` }, "Details"))))));
    app.append(el("div", { class: "card" }, el("h2", {}, "Migration jobs"),
      jobs.length ? table : el("div", { class: "muted" }, "No jobs yet. Pick a powered-off VM under Virtual machines to start one.")));
    // refresh the table while jobs are active
    if (jobs.some((j) => !TERMINAL.includes(j.phase))) {
      const t = setTimeout(() => { if (location.hash === "#/jobs") jobsView(); }, 5000);
      activePoll = () => clearTimeout(t);
    }
  }

  async function jobDetailView(jobId) {
    app.innerHTML = "";
    const c = el("div", { class: "card" });
    app.append(el("div", { class: "toolbar" }, el("a", { href: "#/jobs", class: "muted" }, "\u2190 all jobs")), c);
    activePoll = pollJob(jobId, c);
  }

  // ------------------------------------------------------------------- routing
  async function route() {
    stopPolling();
    if (!state.me) {
      try { setUser(await api("GET", "/auth/me")); } catch (e) { return; /* api() showed the login view */ }
    }
    const hash = location.hash || "#/vms";
    for (const a of nav.querySelectorAll("a")) a.classList.toggle("active", hash.startsWith(a.getAttribute("href")));
    let m;
    if ((m = /^#\/export\/(.+)$/.exec(hash))) return exportView(decodeURIComponent(m[1]));
    if ((m = /^#\/jobs\/(.+)$/.exec(hash))) return jobDetailView(decodeURIComponent(m[1]));
    if (hash === "#/jobs") return jobsView();
    return vmsView();
  }

  window.addEventListener("hashchange", () => { route().catch((e) => showError(e.message)); });
  route().catch((e) => showError(e.message));
})();
