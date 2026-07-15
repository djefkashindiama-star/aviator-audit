"use client";

import { useCallback, useEffect, useMemo, useState } from "react";

type DashboardData = {
  generated_at: string;
  uptime_seconds: number;
  database: string;
  deployment_mode: string;
  rounds: number;
  first_round_at: string | null;
  last_round_at: string | null;
  summary: { median: number | null; mean: number | null; maximum: number | null; above_2x: number };
  survival: { threshold: number; rate: number; reference: number }[];
  histogram: { label: string; count: number }[];
  autocorrelation: { lag: number; value: number }[];
  evaluations: { threshold: number; test_rounds: number; baseline_accuracy: number; lag_state_accuracy: number; improvement: number }[];
  hourly: { hour: string; count: number }[];
  latest: { round_id: string; timestamp: string; multiplier: number; source: string }[];
  source: {
    operator: string; game_id?: string; status: string; collection_ready: boolean;
    catalog_available?: boolean; game_available?: boolean; fun_mode_available?: boolean | null;
    launch_requires_authentication?: boolean | null; display_name?: string; provider?: string;
    message: string; checked_at: string | null;
  };
  campaign: null | {
    id: number; source: string; status: "running" | "completed" | "stopped";
    duration_days: number; started_at: string; ends_at: string; completed_at: string | null;
    polls: number; successful_polls: number; failure_count: number;
    last_poll_at: string | null; last_success_at: string | null; last_error: string | null;
    progress: number; remaining_seconds: number; raw_snapshots: number;
    raw_bytes: number; success_rate: number; max_gap_seconds: number;
  };
};

// Même origine en production. Le serveur Vite relaie /api vers l'API Python en local.
const API = "/api/dashboard";

const formatNumber = (value: number | null, digits = 2) =>
  value == null || Number.isNaN(value) ? "—" : new Intl.NumberFormat("fr-FR", { maximumFractionDigits: digits }).format(value);

