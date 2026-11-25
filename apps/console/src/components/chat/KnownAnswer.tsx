"use client";

export type KnownCheck = {
  status: "known" | "confirm" | "conflict" | "missing";
  value: string | string[] | null;
  source: string | string[] | null;
  values?: { value: string | string[]; source: string; source_url?: string }[];
};

export function KnownAnswer({ check, field, disabled, onSelect, onEdit }: {
  check: KnownCheck; field: string; disabled?: boolean;
  onSelect: (value: string) => void; onEdit: () => void;
}) {
  if (check.status === "missing") return null;
  const text = (value: string | string[] | null) => typeof value === "string" ? value : JSON.stringify(value);
  const values = check.values ?? [{ value: check.value ?? "", source: check.source ?? "Your saved answer" }];
  return <section aria-label={`Your saved ${field}`}>
    <p>{check.status === "known" ? `Your ${field} assumption` : check.status === "confirm" ? `Is this your ${field}?` : `Choose your ${field}`}</p>
    {check.status === "conflict" ? <form onSubmit={event => {
      event.preventDefault();
      const value = new FormData(event.currentTarget).get("knownValue");
      if (typeof value === "string") onSelect(value);
    }}><fieldset disabled={disabled}><legend>Choose the value we should use</legend>
      {values.map((value, index) => <label key={`${index}:${value.source}`} style={{ display: "block" }}>
        <input type="radio" name="knownValue" value={text(value.value)} required /> {text(value.value)} <span>({text(value.source)})</span>
      </label>)}
    </fieldset><button type="submit" disabled={disabled}>Use this value</button></form>
      : <p>{text(check.value)} <span>({text(check.source)})</span>
        {check.status === "confirm" && <button type="button" disabled={disabled} onClick={() => onSelect(text(check.value))}>Yes</button>}
      </p>}
    <button type="button" disabled={disabled} onClick={onEdit}>Edit {field}</button>
  </section>;
}
