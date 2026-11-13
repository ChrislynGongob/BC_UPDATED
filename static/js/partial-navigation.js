(function () {
  "use strict";

  if (!document.body) return;

  const content = document.querySelector("main.content");
  const role = document.body.dataset.role;
  const supportedPatterns = {
    staff: [
      /^\/staff\/(dashboard|complaints|requests|residents)$/,
      /^\/management\/community-issues(?:\/\d+)?$/,
      /^\/(requests|complaints)\/\d+$/,
      /^\/(notifications|search)$/,
    ],
    admin: [
      /^\/admin\/(dashboard|residents|staff|administrators|requests|complaints|announcements|audit-logs)$/,
      /^\/(admin\/reports|reports)$/,
      /^\/management\/community-issues(?:\/\d+)?$/,
      /^\/(requests|complaints)\/\d+$/,
      /^\/(notifications|search)$/,
    ],
    resident: [
      /^\/(dashboard|requests|complaints|hotlines|profile|notifications|search)$/,
      /^\/community-reports(?:\/\d+)?$/,
      /^\/(requests|complaints)\/\d+$/,
    ],
  }[role] || [];
  const nav = document.querySelector(".nav-menu");
  let activeController = null;
  let activeUrl = null;
  let navigationSequence = 0;
  let loadingTimer = null;

  if (!content || !nav || !window.fetch || !window.history.pushState) return;

  function supportedUrl(value) {
    const url = value instanceof URL ? value : new URL(value, window.location.href);
    return url.origin === window.location.origin
      && supportedPatterns.some(function (pattern) { return pattern.test(url.pathname); })
      ? url
      : null;
  }

  function navigationPath(url) {
    const path = url.pathname;
    if (/^\/requests\/\d+$/.test(path)) {
      return role === "admin" ? "/admin/requests" : role === "staff" ? "/staff/requests" : "/requests";
    }
    if (/^\/complaints\/\d+$/.test(path)) {
      return role === "admin" ? "/admin/complaints" : role === "staff" ? "/staff/complaints" : "/complaints";
    }
    if (/^\/management\/community-issues/.test(path)) return "/management/community-issues";
    if (/^\/community-reports/.test(path)) return "/community-reports";
    if (path === "/reports") return "/admin/reports";
    return path;
  }

  function markSelected(url) {
    const activePath = navigationPath(url);
    const navToggle = document.getElementById("nav-toggle");
    if (navToggle) navToggle.checked = false;
    nav.querySelectorAll("a[href]").forEach(function (link) {
      const selected = navigationPath(new URL(link.href, window.location.href)) === activePath;
      link.classList.toggle("active", selected);
      if (selected) link.setAttribute("aria-current", "page");
      else link.removeAttribute("aria-current");
    });
    document.body.classList.toggle(
      "resident-dashboard-page",
      role === "resident" && url.pathname === "/dashboard"
    );
    const topbar = document.querySelector(".topbar");
    if (topbar) {
      topbar.classList.toggle(
        "resident-dashboard-topbar",
        role === "resident" && url.pathname === "/dashboard"
      );
      let mobileBrand = topbar.querySelector(".mobile-brand");
      if (role === "resident" && url.pathname === "/dashboard" && !mobileBrand) {
        mobileBrand = document.createElement("a");
        mobileBrand.className = "mobile-brand";
        mobileBrand.href = "/dashboard";
        mobileBrand.setAttribute("aria-label", "BarangayConnect Resident Portal");
        mobileBrand.innerHTML = "<strong>BarangayConnect</strong><small>Resident Portal</small>";
        topbar.insertBefore(mobileBrand, topbar.querySelector(".welcome"));
      } else if (mobileBrand && !(role === "resident" && url.pathname === "/dashboard")) {
        mobileBrand.remove();
      }
    }
  }

  function setBusy(busy) {
    content.setAttribute("aria-busy", busy ? "true" : "false");
    content.classList.toggle("bc-nav-loading", busy);
    if (!busy) {
      window.clearTimeout(loadingTimer);
      loadingTimer = null;
    }
  }

  function cancelActiveNavigation() {
    if (!activeController) return;
    activeController.abort();
    activeController = null;
    activeUrl = null;
    navigationSequence += 1;
    setBusy(false);
    markSelected(new URL(window.location.href));
  }

  async function navigate(value, options) {
    const url = supportedUrl(value);
    if (!url) return false;
    const settings = options || {};
    if (activeController) activeController.abort();
    window.clearTimeout(loadingTimer);

    const controller = new AbortController();
    activeController = controller;
    activeUrl = url.href;
    const sequence = ++navigationSequence;
    const startedAt = performance.now();
    markSelected(url);
    content.setAttribute("aria-busy", "true");
    loadingTimer = window.setTimeout(function () {
      if (sequence === navigationSequence) content.classList.add("bc-nav-loading");
    }, 140);

    try {
      const response = await fetch(url.href, {
        method: "GET",
        credentials: "same-origin",
        cache: "no-store",
        headers: { "Accept": "text/html", "X-BC-Partial": "1" },
        signal: controller.signal,
      });
      if (sequence !== navigationSequence) return true;
      if (!response.ok || response.headers.get("X-BC-Partial") !== "1") {
        window.location.assign(response.url || url.href);
        return true;
      }

      const fragment = document.createElement("template");
      fragment.innerHTML = await response.text();
      const page = fragment.content.querySelector(".bc-nav-fragment");
      if (!page) {
        window.location.assign(response.url || url.href);
        return true;
      }

      content.innerHTML = page.innerHTML;
      markSelected(url);
      if (settings.mode === "push") {
        window.history.replaceState(
          Object.assign({}, window.history.state, { bcPartial: true, scrollY: window.scrollY }),
          "",
          window.location.href
        );
        window.history.pushState({ bcPartial: true, scrollY: 0 }, "", url.href);
        window.scrollTo(0, 0);
      } else if (settings.mode === "pop" && Number.isFinite(settings.scrollY)) {
        window.scrollTo(0, settings.scrollY);
      }

      const title = content.querySelector(".page-title");
      if (title) document.title = title.textContent.trim() + " — BarangayConnect";
      document.dispatchEvent(new CustomEvent("bc:page-loaded", {
        detail: { path: url.pathname, durationMs: performance.now() - startedAt },
      }));
      return true;
    } catch (error) {
      if (error.name !== "AbortError" && sequence === navigationSequence) {
        window.location.assign(url.href);
      }
      return true;
    } finally {
      if (sequence === navigationSequence) {
        setBusy(false);
        activeController = null;
        activeUrl = null;
      }
    }
  }

  function isPlainLeftClick(event) {
    return event.button === 0 && !event.metaKey && !event.ctrlKey
      && !event.shiftKey && !event.altKey;
  }

  document.addEventListener("click", function (event) {
    if (!isPlainLeftClick(event)) return;
    const link = event.target.closest("a[href]");
    if (!link || link.hasAttribute("download") || link.target) return;
    const url = supportedUrl(link.href);
    if (!url) return;
    if (activeController && activeUrl === url.href) {
      event.preventDefault();
      return;
    }
    if (url.pathname === window.location.pathname && url.search === window.location.search) {
      if (activeController) {
        event.preventDefault();
        cancelActiveNavigation();
      }
      return;
    }
    event.preventDefault();
    navigate(url, { mode: "push" });
  });

  function submitGetForm(form) {
    if (form.method.toUpperCase() !== "GET" || form.target) return false;
    const actionValue = form.getAttribute("action") || window.location.pathname;
    const action = supportedUrl(new URL(actionValue, window.location.href));
    if (!action) return false;
    const url = new URL(action.href);
    new FormData(form).forEach(function (value, key) {
      if (typeof value === "string") url.searchParams.append(key, value);
    });
    navigate(url, { mode: "push" });
    return true;
  }

  document.addEventListener("submit", function (event) {
    if (event.target instanceof HTMLFormElement && submitGetForm(event.target)) {
      event.preventDefault();
    }
  }, true);

  // The complaint filter uses a legacy inline onchange="form.submit()" handler.
  // Capture its change and navigate through the same partial path.
  document.addEventListener("change", function (event) {
    const form = event.target.closest("form");
    if (form && submitGetForm(form)) event.stopImmediatePropagation();
  }, true);

  window.addEventListener("popstate", function (event) {
    if (!event.state || !event.state.bcPartial) return;
    navigate(window.location.href, { mode: "pop", scrollY: event.state.scrollY });
  });

  const initialUrl = supportedUrl(window.location.href);
  if (initialUrl) {
    window.history.replaceState(
      Object.assign({}, window.history.state, { bcPartial: true, scrollY: window.scrollY }),
      "",
      window.location.href
    );
    markSelected(initialUrl);
  }
})();
