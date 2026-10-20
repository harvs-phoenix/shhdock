// ShhDock Browser Link - polls the local bridge for commands and runs them
// in the active tab. MV3 service worker; no external libraries.

const BASE = "http://127.0.0.1:8765";
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

let looping = false;

async function loop() {
  if (looping) return;
  looping = true;
  for (;;) {
    let cmd = null;
    try {
      const r = await fetch(BASE + "/poll?wait=4");
      if (r.status === 200) cmd = await r.json();
    } catch (e) {
      await sleep(2000);          // ShhDock not running yet
      continue;
    }
    if (!cmd) continue;           // 204 = idle timeout, poll again
    let res;
    try {
      res = await run(cmd);
    } catch (e) {
      res = { ok: false, out: String((e && e.message) || e) };
    }
    res.id = cmd.id;
    try {
      await fetch(BASE + "/report", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(res),
      });
    } catch (e) { /* ShhDock went away; command is dropped */ }
  }
}

// ---- helpers ---------------------------------------------------------------

async function activeTab() {
  const tabs = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tabs || !tabs[0]) throw new Error("no active tab");
  return tabs[0];
}

async function inject(tabId, fn, args) {
  const results = await chrome.scripting.executeScript({
    target: { tabId: tabId },
    func: fn,
    args: args || [],
  });
  if (!results || results[0] === undefined || results[0] === null)
    throw new Error("cannot run in that tab (chrome:// and Web Store pages are blocked)");
  return results[0].result;
}

// Self-contained: its source is serialized into the page.
function pageText() {
  const body = document.body ? document.body.innerText : "";
  return {
    title: document.title,
    url: location.href,
    text: body.replace(/\n{3,}/g, "\n\n").slice(0, 6000),
  };
}

function fmtPage(p) {
  if (!p) return "";
  return "PAGE: " + p.title + "\nURL: " + p.url + "\n----------\n" + p.text;
}

async function withPage(tabId, result, delayMs) {
  await sleep(delayMs === undefined ? 1200 : delayMs);
  try {
    const pg = await inject(tabId, pageText);
    result.out = (result.out + "\n\n" + fmtPage(pg)).trim();
  } catch (e) { /* page unreadable after the action; keep the action text */ }
  return result;
}

function clickByText(t) {
  const needle = t.toLowerCase();
  const cands = Array.from(
    document.querySelectorAll(
      'a,button,[role="button"],input[type=submit],input[type=button],summary,[onclick]'
    )
  );
  for (const el of cands) {
    const s = (el.innerText || el.value || el.getAttribute("aria-label") || "")
      .trim()
      .toLowerCase();
    if (s && s.includes(needle)) {
      el.scrollIntoView({ block: "center" });
      el.click();
      return "clicked: " + s.slice(0, 80);
    }
  }
  return null;
}

function clickSelector(sel) {
  const el = document.querySelector(sel);
  if (!el) return null;
  el.scrollIntoView({ block: "center" });
  el.click();
  return "clicked selector " + sel;
}

function typeInto(sel, text) {
  let el = null;
  if (sel) {
    el = document.querySelector(sel);
  } else if (
    document.activeElement &&
    /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName)
  ) {
    el = document.activeElement;
  } else {
    el =
      document.querySelector(
        'input:not([type=hidden]):not([type=submit]),textarea,[contenteditable="true"]'
      ) || document.activeElement;
  }
  if (!el) return null;
  el.focus();
  if (el.isContentEditable) {
    el.textContent = text;
  } else {
    const proto =
      el.tagName === "TEXTAREA"
        ? window.HTMLTextAreaElement
        : window.HTMLInputElement;
    const setter = Object.getOwnPropertyDescriptor(proto.prototype, "value");
    if (setter && setter.set) setter.set.call(el, text);
    else el.value = text;
  }
  el.dispatchEvent(new Event("input", { bubbles: true }));
  el.dispatchEvent(new Event("change", { bubbles: true }));
  return "typed into " + (el.name || el.id || el.placeholder || el.tagName);
}

