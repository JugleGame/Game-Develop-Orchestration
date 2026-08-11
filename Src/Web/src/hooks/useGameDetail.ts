import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, gameStreamUrl, getGameStatus } from "../api/client";
import type { GameStatusResponse, JobEvent } from "../api/types";

const TERMINAL_STATUSES = new Set(["done", "escalated", "cancelled"]);
const MAX_LOG_ENTRIES = 100;

/**
 * Combines a status snapshot (GET /games/{id}/status) with the live SSE
 * stream (GET /games/{id}/stream) so the pipeline view (requirement #2)
 * updates as soon as each LangGraph node transitions, without polling.
 */
export function useGameDetail(gameId: string | null) {
  const [status, setStatus] = useState<GameStatusResponse | null>(null);
  const [log, setLog] = useState<JobEvent[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const gameIdRef = useRef(gameId);
  gameIdRef.current = gameId;

  const refresh = useCallback(async (id: string) => {
    try {
      setError(null);
      const data = await getGameStatus(id);
      if (gameIdRef.current === id) setStatus(data);
    } catch (e) {
      if (gameIdRef.current === id) {
        setError(e instanceof ApiError ? e.detail : "게임 상태를 불러오지 못했습니다.");
      }
    }
  }, []);

  useEffect(() => {
    if (!gameId) {
      setStatus(null);
      setLog([]);
      return;
    }

    setLoading(true);
    setLog([]);
    void refresh(gameId).finally(() => setLoading(false));

    const source = new EventSource(gameStreamUrl(gameId));
    source.onmessage = (message) => {
      let event: JobEvent;
      try {
        event = JSON.parse(message.data) as JobEvent;
      } catch {
        return;
      }
      setLog((prev) => [...prev.slice(-(MAX_LOG_ENTRIES - 1)), event]);
      void refresh(gameId);
    };
    source.onerror = () => {
      // The connection stays open server-side even after the job reaches a
      // terminal status (app/utils/events.py listens on Redis pub/sub
      // indefinitely); the browser's built-in auto-retry is enough here.
    };

    return () => source.close();
  }, [gameId, refresh]);

  const isTerminal = status ? TERMINAL_STATUSES.has(status.status) : false;

  return { status, log, loading, error, isTerminal, refresh: () => gameId && refresh(gameId) };
}
