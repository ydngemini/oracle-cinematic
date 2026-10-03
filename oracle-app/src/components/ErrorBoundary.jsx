import { Component } from 'react';
import { AlertTriangle } from 'lucide-react';
import styles from './ErrorBoundary.module.css';

/**
 * Resilience boundary. Without this, a throw in ANY descendant (WebSocket
 * hook, the gsplat/WebGL viewers, DealPipeline) unmounts the whole tree and
 * leaves a blank screen. Catch it, keep the shell, offer a way back.
 *
 * - No `label`: the app-level boundary in main.jsx — takes the whole screen,
 *   because there is nothing left around it to keep.
 * - With `label` (`<ErrorBoundary label="record sheet">`): renders INLINE in
 *   the failed section only, so the rest of Neoh stays usable. (It used to be
 *   position: fixed for every boundary, so one broken card blanked the app.)
 *
 * The raw error message is kept out of the headline — it goes to the console
 * and behind a collapsed "Technical details" disclosure for support.
 */
export class ErrorBoundary extends Component {
  constructor(props) {
    super(props);
    this.state = { error: null };
  }

  static getDerivedStateFromError(error) {
    return { error };
  }

  componentDidCatch(error, info) {
    console.error(`[ErrorBoundary${this.props.label ? ` · ${this.props.label}` : ''}]`, error, info?.componentStack);
    this.props.onError?.(error, info);
  }

  handleReset = () => {
    this.setState({ error: null });
  };

  render() {
    if (this.state.error) {
      if (this.props.fallback) {
        return this.props.fallback(this.state.error, this.handleReset);
      }
      const scoped = Boolean(this.props.label);
      const technical = this.state.error?.message;
      return (
        <div className={scoped ? styles.inline : styles.boundary} role="alert">
          <div className={styles.panel}>
            <AlertTriangle className={styles.icon} size={22} aria-hidden="true" />
            <h2 className={styles.title}>
              {scoped ? 'This section hit a problem' : 'Something went wrong'}
            </h2>
            <p className={styles.detail}>
              {scoped
                ? 'The rest of Neoh still works, and anything you already saved is safe. Try again, or reload the page if it keeps happening.'
                : 'Neoh hit an unexpected problem. Anything you already saved is safe. Reload the page to continue.'}
            </p>
            <div className={styles.actions}>
              <button type="button" className={styles.retry} onClick={this.handleReset}>
                Try again
              </button>
              <button
                type="button"
                className={styles.reload}
                onClick={() => window.location.reload()}
              >
                Reload page
              </button>
            </div>
            {technical && (
              <details className={styles.technical}>
                <summary>Technical details</summary>
                <code>{scoped ? `${this.props.label}: ${technical}` : technical}</code>
              </details>
            )}
          </div>
        </div>
      );
    }
    return this.props.children;
  }
}
