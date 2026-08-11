import { PIPELINE_STAGES, SIDE_STAGE_META, stepState } from "../lib/stages";
import type { GameStatusResponse, JobEvent } from "../api/types";

const STEP_ICON: Record<ReturnType<typeof stepState>, string> = {
  done: "✓",
  current: "●",
  pending: "○",
  error: "✕",
};

interface PipelineStatusProps {
  status: GameStatusResponse;
  log: JobEvent[];
}

/**
 * Requirement #2: shows which AI module is currently running and lets the
 * user see every module's state at a glance (multiple modules can be
 * involved across a run, even though only one LangGraph node executes at a
 * time) plus a live transition log fed by the SSE stream.
 */
export function PipelineStatus({ status, log }: PipelineStatusProps) {
  const isRetrying = status.current_stage === "ErrorCorrection";
  const isEscalated = status.status === "escalated";

  return (
    <div className="pipeline">
      <ol className="pipeline-steps">
        {PIPELINE_STAGES.map((meta) => {
          const state = stepState(meta.stage, status.current_stage, status.status);
          return (
            <li key={meta.stage} className={`pipeline-step step-${state}`}>
              <span className="step-icon">{STEP_ICON[state]}</span>
              <div className="step-body">
                <span className="step-label">{meta.label}</span>
                <span className="step-module">{meta.module}</span>
              </div>
            </li>
          );
        })}
      </ol>

      {isRetrying && (
        <p className="pipeline-note warn">
          {SIDE_STAGE_META.ErrorCorrection.label} 중 (재시도 {status.iteration_count}회) —
          코드 생성 단계로 되돌아가 다시 시도합니다.
        </p>
      )}
      {isEscalated && (
        <p className="pipeline-note error">
          {SIDE_STAGE_META.HumanEscalation.label}: 자동 재시도 한도를 초과해 운영자 확인이
          필요합니다.
        </p>
      )}

      {status.images_generated > 0 && (
        <p className="pipeline-note muted">
          PixelLab 이미지 {status.images_generated}장 소진 (월 할당량에서 차감).
        </p>
      )}

      <div className="pipeline-log">
        <h4>진행 로그</h4>
        {log.length === 0 ? (
          <p className="muted">아직 수신된 이벤트가 없습니다.</p>
        ) : (
          <ul>
            {[...log].reverse().map((event, idx) => (
              <li key={`${event.stage}-${idx}`}>
                <span className="log-stage">{event.stage}</span>
                <span className="log-status">{event.status || "-"}</span>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}
