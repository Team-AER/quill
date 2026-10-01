import { Check, Copy, MessageSquareText, Share } from "lucide-react";
import { useState } from "react";
import type { ShareableLink } from "../api/types";
import { copyText } from "../lib/copy";
import { timeLeft } from "../lib/people";
import { formatDate } from "../lib/time";

interface Props {
  link: ShareableLink;
  /** The ready-to-paste note that goes with the link. */
  message: string;
  title: string;
}

const canShare = typeof navigator !== "undefined" && typeof navigator.share === "function";

/** A one-time link to hand over by chat: copy the link, copy a note with it, or share. */
export function ShareLink({ link, message, title }: Props) {
  const [copied, setCopied] = useState<"link" | "message" | null>(null);
  const copy = async (what: "link" | "message") => {
    await copyText(what === "link" ? link.url : message, what === "link" ? "Link copied" : "Message copied");
    setCopied(what);
  };
  return (
    <div className="share-link">
      <input
        className="share-url"
        readOnly
        value={link.url}
        aria-label="Link"
        onFocus={(e) => e.currentTarget.select()}
        onClick={(e) => e.currentTarget.select()}
      />
      <div className="share-actions">
        <button type="button" className="btn primary" onClick={() => void copy("link")} autoFocus>
          {copied === "link" ? <Check size={14} /> : <Copy size={14} />} {copied === "link" ? "Copied" : "Copy link"}
        </button>
        <button type="button" className="btn" onClick={() => void copy("message")} title="A short note with the link, for chat">
          {copied === "message" ? <Check size={14} /> : <MessageSquareText size={14} />} {copied === "message" ? "Copied" : "Copy message"}
        </button>
        {canShare && (
          <button
            type="button"
            className="btn"
            onClick={() => void navigator.share({ title, text: message }).catch(() => {})}
          >
            <Share size={14} /> Share…
          </button>
        )}
      </div>
      <p className="share-note">
        Works once{link.email ? ` for ${link.email}` : ", for whoever opens it first"}. Expires {timeLeft(link.expires_at) || formatDate(link.expires_at)}. Quill keeps
        only a fingerprint, so this is the only time the link is shown.
      </p>
    </div>
  );
}
