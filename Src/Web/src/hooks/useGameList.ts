import { useCallback, useEffect, useState } from "react";
import { ApiError, listGames } from "../api/client";
import type { GameSummaryResponse } from "../api/types";

const POLL_INTERVAL_MS = 4000;

/** Polls GET /games for the history sidebar (requirement #3). */
export function useGameList(refreshKey: number) {
  const [games, setGames] = useState<GameSummaryResponse[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      setError(null);
      setGames(await listGames());
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : "게임 목록을 불러오지 못했습니다.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
    const interval = setInterval(() => void refresh(), POLL_INTERVAL_MS);
    return () => clearInterval(interval);
  }, [refresh, refreshKey]);

  return { games, loading, error, refresh };
}
