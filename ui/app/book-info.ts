import type { ManifestData, ManifestNode } from '../src/types';

/** The site's cover if it gave one, else the first page image in the tree. */
export function coverUrl(m: ManifestData): string | null {
  const c = (m.metadata as { cover_url?: unknown } | undefined)?.cover_url;
  return typeof c === 'string' && c ? c : findCoverUrl(m.structure);
}

/** First image URL found in the manifest tree. */
function findCoverUrl(node: ManifestNode | undefined): string | null {
  if (!node) return null;
  const sd = node.source_data as
    | { images?: { url?: string }[]; image_urls?: string[] }
    | undefined;
  const url = sd?.images?.[0]?.url || sd?.image_urls?.[0];
  if (url) return url;
  for (const child of node.children ?? []) {
    const found = findCoverUrl(child);
    if (found) return found;
  }
  return null;
}

export interface BookFacts {
  title: string;
  altTitles: string[];
  creators: string;
  abstract: string;
  rows: { label: string; value: string }[];
  imageCount: number;
  textCount: number;
  sectionCount: number;
}

interface Creator { name?: string; role?: string; dynasty?: string }

/** Pull display-friendly facts out of a manifest's loosely-typed metadata. */
export function bookFacts(m: ManifestData): BookFacts {
  const md = (m.metadata ?? {}) as Record<string, unknown>;
  const str = (v: unknown) => (typeof v === 'string' ? v.trim() : v != null ? String(v) : '');

  const creators = Array.isArray(md.creators)
    ? (md.creators as Creator[])
        .map(c => [c.dynasty && `〔${c.dynasty}〕`, c.name, c.role].filter(Boolean).join(' '))
        .filter(Boolean)
        .join('；')
    : str(md.author);

  const rows: { label: string; value: string }[] = [];
  const push = (label: string, v: unknown) => {
    const s = str(v);
    if (s && s !== '0') rows.push({ label, value: s });
  };
  push('朝代', md.dynasty);
  push('年代', md.date);
  push('分类', md.category);
  push('卷数', md.volumes);
  push('页数', md.pages);
  push('装帧', md.binding);
  push('收藏', md.collection_unit);
  // Adapters put "版本: …"-style lines in notes; split them into rows.
  if (Array.isArray(md.notes)) {
    for (const n of md.notes as unknown[]) {
      const [k, ...rest] = str(n).split(/[:：]\s*/);
      if (rest.length) push(k, rest.join('：'));
    }
  }

  const root = m.structure;
  return {
    title: m.title || str(md.title) || m.book_id,
    altTitles: Array.isArray(md.alt_titles) ? (md.alt_titles as string[]).filter(Boolean) : [],
    creators,
    abstract: str(md.abstract),
    rows,
    imageCount: root?.image_count ?? 0,
    textCount: root?.text_count ?? 0,
    sectionCount: root?.children?.length ?? root?.children_count ?? 0,
  };
}
