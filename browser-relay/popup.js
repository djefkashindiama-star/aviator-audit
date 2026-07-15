"use strict";

const labels = {
  active: "Relais actif",
  watching: "Aviator détecté — attente d'une manche",
  offline: "Render momentanément injoignable",
  unauthorized: "Secret du relais refusé",
  error: "Erreur de transmission",
  completed: "Campagne terminée",
  "configuration-missing": "Configuration manquante"
};

chrome.storage.local.get("aviatorRelayState").then(({ aviatorRelayState = {} }) => {
  const status = document.querySelector("#status");
  status.textContent = labels[aviatorRelayState.status] || "Extension prête";
  status.className = `status ${aviatorRelayState.status || ""}`;
  document.querySelector("#rounds").textContent = aviatorRelayState.rounds ?? "—";
  document.querySelector("#queued").textContent = aviatorRelayState.queued ?? 0;
  document.querySelector("#last").textContent = aviatorRelayState.lastMultiplier
    ? `${aviatorRelayState.lastMultiplier}×`
    : "—";
  document.querySelector("#ends").textContent = aviatorRelayState.campaignEndsAt
    ? new Date(aviatorRelayState.campaignEndsAt).toLocaleDateString("fr-FR")
    : "—";
});
