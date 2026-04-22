import type { ModelId } from "../types";
import { DEFAULT_MODEL, MODEL_OPTIONS } from "../types";

const STORAGE_KEY = "note-agent:model";

const VALID: Set<ModelId> = new Set(MODEL_OPTIONS.map((o) => o.id));

function isValid(id: unknown): id is ModelId {
  return typeof id === "string" && VALID.has(id as ModelId);
}

export function loadModel(): ModelId {
  if (typeof window === "undefined") return DEFAULT_MODEL;
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    return isValid(raw) ? raw : DEFAULT_MODEL;
  } catch {
    return DEFAULT_MODEL;
  }
}

export function saveModel(id: ModelId): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(STORAGE_KEY, id);
  } catch {}
}
