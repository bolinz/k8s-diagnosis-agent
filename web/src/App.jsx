import { startTransition, useDeferredValue, useEffect, useMemo, useRef, useState } from "react";

const TIMEZONES = ["local", "UTC", "Asia/Shanghai"];
const EMPTY_FILTERS = { namespace: "", severity: "", symptom: "" };
const UI_PREFS_KEY = "k8s-diagnosis-ui-prefs-v1";
const DETAIL_VIEWS = ["all", "overview", "attribution", "timeline"];
const TIMELINE_WINDOWS = [
  { value: "all", label: "All" },
  { value: "5m", label: "5m" },
  { value: "15m", label: "15m" },
  { value: "1h", label: "1h" },
  { value: "6h", label: "6h" },
];
const ROLE_OPTIONS = [
  { value: "all", label: "All Roles" },
  { value: "primary", label: "Primary" },
  { value: "owner", label: "Owner" },
  { value: "affected", label: "Affected" },
  { value: "upstream-suspect", label: "Upstream" },
];
const TIMELINE_GROUP_PREVIEW_LIMIT = 6;

function readUiPrefs() {
  if (typeof window === "undefined") {
    return { timezone: "local", autoRefreshEnabled: true, autoRefreshSeconds: 15, opsExpanded: false, timelineGroupSort: "count" };
  }
  try {
    const raw = window.localStorage.getItem(UI_PREFS_KEY);
    if (!raw) return { timezone: "local", autoRefreshEnabled: true, autoRefreshSeconds: 15, opsExpanded: false, timelineGroupSort: "count" };
    const parsed = JSON.parse(raw);
    const seconds = Number(parsed?.autoRefreshSeconds);
    return {
      timezone: TIMEZONES.includes(parsed?.timezone) ? parsed.timezone : "local",
      autoRefreshEnabled: parsed?.autoRefreshEnabled !== false,
      autoRefreshSeconds: [15, 30, 60].includes(seconds) ? seconds : 15,
      opsExpanded: parsed?.opsExpanded === true,
      timelineGroupSort: parsed?.timelineGroupSort === "time" ? "time" : "count",
    };
  } catch {
    return { timezone: "local", autoRefreshEnabled: true, autoRefreshSeconds: 15, opsExpanded: false, timelineGroupSort: "count" };
  }
}

function writeUiPrefs({ timezone, autoRefreshEnabled, autoRefreshSeconds, opsExpanded, timelineGroupSort }) {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(
      UI_PREFS_KEY,
      JSON.stringify({
        timezone,
        autoRefreshEnabled,
        autoRefreshSeconds,
        opsExpanded,
        timelineGroupSort,
      }),
    );
  } catch {
    // ignore storage errors (private mode / quota)
  }
}

function readInitialUiState() {
  const prefs = readUiPrefs();
  if (typeof window === "undefined") {
    return {
      timezone: prefs.timezone,
      filters: EMPTY_FILTERS,
      selectedName: "",
      autoRefreshEnabled: prefs.autoRefreshEnabled,
      autoRefreshSeconds: prefs.autoRefreshSeconds,
      opsExpanded: prefs.opsExpanded,
      timelineGroupSort: prefs.timelineGroupSort,
    };
  }
  const params = new URLSearchParams(window.location.search);
  const timezone = params.get("tz");
  const filters = {
    namespace: params.get("ns") || "",
    severity: params.get("sev") || "",
    symptom: params.get("sym") || "",
  };
  return {
    timezone: timezone && TIMEZONES.includes(timezone) ? timezone : prefs.timezone,
    filters,
    selectedName: params.get("report") || "",
    autoRefreshEnabled: prefs.autoRefreshEnabled,
    autoRefreshSeconds: prefs.autoRefreshSeconds,
    opsExpanded: prefs.opsExpanded,
    timelineGroupSort: prefs.timelineGroupSort,
  };
}

function writeUiStateToUrl({ timezone, filters, selectedName }) {
  if (typeof window === "undefined") return;
  const params = new URLSearchParams();
  if (timezone && timezone !== "local") params.set("tz", timezone);
  if (filters.namespace) params.set("ns", filters.namespace);
  if (filters.severity) params.set("sev", filters.severity);
  if (filters.symptom) params.set("sym", filters.symptom);
  if (selectedName) params.set("report", selectedName);
  const next = params.toString();
  const url = next ? `${window.location.pathname}?${next}` : window.location.pathname;
  window.history.replaceState({}, "", url);
}

function formatAt(ts, timezone) {
  if (!ts) return "";
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return String(ts);
  if (timezone === "local") return d.toLocaleString();
  return d.toLocaleString(undefined, { timeZone: timezone, hour12: false });
}

function workloadLabel(item) {
  const kind = item?.workload?.kind;
  const name = item?.workload?.name;
  if (kind && name) return `${kind}/${name}`;
  return item?.name || item?.symptom || "DiagnosisReport";
}

function asList(values) {
  if (!Array.isArray(values) || values.length === 0) return [];
  return values.filter(Boolean).map((x) => String(x));
}

function objectRefLabel(obj) {
  if (!obj) return "";
  const kind = obj.kind || "";
  const ns = obj.namespace ? `${obj.namespace}/` : "";
  const name = obj.name || "";
  if (!kind && !name) return "";
  return `${kind} ${ns}${name}`.trim();
}

function resourceKindToken(kind) {
  const text = String(kind || "").toLowerCase();
  if (text === "pod") return "Po";
  if (text === "deployment") return "Dp";
  if (text === "replicaset") return "RS";
  if (text === "statefulset") return "SS";
  if (text === "daemonset") return "DS";
  if (text === "persistentvolumeclaim") return "PVC";
  if (text === "node") return "Nd";
  if (text === "service") return "Svc";
  if (text === "configmap") return "CM";
  if (text === "secret") return "Sec";
  return String(kind || "Obj").slice(0, 3);
}

function resourceKindClass(kind) {
  const text = String(kind || "").toLowerCase();
  if (text === "pod") return "pod";
  if (text === "deployment") return "deployment";
  if (text === "replicaset") return "replicaset";
  if (text === "statefulset") return "statefulset";
  if (text === "daemonset") return "daemonset";
  if (text === "persistentvolumeclaim") return "pvc";
  if (text === "node") return "node";
  if (text === "service") return "service";
  if (text === "configmap") return "configmap";
  if (text === "secret") return "secret";
  return "other";
}

function compactObjectName(name, limit = 14) {
  const text = String(name || "").trim();
  if (!text) return "";
  if (text.length <= limit) return text;
  const head = text.slice(0, Math.max(6, Math.floor(limit / 2) - 1));
  const tail = text.slice(-Math.max(4, Math.floor(limit / 2) - 2));
  return `${head}…${tail}`;
}

function eventSignature(ev) {
  return `${ev?.time || ""}|${String(ev?.signal || "")}|${objectRefLabel(ev?.objectRef)}`;
}

function compactSignalLabel(value) {
  const text = String(value || "signal").trim();
  if (text.length <= 24) return text;
  return `${text.slice(0, 23)}…`;
}

