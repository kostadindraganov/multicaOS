/* ============================================================
   house-style / shell.js  —  Creator Magic shared behaviours
   ------------------------------------------------------------
   Two custom elements every app shares, so STRUCTURE (not just
   colour) flows from one place. Link AFTER tokens.css + shell.css:

     <script defer src="https://your-house-style-host/v1/shell.js"></script>

   Vanilla, no build, no deps. Light DOM (not shadow) on purpose so
   shell.css styles reach inside. Change a component here and every
   app that links this file picks it up — that is the whole point.

     <cm-drawer id="side" title="Details">…</cm-drawer>   right slide-out
     <cm-chat endpoint="/chat"></cm-chat>                  chat box

   toggle the drawer from anywhere:  toggleDrawer('side')
   ============================================================ */
(function () {
  "use strict";

  /* ---------------- <cm-drawer> : right-hand slide-out panel ----------------
     A persistent off-canvas panel. Its children become the body; a header
     with a close button is added. open()/close()/toggle() flip the .open
     class (animated in shell.css) and emit cm-open / cm-close events so the
     host app can lazy-load contents. */
  class CmDrawer extends HTMLElement {
    connectedCallback() {
      if (this._init) return;
      this._init = true;
      const title = this.getAttribute("title") || "";
      const body = document.createElement("div");
      body.className = "drawer-body";
      while (this.firstChild) body.appendChild(this.firstChild);
      this.classList.add("drawer");
      this.innerHTML =
        '<div class="drawer-head"><span class="drawer-title"></span>' +
        '<button class="icon-btn drawer-x" aria-label="close">✕</button></div>';
      this.querySelector(".drawer-title").textContent = title;
      this.appendChild(body);
      this.querySelector(".drawer-x").addEventListener("click", () => this.close());
      if (this.hasAttribute("open")) this.open();
    }
    get body() { return this.querySelector(".drawer-body"); }
    open() {
      this.classList.add("open");
      this.dispatchEvent(new CustomEvent("cm-open", { bubbles: true }));
    }
    close() {
      this.classList.remove("open");
      this.dispatchEvent(new CustomEvent("cm-close", { bubbles: true }));
    }
    toggle() { this.classList.contains("open") ? this.close() : this.open(); }
  }

  /* ---------------- <cm-chat> : shared chat box ----------------
     Renders the house composer + a message log. Two ways to use it:

       1. Decoupled (default): emits a `cm-send` CustomEvent { detail:{text} }
          on submit. The host app handles transport however it likes and calls
          el.append('bot', reply) / el.append('user', text).
       2. Wired: set endpoint="/path" and it POSTs {message} as JSON and appends
          the JSON reply ({reply} or {text} or the raw string) for you.

     Attributes: endpoint, placeholder. Enter sends; Shift+Enter = newline. */
  class CmChat extends HTMLElement {
    connectedCallback() {
      if (this._init) return;
      this._init = true;
      const ph = this.getAttribute("placeholder") || "Message…";
      this.classList.add("chat");
      this.innerHTML =
        '<div class="chat-log" role="log" aria-live="polite"></div>' +
        '<div class="compose"><div class="compose-inner"><div class="cbox">' +
        '<textarea rows="1" placeholder=""></textarea>' +
        '<div class="cbar"><span class="lft"></span>' +
        '<button class="btn btn-spark cm-send">Send</button></div>' +
        "</div></div></div>";
      this.log = this.querySelector(".chat-log");
      this.ta = this.querySelector("textarea");
      this.ta.placeholder = ph;
      this.querySelector(".cm-send").addEventListener("click", () => this._submit());
      this.ta.addEventListener("keydown", (e) => {
        if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); this._submit(); }
      });
      this.ta.addEventListener("input", () => {
        this.ta.style.height = "auto";
        this.ta.style.height = Math.min(this.ta.scrollHeight, 180) + "px";
      });
    }
    append(role, text) {
      const m = document.createElement("div");
      m.className = "msg " + (role === "user" ? "user" : "bot");
      m.textContent = text;
      this.log.appendChild(m);
      this.log.scrollTop = this.log.scrollHeight;
      return m;
    }
    async _submit() {
      const text = this.ta.value.trim();
      if (!text) return;
      this.append("user", text);
      this.ta.value = "";
      this.ta.style.height = "auto";
      this.dispatchEvent(new CustomEvent("cm-send", { bubbles: true, detail: { text } }));
      const endpoint = this.getAttribute("endpoint");
      if (!endpoint) return;
      const pending = this.append("bot", "…");
      try {
        const r = await fetch(endpoint, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ message: text }),
        });
        const ct = r.headers.get("content-type") || "";
        let reply;
        if (ct.includes("application/json")) {
          const j = await r.json();
          reply = j.reply ?? j.text ?? j.message ?? JSON.stringify(j);
        } else {
          reply = await r.text();
        }
        pending.textContent = reply;
      } catch (e) {
        pending.className = "msg bot err";
        pending.textContent = "⚠ " + e;
      }
      this.log.scrollTop = this.log.scrollHeight;
    }
  }

  if (!customElements.get("cm-drawer")) customElements.define("cm-drawer", CmDrawer);
  if (!customElements.get("cm-chat")) customElements.define("cm-chat", CmChat);

  // Toggle any drawer by id from inline handlers / anywhere.
  window.toggleDrawer = function (id) {
    const el = document.getElementById(id);
    if (el && typeof el.toggle === "function") el.toggle();
  };

  /* ---------------- off-canvas rail (mobile) ----------------
     The left .rail collapses into a slide-in drawer below 768px (look in
     shell.css). toggleRail(force) flips body.rail-open; the .hamburger calls
     it. It does NOT auto-close on row taps — apps that navigate on a tap call
     toggleRail(false) themselves. initRail() injects the tap-to-dismiss
     backdrop once and snaps the rail shut again on resize back to desktop. */
  window.toggleRail = function (force) {
    const open = typeof force === "boolean"
      ? force : !document.body.classList.contains("rail-open");
    document.body.classList.toggle("rail-open", open);
  };
  function initRail() {
    const rail = document.querySelector(".rail");
    if (!rail || document.querySelector(".rail-backdrop")) return;
    const bd = document.createElement("div");
    bd.className = "rail-backdrop";
    bd.addEventListener("click", () => window.toggleRail(false));
    document.body.appendChild(bd);
    window.addEventListener("resize", () => {
      if (window.innerWidth > 768) window.toggleRail(false);
    });
  }
  if (document.readyState === "loading")
    document.addEventListener("DOMContentLoaded", initRail);
  else initRail();
})();
