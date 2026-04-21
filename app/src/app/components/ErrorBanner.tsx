"use client";

import { AlertCircle, RotateCcw, X } from "lucide-react";

interface Props {
  message: string;
  canRetry: boolean;
  onRetry: () => void;
  onDismiss: () => void;
}

export default function ErrorBanner({ message, canRetry, onRetry, onDismiss }: Props) {
  return (
    <div
      role="alert"
      className="flex items-center gap-3 border-t border-rose-200 bg-rose-50 px-6 py-2 text-xs text-rose-700"
    >
      <AlertCircle size={14} aria-hidden="true" />
      <span className="flex-1">{message}</span>
      {canRetry && (
        <button
          type="button"
          onClick={onRetry}
          className="inline-flex items-center gap-1 rounded-md border border-rose-300 bg-white px-2 py-1 text-rose-700 transition-colors hover:bg-rose-100"
        >
          <RotateCcw size={12} />
          Retry
        </button>
      )}
      <button
        type="button"
        onClick={onDismiss}
        aria-label="Dismiss error"
        className="rounded-md p-1 text-rose-600 hover:bg-rose-100"
      >
        <X size={14} />
      </button>
    </div>
  );
}
