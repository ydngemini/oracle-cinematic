import { useEffect, useMemo, useRef, useState } from 'react';
import {
  geocodeAddress,
  getMapsMapId,
  hasMapsKey,
  loadMapsAndMarkers,
} from '../lib/google3d';
import { apiGet } from '../lib/apiClient';
import styles from './LeadMap.module.css';

const DEFAULT_CENTER = { lat: 39.8283, lng: -98.5795 };
const DEFAULT_ZOOM = 4;
const MAX_GEOCODES_PER_VIEW = 60;

// Geocoding has a billable quota. Cache resolved addresses for the current SPA
// session and only request locations we do not already receive from the API.
const geocodeCache = new Map();

function tier(score) {
  if (Number(score) >= 70) return 'hot';
  if (Number(score) >= 40) return 'warm';
  return 'cool';
}

function coordinate(value, minimum, maximum) {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed >= minimum && parsed <= maximum
    ? parsed
    : null;
}

function leadPosition(lead) {
  const latitude = coordinate(lead.latitude ?? lead.lat, -90, 90);
  const longitude = coordinate(lead.longitude ?? lead.lng ?? lead.lon, -180, 180);
  return latitude === null || longitude === null ? null : { lat: latitude, lng: longitude };
}

function leadAddress(lead) {
  return [lead.address, lead.city, lead.state].filter(Boolean).join(', ');
}

function markerTitle(lead) {
  const confidence = lead.location_confidence === 'source_coordinate'
    ? 'source coordinate'
    : 'Google address approximation';
  return `${leadAddress(lead) || lead.parcel_id} — public-record priority ${lead.motivation_score ?? 0} · ${confidence}`;
}

function geocodeOnce(address) {
  const key = address.trim().toLowerCase();
  if (!geocodeCache.has(key)) {
    geocodeCache.set(
      key,
      geocodeAddress(address)
        .then(({ lat, lng }) => ({ lat, lng }))
        .catch(() => null)
    );
  }
  return geocodeCache.get(key);
}