function HorizontalTimeline({
  events,
  timezone,
  activeObjectLabel,
  onSelectObjectLabel,
  activeEventKey,
  activeEventSignature,
  onSelectEventKey,
  onSelectEvent,
}) {
  const scrollRef = useRef(null);
  if (!Array.isArray(events) || events.length === 0) return <div className="empty">No timeline signals.</div>;
  const parsed = events
    .map((x) => ({ ...x, ms: Date.parse(x.time || ""), signature: eventSignature(x) }))
    .filter((x) => Number.isFinite(x.ms))
    .sort((a, b) => a.ms - b.ms);
  if (parsed.length === 0) return <div className="empty">No timeline signals.</div>;
  const min = parsed[0].ms;
  const max = parsed[parsed.length - 1].ms;
  const range = Math.max(max - min, 1);
  const width = 960;
  const right = 920;
  const railWidth = 880;

  useEffect(() => {
    if (!activeEventSignature) return;
    const root = scrollRef.current;
    if (!root) return;
    const target = root.querySelector(`[data-event-signature="${activeEventSignature.replaceAll("\"", "\\\"")}"]`);
    if (!target) return;
    if (typeof target.scrollIntoView === "function") {
      target.scrollIntoView({ block: "nearest", inline: "center", behavior: "smooth" });
    }
  }, [activeEventSignature, events]);

  return (
    <div className="timeline-scroll" ref={scrollRef}>
      <svg viewBox={`0 0 ${width} 130`} className="timeline-svg" role="img">
      <line x1="40" y1="64" x2={right} y2="64" className="timeline-rail" />
      {parsed.map((ev, idx) => {
        const x = 40 + ((ev.ms - min) / range) * railWidth;
        const label = String(ev.signal || "signal");
        const up = idx % 2 === 0;
        const eventObject = objectRefLabel(ev.objectRef);
        const eventKey = `${ev.signature}-${idx}`;
        const isFirst = idx === 0;
        const isObjectActive = activeObjectLabel && eventObject && activeObjectLabel === eventObject;
        const isActive = isObjectActive || activeEventKey === eventKey || activeEventSignature === ev.signature;
        const shouldLabel = isActive || isFirst || idx === parsed.length - 1 || idx % 2 === 0;
        const labelAnchor = x < 90 ? "start" : x > right - 90 ? "end" : "middle";
        return (
          <g
            key={eventKey}
            data-event-signature={ev.signature}
            className={`timeline-point ${isActive ? "timeline-point-active" : ""} ${isFirst ? "timeline-point-first" : ""}`}
            onClick={() => {
              onSelectEventKey(eventKey);
              onSelectObjectLabel(eventObject || "");
              onSelectEvent({
                key: eventKey,
                time: ev.time || "",
                signal: label,
                objectLabel: eventObject,
                objectRef: ev.objectRef || null,
                signature: ev.signature,
              });
            }}
          >
            <line x1={x} y1={up ? 34 : 64} x2={x} y2={up ? 64 : 94} className="timeline-stem" />
            <circle cx={x} cy="64" r="6" className="timeline-dot" />
            {shouldLabel ? (
              <text x={x} y={up ? 24 : 108} textAnchor={labelAnchor} className="timeline-label">
                {compactSignalLabel(label)}
              </text>
            ) : null}
            <title>{`${isFirst ? "[first abnormal] " : ""}${label} @ ${formatAt(ev.time, timezone)}${eventObject ? ` (${eventObject})` : ""}`}</title>
          </g>
        );
      })}
      <text x="40" y="122" className="timeline-time">{formatAt(parsed[0].time, timezone)}</text>
      <text x={right} y="122" textAnchor="end" className="timeline-time">{formatAt(parsed[parsed.length - 1].time, timezone)}</text>
    </svg>
    </div>
  );
}

function RelationGraph({ relatedObjects, activeObjectLabel, onSelectObjectLabel }) {
  const items = Array.isArray(relatedObjects) ? relatedObjects : [];
  const [hovered, setHovered] = useState("");
  const [focused, setFocused] = useState("");
  if (items.length === 0) return <div className="empty">No related objects.</div>;
  const primary = items.find((x) => x.role === "primary") || items[0];
  const peers = items.filter((x) => x !== primary).slice(0, 8);
  const active = hovered || focused || activeObjectLabel || "";
  const primaryLabel = objectRefLabel(primary);
  const width = 980;
  const height = 250;
  const clamp = (value, min, max) => Math.min(Math.max(value, min), max);
  const centerX = 170;
  const centerY = 118;
  const orbitCenterX = 660;
  const orbitCenterY = 130;
  const orbitRadiusX = 210;
  const orbitRadiusY = 106;
  const kindLegend = [...new Set(items.map((x) => String(x.kind || "Object")).filter(Boolean))].slice(0, 8);
  const peerNodes = peers.map((obj, i) => {
    const total = Math.max(peers.length - 1, 1);
    const ratio = total === 0 ? 0.5 : i / total;
    const angle = (-120 + ratio * 240) * (Math.PI / 180);
    let x = orbitCenterX + Math.cos(angle) * orbitRadiusX;
    let y = orbitCenterY + Math.sin(angle) * orbitRadiusY;
    if (i === 0) {
      x -= 20;
      y -= 8;
    } else if (i === peers.length - 1) {
      x -= 20;
      y += 8;
    } else if (i === 1) {
      y -= 4;
    } else if (i === peers.length - 2) {
      y += 4;
    }
    const nodePadding = 26;
    y = Math.min(Math.max(y, nodePadding), height - nodePadding);
    const unitX = Math.cos(angle);
    const unitY = Math.sin(angle);
    const labelX = clamp(x + unitX * 34, 24, width - 24);
    const labelY = clamp(y + unitY * 34, 14, height - 10);
    const nameX = clamp(x + unitX * 48, 72, width - 72);
    const nameY = clamp(y + unitY * 48, 18, height - 10);
    const textAnchor = "middle";
    const role = String(obj.role || "affected");
    const itemLabel = objectRefLabel(obj);
    const id = `${role}:${itemLabel}:${i}`;
    const cls = role === "owner" ? "relation-owner" : role === "upstream-suspect" ? "relation-upstream" : "relation-affected";
    const isActive = active === id || active === itemLabel;
    return { obj, i, x, y, role, itemLabel, id, cls, isActive, unitX, unitY, labelX, labelY, nameX, nameY, textAnchor };
  });
  const lockedDisplayLabel = focused
    ? focused === "primary"
      ? primaryLabel
      : peerNodes.find((node) => node.id === focused)?.itemLabel || activeObjectLabel || ""
    : "";
  return (
    <>
      <svg viewBox={`0 0 ${width} ${height}`} className="relation-svg" role="img">
        <g className="relation-edges-layer">
          {peerNodes.map((node) => {
            const dx = node.x - centerX;
            const dy = node.y - centerY;
            const length = Math.max(Math.hypot(dx, dy), 1);
            const ux = dx / length;
            const uy = dy / length;
            const primaryRadius = 30;
            const peerRadius = 26;
            const safeGap = 30;
            const startX = centerX + ux * (primaryRadius + safeGap);
            const startY = centerY + uy * (primaryRadius + safeGap);
            const endX = node.x - ux * (peerRadius + safeGap);
            const endY = node.y - uy * (peerRadius + safeGap);
            const spreadBase = (peerNodes.length - 1) / 2;
            let spreadOffset = (node.i - spreadBase) * 10;
            if (node.i <= 1) spreadOffset -= 12;
            if (node.i >= peerNodes.length - 2) spreadOffset += 12;
            const controlX = startX + dx * 0.45;
            const controlY = startY + dy * 0.45 + spreadOffset;
            return (
              <g key={`edge-${node.id}`}>
                <path
                  d={`M ${startX} ${startY} Q ${controlX} ${controlY}, ${endX} ${endY}`}
                  className={`relation-edge relation-edge-role-${node.role} ${node.isActive ? "relation-edge-active" : ""}`}
                />
              </g>
            );
          })}
        </g>
        <g
          onPointerEnter={() => setHovered("primary")}
          onPointerLeave={() => setHovered("")}
          onClick={() => {
            setFocused((prev) => (prev === "primary" ? "" : "primary"));
            onSelectObjectLabel(primaryLabel);
          }}
        >
          <circle cx={centerX} cy={centerY} r="34" className="relation-hitbox" />
          <circle cx={centerX} cy={centerY} r="36" className="relation-primary-halo-outer" />
          <circle cx={centerX} cy={centerY} r="33" className="relation-primary-halo" />
          <circle cx={centerX} cy={centerY} r="28" className={`relation-node relation-primary ${active === "primary" || active === primaryLabel ? "relation-node-active" : ""}`} />
          <circle cx={centerX} cy={centerY} r="30" className={`relation-kind-ring relation-kind-${resourceKindClass(primary.kind)}`} />
          <text x={centerX} y={centerY + 0.5} textAnchor="middle" dominantBaseline="middle" className="relation-kind-token">{resourceKindToken(primary.kind)}</text>
          <text x={centerX} y={centerY + 42} textAnchor="middle" className="relation-kind-outside">{(primary.kind || "Object").slice(0, 18)}</text>
          <text x={centerX} y={centerY + 56} textAnchor="middle" className="relation-name">{compactObjectName(primary.name || "primary", 20)}</text>
        </g>
        {peerNodes.map((node) => {
          return (
            <g
              key={node.id}
              onPointerEnter={() => setHovered(node.id)}
              onPointerLeave={() => setHovered("")}
              onClick={() => {
                setFocused((prev) => (prev === node.id ? "" : node.id));
                onSelectObjectLabel(node.itemLabel);
              }}
            >
              <circle cx={node.x} cy={node.y} r="30" className="relation-hitbox" />
              <circle cx={node.x} cy={node.y} r="24" className={`relation-node ${node.cls} ${node.isActive ? "relation-node-active" : ""}`} />
              <circle cx={node.x} cy={node.y} r="26" className={`relation-kind-ring relation-kind-${resourceKindClass(node.obj.kind)}`} />
              <text x={node.x} y={node.y + 0.5} textAnchor="middle" dominantBaseline="middle" className="relation-kind-token">{resourceKindToken(node.obj.kind)}</text>
              <text
                x={node.labelX}
                y={node.labelY + 4}
                textAnchor={node.textAnchor}
                className="relation-kind-outside"
              >
                {(node.obj.kind || "Obj").slice(0, 12)}
              </text>
              {focused === node.id || activeObjectLabel === node.itemLabel ? (
                <g className="relation-name-pill">
                  <rect
                    x={node.nameX - 63}
                    y={node.nameY - 14}
                    width="124"
                    height="18"
                    rx="9"
                    ry="9"
                  />
                  <text
                    x={node.nameX}
                    y={node.nameY - 1}
                    textAnchor={node.textAnchor}
                    className="relation-name"
                  >
                    {compactObjectName(node.obj.name || "item", 16)}
                  </text>
                </g>
              ) : null}
            </g>
          );
        })}
      </svg>
      <div className="hint relation-hint">
        {focused || activeObjectLabel
          ? `Focused: ${lockedDisplayLabel || activeObjectLabel}`
          : "Hover to inspect, click to lock and show object names."}
      </div>
      {kindLegend.length > 0 ? (
        <div className="relation-kind-legend">
          {kindLegend.map((kind) => (
            <span key={kind} className={`relation-kind-chip relation-kind-${resourceKindClass(kind)}`}>
              {resourceKindToken(kind)} · {kind}
            </span>
          ))}
        </div>
      ) : null}
    </>
  );
}

