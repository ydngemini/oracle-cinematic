// Generic list panel — renders a scrollable .service-list of rows. Each panel
// supplies small functions: primary (name), optional badge, meta (secondary line),
// and optional onSelect (only ec2/rds/lambda rows are selectable). Reuses the
// existing .panel / .service-item CSS — no new styles.
export default function ListPanel({
  title,
  items,
  primary,
  badge,
  meta,
  onSelect,
  rowKey,
  empty = 'None',
  maxRows = 50,
  className = '',
}) {
  const list = items || [];
  const shown = list.slice(0, maxRows);
  const overflow = list.length - shown.length;

  return (
    <div className={`panel ${className}`}>
      <div className="panel-header">
        <h2>{title}</h2>
        <span className="panel-count">{list.length}</span>
      </div>
      <div className="service-list">
        {list.length === 0 ? (
          <div style={{ textAlign: 'center', padding: '18px', color: 'var(--text-secondary)', fontSize: '13px' }}>
            {empty}
          </div>
        ) : (
          <>
            {shown.map((item, i) => {
              const b = badge ? badge(item) : null;
              const m = meta ? meta(item) : null;
              const selectable = !!onSelect;
              return (
                <div
                  key={rowKey ? rowKey(item, i) : i}
                  className={`service-item ${selectable ? '' : 'static'}`}
                  onClick={selectable ? () => onSelect(item) : undefined}
                >
                  <div className="service-header">
                    <span className="service-name">{primary(item)}</span>
                    {b && <span className={`service-state ${b.className || ''}`}>{b.text}</span>}
                  </div>
                  {m && <div className="service-meta">{m}</div>}
                </div>
              );
            })}
            {overflow > 0 && (
              <div style={{ textAlign: 'center', padding: '8px', color: 'var(--text-secondary)', fontSize: '12px' }}>
                + {overflow} more
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}
