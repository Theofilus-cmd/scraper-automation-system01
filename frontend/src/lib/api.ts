import type { PaginatedResponse, RegisterResponse, Run, Source, TokenResponse, TriggeredRun, User } from "./types";

const API_BASE_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

type ApiErrorBody = {
  detail?: string;
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
    throw new ApiError(body.detail ?? `Request failed (${response.status}).`, response.status);
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

export function listSources(token: string): Promise<PaginatedResponse<Source>> {
  return request("/api/v1/sources", {}, token);
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
  sourceId: string;
  limit?: number;
}): Promise<PaginatedResponse<Run>> {
  const searchParams = new URLSearchParams({
    source_id: input.sourceId,
    limit: String(input.limit ?? 3),
  });

  return request(`/api/v1/runs?${searchParams.toString()}`, {}, input.token);
}
