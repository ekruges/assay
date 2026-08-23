export const DATA_BASE_URL = (import.meta.env?.VITE_ASSAY_DATA_URL || "/data").replace(/\/+$/, "");
export const API_BASE_URL = (import.meta.env?.VITE_ASSAY_API_URL || "").replace(/\/+$/, "");
const TICKER = /^[A-Z0-9.-]{1,15}$/;
const DENOMINATOR_FLOOR = 100_000;

export type CompanyStatus = "eligible" | "excluded" | "unresolved";
export type Grade = "A" | "A/B" | "B" | "B/C" | "C" | "C/D" | "D" | "D/E" | "E";
export type FlagName =
  | "degenerate_inputs"
  | "dilution"
  | "distress"
  | "fortress"
  | "late_filer"
  | "short_runway"
  | "thin_data";

export interface Coverage {
  resolved: number;
  wanted: number;
}

export interface Company {
  ticker: string;
  cik: number;
  name: string;
  exchange: string | null;
  status: CompanyStatus;
  reason: string | null;
  sector: string | null;
  sic: number | null;
  sic_description: string | null;
  has_companyfacts: boolean;
}

export interface IndexRow extends Company {
  grade: Grade | null;
  percentile: number | null;
  composite: number | null;
  peer_group: string | null;
  sampling_standard_deviation: number | null;
  coverage: Coverage | null;
  active_flags: FlagName[];
  implied_expectations: Record<string, number | null> | null;
  live_price?: number | null;
  live_price_timestamp?: string | null;
  artifact: string;
}

export interface IndexArtifact {
  schema_version: 1;
  generated_at: string;
  as_of: string;
  summary: Record<string, unknown> & { tickers: number };
  tickers: IndexRow[];
}

export interface UniverseArtifact {
  schema_version: 1;
  generated_at: string;
  as_of: string;
  summary: Record<string, unknown> & { tickers: number };
  tickers: Company[];
}

export interface ArtifactIndexRow {
  artifact?: string;
  ticker_count: number;
}

export interface PeerGroupIndexArtifact {
  schema_version: 1;
  generated_at: string;
  as_of: string;
  peer_groups: Array<ArtifactIndexRow & { peer_group: string }>;
}

export interface SectorIndexArtifact {
  schema_version: 1;
  generated_at: string;
  as_of: string;
  sectors: Array<ArtifactIndexRow & { sector: string }>;
}

export interface CoverageAuditArtifact {
  schema_version: 1;
  generated_at: string;
  as_of: string;
  factor_computability_by_sector: Record<string, Record<string, Record<string, number>>>;
  selected_tags_by_sector: Record<string, Record<string, Record<string, number>>>;
  net_share_issuance_by_market_equity_bucket: Record<string, Record<string, number>>;
  size_measure: string;
}

export interface ConstructionSensitivityArtifact {
  schema_version: 1;
  generated_at: string;
  as_of: string;
  scenarios: Record<string, {
    changed_from_baseline: number;
    companies: number;
    grade_distribution: Record<string, number>;
  }>;
  scope_note: string;
}

export interface MethodologyArtifact {
  schema_version: 1;
  generated_at: string;
  as_of: string;
  claim: string;
  disclaimer: string;
  grade: {
    bands: Array<{ grade: "A" | "B" | "C" | "D" | "E"; upper_percentile: number }>;
    boundary_rule: string;
    factor_definitions: Record<string, {
      direction: "higher" | "lower";
      formula: string;
      notes?: string;
    }>;
    sleeves: Record<string, string[]>;
    live_snapshot_feeds_grade: false;
    minimum_computable_factors: number;
    minimum_peer_group: number;
    [key: string]: unknown;
  };
  chs_12m: {
    caveat: string;
    coefficients: Record<string, number>;
    fitted_sample: string;
    imputation: false;
    intercept: number;
    probability_type: string;
    source: string;
    winsorization: string;
  };
  implied_expectations: {
    feeds_grade: false;
    predictive_claim: false;
    quadrants: false;
    verdict: null;
  };
  market_data: Record<string, unknown>;
  point_in_time: Record<string, unknown>;
}

export interface Receipt {
  accession: string;
  concept: string;
  end: string;
  filed: string;
  filing_url: string;
  fiscal_period: string | null;
  fiscal_year: number | null;
  form: string;
  namespace: string;
  start: string | null;
  tag: string;
  unit: string;
  value: number;
}

