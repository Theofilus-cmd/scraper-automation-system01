"use client";

import { FormEvent, useState } from "react";

import { ApiError, getCurrentUser, loginUser, registerUser } from "../lib/api";
import type { User } from "../lib/types";

type AuthPanelProps = {
  onAuthenticated: (token: string, user: User) => void;
};

type Mode = "login" | "register";

function messageFor(error: unknown): string {
  if (error instanceof ApiError) {
    return error.message;
  }

  return "Something went wrong. Please try again.";
}

export function AuthPanel({ onAuthenticated }: AuthPanelProps) {
  const [mode, setMode] = useState<Mode>("login");
  const [email, setEmail] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [password, setPassword] = useState("");
  const [isPasswordVisible, setIsPasswordVisible] = useState(false);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setErrorMessage(null);
    setIsSubmitting(true);

    try {
      if (mode === "register") {
        const response = await registerUser({ email, password, displayName });
        onAuthenticated(response.token.access_token, response.user);
      } else {
        const token = await loginUser({ email, password });
        const user = await getCurrentUser(token.access_token);
        onAuthenticated(token.access_token, user);
      }
    } catch (error) {
      setErrorMessage(messageFor(error));
    } finally {
      setIsSubmitting(false);
    }
  }

  function switchMode(nextMode: Mode) {
    setMode(nextMode);
    setErrorMessage(null);
  }

  const isRegistering = mode === "register";

  return (
    <section aria-labelledby="auth-heading" className="auth-panel">
      <p className="eyebrow">Workspace access</p>
      <h2 id="auth-heading">{isRegistering ? "Create your account" : "Welcome back"}</h2>
      <p className="muted">
        {isRegistering
          ? "Create an account to start monitoring product sources."
          : "Sign in to manage your scraping sources and runs."}
      </p>

      <div aria-label="Authentication mode" className="auth-tabs" role="tablist">
        <button
          aria-selected={!isRegistering}
          className={!isRegistering ? "active" : undefined}
          onClick={() => switchMode("login")}
          role="tab"
          type="button"
        >
          Sign in
        </button>
        <button
          aria-selected={isRegistering}
          className={isRegistering ? "active" : undefined}
          onClick={() => switchMode("register")}
          role="tab"
          type="button"
        >
          Create account
        </button>
      </div>

      <form onSubmit={handleSubmit}>
        {isRegistering ? (
          <label>
            Display name
            <input
              autoComplete="name"
              onChange={(event) => setDisplayName(event.target.value)}
              required
              value={displayName}
            />
          </label>
        ) : null}

        <label>
          Email
          <input
            autoComplete="email"
            onChange={(event) => setEmail(event.target.value)}
            required
            type="email"
            value={email}
          />
        </label>

        <label>
          Password
          <span className="password-field">
            <input
              autoComplete={isRegistering ? "new-password" : "current-password"}
              minLength={isRegistering ? 12 : 1}
              onChange={(event) => setPassword(event.target.value)}
              required
              type={isPasswordVisible ? "text" : "password"}
              value={password}
            />
            <button
              aria-label={isPasswordVisible ? "Hide password" : "Show password"}
              className="password-toggle"
              onClick={() => setIsPasswordVisible((visible) => !visible)}
              type="button"
            >
              {isPasswordVisible ? "Hide" : "Show"}
            </button>
          </span>
        </label>

        {errorMessage ? (
          <p aria-live="polite" className="form-error" role="alert">
            {errorMessage}
          </p>
        ) : null}

        <button disabled={isSubmitting} type="submit">
          {isSubmitting
            ? "Please wait…"
            : isRegistering
              ? "Create account"
              : "Sign in"}
        </button>
      </form>
    </section>
  );
}
