import type {
  Alert,
  InternalProduct,
  ObservationHistoryRow,
  PaginatedResponse,
  PriceIntelligenceDetails,
  PriceIntelligenceSummary,
  Product,
  ProductDetails,
  ProductMatch,
  RegisterResponse,
  Run,
  Schedule,
  Source,
  SourceDetails,
  Task,
  TokenResponse,
  TriggeredRun,
  User,
} from "./types";

const API_BASE_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

type ApiErrorBody = {
  detail?: string;
  error?: { message?: string };
};

export class ApiError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

async function request<T>(
  path: string,
  options: RequestInit = {},
  token?: string,
): Promise<T> {
  const headers = new Headers(options.headers);
  headers.set("Accept", "application/json");

  if (options.body !== undefined) {
    headers.set("Content-Type", "application/json");
  }

  if (token !== undefined) {
    headers.set("Authorization", `Bearer ${token}`);
  }

  let response: Response;

  try {
    response = await fetch(`${API_BASE_URL}${path}`, {
      ...options,
      headers,
    });
  } catch {
    throw new ApiError(
      "Unable to reach the API. Check that the API service is running.",
      0,
    );
  }

  if (!response.ok) {
    const body = (await response.json().catch(() => ({}))) as ApiErrorBody;
    throw new ApiError(body.detail ?? body.error?.message ?? `Request failed (${response.status}).`, response.status);
  }

  if (response.status === 204) {
    return undefined as T;
  }

  return (await response.json()) as T;
}

export function checkApiReadiness(): Promise<unknown> {
  return request("/readyz");
}

export function registerUser(input: {
  email: string;
  password: string;
  displayName: string;
}): Promise<RegisterResponse> {
  return request("/api/v1/auth/register", {
    method: "POST",
    body: JSON.stringify({
      email: input.email,
      password: input.password,
      display_name: input.displayName,
    }),
  });
}

export function loginUser(input: {
  email: string;
  password: string;
}): Promise<TokenResponse> {
  return request("/api/v1/auth/login", {
    method: "POST",
    body: JSON.stringify(input),
  });
}

export function getCurrentUser(token: string): Promise<User> {
  return request("/api/v1/auth/me", {}, token);
}

export function listSources(
  token: string,
  options: { cursor?: string | null; limit?: number } = {},
): Promise<PaginatedResponse<Source>> {
  const params = new URLSearchParams();
  if (options.cursor) params.set("cursor", options.cursor);
  if (options.limit) params.set("limit", String(options.limit));
  const query = params.toString();
  return request(`/api/v1/sources${query ? `?${query}` : ""}`, {}, token);
}

export function createSource(input: {
  token: string;
  url: string;
}): Promise<Source> {
  return request(
    "/api/v1/sources",
    {
      method: "POST",
      body: JSON.stringify({ url: input.url }),
    },
    input.token,
  );
}

export function triggerSourceRun(input: {
  token: string;
  sourceId: string;
}): Promise<TriggeredRun> {
  return request(
    `/api/v1/sources/${input.sourceId}/runs`,
    {
      method: "POST",
    },
    input.token,
  );
}

export function getRun(input: {
  token: string;
  runId: string;
}): Promise<Run> {
  return request(`/api/v1/runs/${input.runId}`, {}, input.token);
}

export function listRuns(input: {
  token: string;
  sourceId?: string;
  status?: string;
  cursor?: string | null;
  limit?: number;
}): Promise<PaginatedResponse<Run>> {
  const searchParams = new URLSearchParams({ limit: String(input.limit ?? 3) });
  if (input.sourceId) searchParams.set("source_id", input.sourceId);
  if (input.status) searchParams.set("status", input.status);
  if (input.cursor) searchParams.set("cursor", input.cursor);

  return request(`/api/v1/runs?${searchParams.toString()}`, {}, input.token);
}

export function getSource(input: {
  token: string;
  sourceId: string;
}): Promise<SourceDetails> {
  return request(`/api/v1/sources/${input.sourceId}`, {}, input.token);
}

export function upsertSourceSchedule(input: {
  token: string;
  sourceId: string;
  intervalMinutes: number;
  isActive?: boolean;
}): Promise<Schedule> {
  return request(
    `/api/v1/sources/${input.sourceId}/schedule`,
    {
      method: "PUT",
      body: JSON.stringify({
        interval_minutes: input.intervalMinutes,
        is_active: input.isActive ?? true,
      }),
    },
    input.token,
  );
}

export async function deleteSourceSchedule(input: {
  token: string;
  sourceId: string;
}): Promise<void> {
  await request<unknown>(
    `/api/v1/sources/${input.sourceId}/schedule`,
    {
      method: "DELETE",
    },
    input.token,
  );
}

export function updateSourceStatus(input: {
  token: string;
  sourceId: string;
  status: "active" | "paused";
}): Promise<Source> {
  return request(
    `/api/v1/sources/${input.sourceId}`,
    {
      method: "PATCH",
      body: JSON.stringify({ status: input.status }),
    },
    input.token,
  );
}

export async function archiveSource(input: {
  token: string;
  sourceId: string;
}): Promise<void> {
  await request<unknown>(
    `/api/v1/sources/${input.sourceId}`,
    {
      method: "DELETE",
    },
    input.token,
  );
}

export function unarchiveSource(input: {
  token: string;
  sourceId: string;
}): Promise<Source> {
  return request(
    `/api/v1/sources/${input.sourceId}/unarchive`,
    {
      method: "POST",
    },
    input.token,
  );
}

export function listRunTasks(input: {
  token: string;
  runId: string;
}): Promise<PaginatedResponse<Task>> {
  return request(`/api/v1/runs/${input.runId}/tasks`, {}, input.token);
}

