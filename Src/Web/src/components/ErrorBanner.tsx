import type { ExecutionErrorReport } from "../api/types";

/** Surfaces the last QA/compile error report the pipeline recorded for this run. */
export function ErrorBanner({ error }: { error: ExecutionErrorReport }) {
  return (
    <div className="error-banner">
      <strong>
        마지막 오류 [{error.error_type}]
        {error.related_feature_id ? ` · ${error.related_feature_id}` : ""}
      </strong>
      <p>{error.message}</p>
      {error.file && (
        <p className="muted">
          {error.file}
          {error.line != null ? `:${error.line}` : ""}
        </p>
      )}
      {error.suggested_fix && <p className="muted">제안: {error.suggested_fix}</p>}
    </div>
  );
}