function severityClass(severity) {
  if (severity === "critical") return "sev-critical";
  if (severity === "warning") return "sev-warning";
  return "sev-info";
}

export default function App() {
  const initial = useMemo(() => readInitialUiState(), []);
  const [reports, setReports] = useState([]);
  const [selectedName, setSelectedName] = useState(initial.selectedName);
  const [selectedDetail, setSelectedDetail] = useState(null);
  const [listError, setListError] = useState("");
  const [detailError, setDetailError] = useState("");
  const [loading, setLoading] = useState(false);
  const [detailLoading, setDetailLoading] = useState(false);
  const [autoRefreshEnabled, setAutoRefreshEnabled] = useState(initial.autoRefreshEnabled);
  const [autoRefreshSeconds, setAutoRefreshSeconds] = useState(initial.autoRefreshSeconds);
  const [lastRefreshAt, setLastRefreshAt] = useState("");
  const [listLatencyMs, setListLatencyMs] = useState(null);
  const [detailLatencyMs, setDetailLatencyMs] = useState(null);
  const [apiLastError, setApiLastError] = useState("");
  const [listRequestCount, setListRequestCount] = useState(0);
  const [detailRequestCount, setDetailRequestCount] = useState(0);
  const [requestErrorCount, setRequestErrorCount] = useState(0);
  const [nextRefreshAt, setNextRefreshAt] = useState(null);
  const [opsExpanded, setOpsExpanded] = useState(initial.opsExpanded);
  const [opsCopyState, setOpsCopyState] = useState("");
  const [shortcutHelpOpen, setShortcutHelpOpen] = useState(false);
  const [timezone, setTimezone] = useState(initial.timezone);
  const [filters, setFilters] = useState(initial.filters);
  const [detailView, setDetailView] = useState("all");
  const [timelineWindow, setTimelineWindow] = useState("all");
  const [timelineQuery, setTimelineQuery] = useState("");
  const [timelineGroupSort, setTimelineGroupSort] = useState(initial.timelineGroupSort || "count");
  const [timelineNavExpanded, setTimelineNavExpanded] = useState(false);
  const [timelineCollapsedGroups, setTimelineCollapsedGroups] = useState({});
  const [timelineExpandedGroups, setTimelineExpandedGroups] = useState({});
  const [relationRoleFilter, setRelationRoleFilter] = useState("all");
  const [activeObjectLabel, setActiveObjectLabel] = useState("");
  const [activeTimelineEventKey, setActiveTimelineEventKey] = useState("");
  const [activeTimelineEventSignature, setActiveTimelineEventSignature] = useState("");
  const [activeTimelineEvent, setActiveTimelineEvent] = useState(null);
  const symptomInputRef = useRef(null);
  const detailCacheRef = useRef(new Map());

  async function loadReports() {
    const started = performance?.now ? performance.now() : Date.now();
    setLoading(true);
    setListRequestCount((v) => v + 1);
    try {
      setListError("");
      setApiLastError("");
      const resp = await fetch("/api/reports", { cache: "no-store" });
      if (!resp.ok) throw new Error(`load failed (${resp.status})`);
      const payload = await resp.json();
      const items = Array.isArray(payload.items) ? payload.items : [];
      const elapsed = (performance?.now ? performance.now() : Date.now()) - started;
      startTransition(() => {
        setReports(items);
        setLastRefreshAt(new Date().toISOString());
        setListLatencyMs(Math.max(0, Math.round(elapsed)));
      });
      if (items.length > 0) {
        const hasSelected = selectedName && items.some((x) => x.name === selectedName);
        if (!hasSelected && selectedName) {
          const cached = detailCacheRef.current.get(selectedName);
          if (cached) {
            setSelectedDetail(cached);
            setListError("");
            return;
          }
        }
        const nextName = hasSelected ? selectedName : items[0].name;
        if (nextName && nextName !== selectedName) {
          setSelectedName(nextName);
          void loadDetail(nextName);
        }
      } else {
        const cached = selectedName ? detailCacheRef.current.get(selectedName) : null;
        if (cached) {
          setSelectedDetail(cached);
        } else {
          setSelectedName("");
          setSelectedDetail(null);
        }
      }
    } catch (err) {
      const msg = String(err?.message || err);
      setListError(msg);
      setApiLastError(msg);
      setRequestErrorCount((v) => v + 1);
    } finally {
      setLoading(false);
      if (autoRefreshEnabled) {
        setNextRefreshAt(Date.now() + autoRefreshSeconds * 1000);
      }
    }
  }

  async function loadDetail(name) {
    if (!name) return;
    const started = performance?.now ? performance.now() : Date.now();
    setDetailLoading(true);
    setDetailRequestCount((v) => v + 1);
    try {
      setDetailError("");
      setApiLastError("");
      const resp = await fetch(`/api/reports/${encodeURIComponent(name)}`, { cache: "no-store" });
      if (!resp.ok) throw new Error(`detail failed (${resp.status})`);
      const detail = await resp.json();
      setSelectedDetail(detail);
      detailCacheRef.current.set(name, detail);
      const elapsed = (performance?.now ? performance.now() : Date.now()) - started;
      setDetailLatencyMs(Math.max(0, Math.round(elapsed)));
    } catch (err) {
      const fallback =
        reports.find((x) => x.name === name) ||
        detailCacheRef.current.get(name) ||
        (selectedDetail && selectedDetail.name === name ? selectedDetail : null) ||
        selectedDetail ||
        null;
      setSelectedDetail(fallback);
      if (fallback) {
        detailCacheRef.current.set(name, fallback);
      }
      const msg = String(err?.message || err);
      setDetailError(msg);
      setApiLastError(msg);
      setRequestErrorCount((v) => v + 1);
    } finally {
      setDetailLoading(false);
    }
  }

  useEffect(() => {
    void loadReports();
  }, []);

  useEffect(() => {
    if (!autoRefreshEnabled) {
      setNextRefreshAt(null);
      return undefined;
    }
    const first = Date.now() + autoRefreshSeconds * 1000;
    setNextRefreshAt(first);
    const timer = setInterval(() => void loadReports(), autoRefreshSeconds * 1000);
    const tick = setInterval(() => {
      setNextRefreshAt((prev) => {
        const now = Date.now();
        if (!prev || prev <= now) return now + autoRefreshSeconds * 1000;
        return prev;
      });
    }, 1000);
    return () => {
      clearInterval(timer);
      clearInterval(tick);
    };
  }, [autoRefreshEnabled, autoRefreshSeconds, selectedName]);

  const nextRefreshLabel = useMemo(() => {
    if (!autoRefreshEnabled || !nextRefreshAt) return "off";
    const leftMs = Math.max(0, nextRefreshAt - Date.now());
    const leftSec = Math.ceil(leftMs / 1000);
    return `${leftSec}s`;
  }, [autoRefreshEnabled, nextRefreshAt, lastRefreshAt]);
  const apiStatus = apiLastError ? "degraded" : "healthy";

  useEffect(() => {
    writeUiPrefs({ timezone, autoRefreshEnabled, autoRefreshSeconds, opsExpanded, timelineGroupSort });
  }, [timezone, autoRefreshEnabled, autoRefreshSeconds, opsExpanded, timelineGroupSort]);

  useEffect(() => {
    writeUiStateToUrl({ timezone, filters, selectedName });
  }, [timezone, filters, selectedName]);

  useEffect(() => {
    if (!selectedName) return;
    const exists = reports.some((x) => x.name === selectedName);
    if (!exists) return;
    if (selectedDetail && selectedDetail.name === selectedName) return;
    void loadDetail(selectedName);
  }, [selectedName, reports]);

  useEffect(() => {
    setActiveObjectLabel("");
    setActiveTimelineEventKey("");
    setActiveTimelineEventSignature("");
    setActiveTimelineEvent(null);
    setDetailView("all");
    setTimelineWindow("all");
    setTimelineQuery("");
    setTimelineNavExpanded(false);
    setTimelineCollapsedGroups({});
    setTimelineExpandedGroups({});
    setRelationRoleFilter("all");
  }, [selectedName]);

  const namespaces = useMemo(() => {
    const all = reports.map((r) => r.namespace).filter(Boolean);
    return [...new Set(all)].sort();
  }, [reports]);

  const filtered = useMemo(
    () =>
      reports
        .filter((r) => {
          if (filters.namespace && r.namespace !== filters.namespace) return false;
          if (filters.severity && r.severity !== filters.severity) return false;
          if (filters.symptom && !String(r.symptom || "").toLowerCase().includes(filters.symptom.toLowerCase())) return false;
          return true;
        })
        .sort((a, b) => String(b.lastAnalyzedAt || b.triggerAt || "").localeCompare(String(a.lastAnalyzedAt || a.triggerAt || ""))),
    [filters, reports],
  );
  const deferredFiltered = useDeferredValue(filtered);

  useEffect(() => {
    function isTypingTarget(target) {
      return !!target && (target.tagName === "INPUT" || target.tagName === "TEXTAREA" || target.isContentEditable);
    }

    function onKeyDown(event) {
      if (event.metaKey || event.ctrlKey || event.altKey) return;
      const target = event.target;
      const typing = isTypingTarget(target);
      if (event.key === "/") {
        if (typing) return;
        event.preventDefault();
        symptomInputRef.current?.focus();
        return;
      }
      if (typing) return;
      if (event.key === "o" || event.key === "O") {
        event.preventDefault();
        setOpsExpanded((v) => !v);
        return;
      }
      if (event.key === "?") {
        event.preventDefault();
        setShortcutHelpOpen((v) => !v);
        return;
      }
      if (event.key === "1" || event.key === "2" || event.key === "3" || event.key === "4") {
        event.preventDefault();
        const idx = Number(event.key) - 1;
        if (DETAIL_VIEWS[idx]) setDetailView(DETAIL_VIEWS[idx]);
        return;
      }
      const list = deferredFiltered;
      if (!Array.isArray(list) || list.length === 0) return;
      if (event.key !== "j" && event.key !== "k" && event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
      event.preventDefault();
      const currentIdx = Math.max(
        0,
        list.findIndex((x) => x.name === selectedName),
      );
      const delta = event.key === "j" || event.key === "ArrowDown" ? 1 : -1;
      const nextIdx = Math.min(Math.max(currentIdx + delta, 0), list.length - 1);
      const next = list[nextIdx];
      if (!next || next.name === selectedName) return;
      setSelectedName(next.name);
      void loadDetail(next.name);
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [deferredFiltered, selectedName]);

  const selectedFromList = useMemo(
    () => reports.find((x) => x.name === selectedName) || null,
    [reports, selectedName],
  );
  const selected = selectedDetail && (selectedDetail.name === selectedName || !selectedFromList) ? selectedDetail : selectedFromList;
  const selectedRecommendations = asList(selected?.recommendations);
  const selectedEvidence = asList(selected?.evidence);
  const selectedCauses = asList(selected?.probableCauses);
  const relatedObjects = Array.isArray(selected?.relatedObjects) ? selected.relatedObjects : [];
  const displayedRelatedObjects = useMemo(() => {
    if (relationRoleFilter === "all") return relatedObjects;
    return relatedObjects.filter((x) => String(x.role || "") === relationRoleFilter);
  }, [relatedObjects, relationRoleFilter]);
  const allTimelineEvents = Array.isArray(selected?.evidenceTimeline) ? selected.evidenceTimeline : [];
  const displayedTimelineEvents = useMemo(() => {
    if (timelineWindow === "all") return allTimelineEvents;
    if (allTimelineEvents.length === 0) return [];
    const parsed = allTimelineEvents
      .map((x) => ({ item: x, ms: Date.parse(x.time || "") }))
      .filter((x) => Number.isFinite(x.ms))
      .sort((a, b) => a.ms - b.ms);
    if (parsed.length === 0) return [];
    const focusedMs = Date.parse(activeTimelineEvent?.time || "");
    const anchorMs = Number.isFinite(focusedMs) ? focusedMs : parsed[parsed.length - 1].ms;
    const windowMs =
      timelineWindow === "5m"
        ? 5 * 60 * 1000
        : timelineWindow === "15m"
          ? 15 * 60 * 1000
          : timelineWindow === "1h"
            ? 60 * 60 * 1000
            : timelineWindow === "6h"
              ? 6 * 60 * 60 * 1000
              : Infinity;
    const half = windowMs / 2;
    const start = anchorMs - half;
    const end = anchorMs + half;
    return parsed.filter((x) => x.ms >= start && x.ms <= end).map((x) => x.item);
  }, [allTimelineEvents, timelineWindow, activeTimelineEvent]);
  const visibleTimelineEvents = useMemo(() => {
    if (!timelineQuery.trim()) return displayedTimelineEvents;
    const q = timelineQuery.trim().toLowerCase();
    return displayedTimelineEvents.filter((ev) => {
      const signal = String(ev.signal || "").toLowerCase();
      const obj = objectRefLabel(ev.objectRef).toLowerCase();
      return signal.includes(q) || obj.includes(q);
    });
  }, [displayedTimelineEvents, timelineQuery]);
  const orderedVisibleTimelineEvents = useMemo(
    () => visibleTimelineEvents.slice().sort((a, b) => Date.parse(a.time || "") - Date.parse(b.time || "")),
    [visibleTimelineEvents],
  );
  const activeEventOutOfWindow = useMemo(() => {
    if (!activeTimelineEventSignature) return false;
    return !displayedTimelineEvents.some((ev) => eventSignature(ev) === activeTimelineEventSignature);
  }, [displayedTimelineEvents, activeTimelineEventSignature]);
  const focusedTimelineIndex = useMemo(() => {
    if (!activeTimelineEventSignature) return -1;
    return orderedVisibleTimelineEvents.findIndex((ev) => eventSignature(ev) === activeTimelineEventSignature);
  }, [orderedVisibleTimelineEvents, activeTimelineEventSignature]);
  const prevTimelineEvent = focusedTimelineIndex > 0 ? orderedVisibleTimelineEvents[focusedTimelineIndex - 1] : null;
  const nextTimelineEvent =
    focusedTimelineIndex >= 0 && focusedTimelineIndex < orderedVisibleTimelineEvents.length - 1
      ? orderedVisibleTimelineEvents[focusedTimelineIndex + 1]
      : null;
  const navigatorEvent = activeTimelineEvent || orderedVisibleTimelineEvents[0] || null;
  const timelineDensity = useMemo(() => {
    if (!orderedVisibleTimelineEvents.length) return [];
    const parsed = orderedVisibleTimelineEvents
      .map((ev) => ({ ev, ms: Date.parse(ev.time || "") }))
      .filter((x) => Number.isFinite(x.ms))
      .sort((a, b) => a.ms - b.ms);
    if (!parsed.length) return [];
    const spanMs = Math.max(parsed[parsed.length - 1].ms - parsed[0].ms, 1);
    const bucketMs = spanMs <= 15 * 60 * 1000 ? 60 * 1000 : spanMs <= 60 * 60 * 1000 ? 5 * 60 * 1000 : 15 * 60 * 1000;
    const bucketMap = new Map();
    for (const item of parsed) {
      const bucketStart = Math.floor(item.ms / bucketMs) * bucketMs;
      const prev = bucketMap.get(bucketStart) || { ms: bucketStart, count: 0, events: [] };
      prev.count += 1;
      prev.events.push(item.ev);
      bucketMap.set(bucketStart, prev);
    }
    const maxCount = Math.max(...[...bucketMap.values()].map((x) => x.count), 1);
    return [...bucketMap.values()]
      .sort((a, b) => a.ms - b.ms)
      .map((x) => ({
        ...x,
        ratio: x.count / maxCount,
        label: formatAt(new Date(x.ms).toISOString(), timezone),
      }));
  }, [orderedVisibleTimelineEvents, timezone]);
  const groupedTimelineEvents = useMemo(() => {
    const groups = new Map();
    for (const ev of orderedVisibleTimelineEvents) {
      const signal = String(ev.signal || "signal");
      const existing = groups.get(signal) || { signal, events: [], firstMs: Number.POSITIVE_INFINITY, lastMs: Number.NEGATIVE_INFINITY };
      existing.events.push(ev);
      const ms = Date.parse(ev.time || "");
      if (Number.isFinite(ms)) {
        if (ms < existing.firstMs) existing.firstMs = ms;
        if (ms > existing.lastMs) existing.lastMs = ms;
      }
      groups.set(signal, existing);
    }
    const list = [...groups.values()];
    if (timelineGroupSort === "time") {
      return list.sort((a, b) => b.lastMs - a.lastMs || b.events.length - a.events.length || a.signal.localeCompare(b.signal));
    }
    return list.sort((a, b) => b.events.length - a.events.length || b.lastMs - a.lastMs || a.signal.localeCompare(b.signal));
  }, [orderedVisibleTimelineEvents, timelineGroupSort]);
  const rootCandidates = Array.isArray(selected?.rootCauseCandidates) ? selected.rootCauseCandidates : [];
  const topRootCandidate = rootCandidates[0] || null;
  const rawSignal = selected?.rawSignal || {};
  const keySignals = [
    rawSignal.reason ? `Event reason: ${rawSignal.reason}` : "",
    rawSignal.message ? `Event message: ${rawSignal.message}` : "",
    rawSignal.podPhase ? `Pod phase: ${rawSignal.podPhase}` : "",
    rawSignal.podReason ? `Pod reason: ${rawSignal.podReason}` : "",
    rawSignal.containerReason ? `Container reason: ${rawSignal.containerReason}` : "",
    rawSignal.deploymentCondition ? `Deployment condition: ${rawSignal.deploymentCondition}` : "",
    rawSignal.pvcPhase ? `PVC phase: ${rawSignal.pvcPhase}` : "",
  ].filter(Boolean);
  const hasOverviewContent =
    keySignals.length > 0 || selectedRecommendations.length > 0 || selectedCauses.length > 0 || selectedEvidence.length > 0;
  const hasAttributionContent =
    displayedRelatedObjects.length > 0 || !!topRootCandidate || rootCandidates.length > 0;
  const hasTimelineContent =
    orderedVisibleTimelineEvents.length > 0 || !!activeTimelineEvent;

  const summaryStats = useMemo(() => {
    const total = reports.length;
    let critical = 0;
    let warning = 0;
    for (const r of reports) {
      if (r.severity === "critical") critical += 1;
      else if (r.severity === "warning") warning += 1;
    }
    return { total, critical, warning };
  }, [reports]);

  const topSymptoms = useMemo(() => {
    const counts = new Map();
    for (const r of reports) {
      const symptom = String(r.symptom || "").trim();
      if (!symptom) continue;
      counts.set(symptom, (counts.get(symptom) || 0) + 1);
    }
    return [...counts.entries()]
      .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
      .slice(0, 6)
      .map(([symptom, count]) => ({ symptom, count }));
  }, [reports]);

  function focusTimelineEvent(ev) {
    if (!ev) return;
    const sig = eventSignature(ev);
    const obj = objectRefLabel(ev.objectRef);
    setActiveTimelineEvent({
      ...ev,
      signal: ev.signal || "signal",
      time: ev.time || "",
      objectLabel: obj || "",
      signature: sig,
    });
    setActiveTimelineEventSignature(sig);
    setActiveTimelineEventKey("");
    setActiveObjectLabel(obj || "");
  }

  function focusRelativeTimelineEvent(delta) {
    if (!orderedVisibleTimelineEvents.length) return;
    if (!activeTimelineEventSignature) {
      focusTimelineEvent(delta > 0 ? orderedVisibleTimelineEvents[0] : orderedVisibleTimelineEvents[orderedVisibleTimelineEvents.length - 1]);
      return;
    }
    const current = orderedVisibleTimelineEvents.findIndex((ev) => eventSignature(ev) === activeTimelineEventSignature);
    const nextIndex = Math.min(Math.max((current < 0 ? 0 : current) + delta, 0), orderedVisibleTimelineEvents.length - 1);
    focusTimelineEvent(orderedVisibleTimelineEvents[nextIndex]);
  }

  useEffect(() => {
    const activeSignal = String(activeTimelineEvent?.signal || "");
    if (!activeSignal) return;
    setTimelineCollapsedGroups((prev) => {
      if (!prev[activeSignal]) return prev;
      return { ...prev, [activeSignal]: false };
    });
  }, [activeTimelineEvent]);

  useEffect(() => {
    function isTypingTarget(target) {
      return !!target && (target.tagName === "INPUT" || target.tagName === "TEXTAREA" || target.isContentEditable);
    }
    function onTimelineHotkey(event) {
      if (event.metaKey || event.ctrlKey || event.altKey) return;
      if (isTypingTarget(event.target)) return;
      if (event.key === "," || event.key === "<") {
        event.preventDefault();
        focusRelativeTimelineEvent(-1);
      } else if (event.key === "." || event.key === ">") {
        event.preventDefault();
        focusRelativeTimelineEvent(1);
      }
    }
    window.addEventListener("keydown", onTimelineHotkey);
    return () => window.removeEventListener("keydown", onTimelineHotkey);
  }, [orderedVisibleTimelineEvents, activeTimelineEventSignature]);

  async function copyOpsSnapshot() {
    const payload = {
      generatedAt: new Date().toISOString(),
      status: apiStatus,
      listLatencyMs,
      detailLatencyMs,
      listRequestCount,
      detailRequestCount,
      requestErrorCount,
      nextRefreshIn: nextRefreshLabel,
      lastApiError: apiLastError || "",
      selectedReport: selectedName || "",
      filters,
    };
    const text = JSON.stringify(payload, null, 2);
    try {
      if (navigator?.clipboard?.writeText) {
        await navigator.clipboard.writeText(text);
      } else {
        throw new Error("clipboard unavailable");
      }
      setOpsCopyState("copied");
    } catch {
      setOpsCopyState("unavailable");
    } finally {
      setTimeout(() => setOpsCopyState(""), 1800);
    }
  }

  return (
    <main className="app">
      <header className="topbar">
        <div>
          <h1>K8s Diagnosis Workbench</h1>
          <p className="subtitle">Framework baseline: list triage, detail analysis, and timeline evidence.</p>
        </div>
        <div className="actions">
          <label className="refresh-toggle">
            <input
              aria-label="auto-refresh"
              type="checkbox"
              checked={autoRefreshEnabled}
              onChange={(e) => setAutoRefreshEnabled(e.target.checked)}
            />
            Auto
          </label>
          <select
            aria-label="refresh-interval"
            value={String(autoRefreshSeconds)}
            onChange={(e) => setAutoRefreshSeconds(Number(e.target.value) || 15)}
            disabled={!autoRefreshEnabled}
          >
            <option value="15">15s</option>
            <option value="30">30s</option>
            <option value="60">60s</option>
          </select>
          <select value={timezone} onChange={(e) => setTimezone(e.target.value)} aria-label="timezone">
            {TIMEZONES.map((tz) => (
              <option key={tz} value={tz}>{tz}</option>
            ))}
          </select>
          <button onClick={() => void loadReports()} disabled={loading}>{loading ? "Refreshing..." : "Refresh"}</button>
          <button onClick={() => setShortcutHelpOpen((v) => !v)} aria-label="Shortcuts">Shortcuts</button>
        </div>
      </header>
      {shortcutHelpOpen ? (
        <section className="shortcut-card" aria-label="shortcut-help">
          <div className="shortcut-head">
            <strong>Keyboard Shortcuts</strong>
            <button className="panel-reset" onClick={() => setShortcutHelpOpen(false)}>Close</button>
          </div>
          <div className="kv shortcut-kv">
            <span>/</span><strong>Focus symptom search</strong>
            <span>j / k</span><strong>Move report selection</strong>
            <span>1 / 2 / 3 / 4</span><strong>Switch detail view</strong>
            <span>o</span><strong>Toggle observability panel</strong>
            <span>, / .</span><strong>Previous / next timeline event</strong>
            <span>?</span><strong>Toggle this help panel</strong>
          </div>
        </section>
      ) : null}

      <section className="summary-strip">
        <div className="stat"><span>Total</span><strong>{summaryStats.total}</strong></div>
        <div className="stat"><span>Critical</span><strong>{summaryStats.critical}</strong></div>
        <div className="stat"><span>Warning</span><strong>{summaryStats.warning}</strong></div>
        <div className="stat grow"><span>Last refresh</span><strong>{formatAt(lastRefreshAt, timezone) || "not loaded"}</strong></div>
      </section>

      <section className="toolbar">
        <label>
          Namespace
          <select value={filters.namespace} onChange={(e) => setFilters({ ...filters, namespace: e.target.value })}>
            <option value="">All</option>
            {namespaces.map((ns) => (
              <option key={ns} value={ns}>{ns}</option>
            ))}
          </select>
        </label>
        <label>
          Severity
          <select value={filters.severity} onChange={(e) => setFilters({ ...filters, severity: e.target.value })}>
            <option value="">All</option>
            <option value="critical">critical</option>
            <option value="warning">warning</option>
            <option value="info">info</option>
          </select>
        </label>
        <label>
          Symptom
          <input
            ref={symptomInputRef}
            value={filters.symptom}
            onChange={(e) => setFilters({ ...filters, symptom: e.target.value })}
            placeholder="CrashLoopBackOff / Pending / FailedMount"
          />
        </label>
        <label className="toolbar-action">
          Actions
          <button
            className="clear-btn"
            onClick={() => setFilters(EMPTY_FILTERS)}
            disabled={!filters.namespace && !filters.severity && !filters.symptom}
          >
            Clear Filters
          </button>
        </label>
      </section>
      {topSymptoms.length > 0 ? (
        <section className="symptom-strip">
          <span className="symptom-title">Quick Symptom Filter</span>
          {topSymptoms.map((x) => (
            <button
              key={x.symptom}
              className={`symptom-chip ${filters.symptom.toLowerCase() === x.symptom.toLowerCase() ? "symptom-chip-active" : ""}`}
              onClick={() => setFilters({ ...filters, symptom: x.symptom })}
            >
              {x.symptom} ({x.count})
            </button>
          ))}
        </section>
      ) : null}

      {listError ? (
        <div className="error">
          List fetch failed: {listError}
          <button className="inline-action" onClick={() => void loadReports()}>Retry</button>
        </div>
      ) : null}

      <section className="layout">
        <aside className="list">
          {loading ? <div className="hint">Loading reports...</div> : null}
          {deferredFiltered.length === 0 ? <div className="empty">No reports match current filters.</div> : null}
          {deferredFiltered.map((r) => (
            <button
              key={r.name}
              className={`item ${selectedName === r.name ? "active" : ""}`}
              aria-current={selectedName === r.name ? "true" : "false"}
              onClick={() => {
                setSelectedName(r.name);
                setSelectedDetail(r);
                detailCacheRef.current.set(r.name, r);
                void loadDetail(r.name);
              }}
            >
              <div className="title-row">
                <div className="title">{workloadLabel(r)}</div>
                <span className={`severity ${severityClass(r.severity)}`}>{r.severity || "info"}</span>
              </div>
              <div className="chips-row">
                {Array.isArray(r.relatedObjects) && r.relatedObjects.length > 0 ? (
                  <span className="chip">{r.relatedObjects.length} related</span>
                ) : null}
                {Array.isArray(r.rootCauseCandidates) && r.rootCauseCandidates.length > 0 ? (
                  <span className="chip">{r.rootCauseCandidates.length} candidates</span>
                ) : null}
              </div>
              <div className="meta">
                <span>{r.namespace || "-"}</span>
                <span>{r.symptom || "-"}</span>
                <span>{formatAt(r.lastAnalyzedAt || r.triggerAt, timezone)}</span>
              </div>
            </button>
          ))}
        </aside>

        <article className="detail">
          {!selected ? <div className="empty">Select a report to inspect.</div> : null}
          {selected ? (
            <>
              <div className="detail-head">
                <h2>{workloadLabel(selected)}</h2>
                <span className={`severity ${severityClass(selected.severity)}`}>{selected.severity || "info"}</span>
              </div>
              <p className="lead">{selected.summary || "No summary provided."}</p>
              <div className="detail-view-toggle">
                {DETAIL_VIEWS.map((view) => (
                  <button
                    key={view}
                    className={detailView === view ? "view-active" : ""}
                    onClick={() => setDetailView(view)}
                  >
                    {view}
                  </button>
                ))}
              </div>
              <div className="focus-strip">
                {activeObjectLabel ? (
                  <span className="focus-chip">
                    Object focus: {activeObjectLabel}
                    <button
                      className="focus-clear"
                      onClick={() => setActiveObjectLabel("")}
                    >
                      Clear
                    </button>
                  </span>
                ) : null}
                {activeTimelineEvent ? (
                  <span className="focus-chip">
                    Timeline focus: {activeTimelineEvent.signal} @ {formatAt(activeTimelineEvent.time, timezone)}
                    <button
                      className="focus-clear"
                      onClick={() => {
                        setActiveTimelineEvent(null);
                        setActiveTimelineEventKey("");
                        setActiveTimelineEventSignature("");
                      }}
                    >
                      Clear
                    </button>
                  </span>
                ) : null}
              </div>
              {detailError ? (
                <div className="error detail-error-banner">
                  Detail fetch failed: {detailError}
                  <button className="inline-action" onClick={() => selectedName && void loadDetail(selectedName)}>Retry</button>
                </div>
              ) : null}
              {detailView === "overview" && !hasOverviewContent ? (
                <section className="card view-empty-card">
                  <h3>Overview</h3>
                  <div className="empty">No overview data available for this report.</div>
                </section>
              ) : null}
              {detailView === "attribution" && !hasAttributionContent ? (
                <section className="card view-empty-card">
                  <h3>Attribution</h3>
                  <div className="empty">No attribution data available for current role filter.</div>
                </section>
              ) : null}
              {detailView === "timeline" && !hasTimelineContent ? (
                <section className="card view-empty-card">
                  <h3>Timeline</h3>
                  <div className="empty">No timeline data available for current time window.</div>
                </section>
              ) : null}

              {detailView === "all" || detailView === "overview" ? (
              <div className="detail-grid">
                <section className="card">
                  <h3>Workload Context</h3>
                  <div className="kv">
                    <span>Namespace</span><strong>{selected.namespace || "-"}</strong>
                    <span>Symptom</span><strong>{selected.symptom || "-"}</strong>
                    <span>Source</span><strong>{selected.source || "-"}</strong>
                    <span>Trigger At</span><strong>{formatAt(selected.triggerAt, timezone) || "-"}</strong>
                    <span>Last Analyzed</span><strong>{formatAt(selected.lastAnalyzedAt, timezone) || "-"}</strong>
                    <span>Observed For</span><strong>{selected.observedFor ? `${selected.observedFor}s` : "-"}</strong>
                  </div>
                </section>

                <section className="card">
                  <h3>Key Signals</h3>
                  {keySignals.length === 0 ? <div className="empty">No key signals.</div> : null}
                  <ul>{keySignals.map((x) => <li key={x}>{x}</li>)}</ul>
                </section>

                <section className="card">
                  <h3>Fix Suggestions</h3>
                  {selectedRecommendations.length === 0 ? <div className="empty">No suggestions.</div> : null}
                  <ul>{selectedRecommendations.map((x) => <li key={x}>{x}</li>)}</ul>
                </section>

                <section className="card">
                  <h3>Probable Causes</h3>
                  {selectedCauses.length === 0 ? <div className="empty">No causes.</div> : null}
                  <ul>{selectedCauses.map((x) => <li key={x}>{x}</li>)}</ul>
                </section>

                <section className="card">
                  <h3>Evidence</h3>
                  {selectedEvidence.length === 0 ? <div className="empty">No evidence.</div> : null}
                  <ul>{selectedEvidence.map((x) => <li key={x}>{x}</li>)}</ul>
                </section>
              </div>
              ) : null}

              {detailView === "all" || detailView === "attribution" ? (
              <section className="card timeline-card">
                <h3>Related Objects Graph</h3>
                <div className="panel-controls">
                  <label>
                    Role
                    <select value={relationRoleFilter} onChange={(e) => setRelationRoleFilter(e.target.value)}>
                      {ROLE_OPTIONS.map((opt) => (
                        <option key={opt.value} value={opt.value}>{opt.label}</option>
                      ))}
                    </select>
                  </label>
                  <button
                    className="panel-reset"
                    onClick={() => setRelationRoleFilter("all")}
                    disabled={relationRoleFilter === "all"}
                  >
                    Reset
                  </button>
                </div>
                <RelationGraph
                  relatedObjects={displayedRelatedObjects}
                  activeObjectLabel={activeObjectLabel}
                  onSelectObjectLabel={(value) => setActiveObjectLabel(value)}
                />
                {displayedRelatedObjects.length > 0 ? (
                  <ul className="related-list">
                    {displayedRelatedObjects.map((x, idx) => (
                      <li key={`${objectRefLabel(x)}-${idx}`}>
                        <button
                          className={`related-focus ${activeObjectLabel === objectRefLabel(x) ? "related-focus-active" : ""}`}
                          onClick={() => {
                            setActiveObjectLabel(objectRefLabel(x) || "");
                            setActiveTimelineEventKey("");
                            setActiveTimelineEvent(null);
                            setActiveTimelineEventSignature("");
                          }}
                        >
                          <strong>{objectRefLabel(x) || "object"}</strong>
                        </button>
                        {x.role ? ` · ${x.role}` : ""}
                      </li>
                    ))}
                  </ul>
                ) : null}
                {displayedRelatedObjects.length === 0 ? <div className="empty">No related objects in current role filter.</div> : null}
              </section>
              ) : null}

              {detailView === "all" || detailView === "attribution" ? (
              <section className="card">
                <h3>Top Root Candidate</h3>
                {!topRootCandidate ? <div className="empty">No top candidate.</div> : null}
                {topRootCandidate ? (
                  <div className="candidate-top">
                    <strong>{objectRefLabel(topRootCandidate.objectRef) || "candidate"}</strong>
                    {topRootCandidate.reason ? <p>{topRootCandidate.reason}</p> : null}
                    {topRootCandidate.confidence !== undefined && topRootCandidate.confidence !== null ? (
                      <span className="candidate-confidence">confidence {topRootCandidate.confidence}</span>
                    ) : null}
                  </div>
                ) : null}
              </section>
              ) : null}

              {detailView === "all" || detailView === "attribution" ? (
              <section className="card">
                <h3>Root Cause Candidates</h3>
                {rootCandidates.length === 0 ? <div className="empty">No root cause candidates.</div> : null}
                <ul>
                  {rootCandidates.map((x, idx) => (
                    <li key={`${objectRefLabel(x.objectRef)}-${idx}`}>
                      <strong>{objectRefLabel(x.objectRef) || "candidate"}</strong>
                      {x.reason ? `: ${x.reason}` : ""}
                      {x.confidence !== undefined && x.confidence !== null ? ` (confidence ${x.confidence})` : ""}
                    </li>
                  ))}
                </ul>
              </section>
              ) : null}

              {detailView === "all" || detailView === "timeline" ? (
              <section className="card timeline-card">
                <h3>Evidence Timeline</h3>
                <div className="panel-controls">
                  <label>
                    Window
                    <select value={timelineWindow} onChange={(e) => setTimelineWindow(e.target.value)}>
                      {TIMELINE_WINDOWS.map((opt) => (
                        <option key={opt.value} value={opt.value}>{opt.label}</option>
                      ))}
                    </select>
                  </label>
                  <button
                    className="panel-reset"
                    onClick={() => setTimelineWindow("all")}
                    disabled={timelineWindow === "all"}
                  >
                    Reset
                  </button>
                  <label>
                    Find
                    <input
                      value={timelineQuery}
                      placeholder="signal/object"
                      onChange={(e) => setTimelineQuery(e.target.value)}
                    />
                  </label>
                  <label>
                    Group
                    <select aria-label="Group Sort" value={timelineGroupSort} onChange={(e) => setTimelineGroupSort(e.target.value)}>
                      <option value="count">By Count</option>
                      <option value="time">By Time</option>
                    </select>
                  </label>
                  <button
                    className="panel-reset"
                    onClick={() => setTimelineQuery("")}
                    disabled={!timelineQuery}
                  >
                    Clear Find
                  </button>
                </div>
                {activeEventOutOfWindow ? (
                  <div className="hint timeline-hint">
                    Current focused event is outside this window.
                    <button
                      className="inline-action"
                      onClick={() => setTimelineWindow("all")}
                    >
                      Restore Window
                    </button>
                  </div>
                ) : null}
                {activeTimelineEvent ? (
                  <div className="timeline-focus-bar">
                    <span>
                      Focused: {activeTimelineEvent.signal} @ {formatAt(activeTimelineEvent.time, timezone)}
                    </span>
                    <button
                      className="panel-reset"
                      onClick={() => focusRelativeTimelineEvent(-1)}
                      disabled={!prevTimelineEvent}
                    >
                      Prev
                    </button>
                    <button
                      className="panel-reset"
                      onClick={() => focusRelativeTimelineEvent(1)}
                      disabled={!nextTimelineEvent}
                    >
                      Next
                    </button>
                    <button
                      className="panel-reset"
                      onClick={() => {
                        if (activeEventOutOfWindow) setTimelineWindow("all");
                      }}
                    >
                      Jump to Focus
                    </button>
                    <button
                      className="panel-reset"
                      onClick={() => {
                        setActiveTimelineEvent(null);
                        setActiveTimelineEventKey("");
                        setActiveTimelineEventSignature("");
                      }}
                    >
                      Clear
                    </button>
                  </div>
                ) : null}
                <div className="timeline-legend">
                  <span className="legend-dot legend-dot-first" />
                  <span>First abnormal signal</span>
                  <span className="timeline-key-hint">Keys: , / .</span>
                </div>
                {timelineDensity.length > 0 ? (
                  <div className="timeline-density" aria-label="timeline-density">
                    {timelineDensity.map((bucket) => (
                      <button
                        key={bucket.ms}
                        className="timeline-density-bar"
                        aria-label={`density-${bucket.label}-${bucket.count}`}
                        style={{ height: `${Math.max(8, Math.round(bucket.ratio * 30))}px` }}
                        title={`${bucket.label} · ${bucket.count} events`}
                        onClick={() => focusTimelineEvent(bucket.events[bucket.events.length - 1])}
                      />
                    ))}
                  </div>
                ) : null}
                <HorizontalTimeline
                  events={visibleTimelineEvents}
                  timezone={timezone}
                  activeObjectLabel={activeObjectLabel}
                  activeEventKey={activeTimelineEventKey}
                  activeEventSignature={activeTimelineEventSignature}
                  onSelectObjectLabel={(value) => setActiveObjectLabel(value)}
                  onSelectEventKey={(value) => setActiveTimelineEventKey(value)}
                  onSelectEvent={(value) => focusTimelineEvent(value)}
                />
                <div className="timeline-nav">
                  <button
                    className="timeline-nav-toggle"
                    onClick={() => setTimelineNavExpanded((v) => !v)}
                  >
                    {timelineNavExpanded ? "Hide Event Navigator" : "Show Event Navigator"}
                    <span className="timeline-nav-meta">
                      {orderedVisibleTimelineEvents.length}/{displayedTimelineEvents.length} events
                    </span>
                  </button>
                  {timelineNavExpanded && orderedVisibleTimelineEvents.length > 10 ? (
                    <div className="timeline-list-hint">Long list: use page scroll to continue browsing events.</div>
                  ) : null}
                  {timelineNavExpanded ? (
                    <div className="timeline-nav-grid">
                      <div className="timeline-list">
                        {orderedVisibleTimelineEvents.length === 0 ? <div className="empty">No timeline events in current filter.</div> : null}
                        {groupedTimelineEvents.map((group) => {
                          const collapsed = !!timelineCollapsedGroups[group.signal];
                          const expanded = !!timelineExpandedGroups[group.signal];
                          const activeInGroup = group.events.some((ev) => eventSignature(ev) === activeTimelineEventSignature);
                          const visibleEvents =
                            !collapsed && !expanded && group.events.length > TIMELINE_GROUP_PREVIEW_LIMIT
                              ? group.events.slice(0, TIMELINE_GROUP_PREVIEW_LIMIT)
                              : group.events;
                          return (
                            <div key={group.signal} className={`timeline-group ${activeInGroup ? "timeline-group-active" : ""}`}>
                              <button
                                className="timeline-group-toggle"
                                onClick={() =>
                                  setTimelineCollapsedGroups((prev) => ({
                                    ...prev,
                                    [group.signal]: !prev[group.signal],
                                  }))
                                }
                              >
                                <span>{collapsed ? "▸" : "▾"} {group.signal}</span>
                                <span className="timeline-nav-meta">
                                  {group.events.length}
                                  {Number.isFinite(group.lastMs) ? ` · ${formatAt(new Date(group.lastMs).toISOString(), timezone)}` : ""}
                                </span>
                              </button>
                              {!collapsed
                                ? visibleEvents.map((ev, idx) => {
                                    const sig = eventSignature(ev);
                                    const active = activeTimelineEventSignature === sig;
                                    const label = `${formatAt(ev.time, timezone)} · ${ev.signal || "signal"}`;
                                    const obj = objectRefLabel(ev.objectRef);
                                    return (
                                      <button
                                        key={`${group.signal}-${sig}-${idx}`}
                                        className={`timeline-row ${active ? "timeline-row-active" : ""}`}
                                        onClick={() => focusTimelineEvent(ev)}
                                      >
                                        <span>{label}</span>
                                        {obj ? <strong>{obj}</strong> : null}
                                      </button>
                                    );
                                  })
                                : null}
                              {!collapsed && !expanded && group.events.length > TIMELINE_GROUP_PREVIEW_LIMIT ? (
                                <button
                                  className="timeline-group-more"
                                  aria-label={`Show more ${group.signal}`}
                                  onClick={() =>
                                    setTimelineExpandedGroups((prev) => ({
                                      ...prev,
                                      [group.signal]: true,
                                    }))
                                  }
                                >
                                  Show more ({group.events.length - TIMELINE_GROUP_PREVIEW_LIMIT})
                                </button>
                              ) : null}
                              {!collapsed && expanded && group.events.length > TIMELINE_GROUP_PREVIEW_LIMIT ? (
                                <button
                                  className="timeline-group-more"
                                  aria-label={`Show less ${group.signal}`}
                                  onClick={() =>
                                    setTimelineExpandedGroups((prev) => ({
                                      ...prev,
                                      [group.signal]: false,
                                    }))
                                  }
                                >
                                  Show less
                                </button>
                              ) : null}
                            </div>
                          );
                        })}
                      </div>
                      <aside className="timeline-sidecard">
                        <h4>Navigator Context</h4>
                        {!navigatorEvent ? <div className="empty">No focused event.</div> : null}
                        {navigatorEvent ? (
                          <>
                            <div className="kv timeline-side-kv">
                              <span>Window</span><strong>{timelineWindow}</strong>
                              <span>Visible</span><strong>{orderedVisibleTimelineEvents.length}</strong>
                              <span>Total</span><strong>{displayedTimelineEvents.length}</strong>
                            </div>
                            <h4 className="timeline-side-title">Timeline Inspector</h4>
                            <div className="kv timeline-side-kv">
                              <span>Signal</span><strong>{navigatorEvent.signal || "-"}</strong>
                              <span>Time</span><strong>{formatAt(navigatorEvent.time, timezone) || "-"}</strong>
                              <span>Object</span><strong>{navigatorEvent.objectLabel || objectRefLabel(navigatorEvent.objectRef) || "-"}</strong>
                              <span>Previous</span><strong>{prevTimelineEvent ? `${formatAt(prevTimelineEvent.time, timezone)} · ${prevTimelineEvent.signal || "signal"}` : "-"}</strong>
                              <span>Next</span><strong>{nextTimelineEvent ? `${formatAt(nextTimelineEvent.time, timezone)} · ${nextTimelineEvent.signal || "signal"}` : "-"}</strong>
                            </div>
                          </>
                        ) : null}
                      </aside>
                    </div>
                  ) : null}
                </div>
              </section>
              ) : null}

              {detailLoading ? <div className="hint">Loading detail...</div> : null}
            </>
          ) : null}
        </article>
      </section>
      <section className="bottom-ops-bar">
        <div className="ops-summary">
          <strong>Observability</strong>
          <span className={`status-pill ${apiStatus === "healthy" ? "status-healthy" : "status-degraded"}`}>{apiStatus}</span>
          <span className="ops-mini">req {listRequestCount}/{detailRequestCount}</span>
          <span className="ops-mini">err {requestErrorCount}</span>
          <button
            className="ops-toggle"
            onClick={() => setOpsExpanded((v) => !v)}
            aria-label="toggle-observability"
          >
            {opsExpanded ? "Collapse" : "Expand"}
          </button>
        </div>
        {opsExpanded ? (
          <div className="ops-details">
            <div className="ops-actions">
              <button className="ops-action-btn" onClick={() => void copyOpsSnapshot()}>Copy Snapshot</button>
              {opsCopyState === "copied" ? <span className="ops-copy-state success">Copied</span> : null}
              {opsCopyState === "unavailable" ? <span className="ops-copy-state warn">Clipboard unavailable</span> : null}
            </div>
            <div className="ops-item"><span>List API</span><strong>{listLatencyMs !== null ? `${listLatencyMs} ms` : "-"}</strong></div>
            <div className="ops-item"><span>Detail API</span><strong>{detailLatencyMs !== null ? `${detailLatencyMs} ms` : "-"}</strong></div>
            <div className="ops-item"><span>Requests</span><strong>{listRequestCount}/{detailRequestCount}</strong></div>
            <div className="ops-item"><span>Errors</span><strong>{requestErrorCount}</strong></div>
            <div className="ops-item">
              <span>Status</span>
              <strong className={`status-pill ${apiStatus === "healthy" ? "status-healthy" : "status-degraded"}`}>{apiStatus}</strong>
            </div>
            <div className="ops-item"><span>Next Refresh</span><strong>{nextRefreshLabel}</strong></div>
            <div className="ops-item grow"><span>Last API Error</span><strong>{apiLastError || "-"}</strong></div>
          </div>
        ) : null}
      </section>
    </main>
  );
}
