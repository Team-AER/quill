import * as Sentry from '@sentry/browser';

const dsn = import.meta.env.VITE_GLITCHTIP_DSN || '';
if (dsn) {
  Sentry.init({
    dsn,
    tunnel: import.meta.env.VITE_GLITCHTIP_TUNNEL || undefined,
    environment: import.meta.env.VITE_SENTRY_ENVIRONMENT || 'production',
    release: import.meta.env.VITE_SENTRY_RELEASE || 'quill@0.1.0',
    sendDefaultPii: false,
    tracesSampleRate: 0,
    integrations: (defaults) => defaults.filter((i) => ['GlobalHandlers', 'InboundFilters', 'BrowserApiErrors', 'Dedupe'].includes(i.name)),
    beforeSend(event) {
      delete event.request;
      delete event.user;
      delete event.breadcrumbs;
      delete event.extra;
      for (const value of event.exception?.values || []) value.value = value.type || 'Error';
      return event;
    },
  });
}

export function reportError(error: unknown) {
  if (dsn) return Sentry.captureException(error);
}
