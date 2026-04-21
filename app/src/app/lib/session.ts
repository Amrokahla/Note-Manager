export function newSessionId(): string {
  return crypto.randomUUID();
}

export function shortSessionId(id: string): string {
  return id.slice(0, 8);
}
