import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { OracleProvider } from './state';
import { ErrorBoundary } from './components';
import App from './App.jsx';
import './index.css';
import { applyTheme, readTheme } from './theme';

// Belt and braces with the inline stamp in index.html: this also sets the
// browser chrome colour, which the inline script leaves alone.
applyTheme(readTheme());

createRoot(document.getElementById('root')).render(
  <StrictMode>
    <ErrorBoundary>
      <OracleProvider>
        <App />
      </OracleProvider>
    </ErrorBoundary>
  </StrictMode>
);
