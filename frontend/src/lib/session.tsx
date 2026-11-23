"use client";

import {
  createContext,
  type ReactNode,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react";

import { ApiError, checkApiReadiness, getCurrentUser } from "./api";
import {
  clearStoredAccessToken,
  getStoredAccessToken,
  storeAccessToken,
} from "./auth-storage";
import type { ApiStatus, AuthSession, User } from "./types";

type SessionContextValue = {
  apiStatus: ApiStatus;
  session: AuthSession | null;
  isRestoring: boolean;
  signIn: (token: string, user: User) => void;
  signOut: () => void;
};

const SessionContext = createContext<SessionContextValue | null>(null);

export function SessionProvider({ children }: { children: ReactNode }) {
  const [apiStatus, setApiStatus] = useState<ApiStatus>("checking");
  const [session, setSession] = useState<AuthSession | null>(null);
  const [isRestoring, setIsRestoring] = useState(true);

  useEffect(() => {
    let cancelled = false;

    async function check() {
      try {
        await checkApiReadiness();
        if (!cancelled) setApiStatus("ready");
      } catch {
        if (!cancelled) setApiStatus("unavailable");
      }
    }

    void check();
    const interval = window.setInterval(() => void check(), 10_000);
    return () => {
      cancelled = true;
      window.clearInterval(interval);
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    const token = getStoredAccessToken();

    async function restore() {
      if (token === null) {
        if (!cancelled) setIsRestoring(false);
        return;
      }
      try {
        const user = await getCurrentUser(token);
        if (!cancelled) setSession({ token, user });
      } catch (error) {
        if (error instanceof ApiError && error.status === 401) {
          clearStoredAccessToken();
        }
      } finally {
        if (!cancelled) setIsRestoring(false);
      }
    }

    void restore();
    return () => {
      cancelled = true;
    };
  }, []);

  const signIn = useCallback((token: string, user: User) => {
    storeAccessToken(token);
    setSession({ token, user });
  }, []);

  const signOut = useCallback(() => {
    clearStoredAccessToken();
    setSession(null);
  }, []);

  const value = useMemo(
    () => ({ apiStatus, session, isRestoring, signIn, signOut }),
    [apiStatus, session, isRestoring, signIn, signOut],
  );

  return <SessionContext.Provider value={value}>{children}</SessionContext.Provider>;
}

export function useSession(): SessionContextValue {
  const value = useContext(SessionContext);
  if (value === null) {
    throw new Error("useSession must be used inside <SessionProvider>.");
  }
  return value;
}

/** The access token for pages rendered behind the auth gate. */
export function useToken(): string {
  const { session } = useSession();
  if (session === null) {
    throw new Error("useToken requires an authenticated session.");
  }
  return session.token;
}
