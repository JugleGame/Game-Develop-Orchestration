import { JOB_STATUS_LABEL } from "../lib/stages";
import type { JobStatus } from "../api/types";

const STATUS_CLASS: Record<JobStatus, string> = {
  planning: "badge badge-info",
  awaiting_approval: "badge badge-warn",
  developing: "badge badge-info",
  qa_review: "badge badge-info",
  deploying: "badge badge-info",
  done: "badge badge-ok",
  escalated: "badge badge-error",
  cancelled: "badge badge-muted",
};

export function StatusBadge({ status }: { status: JobStatus }) {
  return <span className={STATUS_CLASS[status]}>{JOB_STATUS_LABEL[status]}</span>;
}