export interface Factor {
  name: string;
  status: "resolved" | "unresolved";
  value: number | null;
  unit: string;
  direction: "higher" | "lower";
  period_end: string | null;
  reason: string | null;
  detail: string | null;
  failure_class?: "missing_input" | "non_positive_or_below_floor_denominator" | "insufficient_history";
  inputs: Receipt[];
}

export interface FactorPercentile {
  direction: "higher" | "lower";
  peer_count: number;
  peer_group: string;
  percentile: number;
  raw_value: number;
}

export interface GradeResult {
  status: "resolved" | "unresolved";
  grade: Grade | null;
  percentile: number | null;
  composite: number | null;
  peer_count: number | null;
  peer_group: string | null;
  sampling_standard_deviation: number | null;
  coverage: Coverage;
  sleeve_scores: Record<string, number | null>;
  factor_percentiles: Record<string, FactorPercentile>;
  reason: string | null;
}

export interface PanelMetric {
  name: string;
  status: "resolved" | "unresolved";
  value: number | null;
  unit: string;
  period_end: string | null;
  reason: string | null;
  detail: string | null;
  inputs: Receipt[];
  market_inputs: Record<string, unknown>;
}

export interface LiveInputs {
  price?: { close?: number | null } | null;
  market_equity?: number | null;
  debt?: number | null;
  cash?: number | null;
  ebit?: number | null;
  free_cash_flow?: number | null;
  book_equity?: number | null;
  sales?: number | null;
}

export interface TickerReport {
  schema_version: 1;
  generated_at: string;
  as_of: string;
  company: Company;
  status: string;
  reason?: string | null;
  disclaimer: string;
  coverage?: Coverage;
  grade: GradeResult | null;
  factors?: Factor[];
  facts?: Receipt[];
  flags?: Array<{ name: FlagName; active: boolean; status: string; detail: string }>;
  market_scope?: { status: string; reason?: string; detail?: string };
  market_snapshot?: Quote | Record<string, unknown>;
  live_implied_expectations?: LiveValuation;
  implied_expectations?: {
    metrics: PanelMetric[];
    live_inputs: LiveInputs;
    feeds_grade: false;
    predictive_claim: false;
    verdict: null;
  };
  models?: Record<string, unknown>;
  diagnostics?: Record<string, unknown>;
  grade_sensitivity?: Record<string, {
    status: "resolved" | "unresolved";
    grade: Grade | null;
    percentile: number | null;
    peer_count: number | null;
    peer_group: string | null;
    peer_policy: string;
    weight_policy: string;
  }>;
}

export interface DistributionCurve {
  status: "resolved" | "unresolved";
  reason?: string;
  method?: string;
  bandwidth?: number;
  domain?: [number, number];
  points: Array<{ x: number; y: number; value: number }>;
}

export interface PeerGroupArtifact {
  schema_version: 1;
  generated_at: string;
  as_of: string;
  peer_group: string;
  ticker_count: number;
  distribution_curve: DistributionCurve;
  tickers: IndexRow[];
}

export interface SectorArtifact {
  schema_version: 1;
  generated_at: string;
  as_of: string;
  sector: string;
  display_rules: { continuous: true; quadrants: false; verdict: null; predictive_claim: false };
  peer_cloud: Array<{
    ticker: string;
    financial_condition_percentile: number;
    metrics: Record<string, number | null>;
  }>;
  tickers: IndexRow[];
}

export interface Quote {
  ticker: string;
  status: "resolved" | "unresolved";
  feed: string;
  price: number | null;
  price_timestamp: string | null;
  previous_close: number | null;
  day_change: number | null;
  day_change_percent: number | null;
  bid: number | null;
  ask: number | null;
  quoted_spread: number | null;
  quoted_spread_percent: number | null;
  feeds_grade: false;
  cache_seconds: number;
}

export interface LiveValuation {
  status: "resolved" | "unresolved";
  reason?: "missing_live_or_baseline_price";
  as_of?: string | null;
  price?: number;
  market_equity?: number;
  enterprise_value?: number | null;
  metrics: Record<string, number | null>;
  feeds_grade: false;
  predictive_claim?: false;
  verdict: null;
}

