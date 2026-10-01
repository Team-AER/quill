export interface Group<T> {
  key: string;
  label: string;
  items: T[];
}

const monthFmt = new Intl.DateTimeFormat(undefined, { month: "long" });
const monthYearFmt = new Intl.DateTimeFormat(undefined, { month: "long", year: "numeric" });

/** Today / Yesterday / Earlier this week / <Month> / <Month Year>, newest first, order kept inside a group. */
export function groupByDay<T>(items: T[], dateOf: (t: T) => string | null | undefined, now = new Date()): Group<T>[] {
  const day = (d: Date) => new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
  const today = day(now);
  const out: Group<T>[] = [];
  const at = new Map<string, Group<T>>();
  for (const it of items) {
    const raw = dateOf(it);
    const d = raw ? new Date(raw) : null;
    let key: string;
    let label: string;
    if (!d || Number.isNaN(d.getTime())) {
      key = "undated";
      label = "Undated";
    } else {
      const ago = Math.round((today - day(d)) / 86400000);
      if (ago <= 0) [key, label] = ["today", "Today"];
      else if (ago === 1) [key, label] = ["yesterday", "Yesterday"];
      else if (ago < 7) [key, label] = ["week", "Earlier this week"];
      else {
        key = `${d.getFullYear()}-${d.getMonth()}`;
        label = d.getFullYear() === now.getFullYear() ? monthFmt.format(d) : monthYearFmt.format(d);
      }
    }
    let g = at.get(key);
    if (!g) {
      g = { key, label, items: [] };
      at.set(key, g);
      out.push(g);
    }
    g.items.push(it);
  }
  return out;
}