export function listTasks(input: {
  token: string;
  status?: string;
  cursor?: string | null;
  limit?: number;
}): Promise<PaginatedResponse<Task>> {
  const params = new URLSearchParams({ limit: String(input.limit ?? 25) });
  if (input.status) params.set("status", input.status);
  if (input.cursor) params.set("cursor", input.cursor);
  return request(`/api/v1/tasks?${params.toString()}`, {}, input.token);
}

export function listAlerts(input: {
  token: string;
  sourceId?: string;
  kind?: string;
  cursor?: string | null;
  limit?: number;
}): Promise<PaginatedResponse<Alert>> {
  const params = new URLSearchParams({ limit: String(input.limit ?? 25) });
  if (input.sourceId) params.set("source_id", input.sourceId);
  if (input.kind) params.set("kind", input.kind);
  if (input.cursor) params.set("cursor", input.cursor);
  return request(`/api/v1/alerts?${params.toString()}`, {}, input.token);
}

export function listProducts(input: {
  token: string;
  sourceId: string;
  limit?: number;
}): Promise<PaginatedResponse<Product>> {
  const params = new URLSearchParams({
    source_id: input.sourceId,
    limit: String(input.limit ?? 1),
  });
  return request(`/api/v1/products?${params.toString()}`, {}, input.token);
}

export function getProduct(input: {
  token: string;
  productId: string;
}): Promise<ProductDetails> {
  return request(`/api/v1/products/${input.productId}`, {}, input.token);
}

export function listProductHistory(input: {
  token: string;
  productId: string;
  cursor?: string | null;
  limit?: number;
}): Promise<PaginatedResponse<ObservationHistoryRow>> {
  const params = new URLSearchParams({ limit: String(input.limit ?? 100) });
  if (input.cursor) params.set("cursor", input.cursor);
  return request(
    `/api/v1/products/${input.productId}/history?${params.toString()}`,
    {},
    input.token,
  );
}

export function listInternalProducts(input: {
  token: string;
  status?: string;
  sku?: string;
  q?: string;
  cursor?: string | null;
  limit?: number;
}): Promise<PaginatedResponse<InternalProduct>> {
  const params = new URLSearchParams({ limit: String(input.limit ?? 25) });
  if (input.status) params.set("status", input.status);
  if (input.sku) params.set("sku", input.sku);
  if (input.q) params.set("q", input.q);
  if (input.cursor) params.set("cursor", input.cursor);
  return request(`/api/v1/internal-products?${params.toString()}`, {}, input.token);
}

export function createInternalProduct(input: {
  token: string;
  name: string;
  sku: string;
  own_price: string;
  currency: string;
  barcode?: string;
  brand?: string;
  category?: string;
  map_price?: string;
}): Promise<InternalProduct> {
  return request(
    "/api/v1/internal-products",
    {
      method: "POST",
      body: JSON.stringify(input),
    },
    input.token,
  );
}

export function getInternalProduct(input: {
  token: string;
  id: string;
}): Promise<InternalProduct> {
  return request(`/api/v1/internal-products/${input.id}`, {}, input.token);
}

export function updateInternalProduct(input: {
  token: string;
  id: string;
  name?: string;
  sku?: string;
  own_price?: string;
  currency?: string;
  barcode?: string;
  brand?: string;
  category?: string;
  map_price?: string;
  status?: string;
}): Promise<InternalProduct> {
  return request(
    `/api/v1/internal-products/${input.id}`,
    {
      method: "PATCH",
      body: JSON.stringify(input),
    },
    input.token,
  );
}

export async function deleteInternalProduct(input: {
  token: string;
  id: string;
}): Promise<void> {
  await request<unknown>(
    `/api/v1/internal-products/${input.id}`,
    {
      method: "DELETE",
    },
    input.token,
  );
}

export function listProductMatches(input: {
  token: string;
  internalProductId?: string;
  productId?: string;
  cursor?: string | null;
  limit?: number;
}): Promise<PaginatedResponse<ProductMatch>> {
  const params = new URLSearchParams({ limit: String(input.limit ?? 25) });
  if (input.internalProductId) params.set("internal_product_id", input.internalProductId);
  if (input.productId) params.set("product_id", input.productId);
  if (input.cursor) params.set("cursor", input.cursor);
  return request(`/api/v1/product-matches?${params.toString()}`, {}, input.token);
}

export function createProductMatch(input: {
  token: string;
  internal_product_id: string;
  product_id?: string;
  product_url?: string;
}): Promise<ProductMatch> {
  return request(
    "/api/v1/product-matches",
    {
      method: "POST",
      body: JSON.stringify(input),
    },
    input.token,
  );
}

export async function deleteProductMatch(input: {
  token: string;
  id: string;
}): Promise<void> {
  await request<unknown>(
    `/api/v1/product-matches/${input.id}`,
    {
      method: "DELETE",
    },
    input.token,
  );
}

export function listPriceIntelligence(input: {
  token: string;
  mapViolation?: boolean;
  cursor?: string | null;
  limit?: number;
}): Promise<PaginatedResponse<PriceIntelligenceSummary>> {
  const params = new URLSearchParams({ limit: String(input.limit ?? 25) });
  if (input.mapViolation) params.set("map_violation", "true");
  if (input.cursor) params.set("cursor", input.cursor);
  return request(`/api/v1/price-intelligence?${params.toString()}`, {}, input.token);
}

export function getPriceIntelligenceDetails(input: {
  token: string;
  internalProductId: string;
}): Promise<PriceIntelligenceDetails> {
  return request(`/api/v1/price-intelligence/${input.internalProductId}`, {}, input.token);
}
