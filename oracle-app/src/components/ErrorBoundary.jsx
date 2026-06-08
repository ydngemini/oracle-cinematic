import { Component } from 'react';
import styles from './ErrorBoundary.module.css';

/**
 * App-level resilience boundary. Without this, a throw in ANY descendant
 * (WebSocket hook, Jarvis SpeechRecognition, PropertyCanvas/gsplat, DealPipeline)
 * unmounts the whole tree and leaves a black screen. Catch it, keep the shell,
 * and offer a recovery path instead of a dead page.
 *
 * Scope can be narrowed by wrapping individual panels: `<ErrorBoundary label="Spatial Viewer">`.
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
      return (
        <div className={styles.boundary} role="alert">
          <div className={styles.panel}>
            <span className={styles.tag}>
              {this.props.label ? `${this.props.label} — Fault` : 'System Fault'}
            </span>
            <h2 className={styles.title}>A subsystem failed</h2>
            <p className={styles.detail}>
              {this.state.error?.message || 'An unexpected error interrupted the interface.'}
            </p>
            <div className={styles.actions}>
              <button type="button" className={styles.retry} onClick={this.handleReset}>
                RETRY SUBSYSTEM
              </button>
              <button
                type="button"
                className={styles.reload}
                onClick={() => window.location.reload()}
              >
                RELOAD CONSOLE
              </button>
            </div>
          </div>
        </div>
      );
    }
    return this.props.children;
  }
}
