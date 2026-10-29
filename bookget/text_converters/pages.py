"""Page markers for converters, from a chapter's optional "page_starts"."""

PAGE_MARK = "〔第{}页〕"


def paragraphs_with_pages(chapter: dict):
    """Yield (marks_before, text) per paragraph, with mid-paragraph page
    markers inserted into the text. marks_before lists the pages that begin
    at the very start of the paragraph (several when pages carry no text)."""
    starts: dict[int, list] = {}
    for b in chapter.get("page_starts", []):
        starts.setdefault(b["para"], []).append(b)
    for i, para in enumerate(chapter.get("paragraphs", [])):
        text = str(para).strip()
        before = [b["page"] for b in starts.get(i, []) if b["offset"] == 0]
        inside = sorted((b for b in starts.get(i, []) if b["offset"] > 0),
                        key=lambda b: b["offset"], reverse=True)
        for b in inside:
            text = text[:b["offset"]] + PAGE_MARK.format(b["page"]) + text[b["offset"]:]
        yield before, text
