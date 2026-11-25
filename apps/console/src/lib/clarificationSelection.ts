import type { ClarifyAnswer, ClarifyQuestion } from "./api-types";

export function selectedIds(answer?: ClarifyAnswer): string[] {
  return answer?.option_ids || (answer?.option_id && answer.option_id !== "other" ? [answer.option_id] : []);
}

export function clarificationAnswered(q: ClarifyQuestion, answer?: ClarifyAnswer): boolean {
  if (!answer) return false;
  const ids = selectedIds(answer);
  const other = (answer?.other_text || "").trim();
  const validIds = new Set((q.options || []).map((option) => option.id));
  if (ids.length !== new Set(ids).size || ids.some((id) => !validIds.has(id))) return false;
  if (answer.other_text && (q.allow_other === false || other.length < 3)) return false;
  if (q.selection_mode !== "multiple") {
    if (answer.option_ids?.length) return false;
    if (answer.option_id === "other") return q.allow_other !== false && other.length >= 3;
    return ids.length === 1 && !other;
  }
  const exclusive = q.exclusive_option_ids || [];
  if (ids.some((id) => exclusive.includes(id)) && (ids.length !== 1 || other)) return false;
  return ids.length > 0 || other.length >= 3;
}

export function toggleClarificationOption(q: ClarifyQuestion, answer: ClarifyAnswer | undefined, id: string): ClarifyAnswer {
  if (q.selection_mode !== "multiple") return { option_id: id };
  const current = selectedIds(answer);
  const exclusive = q.exclusive_option_ids || [];
  if (exclusive.includes(id)) return { option_ids: current.length === 1 && current[0] === id ? [] : [id] };
  const withoutExclusive = current.filter((item) => !exclusive.includes(item));
  return {
    option_ids: withoutExclusive.includes(id) ? withoutExclusive.filter((item) => item !== id) : [...withoutExclusive, id],
    other_text: answer?.other_text,
  };
}

export function selectAllClarificationOptions(q: ClarifyQuestion): ClarifyAnswer {
  return { option_ids: (q.options || []).map((option) => option.id).filter((id) => !(q.exclusive_option_ids || []).includes(id)) };
}

export function clarificationPayload(q: ClarifyQuestion, answer?: ClarifyAnswer) {
  if (q.selection_mode === "multiple") {
    return { question_id: q.id, slot_key: q.slot_key, option_ids: selectedIds(answer), other_text: answer?.other_text?.trim() || undefined };
  }
  return answer?.option_id === "other"
    ? { question_id: q.id, slot_key: q.slot_key, other_text: answer.other_text?.trim() }
    : { question_id: q.id, slot_key: q.slot_key, option_id: answer?.option_id };
}
