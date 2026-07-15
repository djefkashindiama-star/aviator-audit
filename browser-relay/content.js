(() => {
  "use strict";

  const TAG = "[Aviator Audit Relay]";
  const QUEUE_KEY = "aviatorRelayQueue";
  const STATE_KEY = "aviatorRelayState";
  const MULTIPLIER = /^(\d{1,6}(?:[.,]\d{1,2})?)\s*[x×]$/i;
  const BLOCK_SELECTORS = [
    ".payouts-block",
    "app-stats-dropdown .payouts-block",
    ".result-history .payouts-block"
  ];
  const ITEM_SELECTORS = [
    ".bubble-multiplier",
    "app-bubble-multiplier",
    ".payout"
  ];

  let monitoredBlock = null;
  let observer = null;
  let lastSignature = null;
  let lastFirstNode = null;
  let processingTimer = null;
  let providerHistoryIds = null;
  let directFlushing = false;

  async function storageGet(keys) {
    return await chrome.storage.local.get(keys);
  }

  async function updateState(patch) {
    const stored = await storageGet(STATE_KEY);
    await chrome.storage.local.set({
      [STATE_KEY]: { ...(stored[STATE_KEY] || {}), ...patch }
    });
  }

  async function ensureCollectorId() {
    const stored = await storageGet("aviatorCollectorId");
    if (stored.aviatorCollectorId) return stored.aviatorCollectorId;
    const id = globalThis.crypto.randomUUID();
    await chrome.storage.local.set({ aviatorCollectorId: id });
    return id;
  }

  function relayConfig() {
    const config = globalThis.AVIATOR_RELAY_CONFIG;
    return config?.endpoint && config?.token && !config.token.includes("REMPLACER") ? config : null;
  }

  async function sendHeartbeat(stage) {
    const config = relayConfig();
    if (!config) return;
    const endpoint = config.endpoint.replace(/\/api\/ingest$/, "/api/relay-heartbeat");
    try {
      await fetch(endpoint, {
        method: "POST",
        headers: {
          "Authorization": `Bearer ${config.token}`,
          "Content-Type": "application/json"
        },
        body: JSON.stringify({ stage, frame_host: location.hostname })
      });
    } catch (_) {
      // La page reste active et réessaiera avec les prochains événements.
    }
  }

  async function flushDirectQueue() {
    if (directFlushing) return;
    const config = relayConfig();
    if (!config) {
      await updateState({ status: "configuration-missing" });
      return;
    }
    directFlushing = true;
    try {
      const collectorId = await ensureCollectorId();
      const stored = await storageGet(QUEUE_KEY);
      const queue = Array.isArray(stored[QUEUE_KEY]) ? stored[QUEUE_KEY] : [];
      while (queue.length) {
        let response;
        try {
          response = await fetch(config.endpoint, {
            method: "POST",
            headers: {
              "Authorization": `Bearer ${config.token}`,
              "Content-Type": "application/json"
            },
            body: JSON.stringify({ ...queue[0], collector_id: collectorId })
          });
        } catch (error) {
          await updateState({ status: "offline", lastError: String(error), queued: queue.length });
          return;
        }
        if (response.status === 401) {
          await updateState({ status: "unauthorized", queued: queue.length });
          return;
        }
        if (response.status === 410) {
          queue.shift();
          await chrome.storage.local.set({ [QUEUE_KEY]: queue });
          await updateState({ status: "completed", queued: queue.length });
          return;
        }
        if (!response.ok) {
          await updateState({ status: "error", lastError: `HTTP ${response.status}`, queued: queue.length });
          return;
        }
        const result = await response.json();
        const sent = queue.shift();
        await chrome.storage.local.set({ [QUEUE_KEY]: queue });
        await updateState({
          status: "active",
          queued: queue.length,
          rounds: result.rounds,
          campaignEndsAt: result.ends_at_utc,
          lastSuccessAt: new Date().toISOString(),
          lastMultiplier: sent.multiplier,
          lastError: null
        });
      }
    } finally {
      directFlushing = false;
    }
  }

  async function enqueueRound(round) {
    const stored = await storageGet(QUEUE_KEY);
    const queue = Array.isArray(stored[QUEUE_KEY]) ? stored[QUEUE_KEY] : [];
    if (!queue.some((item) => item.round_id === round.round_id)) queue.push(round);
    await chrome.storage.local.set({ [QUEUE_KEY]: queue.slice(-500) });
    await updateState({ queued: queue.length, lastDetectedAt: new Date().toISOString() });
    await flushDirectQueue();
  }

  sendHeartbeat(location.hostname.includes("premierbet.com") ? "premierbet-page" : "provider-frame");

  function sendRound(roundId, multiplier, historySize) {
    enqueueRound({
      round_id: `pb-${roundId}`,
      multiplier,
      observed_at_utc: new Date().toISOString(),
      frame_host: location.hostname,
      history_size: historySize
    });
  }

  function acceptProviderHistory(rawItems) {
    if (!location.hostname.endsWith(".aviator.studio")) return;
    const items = Array.isArray(rawItems)
      ? rawItems
          .map((item) => ({
            id: String(item?.id || ""),
            multiplier: Number(item?.multiplier)
          }))
          .filter(
            (item) =>
              /^[a-f0-9]{12,64}$/i.test(item.id) &&
              Number.isFinite(item.multiplier) &&
              item.multiplier >= 1 &&
              item.multiplier <= 1000000
          )
          .slice(0, 100)
      : [];
    if (items.length < 2) return;

    if (providerHistoryIds === null) {
      providerHistoryIds = new Set(items.map((item) => item.id));
      updateState({ status: "watching", frameHost: location.hostname });
      sendHeartbeat("history-detected");
      return;
    }

    const fresh = [];
    for (const item of items) {
      if (providerHistoryIds.has(item.id)) break;
      fresh.push(item);
    }
    providerHistoryIds = new Set(items.map((item) => item.id));
    fresh.reverse().forEach((item) => sendRound(item.id, item.multiplier, items.length));
  }

  window.addEventListener("message", (event) => {
    if (event.source !== window || event.data?.source !== "aviator-audit-probe") return;
    acceptProviderHistory(event.data.history);
  });

  function parseMultiplier(text) {
    const match = String(text || "").trim().match(MULTIPLIER);
    if (!match) return null;
    const value = Number(match[1].replace(",", "."));
    return Number.isFinite(value) && value >= 1 && value <= 1000000 ? value : null;
  }

  function historyItems(block) {
    for (const selector of ITEM_SELECTORS) {
      const items = Array.from(block.querySelectorAll(selector)).filter(
        (element) => parseMultiplier(element.textContent) !== null
      );
      if (items.length) return items;
    }
    return Array.from(block.children).filter(
      (element) => parseMultiplier(element.textContent) !== null
    );
  }

  function readHistory(block) {
    const items = historyItems(block);
    const values = items
      .map((element) => parseMultiplier(element.textContent))
      .filter((value) => value !== null);
    return {
      items,
      values,
      signature: values.slice(0, 12).join("|")
    };
  }

  function makeRoundId() {
    if (globalThis.crypto && typeof globalThis.crypto.randomUUID === "function") {
      return globalThis.crypto.randomUUID();
    }
    return `${Date.now()}-${Math.random().toString(36).slice(2, 14)}`;
  }

  function emitLatest() {
    processingTimer = null;
    if (!monitoredBlock || !monitoredBlock.isConnected) return;
    const current = readHistory(monitoredBlock);
    if (!current.values.length || !current.signature) return;

    const firstNodeChanged = current.items[0] !== lastFirstNode;
    const signatureChanged = current.signature !== lastSignature;
    if (!signatureChanged) {
      lastFirstNode = current.items[0];
      return;
    }

    // Le premier passage sert de ligne de base: aucun ancien résultat n'est rejoué.
    if (lastSignature === null) {
      lastSignature = current.signature;
      lastFirstNode = current.items[0];
      console.info(TAG, "historique détecté, attente de la prochaine manche");
      chrome.runtime.sendMessage({ type: "RELAY_FRAME_READY", frameHost: location.hostname });
      return;
    }

    lastSignature = current.signature;
    lastFirstNode = current.items[0];
    if (!firstNodeChanged && !signatureChanged) return;

    sendRound(makeRoundId(), current.values[0], current.values.length);
  }

  function scheduleRead() {
    if (processingTimer !== null) clearTimeout(processingTimer);
    processingTimer = setTimeout(emitLatest, 180);
  }

  function monitor(block) {
    if (monitoredBlock === block) return;
    if (observer) observer.disconnect();
    monitoredBlock = block;
    lastSignature = null;
    lastFirstNode = null;
    observer = new MutationObserver(scheduleRead);
    observer.observe(block, { childList: true, subtree: true, characterData: true });
    emitLatest();
  }

  function findHistory() {
    for (const selector of BLOCK_SELECTORS) {
      const candidates = Array.from(document.querySelectorAll(selector));
      const block = candidates.find((candidate) => readHistory(candidate).values.length >= 2);
      if (block) {
        monitor(block);
        return;
      }
    }
  }

  findHistory();
  const discovery = new MutationObserver(() => {
    if (!monitoredBlock || !monitoredBlock.isConnected) findHistory();
  });
  discovery.observe(document.documentElement, { childList: true, subtree: true });
  setInterval(findHistory, 5000);
  setInterval(flushDirectQueue, 15000);
})();
