import { approveGame, cancelGame } from "../api/client";
import { useGameDetail } from "../hooks/useGameDetail";
import { ApprovalPanel } from "./ApprovalPanel";
import { ArtifactPanel } from "./ArtifactPanel";
import { ErrorBanner } from "./ErrorBanner";
import { PipelineStatus } from "./PipelineStatus";
import { StatusBadge } from "./StatusBadge";

interface GameDetailProps {
  gameId: string;
  onChanged: () => void;
}

export function GameDetail({ gameId, onChanged }: GameDetailProps) {
  const { status, log, loading, error, isTerminal, refresh } = useGameDetail(gameId);

  if (loading && !status) return <p className="muted">불러오는 중...</p>;
  if (error && !status) return <p className="form-error">{error}</p>;
  if (!status) return null;

  const handleApprove = async (approved: boolean, feedback: string | undefined) => {
    await approveGame(gameId, approved, feedback);
    await refresh();
  };

  const handleCancel = async () => {
    await cancelGame(gameId);
    await refresh();
    onChanged();
  };

  return (
    <div className="game-detail">
      <div className="game-detail-header">
        <h2>게임 #{gameId.slice(0, 8)}</h2>
        <StatusBadge status={status.status} />
        {!isTerminal && (
          <button type="button" className="secondary" onClick={handleCancel}>
            취소
          </button>
        )}
      </div>

      <PipelineStatus status={status} log={log} />

      {status.last_error && <ErrorBanner error={status.last_error} />}

      {status.status === "awaiting_approval" && (
        <ApprovalPanel
          gameDesign={status.game_design}
          featurePrompts={status.feature_prompts}
          onDecision={handleApprove}
        />
      )}

      {status.status === "done" && (
        <ArtifactPanel
          gameId={gameId}
          onRevised={() => {
            void refresh();
            onChanged();
          }}
        />
      )}
    </div>
  );
}
