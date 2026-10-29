import React, { useCallback, useEffect, useState } from 'react';
import type { ManifestData, SiteInfo } from '../src/types';
import { ManifestTree } from '../src/components/ManifestTree';
import { useDownloadManager } from '../src/hooks/useDownloadManager';
import { HttpTransport } from '../src/transport/http-transport';
import { MockTransport } from './mock-transport';
import { BookCover, BookPreview } from './BookPreview';
import { bookFacts, coverUrl } from './book-info';
import { SearchPanel } from './SearchPanel';
import '../src/styles/components.css';
import './glass.css';

// Use ?mock=1 in URL to use MockTransport (local dev without server)
// Otherwise connects to the bookget server (same origin in production, or via Vite proxy in dev)
const useMock = new URLSearchParams(location.search).has('mock');
const transport = useMock ? new MockTransport() : new HttpTransport('');

/** Ask the server to show the OS folder dialog. null = cancelled. */
async function pickFolder(initial: string): Promise<string | null> {
  const r = await fetch('/api/pick-folder', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ initial }),
  });
  if (!r.ok) throw new Error(await r.text());
  return (await r.json()).path ?? null;
}

/** Path helpers that accept either separator (Windows or POSIX). */
const SEP = /[\\/]+/;
const trimSep = (p: string) => p.replace(/[\\/]+$/, '');
const baseName = (p: string) => trimSep(p).split(SEP).pop() || p;
const dirName = (p: string) => trimSep(p).replace(/[\\/][^\\/]*$/, '');
const joinPath = (dir: string, name: string) =>
  trimSep(dir) + (dir.includes('\\') ? '\\' : '/') + name;

interface Book {
  taskId: string;
  url: string;
  /** This book's own folder (`<保存位置>/<book_id>`). */
  dir: string;
  site: SiteInfo | null;
  /** Build an archival PDF after the download (`bookget download --pdf`). */
  pdf?: boolean;
}

/** Post JSON to the local server; throws with the server's error text. */
async function postJson<T>(path: string, body: unknown): Promise<T> {
  const r = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || `HTTP ${r.status}`);
  return data as T;
}

interface Pending extends Book {
  manifest: ManifestData;
}

