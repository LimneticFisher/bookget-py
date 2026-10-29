import React, { useEffect, useState } from 'react';

interface SearchSite {
  site_id: string;
  site_name: string;
  region: string;
  home: string;
  supports_search: boolean;
  supports_match: boolean;
  supports_images: boolean;
  supports_text: boolean;
  supports_pdf: boolean;
}

/** "图 · 文 · PDF" capability marks for the picker. */
const caps = (s: SearchSite) =>
  [s.supports_images && '图', s.supports_text && '文', s.supports_pdf && 'PDF'].filter(Boolean).join('·');

interface Hit {
  title: string;
  url: string;
  snippet?: string;
  cover_url?: string;
  versions?: Hit[];
}

interface Match { id: string; name: string; url: string; details?: string; type?: string }

type Mode = 'search' | 'match';

const PAGE = 20;

async function getJson<T>(url: string): Promise<T> {
  const r = await fetch(url);
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(body.error || `HTTP ${r.status}`);
  return body as T;
}

/** Search a site by title (`bookget search`) or exact-match title + authors (`bookget match`). */
export const SearchPanel: React.FC<{
  onPick: (url: string) => void;
  /** Switch to the link tab (for sites that can't be searched in-app). */
  onUseLink: () => void;
}> = ({ onPick, onUseLink }) => {
  const [sites, setSites] = useState<SearchSite[]>([]);
  const [site, setSite] = useState('shidianguji');
  const [mode, setMode] = useState<Mode>('search');
  const [query, setQuery] = useState('');
  const [authors, setAuthors] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [hits, setHits] = useState<Hit[] | null>(null);
  const [matches, setMatches] = useState<Match[] | null>(null);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [hasMore, setHasMore] = useState(false);

  useEffect(() => {
    getJson<SearchSite[]>('/api/search/sites')
      .then(list => {
        setSites(list);
        if (list.length && !list.some(s => s.site_id === 'shidianguji')) setSite(list[0].site_id);
      })
      .catch(e => setError(String(e.message ?? e)));
  }, []);

  const current = sites.find(s => s.site_id === site);
  const canSearch = !!current?.supports_search;
  const canMatch = !!current?.supports_match;
  const regions = [...new Set(sites.map(s => s.region))];
  useEffect(() => { if (!canMatch && mode === 'match') setMode('search'); }, [canMatch, mode]);

  const run = async (nextOffset = 0) => {
    const q = query.trim();
    if (!q || busy || !canSearch) return;
    setBusy(true);
    setError(null);
    try {
      if (mode === 'search') {
        const r = await getJson<{ results: Hit[]; total_hits: number; has_more: boolean }>(
          `/api/search?site=${encodeURIComponent(site)}&q=${encodeURIComponent(q)}&limit=${PAGE}&offset=${nextOffset}`);
        setHits(prev => (nextOffset > 0 && prev ? [...prev, ...r.results] : r.results));
        setMatches(null);
        setTotal(r.total_hits);
        setHasMore(r.has_more);
        setOffset(nextOffset);
      } else {
        const r = await getJson<{ results: Match[] }>(
          `/api/match?site=${encodeURIComponent(site)}&title=${encodeURIComponent(q)}&authors=${encodeURIComponent(authors)}`);
        setMatches(r.results);
        setHits(null);
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const empty = (hits && hits.length === 0) || (matches && matches.length === 0);

  return (
    <div className="g-searchpanel">
      <form className="g-search g-search-tall" onSubmit={e => { e.preventDefault(); run(0); }}>
        <select value={site} onChange={e => { setSite(e.target.value); setHits(null); setMatches(null); }}
          aria-label="站点">
          {regions.map(r => (
            <optgroup key={r} label={r}>
              {sites.filter(s => s.region === r).map(s => (
                <option key={s.site_id} value={s.site_id}>
                  {s.supports_search ? '🔍 ' : ''}{s.site_name}{caps(s) ? `（${caps(s)}）` : ''}
                </option>
              ))}
            </optgroup>
          ))}
        </select>
        <span className="g-divider" />
        {!canSearch && current ? (
          <span className="g-nosearch">该站点不支持站内搜索，请在网站上找到书后粘贴链接</span>
        ) : (
        <input
          type="text"
          value={query}
          onChange={e => setQuery(e.target.value)}
          placeholder={mode === 'search' ? '书名，如：周易' : '准确书名'}
          spellCheck={false}
        />
        )}
        {mode === 'match' && (
          <>
            <span className="g-divider" />
            <input
              className="g-authors"
              type="text"
              value={authors}
              onChange={e => setAuthors(e.target.value)}
              placeholder="作者（可选，逗号分隔）"
              spellCheck={false}
            />
          </>
        )}
        {canSearch || !current ? (
          <button className="g-btn g-btn-primary" type="submit" disabled={!query.trim() || busy}>
            {busy ? '搜索中…' : mode === 'search' ? '搜索' : '匹配'}
          </button>
        ) : current.home ? (
          <a className="g-btn g-btn-primary" href={current.home} target="_blank" rel="noreferrer">
            去站点找书
          </a>
        ) : null}
      </form>

      {!canSearch && current && (
        <div className="g-hint">
          {current.home
            ? <>在 {current.site_name} 打开书籍页面，复制地址栏链接，然后 </>
            : <>粘贴任意 IIIF Manifest 链接，</>}
          <button className="g-link" onClick={onUseLink}>切换到「链接解析」</button>
        </div>
      )}

      {canSearch && <div className="g-searchopts">
        <div className="g-segmented g-segmented-sm" role="tablist">
          <button role="tab" aria-selected={mode === 'search'} onClick={() => setMode('search')}>模糊搜索</button>
          <button role="tab" aria-selected={mode === 'match'} disabled={!canMatch}
            title={canMatch ? '' : '该站点不支持精确匹配'} onClick={() => setMode('match')}>
            精确匹配书名和作者
          </button>
        </div>
        {hits && <span className="g-muted">共 {total} 条</span>}
      </div>}

      {error && <div className="g-error">{error}</div>}
      {empty && !busy && <div className="g-empty">没有找到结果</div>}

      {hits && hits.length > 0 && (
        <div className="g-results">
          {hits.map((h, i) => (
            <HitRow key={`${h.url}-${i}`} hit={h} onPick={onPick} />
          ))}
          {hasMore && (
            <button className="g-btn g-btn-plain g-more" disabled={busy} onClick={() => run(offset + PAGE)}>
              {busy ? '加载中…' : '加载更多'}
            </button>
          )}
        </div>
      )}

      {matches && matches.length > 0 && (
        <div className="g-results">
          {matches.map(m => (
            <div key={m.id + m.url} className="g-card g-hit">
              <div className="g-hit-body">
                <div className="g-hit-title">{m.name}</div>
                {m.details && <div className="g-hit-snippet">{m.details}</div>}
                <div className="g-hit-url">{m.url}</div>
              </div>
              <button className="g-btn g-btn-tinted g-btn-sm" onClick={() => onPick(m.url)}>预览</button>
            </div>
          ))}
        </div>
      )}
    </div>
  );
};

const HitRow: React.FC<{ hit: Hit; onPick: (url: string) => void }> = ({ hit, onPick }) => (
  <div className="g-card g-hit">
    {hit.cover_url && <img className="g-hit-cover" src={hit.cover_url} alt="" referrerPolicy="no-referrer" />}
    <div className="g-hit-body">
      <div className="g-hit-title">{hit.title}</div>
      {hit.snippet && <div className="g-hit-snippet">{hit.snippet}</div>}
      {hit.versions && hit.versions.length > 0 && (
        <div className="g-versions">
          {hit.versions.map(v => (
            <button key={v.url} className="g-chip g-chip-btn" onClick={() => onPick(v.url)} title={v.url}>
              {v.title}
            </button>
          ))}
        </div>
      )}
    </div>
    {hit.url && (
      <button className="g-btn g-btn-tinted g-btn-sm" onClick={() => onPick(hit.url)}>预览</button>
    )}
  </div>
);
