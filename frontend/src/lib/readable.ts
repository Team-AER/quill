export const UNREADABLE =
  "This file can't be read from disk. It is probably a cloud-only placeholder " +
  "(OneDrive, iCloud Drive, Google Drive). In Finder choose \"Always Keep on This Device\" " +
  "or copy it to a local folder, then drop it again.";

/** Read a byte at the start, middle and end: cloud placeholders (OneDrive "dataless"
 * files) report their full size but fail as soon as the browser reads them. */
export async function probeReadable(file: File): Promise<string | null> {
  if (file.size === 0) return `${file.name} is empty`;
  const offsets = [0, Math.floor(file.size / 2), file.size - 1];
  try {
    for (const at of offsets) await file.slice(at, at + 1).arrayBuffer();
    return null;
  } catch {
    return UNREADABLE;
  }
}
