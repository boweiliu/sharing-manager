"use strict";
const $ = selector => document.querySelector(selector);
let state = null, selected = new URLSearchParams(location.search).get("target") || "files", agents = [], agentsAvailable = false, busy = false, inviteTarget = null;
const emptyScope = () => ({users: [], emails: [], email_domains: []});
const entries = scope => ["users", "emails", "email_domains"].flatMap(kind => (scope[kind] || []).map(value => ({kind, value})));
function showError(message) { $("#error").textContent = message; $("#error").hidden = !message; }
function inform(message) { $("#notice").textContent = message; }
async function api(path, options = {}) {
  const response = await fetch(path, {...options, headers: {"Content-Type": "application/json", "X-Sharing-Action": "1", ...options.headers}});
  const result = await response.json().catch(() => null);
  if (!response.ok) { const error = new Error(result?.detail || result?.error || "The change could not be completed. Refresh and try again."); error.status = response.status; throw error; }
  return result;
}
function iconSource(markup) { return "data:image/svg+xml;base64," + btoa(String.fromCharCode(...new TextEncoder().encode(markup))); }
function scopeFor(target) { return target === "workspace" ? state.workspace : state.services[target] || emptyScope(); }
function permissionCount(target) { return entries(scopeFor(target)).length; }
function linkButtons() {
  const url = state?.targets.find(t => t.name === selected)?.url, usable = !!url && state.state === "up", open = $("#open-link");
  $("#copy-link").disabled = $("#show-qr").disabled = !usable;
  if (usable) { open.href = url; open.removeAttribute("aria-disabled"); } else { open.removeAttribute("href"); open.setAttribute("aria-disabled", "true"); }
}
function qrSvg(text) {
  const code = qrcode(0, "M"); code.addData(text); code.make();
  const count = code.getModuleCount(), quiet = 4, size = count + quiet * 2, ns = "http://www.w3.org/2000/svg";
  let path = "";
  for (let row = 0; row < count; row++) for (let column = 0; column < count; column++) {
    let end = column; while (end < count && code.isDark(row, end)) end++;
    if (end > column) { path += "M" + (column + quiet) + " " + (row + quiet) + "h" + (end - column) + "v1h-" + (end - column) + "z"; column = end; }
  }
  const svg = document.createElementNS(ns, "svg"), ground = document.createElementNS(ns, "rect"), modules = document.createElementNS(ns, "path");
  svg.setAttribute("viewBox", "0 0 " + size + " " + size); svg.setAttribute("shape-rendering", "crispEdges");
  ground.setAttribute("width", size); ground.setAttribute("height", size); ground.setAttribute("fill", "#fff");
  modules.setAttribute("d", path); modules.setAttribute("fill", "#000");
  svg.append(ground, modules); return svg;
}
function selectTarget(target) {
  if (busy) return;
  selected = target;
  history.replaceState(null, "", "?target=" + encodeURIComponent(target));
  $("#grant-value").value = "";
  showError(""); inform(""); render();
}
function navigationButton(target) {
  const button = document.createElement("button"); button.type = "button";
  button.dataset.shareTarget = target.name; button.setAttribute("aria-pressed", String(selected === target.name));
  if (target.icon) { const icon = document.createElement("img"); icon.src = iconSource(target.icon); icon.alt = ""; button.append(icon); }
  const name = document.createElement("span"); name.className = "name"; name.textContent = target.title; button.append(name);
  const count = document.createElement("span"); count.className = "count"; count.textContent = permissionCount(target.name); button.append(count);
  button.onclick = () => selectTarget(target.name); return button;
}
function grantRow(entry, inherited = false) {
  const row = document.createElement("div"); row.className = "grant-row" + (entry.kind === "email_domains" ? " domain" : ""); row.dataset.grantValue = entry.value;
  const lead = document.createElement("span"); lead.className = "grant-lead"; lead.textContent = entry.kind === "email_domains" ? "@" : entry.kind === "users" ? "ID" : entry.value[0].toUpperCase();
  const name = document.createElement("span"); name.className = "grant-name"; name.textContent = entry.kind === "email_domains" ? "Anyone at " + entry.value : entry.value;
  row.append(lead, name);
  if (entry.kind === "users") { const secondary = document.createElement("span"); secondary.className = "grant-secondary"; secondary.textContent = "Account"; row.append(secondary); }
  if (!inherited) { const remove = document.createElement("button"); remove.type = "button"; remove.textContent = "×"; remove.setAttribute("aria-label", "Remove " + entry.value); remove.onclick = async () => {
    if (!await confirmAction("Remove permission?", "Remove " + entry.value + " from " + (selected === "workspace" ? "the whole workspace" : state.targets.find(t => t.name === selected).title) + "? They may still have access through another permission.", "Remove")) return;
    await changePermission(entry.kind, entry.value, "remove");
  }; row.append(remove); }
  return row;
}
function render() {
  if (!state.targets.some(t => t.name === selected)) selected = "workspace";
  const target = state.targets.find(t => t.name === selected), whole = selected === "workspace";
  const nav = $("#targets"), previousSystemOpen = nav.querySelector("details")?.open;
  nav.replaceChildren();
  state.targets.filter(t => !t.system).forEach(t => nav.append(navigationButton(t)));
  const system = state.targets.filter(t => t.system);
  if (system.length) { const details = document.createElement("details"); details.open = previousSystemOpen ?? system.some(t => t.name === selected || permissionCount(t.name)); const summary = document.createElement("summary"); summary.textContent = "System apps"; details.append(summary); system.forEach(t => details.append(navigationButton(t))); nav.append(details); }
  $("#publication-state").textContent = state.state === "up" ? "Published" : state.state === "off" ? "Not published" : "Connection " + state.state;
  $("#publication-description").textContent = state.state === "up" ? "This workspace has a web address. Only people granted permission can access it." : "Manage publishing in Studio. Saved permissions remain here.";
  if (state.sandbox) $("#publication-description").textContent = "Isolated verification copy. Changes affect only this copy.";
  $("#target-heading").textContent = whole ? "Permissions for the whole workspace" : "Permissions for " + target.title;
  $("#scope-description").textContent = whole ? "Permissions below apply to every app in this workspace, and also grant access to files, agent chats and terminal." : "Permissions below apply only to the " + target.title + " app.";
  $("#target-icon").hidden = !target.icon; if(target.icon) $("#target-icon").src = iconSource(target.icon);
  $("#share-link").value = target.url || "Link not available yet";
  linkButtons();
  $("#page-subtitle").textContent = state.machine ? "Manage who can access " + state.machine + " and its apps." : "Manage who can access this workspace and its apps.";
  $("#link-description").textContent = state.state === "off" ? "This link cannot be accessed while sharing is off. Your permissions are preserved but inactive." : "Only people granted permission can open this link.";
  const list = $("#grant-list"), inheritedOpen = list.querySelector("details")?.open;
  list.replaceChildren();
  if (!whole && entries(state.workspace).length) {
    const details = document.createElement("details"); details.className = "inherited"; details.open = inheritedOpen ?? true;
    const summary = document.createElement("summary"); const count = entries(state.workspace).length; summary.textContent = count + " permission" + (count === 1 ? "" : "s") + " inherited from the whole workspace";
    const rows = document.createElement("div"); rows.className = "inherited-rows"; entries(state.workspace).forEach(e => rows.append(grantRow(e, true))); details.append(summary, rows); list.append(details);
  }
  entries(scopeFor(selected)).forEach(e => list.append(grantRow(e)));
  if (!entries(scopeFor(selected)).length && (whole || !entries(state.workspace).length)) { const message = document.createElement("p"); message.className = "muted"; message.textContent = "No permissions have been granted."; list.append(message); }
  $("#agent-description").textContent = whole ? "Invite a coding agent running somewhere else to work in this workspace with exactly this workspace’s access, and nothing more." : "Invite a coding agent running somewhere else to use only this app.";
  $("#invite-result").hidden = inviteTarget !== selected;
  renderAgents(); $("#permissions").hidden = false; $("#loading").hidden = true;
  document.title = "Sharing · " + target.title;
}
function renderAgents() {
  const box = $("#agents"); box.replaceChildren(); $("#agent-section").hidden = !agentsAvailable;
  const visible = agents.filter(a => selected === "workspace" || a.scope === "workspace" || Array.isArray(a.scope) && a.scope.includes(selected));
  if (!visible.length) { const empty = document.createElement("p"); empty.className = "muted"; empty.textContent = "No agents have been invited."; box.append(empty); return; }
  visible.forEach(agent => {
    const row = document.createElement("div"); row.className = "grant-row";
    const name = document.createElement("span"); name.className = "grant-name"; name.textContent = agent.name || agent.agent_id;
    const detail = document.createElement("span"); detail.className = "grant-secondary"; detail.textContent = agent.scope === "workspace" ? "Whole workspace" : Array.isArray(agent.scope) ? agent.scope.join(", ") : agent.scope;
    const revoke = document.createElement("button"); revoke.textContent = "Revoke"; revoke.style.fontSize = "12px";
    revoke.onclick = async () => { if (!await confirmAction("Revoke agent access?", "This will stop " + (agent.name || agent.agent_id) + " from accessing this workspace.", "Revoke")) return; await action(async () => { await api("/api/agents/" + encodeURIComponent(agent.agent_id), {method:"DELETE"}); await loadAgents(); inform("Agent access revoked."); }); };
    row.append(name, detail, revoke); box.append(row);
  });
}
function confirmAction(title, text, accept) {
  const dialog = $("#confirmation"); $("#confirmation-title").textContent = title; $("#confirmation-text").textContent = text; $("#accept-confirmation").textContent = accept;
  return new Promise(resolve => { const finish = value => { dialog.close(); dialog.oncancel = null; resolve(value); }; $("#accept-confirmation").onclick = () => finish(true); $("#cancel-confirmation").onclick = () => finish(false); dialog.oncancel = event => { event.preventDefault(); finish(false); }; dialog.showModal(); });
}
async function action(callback) {
  if (busy) return; busy = true; showError(""); inform("");
  document.querySelectorAll("button,select").forEach(e => e.disabled = true);
  try { await callback(); }
  catch(error) { if(error.status === 409) { try { state = await api("/api/sharing"); render(); } catch {} } showError(error.message); }
  finally { busy = false; document.querySelectorAll("button,select").forEach(e => e.disabled = false); if(state) linkButtons(); }
}
async function changePermission(kind, value, operation) { await action(async () => { state = await api("/api/grants", {method:"POST", body:JSON.stringify({scope:selected,kind,value,operation,revision:state.revision})}); render(); $("#grant-value").value = ""; inform(operation === "add" ? "Permission added. Share the link with them to give them access." : "Permission removed."); }); }
async function loadAgents() { try { const result = await api("/api/agents"); agents = result.agents; agentsAvailable = result.available !== false; renderAgents(); } catch(error) { agentsAvailable = true; renderAgents(); $("#agents").textContent = "Visiting agents could not be loaded. Use Refresh to try again."; } }
async function load() { await action(async () => { state = await api("/api/sharing"); render(); await loadAgents(); }); }
$("#grant-kind").onchange = () => { const domain = $("#grant-kind").value === "email_domains"; $("#grant-value").placeholder = domain ? "example.com" : "name@example.com"; $("#grant-value").value = ""; };
$("#grant-form").onsubmit = async event => { event.preventDefault(); const value = $("#grant-value").value.trim(); if (!value || busy) return; if (selected === "workspace" && !await confirmAction("Grant workspace access?", "Anyone granted workspace access can see and change everything within it, including files, agent chats and terminal.", "Grant access")) return; await changePermission($("#grant-kind").value,value,"add"); };
$("#refresh").onclick = load;
async function copy(text, success) { try { await navigator.clipboard.writeText(text); inform(success); } catch { showError("Copy was blocked by your browser. Select and copy the text manually."); } }
$("#copy-link").onclick = () => copy($("#share-link").value,"Link copied.");
$("#show-qr").onclick = () => {
  const target = state.targets.find(t => t.name === selected); if (!target?.url) return;
  $("#qr-title").textContent = "QR code for " + (selected === "workspace" ? "the whole workspace" : target.title);
  $("#qr-code").replaceChildren(qrSvg(target.url)); $("#qr-code").setAttribute("aria-label", "QR code for " + target.url);
  $("#qr-link").textContent = target.url; $("#qr-dialog").showModal();
};
$("#close-qr").onclick = () => $("#qr-dialog").close();
$("#qr-dialog").onclick = event => { const box = $("#qr-dialog").getBoundingClientRect(); if (event.clientX < box.left || event.clientX > box.right || event.clientY < box.top || event.clientY > box.bottom) $("#qr-dialog").close(); };
$("#invite-agent").onclick = async () => { if (selected === "workspace" && !await confirmAction("Invite an agent to this workspace?", "The agent will be able to see and change everything in this workspace, including files, chats and terminal.", "Create invitation")) return; await action(async () => { const result = await api("/api/agents/invites", {method:"POST",body:JSON.stringify({scope:selected})}); inviteTarget = selected; $("#invite-instructions").value = result.instructions; $("#invite-result").hidden = false; inform("Invitation created. It works once and expires in fifteen minutes."); }); };
$("#copy-instructions").onclick = () => copy($("#invite-instructions").value,"Instructions copied.");
$("#dismiss-invite").onclick = () => { inviteTarget = null; $("#invite-instructions").value = ""; $("#invite-result").hidden = true; };
load();
