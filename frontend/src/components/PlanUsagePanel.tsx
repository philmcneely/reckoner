/**
 * Plan usage — every vendor's usage against its plan, read once for the whole fleet.
 *
 * One card per vendor: each window (5-hour, weekly, monthly, …) as a bar with its used
 * percentage and reset time, the plan, and WHERE the number came from:
 *   API        the vendor's own usage API (read on HAL)
 *   client     the vendor's own client on a fleet box (herdr, a CLI)
 *   our logs   the vendor publishes no limits; our proxies' own record of calls
 * A reading older than its expected refresh is marked stale; a failed read shows the error
 * and the last good numbers.
 */

import { useCallback, useEffect, useState } from 'react';
import { api } from '../utils/api';
import type { QuotaReading, QuotaWindow } from '../types';

const REFRESH_MS = 60_000;

function resetIn(iso: string | null | undefined): string | null {
  if (!iso) return null;
  const ms = new Date(iso).getTime() - Date.now();
  if (!Number.isFinite(ms)) return null;
  if (ms <= 0) return 'resetting';
  const mins = Math.round(ms / 60_000);
  if (mins < 60) return `resets in ${mins}m`;
  const hours = Math.floor(mins / 60);
  if (hours < 48) return `resets in ${hours}h ${mins % 60}m`;
  return `resets in ${Math.round(hours / 24)}d`;
}

function ago(iso: string | null | undefined): string {
  if (!iso) return 'never';
  const s = Math.round((Date.now() - new Date(iso).getTime()) / 1000);
  if (!Number.isFinite(s)) return '—';
  if (s < 90) return `${s}s ago`;
  if (s < 5400) return `${Math.round(s / 60)}m ago`;
  if (s < 172800) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}

function barColor(pct: number): string {
  if (pct >= 85) return 'bg-red-500';
  if (pct >= 60) return 'bg-amber-400';
  return 'bg-emerald-500';
}

const SOURCE_LABEL: Record<QuotaReading['source_kind'], { text: string; cls: string }> = {
  api: { text: 'vendor API', cls: 'text-sky-300 bg-sky-950/60 border-sky-800/50' },
  client: { text: 'client', cls: 'text-violet-300 bg-violet-950/60 border-violet-800/50' },
  our_logs: { text: 'our logs', cls: 'text-amber-300 bg-amber-950/50 border-amber-800/50' },
};

function compactNumber(n: number | null | undefined): string {
  if (n === null || n === undefined) return '—';
  if (n >= 1e9) return `${(n / 1e9).toFixed(1)}B`;
  if (n >= 1e6) return `${(n / 1e6).toFixed(1)}M`;
  if (n >= 1e3) return `${(n / 1e3).toFixed(1)}K`;
  return `${Math.round(n)}`;
}

function WindowRow({ w }: { w: QuotaWindow }) {
  const pct = w.used_pct ?? null;
  return (
    <div className="space-y-1">
      <div className="flex items-baseline justify-between text-xs">
        <span className="text-gray-300">{w.name}</span>
        <span className="text-gray-400 tabular-nums">
          {pct === null ? '—' : `${pct < 10 ? pct.toFixed(1) : Math.round(pct)}%`}
          {w.used != null && w.limit != null && (
            <span className="text-gray-600"> · {compactNumber(w.used)}/{compactNumber(w.limit)}{w.unit ? ` ${w.unit}` : ''}</span>
          )}
        </span>
      </div>
      <div className="h-1.5 rounded-full bg-gray-800 overflow-hidden" role="progressbar"
           aria-valuenow={pct ?? undefined} aria-valuemin={0} aria-valuemax={100} aria-label={w.name}>
        {pct !== null && <div className={`h-full ${barColor(pct)}`} style={{ width: `${Math.min(pct, 100)}%` }} />}
      </div>
      {resetIn(w.resets_at) && <div className="text-[11px] text-gray-600">{resetIn(w.resets_at)}</div>}
    </div>
  );
}

function VendorCard({ r }: { r: QuotaReading }) {
  const source = SOURCE_LABEL[r.source_kind] ?? SOURCE_LABEL.api;
  const shown = r.ok ? r : { ...r, windows: r.last_good?.windows ?? [] };
  const worst = Math.max(0, ...shown.windows.map((w) => w.used_pct ?? 0));
  const extra = r.extra ?? {};
  return (
    <div className={`rounded-2xl border p-4 space-y-3 bg-gray-900/60 ${worst >= 85 ? 'border-red-800/60' : 'border-gray-800/60'}`}>
      <div className="flex items-start justify-between gap-2">
        <div>
          <div className="text-sm font-medium text-gray-100">{r.name}</div>
          <div className="text-xs text-gray-500">{r.plan ?? ' '}</div>
        </div>
        <span className={`text-[10px] px-2 py-0.5 rounded-full border whitespace-nowrap ${source.cls}`} title={r.source}>
          {source.text}
        </span>
      </div>

      {!r.ok && (
        <div className="text-xs text-red-300 bg-red-950/40 border border-red-900/40 rounded-lg px-2 py-1">
          {r.error ?? 'read failed'}{r.last_good ? ' — showing last good reading' : ''}
        </div>
      )}

      {shown.windows.length > 0 ? (
        <div className="space-y-2.5">{shown.windows.map((w) => <WindowRow key={w.name} w={w} />)}</div>
      ) : (
        r.source_kind === 'our_logs' && (
          <div className="text-xs text-gray-400 space-y-0.5">
            <div>{compactNumber(extra.calls_7d as number)} calls in 7 days ({compactNumber(extra.ok_7d as number)} ok)</div>
            <div>{compactNumber(extra.input_tokens_7d as number)} in · {compactNumber(extra.output_tokens_7d as number)} out tokens</div>
          </div>
        )
      )}

      {r.notes && <p className="text-[11px] leading-snug text-gray-500">{r.notes}</p>}

      <div className="flex items-center justify-between text-[11px] text-gray-600">
        <span title={r.source}>{r.source}</span>
        <span className={r.stale ? 'text-amber-400' : ''}>{r.stale ? 'stale · ' : ''}{ago(r.fetched_at)}</span>
      </div>
    </div>
  );
}

export function PlanUsagePanel() {
  const [vendors, setVendors] = useState<QuotaReading[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const res = await api.getQuota();
      setVendors(res.vendors);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    const first = setTimeout(load, 0); // state is set from the fetch callback, not the effect body
    const t = setInterval(load, REFRESH_MS);
    return () => {
      clearTimeout(first);
      clearInterval(t);
    };
  }, [load]);

  if (error && !vendors) {
    return <div className="text-xs text-gray-500">Plan usage unavailable: {error}</div>;
  }
  if (!vendors) return null;

  const sorted = [...vendors].sort((a, b) => {
    const wa = Math.max(0, ...a.windows.map((w) => w.used_pct ?? 0));
    const wb = Math.max(0, ...b.windows.map((w) => w.used_pct ?? 0));
    return wb - wa || a.name.localeCompare(b.name);
  });

  return (
    <section>
      <div className="flex items-center justify-between mb-4">
        <h2 className="text-xs font-semibold text-gray-400 uppercase tracking-widest">Plan usage</h2>
        <span className="text-xs text-gray-600">
          {vendors.filter((v) => v.ok && !v.stale).length} of {vendors.length} current · fullest first
        </span>
      </div>
      <div className="grid gap-4" style={{ gridTemplateColumns: 'repeat(auto-fill, minmax(280px, 1fr))' }}>
        {sorted.map((r) => <VendorCard key={r.id} r={r} />)}
      </div>
    </section>
  );
}
