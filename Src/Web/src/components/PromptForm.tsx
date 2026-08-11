import { useState } from "react";
import type { FormEvent } from "react";

interface PromptFormProps {
  title: string;
  placeholder: string;
  submitLabel: string;
  busyLabel: string;
  onSubmit: (prompt: string) => Promise<void>;
  onCancel?: () => void;
}

/** Requirement #1: the prompt input, reused for both "new game" and "revise". */
export function PromptForm({
  title,
  placeholder,
  submitLabel,
  busyLabel,
  onSubmit,
  onCancel,
}: PromptFormProps) {
  const [prompt, setPrompt] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault();
    if (!prompt.trim() || busy) return;
    setBusy(true);
    setError(null);
    try {
      await onSubmit(prompt.trim());
      setPrompt("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "요청에 실패했습니다.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <form className="prompt-form" onSubmit={handleSubmit}>
      <h3>{title}</h3>
      <textarea
        value={prompt}
        onChange={(e) => setPrompt(e.target.value)}
        placeholder={placeholder}
        rows={3}
        disabled={busy}
      />
      {error && <p className="form-error">{error}</p>}
      <div className="prompt-form-actions">
        <button type="submit" disabled={busy || !prompt.trim()}>
          {busy ? busyLabel : submitLabel}
        </button>
        {onCancel && (
          <button type="button" className="secondary" onClick={onCancel} disabled={busy}>
            취소
          </button>
        )}
      </div>
    </form>
  );
}
