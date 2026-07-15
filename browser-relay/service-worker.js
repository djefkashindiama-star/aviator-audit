"use strict";

importScripts("config.js");

const QUEUE_KEY = "aviatorRelayQueue";
const STATE_KEY = "aviatorRelayState";
let flushing = false;

function setBadge(text, color, title) {
  chrome.action.setBadgeText({ text });
  chrome.action.setBadgeBackgroundColor({ color });
  chrome.action.setTitle({ title });
}

async function storageGet(keys) {
  return await chrome.storage.local.get(keys);
}

async function ensureCollectorId() {
  const stored = await storageGet("aviatorCollectorId");
  if (stored.aviatorCollectorId) return stored.aviatorCollectorId;
  const id = globalThis.crypto.randomUUID();
  await chrome.storage.local.set({ aviatorCollectorId: id });
  return id;
}

async function updateState(patch) {
  const stored = await storageGet(STATE_KEY);
  const state = { ...(stored[STATE_KEY] || {}), ...patch };
  await chrome.storage.local.set({ [STATE_KEY]: state });
}

async function sendHeartbeat(stage, frameHost = "") {
  const config = globalThis.AVIATOR_RELAY_CONFIG;
  if (!config?.endpoint || !config?.token) return;
  const endpoint = config.endpoint.replace(/\/api\/ingest$/, "/api/relay-heartbeat");
  try {
    await fetch(endpoint, {
      method: "POST",
      headers: {
        "Authorization": `Bearer ${config.token}`,
        "Content-Type": "application/json"
      },
      body: JSON.stringify({ stage, frame_host: frameHost })
    });
  } catch (_) {
    // La file des manches reste le mécanisme de reprise principal.
  }
}

async function enqueue(round) {
  const stored = await storageGet(QUEUE_KEY);
  const queue = Array.isArray(stored[QUEUE_KEY]) ? stored[QUEUE_KEY] : [];
  if (!queue.some((item) => item.round_id === round.round_id)) queue.push(round);
  await chrome.storage.local.set({ [QUEUE_KEY]: queue.slice(-500) });
  await updateState({ queued: queue.length, lastDetectedAt: new Date().toISOString() });
  await flushQueue();
}

async function flushQueue() {
  if (flushing) return;
  flushing = true;
  try {
    const config = globalThis.AVIATOR_RELAY_CONFIG;
    if (!config || !config.endpoint || !config.token || config.token.includes("REMPLACER")) {
      setBadge("CFG", "#ffb547", "Configuration du relais manquante");
      await updateState({ status: "configuration-missing" });
      return;
    }
    const collectorId = await ensureCollectorId();
    const stored = await storageGet(QUEUE_KEY);
    const queue = Array.isArray(stored[QUEUE_KEY]) ? stored[QUEUE_KEY] : [];
    while (queue.length) {
      const round = { ...queue[0], collector_id: collectorId };
      let response;
      try {
        response = await fetch(config.endpoint, {
          method: "POST",
          headers: {
            "Authorization": `Bearer ${config.token}`,
            "Content-Type": "application/json"
          },
          body: JSON.stringify(round)
        });
      } catch (error) {
        setBadge("OFF", "#ff4a3d", "Render est momentanément injoignable");
        await updateState({ status: "offline", lastError: String(error), queued: queue.length });
        return;
      }

      if (response.status === 401) {
        setBadge("AUTH", "#ff4a3d", "Secret du relais refusé");
        await updateState({ status: "unauthorized", queued: queue.length });
        return;
      }
      if (response.status === 410) {
        queue.shift();
        await chrome.storage.local.set({ [QUEUE_KEY]: queue });
        setBadge("DONE", "#57d69b", "Campagne de 20 jours terminée");
        await updateState({ status: "completed", queued: queue.length });
        return;
      }
      if (!response.ok) {
        setBadge("ERR", "#ff4a3d", `Erreur Render HTTP ${response.status}`);
        await updateState({ status: "error", lastError: `HTTP ${response.status}`, queued: queue.length });
        return;
      }

      const result = await response.json();
      queue.shift();
      await chrome.storage.local.set({ [QUEUE_KEY]: queue });
      setBadge("ON", "#57d69b", "Relais Aviator actif");
      await updateState({
        status: "active",
        queued: queue.length,
        rounds: result.rounds,
        campaignEndsAt: result.ends_at_utc,
        lastSuccessAt: new Date().toISOString(),
        lastMultiplier: round.multiplier,
        lastError: null
      });
    }
  } finally {
    flushing = false;
  }
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  (async () => {
    if (message?.type === "INGEST_ROUND" && message.round) {
      await enqueue(message.round);
    } else if (message?.type === "RELAY_PAGE_READY") {
      await sendHeartbeat(message.stage, message.frameHost);
    } else if (message?.type === "RELAY_FRAME_READY") {
      setBadge("ON", "#57d69b", "Aviator détecté, attente de la prochaine manche");
      await updateState({ status: "watching", frameHost: message.frameHost });
      await sendHeartbeat("history-detected", message.frameHost);
    }
    sendResponse({ ok: true });
  })().catch(async (error) => {
    await updateState({ status: "error", lastError: String(error) });
    sendResponse({ ok: false });
  });
  // Garde le service worker actif jusqu'à la fin de l'envoi vers Render.
  return true;
});

chrome.runtime.onInstalled.addListener(() => {
  chrome.alarms.create("flushAviatorQueue", { periodInMinutes: 1 });
  setBadge("...", "#ffb547", "Relais installé");
});
chrome.runtime.onStartup.addListener(() => {
  chrome.alarms.create("flushAviatorQueue", { periodInMinutes: 1 });
  flushQueue();
});
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === "flushAviatorQueue") flushQueue();
});
flushQueue();
sendHeartbeat("extension-started");
