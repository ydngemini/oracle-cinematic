import { useEffect, useState } from 'react';
import { SiteDocument } from './SitePreview';

/** /sites/{brokerage}/{site}: a published site, for anyone (prompt 14 T14.05). */
function readRoute() {
  const parts = window.location.pathname.replace(/^\/sites\/+/, '').split('/').filter(Boolean);
  return { tenant: parts[0] || '', slug: parts[1] || '' };
}

export default function PublicSite() {
  const [{ tenant, slug }] = useState(readRoute);
  const [site, setSite] = useState(null);
  const [error, setError] = useState(() => (tenant && slug ? '' : 'This page does not exist.'));

  useEffect(() => {
    if (!tenant || !slug) return undefined;
    let live = true;
    fetch(`/api/public/sites/${encodeURIComponent(tenant)}/${encodeURIComponent(slug)}`, {
      credentials: 'omit',
      headers: { Accept: 'application/json' },
    }).then(async (res) => {
      if (!live) return;
      if (!res.ok) {
        setError(res.status === 404 ? 'This page is not published.' : 'This page could not be loaded right now.');
        return;
      }
      setSite(await res.json());
    }, () => { if (live) setError('This page could not be loaded right now.'); });
    return () => { live = false; };
  }, [tenant, slug]);

  return <SiteDocument site={site} error={error} mode="public" />;
}
