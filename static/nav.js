// Left nav: a data-driven tab list so adding a tab later is "one entry
// here + one <section class="tab-panel" data-tab="..."> in dashboard.html",
// not a structural change. Settings/Profile/Login are wired up the same
// way as the real tabs specifically to prove that -- they just render a
// placeholder because there's nothing behind them yet.
// `hidden: true` keeps a tab out of the left menu while its panel and scripts stay in
// the page untouched, so showing it again is just deleting the flag.
var NAV_TABS = [
  { id: "assistant", label: "Assistant", icon: "🎙️" },
  { id: "queue", label: "Queue", icon: "🎟️" },
  { id: "appointments", label: "Appointments", icon: "📅" },
  { id: "automation", label: "Automation", icon: "🤖" },
  { id: "patients", label: "Patients", icon: "🧑‍🤝‍🧑" },
  { id: "messages", label: "Patient messages", icon: "💬", hidden: true },
  { id: "connectors", label: "Connectors", icon: "🔌" },
  { id: "audit", label: "Audit log", icon: "📜" },
  { id: "settings", label: "Settings", icon: "⚙️" },
  { id: "profile", label: "Profile", icon: "👤" },
  { id: "login", label: "Login/Logout", icon: "🔑" },
];

document.addEventListener("DOMContentLoaded", function () {
  var navList = document.getElementById("nav-tabs");
  if (!navList) return;

  var panels = {};
  document.querySelectorAll(".tab-panel").forEach(function (panel) {
    panels[panel.getAttribute("data-tab")] = panel;
  });

  function selectTab(id) {
    NAV_TABS.forEach(function (tab) {
      var panel = panels[tab.id];
      var link = navList.querySelector('[data-tab-link="' + tab.id + '"]');
      var active = tab.id === id;
      if (panel) panel.hidden = !active;
      if (link) link.classList.toggle("active", active);
    });
    // Lets a tab refresh itself when it becomes visible (see queue.js).
    document.dispatchEvent(new CustomEvent("tabchange", { detail: { id: id } }));
  }

  NAV_TABS.forEach(function (tab) {
    if (tab.hidden) return;
    var li = document.createElement("li");
    var link = document.createElement("button");
    link.type = "button";
    link.className = "nav-link";
    link.setAttribute("data-tab-link", tab.id);
    link.innerHTML = '<span class="nav-icon">' + tab.icon + "</span><span>" + tab.label + "</span>";
    link.addEventListener("click", function () { selectTab(tab.id); });
    li.appendChild(link);
    navList.appendChild(li);
  });

  // Lets other scripts (the voice assistant's "open the calendar") switch tab.
  window.ClinicNav = { select: selectTab };

  selectTab("assistant");
});
