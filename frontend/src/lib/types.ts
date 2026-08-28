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