const formatDate = (value: string | null) => {
  if (!value) return "En attente";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString("fr-FR", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit", second: "2-digit" });
};

const formatDuration = (seconds: number) => {
  if (seconds < 3600) return `${Math.floor(seconds / 60)} min ${Math.floor(seconds % 60)} s`;
  const days = Math.floor(seconds / 86400);
  const hours = Math.floor((seconds % 86400) / 3600);
  return `${days} j ${hours} h`;
};

const formatBytes = (value: number) => {
  if (value < 1024) return `${value} o`;
  if (value < 1024 ** 2) return `${formatNumber(value / 1024, 1)} Ko`;
  if (value < 1024 ** 3) return `${formatNumber(value / 1024 ** 2, 1)} Mo`;
  return `${formatNumber(value / 1024 ** 3, 2)} Go`;
};

function multiplierClass(value: number) {
  if (value >= 10) return "multiplier multiplier--hot";
  if (value >= 2) return "multiplier multiplier--mid";
  return "multiplier";
}

export default function Dashboard() {
  const [data, setData] = useState<DashboardData | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [paused, setPaused] = useState(false);
  const [lastRefresh, setLastRefresh] = useState<Date | null>(null);

  const refresh = useCallback(async () => {
    try {
      const response = await fetch(API, { cache: "no-store" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      setData(await response.json());
      setError(null);
      setLastRefresh(new Date());
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Connexion impossible");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh();
    if (paused) return;
    const timer = window.setInterval(refresh, 5000);
    return () => window.clearInterval(timer);
  }, [paused, refresh]);

  const maxHistogram = useMemo(() => Math.max(1, ...(data?.histogram.map((item) => item.count) ?? [1])), [data]);
  const maxHourly = useMemo(() => Math.max(1, ...(data?.hourly.map((item) => item.count) ?? [1])), [data]);
  const signal = data?.evaluations.length
    ? Math.max(...data.evaluations.map((item) => item.improvement))
    : null;
  const sourceBlocked = Boolean(
    data?.source
    && !data.source.collection_ready
    && data.source.status !== "checking"
    && !data.source.status.startsWith("relay-")
  );
  const authenticationRequired = data?.source?.status === "authentication-required";
  const relayReady = data?.source?.status === "relay-ready";

  return (
    <main className="shell">
      <header className="topbar">
        <div className="brand">
          <div className="brand-mark" aria-hidden="true"><span>AV</span></div>
          <div><p className="eyebrow">Observatoire continu</p><h1>Aviator Audit</h1></div>
        </div>
        <div className="top-actions">
          <div className={`connection ${error || sourceBlocked ? "connection--off" : ""}`}>
            <span className="pulse" />{error ? "API hors ligne" : sourceBlocked ? "Source bloquée" : "API connectée"}
          </div>
          <button className="quiet-button" onClick={() => setPaused((value) => !value)}>{paused ? "Reprendre" : "Pause auto"}</button>
          <button className="primary-button" onClick={refresh}>Actualiser</button>
        </div>
      </header>

      <section className="hero">
        <div>
          <p className="eyebrow accent">Analyse en continu · lecture seule</p>
          <h2>Voir les données. Tester les motifs.<br /><span>Refuser les illusions.</span></h2>
          <p className="hero-copy">Les indicateurs comparent les résultats observés à une référence théorique et testent tout signal sur les manches suivantes — jamais sur celles utilisées pour l’inventer.</p>
        </div>
        <div className="hero-status">
          <div className="orb"><span>{data?.rounds ? "LIVE" : sourceBlocked ? "AUTH" : "WAIT"}</span></div>
          <div><small>Dernière observation</small><strong>{formatDate(data?.last_round_at ?? null)}</strong><span>Rafraîchi {lastRefresh ? lastRefresh.toLocaleTimeString("fr-FR") : "—"}</span></div>
        </div>
      </section>

      {error && (
        <section className="notice">
          <strong>Le serveur de données n’est pas joignable.</strong>
          <span>L’interface fonctionne, mais attend la réponse de l’API de collecte.</span>
        </section>
      )}

      {data?.deployment_mode === "render-free" && (
        <section className="notice">
          <strong>Mode Render gratuit.</strong>
          <span>La collecte s’arrête pendant la mise en veille et la base locale peut être effacée lors d’un redémarrage ou d’un redéploiement.</span>
        </section>
      )}

      {sourceBlocked && (
        <section className="notice notice--blocked">
          <strong>{authenticationRequired ? "Collecte bloquée par l’authentification PremierBet." : "Source inaccessible depuis Render."}</strong>
          <span>{data.source.message} Aucune manche fictive ni donnée d’un autre opérateur n’est injectée.</span>
        </section>
      )}

      {relayReady && (
        <section className="notice notice--ready">
          <strong>Relais local prêt.</strong>
          <span>{data.source.message}</span>
        </section>
      )}

      <section className="campaign-strip" aria-label="Campagne de collecte sur 20 jours">
        <div className="campaign-title">
          <p className="eyebrow">Campagne exhaustive</p>
          <h3>{data?.campaign ? `Collecte #${data.campaign.id} · ${formatNumber(data.campaign.duration_days, 1)} jours` : authenticationRequired ? "Collecte non démarrée — authentification requise" : sourceBlocked ? "Collecte non démarrée — source refusée" : "Collecte de 20 jours en attente"}</h3>
          <span>{data?.campaign ? `${formatDate(data.campaign.started_at)} → ${formatDate(data.campaign.ends_at)}` : sourceBlocked ? `Sonde: ${data.source.display_name ?? "Aviator"} · ${data.source.provider ?? "provider 36"} · ${formatDate(data.source.checked_at)}` : "Vérification de la source réelle en cours."}</span>
        </div>
        <div className="campaign-progress">
          <div className="progress-label"><span>Progression temporelle</span><strong>{formatNumber((data?.campaign?.progress ?? 0) * 100, 2)}%</strong></div>
          <div className="progress-track"><i style={{ width: `${(data?.campaign?.progress ?? 0) * 100}%` }} /></div>
          <small>{data?.campaign ? `${formatDuration(data.campaign.remaining_seconds)} restantes` : "20 j 0 h restantes"}</small>
        </div>
        <div className="campaign-facts">
          <div><small>Réponses brutes</small><strong>{formatNumber(data?.campaign?.raw_snapshots ?? 0, 0)}</strong></div>
          <div><small>Archive originale</small><strong>{formatBytes(data?.campaign?.raw_bytes ?? 0)}</strong></div>
          <div><small>Succès des lectures</small><strong>{formatNumber((data?.campaign?.success_rate ?? 0) * 100, 1)}%</strong></div>
          <div><small>Erreurs journalisées</small><strong className={data?.campaign?.failure_count ? "fact-alert" : ""}>{formatNumber(data?.campaign?.failure_count ?? 0, 0)}</strong></div>
          <div><small>Plus longue coupure</small><strong className={(data?.campaign?.max_gap_seconds ?? 0) > 30 ? "fact-alert" : ""}>{formatDuration(data?.campaign?.max_gap_seconds ?? 0)}</strong></div>
        </div>
      </section>

      <section className="metric-grid" aria-label="Indicateurs principaux">
        <article className="metric-card metric-card--lead">
          <div className="metric-label"><span>Manches collectées</span><i>01</i></div>
          <strong>{loading ? "…" : formatNumber(data?.rounds ?? 0, 0)}</strong>
          <p>{data?.first_round_at ? `Depuis ${formatDate(data.first_round_at)}` : "La collecte réelle n’a pas encore commencé"}</p>
        </article>
        <article className="metric-card"><div className="metric-label"><span>Médiane</span><i>02</i></div><strong>{formatNumber(data?.summary.median ?? null)}<em>×</em></strong><p>Point central des multiplicateurs</p></article>
        <article className="metric-card"><div className="metric-label"><span>Au-dessus de 2×</span><i>03</i></div><strong>{formatNumber((data?.summary.above_2x ?? 0) * 100, 1)}<em>%</em></strong><p>Référence indicative : 48,5 %</p></article>
        <article className="metric-card"><div className="metric-label"><span>Maximum observé</span><i>04</i></div><strong>{formatNumber(data?.summary.maximum ?? null)}<em>×</em></strong><p>Valeur extrême, non prédictive</p></article>
      </section>

      <section className="dashboard-grid">
        <article className="panel panel--wide">
          <div className="panel-head"><div><p className="eyebrow">Distribution</p><h3>Densité des multiplicateurs</h3></div><span>{data?.rounds ?? 0} observations</span></div>
          <div className="histogram">
            {(data?.histogram ?? []).map((item) => (
              <div className="histogram-column" key={item.label}>
                <span>{item.count}</span><div className="histogram-track"><i style={{ height: `${Math.max(3, item.count / maxHistogram * 100)}%` }} /></div><small>{item.label}</small>
              </div>
            ))}
            {!data?.histogram.length && <EmptyChart label="La distribution apparaîtra dès les premières manches." />}
          </div>
        </article>

        <article className="panel survival-panel">
          <div className="panel-head"><div><p className="eyebrow">Calibration</p><h3>Taux de dépassement</h3></div></div>
          <div className="survival-list">
            {(data?.survival ?? []).map((item) => (
              <div className="survival-row" key={item.threshold}>
                <b>≥ {item.threshold}×</b>
                <div className="double-track"><i style={{ width: `${item.reference * 100}%` }} /><span style={{ width: `${item.rate * 100}%` }} /></div>
                <strong>{formatNumber(item.rate * 100, 1)}%</strong>
              </div>
            ))}
          </div>
          <div className="legend"><span><i className="legend-observed" />Observé</span><span><i />Référence RTP/x</span></div>
        </article>

        <article className="panel volume-panel">
          <div className="panel-head"><div><p className="eyebrow">Cadence</p><h3>Volume horaire</h3></div><span>24 dernières heures actives</span></div>
          <div className="volume-chart">
            {(data?.hourly ?? []).map((item) => <div key={item.hour} title={`${item.hour} · ${item.count} manches`}><i style={{ height: `${Math.max(4, item.count / maxHourly * 100)}%` }} /></div>)}
            {!data?.hourly.length && <EmptyChart label="En attente du flux de collecte." />}
          </div>
        </article>

        <article className="panel signal-panel">
          <div className="panel-head"><div><p className="eyebrow">Validation</p><h3>Signal hors-échantillon</h3></div></div>
          <div className={`signal-dial ${signal != null && signal > 0.02 ? "signal-dial--warn" : ""}`}>
            <strong>{signal == null ? "—" : `${signal >= 0 ? "+" : ""}${formatNumber(signal * 100, 2)}`}</strong><span>{signal == null ? "100 manches minimum" : "points vs baseline"}</span>
          </div>
          <p className="panel-copy">Un motif n’est retenu que s’il améliore durablement les résultats sur la portion future des données.</p>
        </article>

        <article className="panel panel--wide autocorrelation-panel">
          <div className="panel-head"><div><p className="eyebrow">Indépendance</p><h3>Autocorrélation par retard</h3></div><span>Log-multiplicateurs</span></div>
          <div className="correlation-chart">
            {(data?.autocorrelation ?? []).map((item) => (
              <div className="correlation-item" key={item.lag}><span>{formatNumber(item.value, 3)}</span><div><i style={{ width: `${Math.abs(item.value || 0) * 100}%`, marginLeft: item.value < 0 ? `${50 - Math.abs(item.value) * 50}%` : "50%" }} /></div><small>t−{item.lag}</small></div>
            ))}
            {!data?.autocorrelation.length && <EmptyChart label="Davantage de manches sont nécessaires." />}
          </div>
        </article>

        <article className="panel table-panel">
          <div className="panel-head"><div><p className="eyebrow">Flux brut</p><h3>Dernières manches</h3></div><span>Actualisation 5 s</span></div>
          <div className="round-table" role="table">
            <div className="table-row table-header" role="row"><span>Heure</span><span>Manche</span><span>Résultat</span></div>
            {(data?.latest ?? []).slice(0, 12).map((round) => (
              <div className="table-row" role="row" key={`${round.source}-${round.round_id}`}><span>{formatDate(round.timestamp)}</span><code>{round.round_id.slice(0, 10)}</code><strong className={multiplierClass(round.multiplier)}>{formatNumber(round.multiplier)}×</strong></div>
            ))}
            {!data?.latest.length && <div className="table-empty">Aucune manche enregistrée pour le moment.</div>}
          </div>
        </article>
      </section>

      <footer><span>AVIATOR AUDIT / LOCAL</span><p>Outil d’analyse statistique — aucune promesse de gain ni placement de pari.</p><span>{data?.database?.split("/").pop() ?? "aviator.sqlite3"}</span></footer>
    </main>
  );
}

function EmptyChart({ label }: { label: string }) {
  return <div className="empty-chart"><span /><p>{label}</p></div>;
}
