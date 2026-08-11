import { useState } from "react";
import { ApiError, createGame } from "./api/client";
import { GameDetail } from "./components/GameDetail";
import { GameHistoryList } from "./components/GameHistoryList";
import { PromptForm } from "./components/PromptForm";
import { useGameList } from "./hooks/useGameList";

export function App() {
  const [selectedGameId, setSelectedGameId] = useState<string | null>(null);
  const [refreshKey, setRefreshKey] = useState(0);
  const { games, loading, error, refresh } = useGameList(refreshKey);

  const handleCreate = async (prompt: string) => {
    try {
      const created = await createGame(prompt);
      setSelectedGameId(created.game_id);
      setRefreshKey((k) => k + 1);
    } catch (e) {
      throw e instanceof ApiError ? new Error(e.detail) : e;
    }
  };

  const handleChanged = () => {
    setRefreshKey((k) => k + 1);
    void refresh();
  };

  return (
    <div className="app">
      <aside className="sidebar">
        <h1>DeveloperAI Console</h1>
        <PromptForm
          title="새 게임 만들기"
          placeholder="예: 더블 점프가 있는 픽셀 아트 플랫포머 게임을 만들어줘"
          submitLabel="게임 생성 요청"
          busyLabel="요청 중..."
          onSubmit={handleCreate}
        />
        <GameHistoryList
          games={games}
          loading={loading}
          error={error}
          selectedGameId={selectedGameId}
          onSelect={setSelectedGameId}
        />
      </aside>
      <main className="main">
        {selectedGameId ? (
          <GameDetail key={selectedGameId} gameId={selectedGameId} onChanged={handleChanged} />
        ) : (
          <div className="empty-state">
            <p>왼쪽에서 새 게임을 만들거나, 히스토리에서 기존 게임을 선택하세요.</p>
          </div>
        )}
      </main>
    </div>
  );
}
