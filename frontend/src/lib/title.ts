/**
 * Meeting title from a recording's file name: drops the extension, recorder
 * boilerplate (Teams "Meeting Recording", Zoom "GMT…_Recording") and date/time
 * stamps, and turns slugs into words.
 * "Discussion with Sales Team-20230208_145424-Meeting Recording.mp4" -> "Discussion with Sales Team"
 */
export function titleFromFilename(name: string): string {
  const base = name.replace(/\.[a-z0-9]{2,5}$/i, "").trim();
  let s = base.replace(/_/g, " ");
  s = s
    .replace(/^GMT\d{8}-\d{6}\s*/i, "")
    .replace(/\b\d{3,4}x\d{3,4}\b/g, "")
    .replace(/[\s-]*meeting[\s-]*recording\b/gi, "")
    .replace(/[\s-]*\brecording(\s\d+)?$/i, "")
    .replace(/\b(19|20)\d{2}-?(0[1-9]|1[0-2])-?(0[1-9]|[12]\d|3[01])(?:[ T-]?\d{2}[:.-]?\d{2}(?:[:.-]?\d{2})?)?\b/g, "");
  // A slug ("infra-weekly", "q4.roadmap") has no spaces of its own.
  if (!/\s/.test(base.replace(/_/g, " ").replace(/^\S*\d{8}\S*$/, ""))) s = s.replace(/[-.]+/g, " ");
  s = s
    .replace(/(\s*[-–—]\s*){2,}/g, " – ")
    .replace(/\s+/g, " ")
    .replace(/^[\s\-–—.,:;]+|[\s\-–—.,:;]+$/g, "")
    .trim();
  if (s.length < 2) return base || name;
  return /^[a-z]/.test(s) ? s[0].toUpperCase() + s.slice(1) : s;
}
