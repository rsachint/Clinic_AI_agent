// Assistant tab: tell the user, once, that a question they asked earlier works now
// (clinic/unanswered.py). Asked when the Assistant tab opens and again after each new turn in
// the conversation; each resolved question is shown ONE time -- the server remembers (notified_at)
// -- as a small dismissible line above the conversation. Text only, never HTML.
(function () {
  function noticeText(item) {
    return "You asked '" + item.question + "' earlier. It works now: " + String(item.note || "").replace(/[\s.]+$/, "") + ". Try it again.";
  }
  var api = { noticeText: noticeText };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  window.UnansweredNotice = api;
  if (typeof document === "undefined" || !document.addEventListener) return;

  document.addEventListener("DOMContentLoaded", function () {
    var root = document.getElementById("unanswered-notices");
    if (!root) return;
    var shown = {};            // ids already on screen: a second check never repeats one
    var checking = false;

    function show(item) {
      shown[item.id] = true;
      var box = document.createElement("div");
      box.className = "flash ok unanswered-notice";
      box.setAttribute("role", "status");
      var text = document.createElement("span");
      text.textContent = noticeText(item);
      var close = document.createElement("button");
      close.type = "button";
      close.className = "btn";
      close.textContent = "Got it";
      close.addEventListener("click", function () { box.remove(); });
      box.appendChild(text);
      box.appendChild(document.createTextNode(" "));
      box.appendChild(close);
      root.appendChild(box);
      // Told once: the server stops offering it as soon as it has been shown.
      fetch("/unanswered/" + item.id + "/notified", { method: "POST" }).catch(function () { /* shown again next time */ });
    }

    function check() {
      if (checking) return;
      checking = true;
      fetch("/unanswered/notices").then(function (r) { return r.json(); }).then(function (r) {
        (r.notices || []).forEach(function (item) { if (!shown[item.id]) show(item); });
      }).catch(function () { /* nothing to say */ }).then(function () { checking = false; });
    }

    document.addEventListener("tabchange", function (event) { if (event.detail && event.detail.id === "assistant") check(); });
    var feed = document.getElementById("conversation-feed");
    if (feed && window.MutationObserver) {
      var timer = null;
      new MutationObserver(function () { clearTimeout(timer); timer = setTimeout(check, 800); }).observe(feed, { childList: true });
    }
  });
})();
