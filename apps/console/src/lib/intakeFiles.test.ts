import assert from "node:assert/strict";
import test from "node:test";
import { droppedFiles, fileLimit, MAX_FILES, MAX_FILE_BYTES, MAX_TOTAL_BYTES } from "./intakeFiles.ts";

test("intake limits leave a smaller-file/remove/batch action", () => {
  assert.equal(fileLimit({size: MAX_FILE_BYTES}, 49, MAX_TOTAL_BYTES - MAX_FILE_BYTES), undefined);
  assert.match(fileLimit({size: 1}, MAX_FILES, 0)!, /Remove/);
  assert.match(fileLimit({size: MAX_FILE_BYTES + 1}, 0, 0)!, /smaller/);
  assert.match(fileLimit({size: 1}, 0, MAX_TOTAL_BYTES)!, /Remove/);
});

function fileEntry(name: string) {
  return {isFile: true, isDirectory: false, fullPath: name, file: (done: (file: File) => void) => done(new File([name], name.split("/").at(-1)!))};
}
function directory(path: string, batches: unknown[][]) {
  return {isFile: false, isDirectory: true, fullPath: path, createReader: () => {
    let index = 0;
    return {readEntries: (done: (entries: unknown[]) => void) => done(batches[index++] || [])};
  }};
}
function transfer(entries: unknown[]) {
  return {items: entries.map(entry => ({kind: "file", webkitGetAsEntry: () => entry})), files: []} as unknown as DataTransfer;
}

test("folder drop reads every directory batch and preserves nested source paths", async () => {
  const result = await droppedFiles(transfer([directory("/brief", [[fileEntry("/brief/idea.md")],
    [directory("/brief/src", [[fileEntry("/brief/src/app.py")]])], []]), fileEntry("/budget.csv")]));
  assert.deepEqual(result.files.map(item => item.name), ["brief/idea.md", "brief/src/app.py", "budget.csv"]);
  assert.equal(result.note, "");
});

test("folder file ceiling reports remaining files instead of hanging", async () => {
  const result = await droppedFiles(transfer([directory("/folder", [Array.from({length: 75}, (_, index) => fileEntry(`/folder/${index}.txt`))])]));
  assert.equal(result.files.length, 50);
  assert.match(result.note, /remaining|batch/);
});

test("ordinary multi-file drop still works without directory entries", async () => {
  const files = [new File(["notes"], "notes.txt"), new File(["code"], "app.py")];
  const result = await droppedFiles({items: [], files} as unknown as DataTransfer);
  assert.deepEqual(result.files.map(item => item.name), ["notes.txt", "app.py"]);
});
