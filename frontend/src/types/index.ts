/**
 * TypeScript types matching the FastAPI backend schemas.
 */

export type ProviderStatus = 'ok' | 'error' | 'unconfigured' | 'stale' | 'disabled';
export type ProviderCategory = 'ai' | 'cloud';

export interface AuthField {
  key: string;
  label: string;
  placeholder: string;
  secret: boolean;
}

export interface BalanceSnapshot {
  provider_id: string;
  provider_name: string;
  category: ProviderCategory;
  balance_usd: number | null;
  total_credits: number | null;
  used_credits: number | null;
  remaining_credits: number | null;
  currency: string;
  status: ProviderStatus;
  error_message: string | null;
  fetched_at: string | null;
  raw_data: Record<string, unknown> | null;
}

export interface DashboardResponse {
  providers: BalanceSnapshot[];
  last_updated: string;
  total_usd_balance: number | null;
}

export interface ProviderMeta {
  id: string;
  name: string;
  category: ProviderCategory;
  auth_type: string;
  auth_fields: AuthField[];
  auth_help: string;
  tier: number;
  note: string | null;
  enabled: boolean;
  is_configured: boolean;
  refresh_interval: number;
  last_refresh: string | null;
}

export interface ProviderSettings {
  id: string;
  name: string;
  category: ProviderCategory;
  enabled: boolean;
  auth_type: string;
  auth_fields: AuthField[];
  auth_help: string;
  credentials: Record<string, string | null>;
  refresh_interval: number;
  tier: number;
  note: string | null;
}

export interface RefreshResponse {
  message: string;
  providers_refreshed: string[];
}

export interface HealthResponse {
  status: string;
  version: string;
  db_connected: boolean;
}

export interface AuthStatus {
  auth_enabled: boolean;
  dashboard_protected: boolean;
  authenticated: boolean;
}

// ── Plan usage / quota ───────────────────────────────────────────────────────

export interface QuotaWindow {
  name: string;
  used_pct: number | null;
  resets_at?: string | null;
  starts_at?: string | null;
  used?: number | null;
  limit?: number | null;
  unit?: string | null;
}

export interface QuotaReading {
  id: string;
  name: string;
  source: string;
  source_kind: 'api' | 'client' | 'our_logs';
  plan: string | null;
  ok: boolean;
  error: string | null;
  fetched_at: string | null;
  windows: QuotaWindow[];
  money: Record<string, number>;
  extra: Record<string, unknown>;
  notes: string | null;
  stale: boolean;
  last_good?: { windows: QuotaWindow[]; fetched_at: string | null } | null;
}

export interface QuotaResponse {
  vendors: QuotaReading[];
}
