import { useState, useEffect, useCallback } from 'react';
import { formatApiError } from '../lib/errorMessages';
import { NetworkContext } from './networkContextValue';

export function NetworkProvider({ children }) {
  const [isOnline, setIsOnline] = useState(navigator.onLine);
  const [lastError, setLastError] = useState(null);

  useEffect(() => {
    const handleOnline = () => setIsOnline(true);
    const handleOffline = () => setIsOnline(false);

    window.addEventListener('online', handleOnline);
    window.addEventListener('offline', handleOffline);

    return () => {
      window.removeEventListener('online', handleOnline);
      window.removeEventListener('offline', handleOffline);
    };
  }, []);

  useEffect(() => {
    const handleAuthExpired = (event) => {
      setLastError(event.detail);
    };

    window.addEventListener('auth:expired', handleAuthExpired);
    return () => window.removeEventListener('auth:expired', handleAuthExpired);
  }, []);

  const clearError = useCallback(() => {
    setLastError(null);
  }, []);

  const handleError = useCallback((error) => {
    setLastError(error);
    return formatApiError(error);
  }, []);

  const value = {
    isOnline,
    lastError,
    clearError,
    handleError,
    formatError: formatApiError,
  };

  return (
    <NetworkContext.Provider value={value}>
      {children}
    </NetworkContext.Provider>
  );
}
