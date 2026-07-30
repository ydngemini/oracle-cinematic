export const ERROR_MESSAGES = {
  NETWORK_ERROR: 'Unable to connect. Please check your internet connection.',
  TIMEOUT: 'Request timed out. Please try again.',
  SERVER_ERROR: 'Server is temporarily unavailable. Please try again in a moment.',
  UNAUTHORIZED: 'Session expired. Redirecting to login...',
  FORBIDDEN: 'You do not have permission to perform this action.',
  NOT_FOUND: 'The requested resource was not found.',
  VALIDATION_ERROR: 'Invalid request. Please check your input.',
  DEFAULT: 'Something went wrong. Please try again.',
};

const STATUS_MESSAGES = {
  400: ERROR_MESSAGES.VALIDATION_ERROR,
  401: ERROR_MESSAGES.UNAUTHORIZED,
  403: ERROR_MESSAGES.FORBIDDEN,
  404: ERROR_MESSAGES.NOT_FOUND,
  408: 'Request timed out. Please try again.',
  429: 'Too many requests. Please wait a moment and try again.',
};

export function formatApiError(error) {
  if (!error) return ERROR_MESSAGES.DEFAULT;

  if (error.isNetworkError) {
    return ERROR_MESSAGES.NETWORK_ERROR;
  }

  if (error.status === 401) {
    return ERROR_MESSAGES.UNAUTHORIZED;
  }

  if (STATUS_MESSAGES[error.status]) {
    return STATUS_MESSAGES[error.status];
  }

  if (error.status >= 500) {
    return ERROR_MESSAGES.SERVER_ERROR;
  }

  if (error.message && error.message !== 'Network error - please check your connection') {
    return error.message;
  }

  return ERROR_MESSAGES.DEFAULT;
}

export function isAuthError(error) {
  return error?.status === 401;
}

export function isNetworkError(error) {
  return error?.isNetworkError === true;
}

export function isRetryableError(error) {
  if (error?.isNetworkError) return true;
  const status = error?.status;
  if (!status) return false;
  if (status === 408 || status === 429) return true;
  if (status >= 500 && status < 600) return true;
  return false;
}
