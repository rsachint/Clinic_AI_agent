// Settings tab: "Follow-up reminders" (when the two WhatsApp reminders go out) and
// the Meta message templates (which ones have been approved by Meta). Both are
// clinic-wide and saved on the server; names and texts are inserted as text, never
// as HTML. Checks like the ones in clinic/settings.py run here first
// (window.fuTimingProblems, from static/followups.js).
document.addEventListener("DOMContentLoaded", function () {
  var root = document.getElementById("settings-followups");
  var flash = document.getElementById("settings-flash");
  if (!root) return;

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  }
  function say(message, ok) {
    if (!flash) return;
    flash.hidden = false;
    flash.className = "flash " + (ok ? "ok" : "error");
    flash.textContent = message;
    if (ok) setTimeout(function () { flash.hidden = true; }, 4000);
    else window.scrollTo({ top: 0, behavior: "smooth" });
  }
  // A save is confirmed by a check mark beside its button (static/save_tick.js), not by text; the
  // top-of-page banner is off screen when the button is further down. The cards are redrawn after
  // a save, so the tick is put back on the new button. A failure keeps a text message beside the button.
  var savedCard = null;   // "timing" or "templates": which card to tick after the redraw
  function statusNote(text) {
    var node = el("span", "save-status error", text);
    node.setAttribute("role", "status");
    return node;
  }
  function post(url, body) {
    return fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })
      .then(function (r) { return r.json(); });
  }
  function field(label, input, hint) {
    var l = el("label");
    l.appendChild(el("span", null, label));
    l.appendChild(input);
    if (hint) l.appendChild(el("span", "muted fu-hint", hint));
    return l;
  }
  function input(type, value, extra) {
    var i = el("input");
    i.type = type;
    i.value = value;
    Object.keys(extra || {}).forEach(function (k) { i.setAttribute(k, extra[k]); });
    return i;
  }

  function draw(data) {
    root.innerHTML = "";
    var tickCard = savedCard;
    savedCard = null;
    var t = data.timing;

    var timing = el("div", "card");
    timing.id = "settings-followup-timing";
    timing.appendChild(el("h2", null, "Follow-up reminders"));
    timing.appendChild(el("p", "muted auto-help", "A follow-up visit sends the patient two WhatsApp reminders: an early one on a set day, and one a few hours before the visit. The early reminder goes out N days before the follow-up date at the time below; the second goes out N hours before the visit but never earlier than the time given for that day. A reminder is never sent once the visit has started, and a reminder that is late (the app was off) goes out as soon as the app is running again."));
    var days = input("number", t.days_before, { min: "1", max: "14" });
    var send = input("time", t.send_time);
    var hours = input("number", t.hours_before, { min: "1", max: "12" });
    var earliest = input("time", t.earliest_send);
    var grid = el("div", "appt-form-grid");
    grid.appendChild(field("Early reminder: days before", days, "1 to 14"));
    grid.appendChild(field("Early reminder: send at", send, "05:00 to 22:59"));
    grid.appendChild(field("Second reminder: hours before the visit", hours, "1 to 12"));
    grid.appendChild(field("Second reminder: not earlier than", earliest, "05:00 to 22:59, on the day"));
    var form = el("form", "appt-form");
    form.autocomplete = "off";
    form.appendChild(grid);
    var error = el("div", "field-note"); error.hidden = true;
    var actions = el("div", "appt-form-actions");
    var save = el("button", "btn-confirm", "Save timing"); save.type = "submit";
    actions.appendChild(save);
    form.appendChild(actions);
    form.appendChild(error);
    form.addEventListener("submit", function (event) {
      event.preventDefault();
      var values = { days_before: days.value, send_time: send.value, hours_before: hours.value, earliest_send: earliest.value };
      var problems = window.fuTimingProblems ? window.fuTimingProblems(values) : [];
      error.hidden = !problems.length;
      error.textContent = problems.join(" ");
      if (problems.length) return;
      save.disabled = true;
      save.textContent = "Saving...";
      post("/settings/followups", values).then(function (r) {
        save.disabled = false;
        save.textContent = "Save timing";
        if (!r.ok) { error.hidden = false; error.textContent = r.error || "Could not save."; return; }
        savedCard = "timing";
        draw(r.data);
      }).catch(function (err) { save.disabled = false; save.textContent = "Save timing"; error.hidden = false; error.textContent = "Could not save: " + err.message; });
    });
    timing.appendChild(form);
    root.appendChild(timing);

    var templates = el("div", "card");
    templates.id = "settings-followup-templates";
    templates.appendChild(el("h2", null, "WhatsApp message templates for reminders"));
    templates.appendChild(el("p", "muted auto-help", "WhatsApp only lets the app write first, outside 24 hours after the patient last messaged, with a message template that Meta has approved. Submit the templates in docs/meta_templates/ in WhatsApp Manager (the README there explains how), and tick each one here once Meta shows it as Approved. Until a template is ticked, a reminder that cannot go out waits in Send manually (Patients tab, Follow-ups) instead of being sent."));
    var boxes = [];
    var list = el("div", "fu-template-list");
    data.templates.forEach(function (tpl) {
      var row = el("label", "fu-template-row");
      var box = el("input"); box.type = "checkbox"; box.checked = !!tpl.approved; box.value = tpl.name;
      boxes.push(box);
      row.appendChild(box);
      var text = el("span", "fu-template-text");
      text.appendChild(el("strong", null, tpl.name));
      text.appendChild(el("span", "muted", " · " + tpl.reminder + " · " + tpl.written_in));
      text.appendChild(el("div", "muted fu-template-body", tpl.body));
      row.appendChild(text);
      list.appendChild(row);
    });
    templates.appendChild(list);
    var tActions = el("div", "appt-form-actions");
    var tSave = el("button", "btn-confirm", "Save approved templates"); tSave.type = "button";
    function showTemplateError(text) {
      var old = tActions.querySelector(".save-status");
      if (old) old.remove();
      tActions.appendChild(statusNote(text));
    }
    tSave.addEventListener("click", function () {
      tSave.disabled = true;
      tSave.textContent = "Saving...";
      var approved = boxes.filter(function (b) { return b.checked; }).map(function (b) { return b.value; });
      post("/settings/followup-templates", { approved: approved }).then(function (r) {
        tSave.disabled = false;
        tSave.textContent = "Save approved templates";
        if (!r.ok) { say(r.error || "Could not save.", false); showTemplateError(r.error || "Could not save."); return; }
        savedCard = "templates";
        draw(r.data);
      }).catch(function (err) {
        tSave.disabled = false;
        tSave.textContent = "Save approved templates";
        say("Could not save: " + err.message, false);
        showTemplateError("Could not save: " + err.message);
      });
    });
    tActions.appendChild(tSave);
    templates.appendChild(tActions);
    root.appendChild(templates);
    if (tickCard === "timing") SaveTick.show(save);
    if (tickCard === "templates") SaveTick.show(tSave);
  }

  function load() {
    return fetch("/settings/followups/data").then(function (r) { return r.json(); }).then(function (r) { if (r.ok) draw(r.data); });
  }
  document.addEventListener("tabchange", function (event) { if (event.detail && event.detail.id === "settings") load(); });
  load();
});
