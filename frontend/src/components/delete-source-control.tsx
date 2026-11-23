"use client";

import { useState } from "react";

import { archiveSource } from "../lib/api";
import { errorMessage } from "../hooks/use-async";
import { ConfirmDialog } from "./ui";

/** "Delete" button + confirmation dialog. The API archives (soft-deletes) the source. */
export function DeleteSourceControl({
  token,
  sourceId,
  sourceUrl,
  onDeleted,
}: {
  token: string;
  sourceId: string;
  sourceUrl: string;
  onDeleted: () => void;
}) {
  const [isOpen, setIsOpen] = useState(false);
  const [isBusy, setIsBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function confirm() {
    setIsBusy(true);
    setError(null);
    try {
      await archiveSource({ token, sourceId });
      setIsOpen(false);
      onDeleted();
    } catch (err) {
      setError(errorMessage(err));
      setIsOpen(false);
    } finally {
      setIsBusy(false);
    }
  }

  return (
    <>
      <button className="source-action-danger" onClick={() => setIsOpen(true)} type="button">
        Delete source
      </button>
      {error ? (
        <p className="form-error" role="alert">
          {error}
        </p>
      ) : null}
      {isOpen ? (
        <ConfirmDialog
          confirmLabel="Delete"
          isBusy={isBusy}
          message={`Delete ${sourceUrl}? The source is archived and its scheduled runs are disabled. You can restore it later by unarchiving.`}
          onCancel={() => setIsOpen(false)}
          onConfirm={() => void confirm()}
          title="Delete this source?"
        />
      ) : null}
    </>
  );
}
