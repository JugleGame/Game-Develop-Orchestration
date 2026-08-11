import { useEffect, useState } from "react";
import { ApiError, getGameArtifact, reviseGame } from "../api/client";
import type { ArtifactResponse } from "../api/types";
import { PromptForm } from "./PromptForm";

interface ArtifactPanelProps {
  gameId: string;
  onRevised: () => void;
}

/** Shown when status === "done": artifact info + requirement #4 (edit/revise). */
export function ArtifactPanel({ gameId, onRevised }: ArtifactPanelProps) {
  const [artifact, setArtifact] = useState<ArtifactResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [showRevise, setShowRevise] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setArtifact(null);
    setError(null);
    getGameArtifact(gameId)
      .then((data) => {
        if (!cancelled) setArtifact(data);
      })
      .catch((e) => {
        if (!cancelled) setError(e instanceof ApiError ? e.detail : "결과를 불러오지 못했습니다.");
      });
    return () => {
      cancelled = true;
    };
  }, [gameId]);

  return (
    <div className="artifact-panel">
      <h3>배포 결과</h3>
      {error && <p className="form-error">{error}</p>}
      {artifact && (
        <dl className="design-summary">
          <dt>저장소</dt>
          <dd>{artifact.repo_name}</dd>
          <dt>브랜치</dt>
          <dd>main</dd>
          <dt>커밋</dt>
          <dd>{artifact.commit_hash}</dd>
          <dt>태그</dt>
          <dd>{artifact.tag}</dd>
          {artifact.repository_url && (
            <>
              <dt>URL</dt>
              <dd>
                <a href={artifact.repository_url} target="_blank" rel="noreferrer">
                  {artifact.repository_url}
                </a>
              </dd>
            </>
          )}
        </dl>
      )}

      {!showRevise ? (
        <button type="button" onClick={() => setShowRevise(true)}>
          이 게임 수정하기
        </button>
      ) : (
        <PromptForm
          title="수정 요청"
          placeholder="예: 더블 점프 능력을 추가해줘"
          submitLabel="수정 요청 보내기"
          busyLabel="요청 중..."
          onCancel={() => setShowRevise(false)}
          onSubmit={async (prompt) => {
            await reviseGame(gameId, prompt);
            setShowRevise(false);
            onRevised();
          }}
        />
      )}
    </div>
  );
}
