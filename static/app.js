/* Audit Master — interactions. Vanilla, no dependencies. */
(function () {
  "use strict";

  var $ = function (s, r) { return (r || document).querySelector(s); };
  var $$ = function (s, r) { return Array.prototype.slice.call((r || document).querySelectorAll(s)); };

  /* ---------------- toast ---------------- */
  var toastEl = $("#toast"), toastTimer;
  function toast(msg) {
    if (!toastEl) return;
    toastEl.textContent = msg;
    toastEl.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { toastEl.classList.remove("show"); }, 2200);
  }

  /* ---------------- upload: drag & drop ---------------- */
  function humanSize(n) {
    if (n < 1024) return n + " B";
    if (n < 1048576) return (n / 1024).toFixed(1) + " KB";
    return (n / 1048576).toFixed(1) + " MB";
  }

  function initDrop(zone) {
    var input = $("[data-input]", zone),
        empty = $("[data-empty]", zone),
        fileBox = $("[data-file]", zone),
        clearBtn = $("[data-clear]", zone);
    if (!input) return;

    function render() {
      var f = input.files && input.files[0];
      if (f) {
        var ext = (f.name.split(".").pop() || "?").toUpperCase().slice(0, 4);
        $("[data-ext]", zone).textContent = ext;
        $("[data-name]", zone).textContent = f.name;
        $("[data-size]", zone).textContent = humanSize(f.size);
        empty.hidden = true; fileBox.hidden = false;
        zone.classList.add("filled");
      } else {
        empty.hidden = false; fileBox.hidden = true;
        zone.classList.remove("filled");
      }
      syncSubmit();
    }

    input.addEventListener("change", render);

    ["dragenter", "dragover"].forEach(function (ev) {
      zone.addEventListener(ev, function (e) {
        e.preventDefault(); e.stopPropagation();
        zone.classList.add("over");
      });
    });
    ["dragleave", "drop"].forEach(function (ev) {
      zone.addEventListener(ev, function (e) {
        e.preventDefault(); e.stopPropagation();
        if (ev === "dragleave" && zone.contains(e.relatedTarget)) return;
        zone.classList.remove("over");
      });
    });
    zone.addEventListener("drop", function (e) {
      var dt = e.dataTransfer;
      if (dt && dt.files && dt.files.length) {
        input.files = dt.files;
        render();
      }
    });

    if (clearBtn) {
      clearBtn.addEventListener("click", function (e) {
        e.preventDefault(); e.stopPropagation();
        input.value = "";
        render();
      });
    }
  }

  // The audit log alone gives a summary; adding a plan turns it into a
  // validation. The button says which one is about to happen.
  function syncSubmit() {
    var go = $("#go"), hint = $("#hint"), lbl = $("[data-golabel]");
    if (!go) return;
    var audit = $("#audit"), plan = $("#plan");
    var haveAudit = audit && audit.files && audit.files.length;
    var havePlan = plan && plan.files && plan.files.length;
    go.disabled = !haveAudit;
    if (lbl) lbl.textContent = havePlan ? "Validate against plan" : "Summarise log";
    if (hint) {
      hint.textContent = !haveAudit
        ? "Choose an audit log to continue"
        : (havePlan ? "Ready — the log will be checked against the plan"
                    : "Ready — add a plan to validate instead of summarise");
      hint.style.color = haveAudit ? "var(--ok)" : "";
    }
    var strict = $("#strictness");
    if (strict) strict.hidden = !havePlan;
  }

  $$("[data-drop]").forEach(initDrop);
  syncSubmit();

  var form = $("#form");
  if (form) {
    form.addEventListener("submit", function () {
      var go = $("#go");
      if (!go || go.disabled) return;
      go.disabled = true;
      var lbl = $("[data-golabel]", go);
      if (lbl) lbl.textContent = "Validating…";
      go.insertBefore(Object.assign(document.createElement("span"), { className: "spin" }), go.firstChild);
    });
  }

  /* ---------------- tabs ---------------- */
  var tabbar = $("#tabs");
  if (tabbar) {
    var tabs = $$("button[data-tab]", tabbar);

    function show(name, push) {
      var found = false;
      tabs.forEach(function (t) {
        var on = t.dataset.tab === name;
        t.setAttribute("aria-selected", String(on));
        if (on) found = true;
      });
      if (!found) return false;
      $$("[data-panel]").forEach(function (p) { p.hidden = p.dataset.panel !== name; });
      if (push && history.replaceState) history.replaceState(null, "", "#" + name);
      return true;
    }

    tabbar.addEventListener("click", function (e) {
      var b = e.target.closest("button[data-tab]");
      if (b) show(b.dataset.tab, true);
    });

    // Arrow-key navigation across the tab strip.
    tabbar.addEventListener("keydown", function (e) {
      if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
      var i = tabs.findIndex(function (t) { return t.getAttribute("aria-selected") === "true"; });
      var next = tabs[(i + (e.key === "ArrowRight" ? 1 : tabs.length - 1)) % tabs.length];
      if (next) { show(next.dataset.tab, true); next.focus(); }
    });

    document.addEventListener("click", function (e) {
      var g = e.target.closest("[data-goto]");
      if (g) { show(g.dataset.goto, true); window.scrollTo({ top: 0, behavior: "smooth" }); }
    });

    if (location.hash) show(location.hash.slice(1), false);
  }

  /* ---------------- animated counters ---------------- */
  var reduceMotion = window.matchMedia &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  $$(".stat .n[data-n]").forEach(function (el) {
    var target = parseInt(el.dataset.n, 10) || 0;
    var suffix = el.dataset.suffix || "";
    // The true value is already rendered server-side, so bailing out here
    // leaves the correct number on screen rather than a stuck zero.
    if (target === 0 || reduceMotion || !window.requestAnimationFrame) return;
    el.textContent = "0" + suffix;
    var start = performance.now(), dur = 520;
    function step(now) {
      var p = Math.min((now - start) / dur, 1);
      // ease-out cubic
      var v = Math.round(target * (1 - Math.pow(1 - p, 3)));
      el.textContent = v.toLocaleString() + suffix;
      if (p < 1) requestAnimationFrame(step);
    }
    requestAnimationFrame(step);
  });

  /* ---------------- expandable rows ---------------- */
  document.addEventListener("click", function (e) {
    var tr = e.target.closest("tr.expandable");
    if (!tr) return;
    var target = document.getElementById(tr.dataset.toggle);
    if (!target) return;
    var open = tr.getAttribute("aria-expanded") === "true";
    tr.setAttribute("aria-expanded", String(!open));
    target.hidden = open;
  });

  /* ---------------- audit row search + filter + sort ---------------- */
  var table = $("#rowtable");
  if (table) {
    var body = $("tbody", table),
        rows = $$("tbody tr", table),
        search = $("#rowsearch"),
        clearSearch = $("[data-clearsearch]"),
        countEl = $("#rowcount"),
        emptyEl = $("#rowempty"),
        filters = $("#rowfilters"),
        activeFilter = "all",
        query = "";

    rows.forEach(function (tr) { tr.dataset.text = tr.textContent.toLowerCase(); });

    function apply() {
      var shown = 0;
      rows.forEach(function (tr) {
        var okFilter = activeFilter === "all" || tr.dataset.attr === activeFilter;
        var okQuery = !query || tr.dataset.text.indexOf(query) !== -1;
        var vis = okFilter && okQuery;
        tr.hidden = !vis;
        if (vis) shown++;
      });
      if (countEl) countEl.textContent = shown + " of " + rows.length + " rows";
      if (emptyEl) emptyEl.hidden = shown !== 0;
      if (clearSearch) clearSearch.hidden = !query;
    }

    if (search) {
      var t;
      search.addEventListener("input", function () {
        clearTimeout(t);
        t = setTimeout(function () { query = search.value.trim().toLowerCase(); apply(); }, 90);
      });
      search.addEventListener("keydown", function (e) {
        if (e.key === "Escape") { search.value = ""; query = ""; apply(); }
      });
    }
    if (clearSearch) {
      clearSearch.addEventListener("click", function () {
        search.value = ""; query = ""; apply(); search.focus();
      });
    }
    if (filters) {
      filters.addEventListener("click", function (e) {
        var c = e.target.closest(".chip");
        if (!c) return;
        activeFilter = c.dataset.f;
        $$(".chip", filters).forEach(function (x) {
          x.setAttribute("aria-pressed", String(x === c));
        });
        apply();
      });
    }

    // Column sorting.
    $$("th.sortable", table).forEach(function (th, idx) {
      th.addEventListener("click", function () {
        var dir = th.dataset.dir === "asc" ? "desc" : "asc";
        $$("th.sortable", table).forEach(function (o) { delete o.dataset.dir; });
        th.dataset.dir = dir;
        var numeric = th.dataset.sort === "num", mul = dir === "asc" ? 1 : -1;
        var sorted = rows.slice().sort(function (a, b) {
          var av = a.cells[idx] ? a.cells[idx].textContent.trim() : "";
          var bv = b.cells[idx] ? b.cells[idx].textContent.trim() : "";
          if (numeric) return ((parseFloat(av) || 0) - (parseFloat(bv) || 0)) * mul;
          return av.localeCompare(bv) * mul;
        });
        sorted.forEach(function (tr) { body.appendChild(tr); });
      });
    });

    apply();

    // "/" focuses search, the way a search-first UI should behave.
    document.addEventListener("keydown", function (e) {
      if (e.key === "/" && document.activeElement !== search &&
          !/^(INPUT|TEXTAREA)$/.test(document.activeElement.tagName)) {
        var panel = search.closest("[data-panel]");
        if (panel && !panel.hidden) { e.preventDefault(); search.focus(); }
      }
    });
  }

  /* ---------------- screen-set filter (summary page) ---------------- */
  var setTable = $("#settable");
  if (setTable) {
    var setRows = $$("tbody tr.expandable", setTable),
        setSearch = $("#setsearch"),
        setCount = $("#setcount"),
        setEmpty = $("#setempty");

    setRows.forEach(function (tr) {
      // Include the hidden detail row, so buttons and languages are searchable.
      var detail = document.getElementById(tr.dataset.toggle);
      tr.dataset.text = (tr.textContent + " " + (detail ? detail.textContent : "")).toLowerCase();
    });

    function applySets() {
      var q = setSearch ? setSearch.value.trim().toLowerCase() : "";
      var shown = 0;
      setRows.forEach(function (tr) {
        var vis = !q || tr.dataset.text.indexOf(q) !== -1;
        tr.hidden = !vis;
        var detail = document.getElementById(tr.dataset.toggle);
        if (detail && !vis) {
          detail.hidden = true;
          tr.setAttribute("aria-expanded", "false");
        }
        if (vis) shown++;
      });
      if (setCount) setCount.textContent = shown + " of " + setRows.length + " screen sets";
      if (setEmpty) setEmpty.hidden = shown !== 0;
    }

    if (setSearch) {
      var st;
      setSearch.addEventListener("input", function () {
        clearTimeout(st);
        st = setTimeout(applySets, 90);
      });
      setSearch.addEventListener("keydown", function (e) {
        if (e.key === "Escape") { setSearch.value = ""; applySets(); }
      });
    }
    applySets();
  }

  /* ---------------- theme toggle ---------------- */
  var themeBtn = $("#themetoggle");
  if (themeBtn) {
    var root = document.documentElement;

    function label() {
      var dark = root.getAttribute("data-theme") === "dark";
      var next = dark ? "light" : "dark";
      themeBtn.setAttribute("aria-label", "Switch to " + next + " theme");
      themeBtn.setAttribute("title", "Switch to " + next + " theme");
    }

    themeBtn.addEventListener("click", function () {
      var dark = root.getAttribute("data-theme") === "dark";
      root.setAttribute("data-theme", dark ? "light" : "dark");
      try { localStorage.setItem("audit-theme", dark ? "light" : "dark"); } catch (e) {}
      label();
    });

    // Follow the OS while the user has not made an explicit choice.
    try {
      var mq = window.matchMedia("(prefers-color-scheme: dark)");
      var onChange = function (e) {
        if (localStorage.getItem("audit-theme")) return;
        root.setAttribute("data-theme", e.matches ? "dark" : "light");
        label();
      };
      if (mq.addEventListener) mq.addEventListener("change", onChange);
      else if (mq.addListener) mq.addListener(onChange);
    } catch (e) {}

    label();
  }

  /* ---------------- copy summary ---------------- */
  var copyBtn = $("[data-copy]");
  if (copyBtn) {
    copyBtn.addEventListener("click", function () {
      var parts = [];
      var h = $("#headline");
      if (h) parts.push(h.textContent.trim());
      $$("#narrative p").forEach(function (p) { parts.push(p.textContent.trim()); });
      var text = parts.join("\n\n");
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(
          function () { toast("Summary copied"); },
          function () { toast("Could not copy"); }
        );
      } else {
        var ta = document.createElement("textarea");
        ta.value = text; document.body.appendChild(ta); ta.select();
        try { document.execCommand("copy"); toast("Summary copied"); }
        catch (err) { toast("Could not copy"); }
        document.body.removeChild(ta);
      }
    });
  }
})();
