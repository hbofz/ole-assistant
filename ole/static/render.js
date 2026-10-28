const esc = (s) => s.replace(/[&<>"']/g, (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));

// Tiny markdown: paragraphs, bullet/numbered lists, **bold**, *italic*, [links](url), bare URLs.
// Citation markers arrive as \u0001n\u0001 and become numbered links.
function render(text, sources) {
  const inline = (s) => esc(s)
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^*])\*([^*\s][^*]*?)\*/g, "$1<em>$2</em>")
    .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>')
    .replace(/(^|[\s(])(https?:\/\/[^\s<)]+[^\s<).,;:!?])/g, '$1<a href="$2" target="_blank" rel="noopener">$2</a>')
    .replace(/(^|[\s(])((?:[a-z]+\.)?stolaf\.edu(?:\/[^\s<)]*[^\s<).,;:!?])?)/g, '$1<a href="https://$2" target="_blank" rel="noopener">$2</a>')
    .replace(/\u0001(\d+)\u0001/g, (_, n) => {
      const src = sources[n];
      return src ? `<a class="cite" href="${esc(src.url)}" target="_blank" rel="noopener" title="${esc(src.title)}">${n}</a>`
                 : `<span class="cite">${n}</span>`;
    });
  const out = [];
  let list = null;
  // a citation that landed on its own line belongs to the line before it
  text = text.replace(/\n+((?:\u0001\d+\u0001)+)/g, "$1");
  for (const raw of text.split("\n")) {
    const line = raw.trimEnd();
    const m = line.match(/^\s*(?:([-*•])|(\d+)[.)])\s+(.*)$/);
    if (m) {
      const tag = m[1] ? "ul" : "ol";
      if (!list || list.tag !== tag) { if (list) out.push(`</${list.tag}>`); list = {tag}; out.push(`<${tag}>`); }
      out.push(`<li>${inline(m[3])}</li>`);
      continue;
    }
    if (list) { out.push(`</${list.tag}>`); list = null; }
    if (line.trim()) out.push(`<p>${inline(line.replace(/^#+\s*/, ""))}</p>`);
  }
  if (list) out.push(`</${list.tag}>`);
  return out.join("");
}

function hostPath(url) {
  try { const u = new URL(url); return (u.host + u.pathname).replace(/\/$/, ""); } catch { return url; }
}

if (typeof module !== "undefined") module.exports = {render, esc, hostPath};