export interface ForecastCallEvent {
  schema_version: 1;
  event: "call";
  id: string;
  ts: string;
  ticker: string;
  horizon_days: number;
  target_date: string;
  p_outperform_spy: number;
  thesis: string;
  grade_at_call: Grade | null;
  price_basis: {
    date: string;
    adjustment: "all";
    feed: string;
    stock_reference_close: number;
    spy_reference_close: number;
  };
}

export interface ForecastResolutionEvent {
  schema_version: 1;
  event: "resolution";
  call_id: string;
  resolved_at: string;
  ticker: string;
  price_start_date: string;
  price_end_date: string;
  stock_start_close: number;
  stock_end_close: number;
  spy_start_close: number;
  spy_end_close: number;
  stock_return: number;
  spy_return: number;
  excess_return: number;
  outcome: 0 | 1;
  brier: number;
  log_loss: number;
}

export type ForecastEvent = ForecastCallEvent | ForecastResolutionEvent;

export interface ForecastArtifact {
  schema_version: 1;
  generated_at: string;
  as_of: string;
  scorecard: {
    calls: number;
    resolved: number;
    pending: number;
    model: Record<string, number | null> | null;
    base_rate_reference: Record<string, number | boolean | null> | null;
    calibration: Array<{
      range: string;
      count: number;
      mean_probability: number;
      observed_rate: number;
    }>;
  };
  events: ForecastEvent[];
}

export interface CompanyPageData {
  report: TickerReport;
  peerGroup: PeerGroupArtifact | null;
  sector: SectorArtifact | null;
}

export interface CompanyLiveData {
  quote: Quote | null;
  quoteError: string | null;
  liveImpliedExpectations: LiveValuation | null;
}

export async function loadIndex(): Promise<IndexArtifact> {
  return loadArtifact<IndexArtifact>("index.json");
}

export async function loadUniverse(): Promise<UniverseArtifact> {
  return loadArtifact<UniverseArtifact>("universe.json");
}

export async function loadTicker(ticker: string): Promise<TickerReport> {
  return loadArtifact<TickerReport>(`tickers/${safeTicker(ticker)}.json`);
}

export async function loadPeerGroup(peerGroup: string): Promise<PeerGroupArtifact> {
  return loadArtifact<PeerGroupArtifact>(`peer_groups/${safeSegment(peerGroup)}.json`);
}

export async function loadSector(sector: string): Promise<SectorArtifact> {
  return loadArtifact<SectorArtifact>(`sectors/${safeSegment(sector)}.json`);
}

export async function loadMethodology(): Promise<MethodologyArtifact> {
  return loadArtifact<MethodologyArtifact>("methodology.json");
}

export async function loadPeerGroupIndex(): Promise<PeerGroupIndexArtifact> {
  return loadArtifact<PeerGroupIndexArtifact>("peer_groups/index.json");
}

export async function loadSectorIndex(): Promise<SectorIndexArtifact> {
  return loadArtifact<SectorIndexArtifact>("sectors/index.json");
}

export async function loadCoverageAudit(): Promise<CoverageAuditArtifact> {
  return loadArtifact<CoverageAuditArtifact>("audit/coverage.json");
}

export async function loadConstructionSensitivity(): Promise<ConstructionSensitivityArtifact> {
  return loadArtifact<ConstructionSensitivityArtifact>("audit/construction_sensitivity.json");
}

export async function loadForecasts(): Promise<ForecastArtifact> {
  return loadArtifact<ForecastArtifact>("forecasts.json");
}

export async function loadQuote(ticker: string): Promise<Quote> {
  const response = await fetch(`${API_BASE_URL}/api/quote?ticker=${encodeURIComponent(safeTicker(ticker))}`);
  if (!response.ok) throw new Error(`Quote request failed (${response.status})`);
  const value: unknown = await response.json();
  if (!value || typeof value !== "object" || (value as { feeds_grade?: unknown }).feeds_grade !== false) {
    throw new Error("Unsupported Assay quote response");
  }
  return value as Quote;
}

export async function loadCompanyPage(ticker: string): Promise<CompanyPageData> {
  const report = await loadTicker(ticker);
  const [peerGroup, sector] = await Promise.all([
    report.grade?.peer_group ? loadPeerGroup(report.grade.peer_group) : null,
    report.company.sector ? loadSector(report.company.sector) : null,
  ]);
  return { report, peerGroup, sector };
}

