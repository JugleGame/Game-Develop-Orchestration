import { useState } from "react";
import type { FeatureImplementationPrompt, GameDesignDocument } from "../api/types";

interface ApprovalPanelProps {
  gameDesign: GameDesignDocument | null;
  featurePrompts: FeatureImplementationPrompt[] | null;
  onDecision: (approved: boolean, feedback: string | undefined) => Promise<void>;
}

/** Shown while status === "awaiting_approval" (ApprovalGate interrupt). */
export function ApprovalPanel({ gameDesign, featurePrompts, onDecision }: ApprovalPanelProps) {
  const [feedback, setFeedback] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const decide = async (approved: boolean) => {
    setBusy(true);
    setError(null);
    try {
      await onDecision(approved, feedback.trim() || undefined);
      setFeedback("");
    } catch (e) {
      setError(e instanceof Error ? e.message : "요청에 실패했습니다.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="approval-panel">
      <h3>기획 승인 대기</h3>
      {gameDesign && (
        <dl className="design-summary">
          <dt>장르</dt>
          <dd>{gameDesign.genre}</dd>
          <dt>아트 스타일</dt>
          <dd>{gameDesign.art_style}</dd>
          <dt>플랫폼</dt>
          <dd>{gameDesign.target_platform}</dd>
          <dt>핵심 메커닉</dt>
          <dd>{gameDesign.core_mechanics.join(", ")}</dd>
          <dt>개요</dt>
          <dd>{gameDesign.structure_overview}</dd>
        </dl>
      )}
      {featurePrompts && featurePrompts.length > 0 && (
        <>
          <h4>구현될 기능</h4>
          <ul className="feature-list">
            {featurePrompts.map((fp) => (
              <li key={fp.feature_id}>
                <span className="feature-priority">{fp.priority}</span>
                <strong>{fp.title}</strong>
                <p>{fp.description}</p>
              </li>
            ))}
          </ul>
        </>
      )}
      <textarea
        placeholder="재기획 요청 시 피드백 (선택)"
        value={feedback}
        onChange={(e) => setFeedback(e.target.value)}
        rows={2}
        disabled={busy}
      />
      {error && <p className="form-error">{error}</p>}
      <div className="approval-actions">
        <button type="button" onClick={() => decide(true)} disabled={busy}>
          승인하고 개발 시작
        </button>
        <button type="button" className="secondary" onClick={() => decide(false)} disabled={busy}>
          재기획 요청
        </button>
      </div>
    </div>
  );
}
