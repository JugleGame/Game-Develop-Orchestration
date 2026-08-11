import type {
  ApprovalResponse,
  ArtifactResponse,
  CancelResponse,
  GameCreateResponse,
  GameStatusResponse,
  GameSummaryResponse,
} from "./types";

const BASE_URL = (import.meta.env.VITE_API_BASE_URL as string | undefined) ?? "http://localhost:8000";
// Optional shared secret; must match the backend's API_KEY. Empty = auth disabled.
const API_KEY = (import.meta.env.VITE_API_KEY as string | undefined) ?? "";

export class ApiError extends Error {
  constructor(
    public readonly status: number,
    public readonly detail: string,
  ) {
    super(`${status}: ${detail}`);
    this.name = "ApiError";
  }
}

function defaultHeaders(): Record<string, string> {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (API_KEY) headers["X-API-Key"] = API_KEY;
  return headers;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${BASE_URL}${path}`, {
    headers: defaultHeaders(),
    ...init,
  });

  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body = (await response.json()) as { detail?: string };
      detail = body.detail ?? detail;
    } catch {
      // response had no JSON body; keep statusText
    }
    throw new ApiError(response.status, detail);
  }

  return (await response.json()) as T;
}

export function createGame(prompt: string): Promise<GameCreateResponse> {
  return request("/games", { method: "POST", body: JSON.stringify({ prompt }) });
}

export function listGames(limit = 50): Promise<GameSummaryResponse[]> {
  return request(`/games?limit=${limit}`);
}

export function getGameStatus(gameId: string): Promise<GameStatusResponse> {
  return request(`/games/${gameId}/status`);
}

export function approveGame(
  gameId: string,
  approved: boolean,
  feedback?: string,
): Promise<ApprovalResponse> {
  return request(`/games/${gameId}/approve`, {
    method: "POST",
    body: JSON.stringify({ approved, feedback: feedback ?? null }),
  });
}

export function getGameArtifact(gameId: string): Promise<ArtifactResponse> {
  return request(`/games/${gameId}/artifact`);
}

export function cancelGame(gameId: string): Promise<CancelResponse> {
  return request(`/games/${gameId}/cancel`, { method: "POST" });
}

export function reviseGame(gameId: string, prompt: string): Promise<GameCreateResponse> {
  return request(`/games/${gameId}/revise`, { method: "POST", body: JSON.stringify({ prompt }) });
}

export function gameStreamUrl(gameId: string): string {
  // EventSource cannot set request headers, so the key rides as a query
  // parameter — the one exception the backend's require_api_key allows.
  const url = `${BASE_URL}/games/${gameId}/stream`;
  return API_KEY ? `${url}?api_key=${encodeURIComponent(API_KEY)}` : url;
}
