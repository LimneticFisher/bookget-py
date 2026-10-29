import React, { useEffect, useState } from 'react';
import type { ManifestData, SiteInfo } from '../src/types';
import { bookFacts, coverUrl } from './book-info';

export interface BookPreviewProps {
  manifest: ManifestData;
  site: SiteInfo | null;
  outputDir: string;
  /** Opens the OS folder dialog; absent in mock mode. */
  onChangeDir?: () => void;
  onConfirm: (concurrency: number, pdf: boolean) => void;
  onChooseParts: () => void;
  onCancel: () => void;
}

/** Cover image, falling back to a traditional thread-bound cover with a title slip. */
export const BookCover: React.FC<{ url: string | null; title: string; size?: 'lg' | 'sm' }> = ({
  url, title, size = 'lg',
}) => {
  const [failed, setFailed] = useState(false);
  useEffect(() => setFailed(false), [url]);

  return (
    <div className={`g-cover g-cover-${size}`}>
      {url && !failed ? (
        <img src={url} alt={title} referrerPolicy="no-referrer" onError={() => setFailed(true)} />
      ) : (
        <div className="g-cover-fallback">
          <span className="g-cover-stitch" />
          <span className="g-cover-slip">{title.slice(0, 12)}</span>
        </div>
      )}
    </div>
  );
};

export const BookPreview: React.FC<BookPreviewProps> = ({
  manifest, site, outputDir, onChangeDir, onConfirm, onChooseParts, onCancel,
}) => {
  const [concurrency, setConcurrency] = useState(3);
  const [pdf, setPdf] = useState(false);
  const facts = bookFacts(manifest);
  const cover = coverUrl(manifest);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onCancel(); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onCancel]);

  const counts = [
    facts.sectionCount > 0 && `${facts.sectionCount} 个分卷`,
    facts.imageCount > 0 && `${facts.imageCount} 张图片`,
    facts.textCount > 0 && '含全文',
  ].filter(Boolean) as string[];

  return (
    <div className="g-overlay" onClick={onCancel}>
      <div className="g-sheet" role="dialog" aria-modal="true" onClick={e => e.stopPropagation()}>
        <BookCover url={cover} title={facts.title} />

        <div className="g-sheet-body">
          {site && <span className="g-chip">{site.site_name}</span>}
          <h2 className="g-sheet-title">{facts.title}</h2>
          {facts.altTitles.length > 0 && (
            <div className="g-sheet-sub">又名 {facts.altTitles.join('、')}</div>
          )}
          {facts.creators && <div className="g-sheet-creators">{facts.creators}</div>}
          {facts.abstract && <p className="g-abstract">{facts.abstract}</p>}

          {facts.rows.length > 0 && (
            <dl className="g-facts">
              {facts.rows.map(r => (
                <React.Fragment key={r.label}>
                  <dt>{r.label}</dt>
                  <dd>{r.value}</dd>
                </React.Fragment>
              ))}
            </dl>
          )}

          {counts.length > 0 && (
            <div className="g-counts">
              {counts.map(c => <span key={c}>{c}</span>)}
            </div>
          )}

          <div className="g-dest">
            <span>保存到</span>
            <code title={outputDir}>{outputDir}</code>
            {onChangeDir && (
              <button className="g-link" onClick={onChangeDir}>更改…</button>
            )}
          </div>

          {onChangeDir && (
            <label className="g-toggle">
              <input type="checkbox" checked={pdf} onChange={e => setPdf(e.target.checked)} />
              <span className="g-toggle-track" aria-hidden="true"><span /></span>
              <span>
                下载后生成 PDF 归档
                <small>图片原样嵌入，附元数据、书签与资料表</small>
              </span>
            </label>
          )}

          <div className="g-sheet-actions">
            <label className="g-stepper" title="同时下载的数量">
              并行
              <button onClick={() => setConcurrency(c => Math.max(1, c - 1))} aria-label="减少">−</button>
              <span>{concurrency}</span>
              <button onClick={() => setConcurrency(c => Math.min(20, c + 1))} aria-label="增加">+</button>
            </label>
            <span className="g-spacer" />
            <button className="g-btn g-btn-plain" onClick={onCancel}>取消</button>
            <button className="g-btn g-btn-tinted" onClick={onChooseParts}>选择分卷</button>
            <button className="g-btn g-btn-primary" onClick={() => onConfirm(concurrency, pdf)} autoFocus>
              确认下载
            </button>
          </div>
        </div>
      </div>
    </div>
  );
};
