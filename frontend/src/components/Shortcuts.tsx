import { X } from "lucide-react";
import { useEffect, useRef } from "react";
import { closeShortcuts, useUI } from "../lib/ui";

const GROUPS: { title: string; keys: [string[], string][] }[] = [
  {
    title: "Anywhere",
    keys: [
      [["⌘", "K"], "Search or jump to a meeting"],
      [["U"], "Upload a recording"],
      [["?"], "Show this list"],
    ],
  },
  {
    title: "Playback",
    keys: [
      [["Space"], "Play or pause"],
      [["K"], "Play or pause"],
      [["J"], "Back 10 seconds"],
      [["L"], "Forward 10 seconds"],
      [["←"], "Back 5 seconds"],
      [["→"], "Forward 5 seconds"],
      [["["], "Slower"],
      [["]"], "Faster"],
      [["M"], "Mute"],
    ],
  },
  {
    title: "Meeting",
    keys: [
      [["/"], "Search the transcript"],
      [["F"], "Follow playback in the transcript"],
      [["C"], "Copy a link to the current moment"],
      [["1", "–", "5"], "Switch tabs"],
    ],
  },
];

export function Shortcuts() {
  const { shortcuts } = useUI();
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const d = ref.current;
    if (!d) return;
    if (shortcuts && !d.open) d.showModal();
    if (!shortcuts && d.open) d.close();
  }, [shortcuts]);
  return (
    <dialog
      ref={ref}
      className="sheet-dialog shortcuts"
      aria-labelledby="keys-title"
      onCancel={(e) => (e.preventDefault(), closeShortcuts())}
      onClick={(e) => e.target === e.currentTarget && closeShortcuts()}
    >
      <header className="sd-head">
        <div className="sd-title">
          <h2 id="keys-title">Keyboard shortcuts</h2>
        </div>
        <button type="button" className="icon-btn" aria-label="Close" title="Close (Esc)" onClick={closeShortcuts} autoFocus>
          <X size={16} />
        </button>
      </header>
      <div className="keys">
        {GROUPS.map((g) => (
          <section key={g.title}>
            <h3>{g.title}</h3>
            <dl>
              {g.keys.map(([k, what], i) => (
                <div key={i}>
                  <dt>
                    {k.map((x, j) => (x === "–" ? <span key={j}>–</span> : <kbd key={j}>{x}</kbd>))}
                  </dt>
                  <dd>{what}</dd>
                </div>
              ))}
            </dl>
          </section>
        ))}
      </div>
    </dialog>
  );
}
