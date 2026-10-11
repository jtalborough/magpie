(function () {
  "use strict";

  const STORAGE_KEY = "magpie.auth.session.v1";
  const WORKSPACE_KEY = "magpie.auth.workspace.v1";
  const SCANNED_WORKSPACE_KEY = "magpie.auth.scan.workspace.v1";
  const originalFetch = window.fetch.bind(window);
  let configPromise;
  let refreshPromise;
  let resolveReady;
  window.magpieAuthConfig = null;
  window.magpieClientBootstrap = null;
  window.magpieAuthReady = new Promise((resolve) => { resolveReady = resolve; });

  function markReady(identity) {
    resolveReady(identity || null);
    window.dispatchEvent(new CustomEvent("magpie-auth-ready", {detail: identity || null}));
  }

  function readSession() {
    try {
      return JSON.parse(localStorage.getItem(STORAGE_KEY) || "null");
    } catch (_) {
      return null;
    }
  }

  function saveSession(session) {
    if (session && session.access_token) {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(session));
    } else {
      localStorage.removeItem(STORAGE_KEY);
    }
  }

  function readWorkspace() {
    return sessionStorage.getItem(SCANNED_WORKSPACE_KEY)
      || localStorage.getItem(WORKSPACE_KEY)
      || "";
  }

  function readScannedWorkspace() {
    return sessionStorage.getItem(SCANNED_WORKSPACE_KEY) || "";
  }

  function clearScannedWorkspace() {
    sessionStorage.removeItem(SCANNED_WORKSPACE_KEY);
  }

  function saveWorkspace(workspaceId) {
    if (workspaceId) localStorage.setItem(WORKSPACE_KEY, workspaceId);
    else localStorage.removeItem(WORKSPACE_KEY);
  }

  function sameOrigin(input) {
    try {
      const url = new URL(typeof input === "string" ? input : input.url, location.href);
      return url.origin === location.origin;
    } catch (_) {
      return false;
    }
  }

  function withToken(init, token) {
    const next = Object.assign({}, init || {});
    const headers = new Headers(next.headers || {});
    if (token && !headers.has("authorization")) {
      headers.set("authorization", `Bearer ${token}`);
    }
    const workspaceId = readWorkspace();
    if (workspaceId && !headers.has("x-magpie-workspace")) {
      headers.set("x-magpie-workspace", workspaceId);
    }
    next.headers = headers;
    return next;
  }

  async function loadConfig() {
    if (!configPromise) {
      configPromise = originalFetch("/auth/config", {cache: "no-store"})
        .then((response) => {
          if (!response.ok) throw new Error("Magpie sign-in configuration is unavailable");
          return response.json();
        })
        .then((config) => {
          window.magpieAuthConfig = config;
          return config;
        })
        .catch((error) => {
          configPromise = null;
          throw error;
        });
    }
    return configPromise;
  }

  async function refreshSession() {
    if (refreshPromise) return refreshPromise;
    refreshPromise = (async () => {
      const config = await loadConfig();
      const session = readSession();
      if (config.mode !== "supabase" || !session || !session.refresh_token) return null;
      const response = await originalFetch(
        `${config.supabase_url}/auth/v1/token?grant_type=refresh_token`,
        {
          method: "POST",
          headers: {
            apikey: config.publishable_key,
            "content-type": "application/json"
          },
          body: JSON.stringify({refresh_token: session.refresh_token})
        }
      );
      if (!response.ok) {
        saveSession(null);
        return null;
      }
      const refreshed = await response.json();
      saveSession(refreshed);
      return refreshed;
    })().finally(() => { refreshPromise = null; });
    return refreshPromise;
  }

  window.fetch = async function magpieAuthenticatedFetch(input, init) {
    const local = sameOrigin(input);
    const session = readSession();
    let response = await originalFetch(
      input,
      local ? withToken(init, session && session.access_token) : init
    );
    if (local && response.status === 401 && !String(new URL(
      typeof input === "string" ? input : input.url,
      location.href
    ).pathname).startsWith("/auth/")) {
      const refreshed = await refreshSession();
      if (refreshed && refreshed.access_token) {
        response = await originalFetch(input, withToken(init, refreshed.access_token));
      }
    }
    return response;
  };

  function captureImplicitSession() {
    const params = new URLSearchParams(location.hash.replace(/^#/, ""));
    const accessToken = params.get("access_token");
    if (!accessToken) return;
    saveSession({
      access_token: accessToken,
      refresh_token: params.get("refresh_token") || "",
      expires_in: Number(params.get("expires_in") || 0),
      token_type: params.get("token_type") || "bearer"
    });
    history.replaceState(null, "", `${location.pathname}${location.search}`);
  }

  function installStyles() {
    if (document.getElementById("magpieAuthStyles")) return;
    const style = document.createElement("style");
    style.id = "magpieAuthStyles";
    style.textContent = `
      .magpie-auth-shell { position:fixed; inset:0; z-index:100000; display:grid; place-items:center; padding:20px; background:var(--bg,#f5f6f8); color:var(--text,#171a1f); font-family:Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }
      body.magpie-auth-locked { overflow:hidden !important; }
      .magpie-auth-panel { width:min(400px,100%); background:var(--surface,#fff); border:1px solid var(--line,#d7dfdc); border-radius:8px; padding:26px; box-shadow:var(--shadow,0 16px 40px rgba(23,26,31,.12)); }
      .magpie-auth-step { margin:0 0 10px; color:var(--accent,#0f766e); font-size:11px; font-weight:750; text-transform:uppercase; }
      .magpie-auth-brand { margin:0 0 6px; font-size:24px; font-weight:750; }
      .magpie-auth-copy { margin:0 0 22px; color:var(--muted,#626a75); font-size:14px; line-height:1.45; }
      .magpie-auth-label { display:block; margin-bottom:6px; color:var(--muted,#626a75); font-size:12px; font-weight:650; }
      .magpie-auth-input { width:100%; min-height:44px; border:1px solid var(--line-strong,#b9c6c1); border-radius:6px; padding:10px 11px; background:var(--surface,#fff); color:var(--text,#171a1f); font:inherit; }
      .magpie-auth-input:focus-visible { outline:2px solid var(--focus-ring,#8dc8be); outline-offset:1px; border-color:var(--accent,#0f766e); }
      .magpie-auth-submit { width:100%; min-height:44px; margin-top:12px; border:1px solid var(--accent,#0f766e); border-radius:6px; background:var(--accent,#0f766e); color:var(--accent-ink,#fff); font:inherit; font-weight:700; cursor:pointer; }
      .magpie-auth-submit:disabled { opacity:.55; cursor:wait; }
      .magpie-auth-message { min-height:20px; margin:12px 0 0; color:var(--muted,#626a75); font-size:13px; line-height:1.4; }
      .magpie-auth-message.error { color:var(--danger,#b42318); }
      .magpie-auth-signout { margin-left:auto; min-height:34px; border:1px solid var(--line,#d7dfdc); border-radius:6px; padding:7px 9px; background:var(--surface,#fff); color:var(--muted,#626a75); font:inherit; font-size:12px; font-weight:650; white-space:nowrap; cursor:pointer; }
      .magpie-auth-signout.with-workspace { margin-left:0; }
      .magpie-workspace-control { margin-left:auto; display:flex; align-items:center; gap:6px; min-width:0; }
      .magpie-workspace-select { min-height:34px; max-width:190px; border:1px solid var(--line,#d7dfdc); border-radius:6px; padding:5px 28px 5px 8px; background:var(--surface,#fff); color:var(--text,#171a1f); font:inherit; font-size:12px; font-weight:650; }
      .magpie-workspace-add { width:34px; height:34px; border:1px solid var(--line,#d7dfdc); border-radius:6px; background:var(--surface,#fff); color:var(--text,#171a1f); font:inherit; font-size:20px; line-height:1; cursor:pointer; }
      .magpie-auth-cancel { width:100%; min-height:40px; margin-top:8px; border:0; background:transparent; color:var(--muted,#626a75); font:inherit; font-weight:650; cursor:pointer; }
      .magpie-auth-delete { width:100%; min-height:40px; margin-top:4px; border:0; background:transparent; color:var(--danger,#b42318); font:inherit; font-weight:650; cursor:pointer; }
      .magpie-auth-submit:focus-visible, .magpie-auth-signout:focus-visible, .magpie-workspace-select:focus-visible, .magpie-workspace-add:focus-visible, .magpie-auth-cancel:focus-visible, .magpie-auth-delete:focus-visible { outline:3px solid var(--focus-ring,#8dc8be); outline-offset:2px; }
      body.kiosk-mode .magpie-auth-signout { display:none; }
      body.kiosk-mode .magpie-workspace-control { display:none; }
      @media (max-width:640px) { .magpie-workspace-select { max-width:132px; } }
      @media (pointer:coarse) { .magpie-auth-signout, .magpie-workspace-select, .magpie-workspace-add { min-height:44px; } .magpie-workspace-add { width:44px; height:44px; } }
    `;
    document.head.appendChild(style);
  }

  function showSignIn(config, message, isError) {
    installStyles();
    document.body.classList.add("magpie-auth-locked");
    document.getElementById("magpieAuthUnavailable")?.remove();
    let shell = document.getElementById("magpieAuthShell");
    if (!shell) {
      shell = document.createElement("div");
      shell.id = "magpieAuthShell";
      shell.className = "magpie-auth-shell";
      shell.innerHTML = `
        <form class="magpie-auth-panel" id="magpieAuthForm">
          <h1 class="magpie-auth-brand">Magpie</h1>
          <p class="magpie-auth-copy">Sign in to your storage workspace.</p>
          <label class="magpie-auth-label" for="magpieAuthEmail">Email</label>
          <input class="magpie-auth-input" id="magpieAuthEmail" type="email" autocomplete="email" required>
          <button class="magpie-auth-submit" type="submit">Email sign-in link</button>
          <p class="magpie-auth-message" id="magpieAuthMessage" role="status"></p>
        </form>`;
      document.body.appendChild(shell);
      shell.querySelector("form").addEventListener("submit", async (event) => {
        event.preventDefault();
        const email = shell.querySelector("input").value.trim();
        const button = shell.querySelector("button");
        const status = shell.querySelector("[role=status]");
        button.disabled = true;
        status.className = "magpie-auth-message";
        status.textContent = "Sending sign-in link";
        try {
          const redirect = `${location.origin}${location.pathname}${location.search}`;
          const response = await originalFetch(
            `${config.supabase_url}/auth/v1/otp?redirect_to=${encodeURIComponent(redirect)}`,
            {
              method: "POST",
              headers: {
                apikey: config.publishable_key,
                Authorization: `Bearer ${config.publishable_key}`,
                "content-type": "application/json"
              },
              body: JSON.stringify({email, create_user: true})
            }
          );
          if (!response.ok) {
            const detail = await response.json().catch(() => ({}));
            throw new Error(detail.msg || detail.message || "Could not send the sign-in link");
          }
          status.textContent = "Check your email for the Magpie sign-in link.";
        } catch (error) {
          status.className = "magpie-auth-message error";
          status.textContent = error.message;
        } finally {
          button.disabled = false;
        }
      });
    }
    const status = shell.querySelector("[role=status]");
    status.textContent = message || "";
    status.className = `magpie-auth-message${isError ? " error" : ""}`;
  }

  function showAuthUnavailable(message) {
    installStyles();
    document.body.classList.add("magpie-auth-locked");
    document.getElementById("magpieAuthShell")?.remove();
    document.getElementById("magpieWorkspaceSetup")?.remove();
    document.getElementById("magpieAuthUnavailable")?.remove();
    const shell = document.createElement("div");
    shell.id = "magpieAuthUnavailable";
    shell.className = "magpie-auth-shell";
    shell.innerHTML = `
      <div class="magpie-auth-panel" role="alert">
        <h1 class="magpie-auth-brand">Magpie is unavailable</h1>
        <p class="magpie-auth-copy">Your session and workspace are still saved. Magpie could not reach the service.</p>
        <button class="magpie-auth-submit" type="button">Try again</button>
        ${readSession()?.access_token ? '<button class="magpie-auth-cancel" type="button">Sign out</button>' : ""}
        <p class="magpie-auth-message error" role="status">${escapeHtml(message || "Check the connection and try again.")}</p>
      </div>`;
    document.body.appendChild(shell);
    shell.querySelector(".magpie-auth-submit").addEventListener("click", async (event) => {
      const button = event.currentTarget;
      const status = shell.querySelector("[role=status]");
      button.disabled = true;
      status.className = "magpie-auth-message";
      status.textContent = "Reconnecting";
      try {
        shell.remove();
        await initialize();
      } catch (error) {
        showAuthUnavailable(error.message);
      }
    });
    shell.querySelector(".magpie-auth-cancel")?.addEventListener("click", () => {
      saveSession(null);
      saveWorkspace("");
      location.reload();
    });
    shell.querySelector(".magpie-auth-submit").focus();
  }

  function escapeHtml(value) {
    return String(value || "").replace(/[&<>"']/g, (char) => ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#39;"
    }[char]));
  }

  function addSignOut(identity) {
    document.body.classList.remove("magpie-auth-locked");
    document.getElementById("magpieAuthShell")?.remove();
    document.getElementById("magpieAuthUnavailable")?.remove();
    if (document.getElementById("magpieAuthSignOut")) return;
    installStyles();
    const button = document.createElement("button");
    button.id = "magpieAuthSignOut";
    button.className = "magpie-auth-signout";
    button.type = "button";
    button.textContent = "Sign out";
    button.title = identity.email || "Sign out";
    button.addEventListener("click", () => {
      saveSession(null);
      saveWorkspace("");
      location.reload();
    });
    const header = document.querySelector("header");
    if (header) header.appendChild(button);
  }

  function showWorkspaceSetup(config, canCancel) {
    installStyles();
    document.body.classList.add("magpie-auth-locked");
    document.getElementById("magpieAuthUnavailable")?.remove();
    document.getElementById("magpieWorkspaceSetup")?.remove();
    const shell = document.createElement("div");
    shell.id = "magpieWorkspaceSetup";
    shell.className = "magpie-auth-shell";
    shell.innerHTML = `
      <form class="magpie-auth-panel">
        <p class="magpie-auth-step">${canCancel ? "New workspace" : "Account setup"} · Step 1 of 2</p>
        <h1 class="magpie-auth-brand">Create your Magpie workspace</h1>
        <p class="magpie-auth-copy">This is the shared account for your household, workshop, studio, or team. You will add the physical storage spaces next.</p>
        <label class="magpie-auth-label" for="magpieWorkspaceName">Workspace name</label>
        <input class="magpie-auth-input" id="magpieWorkspaceName" autocomplete="organization" required maxlength="120" placeholder="My Home">
        <button class="magpie-auth-submit" type="submit">Create workspace</button>
        <button class="magpie-auth-cancel" type="button">${canCancel ? "Cancel" : "Sign out"}</button>
        ${canCancel ? "" : '<button class="magpie-auth-delete" type="button">Delete account</button>'}
        <p class="magpie-auth-message" role="status"></p>
      </form>`;
    document.body.appendChild(shell);
    shell.querySelector("input").focus();
    shell.querySelector(".magpie-auth-cancel")?.addEventListener("click", () => {
      if (canCancel) {
        shell.remove();
        document.body.classList.remove("magpie-auth-locked");
      } else {
        saveSession(null);
        saveWorkspace("");
        location.assign("/");
      }
    });
    shell.querySelector(".magpie-auth-delete")?.addEventListener("click", async () => {
      if (!confirm("Permanently delete your Magpie account?")) return;
      const button = shell.querySelector(".magpie-auth-delete");
      const status = shell.querySelector("[role=status]");
      button.disabled = true;
      status.className = "magpie-auth-message";
      status.textContent = "Deleting account";
      try {
        const response = await window.fetch("/account", {method: "DELETE"});
        const body = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(body.detail || "Could not delete account");
        saveSession(null);
        saveWorkspace("");
        location.reload();
      } catch (error) {
        status.className = "magpie-auth-message error";
        status.textContent = error.message;
        button.disabled = false;
      }
    });
    shell.querySelector("form").addEventListener("submit", async (event) => {
      event.preventDefault();
      const button = shell.querySelector(".magpie-auth-submit");
      const status = shell.querySelector("[role=status]");
      button.disabled = true;
      status.className = "magpie-auth-message";
      status.textContent = "Creating workspace";
      try {
        const response = await window.fetch("/workspaces", {
          method: "POST",
          headers: {"content-type": "application/json"},
          body: JSON.stringify({name: shell.querySelector("input").value.trim()})
        });
        const body = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(body.detail || "Could not create workspace");
        saveWorkspace(body.workspace.id);
        location.assign("/storage?onboarding=1");
      } catch (error) {
        status.className = "magpie-auth-message error";
        status.textContent = error.message;
        button.disabled = false;
      }
    });
  }

  async function addWorkspaceControl(config, identity, providedWorkspaces) {
    const workspaces = Array.isArray(providedWorkspaces) ? providedWorkspaces : [];
    if (!workspaces.length) {
      showWorkspaceSetup(config, false);
      return;
    }
    document.getElementById("magpieWorkspaceControl")?.remove();
    const control = document.createElement("div");
    control.id = "magpieWorkspaceControl";
    control.className = "magpie-workspace-control";
    const select = document.createElement("select");
    select.className = "magpie-workspace-select";
    select.title = "Storage workspace";
    workspaces.forEach((workspace) => {
      const option = document.createElement("option");
      option.value = workspace.id;
      option.textContent = workspace.name || "Storage";
      option.selected = workspace.id === identity.workspace_id;
      select.appendChild(option);
    });
    select.addEventListener("change", () => {
      saveWorkspace(select.value);
      location.reload();
    });
    const add = document.createElement("button");
    add.type = "button";
    add.className = "magpie-workspace-add";
    add.textContent = "+";
    add.title = "Create workspace";
    add.setAttribute("aria-label", "Create workspace");
    add.addEventListener("click", () => showWorkspaceSetup(config, true));
    control.append(select, add);
    const header = document.querySelector("header");
    if (header) {
      header.appendChild(control);
      const signOut = document.getElementById("magpieAuthSignOut");
      if (signOut) {
        signOut.classList.add("with-workspace");
        header.appendChild(signOut);
      }
    }
  }

  async function initialize() {
    captureImplicitSession();
    const config = await loadConfig();
    if (config.mode !== "supabase") {
      markReady(null);
      return;
    }
    let session = readSession();
    if (!session || !session.access_token) {
      showSignIn(config);
      return;
    }
    let response = await window.fetch("/client/bootstrap", {cache: "no-store"});
    if (response.status === 401) {
      session = await refreshSession();
      if (session) response = await window.fetch("/client/bootstrap", {cache: "no-store"});
    }
    if (response.status === 403 && readWorkspace()) {
      if (readScannedWorkspace()) clearScannedWorkspace();
      else saveWorkspace("");
      response = await window.fetch("/client/bootstrap", {cache: "no-store"});
    }
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      if (response.status === 403) {
        showSignIn(config, body.detail || "This account does not have access to this workspace.", true);
      } else if (response.status === 401) {
        saveSession(null);
        showSignIn(config, body.detail || "Your session expired. Sign in again.", true);
      } else {
        showAuthUnavailable(
          typeof body.detail === "string"
            ? body.detail
            : `Magpie could not start (${response.status}).`
        );
      }
      return;
    }
    const body = await response.json();
    window.magpieClientBootstrap = body;
    const identity = body.identity || {};
    if (!identity.workspace_id) {
      showWorkspaceSetup(config, false);
      return;
    }
    if (!readScannedWorkspace()) saveWorkspace(identity.workspace_id);
    addSignOut(identity);
    await addWorkspaceControl(config, identity, body.workspaces);
    markReady(identity);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", () => initialize().catch((error) => {
      showAuthUnavailable(error.message);
    }));
  } else {
    initialize().catch((error) => {
      showAuthUnavailable(error.message);
    });
  }
  if ("serviceWorker" in navigator) {
    const controlledAtLoad = Boolean(navigator.serviceWorker.controller);
    let reloadingForUpdate = false;
    navigator.serviceWorker.addEventListener("controllerchange", () => {
      if (!controlledAtLoad || reloadingForUpdate) return;
      reloadingForUpdate = true;
      window.location.reload();
    });
    navigator.serviceWorker
      .register("/service-worker.js", {updateViaCache:"none"})
      .then((registration) => registration.update())
      .catch(() => {});
  }
})();