function pressKey(key) {
  const el = document.activeElement || document.body;
  const opts = { key: key, code: key, bubbles: true, cancelable: true };
  el.dispatchEvent(new KeyboardEvent("keydown", opts));
  el.dispatchEvent(new KeyboardEvent("keypress", opts));
  el.dispatchEvent(new KeyboardEvent("keyup", opts));
  if ((key === "Enter" || key === "NumpadEnter") && el.form) {
    try {
      el.form.requestSubmit(el);
      return "pressed Enter - submitted the form";
    } catch (e) { /* fall through */ }
  }
  return "pressed " + key + " on " + el.tagName;
}

function scrollBy(dy) {
  window.scrollBy(0, dy);
  return "scrolled " + dy + " px (now at y=" + Math.round(window.scrollY) + ")";
}

function googleUrl(q) {
  return "https://www.google.com/search?q=" + encodeURIComponent(q);
}

// ---- command dispatch ------------------------------------------------------

async function run(cmd) {
  const op = String(cmd.op || "");

  if (op === "extract") {
    const tab = await activeTab();
    const pg = await inject(tab.id, pageText);
    return { ok: true, out: fmtPage(pg) };
  }

  if (op === "open") {
    if (!cmd.url) return { ok: false, out: "no url given" };
    const tab = await chrome.tabs.create({ url: cmd.url });
    await sleep(1800);
    try {
      const pg = await inject(tab.id, pageText);
      return { ok: true, out: "opened " + cmd.url + "\n\n" + fmtPage(pg) };
    } catch (e) {
      return { ok: true, out: "opened " + cmd.url };
    }
  }

  if (op === "search") {
    if (!cmd.query) return { ok: false, out: "no query given" };
    const tab = await chrome.tabs.create({ url: googleUrl(cmd.query) });
    await sleep(2500);
    try {
      const pg = await inject(tab.id, pageText);
      return {
        ok: true,
        out: 'google search for "' + cmd.query + '"\n\n' + fmtPage(pg),
      };
    } catch (e) {
      return { ok: true, out: 'searching google for "' + cmd.query + '"' };
    }
  }

  if (op === "click") {
    const tab = await activeTab();
    let hit = null;
    if (cmd.selector) hit = await inject(tab.id, clickSelector, [cmd.selector]);
    else if (cmd.text) hit = await inject(tab.id, clickByText, [cmd.text]);
    else return { ok: false, out: "need text or selector" };
    if (!hit) return { ok: false, out: "nothing to click (" + (cmd.text || cmd.selector) + ")" };
    return withPage(tab.id, { ok: true, out: hit }, 1800);
  }

  if (op === "type") {
    const tab = await activeTab();
    const hit = await inject(tab.id, typeInto, [cmd.selector || null, cmd.text || ""]);
    if (!hit) return { ok: false, out: "no text box found on this page" };
    return { ok: true, out: hit + " -> " + String(cmd.text).slice(0, 120) };
  }

  if (op === "press") {
    const tab = await activeTab();
    const key = cmd.key || "Enter";
    const out = await inject(tab.id, pressKey, [key]);
    if (key === "Enter" || key === "Escape") return withPage(tab.id, { ok: true, out: out }, 2200);
    return { ok: true, out: out };
  }

  if (op === "scroll") {
    const tab = await activeTab();
    const out = await inject(tab.id, scrollBy, [Number(cmd.dy) || -600]);
    return { ok: true, out: out };
  }

  return { ok: false, out: "unknown op: " + op };
}

// ---- lifecycle -------------------------------------------------------------

chrome.alarms.onAlarm.addListener(() => {
  loop();                                   // revive if Chrome idle-killed us
});
chrome.runtime.onInstalled.addListener(() => {
  chrome.alarms.create("shhdock-keepalive", { periodInMinutes: 0.5 });
  loop();
});
chrome.runtime.onStartup.addListener(() => loop());
chrome.alarms.create("shhdock-keepalive", { periodInMinutes: 0.5 });
loop();
