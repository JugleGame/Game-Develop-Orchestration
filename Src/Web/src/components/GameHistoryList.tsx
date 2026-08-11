import type { GameSummaryResponse } from "../api/types";
import { StatusBadge } from "./StatusBadge";

interface GameHistoryListProps {
  games: GameSummaryResponse[];
  loading: boolean;
  error: string | null;
  selectedGameId: string | null;
  onSelect: (gameId: string) => void;
}

/** Requirement #3: history of previously generated games. */
export function GameHistoryList({
  games,
  loading,
  error,
  selectedGameId,
  onSelect,
}: GameHistoryListProps) {
  return (
    <div className="game-history">
      <h3>게임 히스토리</h3>
      {loading && games.length === 0 && <p className="muted">불러오는 중...</p>}
      {error && <p className="form-error">{error}</p>}
      {!loading && games.length === 0 && !error && (
        <p className="muted">아직 생성된 게임이 없습니다.</p>
      )}
      <ul>
        {games.map((game) => (
          <li key={game.game_id}>
            <button
              type="button"
              className={game.game_id === selectedGameId ? "history-item selected" : "history-item"}
              onClick={() => onSelect(game.game_id)}
            >
              <div className="history-item-top">
                <span className="history-prompt">{game.prompt}</span>
                <StatusBadge status={game.status} />
              </div>
              <div className="history-item-meta">
                <span>{game.genre ?? "장르 미정"}</span>
                <span>{game.repo_name ?? "저장소 없음"}</span>
                <span>{new Date(game.updated_at).toLocaleString()}</span>
              </div>
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}