export function LeadMap({ leads, selected, onToggle, onOpen }) {
  const mapNodeRef = useRef(null);
  const mapRuntimeRef = useRef(null);
  const toggleRef = useRef(onToggle);
  const openRef = useRef(onOpen);
  const [runtimeReady, setRuntimeReady] = useState(false);
  const [status, setStatus] = useState(hasMapsKey() ? 'loading' : 'no-key');
  const [plottedLeads, setPlottedLeads] = useState([]);
  const [viewportLeads, setViewportLeads] = useState([]);

  useEffect(() => {
    toggleRef.current = onToggle;
  }, [onToggle]);

  useEffect(() => {
    openRef.current = onOpen;
  }, [onOpen]);

  // This view is lazy-loaded by DealPipeline, so the Maps JS API is requested
  // only when an operator opens the map rather than during normal dashboard use.
  useEffect(() => {
    if (!hasMapsKey()) return undefined;

    let cancelled = false;
    loadMapsAndMarkers()
      .then(({ google, Map, AdvancedMarkerElement }) => {
        if (cancelled || !mapNodeRef.current) return;
        const map = new Map(mapNodeRef.current, {
          center: DEFAULT_CENTER,
          zoom: DEFAULT_ZOOM,
          mapId: getMapsMapId(),
          gestureHandling: 'greedy',
          fullscreenControl: false,
          mapTypeControl: false,
          streetViewControl: false,
        });
        mapRuntimeRef.current = { google, map, AdvancedMarkerElement };
        setRuntimeReady(true);
        setStatus('ready');

        const refreshViewport = async () => {
          const bounds = map.getBounds();
          if (!bounds) return;
          const northEast = bounds.getNorthEast();
          const southWest = bounds.getSouthWest();
          const minLon = southWest.lng();
          const maxLon = northEast.lng();
          const minLat = southWest.lat();
          const maxLat = northEast.lat();
          if (maxLon - minLon > 180 || maxLat - minLat > 90) return;
          try {
            const data = await apiGet(`/api/v1/pipeline/map-clusters?bbox=${encodeURIComponent(
              `${minLon},${minLat},${maxLon},${maxLat}`
            )}&zoom=${map.getZoom()}&confidence_min=0`);
            const items = Array.isArray(data?.items) ? data.items : [];
            setViewportLeads(items.flatMap((item) => {
              const lat = Number(item.lat);
              const lng = Number(item.lon);
              if (!Number.isFinite(lat) || !Number.isFinite(lng)) return [];
              return [{
                lead: {
                  id: item.id,
                  parcel_id: item.id || item.cluster_id,
                  address: item.address || `${item.count || 1} public-record leads`,
                  motivation_score: item.priority ?? item.avg_motivation_score ?? 0,
                  location_confidence: 'source_coordinate',
                  cluster_count: item.count,
                },
                position: { lat, lng },
                locationConfidence: 'source_coordinate',
              }];
            }));
          } catch {
            // Keep the already-rendered local page visible if the viewport
            // query is unavailable or the session expires.
          }
        };
        const idleListener = map.addListener('idle', refreshViewport);
        mapRuntimeRef.current.idleListener = idleListener;
      })
      .catch(() => {
        if (!cancelled) setStatus('unavailable');
      });

    return () => {
      cancelled = true;
      mapRuntimeRef.current?.idleListener?.remove?.();
      mapRuntimeRef.current = null;
    };
  }, []);

  const directLeads = useMemo(
    () => leads.flatMap((lead) => {
      const position = leadPosition(lead);
      return position ? [{ lead, position, locationConfidence: 'source_coordinate' }] : [];
    }),
    [leads]
  );

  const leadsToGeocode = useMemo(
    () => leads
      .filter((lead) => !leadPosition(lead) && leadAddress(lead))
      .slice(0, MAX_GEOCODES_PER_VIEW),
    [leads]
  );

  useEffect(() => {
    if (!runtimeReady) return undefined;

    let cancelled = false;
    Promise.all(leadsToGeocode.map(async (lead) => {
      const position = await geocodeOnce(leadAddress(lead));
      return position ? { lead, position, locationConfidence: 'address_approximation' } : null;
    })).then((geocoded) => {
      if (cancelled) return;
      setPlottedLeads([...directLeads, ...geocoded.filter(Boolean)]);
      setStatus('ready');
    });

    return () => {
      cancelled = true;
    };
  }, [directLeads, leadsToGeocode, runtimeReady]);

  useEffect(() => {
    const runtime = mapRuntimeRef.current;
    if (!runtime || plottedLeads.length === 0) return;

    const bounds = new runtime.google.maps.LatLngBounds();
    plottedLeads.forEach(({ position }) => bounds.extend(position));
    if (plottedLeads.length === 1) {
      runtime.map.setCenter(plottedLeads[0].position);
      runtime.map.setZoom(15);
    } else {
      runtime.map.fitBounds(bounds, 56);
    }
  }, [plottedLeads]);

  useEffect(() => {
    const runtime = mapRuntimeRef.current;
    if (!runtime) return undefined;

    const markers = (viewportLeads.length ? viewportLeads : plottedLeads)
      .slice(0, 250)
      .map(({ lead, position, locationConfidence }) => {
      const dot = document.createElement('span');
      dot.className = styles.marker;
      dot.dataset.tier = tier(lead.motivation_score);
      dot.dataset.selected = String(selected.has(lead.parcel_id));
      dot.dataset.location = locationConfidence;

      const marker = new runtime.AdvancedMarkerElement({
        map: runtime.map,
        position,
        title: markerTitle(lead),
        gmpClickable: true,
        zIndex: selected.has(lead.parcel_id) ? 2 : 1,
      });
      marker.append(dot);
      marker.addEventListener('gmp-click', () => {
        if (openRef.current) openRef.current(lead.id || lead.parcel_id);
        else toggleRef.current(lead.parcel_id);
      });
      return marker;
      });

    return () => {
      markers.forEach((marker) => {
        marker.map = null;
        marker.replaceChildren();
      });
    };
  }, [plottedLeads, selected, viewportLeads]);

  const statusMessage = status === 'no-key'
    ? 'Google Maps is not configured for this deployment.'
    : status === 'loading'
      ? 'Loading Google Maps…'
      : status === 'unavailable'
        ? 'Google Maps could not be reached. Check the browser key restrictions.'
        : plottedLeads.length === 0
          ? 'No verified coordinates or usable addresses are available for these leads.'
          : null;

  return (
    <section className={styles.map} aria-label="Google map of filtered pipeline leads">
      <div ref={mapNodeRef} className={styles.canvas} />
      {statusMessage ? (
        <p className={styles.status} role="status">{statusMessage}</p>
      ) : null}
      <span className={styles.legend} aria-hidden="true">
        <i data-tier="hot" /> priority
        <i data-tier="warm" /> active
        <i data-tier="cool" /> other
        <b data-location="source_coordinate" /> source coordinate
        <b data-location="address_approximation" /> Google approximation
      </span>
    </section>
  );
}
