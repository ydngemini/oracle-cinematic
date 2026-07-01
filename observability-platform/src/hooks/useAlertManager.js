import { useCallback, useEffect, useRef } from 'react';
import { useAlertContext } from '../components/alerts/AlertSystem';
import useAudioAlertEngine from '../components/alerts/AudioAlertEngine';

export default function useAlertManager(options = {}) {
  const {
    autoPlayAudio = true,
    cooldownMs = 5000,
    maxAlertsPerType = 10,
    onAlertAdded,
    onAlertCleared
  } = options;

  const {
    addAlert,
    clearAlert,
    getActiveAlerts,
    setThreshold,
    getAlertCounts,
    checkThreshold,
    thresholds,
    alerts
  } = useAlertContext();

  const { playSeverity } = useAudioAlertEngine();
  const lastAlertTimeRef = useRef({});

  const addAlertWithCooldown = useCallback((severity, message, resource) => {
    const key = `${severity}-${resource}`;
    const now = Date.now();
    const lastTime = lastAlertTimeRef.current[key] || 0;

    if (now - lastTime < cooldownMs) {
      return null;
    }

    lastAlertTimeRef.current[key] = now;
    
    const activeByType = alerts.filter(a => a.severity === severity && a.active);
    if (activeByType.length >= maxAlertsPerType) {
      const oldest = activeByType[0];
      clearAlert(oldest.id);
    }

    addAlert(severity, message, resource);

    if (autoPlayAudio) {
      playSeverity(severity);
    }

    if (onAlertAdded) {
      onAlertAdded({ severity, message, resource });
    }

    return alerts[alerts.length - 1]?.id;
  }, [addAlert, clearAlert, alerts, autoPlayAudio, cooldownMs, maxAlertsPerType, onAlertAdded, playSeverity]);

  const clearAlertWithCallback = useCallback((id) => {
    clearAlert(id);
    if (onAlertCleared) {
      onAlertCleared(id);
    }
  }, [clearAlert, onAlertCleared]);

  const checkAndAlert = useCallback((service, metric, value, resource) => {
    const severity = checkThreshold(service, metric, value);
    if (severity) {
      const threshold = thresholds[service]?.[metric];
      const message = `${metric} at ${value} (threshold: ${threshold})`;
      addAlertWithCooldown(severity, message, resource);
    }
    return severity;
  }, [addAlertWithCooldown, checkThreshold, thresholds]);

  const getAlertsBySeverity = useCallback((severity) => {
    return alerts.filter(a => a.severity === severity && a.active);
  }, [alerts]);

  const getAlertsByResource = useCallback((resource) => {
    return alerts.filter(a => a.resource === resource && a.active);
  }, [alerts]);

  const clearAllAlerts = useCallback(() => {
    alerts.filter(a => a.active).forEach(a => clearAlert(a.id));
  }, [alerts, clearAlert]);

  const clearAlertsBySeverity = useCallback((severity) => {
    alerts.filter(a => a.severity === severity && a.active).forEach(a => clearAlert(a.id));
  }, [alerts, clearAlert]);

  return {
    addAlert: addAlertWithCooldown,
    clearAlert: clearAlertWithCallback,
    getActiveAlerts,
    setThreshold,
    getAlertCounts,
    checkThreshold,
    checkAndAlert,
    getAlertsBySeverity,
    getAlertsByResource,
    clearAllAlerts,
    clearAlertsBySeverity,
    thresholds,
    alerts
  };
}
