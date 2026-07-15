(() => {
  "use strict";

  const TAG = "[Aviator Audit Relay]";
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
      return `pb-${globalThis.crypto.randomUUID()}`;
    }
    return `pb-${Date.now()}-${Math.random().toString(36).slice(2, 14)}`;
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

    chrome.runtime.sendMessage({
      type: "INGEST_ROUND",
      round: {
        round_id: makeRoundId(),
        multiplier: current.values[0],
        observed_at_utc: new Date().toISOString(),
        frame_host: location.hostname,
        history_size: current.values.length
      }
    });
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
})();
