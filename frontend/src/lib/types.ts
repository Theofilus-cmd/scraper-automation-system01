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

export type Schedule = {
  id: string;
  source_id: string;
  interval_minutes: number;
  is_active: boolean;
  next_run_at: string | null;
  last_run_at: string | null;
  created_at: string | null;
  updated_at: string | null;
};

export type SourceDetails = Source & {
  schedule: Schedule | null;
  current_product?: {
    product: Product;
    observation: Observation | null;
  } | null;
};

export type Pagination = {
  next_cursor: string | null;
  has_more: boolean;
};

export type PaginatedResponse<T> = {
  data: T[];
  pagination: Pagination;
};

export type RunStatus =
  | "queued"
  | "pending"
  | "running"
  | "completed"
  | "completed_with_errors"
  | "failed"
  | "cancelled";

export type Run = {
  id: string;
  source_id: string;
  schedule_id: string | null;
  status: RunStatus;
  triggered_by: "manual" | "schedule";
  total_tasks: number;
  succeeded_tasks: number;
  failed_tasks: number;
  started_at: string | null;
  finished_at: string | null;
  created_at: string | null;
};

export type TriggeredRun = {
  run_id: string;
  task_id: string | null;
  status: RunStatus;
};

export type TaskStatus =
  | "queued"
  | "in_progress"
  | "succeeded"
  | "failed"
  | "retrying"
  | "dead_letter";

export type Task = {
  id: string;
  run_id: string;
  source_id: string;
  status: TaskStatus;
  attempt_count: number;
  max_attempts: number;
  error_reason: string | null;
  error_detail: Record<string, unknown> | null;
  queued_at: string | null;
  started_at: string | null;
  finished_at: string | null;
};

export type AlertKind = "price_changed" | "stock_status_changed";

export type Alert = {
  id: string;
  source_id: string;
  product_id: string;
  run_id: string | null;
  task_id: string | null;
  kind: AlertKind;
  product_name: string;
  product_url: string;
  currency: string | null;
  old_value: string;
  new_value: string;
  email_status: "pending" | "sending" | "sent" | "failed" | "skipped";
  sent_at: string | null;
  created_at: string | null;
};

export type Product = {
  id: string;
  source_id: string;
  product_identity_key: string;
  product_url: string;
  sku: string | null;
  source_product_id: string | null;
  created_at: string | null;
  updated_at: string | null;
};

export type Observation = {
  product_name: string | null;
  brand: string | null;
  category: string | null;
  price: string | null;
  currency: string | null;
  original_price: string | null;
  stock_status: string;
  rating: string | null;
  review_count: number | null;
  image_url: string | null;
  is_valid: boolean;
  scraped_at: string | null;
};

export type ProductDetails = Product & {
  current_observation: Observation | null;
};

export type ObservationHistoryRow = Observation & {
  id: string;
  product_id: string;
  run_id: string;
  task_id: string;
  version_created_at: string | null;
};
