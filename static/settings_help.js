// Settings tab: "Need help requests" -- the target time, in hours from sending, for a request to be resolved (the
// SLA). Saved on the server (setting help_sla_hours); each request copies the value when it is sent, so changing it
// only affects new requests. The card is collapsible like the others (static/settings_collapse.js picks it up).
// Same pattern as static/settings_followups.js; text only, never HTML.
document.addEventListener("DOMContentLoaded", function () {
  var root = document.getElementById("settings-help");
  if (!root) return;
  var savedOnce = false;     // tick the button after the redraw that follows a save

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  }
  function post(url, body) {
    return fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })
      .then(function (r) { return r.json(); });
  }

  function draw(data) {
    while (root.firstChild) root.removeChild(root.firstChild);
    var tick = savedOnce;
    savedOnce = false;
    var card = el("div", "card");
    card.id = "settings-help-sla";
    card.appendChild(el("h2", null, "Need help requests"));
    card.appendChild(el("p", "muted auto-help", "How long, in hours counted on the clock from the moment it is sent, a request from the Need help tab should take to be resolved. Each request keeps the target it was sent with: changing this only affects requests sent from now on."));
    var input = el("input");
    input.type = "number";
    input.min = String(data.min);
    input.max = String(data.max);
    input.value = data.hours;
    input.setAttribute("aria-label", "Hours to resolve a request");
    var label = el("label");
    label.appendChild(el("span", null, "Target time to resolve a request (hours)"));
    label.appendChild(input);
    label.appendChild(el("span", "muted fu-hint", data.min + " to " + data.max));
    var grid = el("div", "appt-form-grid");
    grid.appendChild(label);
    var form = el("form", "appt-form");
    form.autocomplete = "off";
    form.appendChild(grid);
    var error = el("div", "field-note"); error.hidden = true;
    var actions = el("div", "appt-form-actions");
    var save = el("button", "btn-confirm", "Save target time"); save.type = "submit";
    actions.appendChild(save);
    form.appendChild(actions);
    form.appendChild(error);
    form.addEventListener("submit", function (event) {
      event.preventDefault();
      var hours = Number(input.value);
      if (!input.value || !isFinite(hours) || Math.floor(hours) !== hours || hours < data.min || hours > data.max) {
        error.hidden = false;
        error.textContent = "Hours must be a whole number between " + data.min + " and " + data.max + ".";
        return;
      }
      error.hidden = true;
      save.disabled = true;
      save.textContent = "Saving...";
      post("/settings/help-sla", { hours: hours }).then(function (r) {
        save.disabled = false;
        save.textContent = "Save target time";
        if (!r.ok) { error.hidden = false; error.textContent = r.error || "Could not save."; return; }
        savedOnce = true;
        draw(r.data);
      }).catch(function (err) {
        save.disabled = false;
        save.textContent = "Save target time";
        error.hidden = false;
        error.textContent = "Could not save: " + err.message;
      });
    });
    card.appendChild(form);
    root.appendChild(card);
    if (tick && window.SaveTick) SaveTick.show(save);
  }

  function load() {
    return fetch("/settings/help-sla").then(function (r) { return r.json(); }).then(function (r) { if (r.ok) draw(r.data); });
  }
  document.addEventListener("tabchange", function (event) { if (event.detail && event.detail.id === "settings") load(); });
  load();
});
