export type ApiStatus = "checking" | "ready" | "unavailable";

export type TokenResponse = {
  access_token: string;
  token_type: "bearer";
  expires_in: number;
};

export type User = {
  id: string;
  email: string;
  display_name: string;
  is_verified: boolean;
};

export type Workspace = {
  id: string;
  name: string;
  slug: string;
};

export type RegisterResponse = {
  user: User;
  workspace: Workspace;
  token: TokenResponse;
};

export type AuthSession = {
  token: string;
  user: User;
};

export type SourceStatus = "active" | "paused" | "archived";

export type Source = {
  id: string;
  url: string;
  normalized_url: string;
  adapter_type: string;
  status: SourceStatus;
  created_at: string | null;
  updated_at: string | null;
};

export type Pagination = {
  next_cursor: string | null;
  has_more: boolean;
};

export type PaginatedResponse<T> = {
  data: T[];
  pagination: Pagination;
};
