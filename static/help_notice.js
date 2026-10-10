// Need help: tell the person, once, that a request they sent is resolved (clinic/help_requests.py). Asked when the
// page opens, whenever a tab is opened, and every few minutes; each resolved request is shown ONE time -- the
// server remembers (notified_at) -- as a small dismissible line at the top of the page. Same pattern as
// static/unanswered_notice.js. Text only, never HTML.
(function () {
  // The notice sentence (the server sends the same wording in `text`; this is the fallback).
  function noticeText(item) {
    if (item.text) return item.text;
    var text = "Your request " + item.ticket_no + " has been resolved.";
    if (item.note) text += " " + String(item.note).replace(/[\s.]+$/, "") + ".";
    return text;
  }
  var api = { noticeText: noticeText };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  if (typeof window !== "undefined") window.HelpNotice = api;
  if (typeof document === "undefined" || !document.addEventListener) return;

  document.addEventListener("DOMContentLoaded", function () {
    var root = document.getElementById("help-notices");
    if (!root) return;
    var shown = {};            // ids already on screen: a second check never repeats one
    var checking = false;

    function show(item) {
      shown[item.id] = true;
      var box = document.createElement("div");
      box.className = "flash ok help-notice";
      box.setAttribute("role", "status");
      var text = document.createElement("span");
      text.textContent = noticeText(item);
      var open = document.createElement("button");
      open.type = "button";
      open.className = "btn";
      open.textContent = "See my requests";
      open.addEventListener("click", function () {
        if (window.ClinicNav) window.ClinicNav.select("help");
        if (window.HelpSubtabs) window.HelpSubtabs.select("mine");
        box.remove();
      });
      var close = document.createElement("button");
      close.type = "button";
      close.className = "btn";
      close.textContent = "Got it";
      close.addEventListener("click", function () { box.remove(); });
      var buttons = document.createElement("span");
      buttons.className = "help-notice-buttons";
      buttons.appendChild(open);
      buttons.appendChild(document.createTextNode(" "));
      buttons.appendChild(close);
      box.appendChild(text);
      box.appendChild(buttons);
      root.appendChild(box);
      // Told once: the server stops offering it as soon as it has been shown.
      fetch("/help/requests/" + item.id + "/notified", { method: "POST" }).catch(function () { /* shown again next time */ });
    }

    function check() {
      if (checking) return;
      checking = true;
      fetch("/help/notices").then(function (r) { return r.json(); }).then(function (r) {
        (r.notices || []).forEach(function (item) { if (!shown[item.id]) show(item); });
      }).catch(function () { /* nothing to say */ }).then(function () { checking = false; });
    }

    document.addEventListener("tabchange", check);
    setInterval(check, 3 * 60 * 1000);
    check();
  });
})();
