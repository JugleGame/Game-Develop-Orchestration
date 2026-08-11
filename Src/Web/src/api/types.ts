// Mirrors app/models/api.py and app/models/schemas.py (DeveloperAI backend §6).

export type JobStatus =
  | "planning"
  | "awaiting_approval"
  | "developing"
  | "qa_review"
  | "deploying"
  | "done"
  | "escalated"
  | "cancelled";

// Mirrors app/graph/state.py Stage constants — every node the LangGraph
// pipeline can be sitting in when we poll/stream its status.
export type Stage =
  | "Intake"
  | "ConceptPropose"
  | "ConceptGate"
  | "Planning"
  | "ApprovalGate"
  | "CodeGen"
  | "AssetGen"
  | "BuildPrototype"
  | "CompileCheck"
  | "ErrorCorrection"
  | "RuntimeCheck"
  | "StructureCompare"
  | "FunctionalTest"
  | "AssetReview"
  | "Deployment"
  | "HumanEscalation";

export interface GameDesignDocument {
  game_id: string;
  genre: string;
  core_mechanics: string[];
  art_style: string;
  target_platform: "PC" | "Mobile" | "WebGL";
  structure_overview: string;
  created_at: string;
}

export interface FeatureImplementationPrompt {
  feature_id: string;
  title: string;
  description: string;
  priority: "P0" | "P1" | "P2";
  dependencies: string[];
}

export interface ExecutionErrorReport {
  error_type: "compile" | "runtime" | "logic" | "structure_mismatch";
  message: string;
  file: string | null;
  line: number | null;
  suggested_fix: string;
  related_feature_id: string;
}

export interface GameCreateResponse {
  game_id: string;
  status: JobStatus;
}

export interface GameSummaryResponse {
  game_id: string;
  prompt: string;
  status: JobStatus;
  current_stage: Stage;
  genre: string | null;
  repo_name: string | null;
  iteration_count: number;
  created_at: string;
  updated_at: string;
}

export interface GameStatusResponse {
  game_id: string;
  status: JobStatus;
  current_stage: Stage;
  iteration_count: number;
  game_design: GameDesignDocument | null;
  feature_prompts: FeatureImplementationPrompt[] | null;
  last_error: ExecutionErrorReport | null;
  images_generated: number;
  updated_at: string;
}

export interface ApprovalResponse {
  game_id: string;
  status: JobStatus;
}

export interface ArtifactResponse {
  game_id: string;
  repo_name: string;
  repository_url: string | null;
  commit_hash: string;
  tag: string;
  build_download_url: string | null;
}

export interface CancelResponse {
  game_id: string;
  status: JobStatus;
}

// Shape published on GET /games/{id}/stream (app/utils/events.py JobEvent).
export interface JobEvent {
  game_id: string;
  stage: Stage | string;
  status: JobStatus | "";
  detail: Record<string, unknown>;
}
