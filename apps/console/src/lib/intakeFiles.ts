export const MAX_FILES = 50;
export const MAX_FILE_BYTES = 25 * 1024 * 1024;
export const MAX_TOTAL_BYTES = 100 * 1024 * 1024;
export type IntakeFile = { id: string; source_label: string; size: number; status: "reading" | "read" | "skipped"; note?: string; evidence_id?: string };
export type NamedFile = { file: File; name: string };

export function fileLimit(file: { size: number }, count: number, total: number): string | undefined {
  if (count >= MAX_FILES) return "Up to 50 files. Remove some, or add another batch later.";
  if (file.size > MAX_FILE_BYTES) return "Over 25 MB. Choose a smaller file or paste its key sections.";
  if (total + file.size > MAX_TOTAL_BYTES) return "Over 100 MB total. Remove some files or choose a smaller batch.";
}

type Entry = { isFile: boolean; isDirectory: boolean; fullPath: string; file: (ok: (file: File) => void, fail: (error: DOMException) => void) => void;
  createReader: () => { readEntries: (ok: (entries: Entry[]) => void, fail: (error: DOMException) => void) => void } };

export async function droppedFiles(transfer: DataTransfer): Promise<{ files: NamedFile[]; note: string }> {
  const files: NamedFile[] = [];
  let note = "";
  let entriesRead = 0;
  async function walk(entry: Entry, depth = 0): Promise<void> {
    if (++entriesRead > 1000 || depth > 32) { note = "This folder is too deeply nested or has too many entries. Select a smaller subfolder instead."; return; }
    if (files.length >= MAX_FILES) { note = "Only the first 50 files were added. Add the remaining files in another batch later."; return; }
    if (entry.isFile) {
      try {
        const file = await new Promise<File>((resolve, reject) => entry.file(resolve, reject));
        files.push({ file, name: entry.fullPath.replace(/^\//, "") || file.name });
      } catch { note = "Some files could not be read. Select them with Attach files or paste their text."; }
    } else if (entry.isDirectory) {
      const reader = entry.createReader();
      for (;;) {
        const batch = await new Promise<Entry[]>((resolve, reject) => reader.readEntries(resolve, reject));
        if (!batch.length) break;
        for (const child of batch) { await walk(child, depth + 1); if (entriesRead > 1000) return; if (files.length >= MAX_FILES) { note = "Folder limit reached (50 files). Add remaining files in another batch later."; return; } }
      }
    }
  }
  try {
    const entries = Array.from(transfer.items).filter(item => item.kind === "file").map(item => item.webkitGetAsEntry?.() as unknown as Entry | null);
    if (entries.some(Boolean)) { for (const entry of entries) if (entry) await walk(entry); }
    else files.push(...Array.from(transfer.files).map(file => ({ file, name: file.name })));
  } catch { note = "This folder could not be read. Use Attach folder or select its files instead."; }
  return { files, note };
}