export async function loadCompanyLiveData(report: TickerReport): Promise<CompanyLiveData> {
  const quoteResult = await loadQuote(report.company.ticker).then(
    (quote) => ({ quote, quoteError: null }),
    (error: unknown) => ({
      quote: null,
      quoteError: error instanceof Error ? error.message : "Live quote unavailable",
    }),
  );
  const liveImpliedExpectations =
    quoteResult.quote?.status === "resolved" && report.implied_expectations
      ? revalueImpliedExpectations(
          report.implied_expectations.live_inputs,
          quoteResult.quote.price,
          quoteResult.quote.price_timestamp,
        )
      : report.live_implied_expectations ?? null;
  return { ...quoteResult, liveImpliedExpectations };
}

export function searchCompanies(rows: IndexRow[], query: string, limit = 12): IndexRow[] {
  const needle = query.trim().toUpperCase();
  if (!needle) return [];
  return rows
    .filter((row) => row.ticker.includes(needle) || row.name.toUpperCase().includes(needle))
    .sort((left, right) => searchRank(left, needle) - searchRank(right, needle) || left.ticker.localeCompare(right.ticker))
    .slice(0, limit);
}

export function revalueImpliedExpectations(
  inputs: LiveInputs,
  livePrice: number | null,
  priceTimestamp: string | null = null,
): LiveValuation {
  const baselinePrice = positive(inputs.price?.close);
  const baselineMarketEquity = positive(inputs.market_equity);
  const price = positive(livePrice);
  if (!baselinePrice || !baselineMarketEquity || !price) {
    return {
      status: "unresolved",
      reason: "missing_live_or_baseline_price",
      metrics: {},
      feeds_grade: false,
      verdict: null,
    };
  }
  const marketEquity = baselineMarketEquity * price / baselinePrice;
  const debt = finite(inputs.debt);
  const cash = finite(inputs.cash);
  const enterpriseValue = debt !== null && cash !== null ? marketEquity + debt - cash : null;
  const metrics: Record<string, number | null> = {};
  for (const [name, value] of Object.entries({
    fcf_yield: inputs.free_cash_flow,
    book_to_price: inputs.book_equity,
    sales_to_price: inputs.sales,
  })) {
    const numerator = finite(value);
    metrics[name] = numerator !== null && marketEquity > DENOMINATOR_FLOOR ? numerator / marketEquity : null;
  }
  const ebit = finite(inputs.ebit);
  metrics.ebit_to_enterprise_value =
    ebit !== null && enterpriseValue !== null && enterpriseValue > DENOMINATOR_FLOOR
      ? ebit / enterpriseValue
      : null;
  return {
    status: "resolved",
    as_of: priceTimestamp,
    price,
    market_equity: marketEquity,
    enterprise_value: enterpriseValue,
    metrics,
    feeds_grade: false,
    predictive_claim: false,
    verdict: null,
  };
}

async function loadArtifact<T>(path: string): Promise<T> {
  const response = await fetch(`${DATA_BASE_URL}/${path}`);
  if (!response.ok) throw new Error(`Assay data request failed (${response.status}): ${path}`);
  const value: unknown = await response.json();
  if (!value || typeof value !== "object" || (value as { schema_version?: unknown }).schema_version !== 1) {
    throw new Error(`Unsupported Assay artifact: ${path}`);
  }
  return value as T;
}

function safeTicker(value: string): string {
  const ticker = value.trim().toUpperCase();
  if (!TICKER.test(ticker)) throw new Error("Invalid ticker");
  return ticker;
}

function safeSegment(value: string): string {
  if (!/^[a-z0-9_.-]+$/i.test(value)) throw new Error("Invalid artifact segment");
  return value;
}

function searchRank(row: IndexRow, needle: string): number {
  if (row.ticker === needle) return 0;
  if (row.ticker.startsWith(needle)) return 1;
  if (row.name.toUpperCase().startsWith(needle)) return 2;
  return 3;
}

function positive(value: unknown): number | null {
  const number = Number(value);
  return Number.isFinite(number) && number > 0 ? number : null;
}

function finite(value: unknown): number | null {
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}