export const App: React.FC = () => {
  // Ask the server where it will actually write files. Showing the literal
  // "./downloads" left users unable to find their downloads, because it
  // resolves against the server process's working directory (for a
  // double-clicked exe that's wherever Explorer started it).
  const [outputDir, setOutputDir] = useState<string | null>(null);

  useEffect(() => {
    if (useMock) {
      setOutputDir('./downloads');
      return;
    }
    let cancelled = false;
    fetch('/api/config')
      .then((r) => (r.ok ? r.json() : null))
      .then((cfg) => {
        if (!cancelled) setOutputDir(cfg?.defaultOutputDir || './downloads');
      })
      .catch(() => {
        if (!cancelled) setOutputDir('./downloads');
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const {
    manifests, activeDownloads,
    discover, startDownload, cancelDownload, expandNode, deleteNodes, setManifest,
  } = useDownloadManager(transport);

  const [tab, setTab] = useState<'link' | 'search'>('link');
  const [url, setUrl] = useState('');
  const [parsing, setParsing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState<Pending | null>(null);
  const [books, setBooks] = useState<Book[]>([]);
  const [openId, setOpenId] = useState<string | null>(null);
  // Last failure per task — the server reports it only as a task_error event.
  const [taskErrors, setTaskErrors] = useState<Record<string, string>>({});
  // Live percent from `progress` events. Sites without per-volume download
  // (e.g. 识典古籍) only update the manifest once the whole book is done.
  const [livePercent, setLivePercent] = useState<Record<string, number>>({});
  // Per task: building its PDF right now / where the finished PDF is.
  const [pdfBusy, setPdfBusy] = useState<Record<string, boolean>>({});
  const [pdfPaths, setPdfPaths] = useState<Record<string, string>>({});

  useEffect(() => transport.subscribe(ev => {
    if (ev.type === 'progress') {
      if (ev.data.phase === 'pdf') {
        setPdfBusy(prev => ({ ...prev, [ev.taskId]: true }));
        return;
      }
      const pct = Number(ev.data.percent);
      if (Number.isFinite(pct)) setLivePercent(prev => ({ ...prev, [ev.taskId]: pct }));
      return;
    }
    if (ev.type !== 'task_error' && ev.type !== 'task_completed') return;
    setLivePercent(({ [ev.taskId]: _, ...rest }) => rest);
    setPdfBusy(({ [ev.taskId]: _, ...rest }) => rest);
    if (ev.type === 'task_completed' && typeof ev.data.pdfPath === 'string') {
      const path = ev.data.pdfPath;
      setPdfPaths(prev => ({ ...prev, [ev.taskId]: path }));
    }
    const msg = ev.type === 'task_error' ? String(ev.data.message ?? '下载失败') : null;
    setTaskErrors(prev => {
      const next = { ...prev };
      if (msg) next[ev.taskId] = msg; else delete next[ev.taskId];
      return next;
    });
  }), []);

  const download = useCallback((b: Book, nodeIds?: string[], concurrency?: number) => {
    setTaskErrors(({ [b.taskId]: _, ...rest }) => rest);
    startDownload({ taskId: b.taskId, url: b.url, outputDir: b.dir, nodeIds, concurrency, pdf: b.pdf })
      .catch(e => setTaskErrors(prev => ({
        ...prev, [b.taskId]: e instanceof Error ? e.message : String(e),
      })));
  }, [startDownload]);

  const buildPdf = useCallback(async (b: Book) => {
    setTaskErrors(({ [b.taskId]: _, ...rest }) => rest);
    setPdfBusy(prev => ({ ...prev, [b.taskId]: true }));
    try {
      const { path } = await postJson<{ path: string }>('/api/pdf', { dir: b.dir });
      setPdfPaths(prev => ({ ...prev, [b.taskId]: path }));
    } catch (e) {
      setTaskErrors(prev => ({ ...prev, [b.taskId]: e instanceof Error ? e.message : String(e) }));
    } finally {
      setPdfBusy(({ [b.taskId]: _, ...rest }) => rest);
    }
  }, []);

  const reveal = useCallback((path: string) => {
    postJson('/api/open', { path }).catch(e => setError(e instanceof Error ? e.message : String(e)));
  }, []);

  const parse = useCallback(async (raw: string) => {
    const target = raw.trim();
    if (!target || parsing || !outputDir) return;
    setError(null);
    setParsing(true);
    try {
      const check = await transport.checkUrl(target);
      if (!check.supported) {
        setError('暂不支持这个网址');
        return;
      }
      const manifest = await discover({ url: target, outputRoot: outputDir, depth: 1 });
      const taskId = manifest.book_id || target;
      setManifest(taskId, manifest);
      setPending({
        taskId, url: target, manifest, site: check.site ?? null,
        dir: manifest.output_dir || outputDir,
      });
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setParsing(false);
    }
  }, [parsing, outputDir, discover, setManifest]);

  // A search result's 预览 runs the same parse → preview flow as a pasted link.
  const pickFromSearch = useCallback((u: string) => {
    setUrl(u);
    parse(u);
  }, [parse]);

  const chooseRoot = useCallback(async () => {
    try {
      const picked = await pickFolder(outputDir ?? '');
      if (picked) setOutputDir(picked);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [outputDir]);

  // In the preview: the book was already discovered into `<root>/<book_id>`,
  // so move that folder under the newly picked root.
  const changePendingDir = useCallback(async () => {
    if (!pending) return;
    try {
      const picked = await pickFolder(dirName(pending.dir));
      if (!picked) return;
      const dir = joinPath(picked, baseName(pending.dir));
      if (dir === pending.dir) return;
      await postJson('/api/move-book', { from: pending.dir, to: dir });
      setPending(p => (p ? { ...p, dir } : p));
      setOutputDir(picked);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [pending]);

  const addBook = useCallback((p: Pending) => {
    setBooks(prev => [{ taskId: p.taskId, url: p.url, dir: p.dir, site: p.site, pdf: p.pdf },
      ...prev.filter(b => b.taskId !== p.taskId)]);
    setOpenId(p.taskId);
    setPending(null);
    setUrl('');
  }, []);

  const handleConfirm = useCallback((concurrency: number, pdf: boolean) => {
    if (!pending) return;
    const book = { ...pending, pdf };
    download(book, undefined, concurrency);
    addBook(book);
  }, [pending, download, addBook]);

  return (
    <div className="g-app bdm-app">
      <div className="g-backdrop" aria-hidden="true">
        <span className="g-orb g-orb-a" />
        <span className="g-orb g-orb-b" />
        <span className="g-orb g-orb-c" />
      </div>

      <header className="g-toolbar">
        <div className="g-brand">
          <span className="g-brand-mark">籍</span>
          <span className="g-brand-name">Bookget</span>
        </div>
        <span className={`g-status ${useMock ? 'mock' : ''}`}>
          <i />{useMock ? 'Mock 模式' : '已连接'}
        </span>
      </header>

      <main className="g-main">
        <section className="g-hero">
          <h1>古籍下载</h1>
          <p>粘贴书籍链接，或按书名搜索；先预览，再下载。</p>

          <div className="g-segmented" role="tablist">
            <button role="tab" aria-selected={tab === 'link'} onClick={() => setTab('link')}>链接解析</button>
            <button role="tab" aria-selected={tab === 'search'} onClick={() => setTab('search')}
              disabled={useMock} title={useMock ? 'Mock 模式不可用' : ''}>搜索书名</button>
          </div>

          {tab === 'search' && !useMock && <SearchPanel onPick={pickFromSearch} onUseLink={() => setTab('link')} />}

          {tab === 'link' && (
          <form
            className="g-search"
            onSubmit={e => { e.preventDefault(); parse(url); }}
          >
            <svg viewBox="0 0 24 24" aria-hidden="true">
              <path d="M10 14a3.5 3.5 0 0 0 5 0l4-4a3.5 3.5 0 0 0-5-5l-.5.5M14 10a3.5 3.5 0 0 0-5 0l-4 4a3.5 3.5 0 0 0 5 5l.5-.5" />
            </svg>
            <input
              type="text"
              value={url}
              onChange={e => setUrl(e.target.value)}
              placeholder="https://www.shidianguji.com/book/…"
              spellCheck={false}
              autoFocus
            />
            <button className="g-btn g-btn-primary" type="submit" disabled={!url.trim() || parsing || !outputDir}>
              {parsing ? '解析中…' : '解析'}
            </button>
          </form>
          )}

          <label className="g-outdir">
            <span>保存位置</span>
            <input
              type="text"
              value={outputDir ?? ''}
              onChange={e => setOutputDir(e.target.value)}
              spellCheck={false}
            />
            {!useMock && (
              <button type="button" className="g-btn g-btn-plain g-btn-sm" onClick={chooseRoot}>
                选择…
              </button>
            )}
          </label>

          {error && <div className="g-error">{error}</div>}
        </section>

        {books.length > 0 && (
          <section className="g-library">
            <h3>下载列表</h3>
            {books.map(b => {
              const m = manifests[b.taskId];
              if (!m) return null;
              return (
                <BookRow
                  key={b.taskId}
                  manifest={m}
                  site={b.site}
                  downloading={activeDownloads.has(b.taskId)}
                  error={taskErrors[b.taskId] ?? null}
                  livePercent={livePercent[b.taskId]}
                  open={openId === b.taskId}
                  onToggle={() => setOpenId(id => (id === b.taskId ? null : b.taskId))}
                  onDownloadAll={c => download(b, undefined, c)}
                  onDownloadSelected={(ids, c) => download(b, ids, c)}
                  onExpandNode={id => expandNode({ taskId: b.taskId, url: b.url, outputDir: b.dir, nodeId: id })}
                  onCancel={() => cancelDownload(b.taskId)}
                  onDeleteNodes={ids => deleteNodes({ taskId: b.taskId, nodeIds: ids, outputDir: b.dir })}
                  dir={b.dir}
                  pdfBusy={!!pdfBusy[b.taskId]}
                  pdfPath={pdfPaths[b.taskId] ?? null}
                  onBuildPdf={() => buildPdf(b)}
                  onReveal={reveal}
                />
              );
            })}
          </section>
        )}
      </main>

      {parsing && (
        <div className="g-overlay">
          <div className="g-sheet g-sheet-loading">
            <span className="g-spinner" />
            <div>
              <strong>正在解析书籍信息</strong>
              <p>识典古籍需要启动内置浏览器，通常需要 10–30 秒。</p>
            </div>
          </div>
        </div>
      )}

      {pending && outputDir && (
        <BookPreview
          manifest={pending.manifest}
          site={pending.site}
          outputDir={pending.dir}
          onChangeDir={useMock ? undefined : changePendingDir}
          onConfirm={handleConfirm}
          onChooseParts={() => addBook(pending)}
          onCancel={() => setPending(null)}
        />
      )}
    </div>
  );
};

interface BookRowProps {
  manifest: ManifestData;
  site: SiteInfo | null;
  downloading: boolean;
  error: string | null;
  livePercent?: number;
  dir: string;
  pdfBusy: boolean;
  pdfPath: string | null;
  onBuildPdf: () => void;
  onReveal: (path: string) => void;
  open: boolean;
  onToggle: () => void;
  onDownloadAll: (concurrency: number) => void;
  onDownloadSelected: (nodeIds: string[], concurrency: number) => void;
  onExpandNode: (nodeId: string) => void;
  onCancel: () => void;
  onDeleteNodes: (nodeIds: string[]) => void;
}

const BookRow: React.FC<BookRowProps> = ({
  manifest, site, downloading, error, livePercent, open, onToggle,
  dir, pdfBusy, pdfPath, onBuildPdf, onReveal, ...treeProps
}) => {
  const facts = bookFacts(manifest);
  const p = manifest.progress ?? { total: 0, completed: 0, failed: 0, percent: 0 };
  const percent = downloading && livePercent != null ? Math.max(livePercent, p.percent) : p.percent;
  const done = p.total > 0 && p.completed === p.total;
  const failed = !downloading && (!!error || p.failed > 0);
  const state = pdfBusy ? '正在生成 PDF…' : downloading ? '下载中' : done ? '已完成'
    : error ? '下载失败' : p.failed > 0 ? `${p.failed} 项失败` : '等待下载';

  return (
    <div className={`g-card g-book ${open ? 'open' : ''}`}>
      <button className="g-book-head" onClick={onToggle}>
        <BookCover url={coverUrl(manifest)} title={facts.title} size="sm" />
        <div className="g-book-info">
          <div className="g-book-title">{facts.title}</div>
          <div className="g-book-meta">
            {[site?.site_name, facts.creators].filter(Boolean).join(' · ')}
          </div>
          <div className="g-progress">
            <span
              className={done ? 'done' : failed ? 'warn' : ''}
              style={{ width: `${percent}%` }}
            />
          </div>
          <div className="g-book-state">
            <span className={failed ? 'g-text-error' : ''}>{state}</span>
            <span>{downloading ? `${percent}%` : `${p.completed}/${p.total} · ${p.percent}%`}</span>
          </div>
        </div>
        <svg className="g-chevron" viewBox="0 0 24 24" aria-hidden="true"><path d="m9 6 6 6-6 6" /></svg>
      </button>
      {error && !downloading && <div className="g-book-error">{error}</div>}
      {!useMock && (
        <div className="g-book-actions">
          <button className="g-btn g-btn-plain g-btn-sm" onClick={() => onReveal(dir)}>打开文件夹</button>
          {done && (site?.supports_text ?? false) && (
            <button className="g-btn g-btn-plain g-btn-sm" title="带页码的 Markdown 全文"
              onClick={() => onReveal(joinPath(joinPath(dir, 'text'), 'full.md'))}>
              打开文字
            </button>
          )}
          {pdfPath ? (
            <button className="g-btn g-btn-tinted g-btn-sm" onClick={() => onReveal(pdfPath)}>打开 PDF</button>
          ) : (
            <button className="g-btn g-btn-tinted g-btn-sm" onClick={onBuildPdf}
              disabled={!done || downloading || pdfBusy}
              title={done ? '图片原样嵌入，附元数据、书签与资料表' : '下载完成后可用'}>
              {pdfBusy ? '生成中…' : '生成 PDF'}
            </button>
          )}
        </div>
      )}
      {open && (
        <div className="g-book-tree">
          <ManifestTree
            manifest={manifest}
            isDownloading={downloading}
            onDownloadAll={treeProps.onDownloadAll}
            onDownloadSelected={treeProps.onDownloadSelected}
            onExpandNode={treeProps.onExpandNode}
            onCancelDownload={treeProps.onCancel}
            onDeleteNodes={treeProps.onDeleteNodes}
          />
        </div>
      )}
    </div>
  );
};
