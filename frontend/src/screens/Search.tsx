import { Image, MessageSquareText, Search as SearchIcon } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { api, frameImageUrl } from "../api/client";
import type { SearchResults } from "../api/types";
import { linkClick, navigate } from "../lib/router";
import { formatTimestamp } from "../lib/time";
import { errorText } from "../lib/toast";
import { highlightParts } from "../lib/transcript";

/** Render "[[match]]" markers from the API snippet; fall back to client-side highlighting. */
export function Snippet({ snippet, text, q }: { snippet?: string | null; text?: string | null; q: string }) {
  if (snippet && snippet.includes("[[")) {
    const out: React.ReactNode[] = [];
    const re = /\[\[(.*?)\]\]/g;
    let last = 0;
    let m: RegExpExecArray | null;
    let i = 0;
    while ((m = re.exec(snippet))) {
      if (m.index > last) out.push(<span key={i++}>{snippet.slice(last, m.index)}</span>);
      out.push(<mark key={i++}>{m[1]}</mark>);
      last = m.index + m[0].length;
    }
    if (last < snippet.length) out.push(<span key={i++}>{snippet.slice(last)}</span>);
    return <>{out}</>;
  }
  return (
    <>
      {highlightParts(snippet || text || "", q).map((p, i) => (p.hit ? <mark key={i}>{p.text}</mark> : <span key={i}>{p.text}</span>))}
    </>
  );
}

export function SearchScreen({ initialQ }: { initialQ: string }) {
  const [q, setQ] = useState(initialQ);
  const [res, setRes] = useState<SearchResults | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const input = useRef<HTMLInputElement>(null);

  useEffect(() => {
    input.current?.focus();
  }, []);

  useEffect(() => {
    const query = q.trim();
    const h = window.setTimeout(async () => {
      navigate(query ? `/search?q=${encodeURIComponent(query)}` : "/search", { replace: true });
      if (query.length < 2) {
        setRes(null);
        return;
      }
      setBusy(true);
      try {
        setRes(await api.search(query));
        setErr(null);
      } catch (e) {
        setErr(errorText(e));
      } finally {
        setBusy(false);
      }
    }, 250);
    return () => window.clearTimeout(h);
  }, [q]);

  const groups = useMemo(() => {
    const g = new Map<string, { title: string; lines: SearchResults["transcript"]; frames: SearchResults["frames"] }>();
    const get = (id: string, title?: string | null) => {
      if (!g.has(id)) g.set(id, { title: title || "Meeting", lines: [], frames: [] });
      return g.get(id)!;
    };
    res?.transcript.forEach((h) => get(h.meeting_id, h.meeting_title).lines.push(h));
    res?.frames.forEach((h) => get(h.meeting_id, h.meeting_title).frames.push(h));
    return [...g.entries()];
  }, [res]);

  const total = (res?.transcript.length ?? 0) + (res?.frames.length ?? 0);

  return (
    <div className="page">
      <div className="page-inner" style={{ maxWidth: 860 }}>
        <div className="page-head">
          <h1>Search</h1>
          {res && <span className="sub">{busy ? "Searching…" : `${total} result${total === 1 ? "" : "s"}`}</span>}
        </div>
        <form className="search-field search-hero" role="search" onSubmit={(e) => e.preventDefault()} style={{ marginBottom: 20 }}>
          <SearchIcon size={14} />
          <input ref={input} className="input lg" placeholder="Search every transcript and on-screen text" aria-label="Search all meetings" value={q} onChange={(e) => setQ(e.target.value)} />
        </form>
        {err && (
          <div className="banner failed" role="alert">
            <span className="b-text">Search failed: {err}</span>
          </div>
        )}
        {res && total === 0 && !busy && (
          <div className="empty">
            <SearchIcon size={28} />
            <h3>No results for “{q.trim()}”</h3>
            <p>Search matches whole words and word beginnings in transcripts and frame text.</p>
          </div>
        )}
        <div className="search-groups">
          {groups.map(([id, g]) => (
            <section key={id} className="search-group">
              <h3>
                <a href={`/m/${id}`} onClick={linkClick}>
                  {g.title}
                </a>
                <span className="muted" style={{ fontWeight: 400, fontSize: 12 }}>
                  {g.lines.length + g.frames.length} hits
                </span>
              </h3>
              {g.lines.map((h, i) => (
                <a key={`l${h.line_id ?? i}`} className="hit-row" href={`/m/${id}?t=${Math.floor(h.start)}`} onClick={linkClick}>
                  <span className="t num">
                    <MessageSquareText size={11} /> {formatTimestamp(h.start)}
                  </span>
                  <span>
                    {h.speaker_name && <span className="who">{h.speaker_name}: </span>}
                    <Snippet snippet={h.snippet} text={h.text} q={q} />
                  </span>
                </a>
              ))}
              {g.frames.map((h) => (
                <a key={`f${h.frame_id}`} className="hit-row" href={`/m/${id}?t=${Math.floor(h.t)}`} onClick={linkClick}>
                  <img src={h.thumb_url || frameImageUrl(h.frame_id, true)} alt="" loading="lazy" />
                  <span>
                    <span className="t num">
                      <Image size={11} /> {formatTimestamp(h.t)}
                    </span>{" "}
                    {h.title && <span className="who">{h.title}: </span>}
                    <Snippet snippet={h.snippet} text={h.caption || h.visible_text} q={q} />
                  </span>
                </a>
              ))}
            </section>
          ))}
        </div>
        {!res && !q && (
          <div className="empty">
            <SearchIcon size={28} />
            <h3>Search across meetings</h3>
            <p>Find who said what, or text that appeared on a shared screen.</p>
          </div>
        )}
      </div>
    </div>
  );
}
