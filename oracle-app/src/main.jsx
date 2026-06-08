import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { OracleProvider } from './state';
import { ErrorBoundary } from './components';
import App from './App.jsx';
import './index.css';

createRoot(document.getElementById('root')).render(
  <StrictMode>
    <ErrorBoundary>
      <OracleProvider>
        <App />
      </OracleProvider>
    </ErrorBoundary>
  </StrictMode>
);
