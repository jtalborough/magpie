(function () {
  "use strict";

  const PRODUCT_VIEWS = Object.freeze([
    {key: "inventory", label: "Items", ariaLabel: "Browse items", context: "Items", href: "/?view=inventory"},
    {key: "places", label: "Places", ariaLabel: "Browse places", context: "Places", href: "/"},
    {key: "review", label: "Needs review", ariaLabel: "Review Magpie decisions", context: "Needs review", href: "/review"},
    {key: "account", label: "Account", ariaLabel: "Manage account", context: "Account", href: "/account"},
  ]);
  const ASSISTANT_SURFACE_KEY = "magpie.assistant-surface";
  let reviewCount = 0;

  function prefersMobileAssistant(url = window.location) {
    const mobilePath = url.pathname === "/mobile";
    const desktopPath = url.pathname === "/";
    try {
      if (mobilePath) window.sessionStorage.setItem(ASSISTANT_SURFACE_KEY, "mobile");
      else if (desktopPath) window.sessionStorage.removeItem(ASSISTANT_SURFACE_KEY);
      if (window.sessionStorage.getItem(ASSISTANT_SURFACE_KEY) === "mobile") return true;
    } catch (_error) {
      // The viewport fallback still keeps private browsing sessions usable.
    }
    return mobilePath || window.matchMedia("(max-width: 680px)").matches;
  }

  function viewHref(view, url = window.location) {
    if (window.matchMedia("(max-width:900px)").matches) {
      if (view.key === "inventory") return "/storage?view=inventory";
      if (view.key === "places") return "/cabinet";
      if (view.key === "chat") return "/mobile";
    }
    return view.href;
  }

  function activeView(url = window.location) {
    if (
      url.pathname === "/"
      || url.pathname === "/mobile"
    ) return url.pathname === "/mobile" ? "chat"
      : new URLSearchParams(url.search).get("view") === "inventory" ? "inventory" : "places";
    if (url.pathname === "/cabinet") return new URLSearchParams(url.search).get("view") === "inventory" ? "inventory" : "places";
    if (/^\/(?:scan\/[^/]+\/)?i\/[^/]+\/?$/.test(url.pathname)) return "inventory";
    if (/^\/(?:scan\/[^/]+\/)?l\/[^/]+\/?$/.test(url.pathname)) return "places";
    if (url.pathname === "/review") return "review";
    if (url.pathname === "/account") return "account";
    if (url.pathname === "/storage") {
      const params = new URLSearchParams(url.search);
      const view = params.get("view");
      if (view === "places" || view === "rules") return "places";
      if (view === "inventory") return "inventory";
      if (params.has("location") || params.has("resume")) return "places";
      return "inventory";
    }
    return "";
  }

  function activeContext(url = window.location) {
    if (
      url.pathname === "/storage"
      && new URLSearchParams(url.search).get("view") === "rules"
    ) return "Rules";
    const view = PRODUCT_VIEWS.find((candidate) => candidate.key === activeView(url));
    return view?.context || "";
  }

  function renderNavigation(nav, currentView = activeView(), url = window.location) {
    const focusedView = nav.contains(document.activeElement) ? document.activeElement.dataset.productView : null;
    const fragment = document.createDocumentFragment();
    const views = window.matchMedia("(max-width:900px)").matches
      ? [{key:"chat", label:"Chat", ariaLabel:"Chat with Magpie", href:"/mobile"}, ...PRODUCT_VIEWS]
      : PRODUCT_VIEWS;
    for (const view of views) {
      const link = document.createElement("a");
      link.href = viewHref(view, url);
      link.textContent = view.key === "review" && reviewCount ? `${view.label} (${reviewCount})` : view.label;
      link.setAttribute("aria-label", view.ariaLabel);
      link.title = view.ariaLabel;
      link.dataset.productView = view.key;
      if (view.key === currentView) {
        link.classList.add("active");
        link.setAttribute("aria-current", "page");
      }
      fragment.append(link);
    }
    nav.replaceChildren(fragment);
    if (focusedView) nav.querySelector(`[data-product-view="${focusedView}"]`)?.focus({preventScroll:true});
  }

  function renderAll(root = document) {
    const currentView = activeView();
    root.querySelectorAll("[data-product-nav]").forEach((nav) => {
      renderNavigation(nav, currentView);
    });
    const contextLabel = activeContext();
    if (contextLabel) {
      root.querySelectorAll("[data-product-context]").forEach((context) => {
        context.textContent = contextLabel;
      });
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", () => renderAll(), {once: true});
  } else {
    renderAll();
  }
  window.matchMedia("(max-width:900px)").addEventListener("change", () => renderAll());

  window.MagpieProductShell = Object.freeze({
    setReviewCount(count) { reviewCount = Math.max(0, Number(count) || 0); renderAll(); },
    activeContext,
    activeView,
    prefersMobileAssistant,
    renderAll,
    renderNavigation,
    viewHref,
    views: PRODUCT_VIEWS,
  });
})();
