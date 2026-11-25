"use client";

import {
  useMemo,
  useId,
  useRef,
  useState,
  type ChangeEvent,
  type KeyboardEvent,
  type MouseEvent,
  type Ref,
  type CSSProperties,
} from "react";
import { EmployeePortrait } from "@/components/office/avatars";
import { COMPOSER_LINE_PX, COMPOSER_PAD_PX, shouldSendOnKey } from "@/lib/chatComposer";
import { filterMentions, mentionQuery, type MentionAgent, type MentionHit } from "./mentions";
import { useComposerAutoGrow } from "./useComposerAutoGrow";
import styles from "./MentionField.module.css";

export function MentionField({
  agents,
  multiline,
  className,
  placeholder,
  disabled,
  ariaLabel,
  inputRef,
  onSubmit,
  value: controlledValue,
  onChange: controlledOnChange,
  dockHeight,
  linePx = COMPOSER_LINE_PX,
  padPx = COMPOSER_PAD_PX,
}: {
  agents: MentionAgent[];
  multiline?: boolean;
  className?: string;
  placeholder?: string;
  disabled?: boolean;
  ariaLabel?: string;
  inputRef?: Ref<HTMLInputElement | HTMLTextAreaElement | null>;
  onSubmit: (body: string) => void;
  value?: string;
  onChange?: (value: string) => void;
  dockHeight?: number;
  linePx?: number;
  padPx?: number;
}) {
  const uncontrolled = useRef("");
  const [inner, setInner] = useState("");
  const isControlled = controlledValue !== undefined;
  const value = isControlled ? controlledValue : inner;
  const localRef = useRef<HTMLInputElement | HTMLTextAreaElement | null>(null);
  const [caret, setCaret] = useState(0);
  const [active, setActive] = useState(0);
  const [dismissedQuery, setDismissedQuery] = useState<string | null>(null);
  const listId = useId();
  const grow = useComposerAutoGrow(multiline ? value : "", dockHeight, linePx, padPx);

  function setValue(next: string) {
    if (isControlled) controlledOnChange?.(next);
    else {
      setInner(next);
      uncontrolled.current = next;
    }
  }

  function bindRef(node: HTMLInputElement | HTMLTextAreaElement | null) {
    localRef.current = node;
    if (multiline) (grow.ref as { current: HTMLTextAreaElement | null }).current = node as HTMLTextAreaElement | null;
    if (!inputRef) return;
    if (typeof inputRef === "function") inputRef(node);
    else (inputRef as { current: typeof node }).current = node;
  }

  const candidate = mentionQuery(value, caret);
  const queryKey = candidate ? `${candidate.start}:${candidate.query}` : null;
  const query = queryKey === dismissedQuery ? null : candidate;
  const hits = useMemo(
    () => (query ? filterMentions(agents, query.query) : []),
    [agents, query],
  );

  function applyHit(hit: MentionHit) {
    if (!query) return;
    const next = `${value.slice(0, query.start)}${hit.insert} ${value.slice(caret)}`;
    setValue(next);
    const pos = query.start + hit.insert.length + 1;
    setCaret(pos);
    requestAnimationFrame(() => {
      const el = localRef.current;
      if (!el) return;
      el.focus();
      el.setSelectionRange(pos, pos);
      if (multiline) grow.sync();
    });
  }

  function onKeyDown(event: KeyboardEvent<HTMLInputElement | HTMLTextAreaElement>) {
    if (event.nativeEvent.isComposing || event.keyCode === 229) return;
    if (hits.length > 0) {
      if (event.key === "ArrowDown") {
        event.preventDefault();
        setActive(activeIndex => (activeIndex + 1) % hits.length);
        return;
      }
      if (event.key === "ArrowUp") {
        event.preventDefault();
        setActive(activeIndex => (activeIndex - 1 + hits.length) % hits.length);
        return;
      }
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        applyHit(hits[active] || hits[0]);
        return;
      }
      if (event.key === "Escape") {
        event.preventDefault();
        setDismissedQuery(queryKey);
        return;
      }
    }
    if (shouldSendOnKey(event)) {
      event.preventDefault();
      const body = value.trim();
      if (!body || disabled) return;
      onSubmit(body);
    }
  }

  // Height/overflow are owned by useComposerAutoGrow via the DOM ref — do not
  // put height in React style or each render will wipe the grown size.
  const growStyle: CSSProperties | undefined = multiline
    ? { lineHeight: `${linePx}px`, minHeight: linePx + padPx }
    : undefined;

  const shared = {
    ref: bindRef,
    className,
    placeholder,
    disabled,
    value,
    "aria-label": ariaLabel,
    "aria-autocomplete": "list" as const,
    role: "combobox",
    "aria-haspopup": "listbox" as const,
    "aria-controls": hits.length ? listId : undefined,
    "aria-expanded": hits.length > 0,
    "aria-activedescendant": hits.length ? `${listId}-${Math.min(active, hits.length - 1)}` : undefined,
    autoComplete: "off",
    spellCheck: false,
    onChange: (event: ChangeEvent<HTMLInputElement | HTMLTextAreaElement>) => {
      setValue(event.target.value);
      setCaret(event.target.selectionStart ?? event.target.value.length);
      setActive(0);
    },
    onKeyDown,
    onBlur: () => setDismissedQuery(queryKey),
    onClick: (event: MouseEvent<HTMLInputElement | HTMLTextAreaElement>) => {
      setCaret(event.currentTarget.selectionStart ?? value.length);
    },
  };

  return (
    <div className={styles.wrap}>
      {multiline ? (
        <textarea {...shared} rows={1} style={growStyle} />
      ) : (
        <input {...shared} type="text" />
      )}
      {hits.length > 0 && (
        <ul id={listId} className={styles.menu} role="listbox" aria-label="Teammates">
          {hits.map((hit, index) => (
            <li key={hit.id}>
              <button
                type="button"
                role="option"
                id={`${listId}-${index}`}
                tabIndex={-1}
                aria-selected={index === active}
                className={index === active ? styles.optionActive : styles.option}
                onMouseDown={(event) => {
                  event.preventDefault();
                }}
                onClick={() => applyHit(hit)}
              >
                <EmployeePortrait seed={hit.name} size={22} />
                <span>
                  <strong>{hit.insert}</strong>
                  <em>{hit.label}</em>
                </span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
