"""Convert structured JSON to Markdown format."""

from .pages import PAGE_MARK, paragraphs_with_pages


class MarkdownConverter:
    """Convert StructuredText dict to Markdown."""

    def convert(self, data: dict) -> str:
        """Convert structured text data to Markdown string.

        Args:
            data: StructuredText dict (as returned by StructuredText.to_dict())

        Returns:
            Markdown formatted string
        """
        lines = []

        # Title
        title = data.get("title", "")
        if title:
            lines.append(f"# {title}")
            lines.append("")

        # Author info from metadata
        meta = data.get("metadata", {})
        authors = meta.get("authors", [])
        if authors:
            parts = []
            for a in authors:
                dynasty = a.get("dynasty", "")
                name = a.get("name", "")
                role = a.get("role", "")
                if dynasty:
                    parts.append(f"[{dynasty}] {name} {role}".strip())
                else:
                    parts.append(f"{name} {role}".strip())
            lines.append(f"> {', '.join(parts)}")
            lines.append("")

        # Chapters
        chapters = data.get("chapters", [])
        for ch in chapters:
            ch_title = ch.get("title", "")
            if ch_title:
                lines.append(f"## {ch_title}")
                lines.append("")

            headings = set(ch.get("headings", []))
            for i, (pages, text) in enumerate(paragraphs_with_pages(ch)):
                if pages:
                    lines.append(" ".join(f"*{PAGE_MARK.format(n)}*" for n in pages))
                    lines.append("")
                if text:
                    lines.append(f"### {text}" if i in headings else text)
                    lines.append("")

        return "\n".join(lines)
