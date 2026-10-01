"use client";

import { useState } from "react";
import { apiFetch } from "../lib/api";
import { useAuth } from "../lib/auth";
import { useToast } from "../lib/toast";

// Section ids come from the agent manifest's feedback_sections
// (blueprint B9) — the chassis declares no vocabulary of its own.
export default function FeedbackControls({
  runId,
  sectionType,
  citationId,
}: {
  runId: string;
  sectionType: string;
  citationId?: number;
}) {
  const { token } = useAuth();
  const { toast } = useToast();
  const [rating, setRating] = useState<"positive" | "negative" | null>(null);
  const [comment, setComment] = useState("");
  const [showComment, setShowComment] = useState(false);

  async function submit(r: "positive" | "negative") {
    setRating(r);
    if (r === "negative") setShowComment(true);
    try {
      await apiFetch("/feedback", token, {
        method: "POST",
        body: JSON.stringify({
          run_id: runId,
          section_type: sectionType,
          citation_id: citationId ?? null,
          rating: r,
          comment: comment || null,
        }),
      });
      toast("Feedback saved", "success");
    } catch {
      toast("Feedback save failed", "error");
    }
  }

  return (
    <div className="flex flex-col items-end gap-1">
      <div className="flex gap-1">
        <button
          onClick={() => submit("positive")}
          className={`rounded border px-2 py-1 text-xs ${rating === "positive" ? "bg-green-100" : ""}`}
        >
          👍
        </button>
        <button
          onClick={() => submit("negative")}
          className={`rounded border px-2 py-1 text-xs ${rating === "negative" ? "bg-red-100" : ""}`}
        >
          👎
        </button>
      </div>
      {showComment && (
        <textarea
          rows={2}
          value={comment}
          onChange={(e) => setComment(e.target.value)}
          onBlur={() => submit("negative")}
          placeholder="Optional comment…"
          className="w-48 rounded border px-2 py-1 text-xs"
        />
      )}
    </div>
  );
}
