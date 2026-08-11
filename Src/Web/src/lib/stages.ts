// Pipeline/module metadata mirroring app/graph/graph.py + app/graph/state.py Stage,
// used to render "which AI module is running right now" (requirement #2).
import type { JobStatus, Stage } from "../api/types";

export interface StageMeta {
  stage: Stage;
  label: string;
  module: string;
}

// Linear happy-path order (matches the START->...->Deployment edges in graph.py).
// ErrorCorrection/HumanEscalation are side states, handled separately below.
export const PIPELINE_STAGES: StageMeta[] = [
  { stage: "Intake", label: "접수", module: "Orchestrator" },
  { stage: "ConceptPropose", label: "아이디어 근거 조회", module: "StrategicMcpServer" },
  { stage: "ConceptGate", label: "아이디어 승인 대기", module: "사용자 승인" },
  { stage: "Planning", label: "기획 생성", module: "StrategicMcpServer" },
  { stage: "ApprovalGate", label: "기획 승인 대기", module: "사용자 승인" },
  { stage: "CodeGen", label: "코드 생성", module: "UnityMcpServer (Unity Assistance)" },
  { stage: "AssetGen", label: "에셋 생성", module: "AssetGenMcpServer" },
  { stage: "BuildPrototype", label: "프로토타입 빌드", module: "UnityMcpServer (Unity Assistance)" },
  { stage: "CompileCheck", label: "컴파일 확인", module: "UnityMcpServer (Unity Assistance)" },
  { stage: "RuntimeCheck", label: "플레이모드 실행 확인", module: "UnityMcpServer (Unity Assistance)" },
  { stage: "StructureCompare", label: "구조 비교", module: "QaMcpServer" },
  { stage: "FunctionalTest", label: "기능 테스트", module: "QaMcpServer" },
  { stage: "AssetReview", label: "에셋 검수 확인", module: "AssetGenMcpServer" },
  { stage: "Deployment", label: "Git 배포", module: "GitMcpServer" },
];

export const SIDE_STAGE_META: Record<string, StageMeta> = {
  ErrorCorrection: { stage: "ErrorCorrection", label: "에러 수정 재시도", module: "Orchestrator" },
  HumanEscalation: { stage: "HumanEscalation", label: "사람 개입 필요", module: "운영자" },
};

const STAGE_ORDER = PIPELINE_STAGES.map((s) => s.stage);

export type StepState = "done" | "current" | "pending" | "error";

export function stepState(stepStage: Stage, currentStage: Stage, status: JobStatus): StepState {
  // ErrorCorrection is a retry loop back into CodeGen; visually treat CodeGen
  // as "current" while it runs instead of introducing a dead-end column.
  const effectiveCurrent: Stage = currentStage === "ErrorCorrection" ? "CodeGen" : currentStage;
  const currentIdx = STAGE_ORDER.indexOf(effectiveCurrent);
  const stepIdx = STAGE_ORDER.indexOf(stepStage);
  if (currentIdx === -1 || stepIdx === -1) return "pending";

  if (status === "done") return stepIdx <= currentIdx ? "done" : "pending";

  if (status === "escalated" || status === "cancelled") {
    if (stepIdx < currentIdx) return "done";
    if (stepIdx === currentIdx) return "error";
    return "pending";
  }

  if (stepIdx < currentIdx) return "done";
  if (stepIdx === currentIdx) return "current";
  return "pending";
}

export function stageMeta(stage: Stage): StageMeta {
  return (
    PIPELINE_STAGES.find((s) => s.stage === stage) ??
    SIDE_STAGE_META[stage] ?? { stage, label: stage, module: "-" }
  );
}

export const JOB_STATUS_LABEL: Record<JobStatus, string> = {
  planning: "기획 중",
  awaiting_approval: "승인 대기",
  developing: "개발 중",
  qa_review: "QA 검증 중",
  deploying: "배포 중",
  done: "완료",
  escalated: "운영자 확인 필요",
  cancelled: "취소됨",
};
